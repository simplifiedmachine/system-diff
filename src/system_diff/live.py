"""The resident engine: one per repository, in the web server's process (`system-diff watch`), holding the working
tree in memory so that no view has to snapshot it again (docs/design.md, "One resident engine" and "Changes arrive as
events").

Git calls are what costs on a slow machine (starting any program can take seconds where endpoint security scans each
launch), so the engine makes a handful when it starts and none per look, per tick or per edit:

  at start   `git config -l -z` (core.autocrlf, core.filemode, core.symlinks)
             `git ls-files -z -c -o --exclude-standard --eol` (every file git would add, with its eol attributes)
             `git cat-file --batch`, one process kept open for the engine's life (gitfs.CatFile), for blobs in packs
             (+ `git check-attr` once, only when an attributes file names a filter (git-lfs), ident or an encoding)
  per tick   none: git's own files (HEAD, the index, refs) are read in Python (gitfs), files and folders are stat'ed
  per edit   none; a new file: one `git ls-files -o --exclude-standard --eol -- <paths>` (would git add it?)
  per look   none: refs, the checkpoint and the turn read in Python, comparisons served from memory, line counts
             counted in Python, commit messages asked once per range (compare.commit_log)
  a mark     one `git update-ref` (a checkpoint, the start of a turn): the commit itself is written in Python

What it holds: every file of the working tree as git would commit it (`git add -A`: tracked files, and untracked ones
that are not ignored), each with its git blob id, computed in Python (blob_id) after git's end-of-line conversion
(to_git: core.autocrlf, the text and eol attributes; files a filter applies to are hashed by git); and the blueprint
combined from every file's facts (kept by blob id, extract.FactStore). It makes no commit per change: comparisons
name the working tree "live:<identity>", and sources.GitSource of that id is the engine's own LiveSource (from
memory, git's store through the one cat-file, or the disk). compare.LIVE points at it, so compare(root, base, ".")
in this process uses it instead of snapshotting; the result is the same as a snapshot's for the same files. When a
commit must be kept in git (a checkpoint, the turn), materialize() writes its blobs, trees and commit as loose objects
in Python (compare.COMMIT, POST /mark).

How changes arrive:
- POST /touch {"paths": [...]} (the plugin's hooks, the moment Claude's tool returns): those files are looked at now.
- every `interval` seconds, in Python: each known file's size and time (one that moved is read again; its blob id
  recomputed; parsed again only if the id changed), each known folder's time (one that moved is listed: new files
  are asked about in one git call, deleted ones dropped). When the index moves (git add, a commit, a checkout) it is
  read again in Python; when an ignore or attributes rule changes, the start's listing runs again. On the start, a
  file whose size and time are those the index has (and not racily so) is taken as the index's blob without reading.
- the repository's other state the views show (the checkpoint and the turn, the activity log, the agents' bus, the
  LLM's regrouping, the person's learning profile) is watched by file time; a change bumps the version too.

A cold start reads the code in the background in slices (extract's reading order: config and entry points first,
every part in turn), publishing a partial blueprint ("partial": {"done", "total"}) after each, until all is read.
Nothing waits for it: until the first blueprint, pages are a placeholder saying where the engine is (progress(),
GET /progress), and each step of the start moves the version, so a long poll follows it.

Pages: `Pages` keeps the pages already drawn by (kind, view, width, zoom, part) with the version they were drawn
from; a page asked for again at the same version is answered from memory, and the pages asked for lately are drawn
again in the background as soon as the version moves, so a long poll (GET /page?since=V) answers at once. A page
being drawn when asked is waited for a moment (BUSY_WAIT), then the one drawn before is given.

For clients (the thin pane client imports this at every look, so only the standard library is imported here at
import time): client_page() asks the running engine for a page, client_progress() where it is, touch() tells it of
edited files, client_mark() asks it to mark the turn or a checkpoint, starting() says whether one is being started.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import stat as stat_mod
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

WEB = "web.json"              # where the engine (the web server) listens, and its token: under .git/system-diff
INTERVAL = 1.5                # seconds between two looks at `git status`
SLICE = 0.75                  # seconds one slice of the cold start reads for
LONG_POLL = 25.0              # seconds a long poll waits for a newer version
PAGE_TTL = 15.0               # seconds a drawn page is served for at one version ("2 min ago" ages)
WANTED_FOR = 120.0            # seconds a page asked for is kept drawn in the background as versions move
RACY = 2.0                    # a file changed this recently is hashed again next time, whatever its size and time
FIRST_WAIT = 20.0             # seconds a comparison waits for the engine's first blueprint before snapshotting
BUSY_WAIT = 2.0               # seconds a page ask waits for a drawing under way before taking the one drawn before


# ── for clients: stdlib only ──────────────────────────────────────────────────

def _git_dir(root: Path) -> Optional[Path]:
    """The repository's git dir, found without running git when it can be (gitfs.locate: a .git folder, or a .git
    file naming one, as a linked worktree has)."""
    from . import gitfs
    r = gitfs.locate(Path(root))
    return r.gitdir if r is not None else None


def engine_info(root: Path) -> Optional[dict]:
    """web.json of the repository's engine: {"url", "port", "pid", "id", "token"}, or None."""
    g = _git_dir(root)
    if g is None:
        return None
    try:
        info = json.loads((g / "system-diff" / WEB).read_text(encoding="utf-8"))
        return info if isinstance(info, dict) and int(info.get("port") or 0) > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def _connect(port: int, timeout: float) -> http.client.HTTPConnection:
    # connect quickly (Windows retries a refused connection for seconds), then wait as long as asked for the answer
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=min(1.0, timeout))
    conn.connect()
    conn.sock.settimeout(timeout)
    return conn


def _older(version: str) -> bool:
    """An engine older than this system-diff (its code loaded before an upgrade): not to be asked for pages."""
    from . import __version__

    def key(v: str) -> tuple:
        try:
            return tuple(int(x) for x in v.split(".")[:3])
        except ValueError:
            return ()
    return key(version) < key(__version__)


