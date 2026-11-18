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
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import activity, agents, chat, config, design, learn
from .compare import compare_worktree, mark_turn, turn_sha
from .extract import Code
from .sources import git, git_dir, run_hidden

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


def find_root(cwd: Path) -> Optional[Path]:
    """The repository holding `cwd`, found by looking for .git upwards in Python (no git process: starting one costs
    seconds on some machines); None outside one."""
    from . import gitfs
    try:
        here = Path(cwd).absolute()
    except OSError:
        return None
    for d in (here, *here.parents):
        if (d / ".git").exists():
            r = gitfs.locate(d)
            if r is None:
                return None
            try:
                return Path(r.top).resolve()
            except OSError:
                return Path(r.top)
    return None


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


# ── the hooks hand their work to the engine ──────────────────────────────────
# Where starting any program costs seconds (endpoint security), a hook can afford one: its own Python. So a hook does
# only what is local and cheap (it appends to the activity log, reads the agents' bus, builds Claude's context from
# files) and hands the rest to the repository's resident engine (`system-diff watch`), POST /hook, waiting a moment
# for its answer: the engine marks the turn from memory, compares from memory, publishes on the bus. No git, no
# snapshot, no comparison, no background job is started in a hook. With no engine, a hook run by Claude Code starts
# one, detached (the one process it starts), and skips the work this time: the engine catches up from the activity
# log (it marks the turn of a prompt it has not marked). Run by hand (no CLAUDE_PROJECT_DIR: a script, the tests), the
# hook does the work itself, as the engine would.

PROMPT_WAIT = 0.3   # seconds a hook waits for the engine's answer: the work goes on there after it stops waiting
TOOL_WAIT = 0.4     # an edit's one line, when the engine has it within this; else it comes with a later hook
STOP_WAIT = 0.5
CONNECT = 0.5       # seconds to reach the engine at all (Windows retries a refused connection for seconds)
TURN = "turn.json"  # {"at": the prompt whose turn was last marked}: the engine marks a prompt's turn once
SAID = "said"       # what the engine found after the hook stopped waiting, per agent: the next hook says it
HOOKS_LOG = "hooks.jsonl"   # each hook's run: event, seconds, how the engine was reached (doctor shows them)
CATCH_UP_FOR = 3600.0       # a prompt older than this is not marked late
RUN: Dict[str, str] = {}    # how the current hook reached the engine: done, sent, started, skipped, here


def _under_claude(env) -> bool:
    return bool(env.get("CLAUDE_PROJECT_DIR"))


def _may_start(root: Path, env) -> bool:
    if env.get("SYSTEM_DIFF_WEB_START", "1") == "0":
        return False
    try:
        return (config.lenient(root).get("web") or {}).get("start", True) is not False
    except Exception:  # noqa: BLE001
        return True


