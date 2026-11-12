"""Claude Code: `system-diff hook`, which its hooks run (when a prompt is sent, after Claude reads or edits a file, and
when it finishes a turn), and `system-diff setup claude`, which wires them in with the status line (statusline.py), a
short skill telling the agent when to show and explain the system, and the MCP server.

The hooks never fail a turn. On a prompt they keep the working tree as the start of the turn; after a read they note
the file, in the background, so the terminal view and the status line follow the part being worked on; after an edit
that changes the system's structure they say in one line what it changed and why it matters (Claude Code waits for
that hook: only then can it show the line); and at the end of a turn they say what the turn changed in the system, or
which parts it edited when the structure stayed the same. Everything setup changes is printed; `--undo` takes it back.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import activity, agents, config, design, learn
from .compare import compare_worktree, mark_turn, turn_sha
from .extract import Code
from .sources import git, run_hidden

NAME = "system-diff"
SKILL_MARK = "<!-- written by system-diff setup claude -->"
# The skill is named apart from the tool: `/system-diff` is the System diff pane's command (mods/system-diff).
SKILL_NAME, OLD_SKILL_NAME = "system-diff-explain", "system-diff"
SKILL = f"""---
name: system-diff-explain
description: Shows the person what a code change does to the system's structure (parts, endpoints, connections, settings, tables, outside systems) and teaches them the parts and technologies involved. Use after changing code that may change the structure, before asking them to review, when they ask what changed, or when they ask what a part of the system is or does.
---
{SKILL_MARK}

# Showing the system diff

After a change that may touch the system's structure:

1. Call the `system_diff` MCP tool, or run `system-diff diff --base checkpoint --head . --out .git/system-diff/latest`.
   With base `turn`, it shows only what this turn changed.
2. Tell the person, in two or three plain sentences built only from its plan, what changed in the system and why it
   matters: an endpoint added or removed, a new connection between parts, a new outside system, changed stored data,
   or code changed without tests.
3. Point them at the picture: the path the tool returns, the System diff pane beside the chat (`/system-diff`), or
   its web page (`system-diff watch`), which redraw as you work.
4. When they say they have seen it, call the `checkpoint` tool (or run `system-diff checkpoint`), so the next diff
   shows only what is new to them.

# Teaching the system

When the person asks what a part is or does, how it fits, or what a technology in it is, call the `explain` tool with
the part (empty for the one you are working on) and answer from what it returns: what the part is, what is changing in
it and why that matters, and the topics worth knowing, with their links. To show them, paste its `diagram` in a code
block. Keep it short and plain.

Never mention a part, endpoint, table or system that is not in the plan or in what `explain` returns. For a picture of
something else (a process, a sequence of calls), use the `draw` tool.
"""

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
READ_TOOLS = ("Read",)


def _is_repo(path: Path) -> bool:
    try:
        return git("rev-parse", "--is-inside-work-tree", cwd=path).strip() == "true"
    except (OSError, subprocess.CalledProcessError):
        return False


def _clip(text: Any, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def hook_message(since, turn=None, edited: List[str] = ()) -> Optional[str]:
    """One line on what changed in the system: when the turn's start is known, what this turn did, said plainly with
    why it matters, or which parts it edited without changing the structure (None when it edited no code); else
    everything since the checkpoint."""
    if turn is None:
        if not since.diff["changes"]:
            return None
        return f"System diff since {since.base}: {since.diff['headline']} (`system-diff watch` shows the picture.)"
    told = learn.story(turn.diff)
    if not told:
        return (f"System diff, this turn: Claude edited {learn.and_list(edited)}; nothing in the system's structure "
                "changed." if edited else None)
    top = told[0]
    why = f" ({top['why']})" if top["why"] else ""
    more = f" And {len(told) - 1} more change{'' if len(told) == 2 else 's'}." if len(told) > 1 else ""
    return f"System diff, this turn: {top['text'][:-1]}{why}.{more} The System diff pane explains them."


def step_message(before: Iterable[str], after: List[Dict[str, Any]]) -> Optional[str]:
    """What one edit did to the system: the turn's story now, less what it told before the edit."""
    told = set(before)
    new = [s for s in after if s["text"] not in told]
    return f"System diff: {learn.one_line(new)}" if new else None


