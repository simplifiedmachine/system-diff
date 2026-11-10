"""Pictures drawn from data, in one look: white boxes, a column or a stage per role, curves by kind of connection.

    system-diff draw SPEC.json...   # every spec with a "picture" key -> SPEC.svg beside it

Three kinds:

  map       boxes in columns and curves between them. The system map and the system diff are maps; a spec can draw
            one by hand ("where state lives").
  stages    a process left to right, with its loops back, its exits, and where a person comes in.
  sequence  who talks to whom, numbered, in order.

Layouts are deterministic and keyed by id, so a part that did not change never moves between versions.
Standard library only.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

INK, INK2, MUTED, LINE = "#161c24", "#3b4655", "#667385", "#e1e5eb"
SURFACE, SURFACE2 = "#ffffff", "#f8f9fb"
OK, WARN, WARN_BG, ATTN, ATTN_BG, WORK, WORK_BG = "#157347", "#9a5600", "#fff3dd", "#c4321c", "#fdecea", "#1f5fbf", "#e8f0fc"
SANS = "'Segoe UI', FreeSans, 'Helvetica Neue', Arial, sans-serif"
MONO = "Consolas, Inconsolata, 'DejaVu Sans Mono', monospace"

CSS = f"""
text {{ font-family: {SANS}; }}
.bg {{ fill: {SURFACE2}; stroke: {LINE}; }}
.col {{ font-size: 11px; font-weight: 600; letter-spacing: .05em; fill: {MUTED}; }}
.bn .box {{ fill: {SURFACE}; stroke: {LINE}; stroke-width: 1.2; }}
.bn.t-store .box {{ fill: {WORK_BG}; }}
.bn.legacy .box {{ fill: {SURFACE2}; stroke: #c3cad4; stroke-dasharray: 5 4; }}
.bn.legacy .bn-t {{ fill: {INK2}; }}
.bn .band {{ fill: {WORK}; }}
.bn.undeclared .band {{ fill: {WARN}; }}
.bn.s-added .box {{ stroke: {OK}; stroke-width: 1.8; }} .bn.s-added .band {{ fill: {OK}; }}
.bn.s-changed .box {{ stroke: {WARN}; stroke-width: 1.8; }} .bn.s-changed .band {{ fill: {WARN}; }}
.bn.s-removed .box {{ stroke: {ATTN}; stroke-dasharray: 5 4; fill: {ATTN_BG}; }} .bn.s-removed .band {{ fill: {ATTN}; }}
.bn-t {{ font-size: 13px; font-weight: 600; fill: {INK}; }}
.bn-s {{ font-size: 11.5px; fill: {MUTED}; }}
.bn-p {{ font-size: 11.5px; fill: {INK2}; }} .bn-p.code {{ font-family: {MONO}; }} .bn-p.more {{ fill: {MUTED}; }}
.bn-p.s-added {{ fill: {OK}; font-weight: 600; }} .bn-p.s-changed {{ fill: {WARN}; font-weight: 600; }}
.bn-p.s-removed {{ fill: {ATTN}; text-decoration: line-through; }}
.be {{ fill: none; stroke: {MUTED}; stroke-width: 1.4; opacity: .75; }}
.be.k-data {{ stroke: {WORK}; }} .be.k-network {{ stroke: {WARN}; stroke-dasharray: 6 4; }}
.be.s-added {{ stroke: {OK}; stroke-width: 2.2; opacity: 1; }}
.be.s-removed {{ stroke: {ATTN}; stroke-dasharray: 5 4; opacity: 1; }}
.ar {{ fill: {MUTED}; }} .ar.k-data {{ fill: {WORK}; }} .ar.k-network {{ fill: {WARN}; }}
.ar.s-added {{ fill: {OK}; }} .ar.s-removed {{ fill: {ATTN}; }} .ar.k-person {{ fill: {WORK}; }}
.halo {{ paint-order: stroke; stroke: {SURFACE2}; stroke-width: 4px; stroke-linejoin: round; }}
.lg {{ font-size: 11.5px; fill: {MUTED}; }}
.lg.s-added {{ fill: {OK}; font-weight: 600; }} .lg.s-changed {{ fill: {WARN}; font-weight: 600; }}
.lg.s-removed {{ fill: {ATTN}; font-weight: 600; }}
.foot {{ font-size: 11.5px; fill: {INK2}; }}
.st .box {{ fill: {SURFACE}; stroke: {LINE}; stroke-width: 1.4; }}
.st.hi .box {{ fill: {WORK_BG}; stroke: {WORK}; }}
.st-w {{ font-size: 13px; font-weight: 600; fill: {INK}; }}
.fl {{ fill: none; stroke: {MUTED}; stroke-width: 1.4; }} .fl.loop {{ stroke-dasharray: 4 4; }}
.fl.gate {{ stroke: {WORK}; stroke-dasharray: 2 4; opacity: .7; }}
.fl-note {{ font-size: 11.5px; fill: {MUTED}; }}
.gate .box {{ fill: {WORK_BG}; stroke: {WORK}; }} .gate text {{ font-size: 12.5px; font-weight: 600; fill: {WORK}; }}
.exit {{ font-size: 12.5px; font-weight: 600; fill: {INK}; }}
.lane .box {{ fill: {SURFACE}; stroke: {LINE}; stroke-width: 1.2; }}
.lane.person .box {{ fill: {WORK_BG}; stroke: {WORK}; }}
.life {{ stroke: #c9d0da; stroke-width: 1.2; stroke-dasharray: 3 4; }}
.msg {{ fill: none; stroke: {MUTED}; stroke-width: 1.4; }}
.msg.frame {{ stroke: {WORK}; stroke-dasharray: 5 4; }} .msg.person {{ stroke: {WORK}; stroke-width: 2; }}
.msg-t {{ font-size: 11.5px; fill: {INK2}; }} .msg-t.frame {{ fill: {WORK}; }} .msg-t.person {{ fill: {WORK}; font-weight: 600; }}
.num circle {{ fill: {MUTED}; }} .num.person circle {{ fill: {WORK}; }}
.num text {{ fill: #ffffff; font-size: 10px; font-weight: 700; }}
.note .box {{ fill: {WARN_BG}; stroke: #e9c98f; }} .note text {{ font-size: 11.5px; fill: {WARN}; }}
"""

OP = {"added": "+", "changed": "~", "removed": "−", "same": ""}


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def cut(text, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def cut_code(text, n: int) -> str:
    """Like cut, but `METHOD /a/long/path` keeps its method and the end of its path, where routes differ."""
    text = str(text)
    head, _, path = text.partition(" ")
    if len(text) <= n or not path.startswith("/") or " " in path:
        return cut(text, n)
    for i, ch in enumerate(path):
        if ch == "/" and i and len(head) + 2 + len(path) - i <= n:
            return f"{head} …{path[i:]}"
    return cut(text, n)


def text_width(text: str, size: float = 11.5) -> float:
    """Roughly how wide a line of sans text is; only for spacing, never for clipping."""
    return len(text) * size * 0.54


def wrap(text: str, n: int, max_lines: int = 3) -> List[str]:
    """Greedy word wrap to `n` characters; a line breaks before a ` · ` rather than after it."""
    lines: List[str] = []
    line = ""
    for word in str(text).split(" "):
        cand = f"{line} {word}".strip()
        if len(cand) > n and line:
            lines.append(line.rstrip(" ·"))
            line = word if word != "·" else ""
        else:
            line = cand
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [cut(" ".join(lines[max_lines - 1:]), n)]
    return lines


def _svg(width: float, height: float, body: Iterable[str], label: str, defs: Iterable[str] = ()) -> str:
    w, h = int(round(width)), int(round(height))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="{esc(label)}">\n<style>{CSS}</style>\n<defs>{"".join(defs)}</defs>\n'
            f'<rect class="bg" x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10"/>\n'
            + "\n".join(body) + "\n</svg>\n")


def _marker(mid: str, cls: str) -> str:
    return (f'<marker id="{mid}" class="ar {cls}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z"/></marker>')


def _f(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".")


# ── map: boxes in columns ─────────────────────────────────────────────────────
#
# graph = {"columns": [[key, heading], ...],
#          "nodes": [{"id", "col", "title", "sub", "type": component|store|host, "style": new|legacy|shared|undeclared,
#                     "status": same|added|changed|removed, "lines": [{"text", "code", "status"}], "more": n}],
#          "edges": [{"from", "to", "kind": call|data|network, "status"}],
#          "legend": [...] | None, "footer": [...]}
# Nodes are drawn in the order given within their column.

W, GX, PAD, TOP = 200, 64, 14, 30
MAX_LINES = {"component": 4, "store": 3, "host": 0}
LEGEND_H = 30


def _title_lines(title: str) -> List[str]:
    return wrap(title, 24, max_lines=2)


def node_height(n: dict) -> int:
    extra = 16 * (len(_title_lines(n["title"])) - 1)
    if n.get("type") == "host":
        return 40 + extra
    k = min(len(n.get("lines") or []), MAX_LINES.get(n.get("type", "component"), 4))
    more = len(n.get("lines") or []) > k or n.get("more")
    return 46 + k * 15 + (15 if more else 0) + extra


def map_layout(columns: List[List[str]], nodes: List[dict]) -> Tuple[Dict[str, tuple], List[tuple], int, int]:
    """id -> (x, y, height, column index); the headings drawn; width; height. Empty columns take no room."""
    pos: Dict[str, tuple] = {}
    heads: List[tuple] = []
    x, bottom = PAD, 0
    for key, heading in columns:
        col = [n for n in nodes if n["col"] == key]
        if not col:
            continue
        y = TOP
        for n in col:
            h = node_height(n)
            pos[n["id"]] = (x, y, h, len(heads))
            y += h + 14
        heads.append((heading, x))
        bottom = max(bottom, y)
        x += W + GX
    return pos, heads, max(x - GX + PAD, 320), bottom + PAD


def _s(x1: float, y1: float, x2: float, y2: float) -> str:
    m = (x2 - x1) / 2
    return "C{},{} {},{} {},{}".format(*map(_f, (x1 + m, y1, x2 - m, y2, x2, y2)))


def _free_y(boxes: List[tuple], want: float) -> float:
    """Where a connection can cross a column at about `want`: there if no box is in the way, else the nearest gap
    between two boxes, or below the last one."""
    if not any(y - 5 < want < y + h + 5 for y, h in boxes):
        return want
    gaps = [(y1 + h1 + y2) / 2 for (y1, h1), (y2, _h2) in zip(boxes, boxes[1:])]
    gaps.append(boxes[-1][0] + boxes[-1][1] + 10)
    return min(gaps, key=lambda g: (abs(g - want), g))


def _curve(a: tuple, b: tuple, columns: Dict[int, tuple]) -> str:
    """A connection from box a to box b. One that skips a column crosses it through a gap
    between its boxes, so it never seems to leave from a box it only passed behind."""
    ax, ay0, ah, ac = a
    bx, by0, bh, bc = b
    ay, by = ay0 + min(ah / 2, 22), by0 + min(bh / 2, 22)
    if bc == ac:  # same column: an arc on the right
        x = ax + W
        bend = 26 + min(40, abs(by - ay) / 6)
        return "M{},{} C{},{} {},{} {},{}".format(*map(_f, (x, ay, x + bend, ay, x + bend, by, x + 6, by)))
    step = 1 if bc > ac else -1
    x, y = (ax + W, ay) if step > 0 else (ax, ay)
    d = f"M{_f(x)},{_f(y)}"
    for k in range(ac + step, bc, step):
        cx, boxes = columns[k]
        gy = _free_y(boxes, ay + (by - ay) * (k - ac) / (bc - ac))
        near, far = (cx, cx + W) if step > 0 else (cx + W, cx)
        d += f" {_s(x, y, near, gy)} L{_f(far)},{_f(gy)}"
        x, y = far, gy
    end = bx - 6 if step > 0 else bx + W + 6
    return d + f" {_s(x, y, end, by)}"


def _box(n: dict, p: tuple, diff: bool) -> str:
    x, y, h, _ = p
    kind = n.get("type", "component")
    status = n.get("status", "same") if diff else "same"
    cls = ["bn", "t-" + kind, "s-" + status] + ([n["style"]] if n.get("style") else [])
    out = [f'<g class="{" ".join(cls)}" transform="translate({_f(x)},{_f(y)})">',
           f'<rect class="box" width="{W}" height="{h}" rx="{20 if kind == "host" else 8}"/>']
    if kind != "host" and ((diff and status != "same") or (not diff and n.get("style") in ("new", "undeclared"))):
        out.append(f'<rect class="band" width="5" height="{h}" rx="2"/>')
    titles = _title_lines(n["title"])
    for i, t in enumerate(titles):
        mark = f"{OP[status]} " if i == 0 and OP.get(status) else ""
        out.append(f'<text x="12" y="{19 + 16 * i}" class="bn-t">{esc(mark + t)}</text>')
    dy = 16 * (len(titles) - 1)
    if n.get("sub"):
        out.append(f'<text x="12" y="{35 + dy}" class="bn-s">{esc(cut(n["sub"], 32))}</text>')
    lines = n.get("lines") or []
    shown = lines[: MAX_LINES.get(kind, 4)]
    for i, line in enumerate(shown):
        ls = line.get("status", "same") if diff else "same"
        mark = OP.get(ls) or "·"
        code = " code" if line.get("code") else ""
        text = cut_code(line["text"], 29) if code else cut(line["text"], 31)
        out.append(f'<text x="14" y="{53 + dy + i * 15}" class="bn-p s-{ls}{code}">{esc(mark)} {esc(text)}</text>')
    more = len(lines) - len(shown) + int(n.get("more") or 0)
    if more:
        out.append(f'<text x="14" y="{53 + dy + len(shown) * 15}" class="bn-p more">+{more} more</text>')
    out.append(f"<title>{esc(n['title'])}</title></g>")
    return "".join(out)


MAP_LEGEND = [("band", "new", "new"), ("dashed", "", "legacy, still running"),
              ("band", "undeclared", "not named in the config"),
              ("line", "call", "calls"), ("line", "data", "reads and writes data"), ("line", "network", "over the network")]
DIFF_LEGEND = [("op", "added", "added"), ("op", "changed", "changed"), ("op", "removed", "removed"),
               ("line", "call", "calls"), ("line", "data", "reads and writes data"), ("line", "network", "over the network")]


def _legend(items: List[tuple], x: float, y: float) -> List[str]:
    out = []
    for kind, key, text in items:
        if kind == "band":
            fill = WARN if key == "undeclared" else WORK
            out.append(f'<rect x="{_f(x)}" y="{_f(y - 10)}" width="14" height="12" rx="2" fill="{SURFACE}" stroke="{LINE}"/>'
                       f'<rect x="{_f(x)}" y="{_f(y - 10)}" width="4" height="12" rx="1" fill="{fill}"/>')
            x += 20
        elif kind == "dashed":
            out.append(f'<rect x="{_f(x)}" y="{_f(y - 10)}" width="14" height="12" rx="2" fill="{SURFACE2}" '
                       f'stroke="#c3cad4" stroke-dasharray="3 2"/>')
            x += 20
        elif kind == "line":
            out.append(f'<path class="be k-{key}" d="M{_f(x)},{_f(y - 4)} L{_f(x + 20)},{_f(y - 4)}" style="opacity:1"/>')
            x += 26
        elif kind == "op":
            text = f"{OP[key]} {text}"
        cls = f"lg s-{key}" if kind == "op" else "lg"
        out.append(f'<text x="{_f(x)}" y="{_f(y)}" class="{cls}">{esc(text)}</text>')
        x += text_width(text) + 18
    return out


def map_svg(graph: dict, diff: bool = False, label: str = "System map") -> str:
    nodes = graph["nodes"]
    pos, heads, width, height = map_layout(graph["columns"], nodes)
    ids = set(pos)
    edges = [e for e in graph.get("edges", []) if e["from"] in ids and e["to"] in ids and e["from"] != e["to"]]
    legend = graph.get("legend")
    if legend is None:
        legend = DIFF_LEGEND if diff else [i for i in MAP_LEGEND if i[1] != "undeclared"
                                           or any(n.get("style") == "undeclared" for n in nodes)]
    footer = graph.get("footer") or []
    columns: Dict[int, tuple] = {}  # column index -> (x, [(y, height) of each box, top to bottom])
    for x, y, h, c in sorted(pos.values(), key=lambda v: (v[3], v[1])):
        columns.setdefault(c, (x, []))[1].append((y, h))
    body = [f'<text class="col" x="{_f(x)}" y="18">{esc(h.upper())}</text>' for h, x in heads]
    markers = {}
    for e in edges:
        status = e.get("status", "same") if diff else "same"
        kind = e.get("kind", "call")
        mid = f"ar-{kind}-{status}"
        markers.setdefault(mid, _marker(mid, f"k-{kind} s-{status}"))
        body.append(f'<path class="be k-{kind} s-{status}" d="{_curve(pos[e["from"]], pos[e["to"]], columns)}" '
                    f'marker-end="url(#{mid})"><title>{esc(e["from"])} → {esc(e["to"])}</title></path>')
    body += [_box(n, pos[n["id"]], diff) for n in nodes if n["id"] in pos]
    y = height + 6
    if legend:
        body += _legend(legend, PAD + 2, y + 10)
        y += LEGEND_H - 6
    for line in footer:
        body.append(f'<text class="foot" x="{PAD + 2}" y="{_f(y + 12)}">{esc(line)}</text>')
        y += 18
    width = max(width, PAD * 2 + max([0.0] + [sum(text_width(t[2]) + 44 for t in legend)]),
                PAD * 2 + max([0.0] + [text_width(t) for t in footer]))
    return _svg(width, y + (8 if legend or footer else 0), body, label, markers.values())


# ── architecture: the system diagram canvas.layered draws, for a web page ─────
#
# model = {"columns": [[key, heading]], "nodes": [{"id", "col", "title", "sub", "status",
#          "type": actor|component|group|store|host, "agent": bool, "owner": 0..4, "focus": bool,
#          "items": [{"id", "title", "status", "agent", "owner", "hub"}], "lines": [{"text", "status"}]}],
#          "edges": [{"from", "to", "kind": call|data|network, "status", "label", "from_item", "to_item"}]}
# Every box carries data-id "b:<id>", every row in a container "b:<item id>", every flow "e:<from>><to>", the ids
# canvas.layered gives them, so a page lights the same things the pane does by toggling .lit, .picked and .dim.

AW, AGX, APAD, ATOP = 220, 112, 20, 42
AROW, AITEM = 16, 26
OWNERS = (("#0b7f8a", "#4fd3dc"), ("#9b3fc2", "#d482ff"), ("#1d8a3c", "#5fd283"), ("#9a5600", "#f0b44c"),
          ("#1f5fbf", "#5ea4ff"))
_ARCH_LIGHT = {"ink": INK, "ink2": INK2, "muted": MUTED, "line": "#aab4c2", "surface": SURFACE, "surface2": SURFACE2,
               "ok": OK, "warn": WARN, "attn": ATTN, "attn-bg": ATTN_BG, "work": WORK, "work-bg": WORK_BG,
               "item": "#f3f5f8", "hi": "#2f7bff", "hi-bg": "#e4eeff",
               **{f"o{i}": c[0] for i, c in enumerate(OWNERS)}}
_ARCH_DARK = {"ink": "#e6eaf0", "ink2": "#c2cad6", "muted": "#a7b2c1", "line": "#5d6878", "surface": "#1b2028",
              "surface2": "#13171d", "ok": "#5fd283", "warn": "#f0b44c", "attn": "#ff6f6f", "attn-bg": "#3a1d1d",
              "work": "#5ea4ff", "work-bg": "#1a2a40", "item": "#232a34", "hi": "#6cb2ff", "hi-bg": "#173356",
              **{f"o{i}": c[1] for i, c in enumerate(OWNERS)}}


def _vars(d: Dict[str, str]) -> str:
    return " ".join(f"--{k}: {v};" for k, v in d.items())


ARCH_CSS = f"""
svg.sd-arch {{ {_vars(_ARCH_LIGHT)} }}
@media (prefers-color-scheme: dark) {{
  svg.sd-arch:root, :root:not([data-theme=light]) svg.sd-arch {{ {_vars(_ARCH_DARK)} }} }}
:root[data-theme=dark] svg.sd-arch {{ {_vars(_ARCH_DARK)} }}
.sd-arch text {{ font-family: {SANS}; }}
.sd-arch .bg {{ fill: var(--surface2); }}
.sd-arch .col {{ font-size: 11px; font-weight: 600; letter-spacing: .06em; fill: var(--muted); }}
.sd-arch .an .box {{ fill: var(--surface); stroke: var(--line); stroke-width: 1.5; }}
.sd-arch .an.t-actor .box {{ fill: var(--work-bg); stroke: var(--work); stroke-opacity: .8; }}
.sd-arch .an.t-store .box, .sd-arch .an.t-store .lip {{ fill: var(--work-bg); }}
.sd-arch .an .lip {{ stroke: var(--line); stroke-width: 1.3; }}
.sd-arch .an.t-host .box {{ fill: var(--surface2); stroke-dasharray: 5 4; stroke: var(--muted); stroke-opacity: .9; }}
.sd-arch .an.t-group .box {{ fill: var(--surface2); stroke-width: 1.5; }}
.sd-arch .an .band {{ fill: transparent; }}
.sd-arch .an.s-added .box, .sd-arch .an.s-added .lip {{ stroke: var(--ok); stroke-width: 1.9; stroke-opacity: 1; }}
.sd-arch .an.s-changed .box, .sd-arch .an.s-changed .lip {{ stroke: var(--warn); stroke-width: 1.9; stroke-opacity: 1; }}
.sd-arch .an.s-removed .box {{ stroke: var(--attn); stroke-dasharray: 5 4; fill: var(--attn-bg); stroke-opacity: 1; }}
.sd-arch .an.s-added .band {{ fill: var(--ok); }} .sd-arch .an.s-changed .band {{ fill: var(--warn); }}
.sd-arch .an.s-removed .band {{ fill: var(--attn); }}
{chr(10).join(f'.sd-arch .o{i} {{ --own: var(--o{i}); }}' for i in range(len(OWNERS)))}
.sd-arch .an.owned > .box, .sd-arch .an.owned > .lip {{ stroke: var(--own); stroke-width: 2; stroke-opacity: 1; }}
.sd-arch .an.owned > .band {{ fill: var(--own); }}
.sd-arch .an.agent > .box, .sd-arch .an.agent > .lip {{ stroke: var(--work); stroke-width: 2.4; stroke-opacity: 1;
  filter: drop-shadow(0 0 5px var(--work)); }}
.sd-arch .an.focus > .box {{ stroke: var(--work); stroke-width: 2.4; }}
.sd-arch .an-t {{ font-size: 13.5px; font-weight: 600; fill: var(--ink); }}
.sd-arch .t-group .an-t {{ font-size: 11.5px; letter-spacing: .07em; }}
.sd-arch .an-s {{ font-size: 11.5px; fill: var(--muted); }}
.sd-arch .an-p {{ font-size: 11.5px; fill: var(--ink2); }}
.sd-arch .an.agent .an-t, .sd-arch .ai.agent text {{ fill: var(--work); }}
.sd-arch .an.owned .an-t, .sd-arch .ai.owned text {{ fill: var(--own); }}
.sd-arch .s-added > .an-t, .sd-arch .an-p.s-added, .sd-arch .ai.s-added text {{ fill: var(--ok); }}
.sd-arch .s-changed > .an-t, .sd-arch .an-p.s-changed, .sd-arch .ai.s-changed text {{ fill: var(--warn); }}
.sd-arch .s-removed > .an-t, .sd-arch .an-p.s-removed, .sd-arch .ai.s-removed text {{ fill: var(--attn); }}
.sd-arch .ai.s-removed text {{ text-decoration: line-through; }}
.sd-arch .ai rect {{ fill: var(--surface); stroke: var(--line); stroke-width: 1; }}
.sd-arch .ai text {{ font-size: 12.5px; fill: var(--ink); }}
.sd-arch .ai.hub text {{ fill: var(--muted); }}
.sd-arch .ai.s-added rect {{ stroke: var(--ok); }} .sd-arch .ai.s-changed rect {{ stroke: var(--warn); }}
.sd-arch .ai.s-removed rect {{ stroke: var(--attn); stroke-dasharray: 4 3; }}
.sd-arch .ai.owned rect {{ stroke: var(--own); stroke-width: 1.6; }}
.sd-arch .ai.agent rect {{ stroke: var(--work); stroke-width: 1.8; fill: var(--work-bg); }}
.sd-arch .ae path {{ fill: none; stroke: var(--muted); stroke-width: 1.6; opacity: 1; }}
.sd-arch .ae.k-data path {{ stroke: var(--work); }}
.sd-arch .ae.k-network path {{ stroke: var(--warn); stroke-dasharray: 6 4; }}
.sd-arch .ae.s-added path {{ stroke: var(--ok); stroke-width: 2.3; opacity: 1; }}
.sd-arch .ae.s-changed path {{ stroke: var(--warn); stroke-width: 2; opacity: 1; }}
.sd-arch .ae.s-removed path {{ stroke: var(--attn); stroke-dasharray: 5 4; opacity: 1; }}
.sd-arch .ae text {{ font-size: 10.5px; font-weight: 600; letter-spacing: .03em; fill: var(--ink2); }}
.sd-arch .ae .tag {{ fill: var(--surface); stroke: var(--line); stroke-width: 1; }}
.sd-arch .ae.s-added .tag {{ stroke: var(--ok); }} .sd-arch .ae.s-removed .tag {{ stroke: var(--attn); }}
.sd-arch .ae.lit .tag, .sd-arch .ae.picked .tag {{ stroke: var(--work); }}
.sd-arch .ae.s-added text {{ fill: var(--ok); }} .sd-arch .ae.s-removed text {{ fill: var(--attn); }}
.sd-arch .arh {{ fill: var(--muted); }} .sd-arch .arh.k-data {{ fill: var(--work); }}
.sd-arch .arh.k-network, .sd-arch .arh.s-changed {{ fill: var(--warn); }}
.sd-arch .arh.s-added {{ fill: var(--ok); }} .sd-arch .arh.s-removed {{ fill: var(--attn); }}
.sd-arch .lg {{ font-size: 11.5px; fill: var(--muted); }}
.sd-arch .lg.s-added {{ fill: var(--ok); font-weight: 600; }} .sd-arch .lg.s-changed {{ fill: var(--warn); font-weight: 600; }}
.sd-arch .lg.s-removed {{ fill: var(--attn); font-weight: 600; }} .sd-arch .lg.agent {{ fill: var(--work); font-weight: 600; }}
.sd-arch [data-id] {{ transition: opacity .15s; }}
.sd-arch .dim {{ opacity: .45; }}
.sd-arch .an.lit > .box, .sd-arch .an.lit > .lip {{ stroke-width: 2.6; stroke-opacity: 1; }}
.sd-arch .ai.lit rect {{ stroke: var(--hi); stroke-width: 2.2; stroke-opacity: 1; fill: var(--hi-bg); }}
.sd-arch .an.lit > .box, .sd-arch .an.lit > .lip {{ stroke: var(--hi); stroke-width: 2.6; stroke-opacity: 1; }}
.sd-arch .ae.lit path {{ stroke: var(--hi); stroke-width: 2.6; opacity: 1; }}
.sd-arch .ae.lit path {{ stroke-width: 2.8; opacity: 1; }}
.sd-arch .lit {{ opacity: 1; }}
.sd-arch .an.picked > .box, .sd-arch .ai.picked rect {{ stroke: var(--hi); stroke-width: 3.2; stroke-opacity: 1;
  fill: var(--hi-bg); filter: drop-shadow(0 0 4px var(--hi)); }}
.sd-arch .ai.picked text, .sd-arch .an.picked text {{ fill: var(--hi); font-weight: 650; }}
.sd-arch .ae.picked path {{ stroke: var(--hi); stroke-width: 3.2; opacity: 1; }}
"""


def _arch_title_lines(n: dict) -> List[str]:
    title = n["title"].upper() if n.get("type") == "group" else n["title"]
    return wrap(title, 26, max_lines=2)


def arch_height(n: dict) -> int:
    kind = n.get("type", "component")
    extra = AROW * (len(_arch_title_lines(n)) - 1) + (8 if kind == "store" else 0)
    if kind == "group":
        return 50 + extra + AITEM * len(n.get("items") or []) + 6
    rows = (AROW if n.get("sub") else 0) + 15 * min(2, len(n.get("lines") or []))
    rows += 15 if len(n.get("lines") or []) > 2 else 0
    return max(52 if kind == "actor" else 48, 34 + extra + rows)


def _arch_head(n: dict, top: int) -> List[str]:
    """Title (with its change mark and ● where Claude works), subtitle and up to two lines, from `top`."""
    kind = n.get("type", "component")
    status = n.get("status", "same")
    x = 22 if kind == "actor" else 14
    out = []
    titles = _arch_title_lines(n)
    lead = ("● " if n.get("agent") and kind != "group" else "") + (f"{OP[status]} " if OP.get(status) else "")
    for i, t in enumerate(titles):
        out.append(f'<text x="{x}" y="{top + 21 + AROW * i}" class="an-t">{esc((lead if i == 0 else "") + t)}</text>')
    y = top + 21 + AROW * (len(titles) - 1)
    if n.get("sub"):
        y += AROW
        out.append(f'<text x="{x}" y="{y}" class="an-s">{esc(cut(n["sub"], 32))}</text>')
    lines = n.get("lines") or []
    for line in lines[:2]:
        y += 15
        ls = line.get("status", "same")
        out.append(f'<text x="{x + 2}" y="{y}" class="an-p s-{ls}">{esc((OP.get(ls) or "·") + " " + cut(line["text"], 30))}'
                   f'</text>')
    if len(lines) > 2:
        y += 15
        out.append(f'<text x="{x + 2}" y="{y}" class="an-p">+{len(lines) - 2} more</text>')
    return out


def _owner_cls(n: dict) -> List[str]:
    own = n.get("owner")
    return ["owned", f"o{own % len(OWNERS)}"] if isinstance(own, int) and not isinstance(own, bool) else []


def _arch_box(n: dict, p: tuple) -> Tuple[str, Dict[str, float]]:
    """The box, and the y (in the picture) of the middle of each row a container holds."""
    x, y, h, _ = p
    kind = n.get("type", "component")
    status = n.get("status", "same")
    cls = ["an", "t-" + kind, "s-" + status] + _owner_cls(n) + (["agent"] if n.get("agent") else []) \
        + (["focus"] if n.get("focus") else [])
    out = [f'<g class="{" ".join(cls)}" data-id="b:{esc(n["id"])}" transform="translate({_f(x)},{_f(y)})">'
           f'<title>{esc(n["title"])}{(" — " + esc(n["sub"])) if n.get("sub") else ""}</title>']
    rows: Dict[str, float] = {}
    top = 0
    if kind == "store":
        e = 8
        out.append(f'<path class="box" d="M0,{e} A{AW / 2},{e} 0 0 1 {AW},{e} L{AW},{h - e} '
                   f'A{AW / 2},{e} 0 0 1 0,{h - e} Z"/>')
        out.append(f'<path class="lip" d="M0,{e} A{AW / 2},{e} 0 0 0 {AW},{e}"/>')
        top = e
    else:
        rx = h / 2 if kind == "actor" else 14 if kind == "host" else 12 if kind == "group" else 8
        out.append(f'<rect class="box" width="{AW}" height="{h}" rx="{_f(min(rx, 26))}"/>')
        if kind not in ("actor", "host"):
            out.append(f'<rect class="band" x="0" y="6" width="4" height="{h - 12}" rx="2"/>')
    out += _arch_head(n, top)
    if kind == "group":
        ry = 50 + AROW * (len(_arch_title_lines(n)) - 1)
        for it in n.get("items") or []:
            st = it.get("status", "same")
            icls = ["ai", "s-" + st] + _owner_cls(it) + (["agent"] if it.get("agent") else []) \
                + (["hub"] if it.get("hub") else [])
            text = ("● " if it.get("agent") else "") + (f"{OP[st]} " if OP.get(st) else "") + cut(it["title"], 28)
            out.append(f'<g class="{" ".join(icls)}" data-id="b:{esc(it["id"])}">'
                       f'<rect x="10" y="{ry}" width="{AW - 20}" height="{AITEM - 4}" rx="6"/>'
                       f'<text x="20" y="{ry + 15}">{esc(text)}</text><title>{esc(it["title"])}</title></g>')
            rows[it["id"]] = y + ry + (AITEM - 4) / 2
            ry += AITEM
    out.append("</g>")
    return "".join(out), rows


def _route(a: tuple, b: tuple, ay: float, by: float, columns: Dict[int, tuple]) -> Tuple[str, float, float, str]:
    """A flow from box a at height ay to box b at by, as _curve draws one, and where its label goes."""
    ax, _ay0, _ah, ac = a
    bx, _by0, _bh, bc = b
    if bc == ac:  # same column: an arc on the right
        x = ax + AW
        bend = 30 + min(44, abs(by - ay) / 5)
        d = "M{},{} C{},{} {},{} {},{}".format(*map(_f, (x, ay, x + bend, ay, x + bend, by, x + 7, by)))
        return d, x + bend * 0.75 + 6, (ay + by) / 2 + 4, "start"
    step = 1 if bc > ac else -1
    x, y = (ax + AW, ay) if step > 0 else (ax, ay)
    d = f"M{_f(x)},{_f(y)}"
    for k in range(ac + step, bc, step):
        cx, boxes = columns[k]
        gy = _free_y(boxes, ay + (by - ay) * (k - ac) / (bc - ac))
        near, far = (cx, cx + AW) if step > 0 else (cx + AW, cx)
        d += f" {_s(x, y, near, gy)} L{_f(far)},{_f(gy)}"
        x, y = far, gy
    end = bx - 7 if step > 0 else bx + AW + 7
    return d + f" {_s(x, y, end, by)}", (x + end) / 2, (y + by) / 2 - 8, "middle"


ARCH_LEGEND = [("op", "added", "added"), ("op", "changed", "changed"), ("op", "removed", "removed"),
               ("agent", "agent", "● where Claude works"), ("line", "call", "calls"),
               ("line", "data", "reads and writes data"), ("line", "network", "over the network")]


def architecture_svg(model: dict, label: str = "System") -> str:
    """The system diagram canvas.layered draws in the terminal, as an SVG in the map's look: columns of actors (pills),
    components (boxes), containers (their components as rows), stores (cylinders) and outside systems (dashed), joined
    by curved, labelled flows. Scales to its page: a viewBox and no fixed size."""
    present = [(k, h) for k, h in model.get("columns", []) if any(n.get("col") == k for n in model.get("nodes", []))]
    nodes = [n for n in model.get("nodes", []) if n.get("col") in {k for k, _h in present}]
    pos: Dict[str, tuple] = {}
    heads, x, bottom = [], APAD, ATOP
    for key, heading in present:
        y = ATOP
        for n in (n for n in nodes if n["col"] == key):
            h = arch_height(n)
            pos[n["id"]] = (x, y, h, len(heads))
            y += h + 18
        heads.append((heading, x))
        bottom = max(bottom, y)
        x += AW + AGX
    width = max(x - AGX + APAD, 360)
    columns: Dict[int, tuple] = {}
    for bx, by, bh, c in sorted(pos.values(), key=lambda v: (v[3], v[1])):
        columns.setdefault(c, (bx, []))[1].append((by, bh))

    body = [f'<text class="col" x="{_f(hx)}" y="24">{esc(str(h).upper())}</text>' for h, hx in heads]
    rows: Dict[str, Dict[str, float]] = {}
    for n in nodes:
        svg, r = _arch_box(n, pos[n["id"]])
        body.append(svg)
        rows[n["id"]] = r
    markers: Dict[str, str] = {}
    for e in model.get("edges", []):
        s, t = e.get("from"), e.get("to")
        if s not in pos or t not in pos or s == t:
            continue
        kind, status = e.get("kind", "call"), e.get("status", "same")
        a, b = pos[s], pos[t]
        ay = rows[s].get(e.get("from_item") or "", a[1] + min(a[2] / 2, 26))
        by = rows[t].get(e.get("to_item") or "", b[1] + min(b[2] / 2, 26))
        d, lx, ly, anchor = _route(a, b, ay, by, columns)
        mid = f"sda-{kind}-{status}"
        markers.setdefault(mid, f'<marker id="{esc(mid)}" class="arh k-{esc(kind)} s-{esc(status)}" viewBox="0 0 10 10" '
                                f'refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                                f'<path d="M0,0 L10,5 L0,10 z"/></marker>')
        text = ""
        if e.get("label"):
            word = cut(e["label"], 18)
            tw = text_width(word, 10.5) + 12
            rx0 = lx - tw / 2 if anchor == "middle" else lx - 6
            text = (f'<rect class="tag" x="{_f(rx0)}" y="{_f(ly - 11)}" width="{_f(tw)}" height="15" rx="7.5"/>'
                    f'<text x="{_f(rx0 + tw / 2)}" y="{_f(ly)}" text-anchor="middle">{esc(word)}</text>')
        dash = ' stroke-dasharray="5 4"' if status == "removed" or kind == "network" else ""
        body.append(f'<g class="ae k-{esc(kind)} s-{esc(status)}" data-id="e:{esc(s)}&gt;{esc(t)}">'
                    f'<path d="{d}"{dash} marker-end="url(#{esc(mid)})"/>{text}'
                    f'<title>{esc(s)} → {esc(t)}{(" · " + esc(e["label"])) if e.get("label") else ""}</title></g>')

    # legend: only what the picture shows
    statuses = {n.get("status", "same") for n in nodes} | {i.get("status", "same") for n in nodes
                                                          for i in n.get("items") or []} \
        | {e.get("status", "same") for e in model.get("edges", [])}
    kinds_ = {e.get("kind", "call") for e in model.get("edges", []) if e.get("from") in pos and e.get("to") in pos}
    agent = any(n.get("agent") for n in nodes) or any(i.get("agent") for n in nodes for i in n.get("items") or [])
    lx, ly = APAD + 2.0, bottom + 14
    for kind, key, text in ARCH_LEGEND:
        if (kind == "op" and key not in statuses) or (kind == "agent" and not agent) or \
                (kind == "line" and key not in kinds_):
            continue
        if kind == "line":
            body.append(f'<g class="ae k-{key}"><path d="M{_f(lx)},{_f(ly - 4)} L{_f(lx + 22)},{_f(ly - 4)}"/></g>')
            lx += 28
        label_text = f"{OP[key]} {text}" if kind == "op" else text
        cls = f"lg s-{key}" if kind == "op" else "lg agent" if kind == "agent" else "lg"
        body.append(f'<text class="{cls}" x="{_f(lx)}" y="{_f(ly)}">{esc(label_text)}</text>')
        lx += text_width(label_text, 12.5) + 20
    width = max(width, lx + APAD)
    height = ly + 14
    w, h = int(round(width)), int(round(height))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" class="sd-arch" viewBox="0 0 {w} {h}" '
            f'preserveAspectRatio="xMidYMin meet" role="img" aria-label="{esc(label)}">\n'
            f'<style>{ARCH_CSS}</style>\n<defs>{"".join(markers.values())}</defs>\n'
            f'<rect class="bg" x="0" y="0" width="{w}" height="{h}" rx="12"/>\n'
            + "\n".join(body) + "\n</svg>\n")


# ── stages: a process, left to right ──────────────────────────────────────────
#
# spec = {"stages": [{"title", "lines": [text | {"code": text}], "hi": bool}], "width": 170,
#         "loops": [{"from": i, "to": j, "label"}], "exits": [{"label", "note"}],
#         "gate": {"text", "from": [i, ...]}}

def _line_items(lines: Iterable, n: int) -> List[Tuple[str, bool]]:
    out: List[Tuple[str, bool]] = []
    for line in lines:
        code = isinstance(line, dict)
        text = line["code"] if code else str(line)
        out += [(t, code) for t in wrap(text, n - (2 if code else 0), max_lines=2)]
    return out


def stages_svg(spec: dict, label: str = "Stages") -> str:
    stages = spec["stages"]
    sw, sg, x0 = int(spec.get("width", 170)), 36, 14
    per_line = int((sw - 22) / 5.7)
    items = [_line_items(s.get("lines", []), per_line) for s in stages]
    titles = [wrap(s["title"], int((sw - 22) / 6.8), max_lines=2) for s in stages]
    sh = max(64, max(30 + 16 * (len(t) - 1) + 16 * len(i) + 12 for t, i in zip(titles, items)))
    loops = sorted(spec.get("loops", []), key=lambda lp: abs(lp["from"] - lp["to"]))
    heights = [26 + 30 * i for i in range(len(loops))]
    y0 = 24 + (int(0.75 * heights[-1]) + 18 if loops else 0)
    sx = lambda i: x0 + i * (sw + sg)  # noqa: E731
    exits = spec.get("exits", [])
    exit_w = max([0.0] + [max(text_width(e["label"], 12.5), text_width(e.get("note", ""))) for e in exits])
    exit_w = exit_w + 52 if exits else 0
    sh = max(sh, 30 + 34 * len(exits))
    gate = spec.get("gate")
    width = x0 * 2 + len(stages) * sw + (len(stages) - 1) * sg + exit_w
    height = y0 + sh + (84 if gate else 18)
    body: List[str] = []
    for (lp, ht) in zip(loops, heights):
        a, b = sx(lp["from"]) + sw / 2, sx(lp["to"]) + sw / 2
        body.append(f'<path class="fl loop" d="M{_f(a)},{y0} C{_f(a)},{_f(y0 - ht)} {_f(b)},{_f(y0 - ht)} {_f(b)},{y0}" '
                    f'marker-end="url(#ar-stage)"/>')
        body.append(f'<text class="fl-note halo" x="{_f((a + b) / 2)}" y="{_f(y0 - ht * 0.75 - 5)}" '
                    f'text-anchor="middle">{esc(lp["label"])}</text>')
    for i, (s, t, its) in enumerate(zip(stages, titles, items)):
        x = sx(i)
        out = [f'<g class="st{" hi" if s.get("hi") else ""}" transform="translate({_f(x)},{y0})">',
               f'<rect class="box" width="{sw}" height="{sh}" rx="10"/>']
        out += [f'<text x="12" y="{22 + 16 * k}" class="st-w">{esc(line)}</text>' for k, line in enumerate(t)]
        base = 42 + 16 * (len(t) - 1)
        for k, (text, code) in enumerate(its):
            cls = "bn-p code" if code else "fl-note"
            out.append(f'<text x="12" y="{base + 16 * k}" class="{cls}">{esc(text)}</text>')
        out.append("</g>")
        body.append("".join(out))
        if i < len(stages) - 1:
            body.append(f'<path class="fl" d="M{_f(x + sw)},{_f(y0 + sh / 2)} L{_f(x + sw + sg - 4)},{_f(y0 + sh / 2)}" '
                        f'marker-end="url(#ar-stage)"/>')
    ex = sx(len(stages) - 1) + sw
    for k, e in enumerate(exits):
        y = y0 + 22 + 34 * k
        body.append(f'<path class="fl" d="M{_f(ex)},{_f(y)} L{_f(ex + 26)},{_f(y)}" marker-end="url(#ar-stage)"/>')
        body.append(f'<text class="exit" x="{_f(ex + 32)}" y="{_f(y + 4)}">{esc(e["label"])}</text>')
        if e.get("note"):
            body.append(f'<text class="fl-note" x="{_f(ex + 32)}" y="{_f(y + 18)}">{esc(e["note"])}</text>')
    if gate:
        gy = y0 + sh + 46
        for i in gate.get("from", range(len(stages))):
            cx = sx(i) + sw / 2
            body.append(f'<path class="fl gate" d="M{_f(cx)},{y0 + sh} L{_f(cx)},{gy}" marker-end="url(#ar-gate)"/>')
        gw = ex - sx(0)
        body.append(f'<g class="gate" transform="translate({_f(sx(0))},{gy})"><rect class="box" width="{_f(gw)}" '
                    f'height="34" rx="17"/><text x="18" y="22">{esc(gate["text"])}</text></g>')
    defs = [_marker("ar-stage", ""), _marker("ar-gate", "k-person")]
    return _svg(width, height, body, label, defs)


# ── sequence: who talks to whom, in order ─────────────────────────────────────
#
# spec = {"lanes": [{"id", "title", "sub", "person": bool}], "lane": 156,
#         "steps": [{"from", "to", "text", "kind": call|frame} | {"note", "over": [lane, lane]}]}
# A step from a person's lane is drawn as the person's (blue); a frame is what changes the right pane.

def sequence_svg(spec: dict, label: str = "Sequence") -> str:
    lanes = spec["lanes"]
    step_w = int(spec.get("lane", 156))
    x0, top = 16, 14
    cx = {ln["id"]: x0 + step_w / 2 + i * step_w for i, ln in enumerate(lanes)}
    person = {ln["id"] for ln in lanes if ln.get("person")}
    width = x0 * 2 + step_w * len(lanes)
    body: List[str] = []
    for ln in lanes:
        x = cx[ln["id"]] - (step_w - 16) / 2
        cls = "lane person" if ln["id"] in person else "lane"
        out = [f'<g class="{cls}" transform="translate({_f(x)},{top})"><rect class="box" width="{step_w - 16}" '
               f'height="44" rx="{20 if ln["id"] in person else 8}"/>',
               f'<text x="{_f((step_w - 16) / 2)}" y="{19 if ln.get("sub") else 27}" text-anchor="middle" '
               f'class="bn-t">{esc(cut(ln["title"], 20))}</text>']
        if ln.get("sub"):
            out.append(f'<text x="{_f((step_w - 16) / 2)}" y="35" text-anchor="middle" class="bn-s">'
                       f'{esc(cut(ln["sub"], 24))}</text>')
        out.append("</g>")
        body.append("".join(out))
    y, n, rows = top + 44 + 34, 0, []
    for s in spec["steps"]:
        if "note" in s:
            xs = [cx[k] for k in s.get("over", [lanes[0]["id"], lanes[-1]["id"]])]
            x1, x2 = min(xs) - (step_w - 16) / 2, max(xs) + (step_w - 16) / 2
            rows.append(f'<g class="note"><rect class="box" x="{_f(x1)}" y="{_f(y - 14)}" width="{_f(x2 - x1)}" height="24" '
                        f'rx="6"/><text x="{_f((x1 + x2) / 2)}" y="{_f(y + 2)}" text-anchor="middle">{esc(s["note"])}</text></g>')
            y += 36
            continue
        n += 1
        a, b = cx[s["from"]], cx[s["to"]]
        kind = "person" if s["from"] in person else s.get("kind", "call")
        span = abs(b - a) if a != b else step_w * 1.6
        texts = wrap(s["text"], int(max(span - 24, 320) / 5.9), max_lines=2)
        y += 14 * (len(texts) - 1)
        if a == b:
            rows.append(f'<path class="msg {kind}" d="M{_f(a)},{_f(y)} L{_f(a + 30)},{_f(y)} L{_f(a + 30)},{_f(y + 14)} '
                        f'L{_f(a + 7)},{_f(y + 14)}" marker-end="url(#ar-msg-{kind})"/>')
            ty = y + 4 - 14 * (len(texts) - 1)
            rows += [f'<text class="msg-t halo {kind}" x="{_f(a + 38)}" y="{_f(ty + 14 * k)}">{esc(t)}</text>'
                     for k, t in enumerate(texts)]
            y += 14
        else:
            direction = 1 if b > a else -1
            rows.append(f'<path class="msg {kind}" d="M{_f(a + 10 * direction)},{_f(y)} L{_f(b - 7 * direction)},{_f(y)}" '
                        f'marker-end="url(#ar-msg-{kind})"/>')
            ty = y - 7 - 14 * (len(texts) - 1)
            if max(text_width(t) for t in texts) < abs(b - a) - 30:
                x, anchor = (a + b) / 2 + 5 * direction, "middle"
            else:  # wider than its arrow: from the sender's end, clear of the step's number
                x, anchor = a + 14 * direction, "start" if direction > 0 else "end"
            rows += [f'<text class="msg-t halo {kind}" x="{_f(x)}" y="{_f(ty + 14 * k)}" text-anchor="{anchor}">{esc(t)}</text>'
                     for k, t in enumerate(texts)]
        rows.append(f'<g class="num{" person" if kind == "person" else ""}"><circle cx="{_f(a)}" cy="{_f(y)}" r="8"/>'
                    f'<text x="{_f(a)}" y="{_f(y + 3.5)}" text-anchor="middle">{n}</text></g>')
        y += 32
    bottom = y - 6
    for ln in lanes:
        body.insert(0, f'<path class="life" d="M{_f(cx[ln["id"]])},{top + 44} L{_f(cx[ln["id"]])},{_f(bottom)}"/>')
    body += rows
    defs = [_marker(f"ar-msg-{k}", cls) for k, cls in (("call", ""), ("frame", "k-data"), ("person", "k-person"))]
    return _svg(width, bottom + 14, body, label, defs)


# ── specs ─────────────────────────────────────────────────────────────────────

def _map_from_spec(spec: dict) -> dict:
    nodes = []
    for n in spec["nodes"]:
        lines = [{"text": ln["code"], "code": True} if isinstance(ln, dict) else {"text": str(ln)}
                 for ln in n.get("lines", [])]
        nodes.append({**n, "type": n.get("type", "component"), "lines": lines})
    return {"columns": spec["columns"], "nodes": nodes, "edges": spec.get("edges", []),
            "legend": spec.get("legend", []), "footer": spec.get("footer", [])}


def check(spec: dict) -> List[str]:
    """What is wrong with a spec, in words; empty when it can be drawn as meant. The renderer itself tolerates a
    dangling reference (it leaves the line out), which a person reading the picture would not notice."""
    kind = spec.get("picture")
    out: List[str] = []
    if kind == "map":
        cols = {c[0] for c in spec.get("columns") or [] if c}
        ids = [n.get("id") for n in spec.get("nodes") or []]
        out += [f"node {n.get('id')!r} is in column {n.get('col')!r}, which `columns` does not have"
                for n in spec.get("nodes") or [] if n.get("col") not in cols]
        out += [f"node id {i!r} is used twice" for i in sorted({i for i in ids if ids.count(i) > 1})]
        out += [f"edge {e.get('from')!r} → {e.get('to')!r} names a node that does not exist"
                for e in spec.get("edges") or [] if e.get("from") not in ids or e.get("to") not in ids]
        if not spec.get("nodes"):
            out.append("a map needs nodes")
    elif kind == "stages":
        n = len(spec.get("stages") or [])
        bad = lambda i: not isinstance(i, int) or not 0 <= i < n  # noqa: E731
        out += [f"loop {lp.get('from')!r} → {lp.get('to')!r}: stages are numbered 0 to {n - 1}"
                for lp in spec.get("loops") or [] if bad(lp.get("from")) or bad(lp.get("to"))]
        out += [f"gate from stage {i!r}: stages are numbered 0 to {n - 1}"
                for i in (spec.get("gate") or {}).get("from") or [] if bad(i)]
        if not n:
            out.append("stages needs at least one stage")
    elif kind == "sequence":
        lanes = {ln.get("id") for ln in spec.get("lanes") or []}
        for i, st in enumerate(spec.get("steps") or [], 1):
            if "note" in st:
                out += [f"step {i}: the note spans lane {x!r}, which does not exist" for x in st.get("over") or []
                        if x not in lanes]
            elif st.get("from") not in lanes or st.get("to") not in lanes:
                out.append(f"step {i}: {st.get('from')!r} → {st.get('to')!r} names a lane that does not exist")
            elif not st.get("text"):
                out.append(f"step {i} has no text")
        if not lanes:
            out.append("a sequence needs lanes")
    else:
        out.append(f"picture must be map, stages or sequence, not {kind!r}")
    return out


def draw(spec: dict) -> str:
    kind, label = spec["picture"], spec.get("label") or spec["picture"]
    if kind == "map":
        return map_svg(_map_from_spec(spec), label=label)
    if kind == "stages":
        return stages_svg(spec, label=label)
    if kind == "sequence":
        return sequence_svg(spec, label=label)
    raise ValueError(f"unknown picture kind: {kind!r}")


def write_if_changed(path: Path, text: str) -> bool:
    """LF on every OS (a Windows text-mode write would turn each newline into CRLF)."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


def draw_files(paths: List[Path]) -> List[Path]:
    """Each spec (a JSON file with a "picture" key) drawn as an SVG beside it; other JSON files are skipped."""
    drawn = []
    for path in paths:
        spec = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(spec, dict) or "picture" not in spec:
            continue  # the config and other data beside the pictures
        problems = check(spec)
        if problems:
            raise ValueError(f"{path}: " + "; ".join(problems))
        out = path.with_suffix(".svg")
        if write_if_changed(out, draw(spec)):
            drawn.append(out)
    return drawn
