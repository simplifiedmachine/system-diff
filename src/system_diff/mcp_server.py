"""`system-diff mcp`: system-diff as an MCP server over stdio, for Claude Code and other MCP clients.

An optional extra (`pip install "system-diff[mcp]"`): the official MCP SDK is its only dependency. The tools read the
repository the client runs in, and write pictures under .git/system-diff/, which is never committed. They return the
exact text (headline, plan) for the model to explain from, and the picture's path for the person.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

try:  # the MCP SDK's 2.x name; 1.x called it FastMCP, with the same constructor, decorator and run()
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server

from . import activity, agents, config, graph, learn, pictures, textmap
from . import compare as cmp
from .extract import Code as CodeOf  # the spec's own Code, below, is a line of a picture
from .extract import extract
from .sources import GitSource, git_dir, repo_root

INSTRUCTIONS = (
    "system-diff shows what a change does to a system's structure: its parts, entry points, connections, outside "
    "systems and stored data. Use system_diff after changing the code, to tell the person in plain words what changed "
    "in the system and to point them at the picture; base 'checkpoint' is what they last marked as seen, 'turn' what "
    "this turn changed. Use explain when the person asks what a part is or does, or about a technology in it: it "
    "gives the facts to teach from and a diagram to paste in a code block. Use system_map for the whole system, and "
    "draw for a picture of something else (a process, a sequence). Call checkpoint only when the person says they "
    "have seen and understood the current state. For work that touches more than one part, plan a team with "
    "plan_agents and launch its agents in parallel as subagents; agents claim their blocks with claim, tell others "
    "about changes with publish (topics are block ids), and read what others published with inbox. objectives lists what "
    "the person has been optimising for (learned from their requests); when you evaluate or test one, report it with "
    "check_objective. When the person asks from the system-diff web page (their prompt says so, or starts with '[About: '), "
    "call point_to with the parts you are explaining so the page lights them in its diagram, and note to keep a "
    "short finding about a part beside it. The person sees the system-diff page beside the chat: read ui_view when "
    "they say 'this', 'here' or 'the one I picked'; ui_graph gives a view's diagram as data; while explaining, ui_show "
    "and point_to what you name. The diagrams are computed from the code and system-diff.toml: to rename, group or "
    "move a box, find its source with ui_source, preview with ui_config_preview, then edit the file. When the person "
    "says the diagram is wrong (a missing or extra box or connection, a wrong name or group), check it: ui_check lists "
    "what could be wrong and ui_source what is behind a box; compare with the code. When the config explains it, "
    "preview the fix with ui_config_preview and edit system-diff.toml; otherwise report_diagram_issue. Either way, "
    "tell the person what you found. A team can run "
    "in stages (plan_agents order=staged, or after=...): launch each stage as `how` says. When the person asks for "
    "another agent, add_agent (agent_kinds lists extensions' kinds and saved agents); save_workflow keeps a team to "
    "reuse with run_workflow; workflow and agent_log show who runs, who waits, and what each one did. Keys the person's message stored keep their names unless they ask; to rename one, rename_setting (a secret's value never passes through you); ask_for_secret only for a value not stored yet. When the person "
    "asks to keep or save an agent that ran, keep_agent saves it from its run. A project's "
    "settings (hosts, users, ports) and secrets (passwords, tokens) are in your environment by name: project_settings "
    "lists them, save_setting stores a setting the person gives you, and a secret's value is asked for with "
    "ask_for_secret (a password field), never taken from the chat. An agent of a plan calls agent_note (done, next, "
    "blockers) after each step and before it finishes. When a conversation with a plan is continued or resumed (after "
    "a stop), call resume_plan before relaunching anyone: it says which agents are done, which were interrupted or "
    "never launched, and gives each unfinished one a resume brief built from what it did; relaunch those with it."
)
WIDTH = 100  # the columns a diagram is drawn for


class Code(BaseModel):
    code: str = Field(description="shown in a monospace font, e.g. a route or a table")


Line = Union[str, Code]


class _Spec(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class Node(_Spec):
    id: str
    col: str = Field(description="the key of one of `columns`")
    title: str
    sub: str = ""
    type: Literal["component", "store", "host"] = "component"
    style: Literal["", "new", "legacy", "shared", "undeclared"] = ""
    lines: List[Line] = []


class Edge(_Spec):
    from_: str = Field(alias="from")
    to: str
    kind: Literal["call", "data", "network"] = "call"


class Stage(_Spec):
    title: str
    lines: List[Line] = []
    hi: bool = Field(False, description="highlight this stage")


class Loop(_Spec):
    from_: int = Field(alias="from", description="index of the stage it goes back from")
    to: int = Field(description="index of the stage it goes back to")
    label: str = ""


class Exit(_Spec):
    label: str
    note: str = ""


class Gate(_Spec):
    text: str = Field(description="where a person comes in, e.g. 'A person approves'")
    from_: Optional[List[int]] = Field(None, alias="from", description="indexes of the stages that lead to it")


class Lane(_Spec):
    id: str
    title: str
    sub: str = ""
    person: bool = False


class Step(_Spec):
    from_: Optional[str] = Field(None, alias="from", description="a lane id (a message step)")
    to: Optional[str] = Field(None, description="a lane id (a message step)")
    text: Optional[str] = Field(None, description="what is sent (a message step)")
    kind: Optional[Literal["call", "data", "network"]] = None
    note: Optional[str] = Field(None, description="a note across lanes instead of a message")
    over: Optional[List[str]] = Field(None, description="the lane ids a note spans")


class Spec(_Spec):
    """One picture. `map` needs columns and nodes (edges optional); `stages` needs stages; `sequence` needs lanes
    and steps."""
    picture: Literal["map", "stages", "sequence"]
    label: str = ""
    columns: Optional[List[List[str]]] = Field(None, description='map: [["logic", "Logic"], ["store", "Stored data"]]')
    nodes: Optional[List[Node]] = None
    edges: Optional[List[Edge]] = None
    stages: Optional[List[Stage]] = None
    loops: Optional[List[Loop]] = None
    exits: Optional[List[Exit]] = None
    gate: Optional[Gate] = None
    lanes: Optional[List[Lane]] = None
    steps: Optional[List[Step]] = None


_ROOTS: Dict[str, Path] = {}


def _root() -> Path:
    """The repository the client runs in: found in Python (no git process, which costs seconds on some machines),
    once per working directory; git is asked only when that finds none."""
    here = str(Path.cwd())
    got = _ROOTS.get(here)
    if got is None:
        from .agent import find_root
        got = _ROOTS[here] = find_root(Path(here)) or repo_root()
    return got


def _out(root: Path, *parts: str) -> Path:
    """A folder under .git/system-diff (the repository's own git dir, worktrees included)."""
    return git_dir(root, *parts)


def _via_engine(name: str, args: Dict[str, Any], here: Any) -> Dict[str, Any]:
    """A tool that reads the whole system: answered by the repository's running engine, which holds it (a moment);
    with none, read here, for a bounded time (a large repository on a slow machine takes minutes: never a hung
    tool)."""
    from . import uitools
    t = time.monotonic()
    got = uitools.engine_post(_root(), "tool", {"tool": name, "args": args}, timeout=90)
    out = got["result"] if isinstance(got, dict) and "result" in got else uitools.within(90, here, name)
    _note_slow(name, time.monotonic() - t, "engine" if isinstance(got, dict) and "result" in got else "here")
    return out


def _note_slow(tool: str, seconds: float, where: str) -> None:
    """A tool call that took more than 5 s, kept for the doctor (mcp-slow.log under .git/system-diff)."""
    if seconds < 5:
        return
    try:
        with open(git_dir(_root()) / "mcp-slow.log", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": round(time.time(), 3), "tool": tool, "seconds": round(seconds, 2),
                                "where": where}) + "\n")
    except OSError:
        pass


