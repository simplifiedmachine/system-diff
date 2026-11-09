"""`system-diff panel`: what a pane beside the agent shows, as JSON for whatever draws it (the Claude Code mod in
mods/system-diff): rows of (text, style) runs, the styles textmap's, and the parts the person can pick there.

Four views, as in the terminal view:
  now      what the agent is doing and what that part is, what this turn did to the system and why it matters, the
           part zoomed in on (the agent's, or `--part`), what is changing in it and the topics behind it, what just
           happened, and a topic to learn; the parts it connects to can be picked to zoom in on them
  map      the whole system, every part pickable
  changes  exactly what changed since the checkpoint, and this turn
  topics   what the system is built with
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import activity, config, graph, learn, textmap
from .compare import compare, turn_sha
from .extract import Code
from .sources import GitSource

VIEWS = ("now", "map", "changes", "topics")


def markdown_rows(text: str, width: int) -> List[textmap.Line]:
    """Markdown as plain rows: headings bold, emphasis dropped, a link as its address, long lines wrapped."""
    import textwrap
    out: List[textmap.Line] = []
    for line in text.splitlines():
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"(\2)", line)
        if line.startswith("#"):
            out.append([(line.lstrip("# ").upper(), "heading")])
            continue
        bold = line.startswith("**") and line.endswith("**")
        line = line.replace("**", "").replace("`", "")
        line = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", line)
        indent = "  " if line.startswith("- ") else ""
        for i, piece in enumerate(textwrap.wrap(line, max(20, width), subsequent_indent=indent) or [""]):
            out.append([(piece, "bold" if bold else "")])
    return out


# What each colour means, the same everywhere in the pane: blue is where the agent is (and where you are), yellow is
# what changed and what needs you, green is what is new, done or to learn, white is what to read, gray is the
# background (why, when, labels), red only what was taken away. The styles are words the drawing maps to colours.
ATTENTION = 6  # a change this weighty (learn.WEIGHT) is one to look at before the agent goes on
SIGN_STYLE = {"+": "added", "~": "changed", "−": "removed"}


def _cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: max(1, n - 1)] + "…"


def _section(title: str) -> List[textmap.Line]:
    return [[], [(title, "heading")]]


def _told(item: Dict[str, Any], width: int, style: str = "") -> List[textmap.Line]:
    """A change said plainly, its sign coloured, why it matters in gray on the next line."""
    sign = {"breaking-interface": "−", "removed-component": "−", "removed-external": "−", "removed-flow": "−"}.get(
        item.get("impact", ""), "+" if any(w in item["text"] for w in (" now ", "A new")) else "~")
    rows = [[(f" {sign} ", SIGN_STYLE[sign]), (_cut(item["text"].rstrip("."), width - 3), style or "bold")]]
    if item.get("why"):
        rows.append([("   " + _cut(item["why"], width - 3), "muted")])
    return rows


def view_now(root: Path, width: int, part: Optional[str]) -> Dict[str, Any]:
    cfg = config.lenient(root)
    code = Code(cfg)
    events = activity.events(root, 200)
    now = activity.now(events, code)
    c = compare(root, "turn" if turn_sha(root) else "checkpoint", ".")  # one look for the story and the picture
    told = learn.story(c.diff)
    turn = {"story": told, "count": len(told)}
    shown = part or now["part"]
    brief = activity.load_brief(root, now["part"]) if now["part"] else {}
    out: List[textmap.Line] = []

    # 1. Where the agent is, and what that is.
    if now["part"]:
        name = brief.get("label") or activity.label(cfg, now["part"])
        if now["working"]:
            verb = "✎ Editing" if now["doing"] == "editing" else "◎ Reading"
            out.append([("● Claude is working  ", "accent"), (f"{verb} ", "accent"), (name, "accent bold")])
        else:
            out.append([("○ Claude is waiting for you  ", "muted"), ("last on ", "muted"), (name, "bold")])
        if brief.get("role"):
            out.append([("  " + _cut(brief["role"], width - 2), "muted")])
    else:
        out.append([("○ Waiting for Claude to read or edit code", "muted")])
    if now.get("prompt"):
        out.append([("  You asked  ", "muted"), (_cut(now["prompt"], width - 13), "")])

    # 2. What needs the person, then the rest of what the turn did.
    story = turn.get("story") or []
    urgent = [s for s in story if s.get("weight", 0) >= ATTENTION]
    if urgent:
        out += [[], [("▲ NEEDS YOUR ATTENTION", "attention")]]
        for item in urgent:
            out += _told(item, width)
        out.append([("   Worth a look before Claude goes on: ask it why, or steer it.", "muted")])
    rest = [s for s in story if s not in urgent]
    out += _section("THIS TURN" if c.base == "the start of this turn" else f"SINCE {c.base.upper()}")
    if rest or urgent:
        for item in rest[:4]:
            out += _told(item, width)
        more = turn.get("count", len(story)) - len(urgent) - min(len(rest), 4)
        if more > 0:
            out.append([(f"   and {more} more: the Changes tab has them all", "muted")])
        if not rest:
            out.append([("   nothing else", "muted")])
    else:
        out.append([("   nothing in the system's structure has changed yet", "muted")])

    # 3. The part, zoomed in on (a diagram the pane draws with blocks to pick), and what it is.
    picks: List[Dict[str, str]] = []
    head, out, diagram = out, [], None
    if shown:
        marks = graph.change_marks(c.diff, c.before, c.after)
        hood = graph.neighbourhood(c.after, c.cfg, shown, marks, c.before)
        if hood:
            title = hood["nodes"][0]["title"]
            head += _section(f"ZOOMED IN ON {title.upper()}" + ("  (you picked it)" if part and part != now["part"]
                                                                   else ""))
            diagram = zoom_diagram(hood, now["part"])
            picks = [{"id": b["id"], "title": b["title"], "status": b["status"]}
                     for b in diagram["callers"] + diagram["callees"]]

    # 4. What just happened, newest first.
    feed = activity.feed(events, code, cfg, most=8)
    if feed:
        out += _section("WHAT JUST HAPPENED")
        style = {"prompt": "bold", "read": "muted", "edit": "", "change": "attention", "stop": "good"}
        mark = {"prompt": "›", "read": "◎", "edit": "✎", "change": "▲", "stop": "✓"}
        for item in feed:
            text = item["text"].replace("Claude stopped", "Claude finished")
            out.append([(time.strftime("%H:%M ", time.localtime(item["at"])), "muted"),
                        (f"{mark.get(item['kind'], '·')} ", style.get(item["kind"], "")),
                        (_cut(text, width - 9), style.get(item["kind"], ""))])

    # 5. Something to learn about the part.
    topics = brief.get("topics") or []
    if topics:
        name, what = topics[int(time.time() // 20) % len(topics)]
        out += _section("LEARN")
        out.append([("★ ", "good"), (name, "good bold")])
        out += [[(piece, "muted")] for piece in _wrap(what, width)]
    return {"head": head, "diagram": diagram, "rows": out, "picks": picks, "part": shown or ""}


def _wrap(text: str, width: int) -> List[str]:
    import textwrap
    return textwrap.wrap(text, max(20, width - 2)) or [""]


def _block(n: dict, focus: Optional[str]) -> Dict[str, Any]:
    """A box of a diagram, for the pane to draw and the person to pick."""
    return {"id": n["id"], "title": n["title"], "sub": n.get("sub", ""), "status": n.get("status", "same"),
            "outside": n.get("type") in ("host", "store"), "agent": n["id"] == focus}


def zoom_diagram(hood: dict, focus: Optional[str], most: int = 12) -> Dict[str, Any]:
    """graph.neighbourhood as blocks: the part (with its endpoints, settings and tables), what calls it, what it calls
    and uses, each connection's kind and change."""
    nodes = {n["id"]: n for n in hood["nodes"]}
    edges = {(e["from"], e["to"]): e for e in hood["edges"]}
    part = hood["focus"]

    def side(ids: List[str], edge_of) -> List[Dict[str, Any]]:
        out = []
        for nid in ids[:most]:
            e = edge_of(nid) or {}
            out.append({**_block(nodes[nid], focus), "edge": e.get("kind", "call"), "edge_status": e.get("status", "same")})
        return out
    me = _block(nodes[part], focus)
    me["lines"] = [[line["text"], line.get("status", "same")] for line in nodes[part].get("lines") or []][:8]
    heading = dict((k, h) for k, h in hood["columns"]).get(nodes[part]["col"], "")
    return {"kind": "zoom", "heading": heading, "part": me,
            "callers": side(hood["callers"], lambda n: edges.get((n, part))),
            "callees": side(hood["callees"], lambda n: edges.get((part, n))),
            "more_callers": max(0, len(hood["callers"]) - most), "more_callees": max(0, len(hood["callees"]) - most),
            "outside_caller": part.startswith("x:")}


