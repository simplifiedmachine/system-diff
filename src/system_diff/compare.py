"""What changed in the system between two points, where a point is a commit, the working tree, the CHECKPOINT (the
last state a person marked as seen: `system-diff checkpoint`, or "Mark as seen" on the live page), or the TURN (the
working tree as it was when the person last sent their AI coding agent a prompt).

The working tree is read as a SNAPSHOT: a commit made with a temporary index, so untracked files count (ignored ones do
not) and nothing the person uses changes: not their index, not HEAD, not a branch. The checkpoint and the turn are
such snapshots kept under refs/worktree/system-diff/: in this clone only, never pushed, and each worktree its own, so
agents working in parallel worktrees (and the people watching them) never move each other's.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config
from .diff import diff, numstat
from .extract import EXTRACT_VERSION, Code, extract
from .sources import GitSource, git, git_dir

CHECKPOINT_REF = "refs/worktree/system-diff/checkpoint"
OLD_CHECKPOINT_REF = "refs/system-diff/checkpoint"  # before 0.2, shared by every worktree: still read, never written
TURN_REF = "refs/worktree/system-diff/turn"
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


def _ref(root: Path, name: str) -> Optional[str]:
    try:
        return git("rev-parse", "--verify", "-q", name, cwd=root).strip() or None
    except subprocess.CalledProcessError:
        return None


def checkpoint_sha(root: Path) -> Optional[str]:
    return _ref(root, CHECKPOINT_REF) or _ref(root, OLD_CHECKPOINT_REF)


def mark_turn(root: Path) -> str:
    """Keep the working tree as it is now as the start of a turn: the agent's next changes are measured from here."""
    sha = snapshot(root)
    git("update-ref", TURN_REF, sha, cwd=root)
    return sha


def turn_sha(root: Path) -> Optional[str]:
    return _ref(root, TURN_REF)


def resolve(root: Path, point: str) -> tuple:
    """(commit, label) for a point: `checkpoint` (else HEAD, when nothing was marked yet), `turn`, `.` for the working
    tree, or any commit."""
    if point == "checkpoint":
        sha = checkpoint_sha(root)
        return (sha, "checkpoint") if sha else (_head(root) or snapshot(root), "HEAD (no checkpoint yet)")
    if point == "turn":
        sha = turn_sha(root)
        if not sha:
            raise ValueError("no turn has started in this worktree yet: Claude Code marks one when you send a prompt "
                             "(`system-diff setup claude` adds the hook that does)")
        return sha, "the start of this turn"
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
    base_sha: str = ""
    head_sha: str = ""


# A commit reads the same every time: the live page, the terminal view and the hook compare the working tree with the
# same checkpoint again and again, so its blueprint is read once. Keyed by commit, config and extractor; the last few.
_BLUEPRINTS: Dict[Tuple[str, str], dict] = {}
_KEEP = 8
_LOCK = threading.Lock()


def _blueprint(sha: str, cfg: Dict[str, Any], source, keep_in: Optional[Path] = None) -> dict:
    """The blueprint of a commit, read once per process; with `keep_in` (a repository) also kept on disk, for the
    hooks, each its own process, that compare the working tree with the same turn and checkpoint again and again."""
    key = (sha, json.dumps(cfg, sort_keys=True, default=str) + EXTRACT_VERSION)  # a newer extractor reads it again
    with _LOCK:
        bp = _BLUEPRINTS.get(key)
    path = None
    if bp is None and keep_in is not None:
        path = git_dir(keep_in, "blueprints") / f"{sha}-{hashlib.sha1(key[1].encode()).hexdigest()[:10]}.json"
        try:
            bp = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bp = None
    if bp is None:
        bp = extract(source() if callable(source) else source, cfg)
        if path is not None:
            try:
                path.write_text(json.dumps(bp), encoding="utf-8")
                for old in sorted(path.parent.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-_KEEP]:
                    old.unlink()
            except OSError:
                pass  # only a cache
    with _LOCK:
        _BLUEPRINTS[key] = bp
        while len(_BLUEPRINTS) > _KEEP:
            del _BLUEPRINTS[next(iter(_BLUEPRINTS))]
    return dict(bp)  # a copy: callers label it ("ref") for the person, and nothing else changes a blueprint


def _compare(root: Path, base: str, a_sha: str, a_label: str, head: str, b_sha: str, b_label: str,
             config_path: Optional[str]) -> Comparison:
    head_src = GitSource(b_sha, root)
    cfg = config.load(head_src, root, config_path)
    a = _blueprint(a_sha, cfg, lambda: GitSource(a_sha, root), root if base in ("checkpoint", "turn") else None)
    b = _blueprint(b_sha, cfg, head_src)
    for side, point, label in ((a, base, a_label), (b, head, b_label)):
        if point in ("checkpoint", "turn") or point in WORKTREE:
            side["ref"] = label  # a snapshot's commit id means nothing to a person
    d = diff(a, b, numstat(root, a_sha, b_sha, cfg), cfg.get("deploy", []), Code(cfg))
    tip = "HEAD" if head in WORKTREE else b_sha
    d["commits"] = git("log", "--format=%s%n%n%b", f"{a_sha}..{tip}", cwd=root)[:6000] if a_sha != b_sha else ""
    return Comparison(d, a, b, cfg, a_label, b_label, a_sha, b_sha)


def compare(root: Path, base: str = "checkpoint", head: str = ".", config_path: Optional[str] = None) -> Comparison:
    """The typed diff from `base` to `head`, both read with the head's config so a part keeps one name."""
    (a_sha, a_label), (b_sha, b_label) = resolve(root, base), resolve(root, head)
    return _compare(root, base, a_sha, a_label, head, b_sha, b_label, config_path)


def compare_worktree(root: Path, bases: Iterable[str], config_path: Optional[str] = None) -> List[Comparison]:
    """The working tree against each of `bases` (e.g. the checkpoint and the turn), snapshotted and read once."""
    b_sha, b_label = resolve(root, ".")
    return [_compare(root, base, *resolve(root, base), ".", b_sha, b_label, config_path) for base in bases]
