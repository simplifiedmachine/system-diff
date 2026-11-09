"""What an AI coding agent is doing, as it does it: the prompts it is given and the files it reads and edits (written
by Claude Code's hooks), and the files that change on disk (seen by the live views). From these, the FOCUS: the part
of the system it is working on now, which the terminal view zooms in on and the status line names.

Kept in the repository's own git dir (.git/system-diff/, a linked worktree's own), never committed, never sent
anywhere: activity.jsonl, one event a line, newest last, trimmed as it grows; and state.json, the latest diff in a few
words, which the status line shows without reading any code.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .diff import short
from .extract import Code
from .sources import git_dir

LOG, STATE = "activity.jsonl", "state.json"
MAX_BYTES, KEEP = 256 * 1024, 400   # past MAX_BYTES, only the newest KEEP events stay
EDIT, READ, PROMPT, STOP = "edit", "read", "prompt", "stop"


SYSTEM_PROMPTS = ("<task-notification", "<command-name", "<command-message", "<local-command", "<system-reminder",
                  "<bash-input", "<bash-stdout", "<user-memory-input")


def is_system(text: str) -> bool:
    """A prompt the person did not type: a background task's notice, a slash command's echo, a reminder."""
    return (text or "").lstrip().startswith(SYSTEM_PROMPTS)


def record(root: Path, event: Dict[str, Any]) -> None:
    """Add one event, e.g. {"event": "edit", "path": "app/api/routes.py"}, stamped with the time."""
    path = git_dir(root) / LOG
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"at": round(time.time(), 3), **event}, ensure_ascii=False) + "\n")
    if path.stat().st_size > MAX_BYTES:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-KEEP:]
        _replace(path, "\n".join(lines) + "\n")


def events(root: Path, limit: int = 200) -> List[dict]:
    """The newest `limit` events, oldest first."""
    try:
        lines = (git_dir(root) / LOG).read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue  # a line cut short by a write that raced the trim
        if isinstance(e, dict) and isinstance(e.get("at"), (int, float)):
            out.append(e)
    return out


def relative(root: Path, path: str) -> Optional[str]:
    """A file an agent names (absolute, with the platform's separators) as a repository path; None outside it."""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def part_of(path: str, code: Code) -> Optional[str]:
    """The part a file belongs to: only code counts, not its tests, docs or config."""
    return code.part_of(path) if code.is_code(path) and not code.is_test(path) else None


def now(evs: Iterable[dict], code: Code) -> Dict[str, Any]:
    """What is going on: the latest prompt, whether the agent is still at it, what it said when it last stopped, the
    file it touched last, and the FOCUS: the part of its latest edit since the prompt, else of its latest read, else
    (between turns) of its latest edit at all. {"part": None} before it touched any code."""
    out: Dict[str, Any] = {"part": None, "path": "", "doing": "", "at": 0.0, "latest": None,
                           "prompt": "", "said": "", "working": False}
    start = 0.0
    touched = []
    for e in evs:
        kind = e.get("event")
        if kind == PROMPT and not is_system(e.get("text", "")):
            start, out["prompt"], out["working"] = e["at"], e.get("text", ""), True
        elif kind == STOP:
            out["said"], out["working"] = e.get("text", ""), False
        elif kind in (EDIT, READ) and e.get("path"):
            touched.append(e)
    if touched:
        last = touched[-1]
        out["latest"] = {"path": last["path"], "doing": "editing" if last["event"] == EDIT else "reading",
                         "at": last["at"]}
    this_turn = [e for e in touched if e["at"] >= start]
    for group in ([e for e in this_turn if e["event"] == EDIT], this_turn, [e for e in touched if e["event"] == EDIT]):
        hit = next((e for e in reversed(group) if part_of(e["path"], code)), None)
        if hit:
            out.update(part=part_of(hit["path"], code), path=hit["path"], at=hit["at"],
                       doing="editing" if hit["event"] == EDIT else "reading")
            break
    return out


def label(cfg: Dict[str, Any], part: Optional[str]) -> str:
    """What the config calls a part (a part nobody named is called by its folder)."""
    return next((c.get("label", part) for c in cfg.get("components", []) if c.get("id") == part), part or "")


def feed(evs: Iterable[dict], code: Code, cfg: Dict[str, Any], most: int = 14) -> List[Dict[str, Any]]:
    """What happened, newest first, in plain words: what was asked, what the agent read and edited (a run of reads
    or edits in one part told once), what each step changed in the system and why that matters (`change` events,
    from the live views), and what the agent said when it stopped."""
    out: List[Dict[str, Any]] = []
    for e in sorted(evs, key=lambda e: e["at"]):
        kind, path = e.get("event"), e.get("path", "")
        if kind == PROMPT and not is_system(e.get("text", "")):
            out.append({"at": e["at"], "kind": kind, "text": f"You asked: {e.get('text', '')}"})
        elif kind in (READ, EDIT) and path and e.get("source") != "files":
            part = part_of(path, code)
            name = label(cfg, part) if part else path
            last = out[-1] if out else None
            if last and last["kind"] == kind and last.get("part") == part and (part or last["paths"] == {path}):
                last["paths"].add(path)
                last["at"] = e["at"]
            else:
                last = {"at": e["at"], "kind": kind, "part": part, "name": name, "paths": {path}}
                out.append(last)
            verb = "reads" if kind == READ else "edits"
            n = len(last["paths"])
            last["text"] = f"Claude {verb} {name}" + (f" ({path})" if part and n == 1 else f" ({n} files)" if n > 1 else "")
        elif kind == "change":
            out += [{"at": e["at"], "kind": kind, "text": item["text"], "why": item.get("why", "")}
                    for item in e.get("items", [])]
        elif kind == STOP:
            said = e.get("text", "")
            out.append({"at": e["at"], "kind": kind, "text": "Claude stopped" + (f": {said}" if said else ".")})
    return [{k: v for k, v in item.items() if k != "paths"} for item in reversed(out)][:most]


# ── what to teach about a part, kept for the status line ─────────────────────

PARTS = "parts"


def _brief_path(root: Path, part: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in part)[:60]
    return git_dir(root, PARTS) / f"{safe}-{hashlib.sha1(part.encode()).hexdigest()[:8]}.json"


def save_brief(root: Path, brief: Dict[str, Any]) -> None:
    _replace(_brief_path(root, brief["part"]), json.dumps(brief, ensure_ascii=False) + "\n")


def load_brief(root: Path, part: str, max_age: Optional[float] = None) -> Dict[str, Any]:
    """The brief learn.brief made for a part ({} when none, or when older than `max_age` seconds)."""
    path = _brief_path(root, part)
    try:
        if max_age is not None and time.time() - path.stat().st_mtime > max_age:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def only_one(root: Path, wait: float = 0.0, stale: float = 120.0) -> Iterator[bool]:
    """Whether this process may do the work now: one hook at a time looks at the code, the others waiting up to
    `wait` seconds for it; a lock left by one that died (older than `stale` seconds) is taken over."""
    path = git_dir(root) / "hook.lock"
    got = False
    give_up = time.time() + wait
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            got = True
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink()
                    continue
            except OSError:
                pass
            if time.time() >= give_up:
                break
            time.sleep(0.1)
    try:
        yield got
    finally:
        if got:
            try:
                path.unlink()
            except OSError:
                pass


def ago(at: float, clock: Optional[float] = None) -> str:
    s = max(0, int((clock or time.time()) - at))
    return "just now" if s < 5 else f"{s}s ago" if s < 60 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago"


# ── state.json: the latest diff in a few words ───────────────────────────────

def write_state(root: Path, since: Optional[dict], label_since: str = "checkpoint", turn: Optional[dict] = None,
                noted: Optional[List[str]] = None) -> None:
    """Keep the diff since the checkpoint, and this turn's, in a few words and as sentences (learn.story); None for
    nothing new. `noted`: the turn's design notes (design.py) by their text, for telling what the next edit adds."""
    from .learn import story

    def brief(d: Optional[dict]) -> Dict[str, Any]:
        changes = (d or {}).get("changes", [])
        told = story(d or {})
        return {"changes": len(changes), "short": short(changes), "story": told[:6], "count": len(told),
                "told": [t["text"] for t in told][:500],   # every sentence, for telling what the next edit adds
                "headline": (d or {}).get("headline", "No change to the system's structure.")}
    state = {"at": round(time.time(), 3), "since": label_since, **brief(since), "turn": brief(turn) if turn else None}
    if state["turn"] is not None:
        state["turn"]["noted"] = list(noted or [])[:500]
    _replace(git_dir(root) / STATE, json.dumps(state, ensure_ascii=False) + "\n")


def new_turn(root: Path) -> None:
    """A prompt was sent: the turn has changed nothing yet."""
    state = read_state(root)
    if state:
        state["turn"] = {"changes": 0, "short": "", "story": [], "count": 0, "told": [], "noted": [],
                         "headline": "No change to the system's structure."}
        _replace(git_dir(root) / STATE, json.dumps(state, ensure_ascii=False) + "\n")


def after_checkpoint(root: Path, ref: Optional[str]) -> None:
    """The checkpoint moved: nothing is new when it marked the working tree; unknown until the next look otherwise."""
    if ref:
        try:
            (git_dir(root) / STATE).unlink()
        except FileNotFoundError:
            pass
    else:
        write_state(root, None)


def read_state(root: Path) -> Dict[str, Any]:
    try:
        data = json.loads((git_dir(root) / STATE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _replace(path: Path, text: str) -> None:
    """Write `text` to a file beside `path`, then move it over: a reader never sees half a file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)
