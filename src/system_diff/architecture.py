"""The system as a system designer draws it, C4-style, rather than as a map of every module: who uses it (ACTORS,
people and programs), where they come in (ENTRY POINTS), the containers that do the work with their COMPONENTS listed
inside, the data it keeps (STORES) and the systems outside it; joined by FLOWS named for what travels on them
(HTTPS, MCP, SQL, a terminal). Two levels:

  system     every actor, entry point, container, store and outside system; a flow between two boxes stands for
             all the connections between what they hold
  container  one container's components as boxes with the connections between them, and every box outside it
             they connect to (an entry point, another container, a store), with the flows to it

Declared in system-diff.toml where the project says so: `[[actors]]` (id, label, kind person|program, uses = [part
ids], via = "HTTPS"), a component's `group` (its container) and `about` (one line on what it does), an outside
system's `used_by` and `via` where no setting names it. Inferred where it does not: the front end's people, an API's
callers, an MCP server's AI agents, a command line's developer, from the technology each entry point imports; the
parts of a crowded column held in one container named for it.

Reorganised where an LLM has proposed it (regroup.py): an overlay, given to `system`/`container` as `overlay` or set on
the comparison as `cmp.regroup`, names containers for parts, gives parts short labels and columns, and folds away the
parts that are not the running system. What the config declares always wins over it; parts it does not know are drawn
as without it.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from . import graph
from .design import kinds

COLUMNS_FIRST = [("actors", "Who uses it")]
SYSTEM_COLUMN = {"logic": "core", "data": "core", "store": "outside", "external": "outside"}  # four, at the top level
MAIN_FLOWS = 4       # flows drawn from each box at the system level, strongest first
FOLD_TITLE = "Docs and tools"   # the container of folded-away parts that changed
CROWDED = 7          # more parts than this in a column, none grouped: they go in one container
PROTOCOL = (("postgres", "SQL"), ("mysql", "SQL"), ("maria", "SQL"), ("sqlite", "SQL"), ("database", "SQL"),
            ("redis", "Redis"), ("valkey", "Redis"), ("mongo", "MongoDB"), ("smtp", "SMTP"), ("mail", "SMTP"),
            ("kafka", "Kafka"), ("rabbit", "AMQP"), ("s3", "S3 API"), ("minio", "S3 API"), ("git", "git"),
            ("elastic", "HTTP"), ("search", "HTTP"))
ACTOR_BY_TECH = (            # an entry point importing this is used by: (actor id, label, kind, via)
    ("mcp", ("agents", "AI agents", "program", "MCP")),
    ("fastapi", ("clients", "API clients", "program", "HTTPS")),
    ("flask", ("clients", "API clients", "program", "HTTPS")),
    ("django", ("clients", "API clients", "program", "HTTPS")),
    ("express", ("clients", "API clients", "program", "HTTPS")),
    ("http.server", ("browser", "Browser", "person", "HTTP")),
    ("argparse", ("developer", "Developer", "person", "terminal")),
    ("click", ("developer", "Developer", "person", "terminal")),
    ("typer", ("developer", "Developer", "person", "terminal")),
    ("textual", ("developer", "Developer", "person", "terminal")),
)


def protocol(label: str, shape: str = "") -> str:
    low = label.lower()
    hit = next((p for key, p in PROTOCOL if key in low), "")
    return hit or ("SQL" if shape == "db" else "HTTPS")


def _meta(cfg: Dict[str, Any]) -> Dict[str, dict]:
    return {c["id"]: c for c in cfg.get("components", []) if "id" in c}


def _overlay(cmp, overlay: Optional[dict]) -> Optional[dict]:
    ov = overlay if overlay is not None else getattr(cmp, "regroup", None)
    return ov if isinstance(ov, dict) and isinstance(ov.get("parts"), dict) else None


def _proposed(ov: Optional[dict], cid: str, field: str, cfg: Dict[str, Any]) -> Any:
    """What the overlay proposes for a part's field, unless the config declares it (label, kind or group)."""
    if not ov or _meta(cfg).get(cid, {}).get({"layer": "kind"}.get(field, field)):
        return None
    p = ov["parts"].get(cid)
    return p.get(field) if isinstance(p, dict) else None


