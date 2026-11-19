"""Projects inside one repository. Many repositories hold several independent projects as plain folders (a monorepo's
api/, web/, worker/, each with its own manifest; a research repository's 1_x/ ... 6_y/), some of them git submodules
and most not. Each is shown as a system of its own: the same engine, its views scoped to the project's folder (the
files no project claims are shared by all). Git submodules and nested repositories keep their own engines (repos.py)
and are listed beside them.

Found by what folders hold, never by their names: from the repository's file list, cheaply, folders with their own
manifest (pyproject.toml, package.json, go.mod, ...), never shared folders (scripts/, docs/, tests/, ...) or the root's
own manifest; and the folders an extension names where it applies (its `projects(ctx)` hook: research-diff's studies),
which win over a manifest's for the same folder. The outermost folder wins. Fewer than two: none. system-diff.toml decides when it says so: `projects = ["api", "web"]` (or
[[projects]] tables with name and path) names them, `projects = false` turns finding them off. The person can show
the repository as one project instead (or split it again): kept under .git/system-diff/projects.json.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

MANIFESTS = {"pyproject.toml", "setup.py", "setup.cfg", "package.json", "go.mod", "Cargo.toml", "pom.xml",
             "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "Dockerfile", "docker-compose.yml",
             "docker-compose.yaml", "compose.yml", "compose.yaml", "mix.exs", "pubspec.yaml", "Package.swift"}
SHARED = {"scripts", "script", "results", "data", "datasets", "docs", "doc", "tools", "tests", "test", ".github",
          "node_modules", ".venv", "venv", "env", "build", "dist", "out", "target", "vendor", "third_party", "assets",
          "static", "public", "examples", "example", "notebooks", "configs", "config", "ci", ".devcontainer",
          "site-packages", "__pycache__", "figures", "paper", "papers", "logs", "outputs"}
DEPTH = 2          # how deep a manifest is looked for: api/pyproject.toml, services/api/package.json
STATE = "projects.json"
_CACHE: Dict[str, tuple] = {}
_ASKING = threading.local()   # roots whose extensions are being asked in this thread: one asking back gets the
                              # manifests' projects, never a loop


def _files(root: Path) -> List[str]:
    from .uitools import _files as listing
    return listing(root)


def _stamp(root: Path) -> tuple:
    try:
        from .sources import git_dir
        import hashlib
        idx = git_dir(root).parent / "index"
        cfg = Path(root) / "system-diff.toml"
        st = idx.stat() if idx.exists() else None
        # the config by its content: two edits within one tick of a coarse clock (Windows) have the same time
        said = hashlib.sha1(cfg.read_bytes()).hexdigest() if cfg.exists() else ""
        return ((st.st_mtime_ns, st.st_size) if st else (0, 0), said)
    except Exception:  # noqa: BLE001
        return (0, 0)


def _declared(cfg: Dict[str, Any]) -> Optional[List[Dict[str, str]]]:
    """What system-diff.toml says: None (find them), [] (off), or the projects it names."""
    got = cfg.get("projects")
    if got is None:
        return None
    if got is False:
        return []
    out = []
    for p in got if isinstance(got, list) else []:
        if isinstance(p, str) and p.strip("/"):
            out.append({"path": p.strip("/"), "name": p.strip("/").rsplit("/", 1)[-1]})
        elif isinstance(p, dict) and str(p.get("path") or "").strip("/"):
            path = str(p["path"]).strip("/")
            out.append({"path": path, "name": str(p.get("name") or path.rsplit("/", 1)[-1])})
    return out


def detect(root: Path, files: Optional[List[str]] = None, cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The folder projects of this repository: [{"path", "name", "why"}], two or more, else []. Cached while the index
    and the config are unchanged."""
    root = Path(root)
    key = str(root.resolve())
    stamp = _stamp(root)
    if files is None and cfg is None and key in _CACHE and _CACHE[key][0] == stamp:
        return _CACHE[key][1]
    if cfg is None:
        from .config import lenient
        cfg = lenient(root)
    said = _declared(cfg)
    if said is not None:
        out = [{**p, "why": "named in system-diff.toml"} for p in said if (root / p["path"]).is_dir()]
    else:
        out = _find(root, files if files is not None else _files(root), cfg, _from_extensions(root))
    out = out if len(out) >= 2 else []
    if files is None:
        _CACHE[key] = (stamp, out)
    return out


def _clean(path: Any) -> str:
    p = str(path or "").replace("\\", "/").strip().strip("/")
    while p.startswith("./"):
        p = p[2:]
    if not p or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
        return ""
    return p


def _from_extensions(root: Path) -> List[Dict[str, str]]:
    """The folders the extensions that apply here name as projects: [{"path", "name", "why", "by"}]. One extension's
    failure hides only its own."""
    try:
        from . import extensions
        exts = extensions.load()
    except Exception:  # noqa: BLE001
        return []
    if not exts:
        return []
    key = str(Path(root).resolve())
    asking = getattr(_ASKING, "roots", None)
    if asking is None:
        asking = _ASKING.roots = set()
    if key in asking:
        return []
    asking.add(key)
    out: List[Dict[str, str]] = []
    try:
        for ext in exts:
            fn = getattr(ext, "projects", None)
            if fn is None or inspect.ismodule(fn) or not callable(fn):
                continue
            name = str(getattr(ext, "NAME", "") or "extension")
            try:
                if not extensions.applies(ext, Path(root)):
                    continue
                got = fn(extensions.context(Path(root), "", ""))
            except Exception as exc:  # noqa: BLE001
                extensions.failed(name + " projects", exc)
                continue
            for item in got if isinstance(got, (list, tuple)) else []:
                if not isinstance(item, dict):
                    continue
                path = _clean(item.get("path"))
                if not path or not (Path(root) / path).is_dir():
                    continue
                title = str(item.get("title") or item.get("name") or path.rsplit("/", 1)[-1])[:80]
                out.append({"path": path, "name": title, "why": str(item.get("why") or f"named by {name}")[:200],
                            "by": name})
    finally:
        asking.discard(key)
    return out