def tool_map(root: Path, ref: str = ".", diagram: bool = False) -> Dict[str, Any]:
    sha, label = cmp.resolve(root, ref)
    source = GitSource(sha, root)
    cfg = config.load(source, root)
    bp = extract(source, cfg)
    path = _out(root, "mcp") / "system.svg"
    pictures.write_if_changed(path, graph.system_svg(bp, cfg))
    comps = bp["components"]
    extra = {"diagram": textmap.plain(textmap.render_map(graph.overview(bp, cfg), WIDTH))} if diagram else {}
    return {"of": label, "picture": str(path), **extra,
            "parts": [{"id": k, "label": v["label"], "column": graph.part_kind(k, v, bp, cfg), "files": v["files"]}
                      for k, v in sorted(comps.items(), key=lambda kv: graph.part_order(kv[1], kv[0]))],
            "connections": [f"{comps.get(s, {}).get('label', s)} → {comps.get(t, {}).get('label', t)}"
                            for s, t in (k.split(">") for k in bp["flows"])],
            "entry_points": len(bp["routes"]), "tables": len(bp["tables"]),
            "outside_systems": [v["label"] for v in bp["externals"].values()],
            "read_but_not_deployed": [v["label"] for v in bp["dormant"].values()],
            "undeclared_parts": sorted(k for k, v in comps.items() if not v["declared"])}


