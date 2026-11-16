"""How the system evolved: a timeline of its structural changes, each told as what the person asked for and what the
system got, in one sentence ("You asked: add document uploads → Flows now stores files in Amazon S3 and serves 3
endpoints under /flows/documents"), and any two points of it compared (compare()).

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

Each entry's sentence (sentence()) is human.change_sentence's, from its changes, until an LLM has written a better one
(words(), in a background job, start_words(): structure only, never code; up to WORDS_BATCH entries a question; kept
as a {"id", "words": {"text", "by", "for"}} line in the log; off with SYSTEM_DIFF_LLM=off). One written by an LLM is
marked as such (✦) wherever it is shown.

Comparing two points (compare(), the History views' "compare:<A>..<B>"): from the state right after entry A to the
state right after entry B (or now), drawn from the blueprints kept under history/ (kept_blueprint: no git, no code
read on a look's path); a commit's not kept yet is read once in a background job (read_later), the views saying
"reading…" until it is.

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
        elif isinstance(e.get("words"), dict) and e["id"] in found:   # an LLM's sentence for it (words())
            found[e["id"]] = {**found[e["id"]], "words": e["words"]}
    return sorted((_readable(e) for e in found.values()), key=lambda e: (-float(e["at"]), e["id"]))


def _said(text: Any) -> Optional[str]:
    """A request as the person typed it: what they pasted or attached shown as "[pasted text]", never the markup."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        from .objectives import clean
        pasted = "<pasted_content" in text
        said = clean(text)
        if said and pasted:
            said = f"{said} [pasted text]"
        return said or ("[pasted text]" if pasted else None)
    except Exception:  # noqa: BLE001 - only wording
        return text


def _readable(e: Dict[str, Any]) -> Dict[str, Any]:
    """An entry as shown: its request cleaned (entries kept before cleaning was done carry the raw text)."""
    if e.get("request") or e.get("request_full"):
        full = _said(e.get("request_full") or e.get("request"))
        e = {**e, "request": _cut(full, REQUEST_MAX), "request_full": _cut(full, FULL_MAX)}
    return e


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
    if info is not None:
        _note_tree(root, sha, tree)
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


_TREES: Dict[str, Tuple[tuple, Dict[str, str]]] = {}


def _trees_file(root: Path) -> Path:
    return _states(root) / "commits.json"


def _trees(root: Path) -> Dict[str, str]:
    """Commit -> tree, for the commits whose blueprints are kept: so a look finds one without asking git."""
    f = _trees_file(root)
    try:
        st = f.stat()
        mark: tuple = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    got = _TREES.get(str(root))
    if got is not None and got[0] == mark:
        return got[1]
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    _TREES[str(root)] = (mark, data)
    return data


def _note_tree(root: Path, sha: str, tree: str) -> None:
    with _LOCK:
        known = dict(_trees(root))
        if known.get(sha) == tree:
            return
        known[sha] = tree
        if len(known) > STATES_KEEP * 2:
            known = dict(list(known.items())[-STATES_KEEP * 2:])
        f = _trees_file(root)
        tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(known), encoding="utf-8")
            os.replace(tmp, f)
        except OSError:
            pass


