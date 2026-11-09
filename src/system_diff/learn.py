"""Teaching the person what is going on in their system while their AI coding agent works on it, in plain words: what
a part is and does (its role, connections, endpoints, settings, outside systems), what is changing in it and why that
matters, and TOPICS to learn: the technologies its code imports and the ideas its role and the change touch, each with
what it is, why it matters here, and where to read more.

Built from the exact structure (the map, the diff) and the imports in the part's own files, so it never claims what the
code does not show. `ask_llm` can add a longer lesson, written from these same facts and never from the code.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import graph
from .diff import NOUNS, SIGNS
from .extract import Code, first_party, outside_imports
from .topics import CONCEPT, TECH, tech_key

STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))
BASIC = frozenset({"json", "re", "logging", "tempfile", "csv"})  # true of most code: named after the rest
MOST = 7  # topics shown for a part; the rest are named


def rank(key: str) -> int:
    """Packages first, then the standard library, its everyday modules last."""
    return 2 if key in BASIC else 1 if key.split(".")[0] in STDLIB else 0

ROLE = {
    "ui": "what people use: the screens and pages they see and click, which call the entry points for their data",
    "interface": "an entry point: where people, other programs or AI tools reach the system",
    "logic": "logic: the rules and steps of what the system does, between its entry points and its data",
    "data": ("data access: the code that reads and writes stored data, so the rest of the system need not know how "
             "it is stored"),
    "store": "stored data: where the system keeps what must outlive a restart, and every version of the code",
    "external": "an outside system: run by someone else and reached over the network",
}
ROLE_TOPIC = {"ui": "front-end", "interface": "entry-point", "logic": "layers", "data": "data-access",
              "store": "databases", "external": "outside-systems"}

WHY = {
    "new-component": ("A new part is new code to understand and look after: what it calls, and what calls it, decide "
                      "how far a later change in it spreads."),
    "removed-component": "Whatever used this part must do without it: anything still importing it breaks.",
    "new-interface": ("A new endpoint is a new way into the system: anyone who can reach it can call it, so it needs "
                      "the same checks as the others (who may call it, what input it accepts)."),
    "breaking-interface": ("Anything that called this endpoint (a page, another service, a script, a saved link) now "
                           "gets an error, unless it changes at the same time."),
    "new-external": ("A new outside system can be slow, fail or change on its own schedule, needs its own settings "
                     "and secrets, and may cost money for each call."),
    "removed-external": "One fewer outside system to depend on; check that nothing still expects it.",
    "data-schema-change": ("Stored data outlives the code: the migration runs once against the real data, so rows "
                           "written before it must still make sense after it."),
    "deployment-change": ("This changes what runs and how: it takes effect when deployed, not when merged, and can "
                          "differ between environments."),
    "new-flow": ("A new connection: this part now depends on that one, so a change there can break this, and the two "
                 "no longer change apart."),
    "removed-flow": "The two parts no longer depend on each other: each can now change without the other.",
    "new-config": ("A new setting must be set wherever the system runs; until it is, the code falls back to a default "
                   "or fails when it starts."),
    "test-gap": "Code changed and no test file did: nothing checks this change automatically.",
}
VERBS = {"add": "adds", "change": "changes", "remove": "removes"}
IMPACT_TOPIC = {"new-component": "layers", "removed-component": "coupling", "new-interface": "http-endpoint",
                "breaking-interface": "breaking-changes", "new-external": "outside-systems",
                "removed-external": "outside-systems", "data-schema-change": "migrations",
                "deployment-change": "deployment", "new-flow": "coupling", "removed-flow": "coupling",
                "new-config": "settings", "test-gap": "tests"}


def and_list(names: Iterable[str], most: int = 4) -> str:
    names = list(names)
    if len(names) > most:
        names = names[:most] + [f"{len(names) - most} more"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def part_files(source, code: Code, part: str) -> List[str]:
    return [f for f in source.files if code.is_code(f) and not code.is_test(f) and code.part_of(f) == part]


def imports(source, cfg: Dict[str, Any], files: List[str]) -> Counter:
    """How many of `files` import each outside name (a package, or a module of the standard library)."""
    own = first_party(source.files, cfg["code"]["python_paths"])
    texts = source.read_many(files)
    return Counter(name for f in files for name in outside_imports(f, texts[f], own, cfg["code"]["aliases"]))


def technologies(found: Counter) -> tuple:
    """(topic key, files) for the known technologies among imported names, packages before the standard library,
    then by how many files use them; and the names no topic covers."""
    keys: Counter = Counter()
    unknown = set()
    for name, n in found.items():
        key = tech_key(name)
        if key:
            keys[key] = max(keys[key], n)
        else:
            unknown.add(name.split(".")[0] if not name.startswith("@") else name)
    known = sorted(keys.items(), key=lambda kv: (rank(kv[0]), -kv[1], kv[0]))
    return known, sorted(n for n in unknown if n not in STDLIB and not n.startswith("node:"))


def find_part(cmp, name: str, root: Optional[Path] = None) -> Optional[str]:
    """The part someone means: its id or its name (any case), an outside system's id or name, or a file in it."""
    bp, before = cmp.after, cmp.before
    want = name.strip().lower()
    for side in (bp, before):
        for cid, c in side["components"].items():
            if want in (cid.lower(), c["label"].lower()):
                return cid
        for x, e in side["externals"].items():
            if want in (x.lower(), "x:" + x.lower(), e["label"].lower()):
                return "x:" + x
    path = name.replace("\\", "/")
    if root is not None and Path(name).is_absolute():
        from .activity import relative
        path = relative(root, name) or path
    part = Code(cmp.cfg).part_of(path.removeprefix("./"))
    return part if part in bp["components"] or part in before["components"] else None


