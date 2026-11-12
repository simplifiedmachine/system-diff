"""Git's own files, read (and a few written) in Python, so that what the views ask at every look costs no git process.

Starting a program can be slow: on some machines (endpoint security scanning each launch, a home folder on a network
drive) every `git` costs seconds, whatever it does. So what Python can read itself it reads here:

- where the repository's git dir and common dir are (a .git folder, or a .git file "gitdir: …" for a linked worktree,
  whose "commondir" names the shared one): locate()
- HEAD, the branch, any ref (loose files, then packed-refs), and the refs under a prefix: head(), branch(), ref(),
  refs(); an expression like "HEAD~1" or "<sha>^": rev()
- objects: a loose one is a zlib stream under objects/; any other (a pack) through ONE `git cat-file --batch` kept open
  for the process's life (CatFile: thread-safe, started again if it dies). A commit's tree, parents, time and message,
  and a tree's files, read so: commit(), ls_tree()
- the index (.git/index, versions 2 to 4): each file's mode, blob id and the stat data git keeps, read_index()
- writing loose objects (a blob, a tree, a commit) the way git does, for the engine's commits: write_object()

Anything not understood here (a reftable ref store, a sha256 repository's objects, a split or sparse index, an object
in an alternate store) raises Fallback, or returns None where None is an answer; the caller then runs git as before.
Only the standard library is imported: the thin clients (the pane's, the progress line) use locate() too.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import re
import struct
import subprocess
import threading
import time
import zlib
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

HEX = re.compile(r"^[0-9a-f]{40}$")


class Fallback(Exception):
    """Python cannot read this: ask git."""


class Repo:
    """Where a repository's git files are: `gitdir` (this worktree's), `common` (shared by its worktrees)."""
    __slots__ = ("top", "gitdir", "common", "reftable", "sha256")

    def __init__(self, top: Path, gitdir: Path, common: Path):
        self.top, self.gitdir, self.common = top, gitdir, common
        self.reftable = (common / "reftable").is_dir()
        try:
            text = (common / "config").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            text = ""
        self.sha256 = bool(re.search(r"objectformat\s*=\s*sha256", text))
        if re.search(r"refstorage\s*=\s*reftable", text):
            self.reftable = True


_REPOS: Dict[str, Repo] = {}
_LOCK = threading.Lock()


def _run(args: List[str], cwd: Path, data: Optional[bytes] = None) -> bytes:
    from .sources import run_hidden
    return run_hidden(["git", *args], cwd=str(cwd), capture_output=True, check=True, input=data).stdout


def locate(root: Path) -> Optional[Repo]:
    """The repository holding `root`: found by looking for .git upwards (no git process), cached; None outside one.
    Only when that finds nothing usable is git asked (once per root)."""
    key = str(root)
    with _LOCK:
        got = _REPOS.get(key)
    if got is not None and (got.gitdir / "HEAD").exists():
        return got
    here = Path(root).absolute()
    found: Optional[Repo] = None
    for d in (here, *here.parents):
        g = d / ".git"
        try:
            if g.is_dir():
                gitdir = g
            elif g.is_file():
                text = g.read_text(encoding="utf-8", errors="replace").strip()
                if not text.startswith("gitdir:"):
                    break
                p = Path(text[7:].strip())
                gitdir = p if p.is_absolute() else (d / p)
                gitdir = Path(os.path.normpath(gitdir))
            else:
                continue
            if not (gitdir / "HEAD").is_file():
                break
            common = gitdir
            if (gitdir / "commondir").is_file():
                c = Path((gitdir / "commondir").read_text(encoding="utf-8", errors="replace").strip())
                common = Path(os.path.normpath(c if c.is_absolute() else gitdir / c))
            found = Repo(d, gitdir, common)
        except OSError:
            found = None
        break
    if found is None:
        try:
            out = _run(["rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"], here).decode(
                "utf-8", "replace").splitlines()
            top, gitdir, common = Path(out[0]), Path(out[1]), Path(out[2])
            found = Repo(top, gitdir, common if common.is_absolute() else Path(os.path.normpath(here / common)))
        except Exception:  # noqa: BLE001 - not a repository, no git
            return None
    with _LOCK:
        _REPOS[key] = found
    return found


def git_dir(root: Path) -> Path:
    """This worktree's git dir (raises Fallback outside a repository)."""
    r = locate(root)
    if r is None:
        raise Fallback("not a repository")
    return r.gitdir