def folded(bp: dict, cfg: Dict[str, Any], ov: Optional[dict]) -> Set[str]:
    """The parts the overlay says are not the running system (docs, tools): never a declared one or one with routes."""
    if not ov:
        return set()
    served = set(bp["routes"].values())
    return {cid for cid in bp["components"] if _proposed(ov, cid, "fold", cfg) and cid not in served
            and cid not in _meta(cfg)}


def kinds_with(bp: dict, cfg: Dict[str, Any], ov: Optional[dict]) -> Dict[str, str]:
    """design.kinds with the overlay's columns for the parts the config does not place; a part with routes stays an
    entry point."""
    kind = kinds(bp, cfg)
    if ov:
        served = set(bp["routes"].values())
        for cid in bp["components"]:
            layer = _proposed(ov, cid, "layer", cfg)
            if layer in graph.PART_KINDS and cid not in served:
                kind[cid] = layer
    return kind


def groups_of(bp: dict, cfg: Dict[str, Any], kind: Dict[str, str], overlay: Optional[dict] = None) -> Dict[str, str]:
    """Part -> its container ("g:<name>"), as declared, else as the overlay proposes (a container still holding two
    parts); a crowded column with none of either is one container."""
    meta = _meta(cfg)
    out = {cid: "g:" + meta[cid]["group"] for cid in bp["components"] if meta.get(cid, {}).get("group")}
    gone = folded(bp, cfg, overlay)
    if overlay:
        proposed: Dict[str, List[str]] = defaultdict(list)
        for cid in bp["components"]:
            name = _proposed(overlay, cid, "group", cfg)
            if isinstance(name, str) and name and cid not in out and cid not in gone \
                    and not bp["components"][cid].get("quiet"):
                proposed[name].append(cid)
        for name, parts in proposed.items():
            if len(parts) >= 2:
                out.update({p: "g:" + name for p in parts})
    by_col: Dict[str, List[str]] = defaultdict(list)
    for cid in bp["components"]:
        if cid not in out and cid not in gone and not bp["components"][cid].get("quiet"):
            by_col[kind.get(cid, "logic")].append(cid)
    heading = dict(cfg["picture"]["columns"])
    for col, parts in by_col.items():
        if col in ("logic", "data") and len(parts) > CROWDED and not any(kind.get(p) == col for p in out):
            for p in parts:
                out[p] = "g:" + heading.get(col, col)
    return out


def _stack(bp: dict, part: str, extra: Optional[Dict[str, Dict[str, int]]]) -> Set[str]:
    names = set((bp.get("stack") or {}).get(part, {})) | set((extra or {}).get(part, {}))
    return names | {n.split(".")[0] for n in names}


def actors(bp: dict, cfg: Dict[str, Any], kind: Dict[str, str],
           stack: Optional[Dict[str, Dict[str, int]]] = None) -> Tuple[List[dict], List[dict]]:
    """The actors and their flows into the entry points: as declared, else inferred from what each entry point is
    built with. A front end's people use it in a browser; an API a front end calls has the front end as its caller."""
    declared = cfg.get("actors") or []
    nodes, flows = [], []
    if declared:
        for a in declared:
            aid = "a:" + a["id"]
            nodes.append({"id": aid, "col": "actors", "type": "actor", "title": a.get("label", a["id"]),
                          "sub": a.get("about", a.get("kind", "")), "status": "same"})
            for use in a.get("uses", []):
                part, _, via = str(use).partition(":")
                if part in bp["components"]:
                    flows.append({"from": aid, "to": part, "label": via or a.get("via", ""), "kind": "call"})
        return nodes, flows
    called_by_ui = {key.split(">")[1] for key in bp["api"]}
    found: Dict[str, dict] = {}
    for cid in sorted(bp["components"], key=lambda c: graph.part_order(bp["components"][c], c)):
        k = kind.get(cid)
        if k == "ui":
            hit = ("people", "People", "person", "browser")
        elif k == "interface":
            names = _stack(bp, cid, stack)
            hit = next((a for tech, a in ACTOR_BY_TECH if tech in names), None)
            if hit and hit[0] == "clients" and cid in called_by_ui:
                hit = None                          # its front end is its caller, drawn as a flow already
            if hit is None and any(o == cid for o in bp["routes"].values()) and cid not in called_by_ui:
                hit = ("clients", "API clients", "program", "HTTPS")
        else:
            continue
        if hit:
            aid = "a:" + hit[0]
            found.setdefault(aid, {"id": aid, "col": "actors", "type": "actor", "title": hit[1], "sub": hit[2],
                                   "status": "same"})
            flows.append({"from": aid, "to": cid, "label": hit[3], "kind": "call"})
    return list(found.values()), flows


