"""Claude Code: `system-diff hook`, which its Stop hook runs after every turn, and `system-diff setup claude`, which wires
system-diff in: the hook, a short skill telling the agent when to show the system diff, and the MCP server.

The hook never blocks and never fails a turn: it says in one line what changed in the system since the checkpoint, or
nothing when nothing structural did. Everything setup changes is printed, and `--undo` takes it back.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .compare import compare
from .sources import git

NAME = "system-diff"
SKILL_MARK = "<!-- written by system-diff setup claude -->"
SKILL = f"""---
name: system-diff
description: Shows the person what a code change does to the system's structure (parts, endpoints, connections, settings, tables, outside systems). Use after changing code that may change the structure, before asking them to review, or when they ask what changed.
---
{SKILL_MARK}

# Showing the system diff

After a change that may touch the system's structure:

1. Call the `system_diff` MCP tool, or run `system-diff diff --base checkpoint --head . --out .git/system-diff/latest`.
2. Tell the person, in two or three plain sentences built only from its plan, what changed in the system and why it
   matters: an endpoint added or removed, a new connection between parts, a new outside system, changed stored data,
   or code changed without tests.
3. Point them at the picture: the path the tool returns, or `system-diff watch` for a page that redraws as you work.
4. When they say they have seen it, call the `checkpoint` tool (or run `system-diff checkpoint`), so the next diff
   shows only what is new to them.

Never mention a part, endpoint, table or system that is not in the plan. For a picture of something else (a process,
a sequence of calls), use the `draw` tool.
"""


def _is_repo(path: Path) -> bool:
    try:
        return git("rev-parse", "--is-inside-work-tree", cwd=path).strip() == "true"
    except (OSError, subprocess.CalledProcessError):
        return False


def hook_message(root: Path) -> Optional[str]:
    """One line on what changed in the system since the checkpoint; None when nothing structural did."""
    c = compare(root, "checkpoint", ".")
    if not c.diff["changes"]:
        return None
    return f"System diff since {c.base}: {c.diff['headline']} (`system-diff watch` shows the picture.)"


def run_hook(stdin_text: str, emit: Callable[[str], None] = print) -> int:
    """What the Stop hook runs. Always exits 0: a hook that fails must not get in the agent's way."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    cwd = Path(payload.get("cwd") or os.getcwd())
    if not _is_repo(cwd):
        return 0
    try:
        root = Path(git("rev-parse", "--show-toplevel", cwd=cwd).strip())
        message = hook_message(root)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError):
        return 0
    if message:
        emit(json.dumps({"systemMessage": message}))
    return 0


# ── setup ────────────────────────────────────────────────────────────────────

SCOPES = ("user", "project", "local")


def hook_command() -> str:
    """How Claude Code should run system-diff: by name when it is on PATH, else this Python by its full path."""
    if shutil.which(NAME):
        return f"{NAME} hook"
    exe = sys.executable
    return f'"{exe}" -m system_diff hook' if " " in exe else f"{exe} -m system_diff hook"


def _paths(scope: str, root: Path) -> tuple:
    home = Path.home() / ".claude"
    settings = {"user": home / "settings.json", "project": root / ".claude" / "settings.json",
                "local": root / ".claude" / "settings.local.json"}[scope]
    skills = home / "skills" if scope == "user" else root / ".claude" / "skills"
    return settings, skills / NAME / "SKILL.md"


def _ours(entry: Dict[str, Any]) -> bool:
    return any("system-diff hook" in h.get("command", "") or "system_diff hook" in h.get("command", "")
               for h in entry.get("hooks", []))


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


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _claude(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["claude", *args], capture_output=True, text=True)


def setup_claude(root: Path, scope: str = "user", undo: bool = False, out: Callable[[str], None] = print) -> int:
    """Add (or with `undo`, remove) the Stop hook, the skill and the MCP server, printing each change."""
    if scope not in SCOPES:
        raise SystemExit(f"--scope is one of {', '.join(SCOPES)}")
    settings_path, skill_path = _paths(scope, root)
    settings = _load(settings_path)
    stop = settings.setdefault("hooks", {}).setdefault("Stop", [])
    ours = [e for e in stop if _ours(e)]
    has_claude = shutil.which("claude") is not None
    if undo:
        if ours:
            settings["hooks"]["Stop"] = [e for e in stop if not _ours(e)]
            if not settings["hooks"]["Stop"]:
                del settings["hooks"]["Stop"]
            if not settings["hooks"]:
                del settings["hooks"]
            _save(settings_path, settings)
            out(f"removed the Stop hook from {settings_path}")
        if skill_path.is_file() and SKILL_MARK in skill_path.read_text(encoding="utf-8"):
            skill_path.unlink()
            if not any(skill_path.parent.iterdir()):
                skill_path.parent.rmdir()
            out(f"removed the skill {skill_path}")
        if has_claude and _claude(["mcp", "get", NAME]).returncode == 0:
            got = _claude(["mcp", "remove", "--scope", scope, NAME])
            out(f"removed the MCP server ({scope} scope)" if got.returncode == 0 else
                f"could not remove the MCP server: {(got.stderr or got.stdout).strip()[:200]}")
        out("system-diff is no longer wired into Claude Code" + ("" if scope == "user" else f" ({scope} scope)"))
        return 0

    if ours:
        out(f"the Stop hook is already in {settings_path}")
    else:
        stop.append({"hooks": [{"type": "command", "command": hook_command()}]})
        _save(settings_path, settings)
        out(f"added a Stop hook to {settings_path}: after each turn, one line on what changed in the system")
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    if skill_path.is_file() and skill_path.read_text(encoding="utf-8") == SKILL:
        out(f"the skill is already at {skill_path}")
    elif skill_path.is_file() and SKILL_MARK not in skill_path.read_text(encoding="utf-8"):
        out(f"left {skill_path} alone: someone else wrote it")
    else:
        with open(skill_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(SKILL)
        out(f"wrote the skill {skill_path}: when and how to show the system diff")
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
        got = _claude(["mcp", "add", "--scope", scope, NAME, "--", *_split(server), "mcp"])
        out(f"registered the MCP server ({scope} scope): system_map, system_diff, draw, checkpoint"
            if got.returncode == 0 else f"could not register the MCP server: {(got.stderr or got.stdout).strip()[:200]}")
    return 0


def _split(command: str) -> List[str]:
    """`"C:/Program Files/py/python.exe" -m system_diff` -> its words, the quoted path as one."""
    if command.startswith('"'):
        exe, rest = command[1:].split('"', 1)
        return [exe, *rest.split()]
    return command.split()
