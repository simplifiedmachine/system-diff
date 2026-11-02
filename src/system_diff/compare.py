"""What changed in the system between two points, where a point is a commit, the working tree, or the CHECKPOINT: the
last state a person marked as seen (`system-diff checkpoint`, or "Mark as seen" on the live page).

The working tree is read as a SNAPSHOT: a commit made with a temporary index, so untracked files count (ignored ones do
not) and nothing the person uses changes: not their index, not HEAD, not a branch. A checkpoint is such a snapshot kept
under refs/system-diff/checkpoint, so it lives in this clone only and is never pushed.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from . import config
from .diff import diff, numstat
from .extract import Code, extract
from .sources import GitSource, git

CHECKPOINT_REF = "refs/system-diff/checkpoint"
WORKTREE = (".", "worktree")
_IDENTITY = {"GIT_AUTHOR_NAME": "system-diff", "GIT_AUTHOR_EMAIL": "system-diff@localhost",
             "GIT_COMMITTER_NAME": "system-diff", "GIT_COMMITTER_EMAIL": "system-diff@localhost"}


def _git_env(root: Path, env: Dict[str, str], *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()


def _head(root: Path) -> Optional[str]:
    try:
        return git("rev-parse", "--verify", "-q", "HEAD", cwd=root).strip() or None
    except subprocess.CalledProcessError:
        return None  # a repository with no commit yet


def worktree_tree(root: Path) -> str:
    """The working tree's git tree id: cheap, and the same until a file changes."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if _head(root):
            _git_env(root, env, "read-tree", "HEAD")
        _git_env(root, env, "add", "-A")
        return _git_env(root, env, "write-tree")


def snapshot(root: Path) -> str:
    """The working tree as a commit (parent HEAD), made without touching the index, HEAD or any branch."""
    tree = worktree_tree(root)
    head = _head(root)
    env = {**os.environ, **_IDENTITY}
    return _git_env(root, env, "commit-tree", tree, *(["-p", head] if head else []), "-m", "system-diff snapshot")


def checkpoint(root: Path, ref: Optional[str] = None) -> str:
    """Mark `ref` (default: the working tree as it is now) as seen. Returns the commit it points at."""
    sha = git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=root).strip() if ref else snapshot(root)
    git("update-ref", CHECKPOINT_REF, sha, cwd=root)
    return sha


def checkpoint_sha(root: Path) -> Optional[str]:
    try:
        return git("rev-parse", "--verify", "-q", CHECKPOINT_REF, cwd=root).strip() or None
    except subprocess.CalledProcessError:
        return None


def resolve(root: Path, point: str) -> tuple:
    """(commit, label) for a point: `checkpoint` (else HEAD, when nothing was marked yet), `.` for the working tree,
    or any commit."""
    if point == "checkpoint":
        sha = checkpoint_sha(root)
        return (sha, "checkpoint") if sha else (_head(root) or snapshot(root), "HEAD (no checkpoint yet)")
    if point in WORKTREE:
        return snapshot(root), "working tree"
    return git("rev-parse", "--verify", f"{point}^{{commit}}", cwd=root).strip(), point


@dataclass
class Comparison:
    diff: Dict[str, Any]
    before: Dict[str, Any]
    after: Dict[str, Any]
    cfg: Dict[str, Any]
    base: str
    head: str


def compare(root: Path, base: str = "checkpoint", head: str = ".", config_path: Optional[str] = None) -> Comparison:
    """The typed diff from `base` to `head`, both read with the head's config so a part keeps one name."""
    (a_sha, a_label), (b_sha, b_label) = resolve(root, base), resolve(root, head)
    head_src = GitSource(b_sha, root)
    cfg = config.load(head_src, root, config_path)
    a, b = extract(GitSource(a_sha, root), cfg), extract(head_src, cfg)
    for side, point, label in ((a, base, a_label), (b, head, b_label)):
        if point == "checkpoint" or point in WORKTREE:
            side["ref"] = label  # a snapshot's commit id means nothing to a person
    d = diff(a, b, numstat(root, a_sha, b_sha, cfg), cfg.get("deploy", []), Code(cfg))
    tip = "HEAD" if head in WORKTREE else b_sha
    d["commits"] = git("log", "--format=%s%n%n%b", f"{a_sha}..{tip}", cwd=root)[:6000] if a_sha != b_sha else ""
    return Comparison(d, a, b, cfg, a_label, b_label)
