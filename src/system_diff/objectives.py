"""What the person is optimising for: the things they keep asking Claude for while they iterate on a system ("make it
fast on large repos", "must work on Windows", "show it, don't act"), learned from their own requests by an LLM in the
background, kept in front of them and in front of Claude, checked against each change, and cleared by them when wrong.

API (for the integrator):

    objectives.current(root) -> List[dict]
        The active objectives, a fast file read (cached by mtime); [] when none. Each:
        {"id", "text", "evidence": [quotes], "count", "last": ISO time, "parts": [part ids], "check", "since": ISO}

    objectives.ensure(root, blueprint=None) -> bool
        Cheap enough for every prompt and every draw; never waits, never raises. Starts the background distillation
        when it is stale: requests newer than the last one read, at most every MIN_GAP seconds, at least MIN_REQUESTS
        requests to learn from, an LLM configured (narrate.py's gateways), not already running (a lock). `blueprint`
        (Comparison.after) gives the part ids an objective may name. True if a job was started.

    objectives.clear(root, id) -> bool / objectives.restore(root, id) -> bool
        The person says one is wrong (or takes that back). Only system-diff's own notes change, never the code.
        A cleared objective is not proposed again unless at least 3 requests made after the clear clearly ask for it.

    objectives.context(root) -> str
        At most CONTEXT_MAX characters for Claude's context at each prompt ("" when there are none):
        "What this person has been optimising for in this repository (learned from their requests; they can clear
        any): 1. ... 2. ..."

    objectives.touches(objectives, comparison_or_diff, blueprint=None, paths=()) -> {objective id: [reasons]}
        No LLM: an objective is touched when a change touches one of its parts, or when its words name a changed
        part's label or id, a changed endpoint, outside system or table, or one of `paths` (changed files, optional;
        a diff dict's own "paths" or "files" list is used too).

    objectives.record_check(root, id, agent, status, note="") -> dict
        An agent's finding: status "checking" | "holds" | "broken" | "unclear" (ValueError otherwise, or for an
        unknown id). Stored under "checks" (the latest per (id, agent) plus CHECK_HISTORY before it) and published on
        the bus (agents.publish, topic "objective:<id>", kind "check", from the agent).
    objectives.checks(root) -> {objective id: [the latest check of each agent, newest first]}
    objectives.for_brief(root) -> str
        A few lines to append to each agent's brief in a team plan ("" when there are no objectives).

    objectives.learn(root, blueprint=None, say=print) -> int
        The distillation now, in the foreground (`system-diff objectives learn`): reads, asks, merges, saves.

Storage: <git dir>/system-diff/objectives.json, written atomically, never committed, read again after restarts:

    {"version": 1, "objectives": [...], "cleared": [{"id", "text", "at", "was": <the objective>}],
     "checks": [{"id", "agent", "status", "note", "at"}],
     "asked_at": ISO, "from": <how many requests were read>, "seen": ISO of the newest request read, "model": "..."}

Where the requests come from: system-diff's activity log (the prompts its hook recorded) and, when present, Claude
Code's own transcripts of this project (~/.claude/projects/<the project path, every other character than a letter or
digit a ->/*.jsonl): the person's typed messages only, the newest SESSIONS sessions and PROMPTS prompts, each read from
its last TAIL_BYTES. Tool results, system reminders, slash-command echoes, task notices, pasted content and code blocks
are dropped. What is sent to the LLM: those requests (cut to PROMPT_MAX characters), the objectives already kept and
cleared, and the parts' ids and names. Never a file's contents.

Off with SYSTEM_DIFF_OBJECTIVES=0. Under pytest (PYTEST_CURRENT_TEST set) the background job never starts unless the
test sets SYSTEM_DIFF_OBJECTIVES=1 itself. Nothing happens, and nothing fails, when no LLM is configured.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import activity, narrate
from .sources import git_dir

VERSION = 1
FILE, LOCK, JOB = "objectives.json", "objectives.lock", "objectives-job.json"
MAX_OBJECTIVES = 7
MIN_REQUESTS = 3          # fewer requests than this: nothing to learn from yet
MIN_GAP = 600             # seconds between two distillations for one repository
LOCK_STALE = 900          # a lock older than this is a job that died
TIMEOUT = 180             # seconds the job waits for the model
SESSIONS = 8              # transcripts read, newest first
PROMPTS = 150             # requests sent, newest
TAIL_BYTES = 4 << 20      # of each transcript, only its end is read
PROMPT_MAX = 400
TEXT_MAX, QUOTE_MAX, CHECK_MAX, ID_MAX = 120, 140, 160, 32
PARTS_MAX = 5             # parts an objective names
AGAIN = 3                 # requests after a clear that bring a cleared objective back
CONTEXT_MAX = 600
STATUSES = ("checking", "holds", "broken", "unclear")
CHECK_HISTORY = 5         # checks kept per objective and agent
CHECKS_MAX = 300          # checks kept in all
NOTE_MAX = 200
CONTEXT_TAIL = " Report what you check about one with the check_objective tool."
CONTEXT_HEAD = ("What this person has been optimising for in this repository (learned from their requests; "
                "they can clear any):")

SYSTEM = (
    "You read the requests a person typed to an AI coding agent while building one software system, and name what "
    "they are optimising for: the qualities, constraints and principles they keep asking for again and again (for "
    "example \"fast on large repositories\", \"must work on Windows\", \"only shows, never acts for me\"), not single "
    "tasks or features asked once. Reply with one JSON object and nothing else, of this shape:\n"
    '{"objectives": [{"id": "<short-slug>", "text": "<one line in the person\'s own terms>", '
    '"evidence": ["<up to 3 short quotes, copied exactly from their requests>"], "count": <how many requests support '
    'it>, "last": "<ISO time of the latest such request>", "parts": ["<part id>"], "check": "<one line: how a change '
    'could break it>"}]}\n'
    "Rules. At most 7 objectives, the most often asked first; each supported by at least two requests. Quotes are "
    "copied word for word from the requests (a short phrase is enough). Keep the id of an objective already kept "
    "when it is the same objective. Do not propose an objective the person cleared unless at least 3 requests made "
    "after the time it was cleared clearly ask for it again. parts: only part ids given, and only when the objective "
    "is clearly about them; else []. Write in plain words the person would use."
)


# ---------------------------------------------------------------------------------------------------- switches

def enabled() -> bool:
    v = os.environ.get("SYSTEM_DIFF_OBJECTIVES", "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and v not in ("1", "true", "yes", "on"):
        return False                                  # a test turns it on itself, with the model mocked
    return True


def _gateway(root: Path, settings: Iterable[str] = ()) -> bool:
    return bool(narrate.claude_llm(root, list(settings)) or narrate.shell_llm())


# ---------------------------------------------------------------------------------------------------- time

def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unix(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


# ---------------------------------------------------------------------------------------------------- the requests

_DROP_BLOCKS = re.compile(
    r"<(system-reminder|pasted_content|task-notification|command-name|command-message|command-args|local-command-"
    r"stdout|local-command-stderr|local-command-caveat|bash-input|bash-stdout|bash-stderr|user-memory-input|"
    r"tool_use_error)\b[^>]*>.*?(?:</\1>|\Z)", re.S)
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
_NOISE = ("[Request interrupted", "Caveat:", "This session is being continued", "[Image")


def clean(text: Any) -> Optional[str]:
    """A request as the person typed it, without what was pasted or attached to it; None for what they did not type."""
    if not isinstance(text, str) or activity.is_system(text):
        return None
    if text.lstrip().startswith(_NOISE):
        return None
    text = _FENCE.sub(" ", _DROP_BLOCKS.sub(" ", text))
    keep = []
    for line in text.splitlines():
        if line.startswith(("    ", "\t", "$ ", ">>> ", "❯", "Traceback", "  File \"")):
            continue                                  # code, a log, a terminal pasted in
        keep.append(line)
    text = " ".join(" ".join(keep).split())
    if len(text) < 3:
        return None
    return text if len(text) <= PROMPT_MAX else text[:PROMPT_MAX - 1].rstrip() + "…"


def transcripts_dir(root: Path) -> Path:
    """Where Claude Code keeps this project's transcripts."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return base / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(root).resolve()))