# ── refs ──────────────────────────────────────────────────────────────────────

def _per_worktree(name: str) -> bool:
    return (name == "HEAD" or name.startswith(("refs/worktree/", "refs/bisect/", "refs/rewritten/"))
            or ("/" not in name and name.isupper()))


_PACKED: Dict[str, Tuple[tuple, Dict[str, str]]] = {}


def _packed(common: Path) -> Dict[str, str]:
    """packed-refs as {name: sha}, read again only when the file changes."""
    f = common / "packed-refs"
    try:
        st = os.stat(f)
    except OSError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
    key = str(f)
    got = _PACKED.get(key)
    if got is not None and got[0] == stamp:
        return got[1]
    out: Dict[str, str] = {}
    try:
        for line in f.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
            if not line or line[0] in "#^":
                continue
            sha, _, name = line.partition(" ")
            if HEX.match(sha):
                out[name.strip()] = sha
    except OSError:
        return {}
    _PACKED[key] = (stamp, out)
    return out


def _read_ref(r: Repo, name: str, depth: int = 0) -> Optional[str]:
    if r.reftable:
        raise Fallback("reftable")
    if depth > 5:
        return None
    worktree = _per_worktree(name)
    base = r.gitdir if worktree else r.common
    try:
        text = (base / name).read_text(encoding="utf-8", errors="surrogateescape").strip()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        text = ""
    except OSError as exc:
        raise Fallback(str(exc)) from None
    if text.startswith("ref:"):
        return _read_ref(r, text[4:].strip(), depth + 1)
    if text:
        if HEX.match(text):
            return text
        raise Fallback(f"ref {name}: {text[:20]!r}")
    if worktree and r.gitdir != r.common:
        return None   # a linked worktree's own refs are never packed with the shared ones
    return _packed(r.common).get(name)


def _repo(root: Path) -> Repo:
    r = locate(root)
    if r is None:
        raise Fallback("not a repository")
    return r


def ref(root: Path, name: str) -> Optional[str]:
    """The commit (object) a full ref name ("HEAD", "refs/…") points to, or None when there is no such ref."""
    return _read_ref(_repo(root), name)


def head(root: Path) -> Optional[str]:
    """HEAD's commit, or None (no commit yet)."""
    return ref(root, "HEAD")


def symref(root: Path) -> Optional[str]:
    """What HEAD names ("refs/heads/main"), or None for a detached HEAD."""
    r = _repo(root)
    if r.reftable:
        raise Fallback("reftable")
    try:
        text = (r.gitdir / "HEAD").read_text(encoding="utf-8", errors="surrogateescape").strip()
    except OSError as exc:
        raise Fallback(str(exc)) from None
    return text[4:].strip() if text.startswith("ref:") else None


def branch(root: Path) -> Optional[str]:
    """The branch checked out ("main"), or None (a detached HEAD)."""
    s = symref(root)
    return s[len("refs/heads/"):] if s and s.startswith("refs/heads/") else None


def refs(root: Path, prefix: str) -> Dict[str, str]:
    """{full name: sha} of the refs under `prefix` (a folder, ending in "/"), or the one ref `prefix` names."""
    r = _repo(root)
    if r.reftable:
        raise Fallback("reftable")
    out: Dict[str, str] = {}
    if not prefix.endswith("/"):
        got = _read_ref(r, prefix)
        return {prefix: got} if got else {}
    worktree = _per_worktree(prefix)
    if not worktree or r.gitdir == r.common:
        out.update({k: v for k, v in _packed(r.common).items() if k.startswith(prefix)})
    base = (r.gitdir if worktree else r.common) / prefix
    for top, _dirs, names in os.walk(base):
        for n in names:
            if n.endswith(".lock"):
                continue
            name = (Path(top) / n).relative_to(r.gitdir if worktree else r.common).as_posix()
            got = _read_ref(r, name)
            if got:
                out[name] = got
    return out


# ── objects ───────────────────────────────────────────────────────────────────

