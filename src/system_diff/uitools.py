"""The page as tools for Claude: see what the person looks at, move their page to what Claude explains, read a view's
diagram as data, find where a block comes from, and preview a system-diff.toml change before making it.

The MCP server runs in Claude Code's process tree, the page's state lives in the engine (the repository's resident
`system-diff watch`): the tools ask the engine over its local port with the token from web.json, and fall back to
reading in this process when no engine runs. Moving the page is a request kept beside the conversation's files
(.git/system-diff/chat/show.json): the page and the pane act on it at their next look.

Changing what the diagrams show goes through their sources (the code, system-diff.toml), never hidden page state:
`source` says which file or config entry makes a block, `config_preview` shows what an edit of the config would draw.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

GRAPH_MOST = 60


def _web_json(root: Path) -> Dict[str, Any]:
    try:
        from .sources import git_dir
        return json.loads((git_dir(root) / "web.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def engine_get(root: Path, path: str, timeout: float = 20.0) -> Optional[Dict[str, Any]]:
    """GET a JSON route of this repository's running engine (with its token), or None when none answers."""
    info = _web_json(root)
    url, token = str(info.get("url") or ""), str(info.get("token") or "")
    if not url or not token:
        return None
    try:
        req = urllib.request.Request(url.rstrip("/") + "/" + path.lstrip("/"), headers={"X-Token": token})
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - 127.0.0.1, this user's engine
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def project_root(root: Path, project: str = "") -> Path:
    """The repository a tool is asked about: this one (""), a path, a member of this one's family (a submodule's
    path), or a running engine's project by name."""
    if not project:
        return root
    p = Path(project).expanduser()
    if p.is_dir():
        return p.resolve()
    from . import repos
    hit = repos.member(root, project.strip("/"))
    if hit is not None:
        return hit
    try:
        from . import hub
        for e in hub.projects():
            if project in (e.get("name"), e.get("title"), e.get("root")) and e.get("root"):
                return Path(e["root"])
    except Exception:  # noqa: BLE001
        pass
    raise ValueError(f"no project {project!r}: give a path, a submodule's path, or a running project's name")


def views() -> List[Dict[str, str]]:
    from . import extensions
    own = [("now", "Now"), ("review", "Review"), ("agents", "Agents"), ("topics", "Topics"), ("history", "History")]
    return [{"view": v, "label": label, "by": "system-diff"} for v, label in own] + \
           [{"view": v, "label": label, "by": "extension"} for v, label, _k in extensions.views()]


# ── seeing ──────────────────────────────────────────────────────────────────────────────────────────────────────

def view(root: Path, project: str = "") -> Dict[str, Any]:
    """What the person looks at now: each open page (and the pane) with its view, part, picked and hovered block (its
    title and the headline of its details), whether it is visible; and the views there are."""
    r = project_root(root, project)
    got = engine_get(r, "ui/state?full=1")
    if got is None:
        return {"project": str(r), "pages": [], "views": views(),
                "note": "no system-diff engine runs for this repository, so no page is open on it"}
    pages = sorted(got.get("pages") or [], key=lambda p: (not p.get("visible"), -float(p.get("at") or 0)))
    return {"project": str(r), "pages": pages, "views": views(),
            "hint": "the first page is the one most likely in front of the person (visible, most recent)"}


def _strip(page: Dict[str, Any], most: int) -> Dict[str, Any]:
    """A /web page as data: the diagram's nodes, edges and details without the SVG, and the sections as text."""
    g = page.get("diagram") or {}
    graph = g.get("graph") or {}
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    details = g.get("details") or {}
    if not nodes:   # an extension's view without a model: its blocks as the page knows them
        ids = sorted(set(details) | set(g.get("enter") or {}) | set(g.get("files") or {}))
        nodes = [{"id": i[2:] if i.startswith("b:") else i, "title": (details.get(i) or {}).get("title", "")
                  if isinstance(details.get(i), dict) else ""} for i in ids]
    focus = {str(g.get("pick") or "")}
    rank = lambda n: (n.get("status", "same") == "same", "b:" + str(n.get("id")) not in focus,   # noqa: E731
                      not n.get("agent"), str(n.get("id")))
    nodes = sorted(nodes, key=rank)
    shown = nodes[:most]
    keep = {str(n.get("id")) for n in shown}
    edges = [e for e in edges if str(e.get("from")) in keep and str(e.get("to")) in keep]

    def said(d: Any) -> Any:
        if isinstance(d, dict):
            return {k: d[k] for k in ("title", "headline", "what", "how", "insights", "inside") if d.get(k)}
        return d
    sections = []
    for s in page.get("sections") or []:
        items = []
        for it in (s.get("items") or [])[:20]:
            if isinstance(it, dict):
                items.append({k: v for k, v in it.items() if isinstance(v, (str, int, float)) and k != "svg"})
            else:
                items.append(it)
        sections.append({"title": s.get("title", ""), "kind": s.get("kind", ""), "aside": s.get("aside", ""),
                         "items": items})
    return {"view": page.get("view", ""), "part": page.get("part", ""), "title": page.get("title", ""),
            "headline": page.get("headline", ""), "columns": graph.get("columns") or [],
            "nodes": shown, "edges": edges, "more_nodes": max(0, len(nodes) - len(shown)),
            "details": {k[2:] if k.startswith("b:") else k: said(v) for k, v in details.items()
                        if (k[2:] if k.startswith("b:") else k) in keep},
            "zooms_into": {k[2:] if k.startswith("b:") else k: v for k, v in (g.get("enter") or {}).items()},
            "files": {k[2:] if k.startswith("b:") else k: v for k, v in (g.get("files") or {}).items()},
            "sections": sections}


