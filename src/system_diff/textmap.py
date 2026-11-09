"""Pictures for the terminal, in box-drawing characters: the system map, a row of boxes for each of the map's columns
(what people use, entry points, logic, data access, stored data, outside systems), and one part ZOOMED IN on: the
parts that call it on its left, what it calls and uses on its right, its own endpoints and settings in its box. Drawn
from the same graphs as the SVG pictures (graph.py), with the same marks: + added, ~ changed, − removed.

A picture is a list of lines, each a list of (text, style) runs, so one picture prints as plain text (for an agent to
read, and for tests), with ANSI colours (the command line), or as Rich text (the terminal view). Standard library only.
"""
from __future__ import annotations

import textwrap
from typing import Dict, Iterable, List, Optional, Tuple

from .pictures import OP, cut, cut_code

Run = Tuple[str, str]   # (text, style): a style is space-separated words from STYLES, "" for none
Line = List[Run]

BOX = {"light": "┌─┐│└┘", "round": "╭─╮│╰╯", "heavy": "┏━┓┃┗┛", "dashed": "┌╌┐╎└┘"}
GAP = 5                 # between a box and the next column of a zoom: room for an arrow
STYLES = {              # word: (ANSI SGR, Rich style)
    "bold": ("1", "bold"), "muted": ("2", "dim"), "heading": ("1;2", "bold dim"),
    "added": ("32", "green"), "changed": ("33", "yellow"), "removed": ("31", "red"),
    "focus": ("1;36", "bold cyan"), "near": ("34", "blue"), "picked": ("7", "reverse"),
    "data": ("34", "blue"), "network": ("35", "magenta"),
}


class Canvas:
    """A grid of characters, each with its style."""

    def __init__(self) -> None:
        self.rows: List[List[Run]] = []

    def put(self, x: int, y: int, text: str, style: str = "") -> None:
        while len(self.rows) <= y:
            self.rows.append([])
        row = self.rows[y]
        if len(row) < x + len(text):
            row.extend([(" ", "")] * (x + len(text) - len(row)))
        for i, ch in enumerate(text):
            row[x + i] = (ch, style)

    def box(self, x: int, y: int, w: int, h: int, kind: str = "light", style: str = "") -> None:
        tl, hz, tr, vt, bl, br = BOX[kind]
        self.put(x, y, tl + hz * (w - 2) + tr, style)
        for i in range(1, h - 1):
            self.put(x, y + i, vt, style)
            self.put(x + w - 1, y + i, vt, style)
        self.put(x, y + h - 1, bl + hz * (w - 2) + br, style)

    def lines(self) -> List[Line]:
        """Each row as runs of one style, without trailing blanks."""
        out = []
        for row in self.rows:
            runs: Line = []
            for ch, st in row:
                if runs and runs[-1][1] == st:
                    runs[-1] = (runs[-1][0] + ch, st)
                else:
                    runs.append((ch, st))
            while runs and not runs[-1][1] and not runs[-1][0].strip():
                runs.pop()
            if runs and not runs[-1][1]:
                runs[-1] = (runs[-1][0].rstrip(), "")
            out.append(runs)
        return out


def plain(lines: Iterable[Line]) -> str:
    return "\n".join("".join(text for text, _ in line) for line in lines)


def ansi(lines: Iterable[Line]) -> str:
    def sgr(style: str) -> str:
        codes = [STYLES[w][0] for w in style.split() if w in STYLES]
        return f"\x1b[{';'.join(codes)}m" if codes else ""
    out = []
    for line in lines:
        out.append("".join(f"{sgr(st)}{text}\x1b[0m" if sgr(st) else text for text, st in line))
    return "\n".join(out)


def rich_style(style: str) -> str:
    return " ".join(STYLES[w][1] for w in style.split() if w in STYLES)


# ── the parts of a picture ────────────────────────────────────────────────────

