"""What changed in the system between two points, where a point is a commit, the working tree, the CHECKPOINT (the
last state a person marked as seen: `system-diff checkpoint`, or "Mark as seen" on the live page), or the TURN (the
working tree as it was when the person last sent their AI coding agent a prompt).

The working tree is read as a SNAPSHOT: a commit made with a temporary index, so untracked files count (ignored ones do
not) and nothing the person uses changes: not their index, not HEAD, not a branch. The checkpoint and the turn are
such snapshots kept under refs/worktree/system-diff/: in this clone only, never pushed, and each worktree its own, so
agents working in parallel worktrees (and the people watching them) never move each other's.

Inside the resident engine's process (live.Engine, run by `system-diff watch`) the working tree is not snapshotted:
the engine keeps it in memory, told of each change (POST /touch, a cheap `git status`), and registers itself in LIVE;
a comparison with the working tree then takes the engine's blueprint and commit as they are. The result is the same
as a snapshot's for the same files.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config, gitfs
from .diff import diff, numstat
from . import extract as extraction
from .extract import EXTRACT_VERSION, Code, extract
from . import progress
from .sources import GitSource, _trim, git, git_dir, run_hidden

CHECKPOINT_REF = "refs/worktree/system-diff/checkpoint"   # before 0.7, one for every branch: still read, never written
BRANCH_CHECKPOINTS = "refs/worktree/system-diff/checkpoints/"   # + the branch: what the person has seen on that branch
OLD_CHECKPOINT_REF = "refs/system-diff/checkpoint"  # before 0.2, shared by every worktree: still read, never written
TURN_REF = "refs/worktree/system-diff/turn"
WORKTREE = (".", "worktree")
_IDENTITY = {"GIT_AUTHOR_NAME": "system-diff", "GIT_AUTHOR_EMAIL": "system-diff@localhost",
             "GIT_COMMITTER_NAME": "system-diff", "GIT_COMMITTER_EMAIL": "system-diff@localhost"}


def _git_env(root: Path, env: Dict[str, str], *args: str) -> str:
    return run_hidden(["git", *args], cwd=root, env=env, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()


def _head(root: Path) -> Optional[str]:
    try:
        return gitfs.head(root)   # read in Python: no git process
    except gitfs.Fallback:
        pass
    try:
        return git("rev-parse", "--verify", "-q", "HEAD", cwd=root).strip() or None
    except subprocess.CalledProcessError:
        return None  # a repository with no commit yet


def worktree_tree(root: Path) -> str:
    """The working tree's git tree id: cheap, and the same until a file changes."""
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        # Start from a copy of the repository's own index: it knows each file's size and time, so `add -A` hashes only
        # the files that changed. From an empty index it would hash every file every time (slow in a large repository,
        # most of all on Windows).
        try:
            real = Path(_git_env(root, dict(os.environ), "rev-parse", "--git-path", "index"))
            shutil.copyfile(real if real.is_absolute() else root / real, index)
        except (OSError, subprocess.CalledProcessError):
            if _head(root):
                _git_env(root, env, "read-tree", "HEAD")
        _git_env(root, env, "add", "-A")
        return _git_env(root, env, "write-tree")


def snapshot(root: Path) -> str:
    """The working tree as a commit (parent HEAD), made without touching the index, HEAD or any branch."""
    made = COMMIT.get(live_key(root))
    if made is not None:   # the resident engine is in this process: it writes the commit from memory
        try:
            return made()
        except Exception:  # noqa: BLE001 - snapshotted, then
            pass
    tree = worktree_tree(root)
    head = _head(root)
    env = {**os.environ, **_IDENTITY}
    return _git_env(root, env, "commit-tree", tree, *(["-p", head] if head else []), "-m", "system-diff snapshot")


def _branch(root: Path) -> Optional[str]:
    """The branch checked out here, or None (a detached HEAD)."""
    try:
        return gitfs.branch(root)
    except gitfs.Fallback:
        pass
    try:
        return git("symbolic-ref", "-q", "--short", "HEAD", cwd=root).strip() or None
    except subprocess.CalledProcessError:
        return None


def checkpoint_ref(root: Path) -> str:
    """Where this branch's checkpoint is kept: each branch has its own, as a branch can hold a different design."""
    branch = _branch(root)
    return BRANCH_CHECKPOINTS + branch if branch else CHECKPOINT_REF


def _store(root: Path, ref: str) -> str:
    """Point one of system-diff's refs at the working tree as it is now. The resident engine holds the working tree:
    in this process it writes the commit from memory; in another (a hook, the MCP server) it is asked to (POST /mark),
    one git call in all. Else a snapshot, as before."""
    if COMMIT.get(live_key(root)) is None:
        try:
            from . import live
            sha = live.client_mark(root, ref)
        except Exception:  # noqa: BLE001 - snapshotted here, then
            sha = None
        if sha:
            return sha
    sha = snapshot(root)
    git("update-ref", ref, sha, cwd=root)
    return sha


