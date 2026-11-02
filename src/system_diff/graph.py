"""From a blueprint (or a diff) to the graph a picture draws: a column per role, each part where the config declares it,
so a part that did not change keeps its place from one version to the next. Drawn by `pictures.map_svg`."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from . import pictures
from .diff import files_word

PART_KINDS = ("ui", "interface", "logic", "data")
LAYER_RANK = {"new": 0, "shared": 1, "legacy": 2}
def drawn_flows(bp: dict, cfg: Dict[str, Any]) -> List[tuple]:
    """The connections worth drawing: not into a hub, not from or to a quiet part, not between two parts of the
    front end (that is page composition, not architecture), strong enough (`min_flow` import sites), and each part's
    strongest few (`top_flows`). Weaker links stay in system.json and out of the picture."""
    comps = bp["components"]
    ui = set(cfg["picture"]["ui_containers"])
    min_flow, top_flows = cfg["picture"]["min_flow"], cfg["picture"]["top_flows"]
    outgoing = defaultdict(list)
    for key, n in bp["flows"].items():
        src, dst = key.split(">")
        a, b = comps.get(src, {}), comps.get(dst, {})
        if n < min_flow or b.get("hub") or a.get("quiet") or b.get("quiet"):
            continue
        if a.get("container") in ui and b.get("container") in ui:
            continue
        outgoing[src].append((-n, dst))
    return [(src, dst) for src in sorted(outgoing) for _, dst in sorted(outgoing[src])[:top_flows]]


def drawn_api(bp: dict) -> List[tuple]:
    """Each front-end part's main API target, plus any other it calls often."""
    by_src = defaultdict(list)
    for key, n in bp["api"].items():
        src, dst = key.split(">")
        by_src[src].append((-n, dst))
    out = []
    for src in sorted(by_src):
        ranked = sorted(by_src[src])
        out += [(src, dst) for i, (neg, dst) in enumerate(ranked) if i == 0 or -neg >= 10]
    return out


def part_kind(cid: str, c: dict, bp: dict, cfg: Dict[str, Any]) -> str:
    """The column: declared in the config, else the front end is what people use and a part with routes an entry point."""
    if c.get("kind") in PART_KINDS:
        return c["kind"]
    if c.get("container") and c.get("container") in cfg["picture"]["ui_containers"]:
        return "ui"
    return "interface" if cid in set(bp["routes"].values()) else "logic"


def route_lines(bp: dict, cid: str) -> List[dict]:
    """A part's endpoints grouped by their first three path segments, the largest group first."""
    groups: Counter = Counter()
    for route, owner in bp["routes"].items():
        if owner == cid:
            segments = [s for s in route.split(" ", 1)[1].split("/") if s][:3]
            groups["/" + "/".join(segments)] += 1
    return [{"text": f"{g} ({n})", "code": True} for g, n in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))]


def part_order(c: dict, cid: str) -> tuple:
    return LAYER_RANK.get(c.get("layer"), 3), c.get("order", 10 ** 6), cid


def part_node(cid: str, c: dict, bp: dict, cfg: Dict[str, Any]) -> dict:
    sub = cfg["picture"]["hub_note"] if c.get("hub") else files_word(c["files"])
    container = c.get("container", "")
    if container and container not in cfg["picture"]["main_containers"]:
        sub = f"{bp.get('container_labels', {}).get(container, container)} · {sub}"
    kind = part_kind(cid, c, bp, cfg)
    return {"id": cid, "col": kind, "title": c["label"], "sub": sub, "type": "component", "style": c.get("layer", ""),
            "lines": route_lines(bp, cid) if kind == "interface" else []}


def outside_node(ext_id: str, e: dict, bp: dict) -> dict:
    n = len(e["used_by"])
    db = e.get("shape") == "db"
    node = {"id": "x:" + ext_id, "col": "store" if db else "external", "type": "store" if db else "host",
            "title": e["label"], "sub": f"used by {n} part{'' if n == 1 else 's'}", "lines": []}
    if ext_id == bp.get("store", "postgres") and bp["tables"]:
        schemas = Counter(t.split(".", 1)[0] for t in bp["tables"])
        node["sub"] = f"{len(bp['tables'])} tables · {node['sub']}"
        node["lines"] = [{"text": f"{s}: {k} table{'' if k == 1 else 's'}"} for s, k in schemas.most_common()]
    return node


def edges(bp: dict, cfg: Dict[str, Any]) -> List[dict]:
    """The connections a picture draws: each part's strongest calls, the API, and the heaviest users of each outside
    system (`max_ext_edges`; the node counts the rest, and system.json lists them)."""
    comps = bp["components"]
    out_edges = [{"from": s, "to": t, "kind": "call"} for s, t in drawn_flows(bp, cfg) + drawn_api(bp)]
    for key in sorted(bp["internal"]):
        src, container = key.split(">")
        out_edges += [{"from": src, "to": cid, "kind": "call"} for cid, c in sorted(comps.items())
                      if c.get("container") == container and not c.get("quiet")]
    for ext_id, e in bp["externals"].items():
        users = [u for u in e["used_by"] if not comps.get(u, {}).get("quiet")]
        kind = "data" if e.get("shape") == "db" else "network"
        out_edges += [{"from": u, "to": "x:" + ext_id, "kind": kind} for u in users[: cfg["picture"]["max_ext_edges"]]]
    seen, out = set(), []
    for e in out_edges:
        if (e["from"], e["to"]) not in seen:
            seen.add((e["from"], e["to"]))
            out.append(e)
    return out