def touches(c: dict, part: str, bp: dict, before: dict, store: str) -> bool:
    """Whether a change in the plan is about `part`."""
    entity, cid = c["entity"], c["id"]
    comps = {**before["components"], **bp["components"]}
    if entity in ("component", "settings"):
        return cid == part
    if entity == "route":
        return (bp["routes"].get(cid) or before["routes"].get(cid)) == part
    if entity == "flow":
        src, dst = cid.split(">")
        return part in (src, dst) or comps.get(part, {}).get("container") == dst != ""
    if entity == "external":
        user, ext = cid.split(">")
        return part in (user, "x:" + ext)
    if entity in ("table", "migration"):
        return part == store or any(part in e["used_by"] for x, e in bp["externals"].items() if "x:" + x == store)
    return False


def lesson(cmp, part: str, source=None) -> Optional[Dict[str, Any]]:
    """Everything to teach about one part (or outside system, x:<id>) of a comparison's newer side: {"part", "title",
    "column", "what": [sentences], "changing": [{"text", "why"}], "topics": [{"key", "kind": technology|idea, "title",
    "what", "here", "link"}], "more_topics": [titles], "other_imports": [names]}. `source`: the newer side's files,
    to read the part's imports from (GitSource(cmp.head_sha, root)); without it, no technologies. None for a part
    the comparison does not have."""
    bp, before, cfg = cmp.after, cmp.before, cmp.cfg
    marks = graph.change_marks(cmp.diff, before, bp)
    hood = graph.neighbourhood(bp, cfg, part, marks, before)
    if hood is None:
        return None
    nodes = {n["id"]: n for n in hood["nodes"]}
    me = nodes[part]
    title, col = me["title"], me["col"]
    callers = [nodes[i]["title"] for i in hood["callers"]]
    calls = [nodes[i]["title"] for i in hood["callees"] if not i.startswith("x:")]
    outside = [nodes[i]["title"] for i in hood["callees"] if i.startswith("x:")]
    routes = sorted(r for r, owner in bp["routes"].items() if owner == part)
    settings = bp["env"].get(part, [])
    comp = bp["components"].get(part) or before["components"].get(part)
    what = [f"{title} is {ROLE[col]}."]
    if comp is None:  # an outside system
        users = [nodes[u]["title"] for u in hood["callers"]]
        if users:
            what.append(f"{and_list(users)} use{'s' if len(users) == 1 else ''} it.")
        if part == marks["store"] and bp["tables"]:
            what.append(f"It holds {len(bp['tables'])} table{'' if len(bp['tables']) == 1 else 's'}: "
                        f"{and_list(sorted(bp['tables']), 6)}.")
    else:
        what.append(f"It is {comp['files']} file{'' if comp['files'] == 1 else 's'} of code ({comp['lines']} lines).")
        if not comp.get("declared", True):
            what.append("system-diff.toml does not name it, so it is called after its folder.")
        if routes:
            what.append(f"It serves {len(routes)} endpoint{'' if len(routes) == 1 else 's'}: {and_list(routes)}.")
        if calls:
            what.append(f"It calls {and_list(calls)}.")
        if comp.get("hub"):
            what.append("Most of the code uses it, so the map draws no arrows into it.")
        elif callers:
            what.append(f"{and_list(callers)} call{'s' if len(callers) == 1 else ''} it.")
        else:
            what.append("Nothing in the system calls it: it is reached from outside." if col in ("interface", "ui")
                        else "Nothing in the system calls it.")
        if settings:
            what.append(f"It reads the setting{'' if len(settings) == 1 else 's'} {and_list(settings, 5)}: values "
                        "set where the system runs, not in the code.")
        if outside:
            what.append(f"It talks to {and_list(outside)}, outside the system.")
        dormant = [e["label"] for e in bp["dormant"].values() if part in e["used_by"]]
        if dormant:
            what.append(f"It reads the settings of {and_list(dormant)}, which no deploy configuration sets yet.")

    changes = [c for c in cmp.diff["changes"] if touches(c, part, bp, before, marks["store"])]
    groups: Dict[Any, List[dict]] = {}  # changes with one impact share one "why"
    for c in changes:
        groups.setdefault(c["impact"][0] if c["impact"] else (c["op"], c["entity"]), []).append(c)
    changing = []
    for impact, items in groups.items():
        kinds: Dict[tuple, List[dict]] = {}
        for c in items:
            kinds.setdefault((c["op"], c["entity"]), []).append(c)
        texts, says = [], []
        for (op, entity), same in kinds.items():
            one, many = NOUNS.get(entity, (entity, entity + "s"))
            if len(same) == 1:
                c = same[0]
                detail = f": {'; '.join(c['detail'])}" if c["detail"] and entity != "route" else ""
                texts.append(f"{SIGNS[op]} {one} {c['label']}{detail}")
                says.append(f"{VERBS[op]} {one} {c['label']}")
            else:
                texts.append(f"{SIGNS[op]} {len(same)} {many}: {and_list([c['label'] for c in same])}")
                says.append(f"{VERBS[op]} {len(same)} {many}")
        impact = impact if isinstance(impact, str) else ""
        changing.append({"text": "; ".join(texts), "why": WHY.get(impact, ""), "impact": impact,
                         "says": f"This change {and_list(says)}."})
    own_change = any(c["entity"] == "component" and c["id"] == part and c["op"] == "change" for c in changes)
    if own_change and cmp.diff["impacts"].get("test-gap"):
        changing.append({"text": "! no test file changed with it", "why": WHY["test-gap"], "impact": "test-gap",
                         "says": f"{title}'s code changed and no test file did."})

    found = imports(source, cfg, part_files(source, Code(cfg), part)) if comp is not None and source else Counter()
    techs, other = technologies(found)

    topics: List[Dict[str, Any]] = []

    def add(kind: str, key: str, here: str) -> None:
        if key and all(t["key"] != key for t in topics):
            name, text, link = (CONCEPT if kind == "idea" else TECH)[key]
            topics.append({"key": key, "kind": "idea" if kind == "idea" else "technology", "title": name,
                           "what": text, "here": here, "link": link})

    for ch in changing:
        add("idea", IMPACT_TOPIC.get(ch["impact"], ""), ch["says"])
    add("idea", ROLE_TOPIC.get(col, ""), role_here(title, col, callers, calls, outside, bp, cfg))
    for key, n in techs:
        add("tech", key, f"{title} imports it in {n} file{'' if n == 1 else 's'}.")
    if routes:
        add("idea", "http-endpoint", f"{title} serves {and_list(routes, 3)}.")
    if settings:
        add("idea", "settings", f"{title} reads {and_list(settings, 5)}.")
    if outside:
        add("idea", "outside-systems", f"{title} talks to {and_list(outside)}.")
    if part == marks["store"] and bp["migrations"]:
        add("idea", "migrations", f"{len(bp['migrations'])} migration{'' if len(bp['migrations']) == 1 else 's'} "
                                  "make its tables.")
    if comp is not None and comp.get("container"):
        add("idea", "containers", f"{title} runs in the {comp['container']} container.")
    if comp is not None and (comp.get("hub") or len(callers) >= 4):
        add("idea", "coupling", f"{len(callers)} parts import {title}.")
    return {"part": part, "title": title, "column": col, "what": what, "changing": changing,
            "topics": topics[:MOST], "more_topics": [t["title"] for t in topics[MOST:]], "other_imports": other}