def edited_this_turn(root: Path, cfg: Dict[str, Any]) -> List[str]:
    """The parts whose code the agent edited since the last prompt, in the order it first did."""
    code, out, seen = Code(cfg), [], set()
    for e in activity.events(root):
        if e.get("event") == activity.PROMPT:
            out, seen = [], set()
        elif e.get("event") == activity.EDIT:
            part = activity.part_of(e.get("path", ""), code)
            if part and part not in seen:
                seen.add(part)
                out.append(activity.label(cfg, part))
    return out


def start_web(root: Path, wait: float = 3.0) -> None:
    """Start the local web page (`system-diff watch`) if it is not running, giving it at most `wait` seconds; never
    raises: a session must start whatever happens here."""
    try:
        if (config.lenient(root).get("web") or {}).get("start", True) is False:
            return
        from . import watch
        start = getattr(watch, "start_detached", None)
        if start is None:
            return
        import threading

        def go() -> None:
            try:
                start(root)
            except Exception:
                pass
        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(wait)
    except Exception:
        pass


def on_start(root: Path, payload: Dict[str, Any], env=os.environ) -> Optional[str]:
    """A session has started: started by Claude Code (a hook run, which has CLAUDE_PROJECT_DIR), start the web page
    with the pane's views in the background, unless the config says `[web] start = false`."""
    if env.get("CLAUDE_PROJECT_DIR"):
        start_web(root)
    return None


OLD_TASK = "system-diff: terminal view"   # a VS Code task older setups added to open the retired terminal view


def _drop_vscode_task(root: Path, out: Callable[[str], None]) -> None:
    path = root / ".vscode" / "tasks.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks = data["tasks"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    ours = [t for t in tasks if isinstance(t, dict) and t.get("label") == OLD_TASK]
    if not ours:
        return
    data["tasks"] = [t for t in tasks if t not in ours]
    if not data["tasks"] and set(data) <= {"version", "tasks"}:
        path.unlink()
        if not any(path.parent.iterdir()):
            path.parent.rmdir()
    else:
        _save(path, data)
    out(f"removed the VS Code task from {path}")


