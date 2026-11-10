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

import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import activity, agents, architecture, canvas, config, design, graph, inside, profile
from .compare import checkpoint_sha, compare, turn_sha
from .extract import Code, first_party, outside_imports
from .learn import STDLIB, BASIC
from .sources import GitSource, git
from .topics import ROLE_OF, TECH, tech_key

SLOT = 2          # rows under a diagram for the picked box's detail
SIGN = {"added": "+", "removed": "−", "changed": "~", "warn": "▲", "info": "◆"}
NOTE_STYLE = {"added": "added", "removed": "removed", "changed": "changed", "warn": "attention", "info": "accent"}


def _name(nid: str, *bps: dict) -> str:
    return design.name_of(nid, *bps)


def _explain(c, model: Dict[str, Any]) -> Tuple[Dict[str, dict], Dict[str, Tuple[str, str]]]:
    """Each box of a diagram: what Enter does on it ({bid: {"enter": {"zoom": id}}}), and its detail, what it uses and
    what uses it, as (lead, text) ({bid: (lead, text)}), in the diagram's order."""
    conns = graph.connections(c.after)
    names = {n["id"]: n["title"] for n in model["nodes"]}
    names.update({i["id"]: i["title"] for n in model["nodes"] for i in n.get("items") or []})
    on: Dict[str, dict] = {}
    said: Dict[str, Tuple[str, str]] = {}
    for n in model["nodes"]:
        for nid, title in [(n["id"], n["title"])] + [(i["id"], i["title"]) for i in n.get("items") or []]:
            bid = "b:" + nid
            if nid.startswith("f:"):                      # a module: what it imports, what imports it, what it serves
                outs = sorted({names.get(e["to"], e["to"]) for e in model["edges"] if e["from"] == nid})
                ins = sorted({names.get(e["from"], e["from"]) for e in model["edges"] if e["to"] == nid})
                words = ([f"uses {design.and_list(outs, 4)}"] if outs else []) + (
                    [f"used by {design.and_list(ins, 4)}"] if ins else [])
                serves = [line["text"] for line in n.get("lines") or [] if not line["text"].startswith("reads ")]
                if serves:
                    words.append(f"serves {design.and_list(serves, 3)}")
                said[bid] = (f"{nid[2:]}  ", " · ".join(words) or "stands alone in its part")
                continue
            on[bid] = {"enter": {"zoom": nid}}
            if nid.startswith("a:"):
                uses = sorted({names.get(e["to"], e["to"]) for e in model["edges"] if e["from"] == nid})
                said[bid] = (f"{title}  ", f"uses {design.and_list(uses, 5)}")
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
            said[bid] = (f"{title}  ", " · ".join(words) or "connects to nothing in the system")
    return on, said


def _diagram(page: canvas.Page, c, model: Dict[str, Any], width: int, centre: bool = False) -> canvas.Grid:
    """The diagram on the page, every box a block: Enter zooms in on it; its detail says what it uses and what uses
    it, all of it (the diagram draws each box's main flows)."""
    g = canvas.layered(model["nodes"], model["edges"], model["columns"], width=width, centre=centre, zoom=ZOOM)
    on, said = _explain(c, model)
    for bid, (lead, text) in said.items():
        g.details[bid] = canvas.detail_rows(text, page.width, "muted", (lead, "bold"))
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


def _learn_parts(c, reading: design.Reading, part: str, learning: set) -> Dict[str, Any]:
    """What LEARN says about a part: its principle (role, text) or None, its shape notes, where it sits (one
    sentence), and the technology to teach: [(key, title, what, tag)]."""
    role = reading.principle(part)
    notes = reading.shape_notes(part)
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
    # technology: new to the system this turn, or marked new to you, used here
    before, after = c.before.get("stack") or {}, c.after.get("stack") or {}
    had = {tech_key(n) for p in before.values() for n in p} - {None}
    new = sorted({tech_key(n) for p in after.values() for n in p} - had - {None})
    mine = set()
    if not part.startswith("x:") and part in c.after["components"]:
        mine = {tech_key(n) for n in (after.get(part) or {})} - {None}
    teach = [k for k in new if k in TECH] + sorted(k for k in (mine & learning) if k in TECH and k not in new)
    tech = [(k, TECH[k][0], TECH[k][1], ("new in the system" if k in new else "★ new to you")
             + (f" · {ROLE_OF[k]}" if k in ROLE_OF else "")) for k in teach[:3]]
    return {"name": reading.name(part), "role": role, "notes": notes, "where": "; ".join(where) + ".", "tech": tech}


def _learn(page: canvas.Page, root: Path, c, reading: design.Reading, part: str, learning: set) -> None:
    """How the part is designed, as a designer would point it out; and the basics only of the technology new to the
    system this turn, or that the person marked new to them."""
    L = _learn_parts(c, reading, part, learning)
    page.heading(f"LEARN · how {L['name']} is designed")
    role = L["role"]
    if role:
        page.para(role[1], "", first=(f" ◆ {role[0][0].upper() + role[0][1:]}  ", "accent"))
    _notes(page, L["notes"], 3)
    page.para(L["where"], "", indent=0, first=(" ◆ Where it sits  ", "accent"))
    for _key, title, what, tag in L["tech"]:
        page.para(what, "muted", first=(f" {title}  ", "good bold"))
        page.add(["   " + tag, "muted"])