class CatFile:
    """One `git cat-file --batch` for the life of the process: objects by id (or "<commit>:<path>") at the cost of a
    pipe round trip, never a new process. Thread-safe; started again if it dies."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.starts = 0

    def _start(self) -> None:
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=str(self.root), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kw)
        self.starts += 1
        from .sources import SPAWNS
        SPAWNS["git"] += 1

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.stdin.close()  # type: ignore[union-attr]
                p.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass

    def get(self, name: str) -> Optional[Tuple[str, bytes]]:
        """(type, content) of an object, or None when there is none by that name."""
        if "\n" in name or not name:
            return None
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.proc is None or self.proc.poll() is not None:
                        self._start()
                    p = self.proc
                    p.stdin.write(name.encode("utf-8", "surrogateescape") + b"\n")  # type: ignore[union-attr]
                    p.stdin.flush()  # type: ignore[union-attr]
                    head = p.stdout.readline()  # type: ignore[union-attr]
                    if not head:
                        raise OSError("git cat-file ended")
                    parts = head.split()
                    if len(parts) != 3:
                        return None   # missing, ambiguous
                    size = int(parts[2])
                    data = p.stdout.read(size)  # type: ignore[union-attr]
                    p.stdout.read(1)  # type: ignore[union-attr]
                    if len(data) != size:
                        raise OSError("git cat-file: short read")
                    return parts[1].decode(), data
                except (OSError, ValueError):
                    self.close()
                    if attempt:
                        raise
        return None


_CATFILES: Dict[str, CatFile] = {}


def catfile(root: Path) -> CatFile:
    """The process's cat-file for this repository (one per git dir)."""
    r = locate(root)
    key = str(r.gitdir) if r is not None else str(root)
    with _LOCK:
        got = _CATFILES.get(key)
        if got is None:
            got = _CATFILES[key] = CatFile(r.top if r is not None else Path(root))
            while len(_CATFILES) > 8:   # a process that looks at many repositories keeps the last few open
                _CATFILES.pop(next(iter(_CATFILES))).close()
        return got


@atexit.register
def _close_all() -> None:
    for c in list(_CATFILES.values()):
        c.close()


def _loose(r: Repo, sha: str) -> Optional[Tuple[str, bytes]]:
    try:
        raw = zlib.decompress((r.common / "objects" / sha[:2] / sha[2:]).read_bytes())
    except (OSError, zlib.error):
        return None
    head, _, body = raw.partition(b"\0")
    kind, _, size = head.partition(b" ")
    if not size.isdigit() or int(size) != len(body):
        return None
    return kind.decode(), body


def read_object(root: Path, sha: str) -> Optional[Tuple[str, bytes]]:
    """(type, content) of an object: loose from disk, else through the cat-file process; None when there is none."""
    r = _repo(root)
    if not r.sha256 and HEX.match(sha):
        got = _loose(r, sha)
        if got is not None:
            return got
    return catfile(root).get(sha)


_COMMITS: Dict[str, dict] = {}


def commit(root: Path, sha: str) -> Optional[dict]:
    """{"tree", "parents", "time" (committer), "message"} of a commit (peeling an annotated tag), cached by id."""
    got = _COMMITS.get(sha)
    if got is not None:
        return got
    seen = 0
    obj = read_object(root, sha)
    while obj is not None and obj[0] == "tag" and seen < 5:
        m = re.match(rb"object ([0-9a-f]+)", obj[1])
        obj = read_object(root, m.group(1).decode()) if m else None
        seen += 1
    if obj is None or obj[0] != "commit":
        return None
    head, _, message = obj[1].partition(b"\n\n")
    info: dict = {"tree": "", "parents": [], "time": 0, "message": message.decode("utf-8", "replace")}
    for line in head.split(b"\n"):
        key, _, value = line.partition(b" ")
        if key == b"tree":
            info["tree"] = value.decode()
        elif key == b"parent":
            info["parents"].append(value.decode())
        elif key == b"committer":
            m = re.search(rb" (\d+) [+-]\d{4}$", value)
            info["time"] = int(m.group(1)) if m else 0
    if len(_COMMITS) > 4096:
        _COMMITS.clear()
    _COMMITS[sha] = info
    return info


_TREES: Dict[str, List[Tuple[str, str, str]]] = {}


def _tree(root: Path, sha: str) -> List[Tuple[str, str, str]]:
    """A tree's entries (mode, name, id), cached by id (a tree never changes)."""
    got = _TREES.get(sha)
    if got is not None:
        return got
    obj = read_object(root, sha)
    if obj is None or obj[0] != "tree":
        raise Fallback(f"tree {sha} not readable")
    data, i, out = obj[1], 0, []
    while i < len(data):
        sp = data.index(b" ", i)
        nul = data.index(b"\0", sp)
        out.append((data[i:sp].decode(), data[sp + 1:nul].decode("utf-8", "surrogateescape"),
                    data[nul + 1:nul + 21].hex()))
        i = nul + 21
    if len(_TREES) > 20000:
        _TREES.clear()
    _TREES[sha] = out
    return out


