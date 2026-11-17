"""Extensions: other packages that add views to system-diff's own engine, web page and pane, so that one engine, one
port, one page and one pane serve them all, with system-diff's caching (pages kept per engine version) and its
diagrams. research-diff is one: its Evidence, Paper and Auto-research tabs beside Now, Review and the others.

An extension is found through the entry-point group "system_diff.extensions" (in its own pyproject.toml:
`[project.entry-points."system_diff.extensions"] research = "research_diff.system_diff_ext"`). The module it names
may define:

    NAME = "research"                                       # its routes are /x/<NAME>/...
    VIEWS = [("evidence", "Evidence", "e"), ...]            # (view, tab label, key); keys n r a t h b f m c are taken
    def pane(ctx) -> dict                                   # the pane's page: canvas.Page(...).json(view=..., part=...)
    def web(ctx) -> dict                                    # the web page's: pane.web_view's shape ("diagram" with
                                                            #   architecture_svg, "sections" of the known kinds, and
                                                            #   "frame" {"src": "/x/<NAME>/..."} for a page of its own)
    def route(ctx, method, path, query, body) -> (status, content_type, bytes)
                                                            # GET/POST /x/<NAME>/<path>; POST only with the token or
                                                            #   from the page itself (as /send)
    WATCH = ["research-diff/"]                              # paths under the git dir whose change moves the version
    def file_ok(path) -> bool                               # files the page's file viewer may show beside code
    def mcp(server) -> None                                 # MCP tools of its own

`ctx` is a Context: root, view, part, width (pane), zoom, and `live` (compare.Live: the working tree as the engine
holds it, `live.source` reading files without git, `live.identity` the same for the same files: a key to cache by),
None outside the engine.

An extension that fails to load or answer is skipped and said (errors()); it never stops system-diff.
SYSTEM_DIFF_EXTENSIONS=off loads none.
"""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GROUP = "system_diff.extensions"
BUILTIN = ("now", "review", "map", "changes", "agents", "topics", "history")
TAKEN_KEYS = set("nratbhfmc")
_LOCK = threading.Lock()
_LOADED: Optional[List[Any]] = None
_ERRORS: List[str] = []


@dataclass
class Context:
    root: Path
    view: str
    part: str = ""
    width: int = 100
    zoom: int = 0
    live: Any = None


def load() -> List[Any]:
    """The extensions installed, loaded once. Tests may set _LOADED."""
    global _LOADED
    with _LOCK:
        if _LOADED is not None:
            return _LOADED
        found: List[Any] = []
        if os.environ.get("SYSTEM_DIFF_EXTENSIONS", "").lower() not in ("off", "0", "no", "false"):
            try:
                from importlib.metadata import entry_points
                eps = entry_points(group=GROUP)
            except Exception as exc:  # noqa: BLE001
                eps = []
                _ERRORS.append(f"entry points: {exc}")
            for ep in sorted(eps, key=lambda e: e.name):
                try:
                    found.append(ep.load())
                except Exception as exc:  # noqa: BLE001 - one broken extension must not stop system-diff
                    _ERRORS.append(f"{ep.name}: {type(exc).__name__}: {exc}")
        _LOADED = found
        return found


def errors() -> List[str]:
    return list(_ERRORS)


def views() -> List[Tuple[str, str, str]]:
    """Every extension view: (name, label, key), without clashes with system-diff's or each other's."""
    out, names, keys = [], set(BUILTIN), set(TAKEN_KEYS)
    for ext in load():
        for v in getattr(ext, "VIEWS", ()) or ():
            try:
                name, label, key = str(v[0]), str(v[1]), str(v[2] or "")[:1]
            except (TypeError, IndexError):
                continue
            if name in names:
                continue
            names.add(name)
            key = key if key and key not in keys else ""
            keys.add(key)
            out.append((name, label, key))
    return out


def owner(view: str) -> Optional[Any]:
    """The extension that draws `view`, else None (a view of system-diff's own)."""
    if view in BUILTIN:
        return None
    for ext in load():
        if any(str(v[0]) == view for v in getattr(ext, "VIEWS", ()) or ()):
            return ext
    return None


def by_name(name: str) -> Optional[Any]:
    return next((e for e in load() if getattr(e, "NAME", "") == name), None)


def context(root: Path, view: str, part: str = "", width: int = 100, zoom: int = 0) -> Context:
    from . import compare
    return Context(Path(root), view, part or "", width, zoom, compare.live(root))


def watched(gitdir: Path) -> tuple:
    """The stamps of what the extensions watch under the git dir: a change moves the engine's version."""
    out = []
    for ext in load():
        for rel in getattr(ext, "WATCH", ()) or ():
            p = Path(gitdir) / rel
            try:
                if p.is_dir():
                    out.append(tuple(sorted((e.name, e.stat().st_mtime_ns) for e in os.scandir(p))))
                else:
                    s = p.stat()
                    out.append((s.st_mtime_ns, s.st_size))
            except OSError:
                out.append(None)
    return tuple(out)


def file_ok(path: str) -> bool:
    for ext in load():
        f = getattr(ext, "file_ok", None)
        try:
            if f is not None and f(path):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def failed(view: str, exc: BaseException) -> Dict[str, Any]:
    """What a view shows when its extension failed: the error, never a broken page."""
    text = f"The {view} view failed: {type(exc).__name__}: {str(exc)[:300]}"
    print(f"system-diff: extension view {view}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return {"text": text}