def _entry_sub(bp: dict, cid: str, cfg: Dict[str, Any]) -> str:
    about = _meta(cfg).get(cid, {}).get("about", "")
    routes = sum(1 for o in bp["routes"].values() if o == cid)
    if routes:
        return f"{routes} endpoint{'' if routes == 1 else 's'}" + (f" · {about}" if about else "")
    return about or graph.files_word(bp["components"][cid]["files"])


def outside_nodes(bp: dict, cfg: Dict[str, Any], before: Optional[dict] = None) -> Tuple[List[dict], List[dict]]:
    """Stores and outside systems with the flows from the parts that use them: those the settings name (the
    blueprint's), and those the config ties to parts itself (`used_by`)."""
    nodes: Dict[str, dict] = {}
    flows = []
    for side in [s for s in (before, bp) if s]:
        for x, e in side["externals"].items():
            db = e.get("shape") == "db"
            nodes["x:" + x] = {"id": "x:" + x, "col": "store" if db else "external", "type": "store" if db else "host",
                               "title": e["label"], "status": "same", "sub": ""}
    for x, e in bp["externals"].items():
        tables = len(bp["tables"]) if x == bp.get("store", "postgres") else 0
        nodes["x:" + x]["sub"] = f"{tables} tables" if tables else f"used by {len(e['used_by'])}"
        for u in e["used_by"]:
            flows.append({"from": u, "to": "x:" + x, "label": protocol(e["label"], e.get("shape", "")),
                          "kind": "data" if e.get("shape") == "db" else "network"})
    for e in cfg.get("externals", []):
        users = [u for u in e.get("used_by", []) if u in bp["components"]]
        if not users or "x:" + e["id"] in nodes and "x:" + e["id"] in {f["to"] for f in flows}:
            continue
        db = e.get("shape") in ("db", "files")
        nodes["x:" + e["id"]] = {"id": "x:" + e["id"], "col": "store" if db else "external",
                                 "type": "store" if db else "host", "title": e["label"], "status": "same",
                                 "sub": e.get("about", "")}
        for u in users:
            flows.append({"from": u, "to": "x:" + e["id"], "label": e.get("via") or protocol(e["label"], e.get("shape", "")),
                          "kind": "data" if db else "network"})
    return list(nodes.values()), flows


def _status(marks: dict, nid: str) -> str:
    return marks["status"].get(nid, "same")


