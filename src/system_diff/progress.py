"""What a long read of the system is doing now, for the pane to show while it waits (a first read of a large
repository can take a while). One small file under the repository's git dir, written as the work goes and removed
when it is done; `system-diff progress` prints it."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

FILE = "progress.json"
_path: Optional[Path] = None
_started = 0.0
_last = 0.0
_stage = ""


def begin(root: Path) -> None:
    global _path, _started, _last, _stage
    try:
        from .sources import git_dir
        _path = git_dir(root) / FILE
    except Exception:  # noqa: BLE001 - only a convenience
        _path = None
    _started, _last, _stage = time.time(), 0.0, ""


def stage(text: str, done: Optional[int] = None, total: Optional[int] = None) -> None:
    """Where the work is: written when the stage changes, else at most a few times a second."""
    global _last, _stage
    if _path is None:
        return
    now = time.time()
    if text == _stage and now - _last < 0.3:
        return
    _last, _stage = now, text
    data = {"stage": text, "done": done, "total": total, "started": _started, "pid": os.getpid()}
    try:
        tmp = _path.with_name(f"{FILE}.{os.getpid()}")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, _path)
    except OSError:
        pass


def end() -> None:
    global _path
    if _path is not None:
        try:
            _path.unlink()
        except OSError:
            pass
    _path = None


def line(root: Path) -> str:
    """The progress as one line ("Reading the code: 340 of 1,234 files - 12s"), or "" when nothing is under way."""
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=root, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", check=True).stdout.strip()
        data = json.loads((Path(out) / "system-diff" / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, subprocess.CalledProcessError):
        return ""
    if time.time() - float(data.get("started") or 0) > 900:
        return ""  # left by a run that died
    text = str(data.get("stage") or "")
    if data.get("total"):
        text += f": {data.get('done') or 0:,} of {data['total']:,} files"
    return f"{text} - {int(time.time() - float(data.get('started') or time.time()))}s"