def steps(events: List[dict], code: Code, cfg: Dict[str, Any], most: int = 6) -> List[Dict[str, Any]]:
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
            for item in design.summarize({**i, "weight": i.get("weight", 1)} for i in fresh)[:2]:
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
            mark = item["sign"] if item["sign"] in "+−~▲↺" else SIGN.get(item["status"], item["sign"])
            page.add([at, "muted"], [mark + " ", NOTE_STYLE.get(item["status"], "muted")],
                     [canvas.cut(item["text"], page.width - 9), "" if item["status"] != "muted" else "muted"])
            why = canvas.cut(item.get("why", ""), page.width - 9 - len(st.get("file", "")) - 4)
            why += f" ({st['file']})" if st.get("file") else ""
            if why.strip():
                page.add(["        " + why, "muted"])
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


def _now_model(c, root: Path, part: str, now: Dict[str, Any], marks) -> Tuple[Dict[str, Any], str, str]:
    """The diagram Now draws: the part zoomed in on (a component's modules, a container's components), else the
    system; with its heading and aside."""
    agent = now["part"]
    if part and part in c.after["components"]:          # a component: its modules, and what is around them
        model = inside.internals(c, root, part, now.get("path", "") if agent == part else "", marks)
        if model:
            holder = architecture.groups_of(c.after, c.cfg, design.kinds(c.after, c.cfg)).get(part, "")
            where = f" · in {holder[2:]}" if holder else ""
            more = f", {model['more']} more not drawn" if model["more"] else ""
            return model, f"Inside {model['title']}", (f"{model['files']} modules{more}{where} · b back · f follow "
                                                        "Claude · ↵ zoom in")
    if part:
        model = architecture.container(c, part, marks, agent)
        if model:
            return model, f"Zoomed in on {model['title']}", ("its components · b back · f follow Claude · ↵ "
                                                             "a component's insides")
    return (architecture.system(c, marks, agent), "System",
            "● Claude · + new · ~ changed · − gone · click, then ← ↑ ↓ → and ↵")


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

    urgent = [n for n in notes if n["weight"] >= design.ATTENTION] + collisions(root, events, code, cfg)
    if urgent:
        page.heading("▲ Needs your attention", "worth a look before Claude goes on")
        page.rows[-1][0][1] = "attention"
        _notes(page, urgent, 3, ask=True)

    agent = now["part"]
    model, title, aside = _now_model(c, root, part, now, marks)
    page.heading(title, aside)
    badges(model, root, events, code)
    g = _diagram(page, c, model, width, centre=bool(part))
    ids = {b["id"] for b in g.blocks}
    page.pick = next((b for b in ("b:" + (part or ""), "b:" + (agent or "")) if b in ids), "")

    rest = design.summarize([n for n in notes if n not in urgent])
    base = "this turn" if c.base == "the start of this turn" else f"since {c.base}"
    parts = len(design.summarize(notes))
    page.heading("This turn", f"{len(notes)} change{'' if len(notes) == 1 else 's'} to the design, in {parts} "
                 f"part{'' if parts == 1 else 's'}; each change in full on the Changes tab" if notes else base)
    if rest:
        _notes(page, rest, 4)
        if len(rest) > 4:
            page.add([f"   and {len(rest) - 4} more parts changed: the Changes tab has them all", "muted"])
    elif not notes:
        page.add(["   nothing in the system's structure has changed yet", "muted"])

    shown = part or agent
    if shown and (shown in c.after["components"] or shown.startswith("x:")):
        _learn(page, root, c, reading, shown, profile.learning())

    _steps(page, events, code, cfg)
    return page.json(view="now", part=part or "")


def _review_base(root: Path) -> Tuple[str, str]:
    """What Review compares against, and how to say it: the checkpoint; before one, the commit before the last."""
    base, since = "checkpoint", "since your checkpoint"
    if not checkpoint_sha(root):   # nothing marked as seen yet: what the last commit did, and what is not committed
        try:
            git("rev-parse", "--verify", "HEAD~1^{commit}", cwd=root)
            last = git("log", "-1", "--format=%h %s", cwd=root).strip()
            base, since = "HEAD~1", f"since before the last commit ({canvas.cut(last, 50)})"
        except Exception:   # a first commit: from there
            since = "since the last commit"
    return base, since


def view_review(root: Path, width: int) -> Dict[str, Any]:
    """Everything you have not marked as seen: the system diagram since your checkpoint, its changes coloured, then
    each change read for design, then the exact list. Mark it seen and the next review starts from here."""
    base, since = _review_base(root)
    c = compare(root, base, ".")
    agent = activity.now(activity.events(root, 200), Code(c.cfg))["part"]
    page = canvas.Page(width)
    page.add([c.diff["headline"], "bold" if c.diff["changes"] else "muted"])
    page.heading("System " + since, "click, then ← ↑ ↓ → · ↵ zooms in")
    g = _diagram(page, c, architecture.system(c, None, agent), width)
    ids = {b["id"] for b in g.blocks}
    page.pick = "b:" + agent if agent and "b:" + agent in ids else ""
    notes = design.Reading(c).notes()
    page.heading("Every change " + since, f"{len(notes)} to the design" if notes else "")
    if notes:
        _notes(page, notes, 60, ask=True)
        page.blank()
        for line in c.diff["plan"].splitlines():
            page.add([line, "muted"])
        page.blank()
        page.add(["Seen it all? Tell Claude \"mark it as seen\" (or run system-diff checkpoint): the next review "
                  "starts from here.", "muted"])
    else:
        page.add(["   no change to the system's structure " + since.split(" (")[0], "muted"])
    edited = [ch for ch in c.diff["changes"] if ch["entity"] in ("component", "deploy") and ch["op"] == "change"]
    if edited:   # code that changed without changing the structure: still worth a look
        page.heading("Code changed " + since.split(" (")[0], "inside these parts, their structure the same")
        for ch in edited:
            page.add([" ~ ", "changed"], [ch["label"], ""], [("  " + ch["detail"][0]) if ch["detail"] else "", "muted"])
    return page.json(view="review", part="")


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