def checkpoint(root: Path, ref: Optional[str] = None) -> str:
    """Mark `ref` (default: the working tree as it is now) as seen, on the branch checked out. Returns the commit."""
    if not ref:
        return _store(root, checkpoint_ref(root))
    sha = git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=root).strip()
    git("update-ref", checkpoint_ref(root), sha, cwd=root)
    return sha


def _ref(root: Path, name: str) -> Optional[str]:
    try:
        return gitfs.rev(root, name)   # a ref, an id, HEAD~1…: read in Python
    except (gitfs.Fallback, OSError, ValueError):
        pass
    try:
        return git("rev-parse", "--verify", "-q", name, cwd=root).strip() or None
    except subprocess.CalledProcessError:
        return None


def checkpoint_point(root: Path) -> tuple:
    """(commit, label) the person last saw on this branch, or (None, "") when there is nothing to go by.

    The branch's own checkpoint first. A branch with none yet (new, or never looked at) starts from the newest
    checkpoint of another branch (or one from before 0.7) when that one's commit is in this branch's history: what
    was seen there still holds here. Otherwise it starts where this branch and that one parted, so what shows is what
    this branch changed, not every difference between the two branches."""
    own = _ref(root, checkpoint_ref(root))
    if own:
        return own, "checkpoint"
    head = _head(root)
    try:   # the other checkpoints, newest first, read in Python (git: for-each-ref --sort=-creatordate)
        found: Dict[str, str] = {}
        for prefix in (BRANCH_CHECKPOINTS, CHECKPOINT_REF, OLD_CHECKPOINT_REF):
            found.update(gitfs.refs(root, prefix))
        when = {name: (gitfs.commit(root, sha) or {}).get("time", 0) for name, sha in found.items()}
        listed = [f"{found[n]} {n}" for n in sorted(found, key=lambda n: (-when[n], n))]
    except (gitfs.Fallback, OSError, ValueError):
        try:
            listed = git("for-each-ref", "--sort=-creatordate", "--format=%(objectname) %(refname)",
                         BRANCH_CHECKPOINTS, CHECKPOINT_REF, OLD_CHECKPOINT_REF, cwd=root).split("\n")
        except subprocess.CalledProcessError:
            listed = []
    listed = [line for line in listed if line.strip()]
    if not listed or not head:
        return None, ""
    key = (str(root), head, tuple(listed))
    got = _POINTS.get(key)
    if got is None:   # the same refs and the same HEAD: the same answer (the merge-bases are git's, asked once)
        got = _POINTS[key] = _other_checkpoint(root, head, listed)
        _trim(_POINTS, 32)
    return got


_POINTS: Dict[tuple, tuple] = {}


def _other_checkpoint(root: Path, head: str, listed: List[str]) -> tuple:
    for line in listed:
        sha, name = line.split(" ", 1)
        where = name.removeprefix(BRANCH_CHECKPOINTS) if name.startswith(BRANCH_CHECKPOINTS) else "another branch"
        # a checkpoint is a snapshot whose parent is the commit it was taken on: that commit is what a branch shares
        base = _ref(root, f"{sha}^") or sha
        try:
            run_hidden(["git", "merge-base", "--is-ancestor", base, head], cwd=root, check=True, capture_output=True)
            return base, f"what you saw on {where}"
        except subprocess.CalledProcessError:
            pass
        try:
            fork = git("merge-base", head, base, cwd=root).strip()
        except subprocess.CalledProcessError:
            fork = ""
        if fork:
            return fork, f"where this branch left {where}"
    return None, ""


def checkpoint_sha(root: Path) -> Optional[str]:
    return checkpoint_point(root)[0]


def mark_turn(root: Path) -> str:
    """Keep the working tree as it is now as the start of a turn: the agent's next changes are measured from here.
    With the resident engine running, it makes the commit from memory (see _store)."""
    return _store(root, TURN_REF)


def turn_sha(root: Path) -> Optional[str]:
    return _ref(root, TURN_REF)


def resolve(root: Path, point: str) -> tuple:
    """(commit, label) for a point: `checkpoint` (else HEAD, when nothing was marked yet), `turn`, `.` for the working
    tree, or any commit."""
    if point == "checkpoint":
        sha, label = checkpoint_point(root)
        return (sha, label) if sha else (_head(root) or snapshot(root), "HEAD (no checkpoint yet)")
    if point == "turn":
        sha = turn_sha(root)
        if not sha:
            raise ValueError("no turn has started in this worktree yet: Claude Code marks one when you send a prompt "
                             "(`system-diff setup claude` adds the hook that does)")
        return sha, "the start of this turn"
    if point in WORKTREE:
        return snapshot(root), "working tree"
    try:
        sha = gitfs.rev(root, point)
        if sha:
            return sha, point
    except (gitfs.Fallback, OSError, ValueError):
        pass
    return git("rev-parse", "--verify", f"{point}^{{commit}}", cwd=root).strip(), point


