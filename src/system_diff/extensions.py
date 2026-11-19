"""Extensions: other packages that add views to system-diff's own engine, web page and pane, so that one engine, one
port, one page and one pane serve them all, with system-diff's caching (pages kept per engine version) and its
diagrams. research-diff is one: its Evidence, Paper and Auto-research tabs beside Now, Review and the others.

An extension is found through the entry-point group "system_diff.extensions" (in its own pyproject.toml:
`[project.entry-points."system_diff.extensions"] research = "research_diff.system_diff_ext"`). The module it names
may define:

    NAME = "research"                                       # its routes are /x/<NAME>/...
    VIEWS = [("evidence", "Evidence", "e"), ...]            # (view, tab label, key); keys n r a t h b f m c are taken;
                                                            #   a 4th element: the system tab it stands for ("now",
                                                            #   "review", "agents", "history"): in its own set of tabs it
                                                            #   takes that tab's key
    TITLE = "Research"                                      # its set's name on the switch ("Research ▸")
    def pane(ctx) -> dict                                   # the pane's page: canvas.Page(...).json(view=..., part=...)
    def web(ctx) -> dict                                    # the web page's: pane.web_view's shape ("diagram" with
                                                            #   architecture_svg, "sections" of the known kinds, and
                                                            #   "frame" {"src": "/x/<NAME>/..."} for a page of its own)
    def route(ctx, method, path, query, body) -> (status, content_type, bytes)
                                                            # GET/POST /x/<NAME>/<path>; POST only with the token or
                                                            #   from the page itself (as /send)
    WATCH = ["research-diff/"]                              # paths under the git dir whose change moves the version
    def file_ok(path) -> bool                               # files the page's file viewer may show beside code
    def outline(ctx, path, text) -> {"outline": {"chunks": [{"line", "end", "depth", "qual", "name", "kind"}]},
                                     "summary": {"state", "file", "by", "chunks": {qual: {"what", "how", "watch"}}}}
                                                            # the viewer's outline and summary for a file it owns
    def mcp(server) -> None                                 # MCP tools of its own, on system-diff's server
    MCP_SERVER = {"command": "research-diff", "args": ["mcp"]}
                                                            # its own server, for Studio sessions (run as
                                                            #   `python -m <its package>` when it imports here)
    def applies(root) -> bool                               # whether its server is worth starting in a repository
    def workflow(ctx) -> {"steps": [step, ...], "waits": [{"from", "to", "why"}]}
                                                            # its steps on the workflow board; a step: {"id", "title",
                                                            #   "kind" (agent|run|check), "state" (waiting|running|done|
                                                            #   failed|paused), "agent"?, "session"? (a Studio session),
                                                            #   "log"? (text), "files"?, "actions"? (pause|resume|stop|
                                                            #   retry), "detail"?, "started"?, "finished"?}; a wait: "to"
                                                            #   waits for "from" to finish (step ids or agents' names)
    def workflow_action(ctx, id, action) -> {"ok", "said"}  # pause, resume, stop or retry one of its steps
    def step_log(ctx, id) -> {"entries": [{"at", "kind", "text"}], "about"}
                                                            # a long log for the board's pop-up (else the step's "log")
    WORKFLOW_KINDS = [{"kind", "title", "brief", "tools"?, "model"?}]
                                                            # agents Claude can add to a team from the chat (add_agent)
    WORKFLOW_TEMPLATES = [{"name", "description", "agents", "steps", "waits", "order"}]
                                                            # ready-made workflows, offered where applies(root); it saves
                                                            #   its own with system_diff.workflows.save(..., by=NAME)
    def projects(ctx) -> [{"path", "title", "why"}]         # the folders that are projects in a repository it applies
                                                            #   to (research-diff's studies), relative to the root: the
                                                            #   page's list of projects shows them (system-diff finds
                                                            #   others only by a manifest, never by a folder's name);
                                                            #   a function, not a module of that name
    def home(ctx) -> {"view": "research", "system": "collapsed"} | None
                                                            # the view a repository opens on (a research repository:
                                                            #   its own), system-diff's tabs behind one "System" tab
    def resolve(ctx, text) -> {"id", "file"} | None         # its own ids, for point_to, note and answers' links
    def source(ctx, block) -> {"files": [{"path", "line"}], "config": ...} | None
                                                            # where its block comes from (ui_source)

A view's JSON (web) may carry "nav": [{"id", "title", "sub", "badge", "active", "target": {"view", "part", "scope"?}}]
(scope: a project folder, picked as the page's own list picks it): a pinned list on the left of the page (a row of pickable blocks in the pane); a diagram's "files" may map a block to
{"path", "line", "end"}: a click opens the file there, those lines lit.

`ctx` is a Context: root (the repository shown: a submodule's when one is picked), parent (the repository it sits
inside, else None), view, part, width (pane), zoom, scope (the project folder picked on the page, "" the whole
repository), and `live` (compare.Live: the working tree as the engine
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
    parent: Optional[Path] = None
    scope: str = ""


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


_APPLIES: Dict[tuple, tuple] = {}
OPEN_VIEW: Optional[str] = None   # the view a page started by hand opens on: `system-diff watch` "now", --view VIEW
SYSTEM_TABS = ("now", "review", "agents", "topics", "history")


def applies(ext: Any, root: Optional[Path]) -> bool:
    """Whether an extension has something to do in this repository: system-diff.toml decides when it says
    (`extensions = false`, or a list of names), else the extension (`applies(root)`; one without it, everywhere). Kept
    while the repository's index and config are unchanged (an extension may ask git: a program start)."""
    if root is None:
        return True
    name = str(getattr(ext, "NAME", "") or "")
    try:
        from .config import lenient
        said = lenient(Path(root)).get("extensions")
    except Exception:  # noqa: BLE001
        said = None
    if said is False:
        return False
    if isinstance(said, list):
        return name in [str(x) for x in said]
    fn = getattr(ext, "applies", None)
    if fn is None:
        return True
    try:
        from .projects import _stamp
        stamp = _stamp(Path(root))
    except Exception:  # noqa: BLE001
        stamp = None
    key = (str(Path(root).resolve()), name)
    got = _APPLIES.get(key)
    if stamp is not None and got is not None and got[0] == stamp:
        return got[1]
    try:
        yes = bool(fn(Path(root)))
    except Exception as exc:  # noqa: BLE001 - not shown where it cannot tell
        failed(name + " applies", exc)
        yes = False
    if stamp is not None:
        _APPLIES[key] = (stamp, yes)
    return yes