def _sessions(root: Path) -> List[Path]:
    try:
        files = [(f.stat().st_mtime, f) for f in transcripts_dir(root).glob("*.jsonl")]
    except OSError:
        return []
    return [f for _m, f in sorted(files, reverse=True)[:SESSIONS]]


def _tail_lines(f: Path) -> List[str]:
    try:
        with open(f, "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - TAIL_BYTES))
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > TAIL_BYTES else lines   # the first is cut short


def from_transcripts(root: Path) -> List[Dict[str, Any]]:
    """The person's typed messages in Claude Code's transcripts of this project: [{"at", "text"}], oldest first."""
    out = []
    for f in _sessions(root):
        for line in _tail_lines(f):
            if '"user"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain") \
                    or e.get("isCompactSummary") or e.get("toolUseResult") is not None:
                continue
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list):
                if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                content = "\n".join(b.get("text", "") for b in content
                                    if isinstance(b, dict) and b.get("type") == "text")
            text = clean(content)
            at = _unix(e.get("timestamp"))
            if text and at:
                out.append({"at": at, "text": text})
    return sorted(out, key=lambda r: r["at"])


def from_log(root: Path) -> List[Dict[str, Any]]:
    """The prompts system-diff's hook recorded: [{"at", "text"}], oldest first."""
    out = []
    for e in activity.events(root, limit=10000):
        if e.get("event") == activity.PROMPT:
            text = clean(e.get("text", ""))
            if text:
                out.append({"at": float(e["at"]), "text": text})
    return out


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())[:120]