def mark(n: dict) -> str:
    sign = OP.get(n.get("status", "same"), "")
    return f"{sign} " if sign else ""


def _status(item: dict) -> str:
    st = item.get("status", "same")
    return "" if st == "same" else st


def _box_kind(n: dict) -> str:
    if n.get("status") == "removed":
        return "dashed"
    return "round" if n.get("type") in ("host", "store") else "light"


def changed_lines(n: dict, most: int = 3) -> Line:
    """The rows a box lists on a map: what changed in it (an endpoint, a setting, a table), not all it has."""
    lines = [line for line in n.get("lines") or [] if _status(line)]
    rows: Line = [(f"{OP[line['status']]} {line['text']}", _status(line)) for line in lines[:most]]
    return rows + ([(f"+{len(lines) - most} more", "muted")] if len(lines) > most else [])


def _node(cv: Canvas, x: int, y: int, w: int, n: dict, sub: bool, focus: bool = False, near: bool = False,
          picked: bool = False, rows: Line = (), height: int = 0) -> None:
    """A box: the title (with its change mark), its subtitle when `sub`, then `rows`."""
    st = _status(n)
    border = "focus" if focus else st or ("near" if near else "muted")
    cv.box(x, y, w, height or (3 + int(sub) + len(rows)), "heavy" if focus else _box_kind(n), border)
    title = cut(mark(n) + n["title"], w - 4)
    cv.put(x + 2, y + 1, title, " ".join(s for s in ("bold", st, "picked" if picked else "") if s))
    if sub and n.get("sub"):
        cv.put(x + 2, y + 2, cut(n["sub"], w - 4), "muted")
    for i, (text, style) in enumerate(rows):
        cv.put(x + 2, y + 2 + int(sub) + i, cut_code(text, w - 4), style)


def legend(nodes: Iterable[dict], edges: Iterable[dict] = (), focus: bool = False) -> Line:
    """A key to what this picture shows, and only that."""
    statuses = {n.get("status", "same") for n in nodes} | {e.get("status", "same") for e in edges}
    kinds = {e.get("kind", "call") for e in edges}
    out: Line = []

    def item(text: str, style: str) -> None:
        if out:
            out.append(("   ", ""))
        out.append((text, style))

    for st, word in (("added", "added"), ("changed", "changed"), ("removed", "removed")):
        if st in statuses:
            item(f"{OP[st]} {word}", st)
    if focus:
        item("┏━┓ being worked on", "focus")
    for kind, glyph, word in (("call", "─▸", "calls"), ("data", "─▸", "reads and writes data"),
                              ("network", "╌▸", "over the network")):
        if kind in kinds:
            item(f"{glyph} {word}", "muted" if kind == "call" else kind)
    return out


# ── the whole system ──────────────────────────────────────────────────────────

