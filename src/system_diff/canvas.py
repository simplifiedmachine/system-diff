"""Pictures for the pane beside the chat, laid out in character cells: rows of styled runs, each cell knowing what drew
it (a block, a connection), so the pane can let a person pick a block with the arrow keys or a click and light up its
ins and outs, both the lines and the blocks at their other ends.

The system diagram is laid out as the SVG map is (pictures.map_svg): a column per role, left to right, each part's box
where the config puts it. A connection leaves its box's side, runs down or up a lane of the channel between two
columns, crosses a column through a gap between its boxes, and enters its target's side with an arrow; one within a
column loops through the channel on its right. Lanes are given out so no two lines run on top of each other; where
lines meet or cross, the box-drawing character says so (├ ┬ ┼).

A PAGE is what the pane draws: its rows, its blocks (where each is, what Enter and space do), the ids to light when a
block is picked, and a few rows of detail per block, shown in the slot under the picture the block is in.
"""
from __future__ import annotations

import textwrap
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .pictures import OP

Run = List[str]   # [text, style] or [text, style, ids]: ids space-separated

GLYPH = {frozenset("EW"): "─", frozenset("NS"): "│", frozenset("ES"): "┌", frozenset("SW"): "┐",
         frozenset("NE"): "└", frozenset("NW"): "┘", frozenset("NES"): "├", frozenset("NSW"): "┤",
         frozenset("ESW"): "┬", frozenset("NEW"): "┴", frozenset("NESW"): "┼", frozenset("E"): "─",
         frozenset("W"): "─", frozenset("N"): "│", frozenset("S"): "│"}
DASH = {"─": "╌", "│": "╎"}
BOX = {"light": "┌─┐│└┘", "round": "╭─╮│╰╯", "heavy": "┏━┓┃┗┛", "dashed": "┌╌┐╎└┘"}
WEIGHT = {"removed": 3, "added": 2, "changed": 1}   # a cell lines share takes the weightiest one's colour
SIGN = {"added": "+ ", "changed": "~ ", "removed": "− "}


def cut(text: str, n: int) -> str:
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


class Grid:
    """Character cells, each with its style and the ids of what drew it; lines kept apart until drawn, so where they
    meet they join."""

    def __init__(self) -> None:
        self.cells: Dict[Tuple[int, int], list] = {}
        self.links: Dict[Tuple[int, int], dict] = {}
        self.blocks: List[Dict[str, Any]] = []
        self.lights: Dict[str, List[str]] = {}
        self.details: Dict[str, List[List[Run]]] = {}

    def put(self, x: int, y: int, text: str, style: str = "", ids: Iterable[str] = ()) -> None:
        for i, ch in enumerate(text):
            self.cells[(x + i, y)] = [ch, style, set(ids)]

    def box(self, x: int, y: int, w: int, h: int, kind: str, style: str, ids: Iterable[str] = ()) -> None:
        tl, hz, tr, vt, bl, br = BOX[kind]
        self.put(x, y, tl + hz * (w - 2) + tr, style, ids)
        for i in range(1, h - 1):
            self.put(x, y + i, vt, style, ids)
            self.put(x + w - 1, y + i, vt, style, ids)
        self.put(x, y + h - 1, bl + hz * (w - 2) + br, style, ids)

    def link(self, x: int, y: int, dirs: Iterable[str], eid: str, style: str, dash: bool) -> None:
        c = self.links.setdefault((x, y), {"dirs": set(), "ids": set(), "style": "", "dash": True})
        c["dirs"] |= set(dirs)
        c["ids"].add(eid)
        if not c["style"] or WEIGHT.get(style, 0) > WEIGHT.get(c["style"], 0):
            c["style"] = style
        c["dash"] = c["dash"] and dash

    def path(self, points: Sequence[Tuple[int, int]], eid: str, style: str, dash: bool) -> None:
        """An orthogonal line through `points`, each step horizontal or vertical."""
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            if y1 == y2:
                lo, hi = sorted((x1, x2))
                for x in range(lo, hi + 1):
                    self.link(x, y1, ("W" if x > lo else "") + ("E" if x < hi else ""), eid, style, dash)
            else:
                lo, hi = sorted((y1, y2))
                for y in range(lo, hi + 1):
                    self.link(x1, y, ("N" if y > lo else "") + ("S" if y < hi else ""), eid, style, dash)

    def size(self) -> Tuple[int, int]:
        keys = list(self.cells) + list(self.links)
        return (max((x for x, _ in keys), default=-1) + 1, max((y for _, y in keys), default=-1) + 1)

    def rows(self) -> List[List[Run]]:
        """Each row as runs of one style and one set of ids; lines under boxes are hidden by them."""
        w, h = self.size()
        out = []
        for y in range(h):
            runs: List[Run] = []
            last: Optional[tuple] = None
            for x in range(w):
                cell = self.cells.get((x, y))
                if cell is None and (x, y) in self.links:
                    c = self.links[(x, y)]
                    ch = GLYPH.get(frozenset(c["dirs"]), "·")
                    if c["dash"]:
                        ch = DASH.get(ch, ch)
                    cell = [ch, c["style"] or "muted", c["ids"]]
                ch, style, ids = cell if cell else (" ", "", set())
                key = (style, " ".join(sorted(ids)))
                if runs and key == last:
                    runs[-1][0] += ch
                else:
                    runs.append([ch, style] + ([key[1]] if key[1] else []))
                    last = key
            while runs and not runs[-1][1] and len(runs[-1]) == 2 and not runs[-1][0].strip():
                runs.pop()
            if runs and len(runs[-1]) == 2 and not runs[-1][1]:
                runs[-1][0] = runs[-1][0].rstrip()
            out.append(runs)
        return out


