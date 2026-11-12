"""The typed difference between two blueprints, in one sentence (headline) and exactly (plan, `terraform plan`-style)."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .extract import Code, matches
from .sources import _trim, git

IMPACT_WORDS = {
    "new-component": "adds a part", "removed-component": "removes a part",
    "new-interface": "adds an endpoint", "breaking-interface": "removes an endpoint callers may rely on",
    "new-external": "talks to an outside system it did not before", "removed-external": "stops using an outside system",
    "data-schema-change": "changes stored data", "deployment-change": "changes what is deployed",
    "new-flow": "connects parts that were not connected", "removed-flow": "disconnects parts",
    "new-config": "reads new settings", "test-gap": "changes code without changing tests",
}


def files_word(n: int) -> str:
    return f"{n} file" + ("" if n == 1 else "s")


def is_test_path(path: str, code: Optional[Code] = None) -> bool:
    """A test file: one the config's `tests` globs name, under tests/, or named like `x.test.ts`."""
    return path.startswith("tests/") or ".test." in path or bool(code and code.is_test(path))


_NUMSTATS: Dict[tuple, Dict[str, Tuple[int, int]]] = {}
_PAIRS: Dict[Tuple[str, str], Tuple[int, int]] = {}


def numstat(root: Path, base: str, head: Optional[str], cfg: Dict[str, Any]) -> Dict[str, tuple]:
    """{path: (lines added, lines removed, owning part or None)} for every file the change touched; `head` None is
    the working tree. Two commits: `git diff --numstat -M`, asked once per pair (commits never change). The resident
    engine's working tree (a "live:" id): counted in Python from the files whose blob differs (line_counts)."""
    from .sources import GitSource, is_live
    code = Code(cfg)
    if is_live(base) or is_live(head):
        counts = line_counts(GitSource(base, root), GitSource(head, root))
    else:
        key = (str(root), base, head)
        counts = _NUMSTATS.get(key) if head else None
        if counts is None:
            counts = {}
            for line in git("diff", "--numstat", "-M", base, *([head] if head else []), cwd=root).splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                plus, minus, path = parts
                path = re.sub(r"\{([^}]*) => ([^}]*)\}", r"\2", path).split(" => ")[-1]
                counts[path] = (int(plus) if plus.isdigit() else 0, int(minus) if minus.isdigit() else 0)
            if head:
                _NUMSTATS[key] = counts
                _trim(_NUMSTATS, 64)
    out = {}
    for path, (plus, minus) in counts.items():
        comp = code.part_of(path) if code.is_code(path) and not is_test_path(path, code) else None
        out[path] = (plus, minus, comp)
    return out


BIG = 20000   # lines: a file longer than this is counted roughly (a multiset of lines), not diffed


def line_counts(a, b) -> Dict[str, Tuple[int, int]]:
    """{path: (added, removed)} between two sources (files, blobs, read_many), as `git diff --numstat -M` says it for
    the usual change: only the files whose blob differs are read; a file moved unchanged counts as renamed (0, 0) under
    its new path; a binary file as (0, 0). Counts per pair of blobs are kept, so an edit counts only its own file."""
    ba, bb = a.blobs, b.blobs
    changed = [p for p in set(ba) | set(bb) if ba.get(p) != bb.get(p)]
    added = [p for p in changed if p not in ba]
    gone = {ba[p]: p for p in changed if p not in bb}
    out: Dict[str, Tuple[int, int]] = {}
    for p in added:
        old = gone.pop(bb[p], None)
        if old is not None:
            out[p] = (0, 0)   # renamed, unchanged
    gone_paths = set(gone.values())
    todo = [p for p in changed if p not in out and (p in bb or p in gone_paths)]
    unknown = [p for p in todo if (ba.get(p, ""), bb.get(p, "")) not in _PAIRS]
    ta = a.read_many([p for p in unknown if p in ba]) if unknown else {}
    tb = b.read_many([p for p in unknown if p in bb]) if unknown else {}
    for p in todo:
        key = (ba.get(p, ""), bb.get(p, ""))
        got = _PAIRS.get(key)
        if got is None:
            got = _PAIRS[key] = _count(ta.get(p), tb.get(p))
            _trim(_PAIRS, 20000)
        out[p] = got
    return out


