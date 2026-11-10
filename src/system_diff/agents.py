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
    since = float(p.get("at") or 0)
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


def plan(root: Path, c, task: str, most: int = MOST_AGENTS, focus: Iterable[str] = ()) -> Dict[str, Any]:
    """Split `task` across agents by the system's blocks: the areas (containers, else parts) the task names or that
    `focus` gives, one agent each (at most `most`, the rest joining the closest), each with the blocks it claims, the
    blocks it depends on (its subscriptions) and a brief; and how to run them. Saved, and the claims made."""
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
    for a in rest:   # joins the picked area it is most connected to
        best = max(picked, key=lambda p: sum(1 for b in a["blocks"] for d in deps.get(b, ()) if d in p["blocks"])
                   + sum(1 for b in p["blocks"] for d in deps.get(b, ()) if d in a["blocks"]))
        best["blocks"] += a["blocks"]
    agents = []
    owned = {b: a["id"] for a in picked for b in a["blocks"]}
    for a in picked:
        name = re.sub(r"[^a-z0-9]+", "-", a["title"].lower()).strip("-") or "agent"
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
        agents.append({"name": name, "area": a["title"], "blocks": a["blocks"], "subscribes": needs,
                       "talks_to": talks_to, "brief": brief})
    how = ("Launch these agents now, in parallel, each as its own subagent with its brief as the prompt (one message "
           "with several Task/Agent tool calls). Do not do their work yourself. When they finish, read the system-diff "
           "`inbox` tool as \"main\" and integrate: resolve what they published, then check the whole with "
           "`system_diff`." if len(agents) > 1 else
           "One area: do it yourself; no team needed.")
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
    data["plan"] = {"task": task, "at": at, "agents": [{k: v for k, v in g.items() if k != "brief"} for g in agents]}
    for g in agents:
        a = _agent(data, g["name"])
        a["brief"] = g["brief"]
        a["blocks"] = list(g["blocks"])
        a["topics"] = sorted(set(g["subscribes"]) | {f"agent:{g['name']}"})
        a["read"] = last
        for b in g["blocks"]:
            data["claims"][b] = g["name"]
    _save(root, data)
    return {"task": task, "agents": agents, "how": how}


ORCHESTRATE = ("system-diff: this repository works with a team of agents. Before you start on a task that touches "
               "more than one part of the system, call the system-diff `plan_agents` tool with the task; when it "
               "returns two or more agents, launch them in parallel as subagents with their briefs and coordinate "
               "through its bus (`inbox`, `publish`) instead of doing every part yourself. Small, single-part tasks "
               "need no team.")