# ── the system, as boxes in columns joined by lines ──────────────────────────

def box_kind(n: dict) -> str:
    if n.get("status") == "removed":
        return "dashed"
    if n.get("focus"):
        return "heavy"
    return "round" if n.get("type") in ("host", "store") else "light"


def box_style(n: dict) -> str:
    """Blue where the agent is, the change's colour where something changed, gray otherwise."""
    if n.get("focus") or n.get("agent"):
        return "accent"
    st = n.get("status", "same")
    return st if st != "same" else "muted"


def title_style(n: dict) -> str:
    """White for a part like any other, bold in the change's colour, bold blue where the agent is."""
    st = n.get("status", "same")
    if n.get("focus") or n.get("agent"):
        return "title bold accent"
    return f"title bold {st}" if st != "same" else "title"


ZOOMS = {0: (20, 32, 2, 6, 4), 1: (12, 22, 0, 5, 3), 2: (8, 16, 0, 5, 3)}  # min box, max box, rows, channel, gap
                                                                         # by zoom: full, compact, tiny


def layered(nodes: List[dict], edges: List[dict], columns: List[Sequence[str]], *, width: int = 100,
            centre: bool = False, most_lines: int = 2, zoom: int = 0) -> Grid:
    """A system diagram: `nodes` (id, col, title, sub, status, type: actor|component|group|store|host; `agent` where
    the agent works, `focus` for the box zoomed in on, `items` for the components a container holds, `lines` for
    rows inside a box) in `columns` order, joined by `edges` (from, to, kind, status, label; `from_item`/`to_item`:
    the component a flow meets inside a container; `pairs`: the connections it stands for). Boxes narrow to fit
    `width` where they can; past that the picture is wider than the pane, which scrolls it. `centre`: each column
    centred on the tallest, else top-aligned."""
    g = Grid()
    present = [k for k, _h in columns if any(n["col"] == k for n in nodes)]
    if not present:
        return g
    least, most_w, rows_in, chan_min, chan_pad = ZOOMS.get(zoom, ZOOMS[0])
    most_lines = min(most_lines, rows_in)
    if zoom:   # compact: no labels on flows (a picked box's detail names them), tiny: no subtitles either
        edges = [{**e, "label": ""} for e in edges]
        if zoom >= 2:
            nodes = [{**n, "sub": ""} for n in nodes]
    heading = {k: h for k, h in columns}
    ci = {k: i for i, k in enumerate(present)}
    col_nodes = [[n for n in nodes if n["col"] == k] for k in present]
    where = {n["id"]: ci[n["col"]] for n in nodes}
    node = {n["id"]: n for n in nodes}
    edges = [e for e in edges if e["from"] in where and e["to"] in where and e["from"] != e["to"]]

    def content(n: dict) -> List[Tuple[str, str, str]]:
        """(text, style, block id) for each row inside the box."""
        bid = "b:" + n["id"]
        rows = [(("● " if n.get("agent") and not n.get("items") else "") + SIGN.get(n.get("status", "same"), "")
                 + (n["title"].upper() if n.get("items") else n["title"]),
                 "heading" if n.get("items") and not n.get("agent") else title_style(n), bid)]
        rows.append((n.get("sub", ""), "muted", bid))   # always a second row: lines in and out keep their own rows
        for it in n.get("items") or []:
            st = it.get("status", "same")
            style = ("title bold accent" if it.get("agent") else f"title bold {st}" if st != "same"
                     else "title muted" if it.get("hub") else "title")
            rows.append((("● " if it.get("agent") else "") + SIGN.get(st, "") + it["title"], style, "b:" + it["id"]))
        for line in (n.get("lines") or [])[:most_lines]:
            st = line.get("status", "same")
            rows.append((SIGN.get(st, "· ") + line["text"], st if st != "same" else "muted", bid))
        more = len(n.get("lines") or []) - most_lines
        if more > 0:
            rows.append((f"+{more} more", "muted", bid))
        return rows

    contents = {n["id"]: content(n) for n in nodes}
    item_row = {it["id"]: (n["id"], 3 + j) for n in nodes for j, it in enumerate(n.get("items") or [])}

    # widths: as wide as the longest row wants, narrowed together when the whole is wider than the pane
    want = [min(most_w, max([len(t) + 4 for n in col for t, _s, _b in contents[n["id"]][:1]]
                            + [len(t) + 4 for n in col for t, _s, _b in contents[n["id"]][2:]]
                            + [min(len(heading[present[i]]) + 1, most_w), least]))
            for i, col in enumerate(col_nodes)]
    label_w = max([len(e.get("label") or "") for e in edges] + [0])
    gaps = (chan_min + 2 + label_w) * len(present)
    while sum(want) + gaps > width and max(want) > least:
        biggest = max(range(len(want)), key=lambda i: want[i])
        want[biggest] -= 1
    heights = {n["id"]: 2 + len(contents[n["id"]]) for n in nodes}

    # rows: boxes stacked under their column's heading, a row apart
    top, pos_y = 2, {}
    col_h = [sum(heights[n["id"]] + 1 for n in col) - 1 for col in col_nodes]
    tallest = max(col_h)
    for i, col in enumerate(col_nodes):
        y = top + ((tallest - col_h[i]) // 2 if centre else 0)
        for n in col:
            pos_y[n["id"]] = y
            y += heights[n["id"]] + 1
    bottom = top + tallest

    def port(nid: str, item: Optional[str], side_out: bool, right: bool) -> int:
        """The row a flow leaves or enters a box by: a component's own row in a container; else the title row for
        lines leaving right and entering left (the way most flow), the row under it for the other two."""
        if item and item in item_row and item_row[item][0] == nid:
            return pos_y[nid] + item_row[item][1]
        return pos_y[nid] + (1 if side_out == right else 2)

    def free_rows(i: int) -> List[int]:
        taken = set()
        for n in col_nodes[i]:
            taken |= set(range(pos_y[n["id"]], pos_y[n["id"]] + heights[n["id"]]))
        return [y for y in range(1, bottom + 2) if y not in taken]

    crossings: Dict[Tuple[int, int], int] = defaultdict(int)

    def cross(i: int, want_y: float) -> int:
        rows = free_rows(i)
        y = min(rows, key=lambda r: (abs(r - want_y) + 3 * crossings[(i, r)], r))
        crossings[(i, y)] += 1
        return y

    routes = []
    for e in edges:
        s, t = e["from"], e["to"]
        cs, ct = where[s], where[t]
        if cs < ct:
            ys, yt = port(s, e.get("from_item"), True, True), port(t, e.get("to_item"), False, False)
            hops, y = [], ys
            for k in range(cs, ct):
                if k + 1 == ct:
                    hops.append((k, y, yt))
                else:
                    y2 = cross(k + 1, ys + (yt - ys) * (k + 1 - cs) / (ct - cs))
                    hops.append((k, y, y2))
                    y = y2
        elif cs > ct:
            ys, yt = port(s, e.get("from_item"), True, False), port(t, e.get("to_item"), False, True)
            hops, y = [], ys
            for k in range(cs - 1, ct - 1, -1):
                if k == ct:
                    hops.append((k, y, yt))
                else:
                    y2 = cross(k, ys + (yt - ys) * (cs - k) / (cs - ct))
                    hops.append((k, y, y2))
                    y = y2
        else:
            ys, yt = port(s, e.get("from_item"), True, True), port(t, e.get("to_item"), False, True)
            hops = [(cs, ys, yt)]
        routes.append((e, cs, ct, ys, yt, hops))

    # lanes: each channel's vertical runs, given the first lane free over their rows. The lines leaving one box
    # into a channel share a trunk there, each branching off at its own row.
    groups: Dict[tuple, List[Tuple[int, int, int]]] = defaultdict(list)
    sides: Dict[tuple, Tuple[set, set]] = defaultdict(lambda: (set(), set()))   # rows it meets the channel's edges on
    for r, (e, cs, ct, ys, _yt, hops) in enumerate(routes):
        for j, (k, a, b) in enumerate(hops):
            key = ("out", k, e["from"], ys) if j == 0 else ("own", k, r)
            groups[key].append((r, min(a, b), max(a, b)))
            left, right = sides[key]
            if cs < ct:
                left.add(a)
                right.add(b)
            elif cs > ct:
                right.add(a)
                left.add(b)
            else:
                left |= {a, b}
    lanes: Dict[int, List[List[Tuple[int, int]]]] = defaultdict(list)
    lane_of: Dict[Tuple[int, int], int] = {}
    lane_groups: Dict[Tuple[int, int], List[tuple]] = defaultdict(list)
    for k, lo, hi, key, members in sorted(((key[1], min(m[1] for m in ms), max(m[2] for m in ms), key, ms)
                                           for key, ms in groups.items()),
                                          key=lambda v: (v[0], v[2] - v[1], v[1], str(v[3]))):
        used = lanes[k]
        for i, spans in enumerate(used):
            if all(hi < a - 1 or lo > b + 1 for a, b in spans):
                spans.append((lo, hi))
                lane = i
                break
        else:
            used.append([(lo, hi)])
            lane = len(used) - 1
        lane_groups[(k, lane)].append(key)
        for r, _lo, _hi in members:
            lane_of[(r, k)] = lane
    # A line meeting a channel's left side on a row runs from there to its lane; one meeting the right side on the
    # same row runs from its lane on. The first lane must lie left of the second, or the two would read as one line:
    # order each channel's lanes so they do, as far as they can.
    for k in list(lanes):
        n = len(lanes[k])
        after: Dict[int, set] = defaultdict(set)
        for i in range(n):
            for j in range(n):
                if i != j and any(sides[g1][0] & sides[g2][1] for g1 in lane_groups[(k, i)]
                                  for g2 in lane_groups[(k, j)]):
                    after[i].add(j)
        into = {j: sum(j in after[i] for i in range(n)) for j in range(n)}
        order, ready = [], sorted(j for j in range(n) if into[j] == 0)
        while ready:
            i = ready.pop(0)
            order.append(i)
            for j in sorted(after[i]):
                into[j] -= 1
                if into[j] == 0:
                    ready.append(j)
            ready.sort()
        order += [i for i in range(n) if i not in order]   # a cycle: the rest as they were
        place = {old: new for new, old in enumerate(order)}
        for key_rk in [rk for rk in lane_of if rk[1] == k]:
            lane_of[key_rk] = place[lane_of[key_rk]]

    # columns' x: each box, then its channel, wide enough for its lanes and the labels of the flows ending there
    label_in = defaultdict(int)
    for e, cs, ct, _ys, _yt, hops in routes:
        if e.get("label") and cs != ct:
            label_in[hops[-1][0]] = max(label_in[hops[-1][0]], len(e["label"]) + 2)
    widths = want
    chan = [max(chan_min, len(lanes[k]) + chan_pad + label_in[k]) for k in range(len(present))]
    if not lanes[len(present) - 1]:
        chan[-1] = 0
    xs, x = [], 0
    for i in range(len(present)):
        xs.append(x)
        x += widths[i] + chan[i]

    def lane_x(k: int, i: int) -> int:
        return xs[k] + widths[k] + 2 + i

    for i, k in enumerate(present):
        g.put(xs[i], 0, cut(heading[k].upper(), widths[i] + chan[i] - 1), "heading")

    def from_style(e: dict) -> str:
        n = node[e["from"]]
        it = next((i for i in n.get("items") or [] if i["id"] == e.get("from_item")), None)
        return box_style(it) if it and (it.get("agent") or it.get("status", "same") != "same") else box_style(n)

    for r, (e, cs, ct, ys, yt, hops) in enumerate(routes):
        st = e.get("status", "same")
        style = st if st != "same" else "muted"
        dash = st == "removed" or e.get("kind") == "network"
        eid = edge_id(e)
        label = e.get("label") or ""
        ends = [eid]
        if cs < ct:
            pts = [(xs[cs] + widths[cs], ys)]
            for k, a, b in hops:
                lx = lane_x(k, lane_of[(r, k)])
                pts += [(lx, a), (lx, b)]
            pts.append((xs[ct] - 2, yt))
            g.path(pts, eid, style, dash)
            g.put(xs[ct] - 1, yt, "▸", style, ends)
            if label and xs[ct] - 3 - len(label) > pts[-2][0]:
                g.put(xs[ct] - 2 - len(label), yt, label, style, ends)
            g.put(xs[cs] + widths[cs] - 1, ys, "├", from_style(e), [eid])
        elif cs > ct:
            pts = [(xs[cs] - 1, ys)]
            for k, a, b in hops:
                lx = lane_x(k, lane_of[(r, k)])
                pts += [(lx, a), (lx, b)]
            pts.append((xs[ct] + widths[ct] + 1, yt))
            g.path(pts, eid, style, dash)
            g.put(xs[ct] + widths[ct], yt, "◂", style, ends)
            if label and xs[ct] + widths[ct] + 2 + len(label) < pts[-2][0]:
                g.put(xs[ct] + widths[ct] + 2, yt, label, style, ends)
            g.put(xs[cs], ys, "┤", from_style(e), [eid])
        else:
            lx = lane_x(cs, lane_of[(r, cs)])
            g.path([(xs[cs] + widths[cs], ys), (lx, ys), (lx, yt), (xs[cs] + widths[cs] + 1, yt)], eid, style, dash)
            g.put(xs[cs] + widths[cs], yt, "◂", style, ends)
            g.put(xs[cs] + widths[cs] - 1, ys, "├", from_style(e), [eid])

    # the boxes, over the lines; then the ports drawn into their sides again
    ports = {xy: c for xy, c in g.cells.items() if c[0] in "├┤▸◂"}
    for n in nodes:
        i, y = where[n["id"]], pos_y[n["id"]]
        bid, w = "b:" + n["id"], widths[where[n["id"]]]
        g.box(xs[i], y, w, heights[n["id"]], box_kind(n), box_style(n), [bid])
        for j, (text, style, rid) in enumerate(contents[n["id"]]):
            g.put(xs[i] + 2, y + 1 + j, cut(text, w - 4), style, [rid])
            if rid != bid:
                g.blocks.append({"id": rid, "x": xs[i] + 1, "y": y + 1 + j, "w": w - 2, "h": 1})
        g.blocks.append({"id": bid, "x": xs[i], "y": y, "w": w, "h": heights[n["id"]] if not n.get("items") else 3})
    joined = {("│", "├"): "├", ("│", "┤"): "┤", ("┃", "├"): "┠", ("┃", "┤"): "┨", ("╎", "├"): "├", ("╎", "┤"): "┤"}
    for xy, c in ports.items():
        under = g.cells.get(xy)
        if c[0] in "▸◂":
            g.cells[xy] = c
        elif under and (under[0], c[0]) in joined:
            g.cells[xy] = [joined[(under[0], c[0])], under[1], under[2] | c[2]]

    # what a picked block lights: its flows (a component its own), and the blocks at their other ends
    holder = {it["id"]: n["id"] for n in nodes for it in n.get("items") or []}
    for n in nodes:
        for bid in ["b:" + n["id"]] + ["b:" + it["id"] for it in n.get("items") or []]:
            me = bid[2:]
            lit = set()
            for e in edges:
                pairs = e.get("pairs") or [[e["from"], e["to"]]]
                for s, t in pairs:
                    if me in (s, t) or (me == n["id"] and (e["from"] == me or e["to"] == me)):
                        lit.add(edge_id(e))
                        for end, box in ((s, e["from"]), (t, e["to"])):
                            if end != me and box != n["id"]:
                                lit.add("b:" + (end if end in holder and holder[end] == box else box))
                        break
            g.lights[bid] = sorted(lit)
    return g


def edge_id(e: dict) -> str:
    return "e:" + e["from"] + ">" + e["to"]


def box_style_of(nodes: List[dict], nid: str) -> str:
    return next((box_style(n) for n in nodes if n["id"] == nid), "muted")


# ── technologies, as chips in rows ────────────────────────────────────────────

def chips(rows: List[Tuple[str, List[dict]]], width: int, label_w: int = 0) -> Grid:
    """Rows of chips under a label each (`rows`: (label, [{id, text, style}])), wrapped at `width`; each chip a
    block."""
    g = Grid()
    y = 0
    narrow = label_w == 0 or width < label_w + 24
    for label, items in rows:
        if not items:
            continue
        if narrow:
            g.put(0, y, label.upper(), "heading")
            y += 1
            x0 = 2
        else:
            g.put(0, y, cut(label, label_w - 2), "heading")
            x0 = label_w
        x = x0
        for it in items:
            text = f" {it['text']} "
            if x + len(text) > width and x > x0:
                y += 1
                x = x0
            g.put(x, y, text, it.get("style", ""), [it["id"]])
            g.blocks.append({"id": it["id"], "x": x, "y": y, "w": len(text), "h": 1})
            x += len(text) + 1
        y += 1
    return g


# ── a page: what the pane draws ───────────────────────────────────────────────

class Page:
    """Rows of runs, the blocks among them, and what each block shows and lights when picked."""

    def __init__(self, width: int) -> None:
        self.width = max(30, width)
        self.rows: List[List[Run]] = []
        self.blocks: List[Dict[str, Any]] = []
        self.lights: Dict[str, List[str]] = {}
        self.details: Dict[str, List[List[Run]]] = {}
        self.pick = ""

    def add(self, *runs: Sequence[str]) -> None:
        self.rows.append([list(r) for r in runs if r and r[0]])

    def blank(self) -> None:
        if self.rows and self.rows[-1]:
            self.rows.append([])

    def heading(self, text: str, aside: str = "") -> None:
        self.blank()
        self.add([text.upper(), "heading"], ["  " + aside, "muted"] if aside else ["", ""])

    def para(self, text: str, style: str = "", indent: int = 0, first: Optional[Sequence[str]] = None) -> None:
        """Wrapped to the page; `first`, a run before the text on its first row (a sign, a label)."""
        lead = len(first[0]) if first else indent
        for i, piece in enumerate(textwrap.wrap(text, max(16, self.width - lead - 1)) or [""]):
            if i == 0 and first:
                self.add(list(first), [piece, style])
            else:
                self.add([" " * lead + piece, style])

    def grid(self, g: Grid, slot: int = 0, on: Optional[Dict[str, dict]] = None) -> None:
        """A picture, its blocks (with what Enter and space do, from `on`), and under it `slot` rows where a picked
        block's detail shows."""
        y0 = len(self.rows)
        self.rows += g.rows()
        slot_at = len(self.rows)
        self.rows += [[] for _ in range(slot)]
        for b in g.blocks:
            self.blocks.append({**b, "y": b["y"] + y0, **((on or {}).get(b["id"], {})),
                                **({"slot": [slot_at, slot]} if slot else {})})
        self.lights.update(g.lights)
        for bid, rows in g.details.items():
            self.details[bid] = rows[:slot] if slot else rows

    def json(self, **extra: Any) -> Dict[str, Any]:
        wide = max([self.width] + [sum(len(r[0]) for r in row) for row in self.rows])
        return {"protocol": 2, "width": self.width, "wide": wide, "rows": self.rows, "blocks": self.blocks,
                "lights": self.lights, "details": self.details, "pick": self.pick, **extra}


def detail_rows(text: str, width: int, style: str = "", lead: Sequence[str] = ("", "")) -> List[List[Run]]:
    """A block's detail as rows: `lead` (a run) before the wrapped text."""
    out = []
    for i, piece in enumerate(textwrap.wrap(text, max(16, width - len(lead[0]) - 1)) or [""]):
        out.append(([list(lead)] if i == 0 and lead[0] else [[" " * len(lead[0]), ""]] if lead[0] else [])
                   + [[piece, style]])
    return out


def edge_word(kind: str) -> str:
    return {"data": "reads and writes", "network": "over the network"}.get(kind, "")


def sign(status: str) -> str:
    return OP.get(status, "")