def requests(root: Path) -> List[Dict[str, Any]]:
    """Every request read, oldest first, each once (the log and the transcripts hold the same prompts), the newest
    PROMPTS of them."""
    seen: Dict[str, Dict[str, Any]] = {}
    for r in sorted(from_transcripts(root) + from_log(root), key=lambda r: r["at"]):
        k = _key(r["text"])[:80]
        if k and k not in seen:
            seen[k] = r
    return sorted(seen.values(), key=lambda r: r["at"])[-PROMPTS:]


# ---------------------------------------------------------------------------------------------------- the prompt

def parts_of(blueprint: Optional[dict]) -> Dict[str, str]:
    """The part ids an objective may name, with their names."""
    comps = (blueprint or {}).get("components") or {}
    return {cid: str(c.get("label", cid)) for cid, c in sorted(comps.items())
            if isinstance(c, dict) and not c.get("quiet")}


def prompt(reqs: List[Dict[str, Any]], kept: List[dict], cleared: List[dict], parts: Dict[str, str]) -> str:
    lines = ["The person's requests, oldest first (time, text):"]
    lines += [f"- {_iso(r['at'])} {r['text']}" for r in reqs]
    lines.append("\nObjectives already kept (keep their ids when still right):")
    lines += [f"- {o['id']}: {o['text']}" for o in kept] or ["- none"]
    lines.append("\nObjectives the person cleared as wrong (with when):")
    lines += [f"- {c['id']}: {c['text']} (cleared {c['at']})" for c in cleared] or ["- none"]
    lines.append("\nThe system's parts (id: name):")
    lines += [f"- {cid}: {name}" for cid, name in list(parts.items())[:150]] or ["- none known"]
    lines.append(f"\nNow: {_iso(time.time())}. Reply with the JSON object only.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------- the answer

def _json(text: str) -> Any:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    return json.loads(text[start:end + 1])


def _line(v: Any, most: int) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    if not v:
        return None
    return v if len(v) <= most else v[:most - 1].rstrip() + "…"


def slug(v: Any) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(v or "").lower())).strip("-")[:ID_MAX].strip("-")