def tool_diff(root: Path, base: str = "checkpoint", head: str = ".", diagram: bool = False) -> Dict[str, Any]:
    c = cmp.compare(root, base, head)
    picture = graph.diff_graph(c.diff, c.before, c.after, c.cfg)
    svg = pictures.map_svg(picture, diff=True, label="System diff") if picture else None
    path = _out(root, "mcp") / "diff.svg"
    if svg:
        pictures.write_if_changed(path, svg)
    elif path.is_file():
        path.unlink()
    extra = {"diagram": textmap.plain(textmap.render_map(picture, WIDTH))} if diagram and picture else {}
    return {"from": c.base, "to": c.head, "headline": c.diff["headline"], "plan": c.diff["plan"],
            "changes": len(c.diff["changes"]), "impacts": c.diff["impacts"],
            "picture": str(path) if svg else None, **extra,
            "live": "the System diff pane (/system-diff) or its web page (`system-diff watch`) redraw as the code changes"}


def tool_explain(root: Path, part: str = "", base: str = "checkpoint") -> Dict[str, Any]:
    c = cmp.compare(root, base, ".")
    parts = sorted(v["label"] for v in c.after["components"].values())
    if part:
        found = learn.find_part(c, part, root)
        if found is None:
            return {"error": f"no part called {part!r}", "parts": parts}
    else:
        now = activity.now(activity.events(root), CodeOf(c.cfg))
        found = now["part"] if now else None
        if found is None:
            return {"error": "no part has been read or edited yet: name one", "parts": parts}
    les = learn.lesson(c, found, GitSource(c.head_sha, root))
    hood = graph.neighbourhood(c.after, c.cfg, found, graph.change_marks(c.diff, c.before, c.after), c.before)
    return {"part": les["title"], "column": les["column"], "what": " ".join(les["what"]),
            "changing": [{"change": x["text"], "why": x["why"]} for x in les["changing"]],
            "topics": [{k: t[k] for k in ("title", "kind", "what", "here", "link")} for t in les["topics"]],
            "more_topics": les["more_topics"] + les["other_imports"], "since": c.base,
            "diagram": textmap.plain(textmap.render_zoom(hood, WIDTH))}


def tool_draw(root: Path, spec: Spec, name: str = "picture") -> Dict[str, Any]:
    data = spec.model_dump(by_alias=True, exclude_none=True)
    problems = pictures.check(data)
    if problems:
        return {"error": "the spec could not be drawn: " + "; ".join(problems), "do": "fix these and draw again"}
    try:
        svg = pictures.draw(data)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return {"error": f"the spec could not be drawn: {type(exc).__name__}: {exc}",
                "do": "check that every edge, loop, gate and step names an id or index the spec has"}
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name)[:60] or "picture"
    path = _out(root, "pictures") / f"{safe}.svg"
    pictures.write_if_changed(path, svg)
    return {"picture": str(path)}


def tool_objectives(root: Path) -> Dict[str, Any]:
    from . import objectives
    checks = objectives.checks(root)
    return {"objectives": [{"id": o["id"], "text": o["text"], "check": o.get("check", ""),
                            "parts": o.get("parts", []), "evidence": o.get("evidence", []),
                            "checks": checks.get(o["id"], [])} for o in objectives.current(root)],
            "statuses": list(objectives.STATUSES)}


def tool_check_objective(root: Path, agent: str, objective: str, status: str, note: str = "") -> Dict[str, Any]:
    from . import objectives
    try:
        return {"recorded": objectives.record_check(root, objective, agent, status, note)}
    except ValueError as exc:
        return {"error": str(exc)}


def tool_point_to(root: Path, parts: List[str], note: str = "") -> Dict[str, Any]:
    from . import chat
    if not isinstance(parts, list) or not any(isinstance(p, str) and p.strip() for p in parts):
        return {"error": "name at least one part, file, entry point or table"}
    rec = chat.point(root, parts, note)
    return {"pointing_at": rec["parts"], "note": rec["note"],
            "shown": "the system-diff web page lights these in its diagram now"}


def tool_note(root: Path, part: str, text: str) -> Dict[str, Any]:
    from . import chat
    try:
        rec = chat.add_note(root, part, text)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"noted": rec["text"], "part": rec["part"],
            "shown": "in the block's hover card and details on the web page, under Found while answering"}


