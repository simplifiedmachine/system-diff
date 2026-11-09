"""One part zoomed in on, the level below the system's containers: its modules as boxes, laid out in levels (a module
before the modules it imports), each with what it serves (endpoints) and reads (settings); around them the parts that
use it, joined to the modules they import, and the parts, stores and outside systems it uses, joined to the modules
that reach them. What a designer opens to see how a component is built, and where in it the agent is working.
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import graph
from .architecture import COLUMNS_FIRST, actors, outside_nodes, protocol
from .design import kinds
from .diff import numstat
from .extract import (ENV_PY, ENV_TS, JS_ROUTE, PREFIX_ARG, PY_ROUTE, ROUTER, Code, import_paths, matches,
                      path_prefixes, py_imports, settings_fields, ts_imports)
from .sources import GitSource

LEVELS = 4            # columns of modules at most; deeper ones share the last
MOST_FILES = 40       # modules drawn; the rest are counted
MOST_CALLERS = 300    # files of the parts that use it, read to see which modules they import


def _short(files: List[str]) -> Dict[str, str]:
    """Each file by its path below the folder all of them share."""
    common = os.path.commonpath([f.rsplit("/", 1)[0] if "/" in f else "" for f in files]) if len(files) > 1 else (
        files[0].rsplit("/", 1)[0] if files and "/" in files[0] else "")
    cut = len(common) + 1 if common else 0
    return {f: f[cut:] or f for f in files}


def _routes(f: str, text: str) -> List[str]:
    """The endpoints a file declares, under its own router's prefix: `GET /items/{id}`."""
    prefix = {}
    for m in ROUTER.finditer(text):
        arg = PREFIX_ARG.search(m.group(3))
        prefix[m.group(1)] = arg.group(2).strip("\"' ") if arg and arg.group(2).strip()[:1] in "\"'" else ""
    out = [f"{method.upper()} {prefix.get(router, '')}{path or ''}" for router, method, path in PY_ROUTE.findall(text)]
    if not f.endswith(".py"):
        out += [f"{method.upper()} {path}" for _obj, method, path in JS_ROUTE.findall(text) if method != "all"]
    return out


def _answers(called: str, local: str) -> bool:
    """Whether a path the front end calls (`/api/v1/items/42`) is one a module's own route answers (`/items/{id}`,
    below whatever prefix it is mounted at): the route's segments match the call's last ones, a parameter any."""
    want = [s for s in local.split("?")[0].strip("/").split("/") if s]
    have = [s for s in called.split("?")[0].strip("/").split("/") if s]
    if not want or len(want) > len(have):
        return False
    return all(w.startswith("{") or h.startswith("{") or w == h for w, h in zip(want[::-1], have[::-1]))


def _settings(f: str, text: str) -> List[str]:
    if f.endswith(".py"):
        return sorted({a or b for a, b in ENV_PY.findall(text)} | set(settings_fields(text)))
    return sorted(set(ENV_TS.findall(text)))