def graph(root: Path, view_: str = "now", part: str = "", project: str = "", limit: int = GRAPH_MOST
          ) -> Dict[str, Any]:
    """A view's diagram as data (system-diff's views and every extension's), most relevant first."""
    r = project_root(root, project)
    most = max(5, min(int(limit or GRAPH_MOST), 400))
    q = urllib.parse.urlencode({"view": view_, "part": part, "zoom": 0})
    page = engine_get(r, "web?" + q, timeout=60)
    if page is None or page.get("error"):
        from . import pane
        page = pane.web_view(r, view_, part, 0)
    return _strip(page, most)


# ── where a block comes from ────────────────────────────────────────────────────────────────────────────────────

def _files(root: Path) -> List[str]:
    """The repository's files, from git's index (read in Python: no git process). git_dir() is system-diff's folder
    inside the git dir, so the index is beside it. Without an index, the folders walked skip what the config skips
    (node_modules, virtualenvs, build output): never the whole tree."""
    try:
        from . import gitfs
        from .sources import git_dir
        entries, _ = gitfs.read_index(git_dir(root).parent / "index")
        return sorted(entries)
    except Exception:  # noqa: BLE001
        from .config import lenient
        from .extract import Code
        from .sources import DirSource
        return DirSource(root, Code(lenient(root)).skip).files


def _toml_entry(e: Dict[str, Any]) -> str:
    lines = ["[[components]]"]
    for k, v in e.items():
        lines.append(f"{k} = {json.dumps(v)}")
    return "\n".join(lines)


def source(root: Path, block: str, project: str = "") -> Dict[str, Any]:
    """The files (and lines) behind a block and the system-diff.toml entry that names or places it."""
    from . import config, extensions
    from .extract import Code
    r = project_root(root, project)
    bid = block[2:] if block.startswith("b:") else block
    cfg = config.lenient(r)
    code = Code(cfg)
    out: Dict[str, Any] = {"block": bid, "project": str(r)}
    if bid.startswith("f:"):
        out.update(kind="file", files=[{"path": bid[2:]}],
                   config="a file is drawn inside the part that owns it: change the part's paths to move it")
        return out
    files = _files(r)
    declared = next((c for c in cfg.get("components", []) if c.get("id") == bid), None)
    mine = [f for f in files if code.is_code(f) and code.part_of(f) == bid]
    if mine or declared is not None:
        out.update(kind="part", files=[{"path": f} for f in mine[:80]], more_files=max(0, len(mine) - 80))
        if declared is not None:
            out["config"] = {"in": "system-diff.toml", "entry": declared, "toml": _toml_entry(declared),
                             "change": "edit this [[components]] entry: label names the box, paths place files in "
                                       "it, layer and container group it"}
        else:
            folder = bid
            out["config"] = {"in": "none: named from its folder",
                             "toml": _toml_entry({"id": bid, "label": bid.rsplit('/', 1)[-1].title(),
                                                  "paths": [f"{folder}/**"]}),
                             "change": "add this entry to system-diff.toml to name the box or place its files"}
        return out
    ext_out = next((x for x in cfg.get("externals", []) if x.get("id") == bid), None)
    if ext_out is not None:
        out.update(kind="outside system", config={"in": "system-diff.toml", "entry": ext_out})
        return out
    for ext in extensions.load():   # an extension's block: its file and line (a claim in the paper)
        find = getattr(ext, "source", None)
        if find is None:
            continue
        try:
            got = find(extensions.context(r, "", bid), bid)
        except Exception as exc:  # noqa: BLE001
            got = {"error": f"{type(exc).__name__}: {exc}"}
        if got:
            out.update(kind="extension", by=getattr(ext, "NAME", ""), **got)
            return out
    out.update(kind="unknown", note="no part, file or outside system of that id; ui_graph lists the ids")
    return out


