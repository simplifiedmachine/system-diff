"""`system-diff panel --protocol 2`: the pane beside the chat as a PAGE (canvas.Page) the mod paints: rows of styled
runs, the system diagram's boxes as blocks to pick with the arrow keys or a click (a picked block lights its flows and
the boxes at their other ends, and shows a line or two on what it uses and what uses it), Enter to zoom in.

  now      what Claude is doing; WORTH A LOOK, the changes with the most consequence first; what you asked against
           what changed; the system diagram with Claude's component lit (or the
           box you zoomed in on); what this turn did to the design; LEARN, how the part is designed and the
           technology new to the system or to you; what just happened
  map      the system diagram since your checkpoint
  changes  every change since the checkpoint, and this turn, read for design
  topics   the stack, technology by technology: pick one to light the parts that use it; space marks it new to you
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import activity, agents, architecture, regroup, canvas, config, design, graph, human, inside, objectives, \
    profile
from .compare import checkpoint_sha, compare as _compare, turn_sha
from .extract import Code
from .learn import STDLIB, BASIC
from .sources import git
from .topics import ROLE_OF, TECH, TOP_PACKAGE, tech_key


def compare(root: Path, base: str, head: str = "."):
    """A comparison with the LLM's regrouping of the diagram, when one is ready (regroup.ensure reads it, and starts
    the background job that makes it; it never waits). Not for a partial read: the structure is not known yet."""
    c = _compare(root, base, head)
    try:
        c.regroup = None if c.after.get("partial") else regroup.ensure(root, c.after, c.cfg)
    except Exception:  # noqa: BLE001 - only an improvement of the picture
        c.regroup = None
    return c


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
            if nid in c.after["components"] and nid != title and not c.after["components"][nid].get("declared"):
                words.insert(0, nid)        # a part named for what it is: its folder in the detail
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


def _status(page: canvas.Page, now: Dict[str, Any], cfg: Dict[str, Any], prompt: bool = True) -> None:
    if now["part"]:
        name = activity.label(cfg, now["part"])
        if now["working"]:
            verb = "✎ Editing " if now["doing"] == "editing" else "◎ Reading "
            page.add(["● Claude is working  ", "accent"], [verb, "accent"], [name, "accent bold"])
        else:
            page.add(["○ Claude is waiting for you  ", "muted"], ["last on ", "muted"], [name, "bold"])
    else:
        page.add(["○ Waiting for Claude to read or edit code", "muted"])
    if prompt and now.get("prompt"):
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
    """The diagram Now draws: the part zoomed in on (a component's modules; a container's components; an actor's or
    an outside system's connections), else the system; with its heading and aside. A zoom that cannot be drawn (its
    code no longer held, say) falls back a level, never to an error: an error here left the pane on the last page
    it had, which read as Enter doing nothing."""
    agent = now["part"]
    if part and part in c.after["components"]:          # a component: its modules, and what is around them
        try:
            model = inside.internals(c, root, part, now.get("path", "") if agent == part else "", marks)
        except Exception:  # noqa: BLE001 - its parts and flows instead (below)
            model = None
        if model:
            ov = getattr(c, "regroup", None)
            holder = architecture.groups_of(c.after, c.cfg, architecture.kinds_with(c.after, c.cfg, ov),
                                            overlay=ov).get(part, "")
            where = (f" · in {holder[2:]}" + (" (a grouping an LLM proposed)" if holder in _llm_groups(c) else "")
                     if holder else "")
            more = f", {model['more']} more not drawn" if model["more"] else ""
            path = "" if c.after["components"][part].get("declared") or model["title"] == part else f"{part} · "
            return model, f"Inside {model['title']}", (f"{path}{model['files']} modules{more}{where} · b back · "
                                                        "f follow Claude · ↵ zoom in")
    if part:
        try:
            model = architecture.container(c, part, marks, agent, getattr(c, "stack", None))
        except Exception:  # noqa: BLE001 - the system instead, said so below
            model = None
        if model:
            box = model.get("box", part)
            llm = "a grouping an LLM proposed · " if box in _llm_groups(c) else ""
            if box.startswith("g:"):
                n = sum(1 for x in model["nodes"] if str(x.get("col", "")).startswith("in"))
                what = f"its {n} components and their flows in and out · ↵ a component's insides"
            elif box.startswith(("a:", "x:")):
                what = "what it connects to · ↵ zoom in"
            else:
                what = f"{box} · its flows in and out · ↵ zoom in"
            return model, f"Zoomed in on {model['title']}", f"{llm}{what} · b back · f follow Claude"
        return (architecture.system(c, marks, agent), "System",
                f"{part} is not in the system now · b back · f follow Claude")
    return (architecture.system(c, marks, agent), "System",
            "● Claude · + new · ~ changed · − gone · click, then ← ↑ ↓ → and ↵" + _llm_aside(c))


def _llm_groups(c) -> set:
    """The containers ("g:<name>") the diagram has only because an LLM proposed them (regroup.py), not the config
    or the engine's own grouping: named as such wherever the page names them (honest about certainty)."""
    ov = getattr(c, "regroup", None)
    if not ov:
        return set()
    try:
        mine = set(architecture.groups_of(c.after, c.cfg, architecture.kinds_with(c.after, c.cfg, None)).values())
        theirs = set(architecture.groups_of(c.after, c.cfg, architecture.kinds_with(c.after, c.cfg, ov),
                                            overlay=ov).values())
        return theirs - mine
    except Exception:  # noqa: BLE001 - only a label
        return set()


def _llm_aside(c) -> str:
    groups = sorted(g[2:] for g in _llm_groups(c))
    if not groups:
        return ""
    return f" · grouped by an LLM: {design.and_list(groups, 3)}"


# ── worth a look: the changes with the most consequence, and the request against them ──

LOOK_MOST = 5


def _fit(runs: List[tuple], width: int) -> List[tuple]:
    """Runs cut to fit one row of `width`."""
    out, left = [], width
    for text, style in runs:
        if left <= 0:
            break
        text = canvas.cut(text, left)
        out.append((text, style))
        left -= len(text)
    return out


def _decision_detail(d: Dict[str, Any], width: int) -> List[list]:
    """Two rows under WORTH A LOOK for a picked change: why it matters; what it touches, the request, when."""
    touches = ("touches " + design.and_list(d["names"], 4)) if d["names"] else ""
    asked = f'under "{d["request"]}"' if d["request"] else ""
    second = " · ".join(x for x in (touches, asked, d.get("when", "")) if x)
    mine = [[[canvas.cut("  " + human.touched_line(t), width), "attention"]] for t in d.get("touches") or []][:1]
    return [[[canvas.cut("  " + d["why"], width), ""]]] + mine + ([[[canvas.cut("  " + second, width), "muted"]]]
                                                                if second else [])


