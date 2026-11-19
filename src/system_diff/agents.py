"""Many agents on one system: a PLAN that splits a task by the system's blocks, one agent per area, each with its brief;
CLAIMS, so agents keep to their own blocks; and a BUS, an async publish/subscribe channel whose topics are the system's
blocks: an agent subscribes to the blocks its own depend on, every structural change to a block is published on that
block's topic (by the hooks, as it happens), agents publish their own notes too, and each agent's unread messages
reach it with its next tool result of any kind, without waiting on anyone.

Who is who: Claude Code names each subagent by an id (`agent_id` in its hook events; the main conversation has none,
"main" here). A plan names agents by area ("billing"); an id is tied to a name when it first edits a block that name
claimed, or when it claims or publishes under that name.

Kept in .git/system-diff/ (never committed): agents.json (plan, claims, names, subscriptions, read cursors) and
bus.jsonl (the messages, newest last).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import activity, design
from .sources import git_dir

STATE, BUS = "agents.json", "bus.jsonl"
MAIN = "main"
SYSTEM = "system-diff"   # the sender of what system-diff itself publishes
KEEP = 2000        # messages kept on the bus
MOST_AGENTS = 4    # a plan's agents at most, unless asked for more


_GOOD: Dict[str, Dict[str, Any]] = {}   # the team state last read whole, by file: what a failed read falls back to


def _empty() -> Dict[str, Any]:
    return {"plan": {}, "claims": {}, "agents": {}, "ids": {}, "warned": {}}


def _load(root: Path) -> Dict[str, Any]:
    """The team's state. A read that fails while another process moves a new copy in (Windows refuses a file being
    replaced) is tried again, then answered with the copy last read whole: never an empty team, which would draw no
    plan and, saved back, would lose it."""
    f = git_dir(root) / STATE
    for attempt in range(4):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                got = {"plan": data.get("plan") or {}, "claims": data.get("claims") or {},
                       "agents": data.get("agents") or {}, "ids": data.get("ids") or {},
                       "warned": data.get("warned") or {}}
                _GOOD[str(f)] = got
                return json.loads(json.dumps(got))
            break
        except FileNotFoundError:
            _GOOD.pop(str(f), None)
            return _empty()
        except (OSError, ValueError):
            time.sleep(0.02 * (attempt + 1))
    last = _GOOD.get(str(f))
    return json.loads(json.dumps(last)) if last is not None else _empty()


def _save(root: Path, data: Dict[str, Any]) -> None:
    f = git_dir(root) / STATE
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    for attempt in range(4):   # Windows: the move fails while a reader has the file open; a moment later it works
        try:
            activity._replace(f, text)
            _GOOD[str(f)] = json.loads(text)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


def state(root: Path) -> Dict[str, Any]:
    return _load(root)


def _agent(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    return data["agents"].setdefault(name, {"topics": [], "read": 0, "blocks": [], "brief": ""})


def name_of(root: Path, agent_id: str) -> str:
    """The name an agent goes by: its plan name once tied to one, else "main" or a short form of its id."""
    if not agent_id or agent_id == MAIN:
        return MAIN
    return _load(root)["ids"].get(agent_id) or "agent-" + agent_id[:6]


def titles(root: Path) -> Dict[str, str]:
    """How the page shows each agent id: its plan name once tied, else what it does (readable)."""
    out = {g["id"]: g["title"] for g in adhoc(root, time.time() - 7 * 86400, 200)}
    out.update(_load(root)["ids"])
    return out


def title_of(root: Path, agent_id: str, known: Optional[Dict[str, str]] = None) -> str:
    """"You (the main session)", an agent's plan name, else what it does; never agent-<id> when anything says more."""
    if not agent_id or agent_id == MAIN:
        return "You (the main session)"
    return (known if known is not None else titles(root)).get(agent_id) or "agent " + agent_id[:6]


def tie(root: Path, agent_id: str, name: str) -> None:
    if agent_id and agent_id != MAIN and name:
        data = _load(root)
        if data["ids"].get(agent_id) != name:
            data["ids"][agent_id] = name
            short = data["agents"].get("agent-" + agent_id[:6])   # what it read before it had a name stays read
            if short:
                a = _agent(data, name)
                a["read"] = max(a["read"], short["read"])
            _save(root, data)


_BRIEF_NAME = re.compile(r'with agent \\?"([^"\\]+)\\?"')


def match_planned(root: Path, agent_id: str, info: Dict[str, Any]) -> str:
    """The planned agent a subagent is, from what is known as it starts (its prompt, the plan's brief: "You are the
    <Area> agent ... with agent "<name>""; the Task call's description naming the agent or its area): tied at once,
    so the board never draws it as an unknown agent first. "" when it is no planned agent (or one already tied)."""
    if not agent_id or agent_id == MAIN:
        return ""
    data = _load(root)
    if agent_id in data["ids"]:
        return data["ids"][agent_id]
    planned = [g for g in (data.get("plan") or {}).get("agents") or [] if isinstance(g, dict) and g.get("name")]
    if not planned:
        return ""
    taken = set(data["ids"].values())
    free = [g for g in planned if g["name"] not in taken]
    prompt, desc = str(info.get("prompt") or ""), str(info.get("description") or "").lower()
    hit = ""
    m = _BRIEF_NAME.search(prompt)
    if m and any(g["name"] == m.group(1) for g in planned):
        hit = m.group(1)
    if not hit:
        for g in free:
            area = str(g.get("area") or "")
            if area and re.search(r"\bYou are the " + re.escape(area) + r" agent\b", prompt):
                hit = g["name"]
                break
    if not hit and desc:
        named = [g["name"] for g in free if g["name"].lower() in desc
                 or (g.get("area") and str(g["area"]).lower() in desc)]
        if len(named) == 1:
            hit = named[0]
    if hit:
        tie(root, agent_id, hit)
    return hit


# ── claims ────────────────────────────────────────────────────────────────────

def claim(root: Path, agent: str, blocks: Iterable[str]) -> Dict[str, str]:
    """`agent` takes `blocks`; returns the blocks another agent had, by that agent."""
    data = _load(root)
    taken = {}
    for b in blocks:
        had = data["claims"].get(b)
        if had and had != agent:
            taken[b] = had
        data["claims"][b] = agent
        a = _agent(data, agent)
        if b not in a["blocks"]:
            a["blocks"].append(b)
    _save(root, data)
    return taken


def owner(root: Path, block: str) -> Optional[str]:
    return _load(root)["claims"].get(block)


# ── the bus ───────────────────────────────────────────────────────────────────

def publish(root: Path, topic: str, text: str, sender: str = MAIN, why: str = "", kind: str = "note") -> Dict[str, Any]:
    """A message on a topic (a block's id, "all", or "agent:<name>" for one agent)."""
    path = git_dir(root) / BUS
    try:
        last = int(json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["n"])
    except (OSError, ValueError, IndexError, KeyError):
        last = 0
    msg = {"n": last + 1, "at": round(time.time(), 3), "topic": topic, "from": sender, "kind": kind,
           "text": text, "why": why}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    if msg["n"] % 200 == 0:   # trimmed now and then
        lines = path.read_text(encoding="utf-8").splitlines()[-KEEP:]
        activity._replace(path, "\n".join(lines) + "\n")
    return msg


def messages(root: Path, after: int = 0) -> List[Dict[str, Any]]:
    try:
        lines = (git_dir(root) / BUS).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("n", 0) > after:
            out.append(m)
    return out


def subscribe(root: Path, agent: str, topics: Iterable[str]) -> List[str]:
    data = _load(root)
    a = _agent(data, agent)
    for t in topics:
        if t and t not in a["topics"]:
            a["topics"].append(t)
    _save(root, data)
    return a["topics"]


def inbox(root: Path, agent: str, mark: bool = True) -> List[Dict[str, Any]]:
    """`agent`'s unread messages: on the topics it subscribes to, "all", and its own; never its own sends."""
    data = _load(root)
    a = _agent(data, agent)
    want = set(a["topics"]) | {"all", f"agent:{agent}"}
    new = [m for m in messages(root, a["read"]) if m["topic"] in want and m["from"] != agent]
    if mark:
        latest = messages(root, a["read"])
        if latest:
            a["read"] = latest[-1]["n"]
            _save(root, data)
    return new


def pending(root: Path, name: str) -> int:
    """How many unread messages `name` has that were published after the current plan was made (all its unread when
    there is no plan), marking nothing read; 0 for an agent that is neither in the current plan nor "main"."""
    p = _load(root)["plan"] or {}
    if name != MAIN and p and name not in {g.get("name") for g in p.get("agents") or []}:
        return 0
    if p.get("after") is not None:   # by message number: two messages can share a millisecond
        after = int(p["after"])
        return sum(1 for m in inbox(root, name, mark=False) if int(m.get("n") or 0) > after)
    since = float(p.get("at") or 0)   # a plan from before message numbers were kept
    return sum(1 for m in inbox(root, name, mark=False) if float(m.get("at") or 0) > since)


def as_context(msgs: List[Dict[str, Any]], most: int = 8) -> str:
    """Messages as a few lines an agent reads with its next tool result."""
    if not msgs:
        return ""
    lines = [f"[system-diff bus] {len(msgs)} new message{'' if len(msgs) == 1 else 's'} from other agents:"]
    for m in msgs[-most:]:
        why = f" ({m['why']})" if m.get("why") else ""
        lines.append(f"- {m['from']} on {m['topic']}: {m['text']}{why}")
    lines.append("Adjust your work if it depends on these; publish what others need to know with the publish tool.")
    return "\n".join(lines)