def system(cmp, marks: Optional[dict] = None, agent: Optional[str] = None,
           stack: Optional[Dict[str, Dict[str, int]]] = None, overlay: Optional[dict] = None) -> Dict[str, Any]:
    """The system level: {"columns", "nodes", "edges"}, ready for canvas.layered. A container's node lists its
    components as `items`; a flow names the boxes it joins and, where one end is a component in a container, that
    component (`from_item`, `to_item`), so a picked component lights its own flows."""
    bp, before, cfg = cmp.after, cmp.before, cmp.cfg
    marks = marks or graph.change_marks(cmp.diff, before, bp)
    ov = _overlay(cmp, overlay)
    kind = {**kinds_with(before, cfg, ov), **kinds_with(bp, cfg, ov)}
    group = {**groups_of(before, cfg, kind, ov), **groups_of(bp, cfg, kind, ov)}
    fold = folded(before, cfg, ov) | folded(bp, cfg, ov)
    about = (ov or {}).get("groups") or {}
    meta = _meta(cfg)
    comps = {**before["components"], **bp["components"]}
    gone = set(before["components"]) - set(bp["components"])

    nodes: Dict[str, dict] = {}
    box_of: Dict[str, str] = {}
    for cid in sorted(comps, key=lambda c: graph.part_order(comps[c], c)):
        c = comps[cid]
        if c.get("quiet"):
            continue
        st = "removed" if cid in gone else _status(marks, cid)
        col = kind.get(cid, "logic")
        label = _proposed(ov, cid, "label", cfg) or c["label"]
        if cid in fold:                      # not the running system: drawn only while it changes or is worked on
            if st == "same" and cid != agent:
                continue
            group[cid] = "g:" + FOLD_TITLE
        if cid in group:
            gid = group[cid]
            box = nodes.setdefault(gid, {"id": gid, "col": col, "type": "group", "title": gid[2:], "sub": "",
                                         "status": "same", "items": []})
            if isinstance(about, dict) and about.get(gid[2:]):
                box["about"] = about[gid[2:]]
            box["items"].append({"id": cid, "title": label, "status": st, "agent": cid == agent,
                                 "hub": bool(c.get("hub"))})
            box_of[cid] = gid
        else:
            nodes[cid] = {"id": cid, "col": col, "type": "component", "title": label, "status": st,
                          "agent": cid == agent, "sub": (_entry_sub(bp, cid, cfg) if cid in bp["components"]
                                                          and col in ("interface", "ui") else
                                                          meta.get(cid, {}).get("about", "")
                                                          or graph.files_word(c["files"]))}
            box_of[cid] = cid
    for box in nodes.values():
        if box["type"] == "group":
            items = box["items"]
            box["sub"] = f"{len(items)} " + ("entry points" if box["col"] in ("interface", "ui") else "components")
            if any(i["status"] != "same" for i in items):
                box["status"] = "changed"
            box["agent"] = any(i["agent"] for i in items)

    acts, flows = actors(bp, cfg, kind, stack)
    outs, out_flows = outside_nodes(bp, cfg, before)
    for n in acts + outs:
        n["status"] = _status(marks, n["id"])
        nodes[n["id"]] = n
        box_of[n["id"]] = n["id"]
    flows += out_flows

    # every connection between parts, lifted to the boxes that hold them; within one box it is not drawn
    now = graph.connections(bp)
    for (s, t), (k, _n) in now.items():
        if k == "call" and not t.startswith("x:") and not comps.get(t, {}).get("hub"):
            flows.append({"from": s, "to": t, "label": "HTTP" if f"{s}>{t}" in bp["api"] else "", "kind": "call"})
    for (s, t), (k, st) in marks["edges"].items():
        if st == "removed" and (s, t) not in now:
            flows.append({"from": s, "to": t, "label": "", "kind": k, "status": "removed"})

    edges: Dict[Tuple[str, str], dict] = {}
    for f in flows:
        a, b = box_of.get(f["from"]), box_of.get(f["to"])
        if not a or not b or a == b:
            continue
        st = f.get("status") or marks["edges"].get((f["from"], f["to"]), (f["kind"], "same"))[1]
        e = edges.setdefault((a, b), {"from": a, "to": b, "kind": f["kind"], "label": f.get("label", ""),
                                      "status": "same", "pairs": []})
        e["pairs"].append([f["from"], f["to"]])
        if st != "same" and (e["status"] == "same" or st == "removed"):
            e["status"] = st
        if f.get("label") and f["label"] not in e["label"].split(", "):
            e["label"] = f"{e['label']}, {f['label']}" if e["label"] else f["label"]
    for e in edges.values():   # one component at an end: the flow meets it on its row
        if len(e["label"]) > 16:
            e["label"] = e["label"].split(", ")[0] + " +"
        froms = {p[0] for p in e["pairs"]}
        tos = {p[1] for p in e["pairs"]}
        if nodes[e["from"]]["type"] == "group" and len(froms) == 1:
            e["from_item"] = next(iter(froms))
        if nodes[e["to"]]["type"] == "group" and len(tos) == 1:
            e["to_item"] = next(iter(tos))
    heading = dict(cfg["picture"]["columns"])
    for n in nodes.values():
        n["col"] = SYSTEM_COLUMN.get(n["col"], n["col"])
    # the main flows: not one entry point starting another (one program's wiring), and from each box its strongest
    # few; every flow that changed stays. The rest show in a box's own view.
    kept: Dict[str, List[dict]] = defaultdict(list)
    for e in sorted(edges.values(), key=lambda e: (e["status"] == "same", -len(e["pairs"]), e["to"])):
        same_col = nodes[e["from"]]["col"] == nodes[e["to"]]["col"] in ("interface", "ui", "actors")
        if e["status"] != "same" or (not same_col and len(kept[e["from"]]) < MAIN_FLOWS):
            kept[e["from"]].append(e)
    edges = {(e["from"], e["to"]): e for es in kept.values() for e in es}
    columns = COLUMNS_FIRST + [("ui", heading.get("ui", "What people use")),
                               ("interface", heading.get("interface", "Entry points")),
                               ("core", "Inside the system"), ("outside", "Data and outside")]
    return {"columns": columns, "nodes": list(nodes.values()), "edges": list(edges.values())}


