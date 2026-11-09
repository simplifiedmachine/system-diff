"""The terminal view beside the agent, without opening a second terminal by hand. When a Claude Code session starts, its
SessionStart hook opens `system-diff tui` in a pane next to Claude wherever the terminal lets a command do that: tmux,
zellij, WezTerm, kitty (with remote control on) and Windows Terminal. VS Code's terminal does not, so for it setup
adds a task that opens the view in its own terminal whenever the folder opens: it is there before Claude starts.

The view keeps a heartbeat file while it runs (.git/system-diff/tui.alive), so a second one is never opened, and the
hook opens one only for a session someone is watching (not for `claude -p`).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .sources import git, git_dir

ALIVE = "tui.alive"
BEAT, STALE = 2.0, 15.0  # seconds between heartbeats; a heartbeat older than STALE means the view is gone
NAMES = {"tmux": "tmux", "zellij": "zellij", "wezterm": "WezTerm", "kitty": "kitty",
         "windows-terminal": "Windows Terminal", "vscode": "VS Code"}
TASK = "system-diff: terminal view"


def beat(root: Path) -> None:
    """Called by the terminal view while it runs."""
    (git_dir(root) / ALIVE).write_text(str(os.getpid()), encoding="utf-8")


def gone(root: Path) -> None:
    try:
        (git_dir(root) / ALIVE).unlink()
    except (OSError, subprocess.CalledProcessError):
        pass


def running(root: Path) -> bool:
    try:
        return time.time() - (git_dir(root) / ALIVE).stat().st_mtime < STALE
    except (OSError, subprocess.CalledProcessError):
        return False


def where(env: Mapping[str, str] = os.environ) -> Optional[str]:
    """The terminal this runs in, as far as it says."""
    for var, kind in (("TMUX", "tmux"), ("ZELLIJ", "zellij"), ("WEZTERM_PANE", "wezterm"),
                      ("KITTY_WINDOW_ID", "kitty"), ("WT_SESSION", "windows-terminal")):
        if env.get(var):
            return kind
    return "vscode" if env.get("TERM_PROGRAM") == "vscode" else None


def watched(env: Mapping[str, str] = os.environ) -> bool:
    """Whether someone is at this session: not `claude -p`, nor the SDK (when Claude Code says so)."""
    return env.get("CLAUDE_CODE_SESSION_ATTENDED", "1") != "0" and env.get("CLAUDE_CODE_ENTRYPOINT", "cli") == "cli"


def tui_argv() -> List[str]:
    """How to start the terminal view: with this very Python, which has system-diff."""
    return [sys.executable, "-m", "system_diff", "tui"]


def pane_command(kind: str, argv: List[str], root: Path) -> Optional[List[str]]:
    """The command that opens `argv` in a pane to the right of this one, keeping the focus here where it can."""
    cwd = str(root)
    return {
        "tmux": ["tmux", "split-window", "-h", "-d", "-l", "42%", "-c", cwd, shlex.join(argv)],
        "zellij": ["zellij", "run", "--direction", "right", "--cwd", cwd, "--", *argv],
        "wezterm": ["wezterm", "cli", "split-pane", "--right", "--percent", "42", "--cwd", cwd, "--", *argv],
        "kitty": ["kitty", "@", "launch", "--location=vsplit", "--keep-focus", "--cwd", cwd, *argv],
        "windows-terminal": ["wt", "-w", "0", "split-pane", "-V", "-d", cwd, *argv],
    }.get(kind)


def launch(cmd: List[str]) -> subprocess.CompletedProcess:
    """Run the terminal's own command that opens a pane."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def open_beside(root: Path, env: Mapping[str, str] = os.environ) -> str:
    """Open the terminal view in a pane beside this terminal. What happened, in a sentence for the person; "" when
    there was nothing to do or no way to do it here."""
    if running(root) or importlib.util.find_spec("textual") is None:
        return ""
    kind = where(env)
    cmd = pane_command(kind, tui_argv(), root) if kind else None
    if cmd is None or shutil.which(cmd[0]) is None:
        return ""
    try:
        got = launch(cmd)
        if got.returncode != 0 and kind == "tmux":  # a tmux older than 3.1 sizes panes with -p
            got = launch([*cmd[:4], "-p", "42", *cmd[6:]])
    except (OSError, subprocess.SubprocessError):
        return ""
    if got.returncode != 0:
        return ""
    return (f"system-diff: the terminal view is open in a {NAMES[kind]} pane beside Claude: it follows the part Claude "
            "works on (q closes it)")


# ── VS Code: a task that opens the view when the folder opens ────────────────

def vscode_task(command: str) -> Dict[str, Any]:
    return {"label": TASK, "type": "shell", "command": command, "isBackground": True, "problemMatcher": [],
            "runOptions": {"runOn": "folderOpen"},
            "presentation": {"reveal": "always", "focus": False, "panel": "dedicated", "showReuseMessage": False,
                             "clear": True}}


def add_vscode_task(root: Path, command: str, out: Callable[[str], None], undo: bool = False,
                    hide: bool = False) -> None:
    """Add (or with `undo`, remove) the task to .vscode/tasks.json. `hide`: when the file is new, keep it out of git
    status for this clone only (.git/info/exclude), as setup does for its local scope."""
    path = root / ".vscode" / "tasks.json"
    existed = path.is_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if existed else {"version": "2.0.0", "tasks": []}
    except ValueError:  # comments: VS Code allows them, JSON does not
        if not undo:
            out(f"left {path} alone (it is not plain JSON); to open the terminal view when the folder opens, add this "
                f"task to it: {json.dumps(vscode_task(command))}")
        return
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return
    ours = [t for t in tasks if isinstance(t, dict) and t.get("label") == TASK]
    if undo:
        if ours:
            data["tasks"] = [t for t in tasks if t not in ours]
            if not data["tasks"] and set(data) <= {"version", "tasks"}:
                path.unlink()
                if not any(path.parent.iterdir()):
                    path.parent.rmdir()
            else:
                _save(path, data)
            out(f"removed the VS Code task from {path}")
        return
    if ours == [vscode_task(command)]:
        out(f"the VS Code task is already in {path}")
        return
    data["tasks"] = [t for t in tasks if t not in ours] + [vscode_task(command)]
    _save(path, data)
    out(f"{'updated' if ours else 'added'} a VS Code task in {path}: it opens the terminal view whenever this folder "
        f"opens (VS Code asks once whether to allow it); to open it now: Tasks: Run Task, then \"{TASK}\"")
    if hide and not existed:
        exclude = Path(git("rev-parse", "--git-path", "info/exclude", cwd=root).strip())
        exclude = exclude if exclude.is_absolute() else root / exclude
        lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
        if "/.vscode/tasks.json" not in lines:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with open(exclude, "a", encoding="utf-8", newline="\n") as f:
                f.write("/.vscode/tasks.json\n")


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, indent=2) + "\n")