def role_here(title: str, col: str, callers: List[str], calls: List[str], outside: List[str], bp: dict,
              cfg: Dict[str, Any]) -> str:
    """Why the idea behind a part's column matters for this part."""
    if col == "interface":
        entries = sorted(c["label"] for cid, c in bp["components"].items()
                         if graph.part_kind(cid, c, bp, cfg) == "interface" and c["label"] != title)
        return (f"{title} is one of {len(entries) + 1} entry points; the others: {and_list(entries)}." if entries
                else f"{title} is the system's only entry point.")
    if col == "logic":
        return (f"{title} is called by {and_list(callers)} and calls {and_list(calls)}." if callers and calls
                else f"{title} is called by {and_list(callers)}." if callers
                else f"{title} calls {and_list(calls)}." if calls else f"{title} stands on its own.")
    if col == "data":
        return (f"{and_list(callers)} reach{'es' if len(callers) == 1 else ''} stored data through {title}." if callers
                else f"Nothing in the system reaches stored data through {title} yet.")
    if col == "ui":
        return f"{title} calls {and_list(calls)}." if calls else f"{title} calls no API part the map knows."
    if col == "store":
        return f"{title} is used by {and_list(callers)}." if callers else f"{title} is not used yet."
    return f"{title} is used by {and_list(callers)}." if callers else f"{title} is not used yet."


