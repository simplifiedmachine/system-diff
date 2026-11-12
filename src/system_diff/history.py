"""How the system evolved: a timeline of its structural changes, each told as what the person asked for and what the
system got ("You asked: add document uploads → it got: a call to Amazon S3, 3 endpoints under /flows/documents").

An ENTRY: {"id", "at" (its latest change), "since" (its first), "request" (the person's request it came under, cut to
REQUEST_MAX, or None), "request_full", "source" ("turn": while Claude worked on a request; "commit": a commit's own
change, from git; "edit": a change no request explains, such as the person's own edit), "commit" (its id, or None),
"message" (a commit's first line), "summary" (one plain line), "got" (what the system got, in plain words),
"changes" (ranked as human.decisions gives them: id, title, why, kind, parts, names, sign, status, entity, op,
subject, label, impact), "parts" (every part it touched), "base" and "head" (the two states it is the diff between:
a commit id, "empty", or "state:<key>" for a working tree the engine saw), "files" (files changed, when known)}.

Where entries come from:
- as it happens (Recorder, run by the resident engine, live.Engine): each new model whose STRUCTURE (parts,
  connections, routes, settings, tables, outside systems: never line counts) differs from the last recorded one is a
  change; it is attributed to the person's latest request before it (the activity log's prompts when the hooks run,
  else Claude Code's transcripts, objectives.requests). Bursts coalesce: successive changes under one request fold
  into one entry, the diff from where the burst started, until a new request arrives or QUIET seconds pass.
- from git (backfill): the commits from HEAD backwards (first parent, at most BACKFILL), newest first, each the
  structural diff from its parent, its message the summary when no request in the transcripts is close before it.
  A commit whose changes were already recorded as they happened only gets its id added to those entries. Read
  through gitfs (loose objects, the one cat-file process), never one git process per file; blueprints are kept by
  tree, so a tree read once is never read again.

Kept in <git dir>/system-diff/history.jsonl (one entry a line; a later line with the same id replaces an earlier one,
{"id", "gone": true} removes it; compacted when large), the blueprints of the states it names under history/, and
where it is in history-state.json: private, never committed, never sent anywhere. Off with SYSTEM_DIFF_HISTORY=0.
Observational only: nothing here acts on the code.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .sources import git_dir

LOG = "history.jsonl"
STATE = "history-state.json"
STATES = "history"          # blueprints of the states entries name: <key>.json (working trees), t-<tree>-<cfg>.json
QUIET = 120.0               # seconds without a change after which a burst's entry is final
BACKFILL = 50               # commits read back from HEAD
REQUEST_MAX = 200
FULL_MAX = 4000
MAX_BYTES = 1 << 20         # past this, the log is compacted to the newest KEEP entries
KEEP = 500
STATES_KEEP = 300           # blueprint files kept
SEEN_KEEP = 2000            # commits remembered as read
REQUEST_WINDOW = 6 * 3600   # a request this long before a commit (and after its parent) can be what it was for
TURN_WINDOW = 3 * 3600      # without hooks: a change this soon after a request was made under it
_LOCK = threading.RLock()


def enabled() -> bool:
    return os.environ.get("SYSTEM_DIFF_HISTORY", "1") != "0"


# ── the log ───────────────────────────────────────────────────────────────────

def _log(root: Path) -> Path:
    return git_dir(root) / LOG


def _states(root: Path) -> Path:
    return git_dir(root, STATES)


def load(root: Path) -> List[Dict[str, Any]]:
    """Every entry, newest first (a later line with the same id replaces an earlier one; a "gone" line removes it)."""
    try:
        text = _log(root).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: "OrderedDict[str, dict]" = OrderedDict()
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue   # a line cut short
        if not isinstance(e, dict) or not isinstance(e.get("id"), str):
            continue
        if e.get("gone"):
            found.pop(e["id"], None)
        elif isinstance(e.get("at"), (int, float)) and isinstance(e.get("changes"), list):
            found[e["id"]] = e
    return sorted(found.values(), key=lambda e: (-float(e["at"]), e["id"]))


def _write(root: Path, items: Iterable[Dict[str, Any]]) -> None:
    data = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in items)
    if not data:
        return
    with _LOCK:
        path = _log(root)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(data)
        try:
            big = path.stat().st_size > MAX_BYTES
        except OSError:
            big = False
        if big:
            compact(root)


def compact(root: Path, keep: int = KEEP) -> None:
    """The log again with only each entry's latest line, the newest `keep` entries (oldest first, as appended)."""
    with _LOCK:
        entries = load(root)[:keep][::-1]
        path = _log(root)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
        os.replace(tmp, path)