@dataclass
class Live:
    """The working tree as the resident engine holds it: a commit of it (made from the engine's own index, parent
    HEAD, so git can diff and read it like a snapshot), its blueprint (partial, with `known` the paths read, while the
    engine's first read is still under way), the config it was read with, and a source of its files."""
    sha: str
    blueprint: Dict[str, Any]
    known: Optional[set]
    cfg: Dict[str, Any]
    source: Any
    version: int = 0
    identity: str = ""   # sha1 of the sorted (path, blob) pairs: the same files, the same identity


# {repository root (resolved, case-folded where the file system is): a callable giving its Live, or None}: set by the
# resident engine for the repositories it holds, in its own process only. COMMIT: the engine's way to make a commit of
# the working tree from memory (a checkpoint, the start of a turn).
LIVE: Dict[str, Any] = {}
COMMIT: Dict[str, Any] = {}


def live_key(root: Path) -> str:
    return os.path.normcase(str(Path(root).resolve()))


def live(root: Path) -> Optional[Live]:
    """The working tree of `root` as the engine in this process holds it, or None (no engine here, or it has nothing
    yet)."""
    if not LIVE:
        return None
    get = LIVE.get(live_key(root))
    try:
        return get() if get is not None else None
    except Exception:  # noqa: BLE001 - the engine failing must not stop a comparison: it is made the old way
        return None


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
# Only a whole blueprint is kept so: a partial one (see time_box) is kept for this process only, in _PARTIALS.
_BLUEPRINTS: Dict[Tuple[str, str], dict] = {}
_PARTIALS: Dict[tuple, Tuple[dict, set]] = {}
_KEEP = 8
_LOCK = threading.Lock()

# A time box (the pane's): a read of the code stops at this time.monotonic() deadline, and what was not read yet is
# left for the next read, which goes on from there (each file's facts are kept by content). None: read everything.
# The time box is each thread's own: one thread's time-boxed read (the terminal view's, the pane's) must not cut short
# another thread's full read in the same process (the engine's, a hook's).
_box = threading.local()


def _deadline_now() -> Optional[float]:
    return getattr(_box, "deadline", None)


def time_box(seconds: Optional[float]) -> None:
    """From now on, reads of the code in this process stop after `seconds` in all (None: no limit); see partial()."""
    _box.deadline = None if seconds is None else time.monotonic() + seconds
    _box.partial = None
    with _LOCK:
        _PARTIALS.clear()   # a new time box reads on from where the last stopped


def partial() -> Optional[Dict[str, int]]:
    """How far the last time-boxed read got, {"done": files read, "total": files}, when it did not get through all."""
    return getattr(_box, "partial", None)


def _blueprint(sha: str, cfg: Dict[str, Any], source, keep_in: Optional[Path] = None) -> dict:
    """The blueprint of a commit, read once per process; with `keep_in` (a repository) also kept on disk, for the
    hooks, each its own process, that compare the working tree with the same turn and checkpoint again and again."""
    return _read(sha, cfg, source, keep_in)[0]


def _read(sha: str, cfg: Dict[str, Any], source, keep_in: Optional[Path] = None,
          only: Optional[set] = None) -> Tuple[dict, Optional[set]]:
    """_blueprint, and None; or, when the time box ran out first, a partial blueprint and the paths it read (see
    extract.read_blueprint). With `only`, the blueprint of just those paths (partial, unless they are all)."""
    key = (sha, json.dumps(cfg, sort_keys=True, default=str) + EXTRACT_VERSION)  # a newer extractor reads it again
    bp = None
    if only is None:
        with _LOCK:
            bp = _BLUEPRINTS.get(key)
    path = None
    if keep_in is not None:
        path = git_dir(keep_in, "blueprints") / f"{sha}-{hashlib.sha1(key[1].encode()).hexdigest()[:10]}.json"
    if bp is None and path is not None and only is None:
        try:
            bp = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bp = None
    known = None
    if bp is None:
        pkey = (key, None if only is None else hashlib.sha1("\0".join(sorted(only)).encode()).hexdigest())
        with _LOCK:
            got = _PARTIALS.get(pkey)
        if got is not None:   # the same read again in this process: the time box has run out, nothing more to read
            return dict(got[0]), got[1]
        src = source() if callable(source) else source
        if _deadline_now() is None and only is None:
            bp, known = extract(src, cfg), None
        else:
            bp, known = extraction.read_blueprint(src, cfg, deadline=_deadline_now() if only is None else None, only=only)
        if known is not None:
            with _LOCK:
                _PARTIALS[pkey] = (bp, known)
                while len(_PARTIALS) > _KEEP:
                    del _PARTIALS[next(iter(_PARTIALS))]
            return dict(bp), known
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
    return dict(bp), None  # a copy: callers label it ("ref") for the person, and nothing else changes a blueprint


