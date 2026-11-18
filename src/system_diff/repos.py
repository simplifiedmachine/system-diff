"""Repositories inside a repository: git submodules (listed in .gitmodules) and nested repositories (a folder with
its own .git that is not a submodule). Each is a system of its own: it gets its own engine (its own checkpoint, turn,
history and caches, exactly as if it had been opened alone), and the page lists the family on the left: the outer
repository first, then each one inside it, with its branch and whether its engine runs.

`family(root)` works from any member: from a submodule it finds the outer repository (`parent`) and lists the same
family, so the list is the same whichever one the page shows.
"""
from __future__ import annotations

import configparser
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

DEPTH = 4                      # how deep under the outer repository nested repositories are looked for
MOST = 60                      # at most this many: a folder of hundreds of clones is not one system
SKIP_DIRS = {"node_modules", ".venv", "venv", "env", ".env", "__pycache__", ".tox", ".nox", ".mypy_cache",
             ".pytest_cache", "site-packages", "dist", "build", "target", ".git", ".hg", ".svn", ".idea", ".vscode",
             ".next", ".cache", "vendor", "third_party"}
_CACHE: Dict[str, tuple] = {}  # outer root -> (stamp, repos)


def _is_repo(p: Path) -> bool:
    g = p / ".git"
    return g.is_dir() or g.is_file()


def parent(root: Path) -> Optional[Path]:
    """The repository this one sits inside (a submodule's or a nested repository's outer one), else None."""
    root = Path(root).resolve()
    for up in root.parents:
        if _is_repo(up):
            return up
    return None


def submodules(root: Path) -> List[str]:
    """The paths .gitmodules lists, relative to `root`."""
    f = Path(root) / ".gitmodules"
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        cp.read_string(text)
    except configparser.Error:
        return sorted(set(m.group(1).strip() for m in re.finditer(r"^\s*path\s*=\s*(.+)$", text, re.M)))
    out = []
    for sec in cp.sections():
        p = cp.get(sec, "path", fallback="").strip().strip("/")
        if p and ".." not in p.split("/"):
            out.append(p.replace("\\", "/"))
    return sorted(set(out))


def _skip_rx(root: Path) -> Optional[re.Pattern]:
    try:
        from .config import lenient
        from .extract import Code
        return Code(lenient(root)).skip
    except Exception:  # noqa: BLE001 - the plain skip list is enough
        return None


def nested(root: Path, known: List[str]) -> List[str]:
    """Folders under `root` (DEPTH deep at most) with a .git of their own, not submodules; not inside one found."""
    root = Path(root)
    skip = _skip_rx(root)
    found: List[str] = []
    taken = set(known)
    stack = [("", 0)]
    while stack and len(found) < MOST:
        rel, depth = stack.pop()
        try:
            items = sorted(os.scandir(root / rel if rel else root), key=lambda e: e.name, reverse=True)
        except OSError:
            continue
        for e in items:
            if e.name in SKIP_DIRS or e.name.startswith(".") and e.name not in (".github",):
                continue
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            sub = f"{rel}/{e.name}" if rel else e.name
            if skip is not None and skip.search(sub + "/"):
                continue
            if sub in taken:
                continue   # a submodule: listed already, and not looked inside
            if _is_repo(Path(e.path)):
                found.append(sub)
                continue   # its own repositories are its own business
            if depth + 1 < DEPTH:
                stack.append((sub, depth + 1))
    return sorted(found)


def _branch(repo: Path) -> str:
    try:
        from .hub import branch
        return branch(repo)
    except Exception:  # noqa: BLE001
        return ""


def _engine(repo: Path) -> Dict[str, Any]:
    """Where this repository's engine listens, if one runs: {"url", "port"} from its web.json, else {}. Its port is
    asked (GET /whoami gives web.json's id), not its pid alone: on Windows a pid cannot be checked, and a web.json left
    by an engine that was killed must not count as one (watch._look removes such a file)."""
    try:
        from . import watch
        from .sources import git_dir
        f = git_dir(repo) / "web.json"
        if not f.exists():
            return {}
        _url, state = watch._look(repo)
        if state not in ("up", "busy"):   # "busy": ours, reading a large repository; still the one to use
            return {}
        got = json.loads(f.read_text(encoding="utf-8"))
        return {"url": str(got.get("url") or ""), "port": int(got.get("port") or 0)}
    except Exception:  # noqa: BLE001
        return {}


def inside(root: Path) -> List[Dict[str, str]]:
    """The repositories inside `root`: [{"path", "kind"}], submodules first, kept while .gitmodules and the top
    folders are unchanged."""
    root = Path(root).resolve()
    try:
        stamp = (os.stat(root).st_mtime_ns,
                 (root / ".gitmodules").stat().st_mtime_ns if (root / ".gitmodules").exists() else 0)
    except OSError:
        stamp = (0, 0)
    got = _CACHE.get(str(root))
    if got is not None and got[0] == stamp:
        return got[1]
    subs = [p for p in submodules(root) if (root / p).is_dir()]
    out = [{"path": p, "kind": "submodule"} for p in subs] + \
          [{"path": p, "kind": "nested"} for p in nested(root, subs)]
    _CACHE[str(root)] = (stamp, out)
    return out


def family(root: Path) -> List[Dict[str, Any]]:
    """The list for the page's left column: the outer repository, then every repository inside it, each
    {"path" (absolute), "rel" ("" for the outer one), "name", "kind" ("outer", "submodule", "nested"), "branch",
    "current" (the one `root` is), "url", "port" (its engine, when it runs)}. Empty for a repository alone."""
    root = Path(root).resolve()
    outer = parent(root) or root
    kids = inside(outer)
    if not kids:
        return []
    out = []
    for rel, kind in [("", "outer")] + [(k["path"], k["kind"]) for k in kids]:
        p = (outer / rel).resolve() if rel else outer
        out.append({"path": str(p), "rel": rel, "name": rel.rsplit("/", 1)[-1] if rel else outer.name, "kind": kind,
                    "branch": _branch(p), "current": p == root, **_engine(p)})
    return out


def member(root: Path, rel: str) -> Optional[Path]:
    """The repository `rel` of root's family ("" the outer one), if it is one: never a path outside the family."""
    root = Path(root).resolve()
    outer = parent(root) or root
    if not rel:
        return outer
    if any(k["path"] == rel for k in inside(outer)):
        return (outer / rel).resolve()
    return None