def kept_blueprint(root: Path, ref: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The blueprint a state names, only if it is kept (in memory or under history/): never asks git, never reads
    code, so a look can call it. None when it is not kept (for a commit: read_later() reads it)."""
    if not ref:
        return None
    if ref == "empty":
        return commit_blueprint(root, None, cfg)
    if ref.startswith("state:"):
        return state_blueprint(root, ref, cfg)
    tree = _trees(root).get(ref)
    if not tree:
        return None
    ck = _cfg_key(cfg)
    key = (str(root), tree, ck)
    got = _BPS.get(key)
    if got is None:
        try:
            got = json.loads((_states(root) / f"t-{tree}-{ck}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        _BPS[key] = got
        while len(_BPS) > 16:
            _BPS.popitem(last=False)
    bp = dict(got)
    bp["ref"] = ref[:7]
    return bp


READ_STALE = 600   # a reading job older than this died


def _job_lock(path: Path, stale: float) -> bool:
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime < stale:
                    return False
                path.unlink()
            except OSError:
                return False
        except OSError:
            return False
    return False


def _in_test() -> bool:
    """Under pytest, no job is started in the background unless the test asks (SYSTEM_DIFF_HISTORY_JOBS=1)."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and os.environ.get("SYSTEM_DIFF_HISTORY_JOBS") != "1"


def _spawn(root: Path, *args: str) -> None:
    import subprocess
    import sys
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                          "cwd": str(root), "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "system_diff.history", *args, str(root)], **kw)


def read_later(root: Path, sha: str) -> bool:
    """Read a commit's blueprint in the background, once (a job of its own: `python -m system_diff.history read`);
    when it is kept the log is touched, so the views draw again. True if a job was started."""
    if not re.fullmatch(r"[0-9a-f]{7,64}", sha or "") or _in_test():
        return False
    lock = _states(root) / f"reading-{sha[:16]}.lock"
    if not _job_lock(lock, READ_STALE):
        return False
    try:
        _spawn(root, "read", sha)
        return True
    except Exception:  # noqa: BLE001
        try:
            lock.unlink()
        except OSError:
            pass
        return False


def _touch(root: Path) -> None:
    """Move the log's time: the engine watches it, so the pages are drawn again."""
    try:
        path = _log(root)
        if not path.exists():
            path.write_text("", encoding="utf-8")
        os.utime(path)
    except OSError:
        pass


def read_now(root: Path, sha: str) -> bool:
    """The job read_later() starts: the commit's blueprint read and kept; the lock freed."""
    try:
        commit_blueprint(root, sha, _cfg(root))
        _touch(root)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            (_states(root) / f"reading-{sha[:16]}.lock").unlink()
        except OSError:
            pass


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
        one = {"id": d["id"], "title": d["title"], "why": d["why"], "kind": d["kind"], "parts": d["parts"],
               "names": d["names"], "sign": d["sign"], "status": d["status"], "entity": d["entity"],
               "op": d["op"], "subject": d["subject"], "label": ch.get("label", d["subject"]),
               "detail": list(ch.get("detail") or []), "impact": d["impact"], "look": d["look"]}
        if d["entity"] == "external":     # what the outside system is for: the sentence's verb ("sends email through")
            ext = d["subject"].split(">")[-1]
            x = (c.after.get("externals") or {}).get(ext) or (c.before.get("externals") or {}).get(ext) or {}
            one["outside"] = human.outside_kind(ext, x.get("label", ""), x.get("shape", ""))
        out.append(one)
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
    """"2 days ago · You asked: “…” → Control now keeps data in SQLite"."""
    return f"{when(e['at'], clock)} · {asked_line(e)} → {sentence(e)[0]}"


# ── each entry in one sentence: deterministic (human.change_sentence), better words from an LLM later ─────────────

WORDS_STATE = "history-words.json"
WORDS_BATCH = 20          # entries per question
WORDS_GAP = 120.0         # seconds between two jobs
WORDS_RETRY = 24 * 3600   # an entry is asked about once a day at most
WORDS_STALE = 900         # a job older than this died
WORDS_TIMEOUT = 180
WORDS_MAX = 140           # an answer longer than this is cut
WORDS_SYSTEM = (
    "You summarise changes to a software system's structure for the person who directs the work. For each entry you "
    "get its exact, machine-extracted structural changes and a draft sentence. Write one plain sentence of at most "
    "120 characters saying what the system gained or lost, grouped by part and phrased as what each part can now do "
    "(\"Control now sends email and keeps data in SQLite and Redis; 6 smaller changes\"), the most consequential "
    "first. Name only parts, outside systems, endpoints, tables and settings that appear in its changes. No code, no "
    "jargon, no mention of Claude. Answer with JSON only: {\"<entry id>\": \"<sentence>\", ...}."
)


def changes_key(changes: List[Dict[str, Any]]) -> str:
    """Which changes a sentence was written for: an entry that grew (a burst) needs a new one."""
    return hashlib.sha1("|".join(sorted(str(c.get("id")) for c in changes)).encode()).hexdigest()[:12]


def sentence(e: Dict[str, Any], most: int = 0) -> Tuple[str, str]:
    """The entry in one plain sentence (of about `most` characters at most; 0: human.SENTENCE_MAX), and who wrote it:
    ("Control now keeps data in SQLite", "") from the changes themselves, or an LLM's (its name as the second) once it
    has written one for exactly these changes."""
    from . import human
    most = most or human.SENTENCE_MAX
    w = e.get("words")
    if (isinstance(w, dict) and w.get("for") == changes_key(e.get("changes") or []) and isinstance(w.get("text"), str)
            and w["text"].strip()):
        return _cut(w["text"], most) or "", str(w.get("by") or "an LLM")
    return human.change_sentence(e.get("changes") or [], most), ""


def _words_state(root: Path) -> Dict[str, Any]:
    try:
        got = json.loads((git_dir(root) / WORDS_STATE).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_words_state(root: Path, st: Dict[str, Any]) -> None:
    path = git_dir(root) / WORDS_STATE
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def words_wanted(root: Path, entries: Optional[List[Dict[str, Any]]] = None,
                 now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The entries with no LLM sentence for their changes, not asked about lately, newest first."""
    now = time.time() if now is None else now
    asked = _words_state(root).get("asked") or {}
    out = []
    for e in (entries if entries is not None else load(root)):
        if not e.get("changes") or sentence(e)[1]:
            continue
        if now - float(asked.get(f"{e['id']}:{changes_key(e['changes'])}", 0)) < WORDS_RETRY:
            continue
        out.append(e)
    return out


def words_prompt(batch: List[Dict[str, Any]]) -> str:
    """What the LLM is sent: per entry, its structural changes (titles) and the draft. Never code, never a request."""
    from . import human
    items = []
    for e in batch:
        titles = [c["title"] for c in e["changes"][:25]]
        if len(e["changes"]) > 25:
            titles.append(f"and {len(e['changes']) - 25} smaller changes")
        items.append({"id": e["id"], "changes": titles, "draft": human.change_sentence(e["changes"])})
    return "Entries:\n" + json.dumps(items, ensure_ascii=False, indent=1)


def _answer(text: str) -> Dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        got = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def words(root: Path, ask: Optional[Callable[[str], Optional[Tuple[str, str]]]] = None,
          cfg: Optional[Dict[str, Any]] = None, most: int = WORDS_BATCH, now: Optional[float] = None) -> int:
    """Ask an LLM (narrate.complete: the person's own Claude Code, or a gateway) for better sentences for up to
    `most` entries, in one question; keep each answer beside its entry (a "words" line in the log). The sentences
    written. Only in the background (the job start_words() starts, or a command): never on a look's path."""
    from . import narrate
    root = Path(root)
    if narrate.off():
        return 0
    now = time.time() if now is None else now
    batch = words_wanted(root, now=now)[:most]
    if not batch:
        return 0
    with _LOCK:
        st = _words_state(root)
        asked = {k: t for k, t in (st.get("asked") or {}).items() if now - float(t) < WORDS_RETRY}
        for e in batch:
            asked[f"{e['id']}:{changes_key(e['changes'])}"] = now
        st.update({"asked": asked, "last": now})
        _save_words_state(root, st)
    if ask is None:
        cfg = cfg if cfg is not None else _cfg(root)
        ask = lambda user: narrate.complete(user, WORDS_SYSTEM, root, cfg, WORDS_TIMEOUT, limit=20000)  # noqa: E731
    try:
        got = ask(words_prompt(batch))
    except Exception:  # noqa: BLE001 - no answer: the deterministic sentences stay
        got = None
    if not got:
        return 0
    text, by = got
    answer = _answer(text or "")
    lines = []
    for e in batch:
        said = answer.get(e["id"])
        if not isinstance(said, str):
            continue
        said = " ".join(said.split()).strip()
        if not said:
            continue
        if len(said) > WORDS_MAX:
            said = said[:WORDS_MAX - 1].rstrip() + "…"
        lines.append({"id": e["id"], "words": {"text": said, "by": by, "for": changes_key(e["changes"]),
                                               "at": round(now, 3)}})
    _write(root, lines)
    return len(lines)


_WORDS_SEEN: Dict[str, Tuple[int, float]] = {}


def start_words(root: Path, cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Start the background job that asks for better sentences (`python -m system_diff.history words`), when some
    entry has none, no job ran in the last WORDS_GAP seconds and an LLM is configured. Never waits; never raises.
    Off with SYSTEM_DIFF_LLM=off."""
    try:
        from . import narrate
        root = Path(root)
        if narrate.off() or not enabled() or _in_test():
            return False
        now = time.time()
        if now - float(_words_state(root).get("last", 0)) < WORDS_GAP:
            return False
        try:
            mark = _log(root).stat().st_mtime_ns
        except OSError:
            return False
        seen = _WORDS_SEEN.get(str(root))
        if seen is not None and seen[0] == mark and now - seen[1] < WORDS_GAP:
            return False                               # the log has not moved since nothing was wanted
        _WORDS_SEEN[str(root)] = (mark, now)
        if not words_wanted(root):
            return False
        cfg = cfg if cfg is not None else _cfg(root)
        if not narrate.configured(root, cfg.get("narrate", {}).get("settings", [])):
            return False
        lock = git_dir(root) / "history-words.lock"
        if not _job_lock(lock, WORDS_STALE):
            return False
        try:
            _spawn(root, "words")
            return True
        except Exception:  # noqa: BLE001
            lock.unlink()
            return False
    except Exception:  # noqa: BLE001
        return False


def words_now(root: Path) -> int:
    """The job start_words() starts: a batch or two; the lock freed."""
    try:
        n = words(root)
        if n:
            n += words(root)
        return n
    finally:
        try:
            (git_dir(root) / "history-words.lock").unlink()
        except OSError:
            pass


# ── comparing two points in the history ───────────────────────────────────────

NOW = "now"


def compare_part(part: str) -> Optional[Tuple[str, str, str]]:
    """A History page's part that compares: "compare:<A>" (the list, A marked as where to compare from),
    "compare:<A>..<B>" (from A to B; B may be "now"), each optionally after "<block>#". (block, A, B or "")."""
    block, sep, rest = (part or "").partition("#")
    if not sep:
        block, rest = "", part or ""
    if not rest.startswith("compare:"):
        return None
    a, _, b = rest[len("compare:"):].partition("..")
    clean = lambda x: re.sub(r"[^A-Za-z0-9]", "", x)  # noqa: E731
    return block, clean(a), clean(b)


def compare_key(block: str, a: str, b: str = "") -> str:
    """The part that shows a comparison (or, without `b`, the list with `a` marked)."""
    rest = f"compare:{a}" + (f"..{b}" if b else "")
    return f"{block}#{rest}" if block else rest


def _order(e: Dict[str, Any]) -> tuple:
    return (float(e["at"]), e["id"])


def point_line(e: Optional[Dict[str, Any]], most: int = 80) -> str:
    """A point of the history in a few words: "now", or when and what was asked (or the commit)."""
    if e is None:
        return "now"
    if e.get("request"):
        return f"{when(e['at'])} · “{_cut(e['request'], most)}”"
    if e.get("source") == "commit":
        return f"{when(e['at'])} · commit {str(e.get('commit') or '')[:7]}" + (
            f": {_cut(e['message'], most)}" if e.get("message") else "")
    return f"{when(e['at'])} · an edit outside a request"


def compare(root: Path, a: str, b: str, cfg: Optional[Dict[str, Any]] = None,
            now: Optional[Dict[str, Any]] = None, wait: bool = False) -> Dict[str, Any]:
    """From the state right after entry `a` to the state right after entry `b` ("now": the system as it is, whose
    blueprint is `now`), whichever order they are given in: {"a", "b" (None: now), "between" (the entries after a up
    to b, oldest first), "comparison" (None until both states are at hand), "changes" (ranked), "reading" (a state is
    being read in the background), "gone" (a state no longer kept), "missing" (ids not in the history)}.
    Only kept blueprints are used (no git, no reading of code) unless `wait`; a commit's not yet kept is read in the
    background (read_later) and "reading" says so."""
    root = Path(root)
    cfg = cfg if cfg is not None else _cfg(root)
    entries = load(root)
    by = {e["id"]: e for e in entries}
    out: Dict[str, Any] = {"a": None, "b": None, "between": [], "comparison": None, "changes": [], "reading": False,
                           "gone": False, "missing": [x for x in (a, b) if x != NOW and x not in by]}
    if out["missing"] or not a or not b:
        return out
    A, B = (None if a == NOW else by[a]), (None if b == NOW else by[b])
    if A is None:
        A, B = B, None
    if A is None:
        return out                                   # now against now
    if B is not None and _order(A) > _order(B):
        A, B = B, A
    out["a"], out["b"] = A, B
    out["between"] = [e for e in sorted(entries, key=_order)
                      if _order(e) > _order(A) and (B is None or _order(e) <= _order(B))]
    if B is not None and B["id"] == A["id"]:
        return out
    later = sorted((e for e in entries if _order(e) > _order(A)), key=_order)

    def state(ref_first: Optional[str], ref_next: Optional[str]) -> Optional[Dict[str, Any]]:
        """A state's blueprint: the entry's head, else the base of the entry right after it (the same state)."""
        for ref in (ref_first, ref_next):
            if not ref:
                continue
            bp = state_blueprint(root, ref, cfg) if wait else kept_blueprint(root, ref, cfg)
            if bp is not None:
                return bp
        for ref in (ref_first, ref_next):
            if ref and not ref.startswith("state:") and ref != "empty":
                out["reading"] = read_later(root, ref) or (_states(root) / f"reading-{ref[:16]}.lock").exists()
                if out["reading"]:
                    return None
        out["gone"] = True
        return None

    start = state(A.get("head"), later[0].get("base") if later else None)
    if B is None:
        end = now
    else:
        after_b = [e for e in later if _order(e) > _order(B)]
        end = state(B.get("head"), after_b[0].get("base") if after_b else None)
    if start is None or end is None:
        if end is None and B is None and not out["reading"]:
            out["gone"] = True
        return out
    c = comparison(start, end, cfg, "after " + when(A["at"]), "now" if B is None else "after " + when(B["at"]))
    out["comparison"] = c
    out["changes"] = ranked(c)
    return out


def compare_text(root: Path, a: str, b: str, width: int = 100, now: Optional[Dict[str, Any]] = None) -> str:
    """A comparison as text (`system-diff history --compare A B`): from, to, the ranked changes, the requests between."""
    from . import canvas
    got = compare(root, a, b, now=now, wait=True)
    if got["missing"]:
        return f"Not in the history: {', '.join(got['missing'])}. `system-diff history --json` lists the ids.\n"
    if got["a"] is None:
        return "Nothing to compare: pick a change from the history (and another, or now).\n"
    out = [canvas.cut(f"From just after: {point_line(got['a'], width)}", width),
           canvas.cut(f"To {'now' if got['b'] is None else 'just after: ' + point_line(got['b'], width)}", width), ""]
    if got["comparison"] is None:
        out.append("The system at one of these points is no longer kept." if not got["reading"] else
                   "Reading the system at that point… try again in a moment.")
    elif not got["changes"]:
        out.append("No change to the system's structure between them.")
    else:
        from . import human
        out.append(canvas.cut(human.change_sentence(got["changes"], width), width))
        out.append("")
        out.append(f"What changed ({len(got['changes'])}, the most consequential first):")
        for c in got["changes"]:
            out.append(canvas.cut(f"  {c['sign']} {c['title']} — {c['why']}", width))
    if got["between"]:
        out += ["", f"What was asked in between ({len(got['between'])}, oldest first):"]
        for e in got["between"]:
            out.append(canvas.cut(f"  {when(e['at'])} · {asked_line(e)}", width))
            out.append(canvas.cut(f"    → {sentence(e, width - 7)[0]}", width))
    return "\n".join(out).rstrip() + "\n"


def _entry(eid: str, at: float, since: float, request: Optional[dict], source: str, commit: Optional[str],
           message: str, changes: List[dict], base: str, head: str, files: Optional[int]) -> Dict[str, Any]:
    text = _said((request or {}).get("text"))
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
            held = e.held
            start_words(self.root, held.cfg if held is not None else None)   # better sentences, in a job of its own
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
        out.append(canvas.cut(f"{e['id']} · {when(e['at'])} · {asked_line(e)}", width))
        said, by = sentence(e, width - 6)
        out.append(canvas.cut(f"  → {said}" + (" ✦" if by else ""), width))
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
    if any(sentence(e)[1] for e in entries):
        out.append("✦ a summary an LLM wrote from the structural changes listed under it")
    out.append("Compare two points: system-diff history --compare <id> <id|now>")
    return "\n".join(out).rstrip() + "\n"


if __name__ == "__main__":     # the background jobs: `python -m system_diff.history read <sha> <root>` / `words <root>`
    import sys
    _args = sys.argv[1:]
    if len(_args) == 3 and _args[0] == "read":
        sys.exit(0 if read_now(Path(_args[2]), _args[1]) else 1)
    if len(_args) == 2 and _args[0] == "words":
        sys.exit(0 if words_now(Path(_args[1])) else 1)
    sys.exit(2)