def ls_tree(root: Path, commit_sha: str) -> List[Tuple[str, str, str, str]]:
    """Every file of a commit, as `git ls-tree -r` lists them: (mode, type, id, path), in git's order."""
    if _repo(root).sha256:
        raise Fallback("sha256")
    info = commit(root, commit_sha)
    if info is None:
        raise Fallback(f"no commit {commit_sha}")
    out: List[Tuple[str, str, str, str]] = []

    def walk(tree: str, prefix: str) -> None:
        for mode, name, sha in _tree(root, tree):
            if mode in ("40000", "040000"):
                walk(sha, prefix + name + "/")
            else:
                mode = mode.rjust(6, "0")
                out.append((mode, "commit" if mode == "160000" else "blob", sha, prefix + name))
    walk(info["tree"], "")
    return out


_REV = re.compile(r"^(.*?)((?:[~^]\d*)*)(\^\{commit\})?$")


def rev(root: Path, expr: str) -> Optional[str]:
    """The commit an expression names: a full id, HEAD, a full ref name, a branch or tag name, each followed by any
    of ~N, ^N, ^{commit}. None when it names nothing; Fallback for what this does not read (abbreviated ids, @{…})."""
    m = _REV.match(expr.strip())
    if not m or not m.group(1) or "@{" in expr or ":" in expr:
        raise Fallback(expr)
    base, steps = m.group(1), m.group(2)
    if HEX.match(base):
        sha: Optional[str] = base
    elif base in ("HEAD", "@") or base.startswith("refs/"):
        sha = ref(root, "HEAD" if base == "@" else base)
    elif re.match(r"^[0-9a-f]{4,39}$", base):
        raise Fallback("abbreviated id")
    else:
        sha = None
        for full in ((base,) if _per_worktree(base) else ()) + (f"refs/{base}", f"refs/tags/{base}", f"refs/heads/{base}", f"refs/remotes/{base}",
                     f"refs/remotes/{base}/HEAD"):
            sha = ref(root, full)
            if sha:
                break
    sha = _peel(root, sha) if sha else None
    if not sha:
        return None
    for step in re.findall(r"[~^]\d*", steps):
        n = int(step[1:]) if len(step) > 1 else 1
        for _ in range(n if step[0] == "~" else 1):
            info = commit(root, sha)
            k = 1 if step[0] == "~" else n
            if not info or len(info["parents"]) < k:
                return None
            if k == 0:
                break
            sha = info["parents"][k - 1]
    return sha


def _peel(root: Path, sha: str) -> Optional[str]:
    """The commit an object id names: itself, or the commit an annotated tag points to; None for anything else."""
    for _ in range(5):
        obj = read_object(root, sha)
        if obj is None:
            return None
        if obj[0] == "commit":
            return sha
        if obj[0] != "tag":
            return None
        m = re.match(rb"object ([0-9a-f]+)", obj[1])
        if not m:
            return None
        sha = m.group(1).decode()
    return None


# ── writing objects ───────────────────────────────────────────────────────────

def object_id(kind: str, data: bytes) -> str:
    h = hashlib.sha1(b"%s %d\0" % (kind.encode(), len(data)))
    h.update(data)
    return h.hexdigest()


def has_loose(root: Path, sha: str) -> bool:
    r = _repo(root)
    return (r.common / "objects" / sha[:2] / sha[2:]).exists()


def write_object(root: Path, kind: str, data: bytes) -> str:
    """Write a loose object as git does (zlib, under objects/xx/), unless there is one already; its id."""
    r = _repo(root)
    if r.sha256:
        raise Fallback("sha256")
    sha = object_id(kind, data)
    folder = r.common / "objects" / sha[:2]
    path = folder / sha[2:]
    if path.exists():
        return sha
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"tmp_obj_{os.getpid()}_{threading.get_ident()}_{time.monotonic_ns()}"
    with open(tmp, "wb") as f:
        f.write(zlib.compress(b"%s %d\0" % (kind.encode(), len(data)) + data, 1))
    try:
        os.chmod(tmp, 0o444)
    except OSError:
        pass
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        if not path.exists():
            raise
    return sha