def _matches(quote: str, reqs: List[Dict[str, Any]]) -> List[float]:
    """The times of the requests a quote is copied from."""
    q = _key(quote)
    if len(q) < 4:
        return []
    return [r["at"] for r in reqs if q in re.sub(r"[^a-z0-9]+", "", r["text"].lower())]


def validate(answer: Any, reqs: List[Dict[str, Any]], parts: Dict[str, str]) -> List[Dict[str, Any]]:
    """The model's objectives that hold up, at most MAX_OBJECTIVES: a text, at least one quote found in a request, at
    least two supporting requests; unknown parts dropped, lengths capped, `last` the newest quoted request. Each keeps
    "_times", the times of the requests its quotes come from (for the cleared rule; not stored)."""
    if isinstance(answer, str):
        try:
            answer = _json(answer)
        except ValueError:
            return []
    items = answer.get("objectives") if isinstance(answer, dict) else None
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for o in items:
        if not isinstance(o, dict) or len(out) >= MAX_OBJECTIVES:
            continue
        text = _line(o.get("text"), TEXT_MAX)
        oid = slug(o.get("id")) or slug(text)
        if not text or not oid or any(x["id"] == oid for x in out):
            continue
        evidence, times = [], set()
        for q in o.get("evidence") or []:
            q = _line(q, QUOTE_MAX)
            hit = _matches(q, reqs) if q else []
            if hit and q not in evidence and len(evidence) < 3:
                evidence.append(q)
                times.update(hit)
        if not evidence:
            continue                                  # nothing the person said: not believed
        count = o.get("count")
        count = int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else 0
        count = min(max(count, len(times)), len(reqs))
        if count < 2:
            continue
        last = max(times) if times else _unix(o.get("last"))
        out.append({"id": oid, "text": text, "evidence": evidence, "count": count,
                    "last": _iso(min(last or time.time(), time.time())),
                    "parts": list(dict.fromkeys(p for p in (o.get("parts") or [])
                                                if isinstance(p, str) and p in parts))[:PARTS_MAX],
                    "check": _line(o.get("check"), CHECK_MAX) or "", "_times": sorted(times)})
    return out


# ---------------------------------------------------------------------------------------------------- merging

_STOP = set("""a an the and or of to in on for with without from by at as is are be it its this that these those not
no do does don't dont must should always never keep make makes made so very more less than then when what which who
how it's i we you they he she our your their my me us them all any each every one ones also just only still even
will would can could please want need like use using via into onto over under about up out off""".split())


def words(text: str) -> set:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) >= 3 and w not in _STOP:
            out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _similar(a: str, b: str) -> bool:
    x, y = words(a), words(b)
    return bool(x and y) and len(x & y) / len(x | y) >= 0.5


