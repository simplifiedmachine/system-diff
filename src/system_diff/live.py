"""The resident engine: one per repository, in the web server's process (`system-diff watch`), holding the working
tree in memory so that no view has to snapshot it again (docs/design.md, "One resident engine" and "Changes arrive as
events").

What it holds: every file of the working tree as git would commit it (`git add -A`: tracked files, and untracked ones
that are not ignored), each with its git blob id; a commit of that tree, made from the engine's own index under the
git dir (live.index), so git can diff and read it as it does a snapshot; and the blueprint combined from every file's
facts (kept by blob id, extract.FactStore). compare.LIVE points at it, so compare(root, base, ".") in this process uses
it instead of snapshotting; the result is the same as a snapshot's for the same files.

How changes arrive:
- POST /touch {"paths": [...]} (the plugin's hooks, the moment Claude's tool returns): those files are read from disk
  and hashed in Python (sha1 of b"blob <len>\\0" + bytes, as git hashes a blob). The Python hash only decides whether
  a file changed since it was last seen (or differs from the blob git's index has for it); the files that did are
  then given to one `git hash-object -w --stdin-paths`, which writes their blobs (the commit needs them, and so do
  git's line counts) and applies whatever git add would (core.autocrlf, .gitattributes eol and filters), so the ids
  are git's own. Only the changed files are parsed again; the blueprint is recombined and the version goes up.
- every `interval` seconds: `git --no-optional-locks status --porcelain=v1 -z --untracked-files=all`, which uses
  the index's stat cache (cheap) and never writes the index, names what differs from the index: an edit in the
  person's editor, a file made or deleted. When the index or HEAD moves (a commit, a checkout, a branch switch), the
  index is listed again (`git ls-files -s`) and the whole tree is reconciled. Same path as /touch from there.
- the repository's other state the views show (the checkpoint and the turn, the activity log, the agents' bus, the
  LLM's regrouping, the person's learning profile) is watched by file time; a change bumps the version too.

A cold start reads the code in the background in slices (extract's reading order: config and entry points first,
every part in turn), publishing a partial blueprint ("partial": {"done", "total"}) after each, until all is read.
Nothing waits for it.

Pages: `Pages` keeps the pages already drawn by (kind, view, width, zoom, part) with the version they were drawn
from; a page asked for again at the same version is answered from memory, and the pages asked for lately are drawn
again in the background as soon as the version moves, so a long poll (GET /page?since=V) answers at once.

For clients (the thin pane client imports this at every look, so only the standard library is imported here at
import time): client_page() asks the running engine for a page, touch() tells it of edited files.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import stat as stat_mod
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


# ── for clients: stdlib only ──────────────────────────────────────────────────

def _git_dir(root: Path) -> Optional[Path]:
    """The repository's git dir, found without running git when it can be (a .git folder, or a .git file naming one,
    as a linked worktree has)."""
    root = Path(root)
    g = root / ".git"
    try:
        if g.is_dir():
            return g
        if g.is_file():
            text = g.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                p = Path(text[7:].strip())
                return p if p.is_absolute() else (root / p).resolve()
    except OSError:
        pass
    try:
        from .sources import run_hidden
        out = run_hidden(["git", "rev-parse", "--absolute-git-dir"], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=5)
        return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except Exception:  # noqa: BLE001 - no git, not a repository
        return None


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


class Entry:
    """A file of the working tree: its mode and blob id as git would commit it, the (mtime_ns, size) it had when it
    was last hashed (None: hash it again next time), and the Python hash of its raw bytes then."""
    __slots__ = ("mode", "sha", "stamp", "raw")

    def __init__(self, mode: str, sha: str, stamp: Optional[tuple] = None, raw: Optional[str] = None):
        self.mode, self.sha, self.stamp, self.raw = mode, sha, stamp, raw

    def same(self, other: Optional["Entry"]) -> bool:
        return other is not None and other.mode == self.mode and other.sha == self.sha


class LiveSource:
    """The working tree's files as the engine holds them: files, blobs and root, like sources.GitSource, read from
    git's object store by blob id (or from the bytes in hand when they are exactly the blob)."""

    def __init__(self, root: Path, files: List[str], blobs: Dict[str, str], mem: Dict[str, bytes]):
        self.root, self.files, self.blobs, self.label = Path(root), files, blobs, "working tree"
        self._mem = mem
        self._cache: Dict[str, str] = {}

    def read_many(self, paths: Iterable[str]) -> Dict[str, str]:
        from .sources import run_hidden
        paths = list(paths)
        want = []
        for p in paths:
            if p in self._cache:
                continue
            sha = self.blobs.get(p)
            if not sha:
                self._cache[p] = ""
            elif sha in self._mem:
                self._cache[p] = self._mem[sha].decode("utf-8", errors="replace")
            else:
                want.append(p)
        if want:
            out = run_hidden(["git", "cat-file", "--batch"], cwd=self.root, check=True, capture_output=True,
                             input="".join(f"{self.blobs[p]}\n" for p in want).encode()).stdout
            i = 0
            for p in want:
                end = out.index(b"\n", i)
                head = out[i:end].split()
                i = end + 1
                if len(head) < 3 or head[1] == b"missing":
                    self._cache[p] = ""
                    continue
                size = int(head[2])
                self._cache[p] = out[i:i + size].decode("utf-8", errors="replace")
                i += size + 1
        return {p: self._cache.get(p, "") for p in paths}

    def read(self, path: str) -> str:
        return self.read_many([path])[path]