# ── what the hooks do for each agent ─────────────────────────────────────────

def _for_context(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The messages worth putting in an agent's context: spread warnings reach the agent they are about directly; on
    the bus they are a record for the inbox tool."""
    return [m for m in msgs if not (m.get("from") == SYSTEM and m.get("kind") == "warning")]


def deliver(root: Path, agent_id: str) -> str:
    """The unread messages for the agent with this Claude Code id ("" or "main": the main conversation), as context
    text, marked read; "" without writing anything when there is no bus yet."""
    if not (git_dir(root) / BUS).exists():
        return ""
    return as_context(_for_context(inbox(root, name_of(root, agent_id))))


def after_edit(root: Path, agent_id: str, part: Optional[str], notes: List[Dict[str, Any]], bp: dict) -> Dict[str, str]:
    """After an agent's edit: tie it to a plan name if it edited a claimed block nobody else is tied to, subscribe it
    to its block and the blocks that block depends on, publish what the edit changed in the structure on the topics
    of the blocks it changed, and return {"context": unread messages for the agent, "warn": a claim it stepped on}."""
    data = _load(root)
    name = data["ids"].get(agent_id) if agent_id and agent_id != MAIN else MAIN
    claimed = data["claims"].get(part or "")
    if not name and claimed and claimed not in data["ids"].values():
        tie(root, agent_id, claimed)
        name = claimed
    name = name or ("agent-" + agent_id[:6])
    if part:
        deps = sorted(design.depends(bp).get(part, ()))
        subscribe(root, name, [part] + deps)
    for n in design.summarize(notes):
        topic = (n.get("parts") or [part or "all"])[0] or "all"
        publish(root, topic, n["text"], sender=name, why=n.get("why", ""), kind="change")
    warn = ""
    if part and claimed and claimed != name:
        warn = f"{part} is claimed by agent {claimed}; {name} just edited it"
        publish(root, f"agent:{claimed}", f"{name} edited your block {part}", sender=name, kind="warning")
    from . import config
    wide = spread(root, name, agent_id, bp, config.lenient(root))
    unread = _for_context(inbox(root, name))
    context = "\n".join(t for t in (as_context(unread), wide) if t)
    warn = "; ".join(t for t in (warn, wide) if t)
    return {"context": context, "warn": warn, "name": name}


def areas_of(root: Path, bp: dict, cfg: Dict[str, Any]) -> Dict[str, str]:
    """Each block's area: the agent the saved plan gave it to, when there is a plan; else its container, or the
    block itself when it is in none."""
    agents = (_load(root)["plan"] or {}).get("agents") or []
    if agents:
        return {b: g["name"] for g in agents for b in g.get("blocks") or []}
    from .architecture import groups_of
    group = groups_of(bp, cfg, design.kinds(bp, cfg))
    return {cid: (group[cid][2:] if group.get(cid, "").startswith("g:") else cid) for cid in bp["components"]}


def spread(root: Path, name: str, agent_id: str, bp: dict, cfg: Dict[str, Any]) -> str:
    """When one agent has edited blocks in two or more areas this turn: a message telling it to plan a team and hand
    the rest on, once per agent per turn (also published to all); else ""."""
    from .extract import Code
    code = Code(cfg)
    me = "" if agent_id in ("", None, MAIN) else agent_id
    start, parts = 0.0, []
    for e in activity.events(root, 400):
        if e.get("event") == activity.PROMPT and not activity.is_system(e.get("text", "")):
            start, parts = e["at"], []
        elif e.get("event") == activity.EDIT and (e.get("agent") or "") == me and e.get("path"):
            part = activity.part_of(e["path"], code)
            if part and part not in parts:
                parts.append(part)
    area = areas_of(root, bp, cfg)
    seen: List[str] = []
    for p in parts:
        a = area.get(p, p)
        if a not in seen:
            seen.append(a)
    if len(seen) < 2:
        return ""
    data = _load(root)
    turn = str(start)
    told = data["warned"].get(turn) or []
    if name in told:
        return ""
    data["warned"] = {turn: told + [name]}   # only the latest turn is kept
    _save(root, data)
    text = (f"{name} has edited blocks in {len(seen)} areas this turn ({', '.join(seen)}). system-diff runs work "
            "that spans areas as a team: call plan_agents with what is left, launch its agents in parallel, and "
            "keep to one area yourself.")
    publish(root, "all", text, sender=SYSTEM, kind="warning")
    return text


def who_worked(events: Iterable[dict], code) -> Dict[str, Dict[str, Any]]:
    """Each agent seen in the activity log since the last prompt: the parts it edited and read, when it last acted."""
    out: Dict[str, Dict[str, Any]] = {}
    for e in events:
        if e.get("event") == activity.PROMPT and not activity.is_system(e.get("text", "")):
            out = {}
            continue
        if e.get("event") not in (activity.EDIT, activity.READ) or not e.get("path"):
            continue
        a = out.setdefault(e.get("agent") or MAIN, {"edits": [], "reads": [], "at": 0.0})
        part = activity.part_of(e["path"], code)
        if part:
            key = "edits" if e["event"] == activity.EDIT else "reads"
            if part not in a[key]:
                a[key].append(part)
        a["at"] = max(a["at"], e["at"])
    return out


# ── the plan ──────────────────────────────────────────────────────────────────

WORD = re.compile(r"[a-z0-9]+")


def _areas(c) -> List[Dict[str, Any]]:
    """Where work can be split: each container (its parts together), else each part on its own."""
    from .architecture import groups_of
    bp, cfg = c.after, c.cfg
    kind = design.kinds(bp, cfg)
    group = groups_of(bp, cfg, kind)
    areas: Dict[str, Dict[str, Any]] = {}
    for cid, comp in bp["components"].items():
        if comp.get("quiet"):
            continue
        key = group.get(cid, cid)
        a = areas.setdefault(key, {"id": key, "title": key[2:] if key.startswith("g:") else comp["label"],
                                   "blocks": []})
        a["blocks"].append(cid)
    return list(areas.values())


def untangle(waits: Dict[str, set]) -> Dict[str, set]:
    """The waits without circles: agents that wait for each other, directly or round a longer circle, cannot run one
    after the other, so they run together (on the bus) and their waits for each other go."""
    names = list(waits)
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on: set = set()
    group: Dict[str, int] = {}
    counter = [0, 0]

    def visit(v: str) -> None:   # Tarjan's strongly connected components
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in waits.get(v, ()):
            if w not in waits:
                continue
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            while True:
                w = stack.pop()
                on.discard(w)
                group[w] = counter[1]
                if w == v:
                    break
            counter[1] += 1
    for v in names:
        if v not in index:
            visit(v)
    return {v: {w for w in waits[v] if w in waits and group[w] != group[v]} for v in names}


def stages_of(waits: Dict[str, set]) -> Dict[str, int]:
    """Each agent's stage: 1 when it waits for nobody, else one after the latest of those it waits for (circles
    untangled first: their agents share a stage)."""
    clean = untangle(waits)
    stage: Dict[str, int] = {}

    def of(v: str, seen: frozenset = frozenset()) -> int:
        if v in stage:
            return stage[v]
        stage[v] = 1 + max([of(w, seen | {v}) for w in clean[v] if w not in seen] or [0])
        return stage[v]
    for v in clean:
        of(v)
    return stage


def plan(root: Path, c, task: str, most: int = MOST_AGENTS, focus: Iterable[str] = (), order: str = "parallel",
         after: Optional[Dict[str, List[str]]] = None, preset: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Split `task` across agents by the system's blocks: the areas (containers, else parts) the task names or that
    `focus` gives, one agent each (at most `most`, the rest joining the closest), each with the blocks it claims, the
    blocks it depends on (its subscriptions) and a brief; and how to run them. Saved, and the claims made.

    The order they run in: "parallel" (all at once, coordinating on the bus), or "staged" (an agent whose blocks use
    another agent's starts after that one has finished); `after` adds waits by name ({"web": ["api"]}). Agents that
    wait for each other in a circle run together. `preset`: the agents of a saved workflow (workflows.py) instead of
    the areas found: [{"name", "area", "blocks", "waits_for"}]."""
    bp = c.after
    words = set(WORD.findall(task.lower()))
    deps = design.depends(bp)
    scored = []
    for a in _areas(c):
        text = " ".join([a["title"]] + [bp["components"][b]["label"] + " " + b for b in a["blocks"]]).lower()
        hit = len(words & set(WORD.findall(text))) + 3 * len(set(focus) & set(a["blocks"]))
        if hit:
            scored.append((hit, a))
    if not scored:   # nothing named: the areas the task's likely to touch are the ones most depended on
        into = design.fan_in(deps)
        scored = sorted(((sum(into.get(b, 0) for b in a["blocks"]) + 1, a) for a in _areas(c)), key=lambda s: -s[0])
    areas = [a for _s, a in sorted(scored, key=lambda s: (-s[0], s[1]["id"]))]
    picked, rest = areas[:most], areas[most:]
    if preset:   # a saved workflow: its agents, on the blocks of theirs the system still has
        picked = [{"id": str(p.get("name") or "agent"), "title": str(p.get("area") or p.get("name") or "agent"),
                   "name": str(p.get("name") or ""), "blocks": [b for b in p.get("blocks") or [] if b in bp["components"]]}
                  for p in preset if isinstance(p, dict)]
        rest = []
    for a in rest:   # joins the picked area it is most connected to
        best = max(picked, key=lambda p: sum(1 for b in a["blocks"] for d in deps.get(b, ()) if d in p["blocks"])
                   + sum(1 for b in p["blocks"] for d in deps.get(b, ()) if d in a["blocks"]))
        best["blocks"] += a["blocks"]
    agents = []
    owned = {b: a["id"] for a in picked for b in a["blocks"]}
    try:                                              # what the person optimises for: every agent keeps and checks it
        from . import objectives
        keep = objectives.for_brief(root)
    except Exception:  # noqa: BLE001 - the plan stands without it
        keep = ""
    for a in picked:
        name = a.get("name") or re.sub(r"[^a-z0-9]+", "-", a["title"].lower()).strip("-") or "agent"
        needs = sorted({d for b in a["blocks"] for d in deps.get(b, ()) if d not in a["blocks"]})
        needed_by = sorted({b2 for b2, ds in deps.items() for d in ds if d in a["blocks"] and b2 not in a["blocks"]})
        talks_to = sorted({re.sub(r"[^a-z0-9]+", "-", next(p["title"] for p in picked if p["id"] == owned[x]).lower())
                           for x in needs + needed_by if x in owned and owned[x] != a["id"]})
        label = lambda ids: design.and_list([bp["components"][i]["label"] for i in ids if i in bp["components"]], 6)
        brief = (f"You are the {a['title']} agent in a team working on: {task}\n"
                 f"Work only in these blocks (claimed for you): {label(a['blocks'])}. "
                 f"First call the system-diff `claim` tool with agent \"{name}\" and these blocks.\n"
                 + (f"Your blocks use {label(needs)}: you are subscribed to them, so changes other agents make there "
                    "reach you with your next tool result.\n" if needs else "")
                 + (f"{label(needed_by)} use your blocks: keep their interfaces stable, and when you must change one, "
                    f"say so with the `publish` tool (topic: the block's id) before you do.\n" if needed_by else "")
                 + (f"Coordinate with: {', '.join(talks_to)}.\n" if talks_to else "")
                 + f"After each meaningful step, and before you finish, call the system-diff `agent_note` tool with agent "
                 f"\"{name}\" (done, next, blockers): if this session stops, whoever resumes your work starts from it.\n"
                 + "Finish with a short summary of what you changed in the system's structure.")
        if keep:
            brief += "\n" + keep
        agents.append({"name": name, "area": a["title"], "blocks": a["blocks"], "subscribes": needs,
                       "talks_to": talks_to, "brief": brief})
    # the order: who waits for whom (an agent using blocks another one changes, when staged; and what `after` or a
    # saved workflow says), then the stages that gives
    owner_of = {b: g["name"] for g in agents for b in g["blocks"]}
    names = {g["name"] for g in agents}
    said = {str(p.get("name")): list(p.get("waits_for") or []) for p in preset or [] if isinstance(p, dict)}
    waits: Dict[str, set] = {}
    for g in agents:
        w = set()
        if order == "staged":
            w |= {owner_of[d] for d in g["subscribes"] if d in owner_of}
        w |= set((after or {}).get(g["name"]) or []) | set(said.get(g["name"]) or [])
        waits[g["name"]] = {x for x in w if x in names and x != g["name"]}
    waits = untangle(waits)
    stage_of = stages_of(waits)
    notes = {str(p.get("name")): str(p.get("notes") or "") for p in preset or [] if isinstance(p, dict)}
    for g in agents:
        if notes.get(g["name"]):
            g["brief"] += "\n" + notes[g["name"]]
        g["waits_for"] = sorted(waits[g["name"]])
        g["stage"] = stage_of[g["name"]]
        if g["waits_for"]:
            g["brief"] += (f"\nYou start after {design.and_list(g['waits_for'], 6)} "
                           f"{'has' if len(g['waits_for']) == 1 else 'have'} finished: first read the system-diff "
                           f"`inbox` tool as \"{g['name']}\" for what they changed.")
    staged = max([g["stage"] for g in agents] or [1]) > 1
    if len(agents) <= 1:
        how = "One area: do it yourself; no team needed."
    elif not staged:
        how = (f"Launch all {len(agents)} of these agents now, in parallel, each as its own subagent with its brief "
               "as the prompt, in one message with several Task/Agent tool calls: none skipped, none merged into "
               "another. Do not do their work yourself. When they finish, read the "
               "system-diff `inbox` tool as \"main\" and integrate: resolve what they published, then check the whole "
               "with `system_diff`.")
    else:
        steps = []
        for n in range(1, max(g["stage"] for g in agents) + 1):
            here = [g for g in agents if g["stage"] == n]
            if not here:
                continue
            what = ", ".join(g["name"] for g in here)
            steps.append(f"stage {n}: {what}" + (" (in parallel)" if len(here) > 1 else "")
                         + ("" if n == 1 else " once the agents they wait for have finished"))
        how = ("Run these agents in stages, each as its own subagent with its brief as the prompt. Launch every agent "
               "of a stage together, in one message with several Task/Agent tool calls (none skipped, none merged into "
               "another), and start no agent of a later stage before every agent of the earlier stage has finished: "
               + "; ".join(steps) + ". Do not do their work yourself. At the "
               "end, read the system-diff `inbox` tool as \"main\" and integrate, then check the whole with "
               "`system_diff`.")
    at = round(time.time(), 3)
    publish(root, "all", f"Plan: {len(agents)} agent{'' if len(agents) == 1 else 's'} for: {task}", sender=MAIN,
            kind="plan")
    # a new plan starts a clean team: the last plan's agents, claims, ids and warnings go ("main" stays as it was);
    # the new agents start with no history, their cursors at the latest message on the bus
    data = _load(root)
    latest = messages(root)
    last = latest[-1]["n"] if latest else 0
    data["agents"] = {k: v for k, v in data["agents"].items() if k == MAIN}
    data["claims"], data["ids"], data["warned"] = {}, {}, {}
    # "after": the last message number at the plan, the plan's own announcement included: what counts as since the
    # plan (times alone tie within a millisecond)
    data["plan"] = {"task": task, "at": at, "after": last, "order": order,
                    "agents": [{k: v for k, v in g.items() if k != "brief"} for g in agents]}
    for g in agents:
        a = _agent(data, g["name"])
        a["brief"] = g["brief"]
        a["blocks"] = list(g["blocks"])
        a["topics"] = sorted(set(g["subscribes"]) | {f"agent:{g['name']}"})
        a["read"] = last
        for b in g["blocks"]:
            data["claims"][b] = g["name"]
    _save(root, data)
    out: Dict[str, Any] = {"task": task, "agents": agents, "how": how}
    try:   # roles that keep coming back: counted, and a saved subagent used when one fits
        from . import agentlib
        agentlib.planned(root, agents)
        for g in agents:
            hit = agentlib.match(root, g["name"], g["area"])
            if hit is not None:
                g["saved_agent"] = hit["name"]
                g["brief"] += (f"\nStart it as the saved subagent `{hit['name']}` (the Task tool's subagent_type), "
                               "with this brief as its prompt.")
        keep = agentlib.worth_keeping(root)
        if keep:
            out["worth_keeping"] = [{k: r[k] for k in ("name", "why")} for r in keep[:5]]
            out["save_hint"] = ("These roles keep coming back: save_agent saves one as a Claude Code subagent "
                                "(say what you saved).")
    except Exception:  # noqa: BLE001 - the plan stands without the library
        pass
    return out