def map_legend(nodes: List[dict], drawn: List[dict], cfg: Dict[str, Any]) -> List[tuple]:
    """The legend of a system map: a mark for each kind of part the map has, then each kind of line it draws."""
    words = cfg["picture"]["legend"]
    styles = {n.get("style") for n in nodes}
    kinds = {e["kind"] for e in drawn}
    marks = [("band", "new", words.get("new", "new")), ("dashed", "", words.get("legacy", "legacy")),
             ("band", "undeclared", words.get("undeclared", "undeclared"))]
    present = [m for m, s in zip(marks, ("new", "legacy", "undeclared")) if s in styles]
    return present + [m for m in pictures.MAP_LEGEND if m[0] == "line" and m[1] in kinds]


def system_graph(bp: dict, cfg: Dict[str, Any]) -> dict:
    comps = bp["components"]
    nodes = [part_node(cid, comps[cid], bp, cfg) for cid in sorted(comps, key=lambda k: part_order(comps[k], k))]
    nodes += [outside_node(x, e, bp) for x, e in bp["externals"].items()]
    drawn = edges(bp, cfg)
    return {"columns": cfg["picture"]["columns"], "nodes": nodes, "edges": drawn, "legend": map_legend(nodes, drawn, cfg)}


def system_svg(bp: dict, cfg: Dict[str, Any]) -> str:
    return pictures.map_svg(system_graph(bp, cfg), label="System map")


def diff_graph(d: dict, a: dict, b: dict, cfg: Dict[str, Any]) -> Optional[dict]:
    """Only what changed, lit, with the parts it touches beside it for context; None when nothing structural changed."""
    status: Dict[str, str] = {}
    lines: Dict[str, List[dict]] = defaultdict(list)
    subs: Dict[str, str] = {}
    changed: Dict[tuple, tuple] = {}  # (from, to) -> (kind, status)
    comps = {**a["components"], **b["components"]}
    containers = set(a.get("container_labels", {})) | set(b.get("container_labels", {}))
    store = "x:" + b.get("store", "postgres")
    for c in d["changes"]:
        op, entity = {"add": "added", "remove": "removed", "change": "changed"}[c["op"]], c["entity"]
        if entity == "component":
            status[c["id"]] = op
            if c["detail"]:
                subs[c["id"]] = c["detail"][0]
        elif entity == "route":
            owner = b["routes"].get(c["id"]) or a["routes"].get(c["id"])
            lines[owner].append({"text": c["id"], "code": True, "status": op})
            status.setdefault(owner, "changed")
        elif entity == "settings":
            lines[c["id"]].append({"text": "reads " + c["detail"][0], "code": True, "status": "added"})
            status.setdefault(c["id"], "changed")
        elif entity == "flow":
            src, dst = c["id"].split(">")
            targets = [k for k, v in sorted(comps.items()) if v.get("container") == dst] if dst in containers else [dst]
            for t in targets:
                changed[(src, t)] = ("call", op)
        elif entity == "external":
            user, ext = c["id"].split(">")
            meta = b["externals"].get(ext) or a["externals"].get(ext) or {}
            changed[(user, "x:" + ext)] = ("data" if meta.get("shape") == "db" else "network", op)
        elif entity in ("table", "migration"):
            detail = ", ".join(x.replace("+ column ", "+ ") for x in c["detail"])
            lines[store].append({"text": c["id"] + (f": {detail}" if detail else ""), "code": True, "status": op})
            status[store] = "changed"
    for ext in set(a["externals"]) ^ set(b["externals"]):
        status["x:" + ext] = "added" if ext in b["externals"] else "removed"
    hot = set(status) | {end for pair in changed for end in pair}
    if not hot:
        return None
    context = [e for e in edges(b, cfg) if (e["from"], e["to"]) not in changed]
    context += [e for e in edges(a, cfg) if status.get(e["from"]) == "removed" or status.get(e["to"]) == "removed"]
    shown = set(hot)
    for e in context:
        if e["from"] in hot or e["to"] in hot:
            shown |= {e["from"], e["to"]}
    nodes = []
    for cid in sorted(comps, key=lambda k: part_order(comps[k], k)):
        if cid in shown:
            n = part_node(cid, comps[cid], b if cid in b["components"] else a, cfg)
            n.update(status=status.get(cid, "same"), lines=lines.get(cid, []), sub=subs.get(cid, n["sub"]))
            nodes.append(n)
    outside = {**{x: (e, a) for x, e in a["externals"].items()}, **{x: (e, b) for x, e in b["externals"].items()}}
    for ext, (e, side) in outside.items():
        if "x:" + ext in shown:
            n = outside_node(ext, e, side)
            n.update(status=status.get(n["id"], "same"), lines=lines.get(n["id"], []))
            nodes.append(n)
    if store in shown and store not in {n["id"] for n in nodes}:
        nodes.append({"id": store, "col": "store", "type": "store", "title": "database schema", "sub": "",
                      "status": "changed", "lines": lines[store]})
    out_edges = [{"from": s, "to": t, "kind": k, "status": st} for (s, t), (k, st) in changed.items()]
    seen = set(changed)
    for e in context:
        if (e["from"], e["to"]) not in seen and e["from"] in shown and e["to"] in shown:
            seen.add((e["from"], e["to"]))
            out_edges.append({**e, "status": "same"})
    deploy = [f"the {c['id']} container" if c["entity"] == "container" else c["id"]
              for c in d["changes"] if c["entity"] in ("deploy", "container")]
    return {"columns": cfg["picture"]["columns"], "nodes": nodes, "edges": out_edges,
            "footer": ["Also changed, not drawn: " + ", ".join(deploy)] if deploy else []}


def diff_svg(d: dict, a: dict, b: dict, cfg: Dict[str, Any]) -> Optional[str]:
    graph = diff_graph(d, a, b, cfg)
    return pictures.map_svg(graph, diff=True, label="System diff") if graph else None