# ── preview a config change ─────────────────────────────────────────────────────────────────────────────────────

def config_preview(root: Path, changes: Any, view_: str = "now", part: str = "", project: str = "",
                   limit: int = GRAPH_MOST) -> Dict[str, Any]:
    """The diagram an edit of system-diff.toml would give (as `graph` gives it) and what differs from now; nothing
    is written. `changes`: a TOML snippet (as it would go in the file) or the same as a dict."""
    from . import config, pane
    r = project_root(root, project)
    if isinstance(changes, str):
        import tomllib
        try:
            over = tomllib.loads(changes)
        except tomllib.TOMLDecodeError as exc:
            return {"error": f"not TOML: {exc}"}
    elif isinstance(changes, dict):
        over = changes
    else:
        return {"error": "changes: a TOML snippet or a table"}
    now = _strip(pane.web_view(r, view_, part, 0), max(5, min(int(limit or GRAPH_MOST), 400)))
    with config.preview(over):
        then = _strip(pane.web_view(r, view_, part, 0), max(5, min(int(limit or GRAPH_MOST), 400)))
    a = {str(n.get("id")): n for n in now["nodes"]}
    b = {str(n.get("id")): n for n in then["nodes"]}
    renamed = [{"id": i, "from": a[i].get("title"), "to": b[i].get("title")} for i in a.keys() & b.keys()
               if a[i].get("title") != b[i].get("title")]
    moved = [{"id": i, "from": a[i].get("col"), "to": b[i].get("col")} for i in a.keys() & b.keys()
             if a[i].get("col") != b[i].get("col")]
    return {"written": False, "would_draw": then,
            "differs": {"added": sorted(b.keys() - a.keys()), "removed": sorted(a.keys() - b.keys()),
                        "renamed": renamed, "moved": moved},
            "next": "edit system-diff.toml with these changes yourself (the person sees the edit); the page redraws"}


# ── is the diagram right ────────────────────────────────────────────────────────────────────────────────────────

def check(root: Path, part: str = "", view_: str = "now", project: str = "") -> Dict[str, Any]:
    """What could be wrong in a diagram, against the code (diagcheck.check): asked of the engine when it answers
    (it holds the working tree read), else worked out here from what is kept."""
    from . import diagcheck
    r = project_root(root, project)
    got = engine_get(r, "check?" + urllib.parse.urlencode({"part": part, "view": view_}), timeout=30)
    if isinstance(got, dict) and "findings" in got:
        return got
    return diagcheck.check(r, part, view_)


def report_issue(root: Path, part: str, wrong: str, expected: str, evidence: str = "", project: str = ""
                 ) -> Dict[str, Any]:
    from . import diagcheck
    return diagcheck.report(project_root(root, project), part, wrong, expected, evidence)


# ── moving the page ─────────────────────────────────────────────────────────────────────────────────────────────

def show(root: Path, view_: str, part: str = "", pick: str = "", note: str = "", project: str = "",
         wait: float = 3.0) -> Dict[str, Any]:
    """Ask the person's page (and pane) to show `view` (zoomed into `part`, `pick` picked), with `note` on its
    banner; then wait a moment for a page to say whether it moved or kept it waiting."""
    from . import chat
    known = {v["view"] for v in views()}
    if view_ not in known:
        return {"error": f"no view {view_!r}: one of {', '.join(sorted(known))}"}
    scope = None
    try:   # a folder project of this repository: the same page, drawn for that folder
        from . import projects
        if project and projects.valid(root, project.strip("/")):
            scope = project.strip("/")
    except Exception:  # noqa: BLE001
        pass
    r = root if scope is not None else project_root(root, project)
    rec = chat.show_page(r, view_, part, pick, note, scope=scope)
    deadline = time.time() + max(0.0, min(wait, 10.0))
    state = None
    while time.time() < deadline:
        got = engine_get(r, "ui/state", timeout=2)
        hits = [p for p in (got or {}).get("pages") or [] if p.get("shown") == rec["id"]]
        if hits:
            state = "moved" if any(p.get("moved") for p in hits) else "offered"
            break
        if got is None:
            break
        time.sleep(0.25)
    return {"id": rec["id"], "view": view_, "part": part, "pick": pick,
            "page": state or "queued: no page has taken it yet (it will at its next look, unless the person is "
                              "busy on it; the pane shows it at its next look)"}