def client_page(root: Path, view: str = "now", width: int = 80, zoom: int = -1, part: Optional[str] = None,
                since: Optional[int] = None, take: bool = False, timeout: float = 30.0) -> Optional[dict]:
    """{"version", "page"} from the repository's running engine: `page` is what pane.show(root, width, view, part,
    zoom, take) gives. With `since` (a version had), the engine answers when it has a newer one, or after ~25 s with
    the current one. None when no engine answers (none running, not yet listening, an error)."""
    info = engine_info(root)
    if not info or not info.get("token") or _older(str(info.get("version") or "")):
        return None   # none, or one an upgrade left running: the caller starts this version's, which retires it
    q = {"view": view, "width": str(int(width)), "zoom": str(int(zoom)), "take": "1" if take else "0"}
    if part:
        q["part"] = part
    if since is not None:
        q["since"] = str(int(since))
    conn = None
    try:
        conn = _connect(int(info["port"]), timeout)
        conn.request("GET", "/page?" + urlencode(q), headers={"X-Token": str(info["token"]),
                                                              "Accept": "application/json"})
        r = conn.getresponse()
        body = r.read()
        if r.status != 200:
            return None
        data = json.loads(body.decode("utf-8"))
        return data if isinstance(data, dict) and "version" in data and "page" in data else None
    except Exception:  # noqa: BLE001 - no engine: the caller reads for itself
        return None
    finally:
        if conn is not None:
            conn.close()


def touch(root: Path, paths: Iterable[str]) -> None:
    """Tell the repository's engine that these files were just edited (POST /touch), so it reads them now instead of
    at its next look. Waits half a second at most; never raises."""
    try:
        paths = [str(p) for p in paths if p]
        info = engine_info(root)
        if not paths or not info or not info.get("token"):
            return
        conn = _connect(int(info["port"]), 0.5)
        try:
            conn.request("POST", "/touch", body=json.dumps({"paths": paths}).encode("utf-8"),
                         headers={"X-Token": str(info["token"]), "Content-Type": "application/json"})
            conn.getresponse().read()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a hint only: the engine's own look finds the edit a moment later
        pass