def render_map(graph: dict, width: int = 100, focus: Optional[str] = None, picked: Optional[str] = None) -> List[Line]:
    """The system map (or a diff's picture) as rows of boxes, one row per column of the SVG map. `focus`: the part
    being worked on, drawn heavy; `picked`: a part chosen in the terminal, its connections named below the map and
    the parts it connects to tinted."""
    nodes = graph["nodes"]
    edges = graph.get("edges", [])
    near = {e["to"] for e in edges if e["from"] == picked} | {e["from"] for e in edges if e["to"] == picked}
    longest = max([len(mark(n) + n["title"]) for n in nodes] + [len(n.get("sub", "")) for n in nodes] + [12])
    w = max(16, min(30, longest + 4))
    per_row = max(1, (width + 1) // (w + 1))
    cv = Canvas()
    y = 0
    for key, heading in graph["columns"]:
        col = [n for n in nodes if n["col"] == key]
        if not col:
            continue
        cv.put(0, y, heading.upper(), "heading")
        y += 1
        for i in range(0, len(col), per_row):
            row = col[i:i + per_row]
            height = 4 + max(len(changed_lines(n)) for n in row)  # the boxes of a row share one height
            for j, n in enumerate(row):
                _node(cv, j * (w + 1), y, w, n, sub=True, focus=n["id"] == focus, near=n["id"] in near,
                      picked=n["id"] == picked, rows=changed_lines(n), height=height)
            y += height
    lines = cv.lines()
    if picked and any(n["id"] == picked for n in nodes):
        title = {n["id"]: n["title"] for n in nodes}
        calls = sorted({title[e["to"]] for e in edges if e["from"] == picked and e["to"] in title}, key=str.lower)
        called = sorted({title[e["from"]] for e in edges if e["to"] == picked and e["from"] in title}, key=str.lower)
        rest = (" calls " + (", ".join(calls) if calls else "nothing else in the system") + "; called by "
                + (", ".join(called) if called else "nothing in the system"))
        wrapped = textwrap.wrap(title[picked] + rest, max(20, width)) or [""]
        first = wrapped[0]
        lines += [[], [(title[picked], "bold picked"), (first[len(title[picked]):], "")]
                  if first.startswith(title[picked]) else [(first, "")]]
        lines += [[(more, "")] for more in wrapped[1:]]
    key_line = legend(nodes, [], focus=focus is not None and any(n["id"] == focus for n in nodes))
    return lines + ([[], key_line] if key_line else [])


# ── one part, zoomed in ───────────────────────────────────────────────────────

def _content(n: dict, inner: int, most: int = 8) -> Line:
    """The rows inside a zoomed part's box: its title, subtitle, then its endpoints and settings."""
    rows: Line = [(cut(mark(n) + n["title"], inner), " ".join(s for s in ("bold", _status(n)) if s) or "bold")]
    if n.get("sub"):
        rows.append((cut(n["sub"], inner), "muted"))
    lines = n.get("lines") or []
    for line in lines[:most]:
        st = _status(line)
        sign = OP.get(line.get("status", "same")) or "·"
        text = cut_code(line["text"], inner - 2) if line.get("code") else cut(line["text"], inner - 2)
        rows.append((f"{sign} {text}", st))
    if len(lines) > most:
        rows.append((f"+{len(lines) - most} more", "muted"))
    return rows


def _edge_style(e: Optional[dict]) -> Tuple[str, str]:
    """(line glyph, style) of a connection."""
    if not e:
        return "─", "muted"
    st, kind = _status(e), e.get("kind", "call")
    glyph = "╌" if st == "removed" or kind == "network" else "─"
    return glyph, st or ("muted" if kind == "call" else kind)


def render_zoom(graph: dict, width: int = 100, picked: Optional[str] = None, most: int = 10) -> List[Line]:
    """One part (`graph`: graph.neighbourhood) with what calls it on its left and what it calls and uses on its
    right, an arrow for each connection. Narrower than the parts need, the three are stacked instead."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    part = graph["focus"]
    left, right = graph["callers"][:most], graph["callees"][:most]
    more_left, more_right = len(graph["callers"]) - len(left), len(graph["callees"]) - len(right)
    edge = {(e["from"], e["to"]): e for e in graph["edges"]}
    sides = int(bool(left)) + int(bool(right))
    side_w = max([len(mark(nodes[i]) + nodes[i]["title"]) + 4 for i in left + right] + [14])
    side_w = min(side_w, 28)
    content = _content(nodes[part], 60)
    centre_w = max(24, min(44, max(len(t) for t, _ in content) + 4))
    if centre_w + sides * (side_w + GAP) > width and sides:
        spare = width - sides * GAP
        centre_w = max(22, min(centre_w, spare - sides * 14))
        side_w = max(12, min(side_w, (spare - centre_w) // sides))
    if centre_w + sides * (side_w + GAP) > width:
        return _stacked(graph, nodes, left, right, more_left, more_right, width, picked)
    cv = Canvas()
    cx = side_w + GAP if left else 0
    rx = cx + centre_w + GAP
    is_outside = part.startswith("x:")
    if left:
        cv.put(0, 0, "USED BY" if is_outside else "CALLED BY", "heading")
    heading = dict((k, h) for k, h in graph["columns"]).get(nodes[part]["col"], "")
    cv.put(cx, 0, cut(heading.upper(), centre_w), "heading")
    if right:
        outside = any(i.startswith("x:") for i in right)
        inside = any(not i.startswith("x:") for i in right)
        cv.put(rx, 0, "CALLS AND USES" if outside and inside else "USES" if outside else "CALLS", "heading")
    rows = _content(nodes[part], centre_w - 4)
    height = max(len(rows) + 2, 3 * max(len(left) + bool(more_left), len(right) + bool(more_right), 1))
    cv.box(cx, 1, centre_w, height, "heavy", "focus")
    for i, (text, style) in enumerate(rows):
        cv.put(cx + 2, 2 + i, text, style)
    for i, nid in enumerate(left):
        y = 1 + 3 * i
        _node(cv, 0, y, side_w, nodes[nid], sub=False, picked=nid == picked)
        glyph, style = _edge_style(edge.get((nid, part)))
        cv.put(side_w - 1, y + 1, "├", style)
        cv.put(side_w, y + 1, glyph * (GAP - 1) + "▸", style)
        cv.put(cx, y + 1, "┨", "focus")
    for i, nid in enumerate(right):
        y = 1 + 3 * i
        _node(cv, rx, y, side_w, nodes[nid], sub=False, picked=nid == picked)
        glyph, style = _edge_style(edge.get((part, nid)))
        cv.put(cx + centre_w - 1, y + 1, "┠", "focus")
        cv.put(cx + centre_w, y + 1, glyph * (GAP - 1) + "▸", style)
    if more_left:
        cv.put(2, 2 + 3 * len(left), f"+{more_left} more", "muted")
    if more_right:
        cv.put(rx + 2, 2 + 3 * len(right), f"+{more_right} more", "muted")
    key_line = legend(graph["nodes"], graph["edges"])
    return cv.lines() + ([[], key_line] if key_line else [])


def _stacked(graph: dict, nodes: Dict[str, dict], left: List[str], right: List[str], more_left: int,
             more_right: int, width: int, picked: Optional[str]) -> List[Line]:
    """The zoom for a narrow terminal: callers, then the part, then what it calls, top to bottom."""
    part = graph["focus"]
    cv = Canvas()
    y = 0

    def row_of(ids: List[str], more: int, heading: str) -> None:
        nonlocal y
        cv.put(0, y, heading, "heading")
        y += 1
        w = max(12, min(width, max(len(mark(nodes[i]) + nodes[i]["title"]) + 4 for i in ids)))
        per_row = max(1, (width + 1) // (w + 1))
        for k in range(0, len(ids), per_row):
            for j, nid in enumerate(ids[k:k + per_row]):
                _node(cv, j * (w + 1), y, w, nodes[nid], sub=False, picked=nid == picked)
            y += 3
        if more:
            cv.put(2, y, f"+{more} more", "muted")
            y += 1

    if left:
        row_of(left, more_left, "USED BY" if part.startswith("x:") else "CALLED BY")
        cv.put(2, y, "▾", "muted")
        y += 1
    w = max(12, min(width, 44))
    rows = _content(nodes[part], w - 4)
    cv.box(0, y, w, len(rows) + 2, "heavy", "focus")
    for i, (text, style) in enumerate(rows):
        cv.put(2, y + 1 + i, text, style)
    y += len(rows) + 2
    if right:
        cv.put(2, y, "▾", "muted")
        y += 1
        row_of(right, more_right, "CALLS AND USES")
    key_line = legend(graph["nodes"], graph["edges"])
    return cv.lines() + ([[], key_line] if key_line else [])