def _stack_view(c, root: Path) -> Dict[str, Any]:
    """What Topics shows: each technology (key -> {part: files}), in its layer's row, most used first (the standard
    library last); the system diagram; and for each box the technology it uses."""
    cfg = c.cfg
    agent = activity.now(activity.events(root, 200), Code(cfg))["part"]
    techs = stack(GitSource(c.head_sha, root), c.after, cfg)
    kind = design.kinds(c.after, cfg)
    rows: Dict[str, List[str]] = defaultdict(list)
    model = architecture.system(c, None, agent)
    holder = {i["id"]: n["id"] for n in model["nodes"] for i in n.get("items") or []}
    for key in sorted(techs, key=lambda k: (k.split(".")[0] in STDLIB, -sum(techs[k].values()), TECH[k][0].lower())):
        col = Counter()
        for p, n in techs[key].items():
            col[kind.get(p, "logic")] += n
        rows[col.most_common(1)[0][0]].append(key)
    order = [(k, h) for k, h in cfg["picture"]["columns"] if rows.get(k)]
    parts = {key: sorted({"b:" + p for p in ps} | {"b:" + holder[p] for p in ps if p in holder})
             for key, ps in techs.items()}
    boxes: Dict[str, Tuple[str, List[str]]] = {}
    for n in model["nodes"]:                  # a part (or container): the technology it uses, most first
        for box in [n] + list(n.get("items") or []):
            inside = {i["id"] for i in n.get("items") or []} if box is n and n.get("items") else {box["id"]}
            used = sorted((k for k in techs if inside & set(techs[k])),
                          key=lambda k: (-sum(techs[k].get(p, 0) for p in inside), TECH[k][0].lower()))
            if used:
                boxes["b:" + box["id"]] = (box["title"], used)
    return {"techs": techs, "rows": rows, "order": order, "model": model, "parts": parts, "boxes": boxes,
            "learning": profile.learning(), "names": {cid: comp["label"] for cid, comp in c.after["components"].items()}}


def view_topics(root: Path, width: int) -> Dict[str, Any]:
    c = compare(root, "checkpoint", ".")
    S = _stack_view(c, root)
    techs, learning, model = S["techs"], S["learning"], S["model"]
    page = canvas.Page(width)
    page.heading("The stack", "pick one to light where it is used · space: ★ new to me")
    rows = {k: [{"id": "t:" + key, "text": ("★ " if key in learning else "") + TECH[key][0],
                 "style": "good bold" if key in learning else "muted" if key.split(".")[0] in STDLIB else "bold"}
                for key in keys] for k, keys in S["rows"].items()}
    g = canvas.chips([(h, rows[k]) for k, h in S["order"]], width, label_w=18)
    names = S["names"]
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
        g.lights["t:" + key] = S["parts"][key]
    page.grid(g, slot=4, on={"t:" + k: {"space": {"learn": k}} for k in techs})
    page.pick = g.blocks[0]["id"] if g.blocks else ""
    page.heading("Where it is used", "the picked technology's parts light up; a picked part lights what it uses")
    d = _diagram(page, c, model, width)
    for bid, (title, used) in S["boxes"].items():   # a part picked: the technology it uses, lit and named
        if bid not in d.details:
            continue
        page.lights[bid] = list(dict.fromkeys(page.lights.get(bid, []) + ["t:" + k for k in used]))
        page.details[bid] = canvas.detail_rows("uses " + design.and_list([TECH[k][0] for k in used], 6), width,
                                               "muted", (f"{title}  ", "bold"))[:SLOT]
    return page.json(view="topics", part="")


# ── a team of agents ──────────────────────────────────────────────────────────

def team(root: Path, events: List[dict], code: Code) -> Dict[str, Dict[str, Any]]:
    """Every agent: its plan entry (area, blocks, brief), the parts it edited and read this turn, when it last acted,
    by the name it goes by."""
    st = agents.state(root)
    out: Dict[str, Dict[str, Any]] = {}
    for g in (st["plan"].get("agents") or []):
        out[g["name"]] = {"area": g.get("area", ""), "blocks": g.get("blocks", []), "edits": [], "reads": [], "at": 0.0}
    for aid, seen in agents.who_worked(events, code).items():
        name = st["ids"].get(aid) or (agents.MAIN if aid == agents.MAIN else "agent-" + aid[:6])
        a = out.setdefault(name, {"area": "", "blocks": [], "edits": [], "reads": [], "at": 0.0})
        a["edits"] = sorted(set(a["edits"]) | set(seen["edits"]))
        a["reads"] = sorted(set(a["reads"]) | set(seen["reads"]))
        a["at"] = max(a["at"], seen["at"])
    return out


