"""Where the files come from: a commit, read straight from git's object store (no checkout), or a directory."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple



# git processes started by this process (run_hidden and the cat-file process): the doctor and the engine's /progress
# say how many, as starting one can take seconds on some machines.
SPAWNS = {"git": 0}


def run_hidden(*args, **kw):
    """subprocess.run, with no console window on Windows. A process without a visible console (the web page's server,
    a background job, a pane run by Claude Code) would otherwise open a new window for each git it runs: a flood of
    windows taking the focus, and slow."""
    argv = args[0] if args else kw.get("args")
    if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
        SPAWNS["git"] += 1
    if os.name == "nt":
        kw["creationflags"] = kw.get("creationflags", 0) | 0x08000000   # CREATE_NO_WINDOW
    return subprocess.run(*args, **kw)


def git(*args: str, cwd: Path) -> str:
    return run_hidden(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def repo_root(start: Optional[Path] = None) -> Path:
    """The repository holding `start` (default: the current directory), or `start` itself outside one."""
    here = Path(start or Path.cwd()).resolve()
    try:
        return Path(git("rev-parse", "--show-toplevel", cwd=here).strip())
    except (OSError, subprocess.CalledProcessError):
        return here


_MADE: set = set()


def git_dir(root: Path, *parts: str) -> Path:
    """A folder under the repository's own git dir (a linked worktree's own, so each worktree keeps its own), made if
    missing: system-diff keeps what it writes there, where it is never committed. Found without running git
    (gitfs.locate), so asking at every look costs nothing."""
    from . import gitfs
    try:
        base = gitfs.git_dir(root)
    except gitfs.Fallback:
        base = Path(git("rev-parse", "--absolute-git-dir", cwd=root).strip())
    out = base.joinpath("system-diff", *parts)
    key = str(out)
    if key not in _MADE or not out.is_dir():
        out.mkdir(parents=True, exist_ok=True)
        _MADE.add(key)
    return out


# The working tree as the resident engine holds it has no commit: its comparisons name it by a made-up id ("live:" and
# a hash of its files), and GitSource(that id) is the engine's own source of it, from memory. The last few, by id.
LIVE_PREFIX = "live:"
LIVE_SOURCES: Dict[str, object] = {}

# What a commit holds never changes: its files and blob ids are listed once per process.
_LISTINGS: Dict[Tuple[str, str], Tuple[str, List[str], Dict[str, str]]] = {}


def _trim(cache: dict, most: int) -> None:
    """Drop the oldest entries of a process-wide cache past `most` (another thread may be at it too: never raises)."""
    try:
        while len(cache) > most:
            cache.pop(next(iter(cache)), None)
    except (RuntimeError, StopIteration):
        pass


def is_live(ref: Optional[str]) -> bool:
    return bool(ref) and str(ref).startswith(LIVE_PREFIX)


def _listing(ref: str, root: Path) -> Tuple[str, List[str], Dict[str, str]]:
    """(label, files, blobs) of a commit: read through gitfs (loose objects, the one cat-file process) when it can,
    else `git rev-parse --short` and `git ls-tree -r`."""
    from . import gitfs
    key = (str(root), ref)
    got = _LISTINGS.get(key)
    if got is not None:
        return got
    sha = None
    try:
        sha = gitfs.rev(root, ref)
        if sha is None:
            raise gitfs.Fallback(ref)
        files: List[str] = []
        blobs: Dict[str, str] = {}
        for mode, kind, oid, path in gitfs.ls_tree(root, sha):
            files.append(path)
            if kind == "blob":
                blobs[path] = oid
        label = sha[:7]
    except (gitfs.Fallback, OSError, ValueError):
        label = git("rev-parse", "--short", ref, cwd=root).strip()
        files, blobs = [], {}
        for entry in git("ls-tree", "-r", "-z", ref, cwd=root).split("\0"):
            head, tab, path = entry.partition("\t")
            if not tab:
                continue
            files.append(path)
            mode_type_id = head.split()
            if len(mode_type_id) == 3 and mode_type_id[1] == "blob":
                blobs[path] = mode_type_id[2]
    got = (label, files, blobs)
    if sha is not None and gitfs.HEX.match(ref):   # a name (HEAD, a branch) can move: only an id is kept
        _trim(_LISTINGS, 32)
        _LISTINGS[key] = got
    return got


class GitSource:
    """The files of a commit (or, for a "live:" id, the resident engine's working tree: see LIVE_SOURCES)."""

    def __new__(cls, ref: str, root: Path):
        if is_live(ref):
            held = LIVE_SOURCES.get(ref)
            if held is None:
                raise ValueError(f"{ref} is no longer held by the engine")
            return held
        return super().__new__(cls)

    def __init__(self, ref: str, root: Path):
        self.root, self.ref = Path(root), ref
        # each file's blob id too: a content address, so what was read from a file once (extract's per-file facts)
        # is known again wherever and whenever the same content turns up
        label, files, blobs = _listing(ref, self.root)
        self.label, self.files, self.blobs = label, list(files), dict(blobs)
        self._cache: Dict[str, str] = {}

    def read_many(self, paths: Iterable[str]) -> Dict[str, str]:
        paths = list(paths)
        want = [p for p in paths if p not in self._cache]
        if want:
            got = read_blobs(self.root, {p: self.blobs.get(p) or f"{self.ref}:{p}" for p in want})
            for p in want:
                data = got.get(p)
                self._cache[p] = data.decode("utf-8", errors="replace") if data is not None else ""
        return {p: self._cache.get(p, "") for p in paths}

    def read(self, path: str) -> str:
        return self.read_many([path])[path]


def read_blobs(root: Path, names: Dict[str, str]) -> Dict[str, Optional[bytes]]:
    """{key: content} of objects by name (a blob id, or "<commit>:<path>"): loose ones from disk, the rest through the
    process's one `git cat-file --batch` (gitfs.CatFile); one batch run of git if that cannot be used."""
    from . import gitfs
    out: Dict[str, Optional[bytes]] = {}
    try:
        for k, name in names.items():
            got = gitfs.read_object(root, name) if gitfs.HEX.match(name) else gitfs.catfile(root).get(name)
            out[k] = got[1] if got is not None and got[0] == "blob" else None
        return out
    except (gitfs.Fallback, OSError, ValueError):
        pass
    keys = list(names)
    raw = run_hidden(["git", "cat-file", "--batch"], cwd=root, check=True, capture_output=True,
                     input="".join(f"{names[k]}\n" for k in keys).encode("utf-8", "surrogateescape")).stdout
    i = 0
    for k in keys:
        end = raw.index(b"\n", i)
        parts = raw[i:end].split()
        i = end + 1
        if len(parts) < 3 or parts[1] == b"missing":
            out[k] = None
            continue
        size = int(parts[2])
        out[k] = raw[i:i + size]
        i += size + 1
    return out


class DirSource:
    """The files of a directory: the working tree, or a test fixture."""

    def __init__(self, directory: Path, skip: Optional[re.Pattern] = None, label: str = "working tree"):
        self.dir, self.label = Path(directory), label
        files = []
        for top, dirs, names in os.walk(self.dir):
            rel = os.path.relpath(top, self.dir).replace(os.sep, "/")
            dirs[:] = sorted(x for x in dirs if x != ".git" and not (skip and skip.search(f"{rel}/{x}/")))
            files += [(f"{rel}/{n}" if rel != "." else n) for n in names]
        self.files = sorted(files)

    def read_many(self, paths: Iterable[str]) -> Dict[str, str]:
        return {p: self.read(p) for p in paths}

    def read(self, path: str) -> str:
        try:
            return (self.dir / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