def _count(x: Optional[str], y: Optional[str]) -> Tuple[int, int]:
    x, y = x or "", y or ""
    if "\0" in x[:8000] or "\0" in y[:8000]:
        return (0, 0)   # binary: git says "-"
    xs, ys = x.splitlines(keepends=True), y.splitlines(keepends=True)
    i = 0   # what is the same at both ends needs no diff
    while i < len(xs) and i < len(ys) and xs[i] == ys[i]:
        i += 1
    j = 0
    while j < len(xs) - i and j < len(ys) - i and xs[-1 - j] == ys[-1 - j]:
        j += 1
    xs, ys = xs[i:len(xs) - j], ys[i:len(ys) - j]
    if not xs or not ys:
        return (len(ys), len(xs))
    if len(xs) + len(ys) > BIG:
        from collections import Counter as _C
        cx, cy = _C(xs), _C(ys)
        return (sum((cy - cx).values()), sum((cx - cy).values()))
    import difflib
    same = sum(m.size for m in difflib.SequenceMatcher(None, xs, ys, autojunk=False).get_matching_blocks())
    return (len(ys) - same, len(xs) - same)


def diff(a: dict, b: dict, numstat: Optional[Dict[str, tuple]] = None, deploy_globs: Iterable[str] = (),
         code: Optional[Code] = None) -> dict:
    changes: List[dict] = []

    def add(op, entity, key, label="", detail=(), impact=()):
        changes.append({"op": op, "entity": entity, "id": key, "label": label or key,
                        "detail": list(detail), "impact": list(impact)})

    ca, cb = a["components"], b["components"]
    lines = defaultdict(lambda: [0, 0, 0])
    code_changed = tests_changed = False
    for path, (plus, minus, comp) in (numstat or {}).items():
        if comp:
            lines[comp][0] += plus
            lines[comp][1] += minus
            lines[comp][2] += 1
            code_changed = True
        elif is_test_path(path, code):
            tests_changed = True
    for cid in sorted(set(ca) | set(cb)):
        if cid not in ca:
            add("add", "component", cid, cb[cid]["label"], [files_word(cb[cid]["files"])], ["new-component"])
        elif cid not in cb:
            add("remove", "component", cid, ca[cid]["label"], [], ["removed-component"])
        elif cid in lines:
            p, m, n = lines[cid]
            add("change", "component", cid, cb[cid]["label"], [f"+{p} −{m} lines in {files_word(n)}"])
    for kind, impact_add, impact_remove in (("flows", "new-flow", "removed-flow"), ("api", "new-flow", "removed-flow"),
                                            ("internal", "new-flow", "removed-flow")):
        for key in sorted(set(a[kind]) ^ set(b[kind])):
            src, dst = key.split(">")
            label = f"{name_of(b, a, src)} → {name_of(b, a, dst)}"
            add("add" if key in b[kind] else "remove", "flow", key, label, [],
                [impact_add if key in b[kind] else impact_remove])
    for route in sorted(set(a["routes"]) ^ set(b["routes"])):
        if route in b["routes"]:
            add("add", "route", route, route, [name_of(b, a, b["routes"][route])], ["new-interface"])
        else:
            add("remove", "route", route, route, [name_of(b, a, a["routes"][route])], ["breaking-interface"])
    pairs = lambda bp: {(e, u) for e, v in bp["externals"].items() for u in v["used_by"]}  # noqa: E731
    for ext, user in sorted(pairs(a) ^ pairs(b)):
        new = (ext, user) in pairs(b)
        meta = (b if new else a)["externals"][ext]
        add("add" if new else "remove", "external", f"{user}>{ext}", f"{name_of(b, a, user)} → {meta['label']}", [],
            ["new-external" if new else "removed-external"])
    for name in sorted(set(a["containers"]) | set(b["containers"])):
        x, y = a["containers"].get(name), b["containers"].get(name)
        if x != y:
            op = "add" if x is None else "remove" if y is None else "change"
            detail = [] if op != "change" else [f"{k}: {x.get(k)!r} → {y.get(k)!r}" for k in y if x.get(k) != y.get(k)]
            add(op, "container", name, name, detail, ["deployment-change"])
    for table in sorted(set(a["tables"]) | set(b["tables"])):
        if table not in a["tables"]:
            add("add", "table", table, table, [], ["data-schema-change"])
        elif table not in b["tables"]:
            add("remove", "table", table, table, [], ["data-schema-change"])
        else:
            cols = sorted(set(b["tables"][table]) - set(a["tables"][table]))
            if cols:
                add("change", "table", table, table, [f"+ column {c}" for c in cols], ["data-schema-change"])
    for m in sorted(set(b["migrations"]) - set(a["migrations"])):
        add("add", "migration", m, m, [], ["data-schema-change"])
    for path, (plus, minus, _comp) in sorted((numstat or {}).items()):
        if matches(path, deploy_globs):
            add("change", "deploy", path, path, [f"+{plus} −{minus} lines"], ["deployment-change"])
    for comp in sorted(set(b["env"]) & set(a["env"])):
        new_vars = sorted(set(b["env"][comp]) - set(a["env"][comp]))
        if new_vars:
            add("change", "settings", comp, name_of(b, a, comp), [", ".join(new_vars)], ["new-config"])

    impacts = Counter(i for c in changes for i in c["impact"])
    if code_changed and not tests_changed:
        impacts["test-gap"] += 1
    return {"from": a["ref"], "to": b["ref"], "changes": changes, "impacts": dict(impacts),
            "headline": headline(changes), "plan": plan(changes, impacts),
            "dormant": sorted(b["dormant"]), "undeclared": sorted(c for c, v in cb.items() if not v["declared"])}