def badges(model: Dict[str, Any], root: Path, events: List[dict], code: Code) -> None:
    """Who works where, on the diagram: each block's agents (those editing it, else those it is claimed for) after
    its title, when more than one agent is about."""
    who = team(root, events, code)
    if len(who) < 2:
        return
    on: Dict[str, List[str]] = defaultdict(list)
    for name, a in who.items():
        for part in (a["edits"] or a["blocks"]):
            if name not in on[part]:
                on[part].append(name)
    for n in model["nodes"]:
        for box in [n] + list(n.get("items") or []):
            if on.get(box["id"]):
                box["title"] += "  " + " ".join(f"@{t}" for t in on[box["id"]][:2])


def collisions(root: Path, events: List[dict], code: Code, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Agents in each other's way this turn: two editing one block, or one editing a block claimed for another."""
    who = team(root, events, code)
    claims = agents.state(root)["claims"]
    editors: Dict[str, List[str]] = defaultdict(list)
    for name, a in who.items():
        for part in a["edits"]:
            editors[part].append(name)
    out = []
    for part, names in sorted(editors.items()):
        P = activity.label(cfg, part)
        if len(names) > 1:
            out.append({"sign": "▲", "status": "warn", "weight": 9, "text": f"{design.and_list(names)} all edit {P}",
                        "why": "two agents changing one block can undo each other's work", "ask":
                        f"Should {P} be left to {claims.get(part) or names[0]}, the others publishing what they need?"})
        elif claims.get(part) and claims[part] not in names and names != [agents.MAIN]:
            out.append({"sign": "▲", "status": "warn", "weight": 8, "text": f"{names[0]} edits {P}, claimed for "
                        f"{claims[part]}", "why": "outside its own blocks", "ask": ""})
    return out


def colours(who: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Each agent's colour, 0..4 (style word f"agent{n}"): main first, then by name; past five they repeat."""
    order = sorted(who, key=lambda n: (n != agents.MAIN, n))
    return {name: i % 5 for i, name in enumerate(order)}


def owners(model: Dict[str, Any], who: Dict[str, Dict[str, Any]], colour: Dict[str, int]) -> None:
    """Colour each box by its agent: "owner" on every node or item an agent edited this turn (the one that edited
    it most recently), else the one it is claimed for (the first)."""
    edited: Dict[str, str] = {}
    claimed: Dict[str, str] = {}
    for name, a in sorted(who.items(), key=lambda kv: kv[1]["at"]):
        for part in a["edits"]:
            edited[part] = name                      # later (more recent) agents overwrite
    for name in sorted(who, key=lambda n: colour[n]):
        for part in who[name]["blocks"]:
            claimed.setdefault(part, name)
    for n in model["nodes"]:
        for box in [n] + list(n.get("items") or []):
            name = edited.get(box["id"]) or claimed.get(box["id"])
            if name is not None:
                box["owner"] = colour[name]


def view_agents(root: Path, width: int) -> Dict[str, Any]:
    cfg = config.lenient(root)
    code = Code(cfg)
    events = activity.events(root, 400)
    c = compare(root, "turn" if turn_sha(root) else "checkpoint", ".")
    st = agents.state(root)
    who = team(root, events, code)
    page = canvas.Page(width)
    task = (st["plan"] or {}).get("task", "")
    page.heading("The team", f"plan: {canvas.cut(task, width - 22)}" if task else
                 "no plan yet: Claude makes one with plan_agents")
    clash = collisions(root, events, code, cfg)
    if clash:
        _notes(page, clash, 4, ask=True)
    model = architecture.system(c, None, activity.now(events, code)["part"])
    holder = {i["id"]: n["id"] for n in model["nodes"] for i in n.get("items") or []}
    drawn = {n["id"] for n in model["nodes"]} | set(holder)
    colour = colours(who)
    g = canvas.Grid()
    y = 0
    for name, a in sorted(who.items(), key=lambda kv: (kv[0] != agents.MAIN, -kv[1]["at"], kv[0])):
        live = a["at"] and time.time() - a["at"] < 120
        bid = "ag:" + name
        runs = [("● " if live else "○ ", "accent" if live else "muted"), (name, f"agent{colour[name]} bold"),
                (f"  {a['area']}" if a["area"] else "", "muted"),
                (f"  {time.strftime('%H:%M', time.localtime(a['at']))}" if a["at"] else "  not started", "muted")]
        unread = unread_count(root, name)
        if unread > 0:
            runs.append((f"  {unread} unread on the bus", "attention"))
        x = 0
        for text, style in runs:
            if x >= width:
                break
            text = canvas.cut(text, max(0, width - x))
            if text:
                g.put(x, y, text, style, [bid])
                x += len(text)
        g.blocks.append({"id": bid, "x": 0, "y": y, "w": max(1, x), "h": 1})
        parts = list(dict.fromkeys(list(a["edits"]) + list(a["blocks"])))
        g.lights[bid] = sorted({"b:" + p for p in parts if p in drawn} | {"b:" + holder[p] for p in parts
                                                                            if p in holder})
        mine = [activity.label(cfg, b) for b in a["blocks"]]
        done = [activity.label(cfg, b) for b in a["edits"]]
        g.details[bid] = ([[["owns ", "muted"], [canvas.cut(design.and_list(mine, 5) if mine else "no blocks",
                                                            width - 6), ""]]]
                          + [[["edited ", "muted"], [canvas.cut(design.and_list(done, 5) if done else "nothing yet",
                                                               width - 8), ""]]])
        y += 1
    if who:
        page.grid(g, slot=2)
        swimlanes(page, root, events, code, who, colour, width)
    else:
        page.add(["   no agent has touched the code yet", "muted"])
    page.heading("Who works where", "@name after a block: the agents on it, in their colour")
    if who:
        legend: List[list] = []
        for name in sorted(who, key=lambda n: colour[n]):
            legend += [["■ " + name, f"agent{colour[name]}"], ["   ", ""]]
        page.add(*legend)
    badges(model, root, events, code)
    owners(model, who, colour)
    _diagram(page, c, model, width)
    page.heading("The bus", "what agents told each other, newest first")
    msgs = agents.messages(root)[-10:][::-1]
    for m in msgs:
        style = {"change": "changed", "warning": "attention", "plan": "accent"}.get(m.get("kind", ""), "")
        page.add([time.strftime("%H:%M ", time.localtime(m["at"])), "muted"], [m["from"], "bold"],
                 [f" → {m['topic']}: ", "muted"], [canvas.cut(m["text"], width - 20 - len(m["topic"])), style])
        if m.get("why"):
            page.add(["        " + canvas.cut(m["why"], width - 9), "muted"])
    if not msgs:
        page.add(["   nothing yet: changes to a block are published here as agents make them", "muted"])
    return page.json(view="agents", part="")


LANE = {"change": ("▲", 4), "message": ("✉", 3), "edit": ("✎", 2), "read": ("◎", 1)}


def _lanes(root: Path, events: List[dict], who: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The team at work since the turn began: when it began, how long it has run, and for each agent its marks
    [(at, kind)], what it is doing now ("" when idle), when it last acted, and how far it has got."""
    st = agents.state(root)
    start = max((e["at"] for e in events if e.get("event") == activity.PROMPT
                 and not activity.is_system(e.get("text", ""))), default=time.time() - 600)
    now = time.time()
    marks: Dict[str, List[tuple]] = defaultdict(list)
    last: Dict[str, dict] = {}

    def name_of(aid: str) -> str:
        return agents.MAIN if not aid or aid == agents.MAIN else st["ids"].get(aid) or "agent-" + aid[:6]

    for e in events:
        kind = {activity.EDIT: "edit", activity.READ: "read"}.get(e.get("event"))
        if kind and e["at"] >= start:
            n = name_of(e.get("agent") or "")
            marks[n].append((e["at"], kind))
            last[n] = e
    for m in agents.messages(root):
        if m["at"] >= start and m["from"] in who:
            marks[m["from"]].append((m["at"], "change" if m.get("kind") == "change" else "message"))
    changes = {n: sum(1 for _at, k in ms if k == "change") for n, ms in marks.items()}
    lanes: Dict[str, Dict[str, Any]] = {}
    for name, a in who.items():
        e = last.get(name)
        doing = ""
        if e and now - e["at"] < 90:
            doing = ("✎ editing " if e["event"] == activity.EDIT else "◎ reading ") + e["path"].rsplit("/", 1)[-1]
        owned = set(a["blocks"])
        touched = len(owned & set(a["edits"])) if owned else len(a["edits"])
        progress = (f" · {touched}/{len(owned)} blocks" if owned else f" · {touched} blocks") + (
            f" · {changes.get(name, 0)} changes" if changes.get(name) else "")
        lanes[name] = {"marks": marks.get(name, []), "doing": doing, "last": e["at"] if e else 0.0,
                       "progress": progress}
    return {"start": start, "now": now, "span": max(120.0, now - start), "lanes": lanes}


def swimlanes(page: canvas.Page, root: Path, events: List[dict], code: Code, who: Dict[str, Dict[str, Any]],
              colour: Dict[str, int], width: int) -> None:
    """The team at work, live: a lane per agent in its colour, time left to right since the turn began, a mark where
    it read (◎), edited (✎), changed the system's structure (▲) or told the others something (✉); then what it is
    doing now and how far it has got."""
    L = _lanes(root, events, who)
    start, span = L["start"], L["span"]
    label_w = min(14, max(len(n) for n in who) + 2)
    cells = max(10, min(60, width - label_w - 34))
    page.heading("Live", "since the turn began · ◎ read  ✎ edit  ▲ changed the system  ✉ told the others")
    for name in sorted(who, key=lambda n: colour[n]):
        lane = ["·"] * cells
        rank = [0] * cells
        me = L["lanes"][name]
        for at, kind in me["marks"]:
            i = min(cells - 1, int((at - start) / span * cells))
            glyph, r = LANE[kind]
            if r > rank[i]:
                lane[i], rank[i] = glyph, r
        style = f"agent{colour[name]}"
        runs = [[canvas.cut(name, label_w - 1).ljust(label_w), style + " bold"]]
        for ch in lane:
            runs.append([ch, "muted" if ch == "·" else style + (" bold" if ch == "▲" else "")])
        if me["doing"]:
            runs.append(["  ● " + me["doing"], "accent"])
        else:
            at = me["last"]
            runs.append([f"  ○ idle{' since ' + time.strftime('%H:%M', time.localtime(at)) if at else ''}",
                         "muted"])
        runs.append([me["progress"], "muted"])
        page.add(*runs)


ZOOM = 0   # how compact the diagrams are drawn: 0 full, 1 compact, 2 tiny (show sets it)


def show(root: Path, width: int = 80, view: str = "now", part: Optional[str] = None, zoom: int = -1,
         take: bool = True) -> Dict[str, Any]:
    """The page for a view. `zoom` -1: compact below 110 columns, full above. `take`: hand over what the web page
    sent to Claude (the pane, which submits it, does; the web page itself must not, or it would swallow it)."""
    global ZOOM
    ZOOM = (1 if width < 110 else 0) if zoom < 0 else max(0, min(2, zoom))
    out = _show(root, width, view, part)
    out["zoom"] = ZOOM
    out["web"] = web_url(root)
    out["send"] = take_outbox(root) if take else []
    return out


OUTBOX = "outbox.jsonl"   # what the web page's Send to Claude box wrote, for the pane to submit to the session


def take_outbox(root: Path) -> List[Dict[str, str]]:
    """Take what the web page asked to send to Claude: [{"id", "text"}...], each line once. The file is moved aside
    first (so a line the web page writes meanwhile goes to a fresh file, never lost or taken twice), then read and
    deleted. A bad line is skipped; no file, or any failure, is none."""
    try:
        from .sources import git_dir
        folder = git_dir(root)
        box = folder / OUTBOX
        if not box.exists():
            return []
        taken = folder / f"{OUTBOX}.{os.getpid()}.{time.time_ns()}.taking"
        os.replace(box, taken)
    except Exception:
        return []
    out: List[Dict[str, str]] = []
    try:
        for line in taken.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
                out.append({"id": str(item.get("id") or f"{taken.name}:{len(out)}"), "text": item["text"]})
    except Exception:
        pass
    try:
        taken.unlink()
    except Exception:
        pass
    return out


def web_url(root: Path) -> str:
    """The address of the running web page with the same views, or ""."""
    try:
        from . import watch
        url = getattr(watch, "web_url", None)
        return (url(root) or "") if url else ""
    except Exception:
        return ""


def unread_count(root: Path, name: str) -> int:
    """What `name` has not read on the bus since the current plan."""
    try:
        pending = getattr(agents, "pending", None)
        return int(pending(root, name)) if pending else len(agents.inbox(root, name, mark=False))
    except Exception:
        return 0


def _show(root: Path, width: int = 80, view: str = "now", part: Optional[str] = None) -> Dict[str, Any]:
    if view == "agents":
        return view_agents(root, width)
    if view in ("review", "map", "changes"):   # Map and Changes were one review in two tabs: now one
        return view_review(root, width)
    if view == "topics":
        return view_topics(root, width)
    return view_now(root, width, part or "")


# ── the same views for a web page ─────────────────────────────────────────────

def _web_note(n: Dict[str, Any]) -> Dict[str, str]:
    sign = n["sign"] if n["sign"] in "+−~" else SIGN.get(n["status"], n["sign"])
    return {"sign": sign, "status": n.get("status", ""), "text": n.get("text", ""), "why": n.get("why", "") or "",
            "ask": n.get("ask", "") or ""}


def _web_section(title: str, kind: str, items: List[Dict[str, Any]], aside: str = "") -> Dict[str, Any]:
    return {"title": title, "aside": aside, "kind": kind, "items": items}


def _web_diagram(c, model: Dict[str, Any], title: str, aside: str, zoom: int, pick: List[str]) -> Dict[str, Any]:
    """The diagram for the web page: its SVG (pictures.architecture_svg), and what each box lights, says and zooms
    into, as the pane's diagram (the same model, the same _explain)."""
    g = canvas.layered(model["nodes"], model["edges"], model["columns"], width=100, zoom=max(0, min(2, zoom)))
    on, said = _explain(c, model)
    ids = {b["id"] for b in g.blocks}
    svg = ""
    try:
        from . import pictures
        draw = getattr(pictures, "architecture_svg", None)
        svg = draw(model, label=title) if draw else ""
    except Exception:
        svg = ""
    return {"svg": svg or "", "title": title, "aside": aside, "lights": dict(g.lights),
            "details": {bid: text for bid, (_lead, text) in said.items()},
            "enter": {bid: o["enter"]["zoom"] for bid, o in on.items()},
            "pick": next((b for b in pick if b in ids), "")}


def _web_steps(events: List[dict], code: Code, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for st in steps(events, code, cfg):
        at = time.strftime("%H:%M", time.localtime(st["at"]))
        if st["kind"] == "change":
            item = st["item"]
            sign = item["sign"] if item["sign"] in "+−~▲↺" else SIGN.get(item["status"], item["sign"])
            out.append({"at": at, "kind": "change", "sign": sign, "status": item.get("status", ""),
                        "text": item["text"], "why": item.get("why", "") or "", "file": st.get("file", "")})
        elif st["kind"] == "edit":
            files = sorted(st["files"])
            what = "" if st["part"] is None else files[0] if len(files) == 1 else f"{len(files)} files"
            out.append({"at": at, "kind": "edit", "sign": "✎", "status": "muted",
                        "text": f"Claude edited {st['name']}" + (f" ({what})" if what else "")
                        + ": no change to the structure", "why": "", "file": ", ".join(files)})
        elif st["kind"] == "prompt":
            out.append({"at": at, "kind": "prompt", "sign": "›", "status": "muted",
                        "text": "You asked: " + st["text"], "why": "", "file": ""})
        else:
            said = st.get("text", "")
            out.append({"at": at, "kind": "stop", "sign": "✓", "status": "good",
                        "text": "Claude finished" + (f": {said}" if said else ""), "why": "", "file": ""})
    return out


def _web_now(root: Path, part: str, zoom: int) -> Dict[str, Any]:
    cfg = config.lenient(root)
    code = Code(cfg)
    events = activity.events(root, 200)
    now = activity.now(events, code)
    c = compare(root, "turn" if turn_sha(root) else "checkpoint", ".")
    marks = graph.change_marks(c.diff, c.before, c.after)
    reading = design.Reading(c)
    notes = reading.notes()
    agent = now["part"]
    name = activity.label(cfg, agent) if agent else ""
    if agent:
        headline = (("Claude is editing " if now["doing"] == "editing" else "Claude is reading ") + name
                    if now["working"] else f"Claude is waiting for you, last on {name}")
    else:
        headline = "Waiting for Claude to read or edit code"
    sections = []
    urgent = [n for n in notes if n["weight"] >= design.ATTENTION] + collisions(root, events, code, cfg)
    if urgent:
        sections.append(_web_section("Needs your attention", "notes", [_web_note(n) for n in urgent],
                                     "worth a look before Claude goes on"))
    model, title, aside = _now_model(c, root, part, now, marks)
    badges(model, root, events, code)
    diagram = _web_diagram(c, model, title, aside, zoom, ["b:" + (part or ""), "b:" + (agent or "")])
    rest = design.summarize([n for n in notes if n not in urgent])
    base = "this turn" if c.base == "the start of this turn" else f"since {c.base}"
    parts = len(design.summarize(notes))
    sections.append(_web_section("This turn", "notes", [_web_note(n) for n in rest],
                                 f"{len(notes)} change{'' if len(notes) == 1 else 's'} to the design, in {parts} "
                                 f"part{'' if parts == 1 else 's'}" if notes else base))
    shown = part or agent
    if shown and (shown in c.after["components"] or shown.startswith("x:")):
        L = _learn_parts(c, reading, shown, profile.learning())
        items = []
        if L["role"]:
            role = L["role"]
            items.append({"sign": "◆", "status": "info", "text": role[0][0].upper() + role[0][1:], "why": role[1],
                          "ask": ""})
        items += [_web_note(n) for n in L["notes"][:3]]
        items.append({"sign": "◆", "status": "info", "text": "Where it sits", "why": L["where"], "ask": ""})
        items += [{"sign": "★" if tag.startswith("★") else "+", "status": "added", "text": title_,
                   "why": f"{what} ({tag.lstrip('★ ')})", "ask": ""} for _k, title_, what, tag in L["tech"]]
        sections.append(_web_section(f"Learn · how {L['name']} is designed", "notes", items))
    told = _web_steps(events, code, cfg)
    if told:
        sections.append(_web_section("What just happened", "steps", told, "the system diff, step by step"))
    return {"view": "now", "part": part or "", "title": "Now", "headline": headline,
            "status": {"working": bool(now["working"]), "doing": now["doing"] or "", "part": agent or "",
                       "prompt": now.get("prompt", "") or ""},
            "diagram": diagram, "sections": sections}


def _web_review(root: Path, zoom: int) -> Dict[str, Any]:
    base, since = _review_base(root)
    c = compare(root, base, ".")
    agent = activity.now(activity.events(root, 200), Code(c.cfg))["part"]
    diagram = _web_diagram(c, architecture.system(c, None, agent), "System " + since, "click a box · zoom in",
                           zoom, ["b:" + agent] if agent else [])
    notes = design.Reading(c).notes()
    short = since.split(" (")[0]
    sections = [_web_section("Every change " + since, "notes", [_web_note(n) for n in notes],
                             f"{len(notes)} to the design" if notes else f"no change to the system's structure {short}")]
    if notes:
        sections.append(_web_section("The exact list", "lines",
                                     [{"text": line, "style": "muted"} for line in c.diff["plan"].splitlines()],
                                     "Seen it all? Tell Claude \"mark it as seen\": the next review starts from here"))
    edited = [ch for ch in c.diff["changes"] if ch["entity"] in ("component", "deploy") and ch["op"] == "change"]
    if edited:
        sections.append(_web_section("Code changed " + short, "lines",
                                     [{"text": f"~ {ch['label']}" + (f"  {ch['detail'][0]}" if ch["detail"] else ""),
                                       "style": "changed"} for ch in edited],
                                     "inside these parts, their structure the same"))
    return {"view": "review", "part": "", "title": "Review", "headline": c.diff["headline"],
            "diagram": diagram, "sections": sections}


def _web_agents(root: Path, zoom: int) -> Dict[str, Any]:
    cfg = config.lenient(root)
    code = Code(cfg)
    events = activity.events(root, 400)
    c = compare(root, "turn" if turn_sha(root) else "checkpoint", ".")
    st = agents.state(root)
    who = team(root, events, code)
    task = (st["plan"] or {}).get("task", "")
    model = architecture.system(c, None, activity.now(events, code)["part"])
    holder = {i["id"]: n["id"] for n in model["nodes"] for i in n.get("items") or []}
    drawn = {n["id"] for n in model["nodes"]} | set(holder)
    colour = colours(who)
    L = _lanes(root, events, who) if who else {"start": 0.0, "span": 1.0, "lanes": {}}
    lanes, lit = [], {}
    for name, a in sorted(who.items(), key=lambda kv: (kv[0] != agents.MAIN, -kv[1]["at"], kv[0])):
        parts = list(dict.fromkeys(list(a["edits"]) + list(a["blocks"])))
        blocks = sorted({"b:" + p for p in parts if p in drawn} | {"b:" + holder[p] for p in parts if p in holder})
        lit["ag:" + name] = blocks
        me = L["lanes"].get(name, {"marks": [], "doing": "", "progress": ""})
        lanes.append({"name": name, "colour": colour[name], "area": a["area"],
                      "live": bool(a["at"] and time.time() - a["at"] < 120), "doing": me["doing"],
                      "progress": me["progress"].lstrip(" ·"), "unread": unread_count(root, name),
                      "marks": [{"pos": max(0.0, min(1.0, (at - L["start"]) / L["span"])), "kind": kind}
                                for at, kind in sorted(me["marks"])],
                      "blocks": blocks})
    sections = [_web_section("The team", "lanes", lanes, f"plan: {task}" if task else
                             "no plan yet: Claude makes one with plan_agents")]
    clash = collisions(root, events, code, cfg)
    if clash:
        sections.append(_web_section("In each other's way", "notes", [_web_note(n) for n in clash]))
    badges(model, root, events, code)
    owners(model, who, colour)
    diagram = _web_diagram(c, model, "Who works where", "each box in the colour of its agent", zoom, [])
    diagram["lights"].update(lit)
    bus = []
    for m in agents.messages(root)[-10:][::-1]:
        style = {"change": "changed", "warning": "attention", "plan": "accent"}.get(m.get("kind", ""), "")
        bus.append({"text": f"{time.strftime('%H:%M', time.localtime(m['at']))} {m['from']} → {m['topic']}: "
                            f"{m['text']}" + (f" ({m['why']})" if m.get("why") else ""), "style": style})
    sections.append(_web_section("The bus", "lines", bus, "what agents told each other, newest first"))
    return {"view": "agents", "part": "", "title": "Agents",
            "headline": task or "no plan yet: Claude makes one with plan_agents", "diagram": diagram,
            "sections": sections}


def _web_topics(root: Path, zoom: int) -> Dict[str, Any]:
    c = compare(root, "checkpoint", ".")
    S = _stack_view(c, root)
    techs, learning = S["techs"], S["learning"]
    names = S["names"]
    chips = []
    for k, heading in S["order"]:
        for key in S["rows"][k]:
            title, what, link = TECH[key]
            chips.append({"key": key, "title": title, "learning": key in learning,
                          "stdlib": key.split(".")[0] in STDLIB, "layer": heading, "parts": S["parts"][key],
                          "role": ROLE_OF.get(key, ""), "what": what, "link": link})
    diagram = _web_diagram(c, S["model"], "Where it is used",
                           "the picked technology's parts light up; a picked part lights what it uses", zoom, [])
    for key, parts in techs.items():
        diagram["lights"]["t:" + key] = S["parts"][key]
        users = sorted(parts, key=lambda p: (-parts[p], names.get(p, p)))
        files = sum(parts.values())
        diagram["details"]["t:" + key] = (f"used by {design.and_list([names.get(p, p) for p in users], 3)} · "
                                          f"{files} file{'' if files == 1 else 's'}")
    for bid, (_title, used) in S["boxes"].items():
        if bid in diagram["details"]:
            diagram["lights"][bid] = list(dict.fromkeys(diagram["lights"].get(bid, []) + ["t:" + k for k in used]))
            diagram["details"][bid] = "uses " + design.and_list([TECH[k][0] for k in used], 6)
    diagram["pick"] = "t:" + chips[0]["key"] if chips else ""
    return {"view": "topics", "part": "", "title": "Topics",
            "headline": f"{len(chips)} technolog{'y' if len(chips) == 1 else 'ies'} in the system",
            "diagram": diagram,
            "sections": [_web_section("The stack", "chips", chips, "pick one to light where it is used")]}


def web_view(root: Path, view: str = "now", part: str = "", zoom: int = 0) -> Dict[str, Any]:
    """A view of the pane (now, review, agents, topics) structured for a web page instead of rows of text:
    {"view", "part", "title", "headline", "status" (Now only), "diagram": {"svg", "title", "aside", "lights",
    "details", "enter", "pick"}, "sections": [{"title", "aside", "kind", "items"}], "web", "send"}. Section kinds:
    notes {sign, status, text, why, ask}; steps {at, kind, sign, status, text, why, file}; lanes {name, colour,
    area, live, doing, progress, unread, marks [{pos, kind}], blocks}; chips {key, title, learning, stdlib, layer,
    parts, role, what, link}; lines {text, style}. Never takes the outbox: "send" is always []."""
    root = Path(root)
    if view == "agents":
        out = _web_agents(root, zoom)
    elif view in ("review", "map", "changes"):
        out = _web_review(root, zoom)
    elif view == "topics":
        out = _web_topics(root, zoom)
    else:
        out = _web_now(root, part or "", zoom)
    out["web"] = web_url(root)
    out["send"] = []
    return out