def _state(root: Path) -> Dict[str, Any]:
    try:
        got = json.loads((git_dir(root) / STATE).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(root: Path, st: Dict[str, Any]) -> None:
    st["commits"] = list(dict.fromkeys(st.get("commits") or []))[-SEEN_KEEP:]
    path = git_dir(root) / STATE
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# ── states: blueprints by structure (working trees) and by tree (commits) ────

def struct(bp: Dict[str, Any]) -> Dict[str, Any]:
    """What counts as the system's structure: parts, connections, routes, outside systems and who uses them,
    containers, tables, migrations and settings. Not line counts, not which files a part has."""
    def keys(name: str) -> List[str]:
        return sorted(bp.get(name) or {})
    return {"components": {k: (v or {}).get("label", k) for k, v in (bp.get("components") or {}).items()},
            "flows": keys("flows"), "api": keys("api"), "internal": keys("internal"),
            "routes": dict(bp.get("routes") or {}),
            "externals": {k: sorted((v or {}).get("used_by") or []) for k, v in (bp.get("externals") or {}).items()},
            "containers": bp.get("containers") or {},
            "tables": {k: sorted(v or []) for k, v in (bp.get("tables") or {}).items()},
            "migrations": sorted(bp.get("migrations") or []),
            "env": {k: sorted(v or []) for k, v in (bp.get("env") or {}).items()}}


def struct_key(bp: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(struct(bp), sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _cfg_key(cfg: Dict[str, Any]) -> str:
    from .extract import EXTRACT_VERSION
    return hashlib.sha1((json.dumps(cfg, sort_keys=True, default=str) + EXTRACT_VERSION).encode()).hexdigest()[:10]


def _prune(folder: Path) -> None:
    try:
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-STATES_KEEP]:
            old.unlink()
    except OSError:
        pass


def keep_state(root: Path, bp: Dict[str, Any], key: Optional[str] = None) -> str:
    """Keep a working tree's blueprint by its structure: "state:<key>"."""
    key = key or struct_key(bp)
    path = _states(root) / f"{key}.json"
    if not path.exists():
        try:
            path.write_text(json.dumps(bp), encoding="utf-8")
            _prune(path.parent)
        except OSError:
            pass
    else:
        try:
            os.utime(path)
        except OSError:
            pass
    return "state:" + key


class _Empty:
    """No files: the system before the first commit."""

    def __init__(self, root: Path):
        self.root, self.files, self.blobs, self.label = Path(root), [], {}, "empty"

    def read_many(self, paths: Iterable[str]) -> Dict[str, str]:
        return {p: "" for p in paths}


_BPS: "OrderedDict[tuple, dict]" = OrderedDict()


def commit_blueprint(root: Path, sha: Optional[str], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The blueprint of a commit ("empty" or None: of nothing), read with `cfg`, kept by tree in memory and on disk."""
    from . import extract, gitfs
    from .sources import GitSource
    if not sha or sha == "empty":
        bp = extract.extract(_Empty(root), cfg, cache=False)
        bp["ref"] = "nothing yet"
        return bp
    info = gitfs.commit(root, sha)
    tree = (info or {}).get("tree") or sha
    ck = _cfg_key(cfg)
    key = (str(root), tree, ck)
    got = _BPS.get(key)
    if got is None:
        path = _states(root) / f"t-{tree}-{ck}.json"
        try:
            got = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            got = extract.extract(GitSource(sha, root), cfg)
            try:
                path.write_text(json.dumps(got), encoding="utf-8")
                _prune(path.parent)
            except OSError:
                pass
        _BPS[key] = got
        while len(_BPS) > 16:
            _BPS.popitem(last=False)
    bp = dict(got)
    bp["ref"] = sha[:7]
    return bp


def state_blueprint(root: Path, ref: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The blueprint an entry's base or head names; None when it is no longer kept."""
    if ref.startswith("state:"):
        try:
            return json.loads((_states(root) / f"{ref[6:]}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    try:
        return commit_blueprint(root, ref, cfg)
    except Exception:  # noqa: BLE001 - a commit gone (a rebase, a gc): no picture of it
        return None


# ── what changed, in plain words ──────────────────────────────────────────────

def comparison(a: Dict[str, Any], b: Dict[str, Any], cfg: Dict[str, Any], base: str = "before", head: str = "after"):
    """A comparison of two blueprints, structure only (no line counts: those are code inside a part)."""
    from .compare import Comparison
    from .diff import diff
    from .extract import Code
    a, b = dict(a), dict(b)
    a.setdefault("ref", base)
    b.setdefault("ref", head)
    return Comparison(diff(a, b, None, (), Code(cfg)), a, b, cfg, base, head)


def ranked(c) -> List[Dict[str, Any]]:
    """The comparison's changes, the most consequential first, as kept in an entry."""
    from . import human
    labels = {(ch["entity"], ch["op"], ch["id"]): ch for ch in c.diff["changes"]}
    out = []
    for d in human.decisions(c):
        ch = labels.get((d["entity"], d["op"], d["subject"])) or {}
        out.append({"id": d["id"], "title": d["title"], "why": d["why"], "kind": d["kind"], "parts": d["parts"],
                    "names": d["names"], "sign": d["sign"], "status": d["status"], "entity": d["entity"],
                    "op": d["op"], "subject": d["subject"], "label": ch.get("label", d["subject"]),
                    "detail": list(ch.get("detail") or []), "impact": d["impact"], "look": d["look"]})
    return out


def _segments(route: str) -> List[str]:
    path = route.split(" ", 1)[-1]
    return [s for s in path.split("/") if s]


def _routes_phrase(chs: List[dict], added: bool) -> str:
    if len(chs) == 1:
        return (f"the endpoint {chs[0]['label']}" if added else f"the endpoint {chs[0]['label']} removed")
    segs = [_segments(c["label"]) for c in chs]
    common: List[str] = []
    for parts in zip(*segs):
        if len(set(parts)) != 1 or parts[0].startswith(("{", ":", "<")):
            break
        common.append(parts[0])
    under = f" under /{'/'.join(common)}" if common else ""
    return f"{len(chs)} endpoints{under}" + ("" if added else " removed")


def _name(c: dict, i: int) -> str:
    names = c.get("names") or []
    return names[i] if len(names) > i else c.get("label") or c.get("subject") or ""


def phrases(changes: List[dict]) -> List[str]:
    """What the system got, a few words per kind of change, the most consequential first:
    ["a call to Amazon S3", "3 endpoints under /flows/documents"]."""
    groups: "OrderedDict[tuple, List[dict]]" = OrderedDict()
    for c in changes:
        groups.setdefault((c["entity"], c["op"]), []).append(c)
    out: List[str] = []
    for (entity, op), chs in groups.items():
        add, gone = op == "add", op == "remove"
        if entity == "route":
            out.append(_routes_phrase(chs, not gone))
            continue
        for c in chs:
            if entity == "external":
                x = _name(c, 1)
                db = "read and write" in c.get("title", "")
                out.append((f"no more {x}" if gone else f"data kept in {x}" if db else f"a call to {x}"))
            elif entity == "flow":
                s, d = _name(c, 0), _name(c, 1)
                out.append(f"{s} no longer using {d}" if gone else f"{s} now using {d}")
            elif entity == "component":
                n = _name(c, 0)
                out.append(f"the part {n} removed" if gone else f"a new part, {n}" if add else f"changes inside {n}")
            elif entity == "table":
                cols = ", ".join(x.replace("+ column ", "") for x in c.get("detail") or [])
                out.append(f"the table {c['label']}" if add else f"the table {c['label']} dropped" if gone
                           else f"{cols} on the table {c['label']}" if cols else f"changes to the table {c['label']}")
            elif entity == "migration":
                if not any(k[0] == "table" for k in groups):
                    out.append(f"the migration {c['label']}")
            elif entity == "settings":
                names = (c.get("detail") or [""])[0]
                n = len([x for x in names.split(",") if x.strip()])
                out.append(f"{n} new setting{'' if n == 1 else 's'} in {_name(c, 0)} ({names})" if n
                           else f"new settings in {_name(c, 0)}")
            elif entity == "container":
                out.append(f"the container {c['label']}" if add else f"the container {c['label']} removed" if gone
                           else f"changes to the container {c['label']}")
            elif entity == "deploy":
                out.append(f"changes to {c['label']}")
    return list(dict.fromkeys(p for p in out if p))


def got_line(changes: List[dict], most: int = 3) -> str:
    from .design import and_list
    said = phrases(changes)
    return and_list(said, most) if said else "no change to the system's structure"


def when(at: float, clock: Optional[float] = None) -> str:
    """"just now", "5 min ago", "3 h ago", "2 days ago", then the date."""
    s = max(0.0, (clock or time.time()) - at)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    if s < 172800:
        return "yesterday"
    if s < 14 * 86400:
        return f"{int(s // 86400)} days ago"
    return time.strftime("%d %b %Y", time.localtime(at)).lstrip("0")


def _cut(text: Optional[str], most: int) -> Optional[str]:
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


def asked_line(e: Dict[str, Any]) -> str:
    """What the entry came under, in a few words: the request, the commit, or an edit no request explains."""
    if e.get("request"):
        return f"You asked: “{e['request']}”"
    if e.get("source") == "commit":
        return f"commit {str(e.get('commit') or '')[:7]}: {e.get('message') or 'no message'}"
    return "changed outside a request (an edit by hand, a checkout)"


def plain(e: Dict[str, Any], clock: Optional[float] = None) -> str:
    """"2 days ago · You asked: “…” → it got: …"."""
    return f"{when(e['at'], clock)} · {asked_line(e)} → it got: {e.get('got') or got_line(e['changes'])}"


def _entry(eid: str, at: float, since: float, request: Optional[dict], source: str, commit: Optional[str],
           message: str, changes: List[dict], base: str, head: str, files: Optional[int]) -> Dict[str, Any]:
    text = (request or {}).get("text") or None
    got = got_line(changes)
    return {"id": eid, "at": round(at, 3), "since": round(since, 3), "request": _cut(text, REQUEST_MAX),
            "request_full": _cut(text, FULL_MAX), "source": source, "commit": commit, "message": message or "",
            "summary": got if text or not message else message, "got": got, "changes": changes,
            "parts": sorted({p for c in changes for p in c.get("parts") or []}), "base": base, "head": head,
            "files": files}


# ── the person's requests ─────────────────────────────────────────────────────

_REQS: Dict[str, Tuple[float, tuple, List[dict]]] = {}
_INJECTED = re.compile(r"\s*<[a-z]+(?:-[a-z]+)+[\s>]")   # <agent-message …>, <task-notification>: not the person's


def typed(text: Any) -> bool:
    """Whether a prompt is the person's own words (not a notice, a command's echo, another agent's message)."""
    from . import activity
    return isinstance(text, str) and bool(text.strip()) and not activity.is_system(text) \
        and not _INJECTED.match(text)


def requests(root: Path, ttl: float = 30.0) -> List[Dict[str, Any]]:
    """The person's requests, oldest first: {"at", "text"} from the activity log (the hooks) and Claude Code's
    transcripts (objectives.requests), read again at most every `ttl` seconds or when the log moves."""
    from . import activity
    try:
        st = (git_dir(root) / activity.LOG).stat()
        mark: tuple = (st.st_mtime_ns, st.st_size)
    except OSError:
        mark = ()
    key = str(root)
    got = _REQS.get(key)
    if got is not None and got[1] == mark and time.time() - got[0] < ttl:
        return got[2]
    out: List[Dict[str, Any]] = []
    try:
        from . import objectives
        out = [r for r in objectives.requests(root) if typed(r.get("text"))]
    except Exception:  # noqa: BLE001 - no transcripts: the log alone
        out = []
    full = {}
    for e in activity.events(root, 2000):   # the log's own text, uncut, where the hooks kept it
        if e.get("event") == activity.PROMPT and typed(e.get("text")):
            full[round(float(e["at"]))] = e["text"]
    for r in out:
        long = full.get(round(float(r["at"])))
        if long:
            r["full"] = long
    _REQS[key] = (time.time(), mark, out)
    return out


def _latest_prompt(root: Path) -> Tuple[Optional[dict], Optional[float]]:
    """From the activity log (the hooks): the person's latest request and when Claude last stopped after it."""
    from . import activity
    req, stop = None, None
    for e in activity.events(root, 2000):
        kind = e.get("event")
        if kind == activity.PROMPT and typed(e.get("text")):
            req, stop = {"at": float(e["at"]), "text": e["text"]}, None
        elif kind == activity.STOP and req is not None:
            stop = float(e["at"])
    return req, stop


def request_for_change(root: Path, now: float, quiet: float = QUIET) -> Tuple[Optional[dict], str]:
    """The request a change seen now came under, and its source: "turn", or "edit" when none explains it (Claude had
    stopped well before it, or no request is recent)."""
    req, stop = _latest_prompt(root)
    if req is not None:
        if stop is not None and now - stop > quiet:
            return None, "edit"
        return req, "turn"
    reqs = [r for r in requests(root) if r["at"] <= now + 5]
    if reqs and now - reqs[-1]["at"] < TURN_WINDOW:
        r = reqs[-1]
        return {"at": r["at"], "text": r.get("full") or r["text"]}, "turn"
    return None, "edit"


def request_for_commit(reqs: List[dict], at: float, parent_at: float) -> Optional[dict]:
    """The latest request close before a commit (after its parent, at most REQUEST_WINDOW before it)."""
    best = None
    for r in reqs:
        if parent_at < r["at"] <= at + 60 and at - r["at"] <= REQUEST_WINDOW:
            best = r
    return {"at": best["at"], "text": best.get("full") or best["text"]} if best else None


# ── from git ──────────────────────────────────────────────────────────────────

def _files_changed(root: Path, a: Optional[str], b: str) -> Optional[int]:
    from .sources import GitSource
    try:
        x = GitSource(a, root).blobs if a else {}
        y = GitSource(b, root).blobs
        return sum(1 for p in set(x) | set(y) if x.get(p) != y.get(p))
    except Exception:  # noqa: BLE001
        return None


def _cfg(root: Path) -> Dict[str, Any]:
    from . import config
    return config.lenient(root)


def backfill(root: Path, cfg: Optional[Dict[str, Any]] = None, most: int = BACKFILL,
             publish: Optional[Callable[[], None]] = None, stop: Optional[threading.Event] = None,
             head: Optional[str] = None) -> int:
    """Read the commits from HEAD backwards (first parent) not read before, at most `most`, newest first: each one's
    structural change from its parent becomes an entry (published after each, through `publish`). The entries
    written."""
    from . import gitfs
    root = Path(root)
    cfg = cfg if cfg is not None else _cfg(root)
    try:
        head = head or gitfs.head(root)
    except gitfs.Fallback:
        return 0
    if not head:
        return 0
    with _LOCK:
        st = _state(root)
        entries = load(root)
    seen: Set[str] = set(st.get("commits") or [])
    seen |= {e["commit"] for e in entries if e.get("commit")}
    todo: List[Tuple[str, dict]] = []
    sha: Optional[str] = head
    while sha and sha not in seen and len(todo) < most:
        info = gitfs.commit(root, sha)
        if info is None:
            break
        todo.append((sha, info))
        sha = info["parents"][0] if info["parents"] else None
    reqs = requests(root) if todo else []
    written = 0
    ceiling: Optional[float] = None   # newest first, each older than the one before (commits made in one second too)
    for sha, info in todo:
        if stop is not None and stop.is_set():
            break
        parent = info["parents"][0] if info["parents"] else None
        pinfo = gitfs.commit(root, parent) if parent else None
        try:
            if pinfo is not None and pinfo.get("tree") == info.get("tree"):
                changes: List[dict] = []
            else:
                a = commit_blueprint(root, parent, cfg)
                b = commit_blueprint(root, sha, cfg)
                changes = ranked(comparison(a, b, cfg, (parent or "empty")[:7], sha[:7]))
        except Exception:  # noqa: BLE001 - a commit that cannot be read: skipped, the others still told
            changes = []
        with _LOCK:
            st = _state(root)
            st.setdefault("commits", []).append(sha)
            _save_state(root, st)
            if changes:
                written += _record_commit(root, sha, info, parent, pinfo, changes, reqs, ceiling)
        t = float(info.get("time") or time.time())
        ceiling = t if ceiling is None else min(t, ceiling - 0.001)
        if changes and publish is not None:
            try:
                publish()
            except Exception:  # noqa: BLE001
                pass
    return written


def _record_commit(root: Path, sha: str, info: dict, parent: Optional[str], pinfo: Optional[dict],
                   changes: List[dict], reqs: List[dict], ceiling: Optional[float] = None) -> int:
    """A commit's entry, unless what it changed was recorded as it happened: then those entries get its id, and only
    what they did not record (if anything) becomes the commit's own entry."""
    at = float(info.get("time") or time.time())
    parent_at = float((pinfo or {}).get("time") or 0)
    lived = [e for e in load(root) if e.get("source") in ("turn", "edit") and not e.get("commit")
             and parent_at < e["at"] <= at + 5]
    covered = {c["id"] for e in lived for c in e["changes"]}
    left = [c for c in changes if c["id"] not in covered]
    out = []
    if covered & {c["id"] for c in changes}:
        out += [{**e, "commit": sha} for e in lived if {c["id"] for c in e["changes"]} & {c["id"] for c in changes}]
    if left:
        message = (info.get("message") or "").strip().splitlines()[0][:200] if info.get("message") else ""
        req = request_for_commit(reqs, at, parent_at)
        shown = at if ceiling is None else min(at, ceiling - 0.001)
        out.append(_entry("c" + sha[:12], shown, shown, req, "commit", sha, message, left, parent or "empty", sha,
                          _files_changed(root, parent, sha)))
    _write(root, out)
    return len(out)


# ── as it happens: the resident engine's recorder ─────────────────────────────

class Recorder:
    """Watches the engine's model (live.Engine) and records each structural change as it happens; reads the commits
    in the background (backfill) when HEAD moves (and at the start). Runs in its own thread, never in a look's path."""

    def __init__(self, engine: Any, quiet: float = QUIET, most: int = BACKFILL, delay: float = 1.0):
        self.engine, self.root, self.quiet, self.most, self.delay = engine, Path(engine.root), quiet, most, delay
        self.last_key: Optional[str] = None
        self.head_seen: Optional[str] = None
        self.open: Optional[Dict[str, Any]] = None    # the entry of the burst under way: {"entry", "files"}
        self.blobs: Optional[Dict[str, str]] = None   # the files of the last state recorded (for "files changed")
        self.base_blobs: Optional[Dict[str, str]] = None
        self.loaded = False
        self.error = ""
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "Recorder":
        self._thread = threading.Thread(target=self._run, name="system-diff history", daemon=True)
        self._thread.start()
        return self

    def _publish(self) -> None:
        self.engine._bump()

    def _run(self) -> None:
        e = self.engine
        while not e.stopped.is_set() and not e.done.wait(0.5):
            pass
        if e.stopped.wait(self.delay):   # the first pages are drawn first
            return
        seen = -1
        while not e.stopped.is_set():
            try:
                self.step()
                self.error = ""
            except Exception as exc:  # noqa: BLE001 - next time
                self.error = f"{type(exc).__name__}: {exc}"
            seen = e.wait(e.version if seen == -1 else seen, timeout=5.0)

    def _load(self) -> None:
        st = _state(self.root)
        self.last_key = st.get("key") or None
        self.head_seen = st.get("head") or None
        self.loaded = True

    def _mark(self, key: str, head: Optional[str]) -> None:
        with _LOCK:
            st = _state(self.root)
            st.update({"key": key, "head": head, "at": time.time()})
            _save_state(self.root, st)
        self.last_key, self.head_seen = key, head

    def step(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Look once: the commits HEAD gained, then whether the structure moved since the last state recorded. The
        entry written or changed, if any."""
        e = self.engine
        held = e.held
        if held is None or held.known is not None or held.blueprint.get("partial") or not e.done.is_set():
            return None
        if not self.loaded:
            self._load()
        now = time.time() if now is None else now
        cfg, bp, head = held.cfg, held.blueprint, e.head
        moved = head != self.head_seen
        if moved and head:
            backfill(self.root, cfg, self.most, publish=self._publish, stop=e.stopped, head=head)
        key = struct_key(bp)
        blobs = e.blobs() if hasattr(e, "blobs") else None
        if self.last_key is None:                      # the first look: where the history starts
            keep_state(self.root, bp, key)
            self._mark(key, head)
            self.blobs = blobs
            return None
        if key == self.last_key:
            if moved:
                self._mark(key, head)
            self.blobs = blobs
            return None
        if moved and head:
            try:
                same = struct_key(commit_blueprint(self.root, head, cfg)) == key
            except Exception:  # noqa: BLE001
                same = False
            if same:                                   # a checkout: what changed is the commits', told by them
                keep_state(self.root, bp, key)
                self._mark(key, head)
                self.open, self.blobs = None, blobs
                return None
        got = self._record(bp, key, cfg, now, blobs)
        self._mark(key, head)
        self.blobs = blobs
        return got

    def _record(self, bp: Dict[str, Any], key: str, cfg: Dict[str, Any], now: float,
                blobs: Optional[Dict[str, str]]) -> Optional[Dict[str, Any]]:
        req, source = request_for_change(self.root, now, self.quiet)
        head_ref = keep_state(self.root, bp, key)
        op = self.open
        same_burst = (op is not None and now - op["entry"]["at"] < self.quiet
                      and (op["entry"].get("request_full") or None) == (_cut((req or {}).get("text"), FULL_MAX))
                      and op["entry"]["source"] == source)
        if same_burst:
            base_ref, since, eid = op["entry"]["base"], op["entry"]["since"], op["entry"]["id"]
            base_blobs = op.get("blobs")
        else:
            base_ref, since = "state:" + (self.last_key or ""), now
            eid = ("t" if source == "turn" else "e") + hashlib.sha1(f"{now}:{key}".encode()).hexdigest()[:11]
            base_blobs = self.blobs
        a = state_blueprint(self.root, base_ref, cfg)
        if a is None:
            return None
        changes = ranked(comparison(a, bp, cfg, "before", "after"))
        files = (sum(1 for p in set(base_blobs) | set(blobs) if base_blobs.get(p) != blobs.get(p))
                 if base_blobs is not None and blobs is not None else None)
        if not changes:                                # the burst undid itself: no entry
            if same_burst:
                _write(self.root, [{"id": eid, "gone": True, "at": now}])
                self.open = None
                self._publish()
            return None
        ent = _entry(eid, now, since, req, source, None, "", changes, base_ref, head_ref, files)
        _write(self.root, [ent])
        self.open = {"entry": ent, "blobs": base_blobs}
        self._publish()
        return ent


# ── reading it back ───────────────────────────────────────────────────────────

def members(part: str, model: Optional[Dict[str, Any]] = None) -> Set[str]:
    """The part ids a block stands for: itself, and, for a container or group, the parts drawn in it."""
    out = {part}
    for n in (model or {}).get("nodes") or []:
        if n.get("id") == part:
            out |= {i["id"] for i in n.get("items") or []}
    return out


def for_part(entries: List[Dict[str, Any]], parts: Set[str]) -> List[Dict[str, Any]]:
    """Only the entries that touched these parts, each with only its changes to them."""
    out = []
    for e in entries:
        mine = [c for c in e["changes"] if set(c.get("parts") or []) & parts]
        if mine:
            out.append({**e, "changes": mine, "got": got_line(mine), "parts": sorted({p for c in mine
                                                                                   for p in c.get("parts") or []})})
    return out


def find(root: Path, eid: str) -> Optional[Dict[str, Any]]:
    return next((e for e in load(root) if e["id"] == eid), None)


def entry_comparison(root: Path, e: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None):
    """The comparison an entry is (its base state against its head state), for drawing it; None when either
    state is no longer kept."""
    cfg = cfg if cfg is not None else _cfg(root)
    a = state_blueprint(root, e.get("base") or "empty", cfg)
    b = state_blueprint(root, e.get("head") or "", cfg) if e.get("head") else None
    if a is None or b is None:
        return None
    label = when(e["at"])
    return comparison(a, b, cfg, "before it", label)


def split_part(part: str) -> Tuple[str, str]:
    """A History page's part: "<block>" (only what changed it), "#<entry>" or "<block>#<entry>" (one entry)."""
    block, _, eid = (part or "").partition("#")
    return block, re.sub(r"[^A-Za-z0-9]", "", eid)


def text(root: Path, part: str = "", width: int = 100) -> str:
    """The timeline as text (`system-diff history`)."""
    from . import canvas
    entries = load(root)
    if part:
        entries = for_part(entries, {part, *(("x:" + part,) if not part.startswith("x:") else ())})
    if not entries:
        return ("No change to the structure of " + part + " recorded yet." if part else
                "No change to the system's structure recorded yet.")
    out = []
    for e in entries:
        out.append(canvas.cut(f"{when(e['at'])} · {asked_line(e)}", width))
        out.append(canvas.cut(f"  → it got: {e['got']}", width))
        for c in e["changes"][:6]:
            out.append(canvas.cut(f"    {c['sign']} {c['title']} — {c['why']}", width))
        if len(e["changes"]) > 6:
            out.append(f"    and {len(e['changes']) - 6} more")
        tail = []
        if e.get("commit"):
            tail.append(f"commit {e['commit'][:7]}")
        if e.get("files") is not None:
            tail.append(f"{e['files']} file{'' if e['files'] == 1 else 's'} changed")
        if tail:
            out.append("    " + " · ".join(tail))
        out.append("")
    return "\n".join(out).rstrip() + "\n"