def on_prompt(root: Path, payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """The person sent a prompt: a new turn starts, and Claude is told to plan the work for a team of agents (unless
    the config says `[agents] orchestrate = false`). A notice Claude Code sends as a prompt (a background task done)
    is not one: the turn goes on."""
    if activity.is_system(payload.get("prompt") or ""):
        return None
    mark_turn(root)
    activity.new_turn(root)
    activity.record(root, {"event": activity.PROMPT, "text": _clip(payload.get("prompt"), 300),
                           "session": payload.get("session_id", ""), "agent": payload.get("agent_id") or ""})
    if (config.lenient(root).get("agents") or {}).get("orchestrate", True) is False or payload.get("agent_id"):
        return None
    return {"context": agents.ORCHESTRATE}


def on_tool(root: Path, payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """After every tool: hand the agent what other agents published since its last tool. For a read or an edit of
    a file in a part, also note the file and keep a brief of its part for the status line; after an edit to code,
    look at what the turn has done to the system so far, and say what this edit added to it (one hook at a time:
    they can overlap)."""
    tool = payload.get("tool_name", "")
    agent_id = payload.get("agent_id") or ""

    def unread() -> Dict[str, Any]:
        return {"message": None, "context": agents.deliver(root, agent_id)}

    named = (payload.get("tool_input") or {}).get("agent")
    if tool.startswith("mcp__system-diff__") and isinstance(named, str) and agent_id:
        agents.tie(root, agent_id, named)   # a subagent says who it is the first time it claims, publishes or reads

    if tool not in EDIT_TOOLS + READ_TOOLS:
        return unread()
    given = payload.get("tool_input") or {}
    path = activity.relative(root, given.get("file_path") or given.get("notebook_path") or "")
    if not path:
        return unread()
    edit = tool in EDIT_TOOLS
    activity.record(root, {"event": activity.EDIT if edit else activity.READ, "path": path, "tool": tool,
                           "session": payload.get("session_id", ""), "agent": agent_id})
    cfg = config.lenient(root)
    part = activity.part_of(path, Code(cfg))
    if part is None:
        return unread()
    if not activity.load_brief(root, part, max_age=600):
        activity.save_brief(root, learn.brief(root, cfg, part))
    if not (edit and turn_sha(root)):
        return unread()
    with activity.only_one(root, wait=10) as mine:
        if not mine:
            return unread()
        last = activity.read_state(root).get("turn") or {}
        before = last.get("told") or [s["text"] for s in last.get("story") or []]
        since, turn = compare_worktree(root, ["checkpoint", "turn"])
        notes = design.Reading(turn).notes()
        noted = set(last.get("noted") or [])
        record_step(root, path, part, noted, notes)
        activity.write_state(root, since.diff, since.base, turn.diff, noted=[n["text"] for n in notes])
        said = step_message(before, learn.story(turn.diff))
    team = agents.after_edit(root, agent_id, part, [n for n in notes if n["text"] not in noted], turn.after)
    message = "; ".join(m for m in (said, f"System diff: {team['warn']}" if team["warn"] else "") if m)
    return {"message": message, "context": team["context"]}


def record_step(root: Path, path: str, part: str, before: Iterable[str], notes: List[Dict[str, Any]]) -> None:
    """What one edit did to the system's design, kept as a step of what just happened: each change it made, said as
    a design note with why it matters, and each change of the turn it undid."""
    had = set(before)
    now = {n["text"] for n in notes}
    items = [{"text": n["text"], "why": n["why"], "sign": n["sign"], "status": n["status"], "weight": n["weight"],
              "change": n.get("change")} for n in notes if n["text"] not in had]
    items += [{"text": t, "why": "undone: the turn no longer makes this change", "sign": "↺", "status": "muted"}
              for t in sorted(had - now)]
    if items:
        activity.record(root, {"event": "change", "path": path, "part": part, "items": items, "source": "hook"})


def on_stop(root: Path, payload: Dict[str, Any]) -> Optional[str]:
    turn_known = turn_sha(root) is not None
    found = compare_worktree(root, ["checkpoint", "turn"] if turn_known else ["checkpoint"])
    since, turn = found[0], (found[1] if turn_known else None)
    activity.write_state(root, since.diff, since.base, turn.diff if turn else None)
    activity.record(root, {"event": activity.STOP, "text": _clip(payload.get("last_assistant_message"), 400),
                           "session": payload.get("session_id", "")})
    return hook_message(since, turn, edited_this_turn(root, since.cfg))


def run_hook(stdin_text: str, emit: Callable[[str], None] = print) -> int:
    """What Claude Code's hooks run, told apart by the event they name (a hook set up before there were three is a
    Stop hook). Always exits 0: a hook that fails must not get in the agent's way."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    cwd = Path(payload.get("cwd") or os.getcwd())
    if not _is_repo(cwd):
        return 0
    try:
        root = Path(git("rev-parse", "--show-toplevel", cwd=cwd).strip())
        event = payload.get("hook_event_name") or "Stop"
        message = None
        if event == "SessionStart":
            message = on_start(root, payload)
        elif event == "UserPromptSubmit":
            said = on_prompt(root, payload) or {}
            if said.get("context"):
                emit(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                        "additionalContext": said["context"]}}))
        elif event == "PostToolUse":
            said = on_tool(root, payload) or {}
            message = said.get("message") or None
            if said.get("context"):   # what other agents published, for this agent to read with its tool result
                out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": said["context"]}}
                if message:
                    out["systemMessage"] = message
                emit(json.dumps(out))
                message = None
        elif event == "Stop":
            message = on_stop(root, payload)
        if message:
            emit(json.dumps({"systemMessage": message}))
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError):
        return 0
    return 0


# ── setup ────────────────────────────────────────────────────────────────────

SCOPES = ("user", "project", "local")
HOOKS = (  # event, matcher, how it runs, what it does
    ("SessionStart", "startup|resume", {"timeout": 10},
     "when a session starts, opens the terminal view beside Claude where the terminal can split (tmux, zellij, "
     "WezTerm, kitty, Windows Terminal)"),
    ("UserPromptSubmit", None, {"timeout": 10},
     "marks where each turn starts, and asks Claude to plan the work for a team of agents"),
    # One entry for every tool, and not in the background: only a hook Claude Code waits for can show you a line in
    # the conversation or hand the agent its messages. A timeout (seconds) so a stuck git never holds up the agent;
    # Claude Code's own default is 600.
    ("PostToolUse", None, {"timeout": 15},
     "notes the files Claude reads and, after an edit that changes the system's structure, one line on what it "
     "changed; and hands each agent what other agents published"),
    ("Stop", None, {"timeout": 30}, "after each turn, one line on what the turn changed in the system"),
)


def command(sub: str) -> str:
    """How Claude Code should run `system-diff SUB`: by name when it is on PATH, else this Python by its full path."""
    if shutil.which(NAME):
        return f"{NAME} {sub}"
    exe = sys.executable
    return f'"{exe}" -m system_diff {sub}' if " " in exe else f"{exe} -m system_diff {sub}"


def hook_command() -> str:
    return command("hook")


def _paths(scope: str, root: Path) -> tuple:
    home = Path.home() / ".claude"
    settings = {"user": home / "settings.json", "project": root / ".claude" / "settings.json",
                "local": root / ".claude" / "settings.local.json"}[scope]
    skills = home / "skills" if scope == "user" else root / ".claude" / "skills"
    return settings, skills / SKILL_NAME / "SKILL.md"


def _drop_skill(path: Path, out: Callable[[str], None]) -> None:
    """Remove a skill this setup wrote (never one someone else did), and its folder when that is then empty."""
    if path.is_file() and SKILL_MARK in path.read_text(encoding="utf-8"):
        path.unlink()
        if not any(path.parent.iterdir()):
            path.parent.rmdir()
        out(f"removed the skill {path}")


def _is_ours(cmd: Any, sub: str) -> bool:
    return isinstance(cmd, str) and (f"system-diff {sub}" in cmd or f"system_diff {sub}" in cmd)


def _ours(entry: Dict[str, Any]) -> bool:
    return any(_is_ours(h.get("command", ""), "hook") for h in entry.get("hooks", []))


def _load(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON ({exc}); fix it, then run setup again") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not a JSON object; fix it, then run setup again")
    return data


def _quiet_load(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}") if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _not_ours(settings: Dict[str, Any]) -> bool:
    """Whether these settings set a status line that is not system-diff's."""
    line = settings.get("statusLine")
    return bool(line) and not (isinstance(line, dict) and _is_ours(line.get("command"), "statusline"))


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _claude(args: List[str]) -> subprocess.CompletedProcess:
    return run_hidden(["claude", *args], capture_output=True, text=True)


def setup_claude(root: Path, scope: str = "user", undo: bool = False, out: Callable[[str], None] = print,
                 status_line: bool = True) -> int:
    """Add (or with `undo`, remove) the hooks, the status line, the skill and the MCP server, printing each change.
    A status line someone already has is left alone."""
    if scope not in SCOPES:
        raise SystemExit(f"--scope is one of {', '.join(SCOPES)}")
    settings_path, skill_path = _paths(scope, root)
    settings = _load(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f'{settings_path}: "hooks" is not a JSON object; fix it, then run setup again')
    line = settings.get("statusLine")
    line_ours = isinstance(line, dict) and _is_ours(line.get("command"), "statusline")
    # A status line set in another scope would be hidden by ours in this repository (local over project over user).
    theirs = [p for p in (_paths(sc, root)[0] for sc in SCOPES if sc != scope) if _not_ours(_quiet_load(p))]
    has_claude = shutil.which("claude") is not None
    events = list(dict.fromkeys(event for event, *_ in HOOKS))
    if undo:
        changed = False
        for event in events:
            entries = hooks.get(event) or []
            if any(_ours(e) for e in entries):
                hooks[event] = [e for e in entries if not _ours(e)]
                if not hooks[event]:
                    del hooks[event]
                changed = True
                out(f"removed the {event} hook from {settings_path}")
        if line_ours:
            del settings["statusLine"]
            changed = True
            out(f"removed the status line from {settings_path}")
        if not hooks:
            del settings["hooks"]
        if changed:
            _save(settings_path, settings)
        for path in (skill_path, skill_path.parent.parent / OLD_SKILL_NAME / "SKILL.md"):
            _drop_skill(path, out)
        _drop_vscode_task(root, out)
        if has_claude and _claude(["mcp", "get", NAME]).returncode == 0:
            got = _claude(["mcp", "remove", NAME, "--scope", scope])
            out(f"removed the MCP server ({scope} scope)" if got.returncode == 0 else
                f"could not remove the MCP server: {(got.stderr or got.stdout).strip()[:200]}")
        out("system-diff is no longer wired into Claude Code" + ("" if scope == "user" else f" ({scope} scope)"))
        return 0

    changed = False
    for event in events:
        wanted = []
        for ev, matcher, how, _what in HOOKS:
            if ev == event:
                entry: Dict[str, Any] = {"matcher": matcher} if matcher else {}
                entry["hooks"] = [{"type": "command", "command": hook_command(), **how}]
                wanted.append(entry)
        entries = hooks.setdefault(event, [])
        ours = [e for e in entries if _ours(e)]
        if ours == wanted:
            out(f"the {event} hook is already in {settings_path}")
            continue
        hooks[event] = [e for e in entries if not _ours(e)] + wanted  # an older setup's hooks are replaced
        changed = True
        verb = "updated the" if ours else "added a"
        for ev, _matcher, _how, what in HOOKS:
            if ev == event:
                out(f"{verb} {event} hook in {settings_path}: {what}")
    if not status_line:  # the pane beside the chat says the same, and more
        if line_ours:
            del settings["statusLine"]
            changed = True
            out(f"removed the status line from {settings_path}: the system-diff pane beside the chat shows the same")
    elif line is None and theirs:
        out(f"left your status line in {theirs[0]} alone; to have system-diff's in it as well, make it also print "
            "what `system-diff statusline` prints (it reads the same JSON on stdin)")
    elif line is None:
        settings["statusLine"] = {"type": "command", "command": command("statusline")}
        changed = True
        out(f"added a status line to {settings_path}: the part Claude is working on, and what changed in the system")
    elif line_ours:
        out(f"the status line is already in {settings_path}")
    else:
        out(f"left the status line in {settings_path} alone, it is yours; to have system-diff's in it as well, make "
            "it also print what `system-diff statusline` prints (it reads the same JSON on stdin)")
    if changed:
        _save(settings_path, settings)
    _drop_skill(skill_path.parent.parent / OLD_SKILL_NAME / "SKILL.md", out)  # an older setup's: it held /system-diff
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    if skill_path.is_file() and skill_path.read_text(encoding="utf-8") == SKILL:
        out(f"the skill is already at {skill_path}")
    elif skill_path.is_file() and SKILL_MARK not in skill_path.read_text(encoding="utf-8"):
        out(f"left {skill_path} alone: someone else wrote it")
    else:
        with open(skill_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(SKILL)
        out(f"wrote the skill {skill_path}: when and how to show and explain the system")
    _drop_vscode_task(root, out)
    if not has_claude:
        out("Claude Code's `claude` command is not on PATH, so the MCP server was not registered; "
            "run setup again where it is")
        return 0
    try:
        import mcp  # noqa: F401  (the extra the server needs)
    except ImportError:
        out('the MCP server needs its extra: pip install "system-diff[mcp]", then run setup again')
        return 0
    if _claude(["mcp", "get", NAME]).returncode == 0:
        out("the MCP server is already registered")
    else:
        server = hook_command()[: -len(" hook")]
        got = _claude(["mcp", "add", "--scope", scope, "--transport", "stdio", NAME, "--", *_split(server), "mcp"])
        out(f"registered the MCP server ({scope} scope): system_map, system_diff, explain, draw, checkpoint"
            if got.returncode == 0 else f"could not register the MCP server: {(got.stderr or got.stdout).strip()[:200]}")
    return 0


def _split(command: str) -> List[str]:
    """`"C:/Program Files/py/python.exe" -m system_diff` -> its words, the quoted path as one."""
    if command.startswith('"'):
        exe, rest = command[1:].split('"', 1)
        return [exe, *rest.split()]
    return command.split()