def build() -> Server:
    server = Server("system-diff", instructions=INSTRUCTIONS)
    _threaded_tools(server)

    @server.tool()
    def system_map(ref: str = ".", diagram: bool = False) -> Dict[str, Any]:
        """The system's structure: its parts by column, the connections between them, how many entry points and
        tables, and its outside systems. `ref`: a commit, or "." for the working tree. Returns the picture's path;
        with `diagram`, also the map as text to show the person in a code block."""
        return _via_engine("map", {"ref": ref, "diagram": diagram}, lambda: tool_map(_root(), ref, diagram))

    @server.tool()
    def system_diff(base: str = "checkpoint", head: str = ".", diagram: bool = False) -> Dict[str, Any]:
        """What changed in the system from `base` to `head`: one sentence, the exact list (each line with its impact,
        e.g. new-interface, breaking-interface, data-schema-change, test-gap) and a picture of only what changed.
        `base` "checkpoint" is what the person last marked as seen (HEAD if nothing yet), "turn" the working tree
        when they sent their last prompt; `head` "." is the working tree, uncommitted files included. With
        `diagram`, also the picture as text for a code block. Explain it from the plan, never beyond it."""
        return _via_engine("diff", {"base": base, "head": head, "diagram": diagram},
                           lambda: tool_diff(_root(), base, head, diagram))

    @_read_only(server)
    def explain(part: str = "", base: str = "checkpoint") -> Dict[str, Any]:
        """Facts to teach the person one part of their system from: what it is and does, how it connects, what is
        changing in it since `base` and why that matters, topics worth knowing (the technologies its code imports
        and the ideas behind it, each with a link), and a diagram of the part zoomed in, to paste in a code block.
        `part`: its name or id, an outside system's name, or a file in it; empty for the part being worked on."""
        return _via_engine("explain", {"part": part, "base": base}, lambda: tool_explain(_root(), part, base))

    @server.tool()
    def draw(spec: Spec, name: str = "picture") -> Dict[str, Any]:
        """Draw a picture in system-diff's look: a `map` (boxes in columns), `stages` (a process left to right, with
        loops back, exits and where a person comes in) or a `sequence` (who talks to whom, in order). Returns the
        SVG's path, or what is wrong with the spec."""
        return tool_draw(_root(), spec, name)

    @server.tool()
    def checkpoint(ref: str = "") -> Dict[str, Any]:
        """Mark the working tree (or `ref`) as seen by the person, so the next system_diff from "checkpoint" shows
        only what is new to them. Only when the person says they have seen and understood the current state."""
        sha = cmp.checkpoint(_root(), ref or None)
        return {"checkpoint": sha[:9], "of": ref or "the working tree"}

    @server.tool()
    def plan_agents(task: str, most: int = 4, focus: List[str] = [], order: str = "parallel",  # noqa: B006
                    after: Dict[str, List[str]] = {}) -> Dict[str, Any]:  # noqa: B006
        """Split a task across a team of agents by the system's blocks: one agent per area the task touches (a
        container, else a part), at most `most`, each with the blocks it owns, the blocks it depends on (it is
        subscribed to them on the bus) and a brief to launch it with. `focus`: block ids to include. Claims the
        blocks. `order`: "parallel" (all at once) or "staged" (an agent whose blocks use another agent's starts after
        that one finishes); `after`: waits by name, {"web": ["api"]}. Each agent gets its stage; launch the agents of
        a stage together as subagents, each with its brief as its prompt, as `how` says. The person watches the
        workflow (who runs, who waits, each one's log) on the system-diff page's Agents tab."""
        root = _root()
        order = order if order in ("parallel", "staged") else "parallel"
        after = {str(k): [str(x) for x in v] for k, v in (after or {}).items() if isinstance(v, list)}
        from . import uitools
        # the running engine holds the system: it plans at once; else read here, for a bounded time
        got = uitools.engine_post(root, "agents/plan", {"task": task, "most": most, "focus": list(focus or []),
                                                        "order": order, "after": after}, timeout=45)
        if got is not None:
            return got
        return uitools.within(60, lambda: agents.plan(root, cmp.compare(root, "checkpoint", "."), task, most, focus,
                                                      order=order, after=after), "plan_agents")

    @server.tool()
    def add_agent(name: str, brief: str = "", blocks: List[str] = [], waits_for: List[str] = [],  # noqa: B006
                  area: str = "", kind: str = "") -> Dict[str, Any]:
        """Add an agent to the running team when the person asks for one ("add a migrations agent after the API
        one"): it claims `blocks`, waits for the agents or steps in `waits_for`, and gets a brief (yours, or its
        kind's: agent_kinds lists the kinds extensions and saved agents offer). Then launch it as `how` says."""
        return _safe(lambda: agents.add_agent(_root(), name, brief, blocks, waits_for, area, kind))

    @_read_only(server)
    def agent_kinds() -> Dict[str, Any]:
        """The kinds of agent add_agent can add here: extensions' (research-diff's experiment runner, section writer,
        contradiction reviewer...) and the person's saved agents."""
        return {"kinds": list(agents.agent_kinds(_root()).values())}

    @server.tool()
    def agent_note(agent: str, done: str = "", next: str = "", notes: str = "",  # noqa: A002 - the word agents use
                   blockers: str = "") -> Dict[str, Any]:
        """An agent of the plan says where it is, after each meaningful step and before it finishes: what is done,
        what is next, blockers, notes. Kept as its memory: if the session stops, whoever relaunches it (resume_plan)
        starts from here instead of from scratch. `agent`: your name in the plan."""
        return _safe(lambda: agents.agent_note(_root(), agent, done, next, notes, blockers))

    @_read_only(server)
    def resume_plan() -> Dict[str, Any]:
        """Resuming a plan after a stop (a continued or resumed conversation): each planned agent's status (done,
        running, interrupted, failed, not launched), its attempts and latest note, and for each one not done a
        resume_brief (its brief, what it did so far: notes, files edited, unread messages, last answer). `how` says
        which to relaunch together now: launch them with their resume briefs as their prompts."""
        return _via_engine("resume_plan", {}, lambda: agents.resume_plan(_root()))

    @_read_only(server)
    def workflow() -> Dict[str, Any]:
        """The team's workflow as it runs: its stages, and each agent and extension step with who it waits for and
        its status (queued, waiting, running, paused, done, failed), times, files read and edited, last action."""
        return _via_engine("workflow", {}, lambda: agents.workflow(_root()))

    @_read_only(server)
    def agent_log(name: str) -> Dict[str, Any]:
        """One agent's (or extension step's) session: its brief, what it said and did (its transcript), the messages it
        sent and got, the files it touched. What the person sees on clicking it in the workflow."""
        root = _root()
        try:
            return agents.agent_log(root, name)
        except KeyError:
            try:
                return agents.step_log(root, name)
            except KeyError:
                return {"error": f"no agent or step {name!r} in the workflow"}

    @server.tool()
    def workflow_action(step: str, action: str) -> Dict[str, Any]:
        """Pause, resume, stop or retry an extension's step on the workflow (research-diff's experiment run), when the
        person asks and the step offers it."""
        try:
            return agents.step_action(_root(), step, action)
        except KeyError:
            return {"error": f"no step {step!r} in the workflow"}
        except ValueError as exc:
            return {"error": str(exc)}

    @server.tool()
    def save_workflow(name: str, description: str = "", scope: str = "project") -> Dict[str, Any]:
        """Save the team's current workflow (its agents, areas, order, and extensions' steps) to use again for later
        tasks: scope "project" (this repository, shared through git), a kind of project ("research", "software"),
        or "everywhere". Say what you saved."""
        from . import workflows
        return _safe(lambda: workflows.save(_root(), name, description, scope))

    @_read_only(server)
    def workflows() -> Dict[str, Any]:
        """The workflows saved for this project (its own, the person's for its kind or everywhere) and those
        extensions offer here, each with its agents, steps and stages."""
        from . import workflows as wf
        return {"workflows": [wf.summary(w) for w in wf.listing(_root())]}

    @server.tool()
    def run_workflow(name: str, task: str, most: int = 8) -> Dict[str, Any]:
        """Plan a saved workflow's team for a new task (the same agents, areas and order); then launch as `how`
        says."""
        from . import workflows
        root = _root()
        from . import uitools
        got = uitools.engine_post(root, "agents/plan", {"workflow": name, "task": task, "most": most}, timeout=45)
        if got is not None:
            return got
        return uitools.within(60, lambda: _safe(lambda: workflows.run(root, cmp.compare(root, "checkpoint", "."), name,
                                                                       task, most)), "run_workflow")

    @server.tool()
    def claim(agent: str, blocks: List[str]) -> Dict[str, Any]:
        """Take blocks (part ids) for `agent` (its name in the plan), so other agents keep out of them. Returns the
        blocks another agent had."""
        return {"agent": agent, "taken_from_others": agents.claim(_root(), agent, blocks)}

    @server.tool()
    def publish(agent: str, topic: str, text: str) -> Dict[str, Any]:
        """Tell the other agents something, async: on a block's topic (its id: those working on what depends on it
        read it with their next tool result), "all", or "agent:<name>" for one agent. Changes to a block's structure
        are published by system-diff itself; publish plans, contracts and questions."""
        return agents.publish(_root(), topic, text, sender=agent)

    @server.tool()
    def subscribe(agent: str, topics: List[str]) -> Dict[str, Any]:
        """Follow more topics (block ids). An agent already follows its own blocks and the blocks they depend on."""
        return {"agent": agent, "topics": agents.subscribe(_root(), agent, topics)}

    @server.tool()
    def inbox(agent: str = "main") -> Dict[str, Any]:
        """`agent`'s unread messages from the other agents (and marks them read)."""
        return {"agent": agent, "messages": agents.inbox(_root(), agent)}

    @_read_only(server)
    def objectives() -> Dict[str, Any]:
        """What the person has been optimising for in this repository (learned from their repeated requests), each
        with how a change could break it ("check") and the latest check of each agent. Read it before changing or
        reviewing code, and keep these objectives."""
        return tool_objectives(_root())

    @server.tool()
    def check_objective(agent: str, objective: str, status: str, note: str = "") -> Dict[str, Any]:
        """Report what you found about one of the person's objectives (an id from the objectives tool) whenever you
        evaluate or test it: after running tests, measuring, or reviewing a change against it. status: "checking"
        (you started), "holds", "broken" or "unclear"; note: one short line of evidence (what you ran or saw).
        `agent` is your name in the team plan (or "main"). The person sees it, and the other agents get it on the bus
        (topic "objective:<id>")."""
        return tool_check_objective(_root(), agent, objective, status, note)

    @server.tool()
    def point_to(parts: List[str], note: str = "") -> Dict[str, Any]:
        """Point at parts of the system on the person's system-diff web page: it lights them in its diagram at once,
        scrolls to them and shows "Claude is pointing at: …" beside your answer. Use it whenever you explain parts
        of the system to the person (above all when the question came from the web page), as you name each part.
        `parts`: blocks by their readable names or ids (parts, containers, outside systems, actors), files of the
        system (paths), entry points ("POST /chat") or table names; at most 12. `note`: one short line on why
        (optional). Each call replaces the last pointing."""
        return tool_point_to(_root(), parts, note)

    @server.tool()
    def note(part: str, text: str) -> Dict[str, Any]:
        """Keep a short finding (at most ~300 characters) about one block of the system, discovered while answering
        the person: something not obvious from the structure (a hidden coupling, a surprising behaviour, a risk). It
        shows on the web page in the block's hover card and details under "Found while answering", with the
        question it came from; the person can clear it. `part`: the block's name or id, or a file in it. Never put
        secrets in it (they are masked anyway)."""
        return tool_note(_root(), part, text)

    @_read_only(server)
    def ui_view(project: str = "") -> Dict[str, Any]:
        """What the person is looking at now on the system-diff page (and pane): per open page the project, view, part,
        the block they picked or hover (its title and headline), whether the page is visible; and the views there
        are. Look here when they say "this", "here", "the one I picked"."""
        from . import uitools
        return _safe(lambda: uitools.view(_root(), project))

    @_read_only(server)
    def ui_graph(view: str = "now", part: str = "", project: str = "", limit: int = 60) -> Dict[str, Any]:
        """A view's diagram as data (system-diff's views and every extension's): nodes (id, title, sub, status, type,
        column), edges (from, to, kind, status, label), each block's details, what each zooms into, and the view's
        sections as text. Changed, picked and worked-on blocks first; `limit` nodes at most."""
        from . import uitools
        return _safe(lambda: uitools.graph(_root(), view, part, project, limit))

    @_read_only(server)
    def ui_source(block: str, project: str = "") -> Dict[str, Any]:
        """Where a block of the diagram comes from, to change the right thing: the files behind it and the
        system-diff.toml entry that names or places it (or the one to add). The diagram is computed from these:
        change them, never claim a change the page does not show."""
        from . import uitools
        return _safe(lambda: uitools.source(_root(), block, project))

    @_read_only(server)
    def ui_config_preview(changes: str, view: str = "now", part: str = "", project: str = "") -> Dict[str, Any]:
        """The diagram an edit of system-diff.toml would give, and how it differs from now, without writing anything.
        `changes`: a TOML snippet as it would go in the file (a [[components]] entry with the same id changes that
        entry: label renames a box, paths move files into it, layer or container group it). Then edit the file."""
        from . import uitools
        return _safe(lambda: uitools.config_preview(_root(), changes, view, part, project))

    @_read_only(server)
    def ui_check(part: str = "", view: str = "now", project: str = "") -> Dict[str, Any]:
        """What could be wrong in the diagram (or one part's), checked against the code: imports that reach no part
        (a path alias or source folder the config misses), code counted in no part, parts with no connections,
        outside systems drawn from setting names only, config entries that match nothing, a read still under way.
        Each finding: what, where (files and lines), why, and a fix: a system-diff.toml snippet for
        ui_config_preview, or what system-diff does not understand (then report_diagram_issue)."""
        from . import uitools
        return _safe(lambda: uitools.check(_root(), part, view, project))

    @server.tool()
    def report_diagram_issue(part: str, wrong: str, expected: str, evidence: str = "",
                             project: str = "") -> Dict[str, Any]:
        """Keep a diagram problem system-diff.toml cannot fix (system-diff misreads the code) and get a report to
        show the person, ready to paste to system-diff's maintainers: the version, what was drawn, what was expected,
        and the few lines `evidence` names ("path:line" or "path:from-to"), secrets masked. Kept under
        .git/system-diff/diagram-issues.jsonl."""
        from . import uitools
        return _safe(lambda: uitools.report_issue(_root(), part, wrong, expected, evidence, project))

    @server.tool()
    def ui_show(view: str, part: str = "", pick: str = "", note: str = "", project: str = "") -> Dict[str, Any]:
        """Move the person's system-diff page (and pane) to a view, zoomed into `part`, with `pick` picked and `note`
        on a banner that offers Back. Use it while explaining, as each block is named. Changes only what the page
        shows; a page the person is busy on takes it when they pause, or offers it if they said so."""
        from . import uitools
        return _safe(lambda: uitools.show(_root(), view, part, pick, note, project))

    @_read_only(server)
    def saved_agents() -> Dict[str, Any]:
        """The subagents saved for this repository and for the person (name, scope, description, how often planned
        and finished), and the roles worth keeping: planned or finished twice, or a standing role (reviewer, tester)
        that finished once, not saved yet."""
        from . import agentlib
        return agentlib.library(_root())

    @server.tool()
    def save_agent(name: str, description: str, prompt: str, tools: str = "", model: str = "",
                   scope: str = "project") -> Dict[str, Any]:
        """Save a role that keeps coming back as a Claude Code subagent. scope: "project" (this repository's
        .claude/agents, shared through git), a kind of project ("research", "software": the person's library, given
        to every project of that kind), or "everywhere". saved_agents says which scope each suggestion fits. Its
        description says when to use it, its prompt is its standing instructions. Say what you saved."""
        from . import agentlib
        return _safe(lambda: agentlib.save(name, description, prompt, tools, model, scope, root=_root()))

    @server.tool()
    def keep_agent(which: str, name: str = "", scope: str = "project", description: str = "") -> Dict[str, Any]:
        """Keep a subagent that ran (or runs) in this project as an agent, when the person asks to save or keep one:
        `which` is its description's words ("the survey agent"), its role, or its id ("sub:<id>"). Its prompt
        becomes the agent's standing instructions. scope: "project", "kind" (the person's library, for projects of
        this kind) or "everywhere". When several match, it lists them: ask the person which one."""
        from . import agentlib
        return _safe(lambda: agentlib.keep_ran(_root(), which, name, scope, description))

    @server.tool()
    def install_agents(names: List[str]) -> Dict[str, Any]:
        """Copy library agents (of this project's kinds) into this repository's .claude/agents/, so terminal sessions
        here get them too. Only when the person asks: it writes files in their repository."""
        from . import agentlib
        return _safe(lambda: agentlib.install(_root(), names))

    @server.tool()
    def save_skill(name: str, description: str, steps: str, scope: str = "project") -> Dict[str, Any]:
        """Save a procedure an agent keeps repeating (how this repository runs its tests, how it releases) as a
        Claude Code skill (.claude/skills/<name>/SKILL.md). Say what you saved."""
        from . import agentlib
        return _safe(lambda: agentlib.save_skill(_root(), name, description, steps, scope))

    @_read_only(server)
    def team() -> Dict[str, Any]:
        """The plan, who claims which block, and the latest messages on the bus."""
        root = _root()
        st = agents.state(root)
        return {"plan": st["plan"], "claims": st["claims"], "messages": agents.messages(root)[-20:],
                "workflow": agents.workflow(root)}

    # ── a project's settings and secrets (vault.py): what sessions get as environment variables ──
    def _project(project: str) -> Path:
        if not project:
            return _root()
        p = Path(project).expanduser()
        if not p.is_dir():
            raise ValueError(f"{project} is not a folder")
        return p.resolve()

    @_read_only(server)
    def project_settings(project: str = "") -> Dict[str, Any]:
        """The settings and secrets stored for this project (and for all projects), which Studio sessions get as
        environment variables: name, kind (setting or secret), group ("staging server"), description, scope; a
        setting's value (a host, a user, a port: not secret), never a secret's."""
        from . import vault
        return _safe(lambda: {"entries": vault.listing(_project(project)), "store": vault.store_name(),
                              "use": "by name: $NAME in bash, $env:NAME in PowerShell (sessions started from now on)"})

    @server.tool()
    def save_setting(name: str, value: str, group: str = "", description: str = "", scope: str = "project",
                     project: str = "") -> Dict[str, Any]:
        """Store or change a project setting that is not secret (a server's host or IP, a user name, a port, a URL),
        e.g. STAGING_HOST under group "staging server". scope: "project" or "all" (every project). Refused for anything
        that looks secret (a password, a token, a key): those go through ask_for_secret, never through you."""
        from . import vault
        return _safe(lambda: {**vault.save(name, value, _project(project), scope in ("all", "*", "everywhere"),
                                           description, kind="setting", group=group),
                              "note": "Studio sessions started from now on get it; an open one after a restart"})

    @server.tool()
    def rename_setting(old: str, new: str, scope: str = "project", project: str = "") -> Dict[str, Any]:
        """Rename a stored setting or secret (GPU_HOST → A100_GPU_HOST): its value moves inside the key store, a
        secret's without passing through you, so never ask the person for a stored secret again to rename it. Keep the
        names the person's own message stored unless they ask for others."""
        from . import vault
        return _safe(lambda: vault.rename(old, new, _project(project), scope in ("all", "*", "everywhere")))

    @server.tool()
    def forget_setting(name: str, scope: str = "project", project: str = "") -> Dict[str, Any]:
        """Remove a stored setting (or secret) by name, from this project ("project") or all projects ("all")."""
        from . import vault
        return _safe(lambda: vault.forget(name, _project(project), scope in ("all", "*", "everywhere")))

    @server.tool()
    def ask_for_secret(name: str, description: str = "", group: str = "", scope: str = "project",
                       project: str = "") -> Dict[str, Any]:
        """Ask the person for a secret's value (a server's password, a token): in the Studio, a card with a password
        field above the message box; elsewhere, the command they run in a terminal. The value never passes through
        you or the chat; sessions started afterwards get it as $NAME. Only for a value not stored yet: a stored one is
        renamed with rename_setting, never asked for again."""
        from . import vault

        def go() -> Dict[str, Any]:
            vault.check_name(name)
            root = _project(project)
            cmd = f"system-diff secrets set {name}" + (" --all" if scope in ("all", "*") else f' --project "{root}"')
            asked = _ask_studio({"name": name, "description": description, "group": group, "scope": scope,
                                 "project": str(root)})
            if asked:
                return {**asked, "next": "wait for the person to store it; then it is $" + name + " in sessions started "
                                         "afterwards (the Studio offers to restart the open one)", "or": cmd}
            return {"asked": False, "tell_the_person": f"run `{cmd}` in a terminal (the value is asked for, never shown)"}
        return _safe(go)

    from . import extensions
    off = {n.strip() for n in os.environ.get("SYSTEM_DIFF_EXT_TOOLS_OFF", "").split(",") if n.strip()}
    for ext in extensions.load():                  # the extensions' own tools (research-diff's, …)
        add = getattr(ext, "mcp", None)
        if add is not None and str(getattr(ext, "NAME", "")) not in off:   # off: its own server brings them
            try:
                add(server)
            except Exception as exc:  # noqa: BLE001
                extensions.failed(getattr(ext, "NAME", "extension") + " tools", exc)
    return server