def _nobody_listens(port: int) -> bool:
    """Whether nothing listens on this port of this machine (a bind would succeed): an engine that is gone, found at
    once instead of after a refused connection's retries."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def ask_engine(root: Path, body: Dict[str, Any], wait: float) -> tuple:
    """POST /hook to the repository's engine: ("done", its answer) when it did the work within `wait` seconds,
    ("sent", None) when it has the work but did not finish in time (or did not say), ("none", None) when no engine
    of this version answers. Never raises."""
    import http.client
    try:
        from . import live
        info = live.engine_info(root)
        if not info or not info.get("token") or live._older(str(info.get("version") or "")):
            return "none", None
        port = int(info["port"])
        if _nobody_listens(port):
            return "none", None
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=CONNECT)
        conn.connect()
    except Exception:  # noqa: BLE001
        return "none", None
    try:
        conn.sock.settimeout(wait + 1.0)
        conn.request("POST", "/hook", body=json.dumps({**body, "wait": wait}).encode("utf-8"),
                     headers={"X-Token": str(info["token"]), "Content-Type": "application/json"})
        r = conn.getresponse()
        raw = r.read()
        if r.status == 404:
            return "none", None   # an engine from before /hook
        got = json.loads(raw.decode("utf-8")) if r.status == 200 else None
        return ("done", got) if isinstance(got, dict) and got.get("done") else ("sent", None)
    except Exception:  # noqa: BLE001 - sent: the engine may still do it
        return "sent", None
    finally:
        conn.close()


def hand_off(root: Path, body: Dict[str, Any], wait: float, env=os.environ) -> Optional[Dict[str, Any]]:
    """Have the work in `body` done: by the engine (its answer, or None when it answers later); with none, started
    in the background and skipped (run by Claude Code), or done here (run by hand)."""
    state, got = ask_engine(root, body, wait)
    if state == "done":
        RUN["mode"] = "engine"
        return got
    if state == "sent":
        RUN["mode"] = "engine, answered later"
        return None
    if _under_claude(env):
        if _may_start(root, env):
            RUN["mode"] = "engine started, work skipped"
            start_web(root)
        else:
            RUN["mode"] = "no engine ([web] start = false), work skipped"
        return None
    RUN["mode"] = "done in the hook (run by hand)"
    if body.get("paths"):
        touch(root, body["paths"])
    return hook_work(root, body)


def hook_work(root: Path, body: Dict[str, Any]) -> Dict[str, Any]:
    """What a hook hands over, done: in the engine's process (watch.py, POST /hook, one job at a time, in order) or,
    run by hand, in the hook's. {"message", "context"}."""
    event = body.get("event")
    if event == "prompt":
        prompt_work(root, body)
    elif event == "tool":
        return tool_work(root, body)
    elif event == "stop":
        return {"message": stop_work(root, body)}
    elif event == "catch-up":
        catch_up(root)
    return {}