def internals(c, root: Path, part: str, agent_path: str = "", marks: Optional[dict] = None,
              stack: Optional[Dict[str, Dict[str, int]]] = None) -> Optional[Dict[str, Any]]:
    """`part`'s modules and their surroundings, ready for canvas.layered: {"columns", "nodes", "edges", "title",
    "files", "more"}; None for a part with no code in the newer side."""
    cfg, bp, before = c.cfg, c.after, c.before
    if part not in bp["components"]:
        return None
    marks = marks or graph.change_marks(c.diff, before, bp)
    source = GitSource(c.head_sha, root)
    code = Code(cfg)
    files = [f for f in source.files if not code.skip.search(f)]
    fileset = set(files)
    owner = {f: code.part_of(f) for f in files if code.is_code(f) and not code.is_test(f)}
    mine = sorted(f for f, o in owner.items() if o == part)
    if not mine:
        return None
    python_paths = import_paths(files, cfg["code"]["python_paths"])
    prefixes = path_prefixes(files, source.read) if any(not f.endswith(".py") for f in files) else {}

    def aliases_of(f: str) -> Dict[str, str]:
        folder = max((d for d in prefixes if not d or f.startswith(d + "/")), key=len, default=None)
        return {**(prefixes.get(folder) or {}), **cfg["code"]["aliases"]} if folder is not None \
            else cfg["code"]["aliases"]

    def reached(f: str, text: str) -> List[Tuple[str, Counter]]:
        return (py_imports(f, text, fileset, python_paths) if f.endswith(".py")
                else ts_imports(f, text, fileset, aliases_of(f)))

    texts = source.read_many(mine)
    inner: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    out: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    for f in mine:
        for t, names in reached(f, texts[f]):
            o = owner.get(t)
            if t != f and o == part:
                inner[(f, t)].update(names)
            elif o and o != part:
                out[(f, o)].update(names)

    conns = graph.connections(bp)
    callers = sorted({s for (s, t), (k, _n) in conns.items() if t == part and k == "call" and s in bp["components"]})
    into: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    caller_files = [f for f, o in owner.items() if o in callers][:MOST_CALLERS]
    ctexts = source.read_many(caller_files)
    for f in caller_files:
        for t, names in reached(f, ctexts[f]):
            if owner.get(t) == part:
                into[(owner[f], t)].update(names)
    served = {f: _routes(f, texts[f]) for f in mine}
    read = {f: _settings(f, texts[f]) for f in mine}
    for caller in callers:                      # a caller reaching it without an import: over HTTP, to its routes
        if not any(k[0] == caller for k in into):
            called = (bp.get("uses") or {}).get(f"{caller}>{part}", [])
            hits = [f for f in mine if any(_answers(p, r.split(" ", 1)[-1]) for p in called for r in served[f])]
            for target in hits or [next((f for f in mine if served[f]), mine[0])]:
                into[(caller, target)]["HTTP" if f"{caller}>{part}" in bp["api"] else ""] += 1

    # levels: a module comes before the modules it imports
    level: Dict[str, int] = {}

    def depth(f: str, seen: Tuple[str, ...] = ()) -> int:
        if f in level:
            return level[f]
        importers = [a for (a, b) in inner if b == f and a not in seen and a != f]
        d = 0 if not importers else 1 + max(depth(a, seen + (f,)) for a in importers)
        level[f] = min(d, LEVELS - 1)
        return level[f]

    for f in mine:
        depth(f)
    shown = sorted(mine, key=lambda f: (level[f], -texts[f].count("\n"), f))[:MOST_FILES]
    short = _short(mine)

    # what changed in each module since the base, and where the agent is
    changed: Dict[str, str] = {}
    try:
        base_files = set(GitSource(c.base_sha, root).files) if c.base_sha else set()
        for path, (plus, minus, _p) in numstat(root, c.base_sha, c.head_sha, cfg).items():
            if path in fileset and owner.get(path) == part and (plus or minus):
                changed[path] = "added" if path not in base_files else "changed"
    except Exception:   # no base to compare with: nothing marked
        pass

    nodes: List[dict] = []
    for f in shown:
        lines = [{"text": r, "status": "same"} for r in served[f]] + (
            [{"text": "reads " + ", ".join(read[f][:3]) + (" …" if len(read[f]) > 3 else ""), "status": "same"}]
            if read[f] else [])
        nodes.append({"id": "f:" + f, "col": f"in{level[f]}", "type": "component", "title": short[f],
                      "sub": f"{texts[f].count(chr(10))} lines", "status": changed.get(f, "same"),
                      "agent": f == agent_path, "lines": lines})
    edges: List[dict] = []
    for (a, b), names in sorted(inner.items()):
        if "f:" + a in {n["id"] for n in nodes} and "f:" + b in {n["id"] for n in nodes}:
            edges.append({"from": "f:" + a, "to": "f:" + b, "kind": "call", "status": "same", "label": ""})

    comps = bp["components"]
    kind = kinds(bp, cfg)
    around: Dict[str, dict] = {}

    def part_node(pid: str, col: str) -> dict:
        return around.setdefault(pid, {"id": pid, "col": col, "type": "component", "title": comps[pid]["label"],
                                       "sub": graph.files_word(comps[pid]["files"]),
                                       "status": marks["status"].get(pid, "same")})

    ids = {n["id"] for n in nodes}
    for (caller, f), names in sorted(into.items()):
        if "f:" + f in ids:
            part_node(caller, "callers")
            label = "HTTP" if "HTTP" in names else ""
            edges.append({"from": caller, "to": "f:" + f, "kind": "call", "label": label,
                          "status": marks["edges"].get((caller, part), ("call", "same"))[1]})
    for (f, other), names in sorted(out.items()):
        if "f:" + f in ids and other in comps:
            part_node(other, "deps")
            edges.append({"from": "f:" + f, "to": other, "kind": "call", "label": "",
                          "status": marks["edges"].get((part, other), ("call", "same"))[1]})
    if kind.get(part) in ("ui", "interface"):
        acts, flows = actors(bp, cfg, kind, stack)
        for a in acts:
            for fl in flows:
                if fl["from"] == a["id"] and fl["to"] == part:
                    around[a["id"]] = {**a, "col": "actors"}
                    target = next((f for f in shown if served[f]), shown[0])
                    edges.append({"from": a["id"], "to": "f:" + target, "kind": "call", "label": fl["label"],
                                  "status": "same"})
    outs, out_flows = outside_nodes(bp, cfg, before)
    by_id = {n["id"]: n for n in outs}
    declared = {e["id"]: e for e in cfg.get("externals", [])}
    for fl in out_flows:
        if fl["from"] != part:
            continue
        x = fl["to"]
        env = declared.get(x[2:], {}).get("env") or []
        found = " ".join(bp["externals"].get(x[2:], {}).get("found") or [])
        target = next((f for f in shown if env and any(matches(v, env) for v in read[f])), None) or next(
            (f for f in shown if found and any(w in texts[f] for w in found.split() if "." in w or "_" in w)),
            shown[0])
        around[x] = {**by_id[x], "col": "outside"}
        edges.append({"from": "f:" + target, "to": x, "kind": fl["kind"], "label": fl["label"] or
                      protocol(by_id[x]["title"]), "status": marks["edges"].get((part, x), (fl["kind"], "same"))[1]})

    title = comps[part]["label"]
    columns = COLUMNS_FIRST + [("callers", "Uses it")] + [(f"in{i}", f"Inside {title}" if i == 0 else "")
                                                          for i in range(LEVELS)] + [
        ("deps", "It uses"), ("outside", "Data and outside")]
    return {"columns": columns, "nodes": list(around.values()) + nodes, "edges": edges, "title": title,
            "files": len(mine), "more": max(0, len(mine) - len(shown))}