def merge(store: Dict[str, Any], proposed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """`store` with the model's objectives taken in: the same objective keeps its id and text and gets the new
    evidence, count and time; a new one is added; one the person cleared comes back only when AGAIN requests made
    after the clear support it (then as new, and no longer cleared). At most MAX_OBJECTIVES, most asked first."""
    active: List[dict] = [dict(o) for o in store.get("objectives", [])]
    cleared: List[dict] = [dict(c) for c in store.get("cleared", [])]
    now = _iso(time.time())
    for p in proposed:
        times = p.get("_times", [])
        p = {k: v for k, v in p.items() if not k.startswith("_")}
        same = next((o for o in active if o["id"] == p["id"]), None) \
            or next((o for o in active if _similar(o["text"], p["text"])), None)
        if same:
            same["evidence"] = list(dict.fromkeys(p["evidence"] + same.get("evidence", [])))[:3]
            same["count"] = max(int(same.get("count", 0)), p["count"])
            same["last"] = max(same.get("last", ""), p["last"])
            same["parts"] = p["parts"] or same.get("parts", [])
            same["check"] = p["check"] or same.get("check", "")
            continue
        gone = next((c for c in cleared if c["id"] == p["id"] or _similar(c["text"], p["text"])), None)
        if gone:
            after = _unix(gone.get("at")) or 0.0
            if sum(1 for t in times if t > after) < AGAIN:
                continue                                  # the person said it was wrong; not asked for again enough
            cleared.remove(gone)
        if any(o["id"] == p["id"] for o in active):
            continue
        active.append({**p, "since": now})
    active.sort(key=lambda o: (-int(o.get("count", 0)), -(_unix(o.get("last")) or 0.0), o["id"]))
    return {**store, "objectives": active[:MAX_OBJECTIVES], "cleared": cleared}


# ---------------------------------------------------------------------------------------------------- storage

_READ: Dict[str, Tuple[tuple, Dict[str, Any]]] = {}
_DIRS: Dict[str, Path] = {}


def _dir(root: Path) -> Path:
    d = _DIRS.get(str(root))
    if d is None or not d.is_dir():
        d = _DIRS[str(root)] = git_dir(Path(root))
    return d


def _empty() -> Dict[str, Any]:
    return {"version": VERSION, "objectives": [], "cleared": [], "checks": [], "asked_at": "", "from": 0, "seen": ""}


def load(root: Path) -> Dict[str, Any]:
    """The whole store; an empty one when there is none or it is not readable."""
    f = _dir(root) / FILE
    try:
        st = f.stat()
        mtime = (st.st_mtime_ns, st.st_size, st.st_ino)   # every save is a new file (os.replace): a new inode
    except OSError:
        return _empty()
    hit = _READ.get(str(f))
    if hit and hit[0] == mtime:
        return json.loads(json.dumps(hit[1]))
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    data = {**_empty(), **data}
    data["objectives"] = [o for o in data["objectives"] if isinstance(o, dict) and o.get("id") and o.get("text")]
    data["cleared"] = [c for c in data["cleared"] if isinstance(c, dict) and c.get("id")]
    _READ[str(f)] = (mtime, data)
    return json.loads(json.dumps(data))


def save(root: Path, data: Dict[str, Any]) -> None:
    f = _dir(root) / FILE
    tmp = f.with_name(f".{FILE}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({**data, "version": VERSION}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, f)
    _READ.pop(str(f), None)


def current(root: Path) -> List[Dict[str, Any]]:
    try:
        return load(root)["objectives"]
    except Exception:  # noqa: BLE001 - shown beside the work: never stop it
        return []


def clear(root: Path, oid: str) -> bool:
    data = load(root)
    o = next((o for o in data["objectives"] if o["id"] == oid), None)
    if not o:
        return False
    data["objectives"].remove(o)
    data["cleared"] = [c for c in data["cleared"] if c["id"] != oid]
    data["cleared"].append({"id": oid, "text": o["text"], "at": _iso(time.time()), "was": o})
    save(root, data)
    return True


def restore(root: Path, oid: str) -> bool:
    data = load(root)
    c = next((c for c in data["cleared"] if c["id"] == oid), None)
    if not c:
        return False
    data["cleared"].remove(c)
    if not any(o["id"] == oid for o in data["objectives"]):
        was = c.get("was") if isinstance(c.get("was"), dict) else {}
        data["objectives"].append({"evidence": [], "count": 0, "last": "", "parts": [], "check": "", **was,
                                   "id": oid, "text": c.get("text") or was.get("text", oid)})
    save(root, data)
    return True


# ---------------------------------------------------------------------------------------------------- for Claude

def context(root: Path) -> str:
    objs = current(root)
    if not objs:
        return ""
    most = CONTEXT_MAX - len(CONTEXT_TAIL)
    out = CONTEXT_HEAD
    for i, o in enumerate(objs, 1):
        item = f" {i}. {o['text'].rstrip('.')}."
        if len(out) + len(item) > most:
            room = most - len(out) - len(f" {i}. ") - 2
            if room >= 20:
                out += f" {i}. {o['text'][:room].rstrip()}…"
            break
        out += item
    return out + CONTEXT_TAIL


def for_brief(root: Path) -> str:
    """A few lines to append to each agent's brief in a team plan; "" when there are no objectives."""
    objs = current(root)
    if not objs:
        return ""
    lines = ["Keep these objectives the person has been optimising for; when you evaluate or test one (tests run, "
             "a measurement, a review), report it with the check_objective tool (holds, broken or unclear):"]
    for o in objs:
        lines.append(f"- {o['id']}: {o['text']}" + (f" (breaks if: {o['check']})" if o.get("check") else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------- checks

def record_check(root: Path, oid: str, agent: str, status: str, note: str = "") -> Dict[str, Any]:
    """An agent says what it found about an objective: status one of STATUSES. Kept under "checks" in the store
    (the latest per objective and agent, plus a short history) and published on the bus, topic "objective:<id>".
    ValueError for an unknown objective or status."""
    root = Path(root)
    status = str(status or "").strip().lower()
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    data = load(root)
    o = next((o for o in data["objectives"] if o["id"] == oid), None)
    if not o:
        raise ValueError(f"no objective {oid!r}; the objectives: {', '.join(x['id'] for x in data['objectives'])}")
    agent = _line(agent, 60) or "main"
    entry = {"id": oid, "agent": agent, "status": status, "note": _line(note, NOTE_MAX) or "",
             "at": _iso(time.time())}
    history = [c for c in data.get("checks") or [] if isinstance(c, dict)] + [entry]
    kept: List[dict] = []
    per: Dict[Tuple[str, str], int] = {}
    for c in reversed(history):                       # newest first: the latest per (id, agent) and a few before
        k = (c.get("id"), c.get("agent"))
        per[k] = per.get(k, 0) + 1
        if per[k] <= CHECK_HISTORY and len(kept) < CHECKS_MAX:
            kept.append(c)
    data["checks"] = list(reversed(kept))
    save(root, data)
    try:
        from . import agents
        agents.publish(root, f"objective:{oid}", f"{status}: {o['text']}" + (f" ({entry['note']})"
                                                                              if entry["note"] else ""),
                       sender=agent, kind="check")
    except Exception:  # noqa: BLE001 - the bus is a convenience; the check is kept
        pass
    return entry


def checks(root: Path) -> Dict[str, List[Dict[str, Any]]]:
    """{objective id: the latest check of each agent, newest first} for the active objectives."""
    try:
        data = load(root)
    except Exception:  # noqa: BLE001
        return {}
    active = {o["id"] for o in data["objectives"]}
    out: Dict[str, Dict[str, dict]] = {}
    for c in data.get("checks") or []:
        if isinstance(c, dict) and c.get("id") in active:
            out.setdefault(c["id"], {})[c.get("agent", "")] = c      # later ones win
    return {oid: sorted(by.values(), key=lambda c: c.get("at", ""), reverse=True) for oid, by in out.items()}


# ---------------------------------------------------------------------------------------------------- changes

def touches(objs: List[Dict[str, Any]], comparison_or_diff: Any, blueprint: Optional[dict] = None,
            paths: Iterable[str] = ()) -> Dict[str, List[str]]:
    """{objective id: [reasons]} for the objectives a change touches (see the top of this module). Cheap, no LLM."""
    d = getattr(comparison_or_diff, "diff", comparison_or_diff) or {}
    after = blueprint or getattr(comparison_or_diff, "after", None) or {}
    before = getattr(comparison_or_diff, "before", None) or {}
    comps = {**(before.get("components") or {}), **(after.get("components") or {})}
    routes = {**(before.get("routes") or {}), **(after.get("routes") or {})}

    def name(cid: str) -> str:
        c = comps.get(cid)
        return str(c.get("label", cid)) if isinstance(c, dict) else cid

    parts: Dict[str, str] = {}                        # part id -> what the change did to it
    things: List[str] = []                            # other changed names: endpoints, outside systems, tables
    for c in d.get("changes", []) if isinstance(d, dict) else []:
        entity, cid, label = c.get("entity"), str(c.get("id", "")), str(c.get("label", ""))
        verb = {"add": "adds", "remove": "removes"}.get(c.get("op"), "changes")
        if entity in ("component", "settings"):
            parts.setdefault(cid, f"{verb} {name(cid)}")
        elif entity == "route":
            owner = routes.get(cid)
            if owner:
                parts.setdefault(owner, f"{verb} the endpoint {cid} of {name(owner)}")
            things.append(cid)
        elif entity in ("flow", "external") and ">" in cid:
            src, dst = cid.split(">", 1)
            parts.setdefault(src, f"{verb} the connection {label}")
            if entity == "flow" and dst in comps:
                parts.setdefault(dst, f"{verb} the connection {label}")
            things.append(label)
        else:
            things.append(label or cid)
    changed_paths = list(paths) + [p for k in ("paths", "files") for p in (d.get(k) or [] if isinstance(d, dict)
                                                                          else []) if isinstance(p, str)]
    out: Dict[str, List[str]] = {}
    for o in objs:
        why: List[str] = []
        mine = [p for p in o.get("parts", []) if p in parts]
        why += [f"{parts[p]}, which it is about" for p in mine]
        ws = words(o.get("text", ""))

        def hits(target: str) -> List[str]:
            tw = words(target.replace("/", " ").replace("_", " ").replace(".", " "))
            return sorted(w for w in ws if w in tw or any(len(w) >= 5 and t.startswith(w) for t in tw))
        for p, what in parts.items():
            if p in mine:
                continue
            h = hits(name(p) + " " + p)
            if h:
                why.append(f"{what} (\"{h[0]}\")")
        for t in things:
            h = hits(t)
            if h:
                why.append(f"changes {t} (\"{h[0]}\")")
        for f in changed_paths:
            h = hits(f)
            if h:
                why.append(f"changes {f} (\"{h[0]}\")")
        if why:
            out[o["id"]] = list(dict.fromkeys(why))[:4]
    return out


# ---------------------------------------------------------------------------------------------------- the job

def _lock(d: Path) -> bool:
    f = d / LOCK
    for _ in range(2):
        try:
            fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - f.stat().st_mtime < LOCK_STALE:
                    return False
                f.unlink()                                # a job that died holding it
            except OSError:
                return False
        except OSError:
            return False
    return False


def _unlock(d: Path) -> None:
    try:
        (d / LOCK).unlink()
    except OSError:
        pass


def _fresh(root: Path, data: Dict[str, Any]) -> bool:
    """Cheaply: is there a request newer than the last one read, and enough to learn from? (The job reads them all.)"""
    seen = _unix(data.get("seen")) or 0.0
    logged = [e["at"] for e in activity.events(root, limit=400)
              if e.get("event") == activity.PROMPT and not activity.is_system(e.get("text", ""))]
    sessions = _sessions(root)
    newer = any(t > seen for t in logged) or any(f.stat().st_mtime > seen for f in sessions if f.exists())
    enough = int(data.get("from") or 0) + len(logged) >= MIN_REQUESTS or bool(sessions)
    return newer and enough


def ensure(root: Path, blueprint: Optional[dict] = None, settings: Iterable[str] = ()) -> bool:
    try:
        if not enabled():
            return False
        root = Path(root)
        d = _dir(root)
        data = load(root)
        if time.time() - (_unix(data.get("asked_at")) or 0.0) < MIN_GAP or (d / LOCK).exists() \
                and time.time() - (d / LOCK).stat().st_mtime < LOCK_STALE:
            return False
        if not _gateway(root, settings) or not _fresh(root, data):
            return False
        if not _lock(d):
            return False
        try:
            data = load(root)                             # again, under the lock
            if time.time() - (_unix(data.get("asked_at")) or 0.0) < MIN_GAP:
                _unlock(d)
                return False
            data["asked_at"] = _iso(time.time())
            save(root, data)
            job = d / JOB
            job.write_text(json.dumps({"root": str(root), "parts": parts_of(blueprint),
                                       "settings": list(settings)}), encoding="utf-8")
            _spawn(root, job)
            return True
        except Exception:  # noqa: BLE001
            _unlock(d)
            return False
    except Exception:  # noqa: BLE001 - runs at each prompt: never stop it
        return False


def _spawn(root: Path, job: Path) -> None:
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                          "cwd": str(root), "close_fds": True}
    if os.name == "nt":
        # a hidden console, not none: what it runs (git) inherits it, instead of opening a window each time
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "system_diff.objectives", str(job)], **kw)


def distill(root: Path, parts: Dict[str, str], settings: Iterable[str] = (),
            say: Callable[[str], None] = lambda s: None, force: bool = False) -> int:
    """Read the requests, ask the model, merge and save. 0 when it learned, 1 when it did not (nothing new, too few
    requests, no model, an answer that did not hold up)."""
    reqs = requests(root)
    data = load(root)
    newest = max((r["at"] for r in reqs), default=0.0)
    say(f"read {len(reqs)} requests (system-diff's log and Claude Code's transcripts of this project)")
    if len(reqs) < MIN_REQUESTS:
        say(f"fewer than {MIN_REQUESTS} requests: nothing to learn from yet")
        return 1
    if not force and newest <= (_unix(data.get("seen")) or 0.0):
        say("no request since the last time: nothing new to learn")
        return 1
    say("asking the LLM what they keep asking for…")
    got = narrate.complete(prompt(reqs, data["objectives"], data["cleared"], parts), SYSTEM, root,
                           {"narrate": {"settings": list(settings)}}, TIMEOUT, limit=20000)
    data = load(root)                                     # again: the person may have cleared one meanwhile
    data.update({"asked_at": _iso(time.time()), "from": len(reqs)})
    if not got:
        say("no LLM answered from here (see `system-diff doctor`)")
        save(root, data)
        return 1
    found = validate(got[0], reqs, parts)
    data = merge(data, found)
    data.update({"seen": _iso(newest), "model": got[1]})
    save(root, data)
    say(f"{got[1]} found {len(found)} objectives; {len(data['objectives'])} kept")
    return 0 if found else 1


def learn(root: Path, blueprint: Optional[dict] = None, say: Callable[[str], None] = print) -> int:
    """The distillation now, in the foreground, saying what it does."""
    root = Path(root)
    from .config import load as load_cfg
    try:
        settings = load_cfg(root=root).get("narrate", {}).get("settings", [])
    except Exception:  # noqa: BLE001
        settings = []
    if not _gateway(root, settings):
        say("no LLM is configured (Claude Code's gateway or SYSTEM_DIFF_LLM_*): nothing to learn with")
        return 1
    if blueprint is None:
        try:
            from .compare import compare
            blueprint = compare(root, "HEAD", ".").after
        except Exception:  # noqa: BLE001 - the parts only help
            blueprint = None
    d = _dir(root)
    if not _lock(d):
        say("a distillation is already running; try again in a few minutes")
        return 1
    try:
        return distill(root, parts_of(blueprint), settings, say, force=True)
    finally:
        _unlock(d)


def run(job: Path) -> int:
    """The background job: always frees the lock."""
    d = job.parent
    try:
        spec = json.loads(job.read_text(encoding="utf-8"))
        return distill(Path(spec["root"]), spec.get("parts") or {}, spec.get("settings") or [])
    except Exception:  # noqa: BLE001
        return 1
    finally:
        try:
            job.unlink()
        except OSError:
            pass
        _unlock(d)


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1])) if len(sys.argv) > 1 else 2)