def turn_marked_at(root: Path) -> float:
    try:
        return float(json.loads((git_dir(root) / TURN).read_text(encoding="utf-8")).get("at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def prompt_work(root: Path, body: Dict[str, Any]) -> None:
    """A prompt's work: keep the working tree as the turn's start (from the engine's memory: one `git update-ref`
    in the engine), note which prompt it was for, and start learning the objectives when they are stale."""
    mark_turn(root)
    try:
        activity._replace(git_dir(root) / TURN, json.dumps({"at": float(body.get("at") or time.time())}) + "\n")
    except (OSError, ValueError, TypeError):
        pass
    try:
        from . import objectives
        objectives.ensure(root)
    except Exception:  # noqa: BLE001 - only a head start
        pass


def catch_up(root: Path) -> None:
    """An engine starting: the latest prompt's turn, if no one marked it (the hook found no engine, started one and
    went on)."""
    last = next((e for e in reversed(activity.events(root, 400)) if e.get("event") == activity.PROMPT
                 and not activity.is_system(e.get("text", ""))), None)
    if last is None or float(last["at"]) <= turn_marked_at(root) or time.time() - float(last["at"]) > CATCH_UP_FOR:
        return
    prompt_work(root, {"at": last["at"]})


def tool_work(root: Path, body: Dict[str, Any]) -> Dict[str, Any]:
    """After a read or an edit of a file in a part: keep a brief of the part for the status line; after an edit to
    code, look at what the turn has done to the system so far and say what this edit added to it (one at a time)."""
    out: Dict[str, Any] = {"message": None, "context": ""}
    cfg = config.lenient(root)
    part, path, agent_id = body.get("part"), body.get("path") or "", body.get("agent") or ""
    if not part:
        return out
    if body.get("brief") or not activity.load_brief(root, part, max_age=600):
        activity.save_brief(root, learn.brief(root, cfg, part))
    if not (body.get("edit") and turn_sha(root)):
        return out
    with activity.only_one(root, wait=10) as mine:
        if not mine:
            return out
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
    return {"message": message or None, "context": team["context"]}


def stop_work(root: Path, body: Dict[str, Any]) -> Optional[str]:
    """The end of a turn: what the turn changed in the system, kept for the status line and said in one line."""
    turn_known = turn_sha(root) is not None
    found = compare_worktree(root, ["checkpoint", "turn"] if turn_known else ["checkpoint"])
    since, turn = found[0], (found[1] if turn_known else None)
    activity.write_state(root, since.diff, since.base, turn.diff if turn else None)
    return hook_message(since, turn, edited_this_turn(root, since.cfg))


def _said_file(root: Path, agent_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (agent_id or "main"))[:80]
    return git_dir(root, SAID) / f"{safe}.jsonl"


def say_later(root: Path, agent_id: str, result: Dict[str, Any]) -> None:
    """Keep what the engine found after the hook stopped waiting, for the agent's next hook. Never raises."""
    if not (result.get("message") or result.get("context")):
        return
    try:
        with open(_said_file(root, agent_id), "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"at": round(time.time(), 3), "message": result.get("message") or "",
                                "context": result.get("context") or ""}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def take_said(root: Path, agent_id: str) -> List[Dict[str, Any]]:
    """What the engine kept for this agent (say_later), taken: each is said once."""
    f = _said_file(root, agent_id)
    if not f.exists():
        return []
    taking = f.with_name(f"{f.name}.{os.getpid()}.taking")
    try:
        os.replace(f, taking)
        lines = taking.read_text(encoding="utf-8", errors="replace").splitlines()
        taking.unlink()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            out.append(got)
    return out


def note_hook(root: Path, event: str, seconds: float, mode: str) -> None:
    """Keep how long this hook took and how it reached the engine (hooks.jsonl, the newest 200): `system-diff
    doctor` shows them. Never raises."""
    try:
        f = git_dir(root) / HOOKS_LOG
        with open(f, "a", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps({"at": round(time.time(), 3), "event": event, "seconds": round(seconds, 3),
                                  "mode": mode, "pid": os.getpid()}) + "\n")
        if f.stat().st_size > 64 * 1024:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            activity._replace(f, "\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001 - a record only
        pass


def hook_runs(root: Path, most: int = 200) -> List[Dict[str, Any]]:
    """The hooks' recent runs (note_hook), oldest first."""
    try:
        lines = (git_dir(root) / HOOKS_LOG).read_text(encoding="utf-8", errors="replace").splitlines()[-most:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            out.append(got)
    return out


def start_web(root: Path) -> None:
    """Start the repository's engine (`system-diff watch`, the web page) in the background if it is not running,
    without waiting for it: the one process a hook may start. Never raises: a session must go on whatever happens."""
    try:
        if (config.lenient(root).get("web") or {}).get("start", True) is False:
            return
        from . import watch
        start = getattr(watch, "start_detached", None)
        if start is None:
            return
        try:
            start(root, wait=0)
        except TypeError:   # a stand-in that takes no wait
            start(root)
    except Exception:  # noqa: BLE001
        pass


def on_start(root: Path, payload: Dict[str, Any], env=os.environ) -> Optional[str]:
    """A session has started: started by Claude Code (a hook run, which has CLAUDE_PROJECT_DIR), start the engine and
    its web page in the background, unless the config says `[web] start = false`."""
    if env.get("CLAUDE_PROJECT_DIR"):
        RUN["mode"] = "engine started if none ran"
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


def on_prompt(root: Path, payload: Dict[str, Any], env=os.environ) -> Optional[Dict[str, str]]:
    """The person sent a prompt: a new turn starts (the engine marks it), and Claude is told to plan the work for a
    team of agents (unless the config says `[agents] orchestrate = false`). A notice Claude Code sends as a prompt (a
    background task done) is not one: the turn goes on."""
    if activity.is_system(payload.get("prompt") or ""):
        return None
    at = round(time.time(), 3)
    activity.new_turn(root)
    activity.record(root, {"at": at, "event": activity.PROMPT, "text": _clip(payload.get("prompt"), 300),
                           "session": payload.get("session_id", ""), "agent": payload.get("agent_id") or ""})
    asked = None if payload.get("agent_id") else chat.asked(root, payload.get("prompt") or "",
                                                              payload.get("session_id", ""), at)
    hand_off(root, {"event": "prompt", "at": at, "agent": payload.get("agent_id") or ""}, PROMPT_WAIT, env)
    aims = objectives_context(root)
    if asked and asked.get("web"):   # sent from the web page: Claude may point at the diagram as it answers
        aims = chat.WEB_HINT + ("\n\n" + aims if aims else "")
    if (config.lenient(root).get("agents") or {}).get("orchestrate", True) is False or payload.get("agent_id"):
        return {"context": aims} if aims else None
    return {"context": agents.ORCHESTRATE + ("\n\n" + aims if aims else "")}


def objectives_context(root: Path) -> str:
    """What the person has been optimising for, for Claude at each prompt (objectives.context, at most 600
    characters; "" when none), read from its file: learning them is the engine's (prompt_work). Never raises."""
    try:
        from . import objectives
    except Exception:  # noqa: BLE001 - an install without them
        return ""
    try:
        return (objectives.context(root) or "")[:600]
    except Exception:  # noqa: BLE001
        return ""


def edited_paths(root: Path, payload: Dict[str, Any]) -> List[str]:
    """The files an edit tool (Edit, Write, MultiEdit, NotebookEdit) wrote, relative to the repository."""
    if payload.get("tool_name", "") not in EDIT_TOOLS:
        return []
    given = payload.get("tool_input") or {}
    said = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
    out: List[str] = []
    for raw in (given.get("file_path"), given.get("notebook_path"), said.get("filePath")):
        if isinstance(raw, str) and raw:
            rel = activity.relative(root, raw)
            if rel and rel not in out:
                out.append(rel)
    return out


def touch(root: Path, paths: List[str]) -> None:
    """Tell the resident engine which files Claude just edited (POST /touch, at most half a second), so it re-reads
    them at once instead of at its next look. Nothing when no engine runs; never raises."""
    if not paths:
        return
    try:
        from . import live
        tell = getattr(live, "touch", None)
        if tell is None:
            return
        import threading

        def go() -> None:
            try:
                tell(root, list(paths))
            except Exception:  # noqa: BLE001 - a hook never fails a turn
                pass
        t = threading.Thread(target=go, name="system-diff-touch", daemon=True)
        t.start()
        t.join(TOUCH_WAIT)   # live.touch keeps to half a second itself; this holds whatever happens there
    except Exception:  # noqa: BLE001 - a hook never fails a turn
        pass


TOUCH_WAIT = 0.5


def on_tool(root: Path, payload: Dict[str, Any], env=os.environ) -> Optional[Dict[str, str]]:
    """After every tool: hand the agent what other agents published since its last tool, and what the engine found
    after an earlier hook stopped waiting. For a read or an edit of a file, note it; hand the engine the edit (it
    re-reads the file, and says what the edit added to what the turn did to the system) and a stale brief of the
    part for the status line."""
    tool = payload.get("tool_name", "")
    agent_id = payload.get("agent_id") or ""
    named = (payload.get("tool_input") or {}).get("agent")
    if tool.startswith("mcp__system-diff__") and isinstance(named, str) and agent_id:
        agents.tie(root, agent_id, named)   # a subagent says who it is the first time it claims, publishes or reads
    messages: List[str] = []
    context: List[str] = [agents.deliver(root, agent_id)]
    for late in take_said(root, agent_id):
        messages.append(late.get("message") or "")
        context.append(late.get("context") or "")

    def said() -> Dict[str, Any]:
        return {"message": "; ".join(m for m in messages if m) or None,
                "context": "\n".join(c for c in context if c)}

    if tool not in EDIT_TOOLS + READ_TOOLS:
        return said()
    given = payload.get("tool_input") or {}
    path = activity.relative(root, given.get("file_path") or given.get("notebook_path") or "")
    if not path:
        return said()
    edit = tool in EDIT_TOOLS
    if edit:
        tell_inner(root, path)
    activity.record(root, {"event": activity.EDIT if edit else activity.READ, "path": path, "tool": tool,
                           "session": payload.get("session_id", ""), "agent": agent_id})
    part = activity.part_of(path, Code(config.lenient(root)))
    stale = bool(part) and not activity.load_brief(root, part, max_age=600)
    if not edit and not stale:
        return said()
    body = {"event": "tool", "edit": edit, "path": path, "part": part, "brief": stale, "agent": agent_id,
            "session": payload.get("session_id", ""), "paths": (edited_paths(root, payload) or [path]) if edit else []}
    got = hand_off(root, body, TOOL_WAIT if edit else 0.0, env) or {}
    messages.append(got.get("message") or "")
    context.append(got.get("context") or "")
    return said()


def inner_repo(root: Path, path: str) -> Optional[Path]:
    """The repository inside this one (a submodule, a nested repository) that holds `path`, if any: found by walking
    up from the file to the root (a few stats; no scan of the tree)."""
    here = (Path(root) / path).parent
    try:
        here.relative_to(root)
    except ValueError:
        return None
    while here != Path(root) and here != here.parent:
        if (here / ".git").exists():
            return here
        here = here.parent
    return None


def tell_inner(root: Path, path: str) -> None:
    """An edit inside a repository within this one goes to that repository's engine too (its own diagram, history and
    tabs follow), when one runs: one short local request; never in the way of the hook."""
    inner = inner_repo(root, path)
    if inner is None:
        return
    try:
        from .sources import git_dir
        info = json.loads((git_dir(inner) / "web.json").read_text(encoding="utf-8"))
        port, token = int(info.get("port") or 0), str(info.get("token") or "")
        if not port or not token or _nobody_listens(port):
            return
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=CONNECT)
        try:
            rel = (Path(root) / path).relative_to(inner).as_posix()
            conn.request("POST", "/touch", body=json.dumps({"paths": [rel]}).encode("utf-8"),
                         headers={"X-Token": token, "Content-Type": "application/json"})
            conn.getresponse().read()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - its engine finds the edit at its next look anyway
        pass


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


def on_stop(root: Path, payload: Dict[str, Any], env=os.environ) -> Optional[str]:
    """The end of a turn: noted, and the engine says what the turn changed in the system (or, when it takes longer
    than the hook waits, the next hook does)."""
    activity.record(root, {"event": activity.STOP, "text": _clip(payload.get("last_assistant_message"), 400),
                           "session": payload.get("session_id", "")})
    if not payload.get("agent_id"):
        chat.answered(root, payload)   # the answer, for the web page's conversation panel
    late = [x.get("message") for x in take_said(root, payload.get("agent_id") or "") if x.get("message")]
    got = hand_off(root, {"event": "stop", "agent": payload.get("agent_id") or ""}, STOP_WAIT, env) or {}
    return "; ".join(m for m in [*late[-1:], got.get("message")] if m) or None


def run_hook(stdin_text: str, emit: Callable[[str], None] = print, env=None) -> int:
    """What Claude Code's hooks run, told apart by the event they name (a hook set up before there were three is a
    Stop hook). Always exits 0: a hook that fails must not get in the agent's way. No git: the repository is found
    in Python, and the work that needs git is the engine's (hand_off)."""
    began = time.monotonic()
    env = os.environ if env is None else env
    if env.get("SYSTEM_DIFF_APP_SESSION") == "1":
        return 0   # a session the Studio runs: the app runs this for it (sessions.py), once
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    root = find_root(Path(payload.get("cwd") or os.getcwd()))
    if root is None:
        return 0
    event = payload.get("hook_event_name") or "Stop"
    RUN.clear()
    try:
        message = None
        if event == "SessionStart":
            message = on_start(root, payload, env)
        elif event == "UserPromptSubmit":
            said = on_prompt(root, payload, env) or {}
            if said.get("context"):
                emit(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                        "additionalContext": said["context"]}}))
        elif event == "PostToolUse":
            said = on_tool(root, payload, env) or {}
            message = said.get("message") or None
            if said.get("context"):   # what other agents published, for this agent to read with its tool result
                out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": said["context"]}}
                if message:
                    out["systemMessage"] = message
                emit(json.dumps(out))
                message = None
        elif event == "Stop":
            message = on_stop(root, payload, env)
        elif event == "SubagentStop":   # a role finished: counted (agents worth keeping)
            from . import agentlib
            agentlib.finished(root, str(payload.get("agent_type") or ""))
        if message:
            emit(json.dumps({"systemMessage": message}))
    except Exception:  # noqa: BLE001 - a hook never fails a turn
        return 0
    finally:
        note_hook(root, event, time.monotonic() - began, RUN.get("mode", "local only"))
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
    ("SubagentStop", None, {"timeout": 15}, "counts each finished subagent's role, to suggest the ones worth keeping"),
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