def _find(root: Path, files: List[str], cfg: Dict[str, Any],
          named: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, Any]]:
    try:
        from .extract import Code
        skip = Code(cfg).skip
    except Exception:  # noqa: BLE001
        skip = None
    try:
        from . import repos
        gits = {k["path"] for k in repos.inside(root)}
    except Exception:  # noqa: BLE001
        gits = set()
    found: Dict[str, Dict[str, str]] = {}
    for f in files:
        parts = PurePosixPath(f).parts
        if len(parts) < 2 or len(parts) > DEPTH + 1 or parts[-1] not in MANIFESTS:
            continue
        folder = "/".join(parts[:-1])
        if any(p in SHARED or p.startswith(".") for p in parts[:-1]):
            continue
        if skip is not None and skip.search(folder + "/"):
            continue
        found.setdefault(folder, {"name": parts[-2], "why": f"its own {parts[-1]}"})
    for p in named or []:   # an extension's: it knows its own projects
        found[p["path"]] = {"name": p["name"], "why": p["why"]}
    keep = sorted(found)
    keep = [p for p in keep if not any(p != q and p.startswith(q + "/") for q in keep)]   # the outermost
    keep = [p for p in keep if p not in gits and not any(p.startswith(g + "/") for g in gits)]
    return [{"path": p, "name": found[p]["name"], "why": found[p]["why"]} for p in keep]


# ── the person's switch ──────────────────────────────────────────────────────────────────────────────────────────

def _state_file(root: Path) -> Path:
    from .sources import git_dir
    return git_dir(root) / STATE


def as_one(root: Path) -> bool:
    try:
        return bool(json.loads(_state_file(root).read_text(encoding="utf-8")).get("one"))
    except (OSError, ValueError, AttributeError):
        return False


def set_one(root: Path, one: bool) -> None:
    f = _state_file(root)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"one": bool(one)}), encoding="utf-8")
    os.replace(tmp, f)


def listed(root: Path) -> List[Dict[str, Any]]:
    """The folder projects the page lists: none while the person shows the repository as one project."""
    return [] if as_one(root) else detect(root)


def valid(root: Path, scope: str) -> bool:
    return bool(scope) and any(p["path"] == scope for p in listed(root))


# ── one project's view of the system ─────────────────────────────────────────────────────────────────────────────

def scoped(c: Any, root: Path, scope: str) -> Any:
    """A comparison narrowed to one project's folder: its parts, and the shared ones no project claims, with the
    connections, entry points and outside systems among them, and what changed there."""
    from .compare import Comparison
    from .diff import diff as typed_diff
    from .extract import Code
    code = Code(c.cfg)
    others = [p["path"] for p in listed(root) if p["path"] != scope]
    held = None
    try:
        from .compare import live
        held = live(root)
    except Exception:  # noqa: BLE001
        pass
    files = list(getattr(getattr(held, "source", None), "files", None) or _files(root))
    mine = set()
    for f in files:
        if not code.is_code(f):
            continue
        if f.startswith(scope + "/") or not any(f.startswith(o + "/") for o in others):
            mine.add(code.part_of(f))
    a, b = narrow(c.before, mine, scope), narrow(c.after, mine, scope)
    d = typed_diff(a, b, None, c.cfg.get("deploy", []), code)
    out = Comparison(d, a, b, c.cfg, c.base, c.head, getattr(c, "base_sha", ""), getattr(c, "head_sha", ""))
    out.root = getattr(c, "root", Path(root))
    out.regroup = None
    out.scope = scope
    return out


def narrow(bp: Dict[str, Any], parts: set, scope: str) -> Dict[str, Any]:
    if not bp:
        return bp
    out = dict(bp)
    comps = {k: v for k, v in (bp.get("components") or {}).items() if k in parts}
    out["components"] = comps
    keep = lambda key: all(x in comps or x in (bp.get("container_labels") or {}) for x in key.split(">"))  # noqa: E731
    for k in ("flows", "api", "internal"):
        out[k] = {key: v for key, v in (bp.get(k) or {}).items() if keep(key)}
    out["routes"] = {r: o for r, o in (bp.get("routes") or {}).items() if o in comps}
    ext = {}
    for x, e in (bp.get("externals") or {}).items():
        users = [u for u in e.get("used_by") or [] if u in comps]
        if users:
            ext[x] = {**e, "used_by": users}
    out["externals"] = ext
    for k in ("env", "stack", "imported", "uses"):
        if isinstance(bp.get(k), dict):
            out[k] = {c: v for c, v in bp[k].items() if c in comps}
    store = bp.get("store", "postgres")
    if store not in ext:
        out["tables"], out["migrations"] = {}, []
    out["repos"] = {p: r for p, r in (bp.get("repos") or {}).items() if p.startswith(scope + "/")}
    used = {v.get("container") for v in comps.values() if v.get("container")}
    out["containers"] = {n: v for n, v in (bp.get("containers") or {}).items() if n in used}
    return out