def view_map(root: Path, width: int) -> Dict[str, Any]:
    c = compare(root, "checkpoint", ".")
    focus = activity.now(activity.events(root, 200), Code(c.cfg))["part"]
    g = graph.overview(c.after, c.cfg, graph.change_marks(c.diff, c.before, c.after), c.before)
    columns = []
    for key, heading in g["columns"]:
        col = [_block(n, focus) for n in g["nodes"] if n["col"] == key]
        if col:
            columns.append({"heading": heading, "blocks": col})
    picks = [{"id": b["id"], "title": b["title"], "status": b["status"]} for col in columns for b in col["blocks"]]
    return {"head": [], "diagram": {"kind": "map", "columns": columns}, "rows": [], "picks": picks, "part": ""}


def view_changes(root: Path, width: int) -> Dict[str, Any]:
    out: List[textmap.Line] = []
    for point in ["checkpoint"] + (["turn"] if turn_sha(root) else []):
        c = compare(root, point, ".")
        heading = "THIS TURN" if point == "turn" else "SINCE " + ("YOUR CHECKPOINT" if c.base == "checkpoint" else "HEAD")
        out += [[(heading, "heading")], [(c.diff["headline"], "bold")]]
        for told in learn.story(c.diff, final=True):
            out.append([("• ", "muted"), (told["text"], "")] + ([(f" · {told['why']}", "muted")] if told["why"] else []))
        out += [[(line, "muted")] for line in c.diff["plan"].splitlines()] + [[]]
    return {"head": [], "diagram": None, "rows": out, "picks": [], "part": ""}


def view_topics(root: Path, width: int) -> Dict[str, Any]:
    c = compare(root, "checkpoint", ".")
    text = learn.system_markdown(learn.system_topics(GitSource(c.head_sha, root), c.after, c.cfg))
    return {"head": [], "diagram": None, "rows": markdown_rows(text, width), "picks": [], "part": ""}


def show(root: Path, width: int = 80, view: str = "now", part: Optional[str] = None) -> Dict[str, Any]:
    if view == "map":
        return view_map(root, width)
    if view == "changes":
        return view_changes(root, width)
    if view == "topics":
        return view_topics(root, width)
    return view_now(root, width, part)


def rows(root: Path, width: int = 80) -> List[textmap.Line]:
    got = show(root, width)
    return got["head"] + got["rows"]


def main(root: Path, width: int, view: str = "now", part: Optional[str] = None, protocol: int = 1) -> int:
    if protocol >= 2:
        from . import pane
        print(json.dumps(pane.show(root, width, view, part or None), ensure_ascii=False))
        return 0
    got = show(root, width, view, part or None)
    for key in ("head", "rows"):
        got[key] = [[list(run) for run in line] for line in got[key]]
    print(json.dumps(got, ensure_ascii=False))
    return 0