def container(cmp, box: str, marks: Optional[dict] = None, agent: Optional[str] = None,
              stack: Optional[Dict[str, Dict[str, int]]] = None, overlay: Optional[dict] = None
              ) -> Optional[Dict[str, Any]]:
    """One box zoomed in on: a container's components (or a lone part) as boxes, with the connections between them
    and every box outside it that they connect to, each flow as at the system level."""
    ov = _overlay(cmp, overlay)
    whole = system(cmp, marks, agent, stack, ov)
    nodes = {n["id"]: n for n in whole["nodes"]}
    if box not in nodes:
        holder = next((n["id"] for n in whole["nodes"] if any(i["id"] == box for i in n.get("items", []))), None)
        if holder is None:
            return None
        box = holder
    me = nodes[box]
    inside = [i["id"] for i in me.get("items", [])] or [box]
    item = {i["id"]: i for i in me.get("items", [])}
    bp, before, cfg = cmp.after, cmp.before, cmp.cfg
    marks = marks or graph.change_marks(cmp.diff, before, bp)
    kind = {**kinds_with(before, cfg, ov), **kinds_with(bp, cfg, ov)}
    comps = {**before["components"], **bp["components"]}
    out_nodes: Dict[str, dict] = {}
    for cid in inside:
        if cid in item:
            i = item[cid]
            out_nodes[cid] = {"id": cid, "col": kind.get(cid, me["col"]), "type": "component", "title": i["title"],
                              "status": i["status"], "agent": i["agent"],
                              "sub": _meta(cfg).get(cid, {}).get("about", "") or graph.files_word(comps[cid]["files"])}
        else:
            out_nodes[cid] = dict(me)
    edges = []
    # inside: component to component
    now = graph.connections(bp)
    for (s, t), (k, _n) in sorted(now.items()):
        if s in inside and t in inside and s != t and k == "call":
            edges.append({"from": s, "to": t, "kind": "call", "label": "",
                          "status": marks["edges"].get((s, t), (k, "same"))[1]})
    # out of and into the box: the system level's flows, met at their components
    for e in whole["edges"]:
        if box not in (e["from"], e["to"]):
            continue
        other = e["to"] if e["from"] == box else e["from"]
        on = nodes[other]
        out_nodes.setdefault(other, {**on, "items": None, "type": "component" if on["type"] == "group" else on["type"]}
                             if on["type"] == "group" else dict(on))
        if on["type"] == "group":
            out_nodes[other]["sub"] = f"{len(on['items'])} components"
        for s, t in e["pairs"]:
            a = s if s in inside else other
            b = t if t in inside else other
            if a != b:
                edges.append({"from": a, "to": b, "kind": e["kind"], "label": e["label"],
                              "status": marks["edges"].get((s, t), (e["kind"], e["status"]))[1]
                              if e["status"] == "same" else e["status"]})
    seen, uniq = set(), []
    for e in edges:
        if (e["from"], e["to"]) not in seen:
            seen.add((e["from"], e["to"]))
            uniq.append(e)
    for n in out_nodes.values():
        if n["id"] in comps and n["id"] in kind:
            n["col"] = kind[n["id"]]
        elif n["id"].startswith("x:"):
            n["col"] = "store" if n.get("type") == "store" else "external"
        elif n["id"].startswith("g:"):
            first = next((i["id"] for i in nodes[n["id"]].get("items", [])), "")
            n["col"] = kind.get(first, "logic")
    columns = COLUMNS_FIRST + [tuple(c) for c in cfg["picture"]["columns"]]
    return {"columns": columns, "nodes": list(out_nodes.values()), "edges": uniq, "box": box,
            "title": me["title"]}