class Engine:
    """The working tree of one repository, kept current in memory (see the module's doc)."""

    def __init__(self, root: Path, interval: float = INTERVAL, slice_seconds: float = SLICE):
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
        self.index: Dict[str, Tuple[str, str]] = {}   # the real index, as last listed: path -> (mode, sha)
        self.reported: set = set()               # what `git status` named last time
        self.held: Optional[Any] = None          # compare.Live
        self.error = ""
        self.parsed = 0                          # files whose facts were read (not taken from the store): for tests
        self.reads = 0                           # blueprints made: a new one, a new picture
        self.tree = ""
        self.commit = ""
        self.head: Optional[str] = None
        self._mem: Dict[str, bytes] = {}         # blob id -> bytes in hand, for files hashed since the last read
        self._fp: Optional[tuple] = None
        self._sd = git_dir(self.root)
        self._index_file = self._sd / "live.index"
        self._thread: Optional[threading.Thread] = None
        self._filemode = self._symlinks = True
        self._gitdir = Path(self._git_text("rev-parse", "--absolute-git-dir").strip())
        common = self._git_text("rev-parse", "--git-common-dir").strip()
        self._common = Path(common) if Path(common).is_absolute() else (self.root / common).resolve()

    # git
    def _git(self, *args: str, input: Optional[bytes] = None, index: bool = False, env: Optional[dict] = None) -> bytes:
        from .sources import run_hidden
        e = dict(os.environ)
        if index:
            e["GIT_INDEX_FILE"] = str(self._index_file)
        if env:
            e.update(env)
        return run_hidden(["git", *args], cwd=self.root, capture_output=True, check=True, input=input, env=e).stdout

    def _git_text(self, *args: str) -> str:
        return self._git(*args).decode("utf-8", errors="replace")

    def _config(self, name: str, default: bool) -> bool:
        try:
            v = self._git_text("config", "--type=bool", "--get", name).strip()
        except Exception:  # noqa: BLE001 - not set
            return default
        return v == "true" if v else default

    # life
    def start(self) -> "Engine":
        from . import compare
        compare.LIVE[compare.live_key(self.root)] = self.live
        self._thread = threading.Thread(target=self._run, name="system-diff engine", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        from . import compare
        self.stopped.set()
        key = compare.live_key(self.root)
        if compare.LIVE.get(key) == self.live:
            del compare.LIVE[key]
        with self.cond:
            self.cond.notify_all()

    def live(self) -> Optional[Any]:
        """compare.Live of the working tree, for compare: waits for the first blueprint (a slice of the cold start)."""
        if not self.ready.is_set():
            self.ready.wait(FIRST_WAIT)
        return self.held

    def _bump(self) -> int:
        with self.cond:
            self.version += 1
            self.cond.notify_all()
            return self.version

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
            self._filemode = self._config("core.filemode", True)
            self._symlinks = self._config("core.symlinks", True)
            with self.lock:
                self._fp = self._fingerprint()
                self.head = self._head()
                self._reconcile(full=True, read=False)
            last = time.monotonic()
            while not self.stopped.is_set() and not self.done.is_set():
                with self.lock:
                    self._read(deadline=time.monotonic() + self.slice)
                self._bump()
                if time.monotonic() - last >= self.interval:
                    self.tick()
                    last = time.monotonic()
            self._warm_bases()
        except Exception as exc:  # noqa: BLE001 - the views fall back to reading for themselves
            self.error = f"{type(exc).__name__}: {exc}"
            self.ready.set()
        while not self.stopped.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - next time
                self.error = f"{type(exc).__name__}: {exc}"

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

    # what else the views show: file times
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
        git_state = (head_ref, st(branch) if branch else None, st(c / "packed-refs"), st(g / "index"))
        # this branch's own checkpoint (one per branch), and the others' (a branch with none starts from them)
        mine = (g / "refs" / "worktree" / "system-diff" / "checkpoints" / head_ref[len("ref: refs/heads/"):].strip()
                if head_ref.startswith("ref: refs/heads/") else None)
        views = (st(mine) if mine else None, listing(g / "refs" / "worktree" / "system-diff" / "checkpoints"),
                 st(g / "refs" / "worktree" / "system-diff" / "checkpoint"),
                 st(g / "refs" / "worktree" / "system-diff" / "turn"), st(c / "refs" / "system-diff" / "checkpoint"),
                 st(self._sd / "activity.jsonl"), st(self._sd / "agents.json"), st(self._sd / "bus.jsonl"),
                 listing(self._sd / "regroup"), listing(self._sd / "parts"), prof)
        return git_state, views

    def _head(self) -> Optional[str]:
        try:
            return self._git_text("rev-parse", "-q", "--verify", "HEAD^{commit}").strip() or None
        except Exception:  # noqa: BLE001 - no commit yet
            return None

    def tick(self) -> bool:
        """One look: what `git status` names (all of the index again when it or HEAD moved), and the other state
        the views show. True when the version moved."""
        with self.lock:
            fp = self._fingerprint()
            moved_git = self._fp is None or fp[0] != self._fp[0]
            moved = fp != self._fp
            self._fp = fp
            if moved_git:
                head = self._head()
                head_moved, self.head = head != self.head, head
            else:
                head_moved = False
            changed = self._reconcile(full=moved_git, head_moved=head_moved)
        if moved and not changed:
            self._bump()
        return changed or moved

    def sync(self) -> int:
        """Look now (instead of at the next tick); the version after it."""
        self.tick()
        return self.version

    # the files
    def _ls_index(self) -> Dict[str, Tuple[str, str]]:
        out: Dict[str, Tuple[str, str]] = {}
        for rec in self._git("ls-files", "-s", "-z").split(b"\0"):
            if not rec:
                continue
            head, _, path = rec.partition(b"\t")
            mode, sha, stage = head.decode().split()
            p = path.decode("utf-8", "surrogateescape")
            if stage == "0":
                out[p] = (mode, sha)
            else:
                out.pop(p, None)   # unmerged: what is on disk counts (it is in `git status` too)
        return out

    def _status(self) -> Tuple[set, set]:
        """(paths whose content on disk is to be read: changed, new or unmerged; paths deleted from the disk)."""
        raw = self._git("--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all",
                        "--no-renames", "--ignore-submodules=all")
        dirty, gone = set(), set()
        for rec in raw.split(b"\0"):
            if len(rec) < 4:
                continue
            xy, p = rec[:2].decode(), rec[3:].decode("utf-8", "surrogateescape")
            if p.endswith("/"):
                continue   # a repository inside this one: git add would record it as such, not its files
            if xy[1] == "D" or xy == "DD":
                gone.add(p)
            elif xy == "D ":
                continue   # deleted from the index: if it is still on disk, it is listed again as untracked
            elif xy == "??" or xy[1] in "MTA" or "U" in xy or xy == "AA":
                dirty.add(p)
        return dirty, gone

    def _reconcile(self, full: bool, head_moved: bool = False, read: bool = True) -> bool:
        """Bring the files up to date with the disk; True (and a new blueprint, unless `read` is False) when any
        changed. `full`: list the index again and look at every file it has."""
        if full:
            self.index = self._ls_index()
        dirty, gone = self._status()
        new: Dict[str, Optional[Entry]] = {}
        if full:
            for p, (mode, sha) in self.index.items():
                if p not in dirty and p not in gone:
                    old = self.entries.get(p)
                    new[p] = old if old is not None and old.mode == mode and old.sha == sha else Entry(mode, sha)
            for p in set(self.entries) - set(self.index) - dirty:
                new[p] = None
        else:
            for p in self.reported - dirty - gone:   # back to what the index has (or gone, if it has nothing)
                got = self.index.get(p)
                new[p] = Entry(*got) if got else None
        for p in gone:
            new[p] = None
        new.update(self._from_disk(dirty))
        self.reported = dirty | gone
        return self._apply(new, read=read, force=full and not self.entries) or (head_moved and self._recommit(read))

    def _from_disk(self, paths: Iterable[str]) -> Dict[str, Optional[Entry]]:
        """The entries of these files as they are on disk now: hashed in Python, and given to git (which writes the
        blob and applies its conversions) only when they changed."""
        out: Dict[str, Optional[Entry]] = {}
        regular: List[Tuple[str, str, Optional[tuple], str, bytes]] = []
        now = time.time()
        for p in paths:
            full = self.root / p
            try:
                s = os.lstat(full)
            except OSError:
                out[p] = None
                continue
            old = self.entries.get(p)
            if stat_mod.S_ISLNK(s.st_mode) and self._symlinks:
                try:
                    data = os.fsencode(os.readlink(full))
                except OSError:
                    out[p] = None
                    continue
                sha = blob_id(data)
                if not (old and old.sha == sha):
                    self._git("hash-object", "-w", "--stdin", "--no-filters", input=data)
                out[p] = Entry("120000", sha)
                continue
            if not stat_mod.S_ISREG(s.st_mode):
                if stat_mod.S_ISDIR(s.st_mode):
                    out[p] = old   # a folder named by itself (a nested repository): as it was
                else:
                    out[p] = None
                continue
            stamp = (s.st_mtime_ns, s.st_size)
            if self._filemode:
                mode = "100755" if s.st_mode & 0o111 else "100644"
            else:   # the file system cannot tell: the mode the index has (as git add keeps it)
                mode = (self.index.get(p) or (old.mode if old else "100644", ""))[0]
                mode = mode if mode in ("100644", "100755") else "100644"
            if old is not None and old.stamp == stamp and old.mode == mode:
                out[p] = old
                continue
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except OSError:
                out[p] = None
                continue
            raw = blob_id(data)
            keep = stamp if now - s.st_mtime > RACY else None   # changed just now: perhaps again within the same tick
            if old is not None and old.raw == raw and old.mode == mode:
                out[p] = Entry(mode, old.sha, keep, raw)
                continue
            indexed = self.index.get(p)
            if indexed is not None and indexed[1] == raw:   # exactly the blob the index has: nothing for git to do
                self._mem[raw] = data
                out[p] = Entry(mode, raw, keep, raw)
                continue
            regular.append((p, mode, keep, raw, data))
        if regular:
            ids = self._git("hash-object", "-w", "--stdin-paths",
                            input="".join(p + "\n" for p, *_ in regular).encode("utf-8", "surrogateescape"))
            for (p, mode, keep, raw, data), sha in zip(regular, ids.decode().split()):
                if sha == raw:
                    self._mem[sha] = data
                out[p] = Entry(mode, sha, keep, raw)
        return out

    def _apply(self, new: Dict[str, Optional[Entry]], read: bool = True, force: bool = False) -> bool:
        changed = {p: e for p, e in new.items()
                   if not ((e is None and p not in self.entries) or (e is not None and e.same(self.entries.get(p))))}
        for p, e in new.items():   # what was learnt of unchanged files (their time, their raw hash) is kept too
            if e is not None and p not in changed:
                self.entries[p] = e
        if not changed and not force:
            return False
        lines = []
        for p, e in changed.items():
            if e is None:
                self.entries.pop(p, None)
                lines.append(f"0 {'0' * 40}\t{p}")
            else:
                self.entries[p] = e
                lines.append(f"{e.mode} {e.sha}\t{p}")
        if force:   # the engine's own index, from nothing
            try:
                self._index_file.unlink()
            except OSError:
                pass
            lines = [f"{e.mode} {e.sha}\t{p}" for p, e in self.entries.items()]
        if lines:
            self._git("update-index", "-z", "--index-info", index=True,
                      input="".join(line + "\0" for line in lines).encode("utf-8", "surrogateescape"))
        self.tree = self._git("write-tree", index=True).decode().strip()
        self._recommit(read)
        return True

    def _recommit(self, read: bool = True) -> bool:
        from .compare import _IDENTITY
        if not self.tree:
            return False
        self.commit = self._git("commit-tree", self.tree, *(["-p", self.head] if self.head else []),
                                "-m", "system-diff live", env=_IDENTITY).decode().strip()
        if read:
            self._read(deadline=None if self.done.is_set() else time.monotonic())
            self._bump()
        return True

    def identity(self) -> str:
        h = hashlib.sha1()
        for p in _byte_order(self.entries):
            h.update(f"{p}\0{self.entries[p].sha}\n".encode("utf-8", "surrogateescape"))
        return h.hexdigest()

    def _read(self, deadline: Optional[float]) -> None:
        """The blueprint of the files as they are now (with a deadline: of those read by then, partial)."""
        from . import compare, config, extract
        files = _byte_order(self.entries)
        blobs = {p: e.sha for p, e in self.entries.items() if e.mode != "160000"}
        src = LiveSource(self.root, files, blobs, self._mem)
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
        self._mem.clear()
        self.error = ""
        self.held = compare.Live(sha=self.commit, blueprint=bp, known=known, cfg=cfg, source=src,
                                 version=self.version + 1, identity=self.identity())
        self.ready.set()
        if known is None:
            self.done.set()

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
            new = [r for r in rel if r not in self.entries and r not in self.index]
            # never seen and not in the index: counts only if git would add it (it is not ignored)
            ignored = self._check_ignore(new) if new else set()
            want = [r for r in rel if r not in ignored]
            found = self._from_disk(want)
            for r, e in list(found.items()):
                if e is None and r not in self.entries:
                    del found[r]
            self.reported |= set(found)   # the next `git status` decides about them from then on
            self._apply(found)
            return self.version

    def _check_ignore(self, paths: List[str]) -> set:
        from .sources import run_hidden
        r = run_hidden(["git", "check-ignore", "-z", "--stdin"], cwd=self.root, capture_output=True,
                       input="".join(p + "\0" for p in paths).encode("utf-8", "surrogateescape"))
        return {x.decode("utf-8", "surrogateescape") for x in r.stdout.split(b"\0") if x}


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

    def __init__(self, engine: Engine, build: Callable[[tuple], dict], ttl: float = PAGE_TTL):
        self.engine, self.build, self.ttl = engine, build, ttl
        self.cache: Dict[tuple, Tuple[int, float, dict]] = {}
        self.wanted: Dict[tuple, float] = {}
        self.locks: Dict[tuple, threading.Lock] = {}
        self.guard = threading.Lock()
        self._painter: Optional[threading.Thread] = None

    def get(self, key: tuple) -> Tuple[int, dict]:
        """(version, page) for `key`: from memory when drawn at the current version, else drawn now."""
        with self.guard:
            self.wanted[key] = time.monotonic()
            lock = self.locks.setdefault(key, threading.Lock())
        hit = self._hit(key)
        if hit is not None:
            return hit
        with lock:   # one drawing of a page at a time: a second asker waits for it
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