def write_tree(root: Path, files: Dict[str, Tuple[str, str]]) -> str:
    """Write the trees of {path: (mode, blob id)} (the blobs must be there already); the root tree's id."""
    tree: dict = {}
    for path, (mode, sha) in files.items():
        node = tree
        parts = path.split("/")
        for d in parts[:-1]:
            node = node.setdefault(d + "/", {})
        node[parts[-1]] = (mode, sha)

    def write(node: dict) -> str:
        entries = []
        for name, v in node.items():
            if isinstance(v, dict):
                entries.append((name.encode("utf-8", "surrogateescape"), b"40000",
                                name[:-1].encode("utf-8", "surrogateescape"), write(v)))
            else:
                n = name.encode("utf-8", "surrogateescape")
                entries.append((n, v[0].encode(), n, v[1]))
        entries.sort(key=lambda e: e[0])   # git's order: a folder sorts as its name with "/" after it
        body = b"".join(mode + b" " + name + b"\0" + bytes.fromhex(sha) for _k, mode, name, sha in entries)
        return write_object(root, "tree", body)
    return write(tree)


def write_commit(root: Path, tree: str, parents: Iterable[str], message: str, who: str = "system-diff",
                 email: str = "system-diff@localhost") -> str:
    stamp = f"{who} <{email}> {int(time.time())} +0000"
    lines = [f"tree {tree}"] + [f"parent {p}" for p in parents] + [f"author {stamp}", f"committer {stamp}"]
    return write_object(root, "commit", ("\n".join(lines) + "\n\n" + message.rstrip("\n") + "\n").encode())


# ── the index ─────────────────────────────────────────────────────────────────

class IndexEntry:
    __slots__ = ("mode", "sha", "stage", "mtime", "size", "skip")

    def __init__(self, mode: str, sha: str, stage: int, mtime: Tuple[int, int], size: int, skip: bool):
        self.mode, self.sha, self.stage, self.mtime, self.size, self.skip = mode, sha, stage, mtime, size, skip


def read_index(path: Path) -> Tuple[Dict[str, IndexEntry], Dict[str, int]]:
    """({path: entry} of stage 0, {path: stages} of unmerged paths) from an index file; Fallback for what is not read
    here (a split or sparse index, an unknown version)."""
    data = path.read_bytes()
    if data[:4] != b"DIRC" or len(data) < 12:
        raise Fallback("not an index")
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3, 4):
        raise Fallback(f"index version {version}")
    pos, prev = 12, b""
    out: Dict[str, IndexEntry] = {}
    unmerged: Dict[str, int] = {}
    for _ in range(count):
        (_cs, _cn, ms, mn, _dev, _ino, mode, _uid, _gid, size) = struct.unpack(">10I", data[pos:pos + 40])
        sha = data[pos + 40:pos + 60].hex()
        flags = struct.unpack(">H", data[pos + 60:pos + 62])[0]
        p = pos + 62
        skip = False
        if flags & 0x4000:
            if version < 3:
                raise Fallback("extended flags")
            ext = struct.unpack(">H", data[p:p + 2])[0]
            skip = bool(ext & 0x4000)
            p += 2
        if version == 4:
            c = data[p]
            p += 1
            strip = c & 127
            while c & 128:
                c = data[p]
                p += 1
                strip = ((strip + 1) << 7) | (c & 127)
            end = data.index(b"\0", p)
            name = prev[:len(prev) - strip] + data[p:end]
            pos = end + 1
        else:
            end = data.index(b"\0", p)
            name = data[p:end]
            pos += ((end - pos) + 8) // 8 * 8
        prev = name
        stage = (flags >> 12) & 3
        if (mode >> 12) == 0o04:
            raise Fallback("sparse index")
        p_name = name.decode("utf-8", "surrogateescape")
        if stage:
            unmerged[p_name] = unmerged.get(p_name, 0) | (1 << stage)
            out.pop(p_name, None)
            continue
        out[p_name] = IndexEntry(f"{mode:o}", sha, 0, (ms, mn), size, skip)
    end_data = len(data) - 20
    while pos + 8 <= end_data:
        sig = data[pos:pos + 4]
        ln = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        if sig in (b"link", b"sdir"):
            raise Fallback("split or sparse index")
        pos += 8 + ln
    return out, unmerged