def _worth_a_look(page: canvas.Page, found: List[Dict[str, Any]], clash: List[Dict[str, Any]], width: int,
                  base: str, most: Optional[int] = LOOK_MOST, more: str = "in Review") -> List[Dict[str, Any]]:
    """WORTH A LOOK: the changes with the most consequence first (at most `most`), each a block to pick (its detail:
    why it matters, what it touches, the request it came under; lit to its parts in the diagram); agents in each
    other's way after them. Always there, so the person learns where to look; with nothing, it says so on its
    heading. Returns the decisions shown."""
    shown = found if most is None else found[:most]
    narrow = width < 70
    if shown:
        aside = ("most consequential first · pick one" if narrow else
                 "the most consequential first · pick one: why it matters, what it touches")
    elif clash:
        aside = "agents in each other's way"
    else:
        aside = f"nothing: no change to the system's structure {base}"
    warn = any(d["status"] == "warn" for d in shown) or clash
    page.heading("▲ Worth a look" if warn else "Worth a look", canvas.cut(aside, max(8, width - 18)))
    if warn:
        page.rows[-1][0][1] = "attention"
    if shown:
        g = canvas.Grid()
        slot = 3 if any(d.get("touches") for d in shown) else 2
        for y, d in enumerate(shown):
            bid = "w:" + d["id"]
            g.put(0, y, f" {d['sign']} ", NOTE_STYLE.get(d["status"], ""), [bid])
            title = canvas.cut(d["title"], max(8, width - 4))
            g.put(3, y, title, "", [bid])
            w = 3 + len(title)
            room = width - w - 4
            if d.get("touches") and room >= 12:      # touches what the person optimises for: said on its row
                mark = "  ◇ " + canvas.cut(d["touches"][0]["text"], room)
                g.put(w, y, mark, "attention", [bid])
                w += len(mark)
            g.blocks.append({"id": bid, "x": 0, "y": y, "w": w, "h": 1})
            g.details[bid] = _decision_detail(d, width)
        page.grid(g, slot=slot)
        if len(found) > len(shown):
            page.rows.insert(len(page.rows) - slot, [[f"   and {len(found) - len(shown)} more {more}", "muted"]])
            for b in page.blocks[-len(shown):]:
                b["slot"] = [b["slot"][0] + 1, b["slot"][1]]
    _notes(page, clash, 2)
    return shown


def _intent(page: canvas.Page, it: Dict[str, Any], working: bool, width: int) -> None:
    """You asked: … · changed: A, B · not named in your request: C, on one row when it fits; else the request on one
    row and what it changed under it."""
    runs = human.intent_line(it, working)
    asked, effect = runs[:2], runs[2:]
    long = sum(len(t) for t, _s in runs)
    if long <= width:
        page.add(*[list(r) for r in runs])
        return
    page.add(*[list(r) for r in _fit(asked, width)])
    first = (effect[0][0].lstrip(" ·"), effect[0][1])
    page.add(*[list(r) for r in _fit([("  → ", "muted"), first] + list(effect[1:]), width)])


def _decision_lights(shown: List[Dict[str, Any]], model: Dict[str, Any], lights: Dict[str, List[str]]
                     ) -> Dict[str, List[str]]:
    """What each decision lights in the diagram: its parts' boxes (and the container holding one), and the flow
    between them when the diagram draws it."""
    holder = {i["id"]: n["id"] for n in model["nodes"] for i in n.get("items") or []}
    drawn = {n["id"] for n in model["nodes"]} | set(holder)
    edges = {e for lit in lights.values() for e in lit if e.startswith("e:")}
    out = {}
    for d in shown:
        lit = set()
        for p in d["parts"]:
            if p in drawn:
                lit.add("b:" + p)
            if p in holder:
                lit.add("b:" + holder[p])
        if d["entity"] in ("flow", "external") and len(d["parts"]) == 2:
            e = "e:" + d["parts"][0] + ">" + d["parts"][1]
            if e in edges:
                lit.add(e)
        out["w:" + d["id"]] = sorted(lit)
    return out


# ── what the person optimises for: their objectives, and the agents' checks of them ──

CHECK_SIGN = {"holds": ("✓", "good"), "broken": ("✗", "removed"), "checking": ("…", "attention"),
              "unclear": ("?", "attention")}


def _ago_iso(v: Any) -> str:
    """An ISO time said as "2 min ago"; "" when there is none."""
    at = objectives._unix(v)
    return activity.ago(at) if at else ""