def name_of(b: dict, a: dict, ref: str) -> str:
    for bp in (b, a):
        if ref in bp["components"]:
            return bp["components"][ref]["label"]
        if ref in bp.get("container_labels", {}):
            return bp["container_labels"][ref]
    return ref


NOUNS = {"deploy": ("deployment file", "deployment files"), "component": ("part", "parts"), "route": ("endpoint", "endpoints"),
         "external": ("outside connection", "outside connections"), "container": ("container", "containers"),
         "table": ("table", "tables"), "migration": ("migration", "migrations"), "flow": ("connection", "connections"),
         "settings": ("set of settings", "sets of settings")}
ENTITIES = ("component", "route", "table", "migration", "external", "container", "deploy", "flow", "settings")
SIGNS = {"add": "+", "change": "~", "remove": "−"}


def headline(changes: List[dict]) -> str:
    """One sentence: what the change does to the system's structure."""
    parts = []
    for op, verb in (("add", "adds"), ("change", "changes"), ("remove", "removes")):
        phrases = []
        for entity in ENTITIES:
            items = [c for c in changes if c["op"] == op and c["entity"] == entity]
            if items:
                one, many = NOUNS[entity]
                names = [c["label"] for c in items]
                shown = "" if entity == "flow" else " (" + ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "") + ")"
                phrases.append(f"{len(items)} {one if len(items) == 1 else many}{shown}")
        if phrases:
            parts.append(verb + " " + (", ".join(phrases[:-1]) + " and " + phrases[-1] if len(phrases) > 1 else phrases[0]))
    if not parts:
        return "No change to the system's structure."
    s = "; ".join(parts)
    return s[0].upper() + s[1:] + "."


def short(changes: List[dict], most: int = 3) -> str:
    """The diff in a few words, for a status line: `+1 endpoint ~1 table`."""
    counts = Counter((c["op"], c["entity"]) for c in changes)
    words = []
    for op in ("add", "change", "remove"):
        for entity in ENTITIES:
            n = counts.get((op, entity))
            if n:
                one, many = NOUNS[entity]
                words.append(f"{SIGNS[op]}{n} {one if n == 1 else many}")
    return " ".join(words[:most]) + (" …" if len(words) > most else "")


def plan(changes: List[dict], impacts: Counter) -> str:
    """The same diff as `terraform plan`-style text: exact, and readable anywhere."""
    sym = {"add": "+", "change": "~", "remove": "-"}
    out = []
    for c in changes:
        tag = f"   [{', '.join(c['impact'])}]" if c["impact"] else ""
        label = f" — {c['label']}" if c["label"] != c["id"] else ""
        out.append(f"{sym[c['op']]} {c['entity']} {c['id']}{label}{tag}")
        out += [f"    {x}" for x in c["detail"]]
    if impacts.get("test-gap"):
        out.append("! test-gap: code changed and no test file did")
    return "\n".join(out) or "no change to the system's structure"