def _compare(root: Path, base: str, a_sha: str, a_label: str, head: str, b_sha: str, b_label: str,
             config_path: Optional[str], held: Optional[Live] = None) -> Comparison:
    if held is not None:   # the working tree as the resident engine holds it: nothing to snapshot or read again
        head_src, cfg = held.source, held.cfg
    else:
        head_src = GitSource(b_sha, root)
        cfg = config.load(head_src, root, config_path)
    keep_a = root if base in ("checkpoint", "turn") else None
    # the working tree's too: its snapshot's id changes with any file, so a kept one is never stale, and the pane,
    # asking every few seconds, does not read every file again each time
    keep_b = root if head in WORKTREE else None
    # The head first: under a time box (the pane's) it is what the person waits to see. When the time box leaves it
    # partial, the base is read for the same files only (every one of them, whatever the time: most are the same
    # content as the head's, read already, so only the changed ones are read again), and the other way round. Like
    # with like: a partial head against a whole base would say that everything not read yet was removed, and a
    # partial base against a whole head that it was all added. So the diff while the code is still being read is the
    # true diff of the files read so far, and grows to the whole diff as the reading goes on.
    progress.stage(f"Reading the code at {b_label}")
    if held is not None:
        b, b_known = dict(held.blueprint), (set(held.known) if held.known is not None else None)
        keep_b = None
    else:
        b, b_known = _read(b_sha, cfg, head_src, keep_b)
    progress.stage(f"Reading the code at {a_label}")
    a, a_known = _read(a_sha, cfg, lambda: GitSource(a_sha, root), keep_a, only=b_known)
    if a_known is not None and b_known is None:
        b, b_known = _read(b_sha, cfg, head_src, keep_b, only=a_known)
    if b.get("partial"):
        _box.partial = dict(b["partial"])
    progress.stage("Comparing")
    for side, point, label in ((a, base, a_label), (b, head, b_label)):
        if point in ("checkpoint", "turn") or point in WORKTREE:
            side["ref"] = label  # a snapshot's commit id means nothing to a person
    d = diff(a, b, numstat(root, a_sha, b_sha, cfg), cfg.get("deploy", []), Code(cfg))
    tip = _head(root) if head in WORKTREE else b_sha
    d["commits"] = ""
    if a_sha != b_sha and tip:   # no commit yet (a new repository): no commit messages either
        d["commits"] = commit_log(root, a_sha, tip)
    return Comparison(d, a, b, cfg, a_label, b_label, a_sha, b_sha)


_LOGS: Dict[Tuple[str, str, str], str] = {}


def commit_log(root: Path, a: str, b: str) -> str:
    """The messages of the commits in a..b (`git log`), asked once per range: commits never change."""
    if a == b:
        return ""
    key = (str(root), a, b)
    got = _LOGS.get(key)
    if got is None:
        try:
            got = git("log", "--format=%s%n%n%b", f"{a}..{b}", cwd=root)[:6000]
        except subprocess.CalledProcessError:
            got = ""
        if gitfs.HEX.match(a) and gitfs.HEX.match(b):
            _LOGS[key] = got
            _trim(_LOGS, 64)
    return got


def compare(root: Path, base: str = "checkpoint", head: str = ".", config_path: Optional[str] = None) -> Comparison:
    """The typed diff from `base` to `head`, both read with the head's config so a part keeps one name."""
    held = live(root) if config_path is None and (head in WORKTREE or base in WORKTREE) else None
    a_sha, a_label = (held.sha, "working tree") if held is not None and base in WORKTREE else resolve(root, base)
    if held is not None and head in WORKTREE:
        return _compare(root, base, a_sha, a_label, head, held.sha, "working tree", config_path, held)
    b_sha, b_label = resolve(root, head)
    return _compare(root, base, a_sha, a_label, head, b_sha, b_label, config_path)


def compare_worktree(root: Path, bases: Iterable[str], config_path: Optional[str] = None) -> List[Comparison]:
    """The working tree against each of `bases` (e.g. the checkpoint and the turn), snapshotted and read once."""
    held = live(root) if config_path is None else None
    if held is not None:
        return [_compare(root, base, *resolve(root, base), ".", held.sha, "working tree", config_path, held)
                for base in bases]
    progress.stage("Taking a snapshot of the working tree")
    b_sha, b_label = resolve(root, ".")
    return [_compare(root, base, *resolve(root, base), ".", b_sha, b_label, config_path) for base in bases]
