"""Append-only JSON-lines logs (agent runs, the bus, the activity log), read again on every draw of a page: parsed
once, then only the lines added since (a file that shrank or was replaced is read again whole). Thread-safe."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple

_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[int, int, int, List[Any]]] = {}   # path → (inode, offset read up to, size then, parsed)
MOST = 64            # files kept


def read(path: Path) -> List[Any]:
    """The JSON objects of the file's complete lines, in order (a list the caller must not change)."""
    key = os.path.normcase(str(path))
    try:
        st = os.stat(path)
    except OSError:
        return []
    with _LOCK:
        got = _CACHE.get(key)
    ino = getattr(st, "st_ino", 0)
    if got is not None and got[0] == ino and got[2] == st.st_size:
        return got[3]
    start, items = (got[1], list(got[3])) if got is not None and got[0] == ino and st.st_size >= got[2] else (0, [])
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return got[3] if got is not None else []
    end = data.rfind(b"\n") + 1            # a line still being written waits for its newline
    for line in data[:end].splitlines():
        try:
            items.append(json.loads(line.decode("utf-8", "replace")))
        except ValueError:
            continue
    with _LOCK:
        _CACHE[key] = (ino, start + end, start + end if end < len(data) else st.st_size, items)
        while len(_CACHE) > MOST:
            _CACHE.pop(next(iter(_CACHE)))
    return items
