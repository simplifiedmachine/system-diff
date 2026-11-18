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


def _load(root: Path) -> Dict[str, Any]:
    try:
        data = json.loads((git_dir(root) / STATE).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {"plan": data.get("plan") or {}, "claims": data.get("claims") or {},
                    "agents": data.get("agents") or {}, "ids": data.get("ids") or {},
                    "warned": data.get("warned") or {}}
    except (OSError, ValueError):
        pass
    return {"plan": {}, "claims": {}, "agents": {}, "ids": {}, "warned": {}}


def _save(root: Path, data: Dict[str, Any]) -> None:
    activity._replace(git_dir(root) / STATE, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def state(root: Path) -> Dict[str, Any]:
    return _load(root)


def _agent(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    return data["agents"].setdefault(name, {"topics": [], "read": 0, "blocks": [], "brief": ""})


def name_of(root: Path, agent_id: str) -> str:
    """The name an agent goes by: its plan name once tied to one, else "main" or a short form of its id."""
    if not agent_id or agent_id == MAIN:
        return MAIN
    return _load(root)["ids"].get(agent_id) or "agent-" + agent_id[:6]


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
        how = ("Launch these agents now, in parallel, each as its own subagent with its brief as the prompt (one "
               "message with several Task/Agent tool calls). Do not do their work yourself. When they finish, read the "
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
        how = ("Run these agents in stages, each as its own subagent with its brief as the prompt; launch the agents of "
               "one stage together (one message with several Task/Agent tool calls), and the next stage when the "
               "agents it waits for have finished: " + "; ".join(steps) + ". Do not do their work yourself. At the "
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


def record_run(root: Path, event: str, agent_id: str, agent_type: str = "", transcript: str = "") -> None:
    """A subagent started or stopped (the SubagentStart and SubagentStop hooks)."""
    if not agent_id or event not in ("start", "stop"):
        return
    line = json.dumps({"at": round(time.time(), 3), "event": event, "agent_id": agent_id,
                       "agent_type": agent_type or "", "transcript": transcript or ""}, ensure_ascii=False)
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
        "Finish with a short summary of what you changed in the system's structure."
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
    if not agents_:
        return {"task": "", "agents": [], "stages": []}
    since = float(pl.get("at") or 0)
    ids = dict(data["ids"])
    names = {g["name"] for g in agents_}
    info: Dict[str, Dict[str, Any]] = {g["name"]: {"started": 0.0, "finished": 0.0, "transcript": "", "ids": [],
                                                   "edits": 0, "reads": 0, "last": "", "last_at": 0.0}
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
    for g in agents_:
        i = info[g["name"]]
        waits = list(g.get("waits_for") or [])
        if i["finished"]:
            status = "done"
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
                    "added": bool(g.get("added")), "has_log": bool(i["transcript"] or i["ids"])})
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
    done = {a["name"] for a in out if a["status"] == "done"} | {k for k, v in steps.items() if v["status"] == "done"}
    for a in out:
        a["waiting_on"] = [w for w in a["waits_for"] if w not in done]
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
    top = max([a["stage"] for a in out] + [v["stage"] for v in steps.values()] or [1])
    stages = [[a["name"] for a in out if a["stage"] == n] + [k for k, v in steps.items() if v["stage"] == n]
              for n in range(1, top + 1)]
    return {"task": pl.get("task") or "", "at": since, "order": pl.get("order") or "parallel", "agents": out,
            "steps": list(steps.values()), "stages": [s for s in stages if s]}


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
    if me is None:
        raise KeyError(name)
    data = _load(root)
    ids = [i for i, n in data["ids"].items() if n == name]
    since = float(wf.get("at") or 0)
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
    return {"agent": me, "brief": (data["agents"].get(name) or {}).get("brief", ""), "transcript": entries,
            "transcript_path": transcript, "messages": bus, "files": files}