def _ask_studio(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Put a card asking for a secret in the running Studio (its token from app.json); None when no Studio runs."""
    import http.client
    import json as _json
    try:
        from . import app
        have = app.running()
    except Exception:  # noqa: BLE001
        have = None
    if not have:
        return None
    conn = http.client.HTTPConnection("127.0.0.1", int(have["port"]), timeout=5)
    try:
        conn.request("POST", "/api/secrets/ask", body=_json.dumps(body),
                     headers={"X-Token": str(have.get("token") or ""), "Content-Type": "application/json",
                              "Host": f"127.0.0.1:{have['port']}"})
        r = conn.getresponse()
        got = _json.loads(r.read().decode("utf-8") or "{}")
        return got if r.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def _safe(fn) -> Dict[str, Any]:
    """A tool's answer, or what went wrong as its answer (a bad project name, an engine away)."""
    try:
        return fn()
    except ValueError as exc:
        return {"error": str(exc)}


def _threaded_tools(server) -> None:
    """Every tool runs in a worker thread: one that takes long (a large repository read here) never holds the others
    up. The MCP SDK 1.x runs a plain function in its one event loop, so a slow call made every later one wait, and
    Claude's resume_plan or inbox sat "running" behind it; 2.x threads them itself, which this keeps."""
    import functools
    import inspect
    register = server.tool

    def tool(*a: Any, **k: Any):
        deco = register(*a, **k)

        def wrap(fn):
            if inspect.iscoroutinefunction(fn):
                return deco(fn)

            @functools.wraps(fn)   # the same name, arguments and description (the signature follows __wrapped__)
            async def run(*args: Any, **kwargs: Any) -> Any:
                import anyio.to_thread
                return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
            deco(run)
            return fn
        return wrap
    server.tool = tool


def _read_only(server):
    """@server.tool(), marked as a tool that only reads (it changes nothing, here or elsewhere): permission modes can
    run it without asking. Plain @server.tool() with an MCP SDK too old for annotations."""
    try:
        from mcp.types import ToolAnnotations
        hints = ToolAnnotations.model_validate({"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                                                "openWorldHint": False})   # the wire names: 1.x and 2.x take them
        return server.tool(annotations=hints)
    except (ImportError, TypeError):
        return server.tool()


def main() -> None:
    """Serve over stdio (cli._mcp adds the startup that never exits: logged, and served as an error tool)."""
    build().run("stdio")
