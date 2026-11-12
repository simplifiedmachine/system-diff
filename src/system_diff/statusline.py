"""`system-diff statusline`: what Claude Code's status line runs, for someone watching the agent work. Up to three rows:
what it is doing now and what that part of the system is; the most important thing this turn has done to the system,
and why it matters; and something to learn about the part, a new topic every TIP_EVERY seconds.

Made from what the hooks and the live views last wrote under .git/system-diff/: it reads no code, so it stays fast
(Claude Code runs it after every message). Prints nothing outside a repository.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import activity, config, textmap
from .extract import Code
from .sources import git

WIDE = 150      # a row is cut here: Claude Code does not say how wide the terminal is
TIP_EVERY = 20  # seconds each topic stays before the next


def _first_sentence(text: str) -> str:
    head = text.split(". ")[0].split(": ")[0].rstrip(".")
    return head + "."


def _cut(row: textmap.Line, n: int) -> textmap.Line:
    out: textmap.Line = []
    for text, style in row:
        if n <= 0:
            break
        out.append((text if len(text) <= n else text[: n - 1] + "…", style))
        n -= len(text)
    return out


def status_lines(now: Dict[str, Any], state: Dict[str, Any], brief: Dict[str, Any], cfg: Dict[str, Any],
                 clock: Optional[float] = None) -> List[textmap.Line]:
    """The rows, as textmap lines: `now` is activity.now, `state` activity.read_state, `brief` learn.brief of the
    part being worked on."""
    rows: List[textmap.Line] = []
    part = now.get("part")
    working = now.get("working")
    if part:
        name = brief.get("label") or activity.label(cfg, part)
        verb = ("✎ Editing" if now["doing"] == "editing" else "Reading") if working else "Last worked on"
        row: textmap.Line = [(f"{verb} {name}", "focus")]
        if brief.get("role"):
            row.append((f" · {brief['role']}", "muted"))
        rows.append(row)
    turn = state.get("turn") or {}
    told = turn.get("story") or []
    if told:
        top = told[0]
        row = [("This turn: " if working else "Last turn: ", "muted"), (top["text"].rstrip("."), "bold")]
        if top.get("why"):
            row.append((f" · {top['why']}", "muted"))
        count = turn.get("count", len(told))
        if count > 1:
            row.append((f" (and {count - 1} more)", "muted"))
        rows.append(row)
    elif working and part:
        rows.append([("This turn: nothing in the system's structure has changed yet", "muted")])
    topics = brief.get("topics") or []
    if topics:
        title, what = topics[int((time.time() if clock is None else clock) // TIP_EVERY) % len(topics)]
        rows.append([("Learn: ", "changed"), (title, "bold"), (f" · {_first_sentence(what)}", "muted")])
    if not rows:
        rows.append([("system-diff: as Claude works, what it touches, what that changes and why will show here",
                      "muted")])
    return [_cut(row, WIDE) for row in rows]


def statusline(stdin_text: str, emit: Callable[[str], None] = print, color: bool = True) -> int:
    """Read Claude Code's JSON from stdin (only the folder it works in matters here) and print the rows."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    cwd = (payload.get("workspace") or {}).get("current_dir") or payload.get("cwd") or os.getcwd()
    try:
        from . import gitfs
        found = gitfs.locate(Path(cwd))   # no git process: the status line is drawn again and again
        root = found.top if found is not None else Path(git("rev-parse", "--show-toplevel", cwd=Path(cwd)).strip())
        cfg = config.lenient(root)
        now = activity.now(activity.events(root, 80), Code(cfg))
        brief = activity.load_brief(root, now["part"]) if now["part"] else {}
        rows = status_lines(now, activity.read_state(root), brief, cfg)
        emit((textmap.ansi if color else textmap.plain)(rows))
    except (OSError, subprocess.CalledProcessError):
        return 0
    return 0