def views(root: Optional[Path] = None) -> List[Tuple[str, str, str]]:
    """Every extension view: (name, label, key), without clashes with system-diff's or each other's; with `root`,
    only those of the extensions that apply there (research-diff's in a repository with research, not elsewhere)."""
    out, names, keys = [], set(BUILTIN), set(TAKEN_KEYS)
    for ext in load():
        if root is not None and not applies(ext, root):
            continue
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


def views_full(root: Optional[Path] = None) -> List[Dict[str, str]]:
    """views(root), each with its extension's NAME and the system-diff tab it stands for (a VIEWS entry's 4th
    element: research-diff's Now stands for "now"), "" for none."""
    out = []
    for name, label, key in views(root):
        ext = owner(name)
        entry = next((v for v in getattr(ext, "VIEWS", ()) or () if str(v[0]) == name), ()) if ext is not None else ()
        stands = str(entry[3]) if len(entry) > 3 and str(entry[3]) in SYSTEM_TABS else ""
        out.append({"view": name, "label": label, "key": key, "stands": stands,
                    "ext": str(getattr(ext, "NAME", "") or "")})
    return out


def title_of(ext: Any) -> str:
    return str(getattr(ext, "TITLE", "") or str(getattr(ext, "NAME", "") or "extension").replace("-", " ").title())


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


def context(root: Path, view: str, part: str = "", width: int = 100, zoom: int = 0, scope: str = "") -> Context:
    from . import compare, repos
    return Context(Path(root), view, part or "", width, zoom, compare.live(root), repos.parent(root), scope or "")


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


def home(root: Path) -> Optional[Dict[str, str]]:
    """The view this repository opens on, if an extension says so: {"view", "system"} ("collapsed": system-diff's own
    tabs behind one "System" tab), else None."""
    mine = {v for v, _l, _k in views(root)}
    for ext in load():
        f = getattr(ext, "home", None)
        if f is None or not applies(ext, root):
            continue
        try:
            got = f(context(root, "", ""))
        except Exception as exc:  # noqa: BLE001 - the usual first view
            failed(getattr(ext, "NAME", "extension") + " home", exc)
            continue
        if isinstance(got, dict) and got.get("view") in mine:
            return {"view": str(got["view"]), "system": "collapsed" if got.get("system") == "collapsed" else "",
                    "set": str(getattr(ext, "NAME", "") or ""), "title": title_of(ext)}
    return None


def outline(root: Path, path: str, text: str) -> Optional[Dict[str, Any]]:
    """The outline and summary an extension gives for a file it owns (file_ok), else None."""
    for ext in load():
        ok, out = getattr(ext, "file_ok", None), getattr(ext, "outline", None)
        if ok is None or out is None:
            continue
        try:
            if not ok(path):
                continue
            got = out(context(root, "", ""), path, text)
        except Exception as exc:  # noqa: BLE001 - the viewer shows the code without it
            failed(getattr(ext, "NAME", "extension") + " outline", exc)
            return {"outline": {"chunks": []}, "summary": {"state": "failed", "why": f"{type(exc).__name__}: {exc}"}}
        if isinstance(got, dict):
            return got
    return None


def failed(view: str, exc: BaseException) -> Dict[str, Any]:
    """What a view shows when its extension failed: the error, never a broken page."""
    text = f"The {view} view failed: {type(exc).__name__}: {str(exc)[:300]}"
    print(f"system-diff: extension view {view}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return {"text": text}