def _ask(root: Path, method: str, path: str, body: Optional[dict], timeout: float) -> Optional[dict]:
    """A JSON request to the repository's engine (with its token), or None when none answers."""
    info = engine_info(root)
    if not info or not info.get("token"):
        return None
    conn = None
    try:
        conn = _connect(int(info["port"]), timeout)
        headers = {"X-Token": str(info["token"]), "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        if r.status != 200:
            return None
        got = json.loads(raw.decode("utf-8"))
        return got if isinstance(got, dict) else None
    except Exception:  # noqa: BLE001 - no engine
        return None
    finally:
        if conn is not None:
            conn.close()


def client_progress(root: Path, timeout: float = 2.0) -> Optional[dict]:
    """Where the repository's engine is (GET /progress): {"stage", "done", "total", "version", "ready", "complete",
    "seconds"}; None when no engine answers. It answers at once, however busy the engine is."""
    got = _ask(root, "GET", "/progress", None, timeout)
    return got if got is not None and "stage" in got else None


def client_mark(root: Path, ref: str, timeout: float = 20.0) -> Optional[str]:
    """Ask the engine to point one of system-diff's refs (the turn, a checkpoint) at the working tree as it holds it
    (POST /mark): it writes the commit from memory, and one git call moves the ref. The commit, or None (no engine)."""
    got = _ask(root, "POST", "/mark", {"ref": ref}, timeout)
    sha = (got or {}).get("sha")
    return sha if isinstance(sha, str) and sha else None


def starting(root: Path, within: float = 60.0) -> bool:
    """Whether an engine for this repository is being started (watch.start_detached's web.starting, written less
    than `within` seconds ago): stdlib only, for the thin clients."""
    g = _git_dir(root)
    if g is None:
        return False
    try:
        info = json.loads((g / "system-diff" / "web.starting").read_text(encoding="utf-8"))
        return time.time() - float(info.get("at") or 0) < within
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def progress_text(got: Dict[str, Any]) -> str:
    """"Reading the code: 120 of 430 files - 12s" from an engine's progress."""
    text = str(got.get("stage") or "Starting")
    if got.get("total"):
        text += f": {int(got.get('done') or 0):,} of {int(got['total']):,} files"
    return text + f" - {int(float(got.get('seconds') or 0))}s"


def token_ok(given: Optional[str], token: str) -> bool:
    return bool(given) and bool(token) and hmac.compare_digest(str(given).encode(), token.encode())


# ── the engine ────────────────────────────────────────────────────────────────

def blob_id(data: bytes) -> str:
    """The git blob id of these bytes, as git computes it (before any conversion git would apply)."""
    h = hashlib.sha1(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def _byte_order(paths: Iterable[str]) -> List[str]:
    # git's own order for the paths of a tree, as `git ls-tree -r` lists them
    return sorted(paths, key=lambda p: p.encode("utf-8", "surrogateescape"))


# bytes git counts as not printable when it guesses whether a file is text (convert.c, gather_stats)
_CONTROL = bytes(c for c in range(32) if c not in (8, 9, 10, 13, 27, 12)) + b"\x7f"
_NOT_CONTROL = bytes(c for c in range(256) if bytes([c]) not in _CONTROL)


def _binary(data: bytes) -> bool:
    """git's guess that a file is not text (convert.c, convert_is_binary): a NUL, a lone CR, or too many control
    characters."""
    if b"\0" in data:
        return True
    cr = data.count(b"\r")
    if cr != data.count(b"\r\n"):
        return True
    nonprintable = len(data.translate(None, _NOT_CONTROL))
    if data.endswith(b"\x1a"):
        nonprintable -= 1
    printable = len(data) - nonprintable - cr - data.count(b"\n")
    return (printable >> 7) < nonprintable


def to_git(data: bytes, attr: str, autocrlf: str, index_has_cr: Callable[[], bool]) -> bytes:
    """The bytes `git add` would store for a file's content: CRLF made LF where git's end-of-line conversion applies
    (core.autocrlf, the text and eol attributes as `git ls-files --eol` shows them), else as they are."""
    if b"\r\n" not in data:
        return data
    words = attr.split()
    if "-text" in words:
        return data
    if any(w == "text=auto" for w in words):
        auto = True
    elif "text" in words or any(w.startswith("eol=") for w in words):
        auto = False
    elif autocrlf in ("true", "input"):
        auto = True
    else:
        return data
    if auto and (_binary(data) or index_has_cr()):
        return data   # a binary file, or one committed with CRs: git keeps it as it is ("safer autocrlf")
    return data.replace(b"\r\n", b"\n")


class Entry:
    """A file of the working tree: its mode and blob id as git would commit it, the (mtime_ns, size) it had when it
    was last looked at (None: look again next time), and the Python hash of its raw bytes then (None: taken from the
    index's stat data, not read)."""
    __slots__ = ("mode", "sha", "stamp", "raw")

    def __init__(self, mode: str, sha: str, stamp: Optional[tuple] = None, raw: Optional[str] = None):
        self.mode, self.sha, self.stamp, self.raw = mode, sha, stamp, raw

    def same(self, other: Optional["Entry"]) -> bool:
        return other is not None and other.mode == self.mode and other.sha == self.sha


class LiveSource:
    """The working tree's files as the engine holds them: files, blobs, root and label, like sources.GitSource. A
    file whose blob git has (the index's, or one the engine wrote) is read from git's object store (a loose object, or
    the one cat-file process); any other from the bytes the engine kept when it hashed it, or from the disk."""

    def __init__(self, engine: "Engine", files: List[str], blobs: Dict[str, str], ref: str):
        self.root, self.files, self.blobs, self.label, self.ref = engine.root, files, blobs, "working tree", ref
        self._engine = engine
        self._cache: Dict[str, str] = {}

    def read_many(self, paths: Iterable[str]) -> Dict[str, str]:
        paths = list(paths)
        for p in paths:
            if p in self._cache:
                continue
            sha = self.blobs.get(p)
            data = self._engine.content(p, sha) if sha else None
            self._cache[p] = data.decode("utf-8", errors="replace") if data is not None else ""
        return {p: self._cache.get(p, "") for p in paths}

    def read(self, path: str) -> str:
        return self.read_many([path])[path]


MEM_MAX = 64 * 1024 * 1024      # bytes of changed files kept in memory (the rest is read from disk again)
SCAN_SHARE = 0.1                # the stat scan takes at most this share of the time (a huge tree is scanned less often)


class Engine:
    """The working tree of one repository, kept current in memory (see the module's doc)."""

    def __init__(self, root: Path, interval: float = INTERVAL, slice_seconds: float = SLICE):
        from . import gitfs
        from .sources import git_dir
        self.root = Path(root).resolve()
        self.interval, self.slice = interval, slice_seconds
        self.lock = threading.RLock()            # the model: one change at a time
        self.cond = threading.Condition()        # the version, for long polls
        self.version = 0
        self.stopped = threading.Event()
        self.ready = threading.Event()           # the first blueprint is there (partial or whole)
        self.done = threading.Event()            # the cold start has read every file
        self.entries: Dict[str, Entry] = {}
        self.index: Dict[str, Any] = {}          # the real index, as last read: path -> gitfs.IndexEntry
        self.held: Optional[Any] = None          # compare.Live
        self.error = ""
        self.parsed = 0                          # files whose facts were read (not taken from the store): for tests
        self.reads = 0                           # blueprints made: a new one, a new picture
        self.head: Optional[str] = None
        self.git_calls: List[str] = []           # what this engine ran git for (the doctor and the tests count them)
        self.stage, self.stage_done, self.stage_total = "Starting", 0, 0
        self.started = time.time()
        self._mem: Dict[str, bytes] = {}         # blob id -> bytes, for blobs git does not have (changed files)
        self._mem_size = 0
        self._stored: set = set()                # blob ids git has: the index's, and those the engine wrote
        self._attrs: Dict[str, str] = {}         # path -> its eol attributes, as `git ls-files --eol` says them
        self._filtered: set = set()              # paths a filter (git-lfs…), ident or encoding applies to: git hashes
        self._filter_attrs = False               # an attributes file names a filter, ident or an encoding
        self._dirs: Dict[str, Optional[int]] = {}   # folder -> its mtime_ns: a new or deleted file changes it
        self._ignored: set = set()               # paths seen appear that git would not add (ignored)
        self._pending: set = set()               # paths told of (POST /touch): looked at in the next scan, read
        self._fp: Optional[tuple] = None
        self._index_stamp: Optional[tuple] = None
        self._made: Optional[Tuple[tuple, str]] = None   # ((identity, head), commit) of the last commit made
        self._scan_cost = 0.0
        self._last_scan = 0.0
        self._config: Dict[str, str] = {}
        self._repo = gitfs.locate(self.root)
        if self._repo is None:
            raise ValueError(f"{self.root} is not a git repository")
        self._sd = git_dir(self.root)
        self._gitdir, self._common = self._repo.gitdir, self._repo.common
        self._thread: Optional[threading.Thread] = None
        self._filemode = self._symlinks = True
        self._autocrlf = "false"
        self._live_ids: List[str] = []

    # git: the few calls the engine makes, each noted
    def _git(self, *args: str, input: Optional[bytes] = None, check: bool = True) -> bytes:
        from .sources import run_hidden
        self.git_calls.append(" ".join(args[:3]))
        r = run_hidden(["git", *args], cwd=self.root, capture_output=True, check=check, input=input)
        return r.stdout

    def _load_config(self) -> None:
        """`git config -l -z`, once: what decides how a file is added (core.filemode, core.symlinks, core.autocrlf)."""
        conf: Dict[str, str] = {}
        try:
            for rec in self._git("config", "-l", "-z").split(b"\0"):
                if rec:
                    k, _, v = rec.decode("utf-8", "replace").partition("\n")
                    conf[k.lower()] = v
        except Exception:  # noqa: BLE001 - git's defaults
            pass
        self._config = conf

        def flag(name: str, default: bool) -> bool:
            v = conf.get(name)
            return default if v is None else v.lower() in ("true", "yes", "on", "1", "")
        self._filemode = flag("core.filemode", True)
        self._symlinks = flag("core.symlinks", True)
        v = conf.get("core.autocrlf", "false").lower()
        self._autocrlf = "input" if v == "input" else "true" if v in ("true", "yes", "on", "1", "") else "false"

    # life
    def start(self) -> "Engine":
        from . import compare
        compare.LIVE[compare.live_key(self.root)] = self.live
        compare.COMMIT[compare.live_key(self.root)] = self.materialize
        self._thread = threading.Thread(target=self._run, name="system-diff engine", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        from . import compare
        self.stopped.set()
        key = compare.live_key(self.root)
        if compare.LIVE.get(key) == self.live:
            del compare.LIVE[key]
        if compare.COMMIT.get(key) == self.materialize:
            del compare.COMMIT[key]
        with self.cond:
            self.cond.notify_all()

    def live(self) -> Optional[Any]:
        """compare.Live of the working tree, for compare: waits for the first blueprint (a slice of the cold start)."""
        if not self.ready.is_set():
            self.ready.wait(FIRST_WAIT)
        return self.held

    @property
    def commit(self) -> str:
        """A commit of the working tree as it is now (made when asked: see materialize)."""
        return self.materialize()

    def _bump(self) -> int:
        with self.cond:
            self.version += 1
            self.cond.notify_all()
            return self.version

    def _say(self, stage: str, done: int = 0, total: int = 0, bump: bool = True) -> None:
        moved = stage != self.stage
        self.stage, self.stage_done, self.stage_total = stage, done, total
        if moved and bump and not self.ready.is_set():
            self._bump()   # the "reading" page says where the start is: a long poll answers with each step

    def progress(self) -> Dict[str, Any]:
        """Where the engine is: {"stage", "done", "total", "version", "ready", "complete", "seconds", "error",
        "engine_git_calls" (the last of the git calls the engine made itself), "git_spawns" (every git process its
        server's process started: the engine's, and the pages')}."""
        held = self.held
        part = (held.blueprint.get("partial") if held is not None else None) or {}
        done, total = self.stage_done, self.stage_total
        if part:
            done, total = int(part.get("done") or 0), int(part.get("total") or 0)
        from .sources import SPAWNS
        return {"stage": self.stage, "done": done, "total": total, "version": self.version,
                "ready": self.ready.is_set(), "complete": self.done.is_set(),
                "seconds": round(time.time() - self.started, 1), "error": self.error,
                "engine_git_calls": list(self.git_calls[-20:]), "git_spawns": SPAWNS["git"]}

    def poke(self) -> None:
        """Wake the long polls (something they also wait for, the outbox, has arrived)."""
        with self.cond:
            self.cond.notify_all()

    def wait(self, since: int, timeout: float = LONG_POLL, also: Optional[Callable[[], bool]] = None) -> int:
        """The version, once it is not `since` any more (or after `timeout`, or when `also()` says so)."""
        end = time.monotonic() + timeout
        with self.cond:
            while self.version == since and not self.stopped.is_set() and not (also and also()):
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(min(left, 0.5))
            return self.version

    def _run(self) -> None:
        try:
            with self.lock:
                self._say("Looking at the repository")
                self._load_config()
                self._fp = self._fingerprint()
                self.head = self._head()
                self._read_index()
                self._relist()
                self._say("Reading the code")
            last = time.monotonic()
            while not self.stopped.is_set() and not self.done.is_set():
                with self.lock:
                    self._read(deadline=time.monotonic() + self.slice)
                self._bump()
                if time.monotonic() - last >= self.interval:
                    self.tick()
                    last = time.monotonic()
            self._say("Ready", bump=False)
            self._warm_bases()
        except Exception as exc:  # noqa: BLE001 - the views fall back to reading for themselves
            self.error = f"{type(exc).__name__}: {exc}"
            self.ready.set()
        while not self.stopped.wait(self._pause()):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - next time
                self.error = f"{type(exc).__name__}: {exc}"

    def _pause(self) -> float:
        # a scan of a huge tree takes a while: then it runs less often, so it never takes more than its share
        return max(self.interval, self._scan_cost / SCAN_SHARE)

    def _warm_bases(self) -> None:
        """Read the checkpoint's and the turn's blueprints now, so the first page does not."""
        from . import compare
        held = self.held
        if held is None:
            return
        for sha in {compare.checkpoint_sha(self.root), compare.turn_sha(self.root), self.head} - {None}:
            try:
                from .sources import GitSource
                compare._blueprint(sha, held.cfg, lambda s=sha: GitSource(s, self.root), self.root)
            except Exception:  # noqa: BLE001 - only a head start
                pass

    # what else the views show, and git's own state: file times, read in Python
    def _fingerprint(self) -> tuple:
        def st(p: Path) -> Optional[tuple]:
            try:
                s = os.stat(p)
                return (s.st_mtime_ns, s.st_size)
            except OSError:
                return None

        def listing(p: Path) -> tuple:
            try:
                return tuple(sorted((e.name, e.stat().st_mtime_ns) for e in os.scandir(p)))
            except OSError:
                return ()
        g, c = self._gitdir, self._common
        try:
            head_ref = (g / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            head_ref = ""
        branch = c / head_ref[5:].strip() if head_ref.startswith("ref:") else None
        try:
            from . import profile
            prof = st(profile.path())
        except Exception:  # noqa: BLE001
            prof = None
        git_state = (head_ref, st(branch) if branch else None, st(c / "packed-refs"))
        # this branch's own checkpoint (one per branch), and the others' (a branch with none starts from them)
        mine = (g / "refs" / "worktree" / "system-diff" / "checkpoints" / head_ref[len("ref: refs/heads/"):].strip()
                if head_ref.startswith("ref: refs/heads/") else None)
        views = (st(mine) if mine else None, listing(g / "refs" / "worktree" / "system-diff" / "checkpoints"),
                 st(g / "refs" / "worktree" / "system-diff" / "checkpoint"),
                 st(g / "refs" / "worktree" / "system-diff" / "turn"), st(c / "refs" / "system-diff" / "checkpoint"),
                 st(self._sd / "activity.jsonl"), st(self._sd / "agents.json"), st(self._sd / "bus.jsonl"),
                 listing(self._sd / "regroup"), listing(self._sd / "parts"), prof)
        rules = (st(c / "info" / "exclude"), st(c / "info" / "attributes"), st(g / "index"))
        return git_state, views, rules

    def _head(self) -> Optional[str]:
        from . import gitfs
        try:
            return gitfs.head(self.root)
        except gitfs.Fallback:
            try:
                return self._git("rev-parse", "-q", "--verify", "HEAD^{commit}").decode().strip() or None
            except Exception:  # noqa: BLE001 - no commit yet
                return None

    # the index and the listing: the start's calls, again only when git's own state moves
    def _read_index(self) -> None:
        """The real index: read in Python (gitfs.read_index), else `git ls-files -s`. Its blobs are in git's store."""
        from . import gitfs
        f = self._gitdir / "index"
        try:
            st = os.stat(f)
            self._index_stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._index_stamp = None
            self.index = {}
            return
        try:
            self.index, _unmerged = gitfs.read_index(f)
        except (gitfs.Fallback, OSError, ValueError, struct.error):
            out: Dict[str, Any] = {}
            for rec in self._git("ls-files", "-s", "-z").split(b"\0"):
                if not rec:
                    continue
                head, _, path = rec.partition(b"\t")
                mode, sha, stage = head.decode().split()
                p = path.decode("utf-8", "surrogateescape")
                if stage == "0":
                    out[p] = gitfs.IndexEntry(mode, sha, 0, (-1, -1), -1, False)
                else:
                    out.pop(p, None)
            self.index = out
        self._stored.update(e.sha for e in self.index.values())

    def _relist(self) -> None:
        """Every file git would add (`git ls-files -c -o --exclude-standard --eol`: tracked, and untracked but not
        ignored, with their end-of-line attributes), each looked at on disk: the model from nothing, or again after
        the ignore rules or the attributes changed."""
        listed = self._ls(None)
        self._ignored.clear()
        self._read_filters(listed)
        new: Dict[str, Optional[Entry]] = {}
        old_attrs, self._attrs = self._attrs, {p: a for p, a in listed.items()}
        paths = set(listed)
        for p, ie in self.index.items():   # a sparse checkout's files not on disk still count, as git add -A keeps them
            if ie.skip:
                paths.add(p)
        self._say("Looking at the files", 0, len(paths))
        for i, p in enumerate(sorted(paths)):
            force = old_attrs.get(p) != self._attrs.get(p) and p in self.entries
            new[p] = self._from_disk(p, force=force)
            if i % 200 == 0:
                self._say("Looking at the files", i, len(paths), bump=False)
        for p in set(self.entries) - paths:
            new[p] = None
        self._hash_filtered(new)
        self._dirs = {}
        for p, e in new.items():
            if e is not None:
                self._watch_dir(p.rpartition("/")[0])
        self._watch_dir("")
        self._apply(new, read=False, force=not self.entries)

    def _ls(self, paths: Optional[List[str]]) -> Dict[str, str]:
        """{path: eol attributes} of the files git would add: all of them (None), or those of `paths` (files, and
        folders "dir/") that are untracked and not ignored. One git call."""
        args = ["--literal-pathspecs", "ls-files", "-z", "--exclude-standard", "--eol"]
        if paths is None:
            args += ["-c", "-o"]
            raw = self._git(*args)
        else:
            args += ["-o", "--"]
            raw = self._git(*args, *paths)
        out: Dict[str, str] = {}
        for rec in raw.split(b"\0"):
            if not rec:
                continue
            info, tab, path = rec.partition(b"\t")
            if not tab:
                continue
            p = path.decode("utf-8", "surrogateescape")
            if p.endswith("/"):
                continue   # a repository inside this one: git add would record it as such, not its files
            text = info.decode("utf-8", "replace")
            at = text.find("attr/")
            out[p] = text[at + 5:].strip() if at >= 0 else ""
        return out

    def _read_filters(self, listed: Dict[str, str]) -> None:
        """Whether any attributes file names a filter (git-lfs), ident or a working-tree encoding; if so, which files
        they apply to (`git check-attr`, once): those are hashed by git, not here."""
        files = [p for p in listed if p == ".gitattributes" or p.endswith("/.gitattributes")]
        texts = []
        for f in [self.root / p for p in files] + [self._common / "info" / "attributes"]:
            try:
                texts.append(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        glob = self._config.get("core.attributesfile")
        for f in ([Path(os.path.expanduser(glob))] if glob else
                  [Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git" / "attributes"]):
            try:
                texts.append(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        self._filter_attrs = any(re.search(r"\b(filter|ident|working-tree-encoding)\b", t) for t in texts)
        self._filtered = self._check_filters(list(listed)) if self._filter_attrs else set()

    def _check_filters(self, paths: List[str]) -> set:
        if not paths:
            return set()
        raw = self._git("check-attr", "-z", "--stdin", "filter", "ident", "working-tree-encoding",
                        input="".join(p + "\0" for p in paths).encode("utf-8", "surrogateescape"))
        out, parts = set(), raw.split(b"\0")
        for i in range(0, len(parts) - 2, 3):
            if parts[i + 2] not in (b"unspecified", b"unset"):
                out.add(parts[i].decode("utf-8", "surrogateescape"))
        return out

    def _watch_dir(self, d: str) -> None:
        while True:
            if d in self._dirs:
                return
            try:
                self._dirs[d] = os.stat(self.root / d if d else self.root).st_mtime_ns
            except OSError:
                self._dirs[d] = None
            if not d:
                return
            d = d.rpartition("/")[0]

    def tick(self) -> bool:
        """One look, in Python: git's own files (HEAD, the index, refs, the ignore rules) by time, each known file and
        folder by stat; a file whose size or time moved is read again, a folder whose time moved is listed for new
        files (git asked only whether those would be added). True when the version moved."""
        with self.lock:
            began = time.monotonic()
            fp = self._fingerprint()
            moved = fp != self._fp
            old, self._fp = self._fp, fp
            relist = False
            if old is None or fp[0] != old[0]:
                self.head = self._head()
            if old is None or fp[2] != old[2]:
                relist = self._index_moved() or (old is not None and fp[2][:2] != old[2][:2])
            changed = self._scan(relist)
            self._scan_cost = time.monotonic() - began
            self._last_scan = time.monotonic()
        if moved and not changed:
            self._bump()
        return changed or moved

    def _index_moved(self) -> bool:
        """The index changed (git add, a commit, a checkout): read it again (Python). True when what is tracked
        changed so that git must say again what it would add (a file now tracked that was not known, or one no longer
        tracked that is still here: it may be ignored)."""
        try:
            st = os.stat(self._gitdir / "index")
            stamp: Optional[tuple] = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp == self._index_stamp:
            return False
        before = set(self.index)
        self._read_index()
        now = set(self.index)
        return bool((now - before) - set(self.entries)) or bool((before - now) & set(self.entries))

    def sync(self) -> int:
        """Look now (instead of at the next tick); the version after it."""
        self.tick()
        return self.version

    def _scan(self, relist: bool = False) -> bool:
        if relist:
            before = {p: e.sha for p, e in self.entries.items()}
            self._relist()
            return self._model_moved(before)
        new: Dict[str, Optional[Entry]] = {}
        rules = False
        for p, e in self.entries.items():
            if e.mode == "160000":
                continue
            force = p in self._pending
            try:
                s = os.lstat(self.root / p)
                stamp = (s.st_mtime_ns, s.st_size)
            except OSError:
                ie = self.index.get(p)
                if ie is not None and ie.skip:
                    continue
                new[p] = None
                rules = rules or _is_rules(p)
                continue
            if force or e.stamp != stamp:
                got = self._from_disk(p, force=force)
                if got is None or not got.same(e) or got.stamp != e.stamp or got.raw != e.raw:
                    new[p] = got
                    rules = rules or (_is_rules(p) and (got is None or got.sha != e.sha))
        # new files: a folder whose time moved is listed again
        candidates: List[str] = []
        for d, mt in list(self._dirs.items()):
            try:
                now = os.stat(self.root / d if d else self.root).st_mtime_ns
            except OSError:
                del self._dirs[d]
                continue
            if now == mt:
                continue
            self._dirs[d] = now
            try:
                listing = list(os.scandir(self.root / d if d else self.root))
            except OSError:
                continue
            for item in listing:
                rel = f"{d}/{item.name}" if d else item.name
                if item.name == ".git" or rel in self.entries or rel in self._ignored or rel in self._dirs:
                    continue
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                candidates.append(rel + "/" if is_dir else rel)
        candidates += [p for p in self._pending if p not in self.entries and p not in candidates]
        self._pending.clear()
        for c in [c for c in candidates if c in self.index]:   # tracked, back on disk: no need to ask git
            candidates.remove(c)
            new[c] = self._from_disk(c)
            if new[c] is not None:
                self._watch_dir(c.rpartition("/")[0])
        if candidates:
            listed = self._ls(candidates)
            for c in candidates:
                if c.endswith("/"):
                    if any(p.startswith(c) for p in listed):
                        continue
                    if _has_files(self.root / c):
                        self._ignored.add(c[:-1])   # files, none of them to add: an ignored folder
                    else:
                        self._watch_dir(c[:-1])     # nothing in it yet: watched, for what comes
                elif c not in listed:
                    self._ignored.add(c)
            if listed and self._filter_attrs:
                self._filtered |= self._check_filters(list(listed))
            for p, attr in listed.items():
                self._attrs[p] = attr
                new[p] = self._from_disk(p)
                if new[p] is not None:
                    self._watch_dir(p.rpartition("/")[0])
                rules = rules or _is_rules(p)
        if not new:
            return False
        self._hash_filtered(new)
        before = {p: e.sha for p, e in self.entries.items()}
        if rules:   # an ignore or attributes file changed: what git would add may be another set of files
            self._apply(new, read=False)
            self._relist()
            return self._model_moved(before)
        return self._apply(new)

    def _model_moved(self, before: Dict[str, str]) -> bool:
        if {p: e.sha for p, e in self.entries.items()} == before and self.held is not None:
            return False
        self._read(deadline=None if self.done.is_set() else time.monotonic())
        self._bump()
        return True

    def _from_disk(self, p: str, force: bool = False) -> Optional[Entry]:
        """The entry of this file as it is on disk now: taken from the index when its stat data says it is the
        index's blob, else read and hashed in Python (with git's end-of-line conversion applied)."""
        full = self.root / p
        try:
            s = os.lstat(full)
        except OSError:
            ie = self.index.get(p)
            return Entry(ie.mode, ie.sha) if ie is not None and ie.skip else None
        old = self.entries.get(p)
        ie = self.index.get(p)
        if stat_mod.S_ISLNK(s.st_mode) and self._symlinks:
            try:
                data = os.fsencode(os.readlink(full))
            except OSError:
                return None
            sha = blob_id(data)
            self._keep(sha, data)
            return Entry("120000", sha, (s.st_mtime_ns, s.st_size), sha)
        if stat_mod.S_ISDIR(s.st_mode):
            if ie is not None and ie.mode == "160000":
                return Entry("160000", ie.sha)   # a submodule: as the index has it
            return old   # a folder named by itself (a nested repository): as it was
        if not stat_mod.S_ISREG(s.st_mode):
            return None
        stamp = (s.st_mtime_ns, s.st_size)
        if ie is not None and ie.mode == "120000" and not self._symlinks:
            mode = "120000"   # a link checked out as a file: git keeps it a link
        elif self._filemode:
            mode = "100755" if s.st_mode & 0o111 else "100644"
        else:   # the file system cannot tell: the mode the index has (as git add keeps it)
            mode = (ie.mode if ie is not None else old.mode if old is not None else "100644")
            mode = mode if mode in ("100644", "100755") else "100644"
        if not force and old is not None and old.stamp == stamp and old.mode == mode:
            return old
        keep = stamp if time.time() - s.st_mtime > RACY else None   # changed just now: perhaps again, look again
        if not force and ie is not None and ie.size >= 0 and self._clean(ie, s):
            return Entry(mode, ie.sha, keep, None)
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return None
        raw = blob_id(data)
        if old is not None and old.raw == raw and old.mode == mode and p not in self._filtered:
            return Entry(mode, old.sha, keep, raw)
        if p in self._filtered:
            return Entry(mode, "", keep, raw)   # git hashes it (see _hash_filtered)
        conv = to_git(data, self._attrs.get(p, ""), self._autocrlf, lambda: self._index_has_cr(p))
        sha = raw if conv is data else blob_id(conv)
        if sha not in self._stored:
            self._keep(sha, conv)
        return Entry(mode, sha, keep, raw)

    def _clean(self, ie: Any, s: os.stat_result) -> bool:
        """Whether the index's stat data says the file is its blob (as git status decides, by size and time), and
        the index was written after the file's time (else it may be "racily clean": read it)."""
        if (s.st_size & 0xFFFFFFFF) != ie.size:
            return False
        sec, nsec = divmod(s.st_mtime_ns, 1_000_000_000)
        if (sec & 0xFFFFFFFF) != ie.mtime[0] or (ie.mtime[1] and ie.mtime[1] != nsec):
            return False
        # written before the index was: else it may have changed within the index's own tick ("racily clean")
        return self._index_stamp is not None and s.st_mtime_ns < self._index_stamp[0]

    def _index_has_cr(self, p: str) -> bool:
        from . import gitfs
        ie = self.index.get(p)
        if ie is None:
            return False
        try:
            got = gitfs.read_object(self.root, ie.sha)
        except (gitfs.Fallback, OSError):
            return False
        return got is not None and b"\r" in got[1]

    def _hash_filtered(self, new: Dict[str, Optional[Entry]]) -> None:
        """The files a filter applies to (git-lfs…): git hashes them, all in one call, and writes their blobs."""
        want = [p for p, e in new.items() if e is not None and not e.sha]
        if not want:
            return
        ids = self._git("hash-object", "-w", "--stdin-paths",
                        input="".join(p + "\n" for p in want).encode("utf-8", "surrogateescape")).decode().split()
        for p, sha in zip(want, ids):
            new[p].sha = sha  # type: ignore[union-attr]
            self._stored.add(sha)
        for p in want[len(ids):]:
            new[p] = None

    def _keep(self, sha: str, data: bytes) -> None:
        if sha in self._mem or len(data) > MEM_MAX // 16:
            return
        self._mem[sha] = data
        self._mem_size += len(data)
        if self._mem_size > MEM_MAX:
            live = {e.sha for e in self.entries.values()}
            for k in [k for k in self._mem if k not in live]:
                self._mem_size -= len(self._mem.pop(k))

    def content(self, p: str, sha: str) -> Optional[bytes]:
        """The bytes of blob `sha` of path `p`: kept in memory, in git's store, or read from the disk again (and
        given only if it still is that blob)."""
        from . import gitfs
        data = self._mem.get(sha)
        if data is not None:
            return data
        if sha in self._stored or gitfs.has_loose(self.root, sha):
            try:
                got = gitfs.read_object(self.root, sha)
                if got is not None:
                    return got[1]
            except (gitfs.Fallback, OSError, ValueError):
                pass
        try:
            raw = (self.root / p).read_bytes()
        except OSError:
            return None
        conv = to_git(raw, self._attrs.get(p, ""), self._autocrlf, lambda: self._index_has_cr(p))
        if blob_id(conv) == sha:
            return conv
        with self.lock:
            self._pending.add(p)   # it moved since it was hashed: the next scan reads it again
        return None

    def _apply(self, new: Dict[str, Optional[Entry]], read: bool = True, force: bool = False) -> bool:
        changed = {p: e for p, e in new.items()
                   if not ((e is None and p not in self.entries) or (e is not None and e.same(self.entries.get(p))))}
        for p, e in new.items():   # what was learnt of unchanged files (their time, their raw hash) is kept too
            if e is not None and p not in changed:
                self.entries[p] = e
        if not changed and not force:
            return False
        for p, e in changed.items():
            if e is None:
                self.entries.pop(p, None)
            else:
                self.entries[p] = e
        if read:
            self._read(deadline=None if self.done.is_set() else time.monotonic())
            self._bump()
        return True

    def identity(self) -> str:
        h = hashlib.sha1()
        for p in _byte_order(self.entries):
            h.update(f"{p}\0{self.entries[p].sha}\n".encode("utf-8", "surrogateescape"))
        return h.hexdigest()

    def blobs(self) -> Dict[str, str]:
        with self.lock:
            return {p: e.sha for p, e in self.entries.items() if e.mode != "160000"}

    def _read(self, deadline: Optional[float]) -> None:
        """The blueprint of the files as they are now (with a deadline: of those read by then, partial)."""
        from . import compare, config, extract
        from .sources import LIVE_PREFIX, LIVE_SOURCES
        files = _byte_order(self.entries)
        blobs = {p: e.sha for p, e in self.entries.items() if e.mode != "160000"}
        ident = self.identity()
        ref = LIVE_PREFIX + ident
        src = LiveSource(self, files, blobs, ref)
        LIVE_SOURCES[ref] = src
        if ref not in self._live_ids:
            self._live_ids.append(ref)
        while len(self._live_ids) > 8:
            LIVE_SOURCES.pop(self._live_ids.pop(0), None)
        try:
            cfg = config.load(src, self.root)
        except ValueError as exc:   # the config does not parse: compare says so, the old way
            self.error = f"config: {exc}"
            self.held = None
            self.ready.set()
            return
        counted = _Counted(src)
        bp, known = extract.read_blueprint(counted, cfg, deadline=deadline)
        self.parsed += counted.read
        self.reads += 1
        self.error = ""
        self.held = compare.Live(sha=ref, blueprint=bp, known=known, cfg=cfg, source=src,
                                 version=self.version + 1, identity=ident)
        self.ready.set()
        if known is None:
            self.done.set()

    # commits: only when something must be kept in git (a checkpoint, the start of a turn)
    def materialize(self) -> str:
        """A commit of the working tree as it is now (parent HEAD), the same as a snapshot's (`git add -A`, write-tree,
        commit-tree): its blobs, trees and commit written as loose objects in Python, so no git runs. Made once per
        state of the files."""
        from . import compare, gitfs
        with self.lock:
            self.tick()
            key = (self.identity(), self.head)
            if self._made is not None and self._made[0] == key:
                return self._made[1]
            try:
                files: Dict[str, Tuple[str, str]] = {}
                for p, e in list(self.entries.items()):
                    if e.mode != "160000" and e.sha not in self._stored and not gitfs.has_loose(self.root, e.sha):
                        data = self._mem.get(e.sha) or self.content(p, e.sha)
                        if data is None:   # it moved under us: as it is now
                            got = self._from_disk(p, force=True)
                            if got is None:
                                self.entries.pop(p, None)
                                continue
                            self.entries[p] = e = got
                            data = self._mem.get(e.sha) or self.content(p, e.sha)
                            if data is None:
                                raise gitfs.Fallback(f"{p} keeps changing")
                        gitfs.write_object(self.root, "blob", data)
                        self._stored.add(e.sha)
                    files[p] = (e.mode, e.sha)
                tree = gitfs.write_tree(self.root, files)
                commit = gitfs.write_commit(self.root, tree, [self.head] if self.head else [], "system-diff live")
            except (gitfs.Fallback, OSError):
                self.git_calls.append("snapshot")
                commit = compare.snapshot(self.root)
            self._made = (key, commit)
            return commit

    def mark(self, ref: str) -> str:
        """Point `ref` (one of system-diff's own, under refs/worktree/system-diff/) at the working tree as it is now:
        one git call (update-ref). The commit."""
        if not ref.startswith("refs/worktree/system-diff/") or ".." in ref:
            raise ValueError(f"not a system-diff ref: {ref}")
        sha = self.materialize()
        self._git("update-ref", ref, sha)
        self.tick()
        return sha

    # Claude's edits
    def touch(self, paths: Iterable[str]) -> int:
        """These files were just edited: look at them now. The version after it."""
        rel = []
        for p in paths:
            try:
                q = Path(p)
                q = q if q.is_absolute() else self.root / q
                r = os.path.relpath(os.path.abspath(q), self.root).replace(os.sep, "/")
            except (ValueError, OSError):
                continue   # another drive (Windows)
            if r == "." or r.startswith("../") or r == ".." or r == ".git" or r.startswith(".git/"):
                continue
            rel.append(r)
        if not rel:
            return self.version
        with self.lock:
            if self._fp is None:   # the first look has not run yet: it will see these
                return self.version
            for r in rel:
                if r in self._ignored and r not in self.index:
                    continue
                self._ignored.discard(r)
                self._pending.add(r)
            self._scan()
            return self.version


def _has_files(folder: Path) -> bool:
    for _top, _dirs, names in os.walk(folder):
        if names:
            return True
    return False


def _is_rules(p: str) -> bool:
    name = p.rpartition("/")[2]
    return name in (".gitignore", ".gitattributes")


class _Counted:
    """A source that counts the files read through it (extract reads only those whose facts it has not kept)."""

    def __init__(self, src: LiveSource):
        self._src, self.read = src, 0
        self.root, self.files, self.blobs, self.label = src.root, src.files, src.blobs, src.label

    def read_many(self, paths: Iterable[str]) -> Dict[str, str]:
        paths = list(paths)
        self.read += len(paths)
        return self._src.read_many(paths)


# ── pages ─────────────────────────────────────────────────────────────────────

class Pages:
    """The pages already drawn, by key, with the version each was drawn at; the ones asked for lately are drawn again
    in the background when the version moves. `build(key)` draws one (the caller serialises what must be)."""

    def __init__(self, engine: Engine, build: Callable[[tuple], dict], ttl: float = PAGE_TTL,
                 placeholder: Optional[Callable[[tuple], dict]] = None):
        self.engine, self._build, self.ttl, self.placeholder = engine, build, ttl, placeholder
        self.cache: Dict[tuple, Tuple[int, float, dict]] = {}
        self.wanted: Dict[tuple, float] = {}
        self.locks: Dict[tuple, threading.Lock] = {}
        self.guard = threading.Lock()
        self._painter: Optional[threading.Thread] = None

    def build(self, key: tuple) -> dict:
        """Draw a page: before the engine has its first blueprint, the placeholder (where the start is), never a
        comparison that would wait for it."""
        if self.placeholder is not None and not self.engine.ready.is_set():
            return self.placeholder(key)
        return self._build(key)

    def get(self, key: tuple) -> Tuple[int, dict]:
        """(version, page) for `key`: from memory when drawn at the current version, else drawn now. Never long: while
        the engine starts, the placeholder; while this page is being drawn (by the painter), the one drawn before, if
        the drawing does not end within a moment."""
        with self.guard:
            self.wanted[key] = time.monotonic()
            lock = self.locks.setdefault(key, threading.Lock())
        hit = self._hit(key)
        if hit is not None:
            return hit
        if not lock.acquire(timeout=BUSY_WAIT):
            with self.guard:
                got = self.cache.get(key)
            if got is not None:
                return got[0], got[2]
            lock.acquire()   # nothing drawn yet: wait for the drawing under way
        try:   # one drawing of a page at a time: a second asker waits for it
            hit = self._hit(key)
            if hit is not None:
                return hit
            version = self.engine.version
            page = self.build(key)
            with self.guard:
                self.cache[key] = (version, time.monotonic(), page)
                while len(self.cache) > 64:
                    del self.cache[next(iter(self.cache))]
            return version, page
        finally:
            lock.release()

    def current(self, key: tuple) -> bool:
        """Whether this page is drawn at the current version (a long poll for it then waits for the next)."""
        with self.guard:
            got = self.cache.get(key)
        return got is not None and got[0] == self.engine.version

    def _hit(self, key: tuple) -> Optional[Tuple[int, dict]]:
        with self.guard:
            got = self.cache.get(key)
        if got is not None and got[0] == self.engine.version and time.monotonic() - got[1] < self.ttl:
            return got[0], got[2]
        return None

    def start(self) -> "Pages":
        self._painter = threading.Thread(target=self._paint, name="system-diff pages", daemon=True)
        self._painter.start()
        return self

    def _paint(self) -> None:
        seen = self.engine.version
        while not self.engine.stopped.is_set():
            seen = self.engine.wait(seen, timeout=5.0)
            if self.engine.stopped.is_set():
                return
            now = time.monotonic()
            with self.guard:
                keys = [k for k, at in self.wanted.items() if now - at < WANTED_FOR]
                for k in [k for k, at in self.wanted.items() if now - at >= WANTED_FOR]:
                    del self.wanted[k]
            for k in keys:
                if self.engine.version != seen:
                    break   # newer again: start over from there
                try:
                    with self.guard:
                        lock = self.locks.setdefault(k, threading.Lock())
                    with lock:
                        if self._hit(k) is None:
                            version = self.engine.version
                            page = self.build(k)
                            with self.guard:
                                self.cache[k] = (version, time.monotonic(), page)
                except Exception:  # noqa: BLE001 - drawn when asked for, then, and the error told there
                    pass
