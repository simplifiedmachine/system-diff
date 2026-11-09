"""`system-diff panel --protocol 2`: the pane beside the chat as a PAGE (canvas.Page) the mod paints: rows of styled
runs, the system diagram's boxes as blocks to pick with the arrow keys or a click (a picked block lights its flows and
the boxes at their other ends, and shows a line or two on what it uses and what uses it), Enter to zoom in.

  now      what Claude is doing; what needs your attention; the system diagram with Claude's component lit (or the
           box you zoomed in on); what this turn did to the design; LEARN, how the part is designed and the
           technology new to the system or to you; what just happened
  map      the system diagram since your checkpoint
  changes  every change since the checkpoint, and this turn, read for design
  topics   the stack, technology by technology: pick one to light the parts that use it; space marks it new to you
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import activity, architecture, canvas, config, design, graph, profile
from .compare import compare, turn_sha
from .extract import Code, first_party, outside_imports
from .learn import STDLIB, BASIC
from .sources import GitSource
from .topics import ROLE_OF, TECH, tech_key

SLOT = 2          # rows under a diagram for the picked box's detail
SIGN = {"added": "+", "removed": "−", "changed": "~", "warn": "▲", "info": "◆"}
NOTE_STYLE = {"added": "added", "removed": "removed", "changed": "changed", "warn": "attention", "info": "accent"}


def _name(nid: str, *bps: dict) -> str:
    return design.name_of(nid, *bps)


def _diagram(page: canvas.Page, c, model: Dict[str, Any], width: int, centre: bool = False) -> canvas.Grid:
    """The diagram on the page, every box a block: Enter zooms in on it; its detail says what it uses and what uses
    it, all of it (the diagram draws each box's main flows)."""
    g = canvas.layered(model["nodes"], model["edges"], model["columns"], width=width, centre=centre)
    conns = graph.connections(c.after)
    names = {n["id"]: n["title"] for n in model["nodes"]}
    names.update({i["id"]: i["title"] for n in model["nodes"] for i in n.get("items") or []})
    on: Dict[str, dict] = {}
    for n in model["nodes"]:
        for nid, title in [(n["id"], n["title"])] + [(i["id"], i["title"]) for i in n.get("items") or []]:
            bid = "b:" + nid
            on[bid] = {"enter": {"zoom": nid}}
            if nid.startswith("a:"):
                uses = sorted({names.get(e["to"], e["to"]) for e in model["edges"] if e["from"] == nid})
                g.details[bid] = canvas.detail_rows(f"uses {design.and_list(uses, 5)}", page.width, "muted",
                                                    (f"{title}  ", "bold"))
                continue
            inside = [i["id"] for i in n.get("items") or []] if nid == n["id"] and n.get("items") else [nid]
            outs = sorted({_name(t, c.after, c.before) for (s, t), _k in conns.items() if s in inside
                           and t not in inside})
            ins = sorted({_name(s, c.after, c.before) for (s, t), _k in conns.items() if t in inside
                          and s not in inside})
            words = []
            if outs:
                words.append("uses " + design.and_list(outs, 4))
            if ins:
                words.append("used by " + design.and_list(ins, 4))
            g.details[bid] = canvas.detail_rows(" · ".join(words) or "connects to nothing in the system",
                                                page.width, "muted", (f"{title}  ", "bold"))
    page.grid(g, slot=SLOT, on=on)
    return g


def _notes(page: canvas.Page, notes: List[Dict[str, Any]], most: int, ask: bool = False) -> None:
    for n in notes[:most]:
        page.add([f" {SIGN.get(n['sign'] if n['sign'] in '+−~' else n['status'], n['sign'])} ",
                  NOTE_STYLE.get(n["status"], "")], [canvas.cut(n["text"], page.width - 4), ""])
        if n["why"]:
            page.para(n["why"], "muted", indent=3)
        if ask and n.get("ask"):
            page.para(n["ask"], "", indent=3, first=("   Ask Claude  ", "accent"))


def _status(page: canvas.Page, now: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    if now["part"]:
        name = activity.label(cfg, now["part"])
        if now["working"]:
            verb = "✎ Editing " if now["doing"] == "editing" else "◎ Reading "
            page.add(["● Claude is working  ", "accent"], [verb, "accent"], [name, "accent bold"])
        else:
            page.add(["○ Claude is waiting for you  ", "muted"], ["last on ", "muted"], [name, "bold"])
    else:
        page.add(["○ Waiting for Claude to read or edit code", "muted"])
    if now.get("prompt"):
        page.add(["  You asked  ", "muted"], [canvas.cut(now["prompt"], page.width - 13), ""])


def _learn(page: canvas.Page, root: Path, c, reading: design.Reading, part: str, learning: set) -> None:
    """How the part is designed, as a designer would point it out; and the basics only of the technology new to the
    system this turn, or that the person marked new to them."""
    P = reading.name(part)
    page.heading(f"LEARN · how {P} is designed")
    role = reading.principle(part)
    if role:
        page.para(role[1], "", first=(f" ◆ {role[0][0].upper() + role[0][1:]}  ", "accent"))
    notes = reading.shape_notes(part)
    _notes(page, notes, 3)
    s = reading.shape(part)
    where = []
    if part.startswith("x:"):
        where.append(f"{design.and_list([reading.name(u) for u in s['used_by']], 4)} use it" if s["used_by"]
                     else "nothing in the system uses it yet")
    else:
        where.append(f"uses {len(s['uses'])} part{'' if len(s['uses']) == 1 else 's'}, "
                     f"{len(s['used_by'])} use{'s' if len(s['used_by']) == 1 else ''} it")
        if s["entries"]:
            where.append(f"requests from {design.and_list([reading.name(e) for e in s['entries']], 3)} reach it")
        if s["outside"]:
            direct = set(s.get("direct_outside", []))
            where.append("it reaches " + design.and_list(
                [reading.name(x) + ("" if x in direct else " (through others)") for x in s["outside"]], 3))
        if not s["uses"] and s["used_by"]:
            where.append("a leaf: it depends on nothing in the system, so it changes freely")
    page.para("; ".join(where) + ".", "", indent=0, first=(" ◆ Where it sits  ", "accent"))
    # technology: new to the system this turn, or marked new to you, used here
    before, after = c.before.get("stack") or {}, c.after.get("stack") or {}
    had = {tech_key(n) for p in before.values() for n in p} - {None}
    new = sorted({tech_key(n) for p in after.values() for n in p} - had - {None})
    mine = set()
    if not part.startswith("x:") and part in c.after["components"]:
        mine = {tech_key(n) for n in (after.get(part) or {})} - {None}
    teach = [k for k in new if k in TECH] + sorted(k for k in (mine & learning) if k in TECH and k not in new)
    for key in teach[:3]:
        title, what, _link = TECH[key]
        tag = "new in the system" if key in new else "★ new to you"
        page.para(what, "muted", first=(f" {title}  ", "good bold"))
        page.add(["   " + tag + (f" · {ROLE_OF[key]}" if key in ROLE_OF else ""), "muted"])


def steps(events: List[dict], code: Code, cfg: Dict[str, Any], most: int = 8) -> List[Dict[str, Any]]:
    """What just happened to the system, step by step, newest first: each change an edit made, as a design note with
    why it matters and the file it came from; edits that changed no structure, told once per run; each prompt; the
    end of a turn. Reads are left out: they change nothing."""
    out: List[Dict[str, Any]] = []
    seen: Dict[str, float] = {}
    for e in sorted(events, key=lambda e: e["at"]):
        kind = e.get("event")
        if kind == activity.PROMPT and not activity.is_system(e.get("text", "")):
            out.append({"at": e["at"], "kind": "prompt", "text": e.get("text", "")})
        elif kind == activity.EDIT and e.get("path") and e.get("source") != "files":
            part = activity.part_of(e["path"], code)
            name = activity.label(cfg, part) if part else e["path"]
            last = out[-1] if out else None
            file = e["path"].rsplit("/", 1)[-1]
            if last and last["kind"] == "edit" and last["part"] == part:
                last["files"][file] = e["at"]
                last["at"] = max(last["files"].values())
            else:
                out.append({"at": e["at"], "kind": "edit", "part": part, "name": name, "files": {file: e["at"]}})
        elif kind == "change":
            fresh = []
            for item in e.get("items", []):
                if e["at"] - seen.get(item["text"], -1e9) < 60:   # the live views saw the same step: told once
                    continue
                seen[item["text"]] = e["at"]
                fresh.append(item)
            if not fresh:
                continue
            file = e.get("path", "").rsplit("/", 1)[-1]
            if out and out[-1]["kind"] == "edit" and file in out[-1]["files"]:
                last = out[-1]                                    # that edit is told by what it changed
                del last["files"][file]
                if last["files"]:
                    last["at"] = max(last["files"].values())
                else:
                    out.pop()
            for item in fresh:
                out.append({"at": e["at"], "kind": "change", "item": item,
                            "file": e.get("path", "").rsplit("/", 1)[-1]})
        elif kind == activity.STOP:
            out.append({"at": e["at"], "kind": "stop", "text": e.get("text", "")})
    return out[::-1][:most]


def _steps(page: canvas.Page, events: List[dict], code: Code, cfg: Dict[str, Any]) -> None:
    told = steps(events, code, cfg)
    if not told:
        return
    page.heading("What just happened", "the system diff, step by step")
    for st in told:
        at = time.strftime("%H:%M ", time.localtime(st["at"]))
        if st["kind"] == "change":
            item = st["item"]
            mark = SIGN.get(item["sign"] if item["sign"] in "+−~" else item["status"], item["sign"])
            page.add([at, "muted"], [mark + " ", NOTE_STYLE.get(item["status"], "muted")],
                     [canvas.cut(item["text"], page.width - 9), "" if item["status"] != "muted" else "muted"])
            why = item.get("why", "") + (f" ({st['file']})" if st.get("file") else "")
            if why:
                page.para(why, "muted", indent=8)
        elif st["kind"] == "edit":
            files = sorted(st["files"])
            what = "" if st["part"] is None else files[0] if len(files) == 1 else f"{len(files)} files"
            page.add([at, "muted"], ["✎ ", "muted"],
                     [canvas.cut(f"Claude edited {st['name']}" + (f" ({what})" if what else "")
                                 + ": no change to the structure", page.width - 9), "muted"])
        elif st["kind"] == "prompt":
            page.add([at, "muted"], ["› ", "muted"], [canvas.cut("You asked: " + st["text"], page.width - 9), "muted"])
        else:
            said = st.get("text", "")
            page.add([at, "muted"], ["✓ ", "good"],
                     [canvas.cut("Claude finished" + (f": {said}" if said else ""), page.width - 9), "good"])


def view_now(root: Path, width: int, part: str) -> Dict[str, Any]:
    cfg = config.lenient(root)
    code = Code(cfg)
    events = activity.events(root, 200)
    now = activity.now(events, code)
    c = compare(root, "turn" if turn_sha(root) else "checkpoint", ".")
    marks = graph.change_marks(c.diff, c.before, c.after)
    reading = design.Reading(c)
    notes = reading.notes()
    page = canvas.Page(width)
    _status(page, now, cfg)

    urgent = [n for n in notes if n["weight"] >= design.ATTENTION]
    if urgent:
        page.heading("▲ Needs your attention", "worth a look before Claude goes on")
        page.rows[-1][0][1] = "attention"
        _notes(page, urgent, 3, ask=True)

    agent = now["part"]
    model = architecture.container(c, part, marks, agent) if part else None
    if model:
        page.heading(f"Zoomed in on {model['title']}", "b back · f follow Claude · ↵ zoom in")
    else:
        model = architecture.system(c, marks, agent)
        page.heading("System", "● Claude · + new · ~ changed · − gone · click, then ← ↑ ↓ → and ↵")
    g = _diagram(page, c, model, width, centre=bool(part))
    ids = {b["id"] for b in g.blocks}
    page.pick = next((b for b in ("b:" + (part or ""), "b:" + (agent or "")) if b in ids), "")

    rest = [n for n in notes if n not in urgent]
    base = "this turn" if c.base == "the start of this turn" else f"since {c.base}"
    page.heading(f"This turn · {len(notes)} change{'' if len(notes) == 1 else 's'} to the design" if notes
                 else "This turn", "" if notes else base)
    if rest:
        _notes(page, rest, 4)
        if len(rest) > 4:
            page.add([f"   and {len(rest) - 4} more: the Changes tab has them all", "muted"])
    elif not notes:
        page.add(["   nothing in the system's structure has changed yet", "muted"])

    shown = part or agent
    if shown and (shown in c.after["components"] or shown.startswith("x:")):
        _learn(page, root, c, reading, shown, profile.learning())

    _steps(page, events, code, cfg)
    return page.json(view="now", part=part or "")


def view_map(root: Path, width: int) -> Dict[str, Any]:
    c = compare(root, "checkpoint", ".")
    agent = activity.now(activity.events(root, 200), Code(c.cfg))["part"]
    page = canvas.Page(width)
    page.add([c.diff["headline"], "bold" if c.diff["changes"] else "muted"])
    page.heading("System" + (" since your checkpoint" if c.base == "checkpoint" else f" since {c.base}"),
                 "click, then ← ↑ ↓ → · ↵ zooms in")
    g = _diagram(page, c, architecture.system(c, None, agent), width)
    ids = {b["id"] for b in g.blocks}
    page.pick = "b:" + agent if agent and "b:" + agent in ids else ""
    return page.json(view="map", part="")


def view_changes(root: Path, width: int) -> Dict[str, Any]:
    page = canvas.Page(width)
    for point in ["checkpoint"] + (["turn"] if turn_sha(root) else []):
        c = compare(root, point, ".")
        notes = design.Reading(c).notes()
        page.heading("This turn" if point == "turn" else "Since " + ("your checkpoint" if c.base == "checkpoint"
                                                                      else c.base))
        page.add([c.diff["headline"], "bold"])
        _notes(page, notes, 40, ask=True)
        page.blank()
        for line in c.diff["plan"].splitlines():
            page.add([line, "muted"])
    return page.json(view="changes", part="")


def stack(source, bp: dict, cfg: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """Technology key -> {part: files that import it}, for the whole system: from the blueprint's own record of
    what each part imports, else read from its files."""
    if bp.get("stack"):
        found: Dict[str, Counter] = defaultdict(Counter)
        for part, names in bp["stack"].items():
            for name, n in names.items():
                key = tech_key(name)
                if key and key not in BASIC:
                    found[key][part] = max(found[key][part], n)
        return {k: dict(v) for k, v in found.items()}
    code = Code(cfg)
    own = first_party(source.files, cfg["code"]["python_paths"])
    files = [f for f in source.files if code.is_code(f) and not code.is_test(f)]
    texts = source.read_many(files)
    out: Dict[str, Counter] = defaultdict(Counter)
    for f in files:
        for name in outside_imports(f, texts[f], own, cfg["code"]["aliases"]):
            key = tech_key(name)
            if key and key.split(".")[0] not in BASIC and key not in BASIC:
                out[key][code.part_of(f)] += 1
    return {k: dict(v) for k, v in out.items()}


def view_topics(root: Path, width: int) -> Dict[str, Any]:
    c = compare(root, "checkpoint", ".")
    cfg = c.cfg
    agent = activity.now(activity.events(root, 200), Code(cfg))["part"]
    learning = profile.learning()
    techs = stack(GitSource(c.head_sha, root), c.after, cfg)
    kind = design.kinds(c.after, cfg)
    heading = dict(cfg["picture"]["columns"])
    page = canvas.Page(width)
    page.heading("The stack", "pick one to light where it is used · space: ★ new to me")
    rows: Dict[str, List[dict]] = defaultdict(list)
    model = architecture.system(c, None, agent)
    holder = {i["id"]: n["id"] for n in model["nodes"] for i in n.get("items") or []}
    for key in sorted(techs, key=lambda k: (k.split(".")[0] in STDLIB, -sum(techs[k].values()), TECH[k][0].lower())):
        parts = techs[key]
        col = Counter()
        for p, n in parts.items():
            col[kind.get(p, "logic")] += n
        where = col.most_common(1)[0][0]
        std = key.split(".")[0] in STDLIB
        rows[where].append({"id": "t:" + key, "text": ("★ " if key in learning else "") + TECH[key][0],
                            "style": "good bold" if key in learning else "muted" if std else "bold"})
    order = [k for k, _h in cfg["picture"]["columns"] if rows.get(k)]
    g = canvas.chips([(heading.get(k, k), rows[k]) for k in order], width, label_w=18)
    names = {cid: comp["label"] for cid, comp in c.after["components"].items()}
    for key, parts in techs.items():
        title, what, link = TECH[key]
        users = sorted(parts, key=lambda p: (-parts[p], names.get(p, p)))
        files = sum(parts.values())
        role = ROLE_OF.get(key, "")
        g.details["t:" + key] = (
            [[[f"{title}  ", "good bold" if key in learning else "bold"],
              [canvas.cut(f"used by {design.and_list([names.get(p, p) for p in users], 3)} · {files} file"
                          f"{'' if files == 1 else 's'}", width - len(title) - 3), "muted"]]]
            + ([[["  " + canvas.cut(role, width - 3), ""]]] if role else [])
            + canvas.detail_rows(what + f"  {link}", width, "muted", ("  ", "")))[:4]
        g.lights["t:" + key] = sorted({"b:" + p for p in parts} | {"b:" + holder[p] for p in parts if p in holder})
    page.grid(g, slot=4, on={"t:" + k: {"space": {"learn": k}} for k in techs})
    page.pick = g.blocks[0]["id"] if g.blocks else ""
    page.heading("Where it is used", "the picked technology's parts light up")
    _diagram(page, c, model, width)
    return page.json(view="topics", part="")


def show(root: Path, width: int = 80, view: str = "now", part: Optional[str] = None) -> Dict[str, Any]:
    if view == "map":
        return view_map(root, width)
    if view == "changes":
        return view_changes(root, width)
    if view == "topics":
        return view_topics(root, width)
    return view_now(root, width, part or "")
