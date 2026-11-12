"""Where the files come from: a commit, read straight from git's object store (no checkout), or a directory."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional



def run_hidden(*args, **kw):
    """subprocess.run, with no console window on Windows. A process without a visible console (the web page's server,
    a background job, a pane run by Claude Code) would otherwise open a new window for each git it runs: a flood of
    windows taking the focus, and slow."""
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


def git_dir(root: Path, *parts: str) -> Path:
    """A folder under the repository's own git dir (a linked worktree's own, so each worktree keeps its own), made if
    missing: system-diff keeps what it writes there, where it is never committed."""
    out = Path(git("rev-parse", "--absolute-git-dir", cwd=root).strip()).joinpath("system-diff", *parts)
    out.mkdir(parents=True, exist_ok=True)
    return out


class GitSource:
    """The files of a commit."""

    def __init__(self, ref: str, root: Path):
        self.root, self.ref = Path(root), ref
        self.label = git("rev-parse", "--short", ref, cwd=self.root).strip()
        # each file's blob id too: a content address, so what was read from a file once (extract's per-file facts)
        # is known again wherever and whenever the same content turns up
        self.files: List[str] = []
        self.blobs: Dict[str, str] = {}
        for entry in git("ls-tree", "-r", "-z", ref, cwd=self.root).split("\0"):
            head, tab, path = entry.partition("\t")
            if not tab:
                continue
            self.files.append(path)
            mode_type_id = head.split()
            if len(mode_type_id) == 3 and mode_type_id[1] == "blob":
                self.blobs[path] = mode_type_id[2]
        self._cache: Dict[str, str] = {}

    def read_many(self, paths: Iterable[str]) -> Dict[str, str]:
        paths = list(paths)
        want = [p for p in paths if p not in self._cache]
        if want:
            out = run_hidden(["git", "cat-file", "--batch"], cwd=self.root, check=True, capture_output=True,
                                 input="".join(f"{self.ref}:{p}\n" for p in want).encode()).stdout
            i = 0
            for p in want:
                header_end = out.index(b"\n", i)
                parts = out[i:header_end].split()
                i = header_end + 1
                if len(parts) < 3 or parts[1] == b"missing":
                    self._cache[p] = ""
                    continue
                size = int(parts[2])
                self._cache[p] = out[i:i + size].decode("utf-8", errors="replace")
                i += size + 1
        return {p: self._cache.get(p, "") for p in paths}

    def read(self, path: str) -> str:
        return self.read_many([path])[path]


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