def _verdict(cs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The check to show beside an objective: a broken one first (some agent found it broken), else the newest."""
    return next((x for x in cs if x.get("status") == "broken"), None) or (cs[0] if cs else None)


def _check_words(ch: Dict[str, Any], who: bool = True, when: bool = True) -> str:
    """"✓ holds · engine, 2 min ago"; "… checking" (the agent and the time as they fit)."""
    sign, _style = CHECK_SIGN.get(ch.get("status", ""), ("?", "attention"))
    said = f"{sign} {ch.get('status', '')}"
    tail = ", ".join(x for x in ((ch.get("agent") or "") if who else "", _ago_iso(ch.get("at")) if when else "")
                     if x)
    return said + (f" · {tail}" if tail else "")


def _asked(o: Dict[str, Any]) -> str:
    n = int(o.get("count") or 0)
    last = _ago_iso(o.get("last"))
    return (f"asked {n}×" if n else "") + (f", last {last}" if last and n else f"last {last}" if last else "")


def _objectives(page: canvas.Page, objs: List[Dict[str, Any]], checks: Dict[str, List[Dict[str, Any]]],
                width: int) -> None:
    """OBJECTIVES: what the person has been optimising for, learned from their requests, a block to pick each (its
    detail: how a change could break it, and a quote it rests on; lit to its parts; x clears it), with how often they
    asked and the agents' latest check. Nothing at all when there are none."""
    if not objs:
        return
    page.heading("Objectives", canvas.cut("what you keep asking for · pick one · x clears it" if width >= 70 else
                                          "what you keep asking for · x clears", max(8, width - 13)))
    g = canvas.Grid()
    on: Dict[str, dict] = {}
    for y, o in enumerate(objs):
        bid = "o:" + o["id"]
        ch = _verdict(checks.get(o["id"]) or [])
        style = CHECK_SIGN.get(ch["status"], ("?", "attention"))[1] if ch else "accent"
        g.put(0, y, " ◆ ", "accent", [bid])                     # its check says its own sign: ✓ holds, ✗ broken
        x = 3
        asked = _asked(o)
        said = _check_words(ch) if ch else ""
        tails = [t for t in (asked, said) if t]
        tail = "  " + " · ".join(tails) if tails else ""
        if len(tail) > max(0, width - x - 20):          # narrow: the check before how often it was asked
            tail = ("  " + said) if said else ("  " + asked if asked else "")
        if len(tail) > max(0, width - x - 16):
            tail = ""
        text = canvas.cut(o["text"], max(8, width - x - len(tail) - 1))
        g.put(x, y, text, "bold" if ch and ch["status"] == "broken" else "", [bid])
        x += len(text)
        if tail:
            if said and tail.endswith(said):
                lead = tail[:len(tail) - len(said)]
                g.put(x, y, lead, "muted", [bid])
                g.put(x + len(lead), y, said, style, [bid])
            else:
                g.put(x, y, tail, "muted", [bid])
            x += len(tail)
        g.blocks.append({"id": bid, "x": 0, "y": y, "w": x, "h": 1})
        rows: List[list] = []
        if o.get("check"):
            rows.append([["  breaks if: ", "muted"], [canvas.cut(o["check"], max(8, width - 13)), ""]])
        quote = next((q for q in o.get("evidence") or [] if q), "")
        if quote:
            rows.append([[canvas.cut(f'  "{quote}"', width), "muted"]])
        if ch and ch.get("note") and len(rows) < 2:
            rows.append([[canvas.cut(f"  {ch.get('agent', '')}: {ch['note']}", width), style]])
        g.details[bid] = rows
        on[bid] = {"x": {"clear_objective": o["id"]}}
    page.grid(g, slot=2, on=on)


def _objective_lights(objs: List[Dict[str, Any]], model: Dict[str, Any]) -> Dict[str, List[str]]:
    """What each objective lights in a diagram: the boxes of the parts it is about (and the container holding one)."""
    holder = {i["id"]: n["id"] for n in model["nodes"] for i in n.get("items") or []}
    drawn = {n["id"] for n in model["nodes"]} | set(holder)
    out = {}
    for o in objs:
        lit = set()
        for p in o.get("parts") or []:
            if p in drawn:
                lit.add("b:" + p)
            if p in holder:
                lit.add("b:" + holder[p])
        out["o:" + o["id"]] = sorted(lit)
    return out


def _by_agent(checks: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    """{agent: its latest check of each objective, broken first}."""
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for cs in checks.values():
        for ch in cs:
            out[ch.get("agent", "")].append(ch)
    return {a: _sorted_checks(cs) for a, cs in out.items()}


def _objective_order(objs: List[Dict[str, Any]], checks: Dict[str, List[Dict[str, Any]]]
                     ) -> List[Dict[str, Any]]:
    """Objectives an agent found broken first, then those being checked, then the rest, in their own order."""
    def rank(o: Dict[str, Any]) -> int:
        st = {ch.get("status") for ch in checks.get(o["id"]) or []}
        return 0 if "broken" in st else 1 if st & {"checking", "unclear"} else 2 if st else 3
    return sorted(objs, key=rank)


def _sorted_checks(cs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Broken first, then the newest."""
    return sorted(cs, key=lambda ch: (ch.get("status") != "broken", -(objectives._unix(ch.get("at")) or 0.0)))


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
    it = human.intent(c, events, lambda p: _name(p, c.after, c.before))
    _status(page, now, cfg, prompt=it is None)

    base = "this turn" if c.base == "the start of this turn" else f"since {c.base}"
    objs = objectives.current(root)
    found = [d for d in human.mark_objectives(human.decisions(c, events, code, reading), c, objs) if d["look"]]
    clash = collisions(root, events, code, cfg)
    looks = _worth_a_look(page, found, clash, width, base)
    if it:
        page.blank()   # after WORTH A LOOK's empty detail rows it adds none; after agents in each other's way, one
        _intent(page, it, now["working"], width)
    _objectives(page, objs, objectives.checks(root) if objs else {}, width)

    agent = now["part"]
    model, title, aside = _now_model(c, root, part, now, marks)
    page.heading(title, canvas.cut(aside, max(8, width - len(title) - 2)))
    g = _diagram(page, c, model, width, centre=bool(part))
    ids = {b["id"] for b in g.blocks}
    page.pick = next((b for b in ("b:" + (part or ""), "b:" + (agent or "")) if b in ids), "")
    for bid, lit in _decision_lights(looks, model, page.lights).items():
        page.lights[bid] = lit
    page.lights.update(_objective_lights(objs, model))

    rest = design.summarize(notes)
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


def _last_commit(root: Path) -> str:
    """"<short id> <subject>" of HEAD, when it has a parent (else raises): read in Python (gitfs), else git."""
    from . import gitfs
    try:
        head = gitfs.head(root)
        info = gitfs.commit(root, head) if head else None
        if info is not None:
            if not info["parents"]:
                raise ValueError("a first commit")
            return f"{head[:7]} {info['message'].splitlines()[0] if info['message'] else ''}".strip()
    except gitfs.Fallback:
        pass
    git("rev-parse", "--verify", "HEAD~1^{commit}", cwd=root)
    return git("log", "-1", "--format=%h %s", cwd=root).strip()


def _review_base(root: Path) -> Tuple[str, str]:
    """What Review compares against, and how to say it: the checkpoint; before one, the commit before the last."""
    base, since = "checkpoint", "since your checkpoint"
    if not checkpoint_sha(root):   # nothing marked as seen yet: what the last commit did, and what is not committed
        try:
            last = _last_commit(root)
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
    reading = design.Reading(c)
    notes = reading.notes()
    model = architecture.system(c, None, agent)
    ranked = human.mark_objectives(human.decisions(c, activity.events(root, 200), Code(c.cfg), reading), c,
                                   objectives.current(root))
    if ranked:
        page.heading("Every change " + since.split(" (")[0],
                     canvas.cut(f"{len(ranked)}, the most consequential first · pick one", max(8, width - 30)))
        g = canvas.Grid()
        for y, d in enumerate(ranked):
            bid = "w:" + d["id"]
            g.put(0, y, f" {d['sign']} ", NOTE_STYLE.get(d["status"], ""), [bid])
            title = canvas.cut(d["title"], max(8, width - 4))
            g.put(3, y, title, "" if d["look"] else "muted", [bid])
            g.blocks.append({"id": bid, "x": 0, "y": y, "w": 3 + len(title), "h": 1})
            g.details[bid] = _decision_detail(d, width)
        page.grid(g, slot=3 if any(d.get("touches") for d in ranked) else 2)
        for bid, lit in _decision_lights(ranked, model, page.lights).items():
            page.lights[bid] = lit
    if notes:
        page.heading("Read for design", f"{len(notes)} note{'' if len(notes) == 1 else 's'}")
        _notes(page, notes, 60)
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


def stack(bp: dict) -> Dict[str, Dict[str, int]]:
    """Technology key -> {part: files that import it}, for the whole system, from what the extraction recorded for
    each part (its "imported" names when the blueprint has them, else its "stack" of packages): no file is read again,
    so it is quick in a large repository and names exactly the parts the diagram draws."""
    names_of = bp.get("imported") or bp.get("stack") or {}
    found: Dict[str, Counter] = defaultdict(Counter)
    for part, names in names_of.items():
        if part not in (bp.get("components") or {part: None}):
            continue
        for name, n in (names or {}).items():
            key = tech_key(name) or tech_key(TOP_PACKAGE.get(name, ""))
            if key and key not in BASIC and key.split(".")[0] not in BASIC:
                found[key][part] = max(found[key][part], int(n or 1))
    return {k: dict(v) for k, v in found.items()}


def _stack_view(c, root: Path) -> Dict[str, Any]:
    """What Topics shows: each technology (key -> {part: files}), in its layer's row, most used first (the standard
    library last); the system diagram; and for each box the technology it uses."""
    cfg = c.cfg
    agent = activity.now(activity.events(root, 200), Code(cfg))["part"]
    techs = stack(c.after)
    kind = design.kinds(c.after, cfg)
    rows: Dict[str, List[str]] = defaultdict(list)
    model = architecture.system(c, None, agent)
    holder = {i["id"]: n["id"] for n in model["nodes"] for i in n.get("items") or []}
    for key in sorted(techs, key=lambda k: (k.split(".")[0] in STDLIB, -sum(techs[k].values()), TECH[k][0].lower())):
        col = Counter()
        for p, n in techs[key].items():
            col[kind.get(p, "logic")] += n
        rows[col.most_common(1)[0][0]].append(key)
    # every row its heading: the configured columns, then any layer they leave out (a config that renames or drops
    # a column must not hide the technology of the parts in it)
    headings = [tuple(kh) for kh in cfg["picture"]["columns"]]
    named = {k for k, _ in headings}
    headings += [(k, h) for k, h in config.DEFAULTS["picture"]["columns"] if k not in named]
    named |= {k for k, _ in headings}
    headings += [(k, k.capitalize()) for k in sorted(rows) if k not in named]
    order = [(k, h) for k, h in headings if rows.get(k)]
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
    objs = objectives.current(root)
    checks = objectives.checks(root) if objs else {}
    checked = _by_agent(checks)
    texts = {o["id"]: o["text"] for o in objs}
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
        if checked.get(name):                        # the objectives it checked, broken first
            row: List[list] = [["checked ", "muted"]]
            x = 8
            for ch in checked[name]:
                sign, style = CHECK_SIGN.get(ch.get("status", ""), ("?", "attention"))
                piece = f"{sign} {texts.get(ch['id'], ch['id'])}  "
                if x + len(piece) > width:
                    row.append([canvas.cut(piece, max(0, width - x)), style])
                    break
                row.append([piece, style])
                x += len(piece)
            g.details[bid].append(row)
        y += 1
    if who:
        page.grid(g, slot=3 if any(checked.get(n) for n in who) else 2)
        swimlanes(page, root, events, code, who, colour, width, checked)
    else:
        page.add(["   no agent has touched the code yet", "muted"])
    if objs:
        _agent_objectives(page, objs, checks, colour, width)
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
              colour: Dict[str, int], width: int, checked: Optional[Dict[str, List[Dict[str, Any]]]] = None
              ) -> None:
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
        for ch in (checked or {}).get(name, [])[:4]:     # the objectives it checked: ✓ held, ✗ broke, … checking
            sign, cstyle = CHECK_SIGN.get(ch.get("status", ""), ("?", "attention"))
            runs.append([" " + sign, cstyle])
        page.add(*runs)


def _agent_objectives(page: canvas.Page, objs: List[Dict[str, Any]], checks: Dict[str, List[Dict[str, Any]]],
                      colour: Dict[str, int], width: int) -> None:
    """OBJECTIVES on the Agents view: each objective, those found broken first, with every agent's latest check of
    it (the agent in its colour, the status, its note, when)."""
    some = sum(len(v) for v in checks.values())
    page.heading("Objectives", canvas.cut(f"{some} check{'' if some == 1 else 's'} by the agents, broken first"
                                          if some else "what the person optimises for · no agent has checked one yet",
                                          max(8, width - 13)))
    for o in _objective_order(objs, checks):
        cs = _sorted_checks(checks.get(o["id"]) or [])
        top = cs[0]["status"] if cs else ""
        sign, style = CHECK_SIGN.get(top, ("◆", "accent"))
        page.add([f" {sign} ", style], [canvas.cut(o["text"], width - 4), "bold"])
        for ch in cs:
            csign, cstyle = CHECK_SIGN.get(ch.get("status", ""), ("?", "attention"))
            who = ch.get("agent", "")
            agent_style = f"agent{colour[who]} bold" if who in colour else "bold"
            when = _ago_iso(ch.get("at"))
            head = [["   " + csign + " ", cstyle], [canvas.cut(who, 16), agent_style],
                    [" " + ch.get("status", ""), cstyle]]
            used = 3 + 2 + min(16, len(who)) + 1 + len(ch.get("status", ""))
            tail = (f" · {when}" if when else "") + (f" · {ch['note']}" if ch.get("note") else "")
            if tail:
                head.append([canvas.cut(tail, max(0, width - used)), "muted"])
            page.add(*head)
        if not cs:
            page.add(["     not checked yet", "muted"])


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
    """The address of the running web page with the same views, or "". With none running, one is started (in the
    background, never waited for: a later page shows its link), so the page does not depend on the session's start
    hook having run (on Windows, where hooks run through sh, it may not)."""
    try:
        from . import config, watch
        url = getattr(watch, "web_url", None)
        found = (url(root) or "") if url else ""
        if (not found and os.environ.get("SYSTEM_DIFF_WEB_START", "1") != "0"
                and (config.lenient(root).get("web") or {}).get("start", True) is not False):
            start = getattr(watch, "start_detached", None)
            if start is not None:
                start(root, wait=0)
        return found
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
    if view == "history":
        return view_history(root, width, part or "")
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
    objs = objectives.current(root)
    checks = objectives.checks(root) if objs else {}
    decs = human.mark_objectives(human.decisions(c, events, code, reading), c, objs)
    found = [d for d in decs if d["look"]]
    shown = found[:LOOK_MOST]
    model, title, aside = _now_model(c, root, part, now, marks)
    diagram = _web_diagram(c, model, title, aside, zoom, ["b:" + (part or ""), "b:" + (agent or "")])
    diagram["lights"].update(_decision_lights(shown, model, diagram["lights"]))
    clash = collisions(root, events, code, cfg)
    if shown or clash:
        more = f" · and {len(found) - LOOK_MOST} more in Review" if len(found) > LOOK_MOST else ""
        sections.append(_web_section("Worth a look", "notes", [_web_decision(d) for d in shown]
                                     + [_web_note(n) for n in clash],
                                     "the most consequential first: why each matters, what it touches" + more))
    it = human.intent(c, events, lambda p: _name(p, c.after, c.before))
    if it:
        sections.append(_web_section("Intent against effect", "lines",
                                     [{"text": "".join(t for t, _s in human.intent_line(it, now["working"])),
                                       "style": "attention" if it["unnamed"] else ""}],
                                     "what you asked, next to what changed"))
    if objs:
        sections.append(_web_section("Objectives", "notes", [_web_objective(o, checks) for o in objs],
                                     "what you keep asking for, learned from your requests · "
                                     "clear one with `system-diff objectives clear <id>`"))
        diagram["lights"].update(_objective_lights(objs, model))
    rest = design.summarize(notes)
    base = "this turn" if c.base == "the start of this turn" else f"since {c.base}"
    parts = len(design.summarize(notes))
    sections.append(_web_section("This turn", "notes", [_web_note(n) for n in rest],
                                 f"{len(notes)} change{'' if len(notes) == 1 else 's'} to the design, in {parts} "
                                 f"part{'' if parts == 1 else 's'}" if notes else base))
    taught = part or agent
    if taught and (taught in c.after["components"] or taught.startswith("x:")):
        L = _learn_parts(c, reading, taught, profile.learning())
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
    hover = _box_hover(c, model, decs, team(root, events, code), base)
    hover.update({"w:" + d["id"]: _decision_hover(d) for d in shown})
    hover.update(_objective_hover(root, objs, checks))
    return {"view": "now", "part": part or "", "title": "Now", "headline": headline,
            "status": {"working": bool(now["working"]), "doing": now["doing"] or "", "part": agent or "",
                       "prompt": now.get("prompt", "") or ""},
            "intent": it, "diagram": diagram, "sections": sections, "hover": hover}


def _web_decision(d: Dict[str, Any]) -> Dict[str, Any]:
    """A WORTH A LOOK item for the web page: a note (sign, status, text, why, ask) with its id ("w:<id>", what it
    lights in the diagram and its hover card), the parts it touches and the request it came under."""
    why = d["why"] + "".join(" · " + human.touched_line(t) for t in d.get("touches") or [])
    return {"id": "w:" + d["id"], "sign": d["sign"], "status": d["status"], "text": d["title"], "why": why,
            "ask": "", "kind": d["kind"], "impact": d["impact"], "parts": d["names"], "request": d["request"],
            "objectives": ["o:" + i for i in d.get("objectives") or []], "touches": d.get("touches") or []}


def _when(at: float) -> str:
    return (time.strftime("%H:%M", time.localtime(at)) + f" ({activity.ago(at)})") if at else ""


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _decision_hover(d: Dict[str, Any]) -> Dict[str, Any]:
    """A WORTH A LOOK item's hover card: the change, why it matters, the parts it touches, the request, when."""
    lines = ["Why it matters: " + d["why"]]
    if d["more"] and d["more"] != d["why"]:
        lines.append(_cap(d["more"]))
    if d["names"]:
        lines.append("Touches " + design.and_list(d["names"], 5))
    lines += [_cap(human.touched_line(t)) for t in d.get("touches") or []]
    if d["request"]:
        lines.append(f'Under your request "{canvas.cut(d["request"], 120)}"')
    if d["at"]:
        lines.append("Claude's last edit there: " + _when(d["at"]))
    return {"title": d["title"], "lines": lines}


KIND_WORDS = {"ui": "a front end people use", "interface": "an entry point", "logic": "logic inside the system",
              "data": "data access", "store": "an outside store of data", "external": "an outside system"}


def _box_hover(c, model: Dict[str, Any], decs: List[Dict[str, Any]], who: Dict[str, Dict[str, Any]],
               since: str) -> Dict[str, Dict[str, Any]]:
    """Each diagram box's hover card ("b:<id>", as pictures.architecture_svg's data-id): what it is, what changed in
    it (the ranked changes touching it, the most consequential first, with why the first matters), the request it
    came under, and the agents that edited or read it."""
    kind = design.kinds(c.after, c.cfg)
    llm = _llm_groups(c)
    out: Dict[str, Dict[str, Any]] = {}
    for n in model["nodes"]:
        for box in [n] + list(n.get("items") or []):
            nid = box["id"]
            inside = {i["id"] for i in n.get("items") or []} if box is n and n.get("items") else {nid}
            lines = []
            comp = c.after["components"].get(nid) or c.before["components"].get(nid)
            if nid.startswith("a:"):
                lines.append("Who uses the system" + (f": {box.get('sub')}" if box.get("sub") else ""))
            elif nid.startswith("f:"):
                lines.append(f"A module: {nid[2:]}")
            elif comp:
                lines.append(f"{KIND_WORDS.get(kind.get(nid, ''), 'a part')[0].upper()}"
                             f"{KIND_WORDS.get(kind.get(nid, ''), 'a part')[1:]} · "
                             f"{graph.files_word(comp.get('files', 0))}")
            elif nid.startswith("x:"):
                lines.append(KIND_WORDS.get(kind.get(nid, ""), "an outside system")[0].upper()
                             + KIND_WORDS.get(kind.get(nid, ""), "an outside system")[1:])
            elif box is n and n.get("items"):
                lines.append(f"A container of {len(n['items'])} parts"
                             + (" (a grouping an LLM proposed)" if nid in llm else ""))
            mine = [d for d in decs if inside & set(d["parts"])]
            if mine:
                lines.append(f"Changed {since}:")
                lines += ["  " + d["title"] for d in mine[:4]]
                if len(mine) > 4:
                    lines.append(f"  and {len(mine) - 4} more")
                top = mine[0]
                if top["look"]:
                    lines.append("Why it matters: " + top["why"])
                asked = next((d["request"] for d in mine if d["request"]), "")
                if asked:
                    lines.append(f'Under your request "{canvas.cut(asked, 100)}"')
            else:
                lines.append(f"No change to its structure {since}")
            edited = sorted(a for a, w in who.items() if inside & set(w["edits"]))
            read = sorted(a for a, w in who.items() if inside & set(w["reads"]) and a not in edited)
            if edited:
                lines.append("Edited by " + design.and_list(edited, 4))
            if read:
                lines.append("Read by " + design.and_list(read, 4))
            out["b:" + nid] = {"title": box.get("title", nid).split("  @")[0], "lines": lines}
    return out


WEB_STATUS = {"holds": "ok", "broken": "bad", "checking": "warn", "unclear": "warn"}


def _web_objective(o: Dict[str, Any], checks: Dict[str, List[Dict[str, Any]]],
                   colour: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """An objective for the web page: a note ("o:<id>": its text; why: how often asked and the agents' latest
    check), with the parts it is about, how it could break, and every agent's latest check."""
    cs = _sorted_checks(checks.get(o["id"]) or [])
    ch = _verdict(cs)
    sign = CHECK_SIGN[ch["status"]][0] if ch and ch["status"] in CHECK_SIGN else "◆"
    why = " · ".join(x for x in (_asked(o), _check_words(ch) if ch else "") if x)
    return {"id": "o:" + o["id"], "sign": sign, "status": WEB_STATUS.get(ch["status"], "info") if ch else "info",
            "text": o["text"], "why": why, "ask": "", "parts": list(o.get("parts") or []),
            "check": o.get("check", ""), "count": int(o.get("count") or 0), "last": o.get("last", ""),
            "checks": [{"agent": x.get("agent", ""), "status": x.get("status", ""), "note": x.get("note", ""),
                        "at": x.get("at", ""), "colour": (colour or {}).get(x.get("agent", ""), -1)} for x in cs]}


def _quote_times(root: Path, quotes: List[str]) -> Dict[str, float]:
    """When the person typed each quote, newest, from system-diff's log of their requests (no transcript read)."""
    try:
        reqs = objectives.from_log(root)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for q in quotes:
        hit = objectives._matches(q, reqs)
        if hit:
            out[q] = max(hit)
    return out


def _objective_hover(root: Path, objs: List[Dict[str, Any]], checks: Dict[str, List[Dict[str, Any]]]
                     ) -> Dict[str, Dict[str, Any]]:
    """Each objective's hover card ("o:<id>"): its full text, how often and when it was asked, every quote it rests
    on (with when, when the log has it), how a change could break it, the parts it is about, and every agent's
    latest check with its note."""
    out = {}
    for o in objs:
        lines = []
        asked = _asked(o)
        if asked:
            lines.append(_cap(asked) + (f" · kept since {_ago_iso(o.get('since'))}" if o.get("since") else ""))
        quotes = [q for q in o.get("evidence") or [] if q]
        times = _quote_times(root, quotes) if quotes else {}
        for q in quotes:
            lines.append(f'You said: "{q}"' + (f" ({_when(times[q])})" if q in times else ""))
        if o.get("check"):
            lines.append("Breaks if: " + o["check"])
        if o.get("parts"):
            lines.append("About " + design.and_list(o["parts"], 5))
        cs = _sorted_checks(checks.get(o["id"]) or [])
        for ch in cs:
            lines.append(f"{ch.get('agent', '')}: {_check_words(ch, who=False)}"
                         + (f": {ch['note']}" if ch.get("note") else ""))
        if not cs:
            lines.append("No agent has checked it yet")
        lines.append(f"Wrong? Clear it: system-diff objectives clear {o['id']}")
        out["o:" + o["id"]] = {"title": o["text"], "lines": lines}
    return out


def _web_review(root: Path, zoom: int) -> Dict[str, Any]:
    base, since = _review_base(root)
    c = compare(root, base, ".")
    agent = activity.now(activity.events(root, 200), Code(c.cfg))["part"]
    diagram = _web_diagram(c, architecture.system(c, None, agent), "System " + since, "click a box · zoom in",
                           zoom, ["b:" + agent] if agent else [])
    reading = design.Reading(c)
    notes = reading.notes()
    short = since.split(" (")[0]
    cfg = c.cfg
    events = activity.events(root, 200)
    model = architecture.system(c, None, agent)
    ranked = human.mark_objectives(human.decisions(c, events, Code(cfg), reading), c, objectives.current(root))
    diagram["lights"].update(_decision_lights(ranked, model, diagram["lights"]))
    hover = _box_hover(c, model, ranked, team(root, events, Code(cfg)), short)
    hover.update({"w:" + d["id"]: _decision_hover(d) for d in ranked})
    sections = [_web_section("Every change " + short, "notes", [_web_decision(d) for d in ranked],
                             f"{len(ranked)}, the most consequential first" if ranked
                             else f"no change to the system's structure {short}")]
    if notes:
        sections.append(_web_section("Read for design", "notes", [_web_note(n) for n in notes],
                                     f"{len(notes)} note{'' if len(notes) == 1 else 's'}"))
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
            "diagram": diagram, "sections": sections, "hover": hover}


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
    objs = objectives.current(root)
    checks = objectives.checks(root) if objs else {}
    checked = _by_agent(checks)
    texts = {o["id"]: o["text"] for o in objs}
    for lane in lanes:                                # the objectives each agent checked, beside its lane
        lane["checks"] = [{"id": "o:" + ch["id"], "text": texts.get(ch["id"], ch["id"]),
                           "status": ch.get("status", ""), "note": ch.get("note", ""), "at": ch.get("at", "")}
                          for ch in checked.get(lane["name"], [])]
        if lane["checks"]:
            lane["doing"] = ((lane["doing"] + " · ") if lane["doing"] else "") + "checked " + ", ".join(
                f"{CHECK_SIGN.get(x['status'], ('?', ''))[0]} {x['text']}" for x in lane["checks"][:3])
    sections = [_web_section("The team", "lanes", lanes, f"plan: {task}" if task else
                             "no plan yet: Claude makes one with plan_agents")]
    if objs:
        some = sum(len(v) for v in checks.values())
        sections.append(_web_section("Objectives", "notes",
                                     [_web_objective(o, checks, colour) for o in _objective_order(objs, checks)],
                                     f"{some} check{'' if some == 1 else 's'} by the agents, broken first" if some
                                     else "what the person optimises for · no agent has checked one yet"))
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
    hover = _box_hover(c, model, human.decisions(c, events, code), who, "this turn")
    hover.update(_agent_hover(root, cfg, who, L, st, checked, texts))
    hover.update(_objective_hover(root, objs, checks))
    if objs:
        diagram["lights"].update(_objective_lights(objs, model))
    return {"view": "agents", "part": "", "title": "Agents",
            "headline": task or "no plan yet: Claude makes one with plan_agents", "diagram": diagram,
            "sections": sections, "hover": hover}


def _agent_hover(root: Path, cfg: Dict[str, Any], who: Dict[str, Dict[str, Any]], L: Dict[str, Any],
                 st: Dict[str, Any], checked: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                 texts: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """Each agent's hover card ("ag:<name>", as the web page's lanes): its area, the blocks it owns, what it edited
    and read this turn, its last action and when, the objectives it checked, and the latest it told the others and
    was told."""
    msgs = agents.messages(root)
    out = {}
    for name, a in who.items():
        label = lambda ps: design.and_list([activity.label(cfg, p) for p in ps], 5)   # noqa: E731
        lines = [f"Area: {a['area']}" if a["area"] else ("The main session" if name == agents.MAIN else "No area")]
        lines.append("Owns " + label(a["blocks"]) if a["blocks"] else "Owns no blocks")
        lines.append("Edited " + label(a["edits"]) + " this turn" if a["edits"] else "Edited nothing this turn")
        read = [p for p in a["reads"] if p not in a["edits"]]
        if read:
            lines.append("Read " + label(read) + " this turn")
        me = (L.get("lanes") or {}).get(name) or {}
        if me.get("doing"):
            lines.append("Now: " + me["doing"].lstrip("✎◎ "))
        elif a["at"]:
            lines.append("Last acted " + _when(a["at"]))
        else:
            lines.append("Not started")
        for ch in (checked or {}).get(name, []):
            lines.append(f"Checked \"{(texts or {}).get(ch['id'], ch['id'])}\": {_check_words(ch, who=False)}"
                         + (f" ({ch['note']})" if ch.get("note") else ""))
        topics = set((st.get("agents") or {}).get(name, {}).get("topics") or []) | set(a["blocks"]) | {
            "all", f"agent:{name}"}
        sent = [m for m in msgs if m["from"] == name][-2:]
        got = [m for m in msgs if m["from"] != name and m["topic"] in topics][-2:]
        for m in sent[::-1]:
            lines.append(f"Told {m['topic']} ({time.strftime('%H:%M', time.localtime(m['at']))}): "
                         + canvas.cut(m["text"], 100))
        for m in got[::-1]:
            lines.append(f"Heard from {m['from']} ({time.strftime('%H:%M', time.localtime(m['at']))}): "
                         + canvas.cut(m["text"], 100))
        out["ag:" + name] = {"title": name, "lines": lines}
    return out


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
    elif view == "history":
        out = _web_history(root, part or "", zoom)
    else:
        out = _web_now(root, part or "", zoom)
    out["hover"] = _rendered(out)
    out["web"] = web_url(root)
    out["send"] = []
    return out


def _rendered(out: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The view's hover cards, only for the ids the web page renders: the diagram's boxes as its SVG names them
    (data-id), the WORTH A LOOK and ranked items and the agents' lanes as its sections list them."""
    hover = out.get("hover") or {}
    svg = (out.get("diagram") or {}).get("svg") or ""
    boxes = {html.unescape(i) for i in re.findall(r'data-id="([^"]+)"', svg)} if svg else None
    shown = set()
    for sec in out.get("sections") or []:
        for item in sec.get("items") or []:
            if isinstance(item, dict) and item.get("id"):
                shown.add(item["id"])
            if sec.get("kind") == "lanes" and isinstance(item, dict):
                shown.add("ag:" + item.get("name", ""))
    keep = {}
    for k, v in hover.items():
        if k.startswith("b:") and (boxes is None or k in boxes):
            keep[k] = v
        elif k in shown:
            keep[k] = v
    return keep


# ── history: how the system evolved, as what was asked and what the system got (history.py) ──

HISTORY_MOST = 20   # entries the pane lists; the web page and `system-diff history` have them all


def _history_now(root: Path):
    """The system as it is (since the checkpoint), and its diagram's model: what a block stands for."""
    c = compare(root, "checkpoint", ".")
    return c, architecture.system(c, None, None)


def _block_name(block: str, c, model: Dict[str, Any]) -> str:
    for n in model["nodes"]:
        for box in [n] + list(n.get("items") or []):
            if box["id"] == block:
                return str(box.get("title", block)).split("  @")[0]
    return _name(block, c.after, c.before)


def _history_decs(changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """An entry's changes as decisions the Review machinery draws (no request or edit time of their own)."""
    return [{"more": "", "request": "", "at": 0, "when": "", "touches": [], "objectives": [], "look": True, **d}
            for d in changes]


def _history_entry_grid(e: Dict[str, Any], width: int) -> List[list]:
    """A picked entry's detail: its changes with why each matters, then where it is from."""
    rows: List[list] = []
    for d in e["changes"][:2]:
        rows.append([[f" {d['sign']} ", NOTE_STYLE.get(d["status"], "")], [canvas.cut(d["title"], width - 4), ""]])
        rows.append([[canvas.cut("   " + d["why"], width), "muted"]])
    tail = []
    if len(e["changes"]) > 2:
        tail.append(f"and {len(e['changes']) - 2} more")
    if e.get("commit"):
        tail.append(f"commit {e['commit'][:7]}")
    if e.get("files") is not None:
        tail.append(f"{e['files']} file{'' if e['files'] == 1 else 's'}")
    tail.append("↵ shows it on the diagram")
    rows.append([[canvas.cut("   " + " · ".join(tail), width), "muted"]])
    return rows


def view_history(root: Path, width: int, part: str = "") -> Dict[str, Any]:
    """How the system evolved, newest first: each entry what was asked (or the commit) and what the system got, a
    block to pick (its changes, with why each matters) and to open (↵: that change on the diagram). With a block
    (`part`), only the entries that changed it, and only their changes to it; "<block>#<entry>" opens one."""
    from . import history
    block, eid = history.split_part(part)
    if eid:
        return _history_entry_page(root, width, block, eid, part)
    entries = history.load(root)
    page = canvas.Page(width)
    narrow = width < 70
    if block:
        c, model = _history_now(root)
        entries = history.for_part(entries, history.members(block, model))
        name = _block_name(block, c, model)
        page.heading(f"History of {name}", canvas.cut("only what changed it, newest first · ↵ one · b back"
                                                      if narrow else "only what changed it, newest first · "
                                                      "↵ shows one on the diagram · b back", max(8, width - 14 - len(name))))
    else:
        page.heading("History", canvas.cut("newest first · ↵ shows one" if narrow else
                                           "how the system changed, newest first · ↵ shows one on the diagram · "
                                           "h on a box: its own history", max(8, width - 10)))
    if not entries:
        page.add(["   nothing recorded yet" + (" for this block" if block else "") + ": each change to the "
                  "structure is kept as it happens, and the commits are read in the background", "muted"])
        return page.json(view="history", part=part)
    g = canvas.Grid()
    shown = entries[:HISTORY_MOST]
    for i, e in enumerate(shown):
        y, bid = 2 * i, "hy:" + e["id"]
        lead = f" {history.when(e['at'])} · "
        g.put(0, y, lead, "muted", [bid])
        asked = history.asked_line(e)
        style = "bold" if e.get("request") else ""
        if asked.startswith("You asked: "):
            g.put(len(lead), y, "You asked: ", "muted", [bid])
            g.put(len(lead) + 11, y, canvas.cut(asked[11:], max(8, width - len(lead) - 12)), style, [bid])
        else:
            g.put(len(lead), y, canvas.cut(asked, max(8, width - len(lead) - 1)), "muted" if not e.get("commit")
                  else "", [bid])
        arrow = "   → " if narrow else "   → it got: "
        g.put(0, y + 1, arrow, "muted", [bid])
        g.put(len(arrow), y + 1, canvas.cut(e["got"], max(8, width - len(arrow) - 1)),
              "attention" if any(d["status"] == "warn" for d in e["changes"]) else "changed", [bid])
        g.blocks.append({"id": bid, "x": 0, "y": y, "w": width - 1, "h": 2})
        g.details[bid] = _history_entry_grid(e, width)
    page.grid(g, slot=5, on={"hy:" + e["id"]: {"enter": {"entry": e["id"]}} for e in shown})
    if len(entries) > len(shown):
        page.rows.insert(len(page.rows) - 5, [[f"   and {len(entries) - len(shown)} older: on the web page, or "
                                               "`system-diff history`", "muted"]])
        for b in page.blocks:
            b["slot"] = [b["slot"][0] + 1, b["slot"][1]]
    page.pick = "hy:" + shown[0]["id"]
    return page.json(view="history", part=part)


def _history_entry_page(root: Path, width: int, block: str, eid: str, part: str) -> Dict[str, Any]:
    """One entry: what was asked and what the system got, the system diagram after it with what it changed
    coloured (its base state against its head state, drawn as Review draws), and each change with why it matters."""
    from . import history
    page = canvas.Page(width)
    e = history.find(root, eid)
    if e is None:
        page.add(["This change is no longer in the history. b goes back.", "muted"])
        return page.json(view="history", part=part)
    cfg = config.lenient(root)
    c = history.entry_comparison(root, e, cfg)
    model = architecture.system(c, None, None) if c is not None else None
    name = ""
    if block:
        mine = history.members(block, model) | ({block} if model is None else set())
        e = next(iter(history.for_part([e], mine)), {**e, "changes": []})
        name = _block_name(block, c, model) if c is not None and model is not None else block
    page.heading("Change" + (f" to {name}" if name else ""), canvas.cut(
        history.when(e["at"]) + " · b back", max(8, width - 12 - len(name))))
    page.para(history.asked_line(e), "bold")
    page.para("→ it got: " + (e.get("got") or history.got_line(e["changes"])), "changed", indent=0)
    tail = []
    if e.get("commit"):
        tail.append(f"commit {e['commit'][:7]}" + (f": {e['message']}" if e.get("message") and e.get("request")
                                                    else ""))
    if e.get("files") is not None:
        tail.append(f"{e['files']} file{'' if e['files'] == 1 else 's'} changed")
    if tail:
        page.add([canvas.cut(" · ".join(tail), width), "muted"])
    decs = _history_decs(e["changes"])
    if c is not None and model is not None:
        page.heading("The system after it", canvas.cut("what it changed is coloured · ↵ on a box: its history",
                                                       max(8, width - 22)))
        g = _diagram(page, c, model, width)
        ids = {b["id"] for b in g.blocks}
        for b in page.blocks:
            if b["id"] in ids and (b.get("enter") or {}).get("zoom"):
                b["enter"] = {"history": b["enter"]["zoom"]}
    else:
        page.add(["   the diagram at that point is no longer kept: what it changed is below", "muted"])
    if decs:
        page.heading("What it changed", canvas.cut(f"{len(decs)}, the most consequential first · pick one",
                                                   max(8, width - 18)))
        g = canvas.Grid()
        for y, d in enumerate(decs):
            bid = "w:" + d["id"]
            g.put(0, y, f" {d['sign']} ", NOTE_STYLE.get(d["status"], ""), [bid])
            title = canvas.cut(d["title"], max(8, width - 4))
            g.put(3, y, title, "", [bid])
            g.blocks.append({"id": bid, "x": 0, "y": y, "w": 3 + len(title), "h": 1})
            g.details[bid] = _decision_detail(d, width)
        page.grid(g, slot=2)
        if model is not None:
            for bid, lit in _decision_lights(decs, model, page.lights).items():
                page.lights[bid] = lit
        page.pick = "w:" + decs[0]["id"]
    return page.json(view="history", part=part)


def _history_lights(changes: List[Dict[str, Any]], model: Dict[str, Any], lights: Dict[str, List[str]]
                    ) -> List[str]:
    lit: set = set()
    for v in _decision_lights(_history_decs(changes), model, lights).values():
        lit |= set(v)
    return sorted(lit)


def _history_hover(e: Dict[str, Any]) -> Dict[str, Any]:
    """An entry's hover card: the whole request, every change with why it matters, the commit, the files."""
    from . import history
    title = (f"You asked: “{e['request_full']}”" if e.get("request_full") else history.asked_line(e))
    lines = ["It got: " + (e.get("got") or history.got_line(e["changes"]))]
    lines += [f"{d['sign']} {d['title']}: {d['why']}" for d in e["changes"]]
    if e.get("commit"):
        lines.append(f"Commit {e['commit'][:7]}" + (f": {e['message']}" if e.get("message") else ""))
    if e.get("files") is not None:
        lines.append(f"{e['files']} file{'' if e['files'] == 1 else 's'} changed")
    span = time.strftime("%d %b %Y %H:%M", time.localtime(e["at"]))
    lines.append(f"{span} ({history.when(e['at'])})")
    return {"title": title, "lines": lines}


def _web_history(root: Path, part: str, zoom: int) -> Dict[str, Any]:
    """The History view for the web page: the timeline (section kind "history": {id, when, at, asked, request, got,
    source, commit, files, changes [{sign, status, text, why}], parts}) over the system diagram as it is now (an
    entry lights the boxes it changed; a click on a box shows that box's history); with "<block>#<entry>", that
    entry: the system after it, what it changed coloured, and each change."""
    from . import history
    block, eid = history.split_part(part)
    if eid:
        e = history.find(root, eid)
        if e is None:
            return {"view": "history", "part": part, "title": "History",
                    "headline": "This change is no longer in the history", "diagram": {}, "sections": [], "hover": {}}
        cfg = config.lenient(root)
        c = history.entry_comparison(root, e, cfg)
        model = architecture.system(c, None, None) if c is not None else None
        if block:
            e = next(iter(history.for_part([e], history.members(block, model) | {block})), {**e, "changes": []})
        decs = _history_decs(e["changes"])
        diagram: Dict[str, Any] = {}
        hover: Dict[str, Any] = {}
        if c is not None and model is not None:
            diagram = _web_diagram(c, model, "The system after it", "what it changed is coloured · click a box: "
                                   "its history", zoom, [])
            diagram["lights"].update(_decision_lights(decs, model, diagram["lights"]))
            hover = _box_hover(c, model, decs, {}, "in this change")
        hover.update({"w:" + d["id"]: _decision_hover(d) for d in decs})
        about = [{"text": history.asked_line({**e, "request": e.get("request_full") or e.get("request")}),
                  "style": ""}, {"text": "→ it got: " + e["got"], "style": "changed"}]
        if e.get("commit"):
            about.append({"text": f"commit {e['commit'][:7]}" + (f": {e['message']}" if e.get("message") else ""),
                          "style": "muted"})
        if e.get("files") is not None:
            about.append({"text": f"{e['files']} file{'' if e['files'] == 1 else 's'} changed", "style": "muted"})
        sections = [_web_section("This change", "lines", about, history.when(e["at"])),
                    _web_section("What it changed", "notes", [_web_decision(d) for d in decs],
                                 f"{len(decs)}, the most consequential first")]
        return {"view": "history", "part": part, "title": "History", "headline": history.plain(e),
                "diagram": diagram, "sections": sections, "hover": hover}
    c, model = _history_now(root)
    entries = history.load(root)
    name = ""
    if block:
        entries = history.for_part(entries, history.members(block, model))
        name = _block_name(block, c, model)
    diagram = _web_diagram(c, model, "The system now", "hover an entry to light what it changed · click a box: "
                           "its history", zoom, ["b:" + block] if block else [])
    items, hover = [], {}
    for e in entries:
        hid = "hy:" + e["id"]
        lit = _history_lights(e["changes"], model, diagram["lights"])
        diagram["lights"][hid] = lit
        items.append({"id": hid, "when": history.when(e["at"]), "at": e["at"], "asked": history.asked_line(e),
                      "request": e.get("request") or "", "got": e["got"], "source": e.get("source", ""),
                      "commit": (e.get("commit") or "")[:7], "files": e.get("files"), "parts": lit,
                      "changes": [{"sign": d["sign"], "status": d["status"], "text": d["title"], "why": d["why"]}
                                  for d in e["changes"]]})
        hover[hid] = _history_hover(e)
    decs = [d for e in entries for d in _history_decs(e["changes"])]
    hover.update(_box_hover(c, model, decs, {}, "in its history"))
    title = f"History of {name}" if name else "History"
    aside = (f"{len(entries)} change{'' if len(entries) == 1 else 's'}, newest first · click one to see it on the "
             "diagram" if entries else "nothing recorded yet: each change is kept as it happens, and the commits "
             "are read in the background")
    return {"view": "history", "part": part, "title": "History",
            "headline": (f"How {name} evolved" if name else "How the system evolved"),
            "diagram": diagram, "sections": [_web_section(title, "history", items, aside)], "hover": hover}
