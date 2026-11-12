"""`system-diff mcp`: system-diff as an MCP server over stdio, for Claude Code and other MCP clients.

An optional extra (`pip install "system-diff[mcp]"`): the official MCP SDK is its only dependency. The tools read the
repository the client runs in, and write pictures under .git/system-diff/, which is never committed. They return the
exact text (headline, plan) for the model to explain from, and the picture's path for the person.
"""
from __future__ import annotations

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
    "check_objective."
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


def _out(root: Path, *parts: str) -> Path:
    """A folder under .git/system-diff (the repository's own git dir, worktrees included)."""
    return git_dir(root, *parts)


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
        found = activity.now(activity.events(root), CodeOf(c.cfg))["part"]
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


def build() -> Server:
    server = Server("system-diff", instructions=INSTRUCTIONS)

    @server.tool()
    def system_map(ref: str = ".", diagram: bool = False) -> Dict[str, Any]:
        """The system's structure: its parts by column, the connections between them, how many entry points and
        tables, and its outside systems. `ref`: a commit, or "." for the working tree. Returns the picture's path;
        with `diagram`, also the map as text to show the person in a code block."""
        return tool_map(repo_root(), ref, diagram)

    @server.tool()
    def system_diff(base: str = "checkpoint", head: str = ".", diagram: bool = False) -> Dict[str, Any]:
        """What changed in the system from `base` to `head`: one sentence, the exact list (each line with its impact,
        e.g. new-interface, breaking-interface, data-schema-change, test-gap) and a picture of only what changed.
        `base` "checkpoint" is what the person last marked as seen (HEAD if nothing yet), "turn" the working tree
        when they sent their last prompt; `head` "." is the working tree, uncommitted files included. With
        `diagram`, also the picture as text for a code block. Explain it from the plan, never beyond it."""
        return tool_diff(repo_root(), base, head, diagram)

    @server.tool()
    def explain(part: str = "", base: str = "checkpoint") -> Dict[str, Any]:
        """Facts to teach the person one part of their system from: what it is and does, how it connects, what is
        changing in it since `base` and why that matters, topics worth knowing (the technologies its code imports
        and the ideas behind it, each with a link), and a diagram of the part zoomed in, to paste in a code block.
        `part`: its name or id, an outside system's name, or a file in it; empty for the part being worked on."""
        return tool_explain(repo_root(), part, base)

    @server.tool()
    def draw(spec: Spec, name: str = "picture") -> Dict[str, Any]:
        """Draw a picture in system-diff's look: a `map` (boxes in columns), `stages` (a process left to right, with
        loops back, exits and where a person comes in) or a `sequence` (who talks to whom, in order). Returns the
        SVG's path, or what is wrong with the spec."""
        return tool_draw(repo_root(), spec, name)

    @server.tool()
    def checkpoint(ref: str = "") -> Dict[str, Any]:
        """Mark the working tree (or `ref`) as seen by the person, so the next system_diff from "checkpoint" shows
        only what is new to them. Only when the person says they have seen and understood the current state."""
        sha = cmp.checkpoint(repo_root(), ref or None)
        return {"checkpoint": sha[:9], "of": ref or "the working tree"}

    @server.tool()
    def plan_agents(task: str, most: int = 4, focus: List[str] = []) -> Dict[str, Any]:  # noqa: B006
        """Split a task across a team of agents by the system's blocks: one agent per area the task touches (a
        container, else a part), at most `most`, each with the blocks it owns, the blocks it depends on (it is
        subscribed to them on the bus) and a brief to launch it with. `focus`: block ids to include. Claims the
        blocks. Then launch the agents in parallel as subagents, each with its brief as its prompt."""
        root = repo_root()
        c = cmp.compare(root, "checkpoint", ".")
        return agents.plan(root, c, task, most, focus)

    @server.tool()
    def claim(agent: str, blocks: List[str]) -> Dict[str, Any]:
        """Take blocks (part ids) for `agent` (its name in the plan), so other agents keep out of them. Returns the
        blocks another agent had."""
        return {"agent": agent, "taken_from_others": agents.claim(repo_root(), agent, blocks)}

    @server.tool()
    def publish(agent: str, topic: str, text: str) -> Dict[str, Any]:
        """Tell the other agents something, async: on a block's topic (its id: those working on what depends on it
        read it with their next tool result), "all", or "agent:<name>" for one agent. Changes to a block's structure
        are published by system-diff itself; publish plans, contracts and questions."""
        return agents.publish(repo_root(), topic, text, sender=agent)

    @server.tool()
    def subscribe(agent: str, topics: List[str]) -> Dict[str, Any]:
        """Follow more topics (block ids). An agent already follows its own blocks and the blocks they depend on."""
        return {"agent": agent, "topics": agents.subscribe(repo_root(), agent, topics)}

    @server.tool()
    def inbox(agent: str = "main") -> Dict[str, Any]:
        """`agent`'s unread messages from the other agents (and marks them read)."""
        return {"agent": agent, "messages": agents.inbox(repo_root(), agent)}

    @server.tool()
    def objectives() -> Dict[str, Any]:
        """What the person has been optimising for in this repository (learned from their repeated requests), each
        with how a change could break it ("check") and the latest check of each agent. Read it before changing or
        reviewing code, and keep these objectives."""
        return tool_objectives(repo_root())

    @server.tool()
    def check_objective(agent: str, objective: str, status: str, note: str = "") -> Dict[str, Any]:
        """Report what you found about one of the person's objectives (an id from the objectives tool) whenever you
        evaluate or test it: after running tests, measuring, or reviewing a change against it. status: "checking"
        (you started), "holds", "broken" or "unclear"; note: one short line of evidence (what you ran or saw).
        `agent` is your name in the team plan (or "main"). The person sees it, and the other agents get it on the bus
        (topic "objective:<id>")."""
        return tool_check_objective(repo_root(), agent, objective, status, note)

    @server.tool()
    def team() -> Dict[str, Any]:
        """The plan, who claims which block, and the latest messages on the bus."""
        root = repo_root()
        st = agents.state(root)
        return {"plan": st["plan"], "claims": st["claims"], "messages": agents.messages(root)[-20:]}

    return server


def main() -> None:
    build().run("stdio")