ORCHESTRATE = ("system-diff: this repository works with a team of agents. Before you start on a task that touches "
               "more than one part of the system, call the system-diff `plan_agents` tool with the task; when it "
               "returns two or more agents, launch them in parallel as subagents with their briefs and coordinate "
               "through its bus (`inbox`, `publish`) instead of doing every part yourself. Small, single-part tasks "
               "need no team.")


# ── the workflow as it runs: who started, who waits, who is done, and what each one did ─────────────────────────

RUNS = "agent-runs.jsonl"   # each subagent's start and stop, from Claude Code's hooks: {"at", "event", "agent_id", ...}
RUNS_KEEP = 2000


def record_run(root: Path, event: str, agent_id: str, agent_type: str = "", transcript: str = "", **more: Any) -> None:
    """A subagent started or stopped (the SubagentStart and SubagentStop hooks), or was launched (its Task call's
    result: "description", "prompt", "status")."""
    if not agent_id or event not in ("start", "stop", "launch", "interrupted"):
        return
    rec: Dict[str, Any] = {"at": round(time.time(), 3), "event": event, "agent_id": agent_id,
                           "agent_type": agent_type or "", "transcript": transcript or ""}
    rec.update({k: (str(v)[:4000] if isinstance(v, str) else v) for k, v in more.items() if v})
    line = json.dumps(rec, ensure_ascii=False)
    f = git_dir(root) / RUNS
    try:
        with open(f, "a", encoding="utf-8") as out:
            out.write(line + "\n")
        if f.stat().st_size > RUNS_KEEP * 400:
            keep = f.read_text(encoding="utf-8", errors="replace").splitlines()[-RUNS_KEEP // 2:]
            activity._replace(f, "\n".join(keep) + "\n")
    except OSError:
        pass


def runs(root: Path, since: float = 0.0) -> List[Dict[str, Any]]:
    try:
        lines = (git_dir(root) / RUNS).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and float(r.get("at") or 0) >= since:
            out.append(r)
    return out


BUILT_IN = ("general-purpose", "Explore", "Plan", "fork", "claude", "statusline-setup", "claude-code-guide")
_INFO: Dict[str, tuple] = {}


def subagent_info(transcript: str) -> Dict[str, Any]:
    """What Claude Code says about a subagent beside its transcript (agent-<id>.meta.json: the Task call's
    description and the agent's type) and the prompt it was given (its transcript's first entry). Cached while the
    files stay the same; {} when there is nothing to read."""
    if not transcript:
        return {}
    t = Path(transcript)
    meta = t.with_name(t.stem + ".meta.json")
    try:
        stamp = (meta.stat().st_mtime_ns if meta.exists() else 0, t.stat().st_size if t.exists() else 0)
    except OSError:
        return {}
    hit = _INFO.get(transcript)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    out: Dict[str, Any] = {}
    try:
        m = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
        if isinstance(m, dict):
            out.update({"description": str(m.get("description") or "")[:200], "type": str(m.get("agentType") or ""),
                        "finished": bool(m.get("finished"))})
    except (OSError, ValueError):
        pass
    try:
        if t.exists():
            with open(t, encoding="utf-8", errors="replace") as f:
                for _n, line in zip(range(5), f):   # the prompt is the first user entry
                    e = json.loads(line)
                    msg = e.get("message") if isinstance(e, dict) else None
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        c = msg.get("content")
                        if isinstance(c, list):
                            c = " ".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
                        out["prompt"] = str(c or "")[:4000]
                        break
    except (OSError, ValueError):
        pass
    if len(_INFO) > 500:
        _INFO.clear()
    _INFO[transcript] = (stamp, out)
    return out


def readable(info: Dict[str, Any], agent_type: str = "", agent_id: str = "") -> str:
    """A subagent by what it does: the Task call's description, else its type (when not a generic one), else the
    first words of its prompt, else a short id."""
    d = str(info.get("description") or "").strip()
    if d:
        return d[:1].upper() + d[1:80]
    kind = str(info.get("type") or agent_type or "")
    if kind and kind not in BUILT_IN:
        return kind
    words = re.sub(r"\s+", " ", str(info.get("prompt") or "")).strip().split(" ")
    if words and words[0]:
        text = " ".join(words[:7])
        return (text if len(words) <= 7 else text + "…")[:80]
    return kind or ("agent " + agent_id[:6] if agent_id else "an agent")


def adhoc(root: Path, since: float = 0.0, most: int = 30) -> List[Dict[str, Any]]:
    """The subagents Claude launched on its own (no plan named them): each {"id", "title", "type", "started",
    "finished", "transcript", "prompt", "description", "status"}, oldest first, the last `most`."""
    ids = _load(root)["ids"]
    by: Dict[str, Dict[str, Any]] = {}
    for r in runs(root, since):
        aid = str(r.get("agent_id") or "")
        if not aid or aid in ids:
            continue
        g = by.setdefault(aid, {"id": aid, "type": "", "started": 0.0, "finished": 0.0, "transcript": "",
                                "description": "", "prompt": "", "status": ""})
        at = float(r.get("at") or 0)
        g["type"] = g["type"] or str(r.get("agent_type") or "")
        if r.get("transcript"):
            g["transcript"] = str(r["transcript"])
        if r.get("event") == "start":
            g["started"] = g["started"] or at
        elif r.get("event") == "stop":
            g["finished"] = at
        elif r.get("event") == "launch":
            g["description"] = g["description"] or str(r.get("description") or "")
            g["prompt"] = g["prompt"] or str(r.get("prompt") or "")
            g["type"] = g["type"] or str(r.get("agent_type") or "")
            g["started"] = g["started"] or at
            if r.get("status") in ("failed", "error"):
                g["status"] = "failed"
    out = []
    for g in by.values():
        info = subagent_info(g["transcript"])
        g["description"] = g["description"] or info.get("description", "")
        g["prompt"] = g["prompt"] or info.get("prompt", "")
        g["type"] = g["type"] or info.get("type", "")
        if not g["finished"] and info.get("finished"):   # Claude Code says it finished; its stop was not heard
            try:
                g["finished"] = max(g["started"], Path(g["transcript"]).stat().st_mtime)
            except OSError:
                g["finished"] = g["started"]
        g["title"] = readable({**info, "description": g["description"], "prompt": g["prompt"]}, g["type"], g["id"])
        g["session"] = Path(g["transcript"]).parent.parent.name if g["transcript"] else ""
        if g["started"] or g["finished"]:
            out.append(g)
    out.sort(key=lambda g: g["started"] or g["finished"])
    return out[-most:]


AFTER_GAP = 600.0   # seconds between one subagent's end and another's start that still read as "after it"


def adhoc_waits(team: List[Dict[str, Any]]) -> Dict[str, set]:
    """Who ran after whom among ad-hoc subagents: B after A when, in the same session, B started once A had finished
    (and not long after); only the nearest (A is dropped when B also waited for someone who came after A)."""
    waits: Dict[str, set] = {}
    for b in team:
        waits[b["id"]] = {a["id"] for a in team if a is not b and a["finished"] and b["started"]
                          and a.get("session", "") == b.get("session", "")
                          and a["finished"] <= b["started"] + 0.5 and b["started"] - a["finished"] < AFTER_GAP}
    return {k: {a for a in v if not any(a in waits.get(c, set()) for c in v if c != a)} for k, v in waits.items()}


def subagent_transcript(main_transcript: str, agent_id: str) -> str:
    """Where Claude Code writes a subagent's own transcript: beside its session's, <session>/subagents/agent-<id>.jsonl."""
    if not main_transcript or not agent_id:
        return ""
    p = Path(main_transcript)
    return str(p.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl")


def agent_kinds(root: Path) -> Dict[str, Dict[str, Any]]:
    """The kinds of agent Claude can add to a team: those extensions offer (`WORKFLOW_KINDS`: research-diff's
    experiment runner, section writer, contradiction reviewer...) and the person's saved agents (agentlib) for this
    project, by kind name."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from . import agentlib
        for a in agentlib.for_project(root):
            out[a["name"]] = {"kind": a["name"], "title": a["name"].replace("-", " "), "brief": a.get("description", ""),
                              "by": a.get("source", ""), "subagent_type": a["name"]}
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import extensions
        for ext in extensions.load():
            by = str(getattr(ext, "NAME", "") or "extension")
            items = getattr(ext, "WORKFLOW_KINDS", None)
            if callable(items):
                items = items(extensions.context(root, "", ""))
            for k in items or []:
                if isinstance(k, dict) and k.get("kind"):
                    kid = re.sub(r"[^a-z0-9]+", "-", str(k["kind"]).lower()).strip("-")
                    out[kid] = {"kind": kid, "title": str(k.get("title") or kid), "brief": str(k.get("brief") or ""),
                                "by": by, "tools": k.get("tools") or "", "model": k.get("model") or ""}
    except Exception:  # noqa: BLE001
        pass
    return out


def add_agent(root: Path, name: str, brief: str = "", blocks: Iterable[str] = (), waits_for: Iterable[str] = (),
              area: str = "", kind: str = "") -> Dict[str, Any]:
    """Add an agent to the team's plan as it runs (the person asked for one in the chat): it claims its blocks, waits
    for the agents (or extensions' steps) named, and gets a brief; the plan's stages follow. `kind`: one of
    agent_kinds (an extension's, or a saved agent): its brief, and the subagent type to start it as."""
    name = re.sub(r"[^a-z0-9]+", "-", str(name or kind or "").lower()).strip("-")
    k = agent_kinds(root).get(re.sub(r"[^a-z0-9]+", "-", str(kind or "").lower()).strip("-")) if kind else None
    if kind and k is None:
        raise ValueError(f"no agent kind {kind!r}: agent_kinds lists them")
    if k is not None:
        area = area or k["title"]
        brief = (brief + "\n" if brief else "") + str(k.get("brief") or "")
    if not name:
        raise ValueError("name the agent: lowercase letters, digits and dashes")
    data = _load(root)
    pl = data["plan"] or {}
    if not pl.get("agents"):
        pl = {"task": brief or name, "at": round(time.time(), 3), "after": (messages(root) or [{"n": 0}])[-1]["n"],
              "order": "parallel", "agents": []}
    if any(g.get("name") == name for g in pl["agents"]):
        raise ValueError(f"the plan has an agent {name!r} already")
    names = {g["name"] for g in pl["agents"]} | {str(x.get("id")) for x in pl.get("steps") or [] if isinstance(x, dict)}
    try:
        names |= {x["id"] for x in _ext_steps(root) if x.get("id")}
    except Exception:  # noqa: BLE001
        pass
    waits = sorted({w for w in waits_for if w in names})
    unknown = sorted(set(waits_for) - names)
    stage = 1 + max([g.get("stage", 1) for g in pl["agents"] if g["name"] in waits] or [0])
    blocks = [b for b in blocks if isinstance(b, str) and b]
    taken = {b: data["claims"][b] for b in blocks if data["claims"].get(b) not in (None, name)}
    task = pl.get("task") or ""
    brief = brief.replace("{task}", task)
    text = (brief.strip() or (f"You are the {area or name} agent in a team working on: {task}")) + "\n" + \
        (f"First call the system-diff `claim` tool with agent \"{name}\" and these blocks: {', '.join(blocks)}.\n"
         if blocks else f"Call system-diff tools with agent \"{name}\".\n") + \
        (f"You start after {design.and_list(waits, 6)} {'has' if len(waits) == 1 else 'have'} finished: first read "
         f"the system-diff `inbox` tool as \"{name}\" for what they changed.\n" if waits else "") + \
        (f"After each meaningful step, and before you finish, call the system-diff `agent_note` tool with agent "
         f"\"{name}\" (done, next, blockers): if this session stops, whoever resumes your work starts from it.\n"
         "Finish with a short summary of what you changed in the system's structure.")
    pl["agents"].append({"name": name, "area": area or name, "blocks": blocks, "subscribes": [], "talks_to": [],
                         "waits_for": waits, "stage": stage, "added": round(time.time(), 3),
                         **({"kind": k["kind"], "by": k["by"]} if k else {})})
    data["plan"] = pl
    a = _agent(data, name)
    a["brief"], a["blocks"] = text, blocks
    a["topics"] = sorted(set(a["topics"]) | {f"agent:{name}"})
    a["read"] = (messages(root) or [{"n": 0}])[-1]["n"]
    for b in blocks:
        data["claims"].setdefault(b, name)
    _save(root, data)
    publish(root, "all", f"Added to the plan: {name}" + (f", after {', '.join(waits)}" if waits else ""),
            sender=MAIN, kind="plan")
    wf = workflow(root)
    status = {g["name"]: g["status"] for g in wf["agents"]}
    status.update({x["id"]: x["status"] for x in wf["steps"]})
    ready = all(status.get(w) == "done" for w in waits)
    start_as = (f" (Task tool, subagent_type {k['subagent_type']!r})" if k and k.get("subagent_type") else "")
    return {"agent": name, "brief": text, "stage": stage, "waits_for": waits, "unknown_waits": unknown,
            "taken_from_others": taken, "kind": k["kind"] if k else "",
            "how": (f"Launch it now as its own subagent{start_as}, with this brief as its prompt." if ready else
                    f"Launch it as its own subagent{start_as}, with this brief as its prompt, once "
                    f"{design.and_list(waits, 6)} {'has' if len(waits) == 1 else 'have'} finished.")}


def set_steps(root: Path, steps: List[Dict[str, Any]]) -> None:
    """The steps a saved workflow brings (an extension's, by id), kept with the plan; extensions report their status."""
    data = _load(root)
    if data["plan"]:
        data["plan"]["steps"] = [dict(x) for x in steps]
        _save(root, data)


STATES = ("queued", "waiting", "running", "paused", "done", "failed")
ACTIONS = ("pause", "resume", "stop", "retry")


def _ext_steps(root: Path) -> List[Dict[str, Any]]:
    """The steps extensions add to the workflow as it runs. An extension's `workflow(ctx)` gives {"steps": [...],
    "waits": [...]} (or the steps alone): a step {"id", "title", "kind" (agent | run | check), "state" (waiting,
    running, done, failed, paused), "agent"?, "session"? (a Studio session id), "log"? (text), "files"?, "actions"?
    (pause, resume, stop, retry), "detail"?, "started"?, "finished"?}; a wait {"from": what must finish first, "to":
    what waits for it, "why"?}, by step id or a team agent's name. Each step marked with the extension's name."""
    out: List[Dict[str, Any]] = []
    try:
        from . import extensions
        exts = extensions.load()
    except Exception:  # noqa: BLE001
        return out
    for ext in exts:
        fn = getattr(ext, "workflow", None)
        if not callable(fn):
            continue
        by = str(getattr(ext, "NAME", "") or "extension")
        try:
            got = fn(extensions.context(root, "", ""))
        except Exception as exc:  # noqa: BLE001 - its steps are missing, nothing else
            extensions.failed(by + " workflow", exc)
            continue
        steps = got.get("steps") if isinstance(got, dict) else got
        waits = got.get("waits") if isinstance(got, dict) else []
        mine: Dict[str, Dict[str, Any]] = {}
        for x in steps or []:
            if isinstance(x, dict) and x.get("id"):
                mine[str(x["id"])] = {**x, "by": by, "waits_for": [str(w) for w in x.get("waits_for") or []],
                                      "why": dict(x.get("why") or {}) if isinstance(x.get("why"), dict) else {}}
        extra = []
        for w in waits or []:
            if not isinstance(w, dict) or not w.get("from") or not w.get("to"):
                continue
            a, b = str(w["from"]), str(w["to"])
            if b in mine:
                mine[b]["waits_for"].append(a)
                if w.get("why"):
                    mine[b]["why"][a] = str(w["why"])[:200]
            else:   # a team agent waits for one of its steps
                extra.append({"agent": b, "waits_for": a, "why": str(w.get("why") or "")[:200], "by": by})
        out += list(mine.values())
        out += [{"_agent_wait": True, **e} for e in extra]
    return out


def workflow(root: Path) -> Dict[str, Any]:
    """The team's plan as it runs: its stages, and each agent with who it waits for and its status ("queued": its
    waits are done and it was not launched yet; "waiting": for the agents named; "running"; "done"), when it started
    and finished, what it read and edited, and its last action. From the plan, the subagents' starts and stops
    (hooks), and the activity log (each hook knows which subagent acted)."""
    data = _load(root)
    pl = data["plan"] or {}
    agents_ = [g for g in pl.get("agents") or [] if isinstance(g, dict) and g.get("name")]
    since = float(pl.get("at") or 0)   # with no team plan: the extensions' steps alone (auto-research's runs)
    ids = dict(data["ids"])
    names = {g["name"] for g in agents_}
    info: Dict[str, Dict[str, Any]] = {g["name"]: {"started": 0.0, "finished": 0.0, "transcript": "", "ids": [],
                                                   "edits": 0, "reads": 0, "last": "", "last_at": 0.0,
                                                   "interrupted": 0.0}
                                       for g in agents_}
    for r in runs(root, since):
        who = ids.get(r["agent_id"]) or (r.get("agent_type") if r.get("agent_type") in names else "")
        if not who:
            continue
        i = info[who]
        if r["agent_id"] not in i["ids"]:
            i["ids"].append(r["agent_id"])
        if r["event"] == "start":
            i["started"] = i["started"] or float(r["at"])
            i["finished"] = 0.0 if float(r["at"]) > i["finished"] else i["finished"]
            i["interrupted"] = 0.0
        elif r["event"] == "interrupted":
            i["interrupted"] = float(r["at"])
        elif i["interrupted"] and float(r["at"]) - i["interrupted"] < TEARDOWN:
            continue   # the stop Claude Code reports for the subagent it killed while closing
        else:
            i["finished"] = float(r["at"])
        if r.get("transcript"):
            i["transcript"] = r["transcript"]
    for e in activity.events(root, 2000):
        if float(e.get("at") or 0) < since:
            continue
        who = ids.get(str(e.get("agent") or ""))
        if who not in info:
            continue
        i = info[who]
        i["started"] = i["started"] or float(e["at"])
        if e.get("event") == activity.EDIT:
            i["edits"] += 1
        else:
            i["reads"] += 1
        i["last"] = ("edited " if e.get("event") == activity.EDIT else "read ") + str(e.get("path") or "")
        i["last_at"] = float(e["at"])
    now = time.time()
    out = []
    try:
        mem = memory(root)
    except Exception:  # noqa: BLE001 - the board stands without the agents' memory
        mem = {}
    for g in agents_:
        i = info[g["name"]]
        waits = list(g.get("waits_for") or [])
        # launched before an agent it waits for had finished (or while that one never ran): it started early
        early = bool(i["started"]) and any(not info.get(w, {}).get("finished") or info[w]["finished"] > i["started"]
                                            for w in waits if w in info)
        if i["finished"]:
            status = "done"
        elif i["started"] and i["interrupted"]:
            status = "interrupted"   # its session ended mid-task: resume_plan relaunches it from its memory
        elif i["started"]:
            status = "running"
        elif any(not info.get(w, {}).get("finished") for w in waits):
            status = "waiting"
        else:
            status = "queued"
        end = i["finished"] or (now if i["started"] else 0)
        out.append({"name": g["name"], "area": g.get("area") or g["name"], "blocks": list(g.get("blocks") or []),
                    "waits_for": waits, "stage": int(g.get("stage") or 1), "status": status,
                    "waiting_on": [w for w in waits if not info.get(w, {}).get("finished")],
                    "started": i["started"], "finished": i["finished"],
                    "seconds": round(end - i["started"], 1) if i["started"] else 0.0,
                    "edits": i["edits"], "reads": i["reads"], "last": i["last"], "last_at": i["last_at"],
                    "added": bool(g.get("added")), "has_log": bool(i["transcript"] or i["ids"]),
                    "early": early, "launched": bool(i["started"]), "attempts": len(i["ids"]),
                    "note": _note_text(((mem.get(g["name"]) or {}).get("notes") or [None])[-1])})
    # the steps extensions add (a saved workflow's, kept with the plan; and those extensions report live)
    steps: Dict[str, Dict[str, Any]] = {}
    for x in pl.get("steps") or []:
        if isinstance(x, dict) and x.get("id"):
            steps[str(x["id"])] = {"id": str(x["id"]), "title": str(x.get("title") or x["id"]),
                                   "waits_for": [str(w) for w in x.get("waits_for") or []], "by": str(x.get("by") or ""),
                                   "status": "", "detail": "", "started": 0.0, "finished": 0.0}
    agent_waits: Dict[str, List[str]] = {}
    for x in _ext_steps(root):
        if x.get("_agent_wait"):
            agent_waits.setdefault(x["agent"], []).append(x["waits_for"])
            continue
        sid = str(x["id"])
        if sid in names:
            sid = f"{x['by']}:{sid}"   # never one of the agents' names
        base = steps.get(sid) or {}
        state = str(x.get("state") or x.get("status") or "")
        steps[sid] = {**base, "id": sid, "title": str(x.get("title") or base.get("title") or sid),
                      "waits_for": [str(w) for w in (x.get("waits_for") or base.get("waits_for") or [])],
                      "why": dict(x.get("why") or {}), "by": str(x.get("by") or base.get("by") or ""),
                      "status": state if state in STATES else "", "kind_of_step": str(x.get("kind") or "run"),
                      "agent_name": str(x.get("agent") or ""), "session": str(x.get("session") or ""),
                      "log": str(x.get("log") or "")[-20000:], "files": [str(f) for f in x.get("files") or []][:50],
                      "actions": [a for a in x.get("actions") or [] if a in ACTIONS],
                      "detail": str(x.get("detail") or "")[:300], "started": float(x.get("started") or 0),
                      "finished": float(x.get("finished") or 0), "has_log": True}
    for a in out:   # an agent of the team that waits for an extension's step
        extra = [w for w in agent_waits.get(a["name"], []) if w not in a["waits_for"]]
        if extra:
            a["waits_for"] = a["waits_for"] + extra
            if a["status"] == "queued":
                a["status"] = "waiting"
    # the subagents Claude launched on its own (no plan named them): drawn too, ordered by their times
    for a in out:
        a["title"] = a["name"]
    loose = adhoc(root, since if agents_ else time.time() - 12 * 3600)
    loose_waits = adhoc_waits(loose)
    acts: Dict[str, Dict[str, Any]] = {}
    if loose:
        for e in activity.events(root, 2000):
            aid = str(e.get("agent") or "")
            if aid:
                c = acts.setdefault(aid, {"reads": 0, "edits": 0, "last": "", "last_at": 0.0})
                if e.get("event") == activity.EDIT:
                    c["edits"] += 1
                else:
                    c["reads"] += 1
                c["last"] = ("edited " if e.get("event") == activity.EDIT else "read ") + str(e.get("path") or "")
                c["last_at"] = float(e.get("at") or 0)
    for g in loose:
        c = acts.get(g["id"], {"reads": 0, "edits": 0, "last": "", "last_at": 0.0})
        status = "failed" if g["status"] == "failed" else "done" if g["finished"] else "running"
        end = g["finished"] or now
        out.append({"name": "sub:" + g["id"], "title": g["title"], "type": g["type"], "adhoc": True,
                    "area": g["type"] if g["type"] and g["type"] not in BUILT_IN else "", "blocks": [],
                    "waits_for": ["sub:" + w for w in sorted(loose_waits.get(g["id"], set()))], "stage": 1,
                    "status": status, "waiting_on": [], "started": g["started"], "finished": g["finished"],
                    "seconds": round(end - g["started"], 1) if g["started"] else 0.0,
                    "edits": c["edits"], "reads": c["reads"], "last": c["last"], "last_at": c["last_at"],
                    "added": False, "has_log": bool(g["transcript"])})
    done = {a["name"] for a in out if a["status"] in ("done", "failed")} | {k for k, v in steps.items() if v["status"] == "done"}
    for a in out:
        a["waiting_on"] = [] if a.get("adhoc") else [w for w in a["waits_for"] if w not in done]
        if a["status"] == "waiting" and not a["waiting_on"]:
            a["status"] = "queued"
    for v in steps.values():
        if v["status"] not in ("running", "done", "failed", "paused"):
            v["status"] = "waiting" if any(w not in done for w in v["waits_for"] if w in names or w in steps) else "queued"
        v["waiting_on"] = [w for w in v["waits_for"] if w not in done and (w in names or w in steps)]
        v["kind"] = "step"
    # one order for both: who waits for whom, circles untangled, then the stages
    every = {a["name"]: set(a["waits_for"]) for a in out}
    every.update({k: set(v["waits_for"]) for k, v in steps.items()})
    every = {k: {w for w in v if w in every} for k, v in every.items()}
    stage_of = stages_of(every)
    for a in out:
        a["stage"] = stage_of.get(a["name"], a["stage"])
        a["kind"] = "agent"
    for k, v in steps.items():
        v["stage"] = stage_of.get(k, 1)
    planned = [a for a in out if not a.get("adhoc")]
    for a in planned:   # not launched while an agent of its stage or a later one runs or ran: Claude skipped it?
        a["skipped"] = not a.get("launched") and any(b.get("launched") and b["stage"] >= a["stage"]
                                                     for b in planned if b is not a)
    top = max([a["stage"] for a in out] + [v["stage"] for v in steps.values()] or [1])
    stages = [[a["name"] for a in out if a["stage"] == n] + [k for k, v in steps.items() if v["stage"] == n]
              for n in range(1, top + 1)]
    return {"task": pl.get("task") or ("" if agents_ or not loose else "Subagents Claude launched on its own"),
            "at": since, "order": pl.get("order") or "parallel", "agents": out,
            "steps": list(steps.values()), "stages": [s for s in stages if s],
            "planned": len(planned), "launched": sum(1 for a in planned if a.get("launched")),
            "skipped": [a["name"] for a in planned if a.get("skipped")]}


# ── each planned agent's memory: what it did, so an agent relaunched after a stop resumes instead of starting over ──

MEMORY = "agent-memory.json"
NOTES_KEEP, FILES_KEEP, MSGS_KEEP, ANSWER_MOST = 20, 50, 30, 4000
FAILED = ("failed", "error", "killed", "cancelled")
TEARDOWN = 60.0   # seconds after the Studio marks an agent interrupted in which Claude Code's closing events don't count
_MEM_LOCK = threading.Lock()


def _mem_load(root: Path) -> Dict[str, Any]:
    """{"plan_at", "agents": {name: {"notes", "files", "messages", "answer", "answer_at"}}}: a new plan, a new memory."""
    f = git_dir(root) / MEMORY
    at = float((_load(root).get("plan") or {}).get("at") or 0)
    for attempt in range(4):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and float(data.get("plan_at") or 0) == at and isinstance(data.get("agents"), dict):
                return data
            break
        except FileNotFoundError:
            break
        except (OSError, ValueError):
            time.sleep(0.02 * (attempt + 1))
    return {"plan_at": at, "agents": {}}


def _mem_save(root: Path, data: Dict[str, Any]) -> None:
    f = git_dir(root) / MEMORY
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    for attempt in range(4):
        try:
            activity._replace(f, text)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}"
                    r"|sk-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})")
_LONG = re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b")
_PAIR = re.compile(r"(?i)(\b[\w.-]*(?:key|token|secret|password|passwd|pwd|pat)[\w.-]*\s*[=:]\s*['\"]?)(?!\$)[^\s'\",;]{4,}")


def _masked(text: str) -> str:
    """Free text with what looks like a key or token masked (an agent's note or answer: kept on disk and shown)."""
    from .extract import redact
    out = []
    for line in str(text or "").split("\n"):
        line = redact(line) if ("=" in line or "@" in line) else line
        line = _PAIR.sub(lambda m: m.group(1) + "***", line)
        line = _TOKEN.sub("***", line)
        line = _LONG.sub(lambda m: "***" if (re.search(r"\d", m.group(0)) and re.search(r"[A-Za-z]", m.group(0))
                                             and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", m.group(0))
                                             and "/" not in m.group(0)) else m.group(0), line)
        out.append(line)
    return "\n".join(out)


def _mem_of(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    return data["agents"].setdefault(name, {"notes": [], "files": [], "messages": [], "answer": "", "answer_at": 0.0})


def memory(root: Path) -> Dict[str, Dict[str, Any]]:
    """Every planned agent's memory, by name."""
    return _mem_load(root)["agents"]


def planned_names(root: Path) -> List[str]:
    return [str(g["name"]) for g in (_load(root).get("plan") or {}).get("agents") or [] if isinstance(g, dict)
            and g.get("name")]


def agent_note(root: Path, agent: str, done: str = "", next_: str = "", notes: str = "",
               blockers: str = "") -> Dict[str, Any]:
    """An agent of the plan says where it is (at each milestone, and before it finishes): kept, the newest
    NOTES_KEEP, so an agent relaunched after a stop starts from it."""
    agent = str(agent or "").strip()
    if not agent:
        raise ValueError("agent: your name in the plan")
    names = planned_names(root)
    if names and agent not in names:
        raise ValueError(f"no agent {agent!r} in the plan: one of {', '.join(names)}")
    said = {k: _masked(str(v))[:1500] for k, v in (("done", done), ("next", next_), ("notes", notes),
                                                  ("blockers", blockers)) if str(v or "").strip()}
    if not said:
        raise ValueError("say at least one of done, next, notes, blockers")
    with _MEM_LOCK:
        data = _mem_load(root)
        m = _mem_of(data, agent)
        m["notes"] = (m["notes"] + [{"at": round(time.time(), 3), **said}])[-NOTES_KEEP:]
        _snapshot(root, data, agent)
        _mem_save(root, data)
    return {"kept": agent, "notes": len(m["notes"]),
            "next": "keep going; call agent_note again at your next milestone and before you finish"}


def response_text(got: Any) -> str:
    """A Task call's answer as text (its result), whatever shape the hook gives it in."""
    if isinstance(got, str):
        return got
    if isinstance(got, dict):
        for k in ("result", "text", "output"):
            if isinstance(got.get(k), str) and got[k].strip():
                return got[k]
        content = got.get("content")
        if isinstance(content, list):
            return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(content, str):
            return content
    if isinstance(got, list):
        return "\n".join(response_text(x) for x in got)
    return ""


def last_answer(transcript: str) -> str:
    """The last thing a subagent said (the last assistant text of its transcript), "" when there is none."""
    if not transcript:
        return ""
    last = ""
    try:
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                content = ((d.get("message") or {}).get("content")) if isinstance(d, dict) else None
                text = "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict)
                                 and b.get("type") == "text").strip() if isinstance(content, list) else ""
                if text:
                    last = text
    except OSError:
        return ""
    return last


def _snapshot(root: Path, data: Dict[str, Any], name: str) -> None:
    """What `name` read and edited and what it said and heard on the bus, merged into its memory (kept when the
    activity log or the bus rolls past them)."""
    m = _mem_of(data, name)
    team = _load(root)
    ids = {i for i, n in team["ids"].items() if n == name}
    since = float(data.get("plan_at") or 0)
    files = {f["path"]: f for f in m.get("files") or [] if isinstance(f, dict) and f.get("path")}
    for e in activity.events(root, 2000):
        if str(e.get("agent") or "") not in ids or float(e.get("at") or 0) < since or not e.get("path"):
            continue
        path = str(e["path"])
        was = files.pop(path, None) or {"path": path, "edited": False}
        files[path] = {"path": path, "edited": bool(was.get("edited") or e.get("event") == activity.EDIT),
                       "at": float(e["at"])}
    m["files"] = list(files.values())[-FILES_KEEP:]
    a = team["agents"].get(name) or {}
    topics = set(a.get("topics") or []) | {f"agent:{name}", "all"}
    after = int((team.get("plan") or {}).get("after") or 0)
    got = [{"n": x["n"], "at": x.get("at"), "from": x.get("from"), "topic": x.get("topic"),
            "text": _masked(str(x.get("text") or ""))[:500]}
           for x in messages(root, after) if x.get("from") == name or x.get("topic") in topics]
    seen = {x["n"]: x for x in m.get("messages") or [] if isinstance(x, dict) and "n" in x}
    seen.update({x["n"]: x for x in got})
    m["messages"] = [seen[k] for k in sorted(seen)][-MSGS_KEEP:]


def remember(root: Path, name: str, answer: str = "") -> None:
    """Keep what `name` did (a subagent of it stopped, or its Task call came back): its files and messages, and its
    last answer. Never raises: the hooks call it."""
    try:
        if not name or name == MAIN or name not in planned_names(root):
            return
        with _MEM_LOCK:
            data = _mem_load(root)
            m = _mem_of(data, name)
            if answer and answer.strip():
                m["answer"], m["answer_at"] = _masked(answer.strip())[-ANSWER_MOST:], round(time.time(), 3)
            _snapshot(root, data, name)
            _mem_save(root, data)
    except Exception:  # noqa: BLE001 - memory is a help: a failure never stops a hook
        pass


def attempts(root: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Each planned agent's launches since the plan, oldest first: {"id", "started", "finished", "status" (the Task
    call's), "transcript", "last_at"}. A relaunch after a stop is a second attempt under the same name."""
    data = _load(root)
    since = float((data.get("plan") or {}).get("at") or 0)
    names = set(planned_names(root))
    out: Dict[str, Dict[str, Dict[str, Any]]] = {n: {} for n in names}
    for r in runs(root, since):
        who = data["ids"].get(r["agent_id"]) or (r.get("agent_type") if r.get("agent_type") in names else "")
        if who not in out:
            continue
        a = out[who].setdefault(r["agent_id"], {"id": r["agent_id"], "started": 0.0, "finished": 0.0, "status": "",
                                                "transcript": "", "last_at": 0.0, "interrupted": 0.0})
        at = float(r.get("at") or 0)
        if r["event"] == "start":
            a["started"] = a["started"] or at
        elif r["event"] == "interrupted":
            a["interrupted"] = at
        elif a["interrupted"] and at - a["interrupted"] < TEARDOWN:
            pass   # Claude Code, closing, reports the subagent it killed as stopped and completed: it was not
        elif r["event"] == "stop":
            a["finished"] = at
        elif r["event"] == "launch":
            a["started"] = a["started"] or at
            a["status"] = str(r.get("status") or a["status"])
            if a["status"] in ("completed", "done") and not a["finished"]:
                a["finished"] = at
        a["transcript"] = r.get("transcript") or a["transcript"]
        a["last_at"] = max(a["last_at"], at)
    for e in activity.events(root, 2000):
        aid = str(e.get("agent") or "")
        who = data["ids"].get(aid)
        if who in out and aid in out[who]:
            out[who][aid]["last_at"] = max(out[who][aid]["last_at"], float(e.get("at") or 0))
    return {n: sorted(v.values(), key=lambda a: a["started"]) for n, v in out.items()}


def _note_text(n: Optional[Dict[str, Any]]) -> str:
    """An agent's note in one line: "done: …; next: …"."""
    if not isinstance(n, dict):
        return ""
    return "; ".join(f"{k}: {str(n[k])[:160]}" for k in ("done", "next", "blockers", "notes") if n.get(k))


def status_of(tries: List[Dict[str, Any]], now: Optional[float] = None) -> str:
    """"not launched", "running" (its last attempt acted within two minutes), "interrupted" (launched, never
    finished: its session stopped), "failed" or "done"."""
    if not tries:
        return "not launched"
    last = tries[-1]
    if last["status"] in FAILED:
        return "failed"
    if last["finished"]:
        return "done"
    if last.get("interrupted"):
        return "interrupted"
    return "running" if (now or time.time()) - (last["last_at"] or last["started"]) < 120 else "interrupted"


def interrupted(root: Path, session: str) -> List[str]:
    """The session `session` (Claude Code's id) ended: its planned agents still running are interrupted (recorded,
    so resume_plan relaunches them at once instead of taking them for running). Their names."""
    if not session:
        return []
    data = _load(root)
    since = float((data.get("plan") or {}).get("at") or 0)
    open_: Dict[str, str] = {}
    for r in runs(root, since):
        aid = str(r.get("agent_id") or "")
        if r["event"] == "start" and session in str(r.get("transcript") or "") and data["ids"].get(aid):
            open_[aid] = str(r.get("agent_type") or "")
        elif r["event"] in ("stop", "interrupted") or (r["event"] == "launch" and r.get("status") in ("completed", "done")):
            open_.pop(aid, None)
    for aid, kind in open_.items():
        record_run(root, "interrupted", aid, kind, "")
    return sorted({data["ids"][a] for a in open_})


def resume_brief(root: Path, name: str, mem: Optional[Dict[str, Any]] = None,
                 tries: Optional[List[Dict[str, Any]]] = None) -> str:
    """The brief to relaunch `name` with: its first brief, what it did so far, then how to go on."""
    data = _load(root)
    brief = str((data["agents"].get(name) or {}).get("brief") or f"You are the {name} agent.")
    m = mem if mem is not None else _mem_of(_mem_load(root), name)
    tries = tries if tries is not None else attempts(root).get(name, [])
    parts = [brief, "", f"You are resuming (attempt {len(tries) + 1}): what you did so far:"]
    head = len(parts)
    for n in m.get("notes") or []:
        line = "; ".join(f"{k}: {n[k]}" for k in ("done", "next", "notes", "blockers") if n.get(k))
        parts.append(f"- note: {line}")
    edited = [f["path"] for f in m.get("files") or [] if f.get("edited")]
    read = [f["path"] for f in m.get("files") or [] if not f.get("edited")]
    if edited:
        parts.append("- files you edited: " + ", ".join(edited[-30:]))
    if read:
        parts.append("- files you read: " + ", ".join(read[-15:]))
    unread = inbox(root, name, mark=False)
    if unread:
        parts.append("- messages for you, not read yet:")
        parts += [f"  - from {x.get('from')}: {str(x.get('text') or '')[:300]}" for x in unread[-10:]]
    if m.get("answer"):
        parts.append("- your last answer (before the stop):\n" + str(m["answer"])[-1500:])
    if len(parts) == head:
        parts.append("- nothing recorded: start from the brief")
    parts += ["", "First check the current state of your files before changing anything; don't redo finished steps."]
    return "\n".join(parts)


def resume_plan(root: Path) -> Dict[str, Any]:
    """The plan, to resume after a stop: each planned agent's status ("done", "running", "interrupted", "failed",
    "not launched"), its attempts and latest note, a ready-to-launch resume brief for every agent not done, and how
    to launch them (the earliest unfinished stage first)."""
    pl = _load(root).get("plan") or {}
    planned = [g for g in pl.get("agents") or [] if isinstance(g, dict) and g.get("name")]
    if not planned:
        return {"task": "", "agents": [], "unfinished": [],
                "how": "There is no team plan here: plan one with plan_agents."}
    mem = _mem_load(root)
    tries = attempts(root)
    stage_of = {a["name"]: a["stage"] for a in workflow(root)["agents"] if not a.get("adhoc")}
    now = time.time()
    out = []
    for g in planned:
        name = g["name"]
        m = _mem_of(mem, name)
        st = status_of(tries.get(name, []), now)
        row: Dict[str, Any] = {"name": name, "stage": int(stage_of.get(name, g.get("stage") or 1)), "status": st,
                               "attempts": len(tries.get(name, [])), "waits_for": list(g.get("waits_for") or []),
                               "last_note": (m.get("notes") or [None])[-1],
                               "edited": [f["path"] for f in m.get("files") or [] if f.get("edited")][-20:]}
        if st not in ("done", "running"):
            row["resume_brief"] = resume_brief(root, name, m, tries.get(name, []))
        out.append(row)
    todo = [r for r in out if r["status"] != "done"]
    if not todo:
        how = ("Every agent of the plan is done: read the system-diff `inbox` as \"main\", integrate, and check the "
               "whole with `system_diff`.")
    else:
        first = min(r["stage"] for r in todo)
        go = [r["name"] for r in todo if r["stage"] == first and r["status"] != "running"]
        running = [r["name"] for r in todo if r["status"] == "running"]
        how = ((f"Relaunch together, in one message with one Task/Agent call each, with each one's resume_brief as its "
                f"prompt: {', '.join(go)}. ") if go else "") + \
            (f"Still running: {', '.join(running)} (do not launch them again). " if running else "") + \
            "Start a later stage only once these have finished; do not redo what the done agents did."
    return {"task": pl.get("task") or "", "agents": out, "unfinished": [r["name"] for r in todo], "how": how}

LOG_MOST = 400


def _iso(ts: Any) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def transcript_entries(path: str, most: int = LOG_MOST) -> List[Dict[str, Any]]:
    """A subagent's transcript as the page shows it: its prompt (the brief), what it said, each tool call (and the
    file or command), each result (cut), oldest first. Secrets masked."""
    from .extract import redact
    out: List[Dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") not in ("user", "assistant"):
            continue
        at = _iso(d.get("timestamp"))
        content = (d.get("message") or {}).get("content")
        if isinstance(content, str):
            if d["type"] == "user" and content.strip():
                out.append({"at": at, "kind": "prompt", "text": redact(content)[:6000]})
            continue
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and str(b.get("text") or "").strip():
                out.append({"at": at, "kind": "prompt" if d["type"] == "user" else "text",
                            "text": redact(str(b["text"]))[:4000]})
            elif t == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                what = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("description") or ""
                out.append({"at": at, "kind": "tool", "name": str(b.get("name") or ""),
                            "text": redact(str(what))[:300]})
            elif t == "tool_result":
                c = b.get("content")
                text = c if isinstance(c, str) else " ".join(str(x.get("text") or "") for x in c or []
                                                             if isinstance(x, dict))
                out.append({"at": at, "kind": "result", "error": bool(b.get("is_error")),
                            "text": redact(text)[:1500]})
    return out[-most:]


def step_log(root: Path, sid: str) -> Dict[str, Any]:
    """An extension's step, for the pop-up: what the extension says about it (`step_log(ctx, id) -> {"entries":
    [{"at", "kind", "text"}], ...}`)."""
    wf = workflow(root)
    me = next((x for x in wf["steps"] if x["id"] == sid), None)
    if me is None:
        raise KeyError(sid)
    got: Dict[str, Any] = {}
    try:
        from . import extensions
        ext = next((e for e in extensions.load() if str(getattr(e, "NAME", "")) == me["by"]), None)
        fn = getattr(ext, "step_log", None) if ext is not None else None
        if fn is not None:
            got = fn(extensions.context(root, "", ""), sid.split(":", 1)[-1] if sid.startswith(me["by"] + ":") else sid) or {}
    except Exception as exc:  # noqa: BLE001
        got = {"entries": [{"at": time.time(), "kind": "result", "error": True, "text": f"{type(exc).__name__}: {exc}"}]}
    entries = list(got.get("entries") or [])
    if not entries and me.get("log"):
        entries = [{"at": 0, "kind": "text", "text": line} for line in me["log"].splitlines() if line.strip()]
    return {"step": me, "transcript": entries[-LOG_MOST:], "brief": str(got.get("about") or me.get("detail") or ""),
            "messages": [], "files": [{"at": 0, "event": "file", "path": f} for f in me.get("files") or []],
            "session": me.get("session") or ""}


def step_action(root: Path, sid: str, action: str) -> Dict[str, Any]:
    """Pause, resume, stop or retry an extension's step: the extension does it (`workflow_action(ctx, id, action)`),
    and only for the actions the step lists."""
    if action not in ACTIONS:
        raise ValueError(f"action is one of {', '.join(ACTIONS)}")
    wf = workflow(root)
    me = next((x for x in wf["steps"] if x["id"] == sid), None)
    if me is None:
        raise KeyError(sid)
    if action not in me.get("actions", []):
        raise ValueError(f"the step {sid!r} does not offer {action!r} (it offers: {', '.join(me.get('actions') or []) or 'nothing'})")
    from . import extensions
    ext = next((e for e in extensions.load() if str(getattr(e, "NAME", "")) == me["by"]), None)
    fn = getattr(ext, "workflow_action", None) if ext is not None else None
    if fn is None:
        raise ValueError(f"{me['by']} does not take actions on its steps")
    got = fn(extensions.context(root, "", ""), sid.split(":", 1)[-1] if sid.startswith(me["by"] + ":") else sid, action)
    return {"step": sid, "action": action, **(got if isinstance(got, dict) else {"ok": bool(got)})}


def agent_log(root: Path, name: str) -> Dict[str, Any]:
    """One agent of the plan, for the page's pop-up: its place in the workflow, its brief, its session (the
    subagent's transcript, when Claude Code wrote one), the messages it sent and got on the bus, and the files it
    read and edited."""
    wf = workflow(root)
    me = next((a for a in wf["agents"] if a["name"] == name), None)
    if me is None and (name.startswith("agent-") or name.startswith("sub:")):
        # a subagent the team lists (agent-<the start of its id>) though the board does not (it ran before this
        # plan, or beside it): its log from its recorded runs, its launch giving its task and prompt
        short = name.split("-", 1)[1] if name.startswith("agent-") else name[4:]
        hits = [a for a in wf["agents"] if str(a["name"]).startswith("sub:" + short)]
        if len(hits) == 1:
            me, name = hits[0], hits[0]["name"]
        else:
            ids_ = sorted({str(r.get("agent_id")) for r in runs(root, 0) if str(r.get("agent_id") or "").startswith(short)})
            if len(ids_) == 1:
                rs = [r for r in runs(root, 0) if r.get("agent_id") == ids_[0]]
                launch = next((r for r in reversed(rs) if r.get("event") == "launch"), {})
                done = next((r for r in reversed(rs) if r.get("event") == "stop"), None)
                start = next((r for r in rs if r.get("event") == "start"), {})
                me = {"name": "sub:" + ids_[0], "title": launch.get("description") or start.get("agent_type") or name,
                      "kind": "subagent", "type": start.get("agent_type") or "", "blocks": [], "waits_for": [],
                      "status": "done" if done else "running", "brief": launch.get("prompt") or ""}
                name = me["name"]
    if me is None:
        raise KeyError(name)
    data = _load(root)
    ids = [name[4:]] if name.startswith("sub:") else [i for i, n in data["ids"].items() if n == name]
    since = 0.0 if name.startswith("sub:") else float(wf.get("at") or 0)
    transcript = ""
    for r in runs(root, since):
        if r["agent_id"] in ids or r.get("agent_type") == name:
            transcript = r.get("transcript") or transcript
    entries = transcript_entries(transcript) if transcript else []
    blocks = set(me["blocks"])
    bus = [m for m in messages(root) if float(m.get("at") or 0) >= since and (
        m.get("from") == name or m.get("topic") in ({f"agent:{name}", "all"} | blocks))][-100:]
    files = [{"at": e["at"], "event": e.get("event"), "path": e.get("path")} for e in activity.events(root, 2000)
             if float(e.get("at") or 0) >= since and str(e.get("agent") or "") in ids][-200:]
    try:   # what it kept for a relaunch: its notes, its attempts, its last answer
        mine = memory(root).get(name) or {}
        tries = attempts(root).get(name, [])
        memo = {"notes": mine.get("notes") or [], "answer": mine.get("answer") or "",
                "attempts": [{k: a[k] for k in ("id", "started", "finished", "status")} for a in tries],
                "status": status_of(tries) if name in planned_names(root) else ""}
    except Exception:  # noqa: BLE001
        memo = {"notes": [], "answer": "", "attempts": [], "status": ""}
    return {"agent": me, "memory": memo,
            "brief": (data["agents"].get(name) or {}).get("brief", "") or me.get("brief", ""), "transcript": entries,
            "transcript_path": transcript, "messages": bus, "files": files}