def system_topics(source, bp: dict, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The technologies the whole system imports, each with the parts that use it: packages first, then the most
    used."""
    code = Code(cfg)
    own = first_party(source.files, cfg["code"]["python_paths"])
    files = [f for f in source.files if code.is_code(f) and not code.is_test(f)]
    texts = source.read_many(files)
    users: Dict[str, set] = defaultdict(set)
    for f in files:
        for name in outside_imports(f, texts[f], own, cfg["code"]["aliases"]):
            key = tech_key(name)
            if key:
                users[key].add(code.part_of(f))
    label = {cid: c["label"] for cid, c in bp["components"].items()}
    out = []
    for key, parts in users.items():
        name, text, link = TECH[key]
        out.append({"key": key, "title": name, "what": text, "link": link, "stdlib": key.split(".")[0] in STDLIB,
                    "used_by": sorted((label.get(p, p) for p in parts), key=str.lower)})
    return sorted(out, key=lambda t: (rank(t["key"]), -len(t["used_by"]), t["title"].lower()))


# ── what just happened, said plainly ─────────────────────────────────────────

WEIGHT = {"breaking-interface": 9, "removed-component": 8, "data-schema-change": 8, "new-external": 7,
          "removed-external": 6, "new-interface": 6, "new-component": 5, "new-config": 5, "deployment-change": 5,
          "new-flow": 4, "removed-flow": 3, "test-gap": 2}
SHORT_WHY = {
    "new-component": "new code to understand and look after",
    "removed-component": "anything still importing it breaks",
    "new-interface": "a new way in: it needs the same checks as the others",
    "breaking-interface": "whatever called it now gets an error",
    "new-external": "it can be slow, fail or cost money, and needs its own settings",
    "removed-external": "one fewer outside system to depend on",
    "data-schema-change": "stored data outlives the code: this runs once on the real data",
    "deployment-change": "it takes effect when deployed, not when merged",
    "new-config": "it must be set wherever the system runs",
    "test-gap": "nothing checks this change automatically",
}


def say(c: dict) -> Dict[str, Any]:
    """One change of a diff as a sentence someone can follow without the plan's notation, why it matters, and how
    much (`weight`: what a person should hear about first)."""
    op, entity, label, detail = c["op"], c["entity"], c["label"], c["detail"]
    impact = c["impact"][0] if c["impact"] else ""
    first = detail[0] if detail else ""
    why = SHORT_WHY.get(impact, "")
    if entity == "component":
        text = {"add": f"A new part: {label}.", "remove": f"The part {label} is gone.",
                "change": f"{label} changes ({first})."}[op]
    elif entity == "route":
        text = f"{first or 'The system'} {'now answers' if op == 'add' else 'no longer answers'} {label}."
    elif entity in ("flow", "external"):
        a, _, b = label.partition(" → ")
        if entity == "flow":
            text = f"{a} now uses {b}." if op == "add" else f"{a} no longer uses {b}."
            why = f"a change in {b} can now break {a}" if op == "add" else "the two can now change apart"
        else:
            text = f"{a} now talks to {b}, outside the system." if op == "add" else f"{a} stops talking to {b}."
    elif entity == "table":
        text = {"add": f"A new table: {label}.", "remove": f"The table {label} is dropped.",
                "change": f"The table {label} changes: {', '.join(detail)}."}[op]
    elif entity == "migration":
        text = f"A new migration: {label}."
    elif entity == "settings":
        text = f"{label} now reads {first}."
    elif entity == "container":
        text = {"add": f"A new container: {label}.", "remove": f"The {label} container goes.",
                "change": f"The {label} container changes."}[op]
    else:
        text = f"A deployment file changes: {label}."
    return {"text": text, "why": why, "impact": impact, "weight": WEIGHT.get(impact, 1)}


def story(d: dict, final: bool = False) -> List[Dict[str, Any]]:
    """A diff as sentences, what matters most first; lines merely changed in a part are left out (the edits say
    that). `final`, at the end of a turn: also when code changed and no test did."""
    told = [say(c) for c in d.get("changes", []) if c["impact"]]
    if final and d.get("impacts", {}).get("test-gap"):
        told.append({"text": "Code changed and no test did.", "why": SHORT_WHY["test-gap"], "impact": "test-gap",
                     "weight": WEIGHT["test-gap"]})
    return sorted(told, key=lambda s: -s["weight"])


def one_line(told: List[Dict[str, Any]]) -> str:
    """A story in one line: what matters most, why, and how much more there is."""
    if not told:
        return ""
    top = told[0]
    why = f" · {top['why']}" if top["why"] else ""
    return top["text"].rstrip(".") + why + (f" (and {len(told) - 1} more)" if len(told) > 1 else "")


def brief(root: Path, cfg: Dict[str, Any], part: str) -> Dict[str, Any]:
    """A part in a line, cheaply, for the status line (its files read from disk, no diff): its name, what it is,
    and topics to learn: the idea behind its role, then the technologies it imports."""
    from .sources import git
    code = Code(cfg)
    names = [f for f in git("ls-files", "-z", "-co", "--exclude-standard", cwd=root).split("\0") if f]
    files = [f for f in names if code.is_code(f) and not code.is_test(f) and code.part_of(f) == part]
    own = first_party(names, cfg["code"]["python_paths"])
    found: Counter = Counter()
    for f in files[:200]:
        try:
            text = (Path(root) / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.update(outside_imports(f, text, own, cfg["code"]["aliases"]))
    meta = next((c for c in cfg.get("components", []) if c.get("id") == part), {})
    col = meta.get("kind") if meta.get("kind") in ROLE else "logic"
    idea = CONCEPT[ROLE_TOPIC[col]]
    techs = [TECH[k] for k, _ in technologies(found)[0]]
    return {"part": part, "label": meta.get("label", part), "role": ROLE[col],
            "topics": [[t[0], t[1]] for t in [idea] + techs][:8]}


# ── as text ───────────────────────────────────────────────────────────────────

def to_markdown(les: Dict[str, Any]) -> str:
    out = [f"## {les['title']}", "", " ".join(les["what"])]
    if les["changing"]:
        out += ["", "**What is changing**", ""]
        out += [f"- `{c['text']}`" + (f": {c['why']}" if c["why"] else "") for c in les["changing"]]
    if les["topics"]:
        out += ["", "**Topics**", ""]
        out += [f"- **{t['title']}** ({t['kind']}). {t['what']} *{t['here']}* [Read more]({t['link']})"
                for t in les["topics"]]
    more = les["more_topics"] + les["other_imports"]
    if more:
        out += ["", "Also: " + ", ".join(more) + "."]
    return "\n".join(out) + "\n"


def system_markdown(topics: List[Dict[str, Any]]) -> str:
    if not topics:
        return "No technology the topics know is imported here yet.\n"
    out = ["## What this system is built with", ""]
    for stdlib, heading in ((False, "**Packages**"), (True, "**From the standard library**")):
        group = [t for t in topics if t["stdlib"] == stdlib]
        if group:
            out += [heading, ""] + [f"- **{t['title']}**: {t['what']} *Used by {and_list(t['used_by'])}.* "
                                    f"[Read more]({t['link']})" for t in group] + [""]
    return "\n".join(out)


TEACH = (
    "You teach a developer about the system they are building with an AI coding agent, so they understand what the "
    "agent is doing to it and the technology involved. You are given exact, machine-extracted facts about one part "
    "of the system: what it is, how it connects, what is changing in it, and topics. Write three short paragraphs "
    "in plain words, no headings: 1) what this part does in the system and how it fits with the parts around it; "
    "2) if something is changing, what the change does and what could go wrong; 3) for up to three of the topics, "
    "what to learn about it and why it matters here. Never mention a part, endpoint, setting or technology that is "
    "not in the facts."
)


def ask_llm(les: Dict[str, Any], root: Path, cfg: Dict[str, Any]) -> Optional[str]:
    """A longer lesson from your LLM (the one narrate uses), written from the facts above; None when none answers."""
    from . import narrate
    got = narrate.complete(to_markdown(les), TEACH, root, cfg, limit=2500)
    return f"{got[0]}\n\n_Written by {got[1]} from the facts above, never from the code._" if got else None
