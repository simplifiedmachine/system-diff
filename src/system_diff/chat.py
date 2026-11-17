"""The web page's conversation with Claude: what was asked (from the page's Send to Claude box, or typed in the Claude
Code chat), where each question is (queued, delivered to the session, Claude answering, answered), Claude's answers,
what Claude points at in the system diagram (the point_to tool) and what it found while answering (the note tool).

Everything lives under <git dir>/system-diff/, private and never committed:

- outbox.jsonl     typed on the page, not yet taken by the pane (watch.outbox_put; pane.take_outbox takes it)
- delivered.jsonl  taken by the pane, to be submitted as the person's prompt: {"id", "text", "at"}
- asking.json      the prompt each session is answering now: {session: {"key", "id", "prompt", "about", "at", "web"}}
- answers.jsonl    the turn's final assistant text, with the prompt it answers: {"key", "id", "prompt", "about", "at",
                   "answered", "text", "session", "web"}
- pointing.json    what Claude points at now: {"at", "parts", "note", "question"}
- notes.json       findings attached to blocks: {"items": [{"nid", "part", "id", "text", "at", "question"}]}

Stdlib only, and cheap: the hooks write here (no git, no engine needed) and the engine reads it for the page.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DELIVERED = "delivered.jsonl"
ASKING = "asking.json"
ANSWERS = "answers.jsonl"
POINTING = "pointing.json"
NOTES = "notes.json"
OUTBOX = "outbox.jsonl"

ANSWER_MAX = 20_000          # characters of one answer kept
ANSWERS_KEEP = 40            # answers kept (the file is cut back to these)
ANSWERS_BYTES = 512 * 1024   # the file is cut back when it grows past this
DELIVERED_KEEP = 60
NOTE_MAX = 300               # characters of one note
NOTES_KEEP = 200             # notes kept in all
NOTES_SHOWN = 3              # notes shown per block, newest first
POINT_MOST = 12              # parts one point_to names at most
ITEMS_SHOWN = 30             # questions the panel lists
ANSWERING_FOR = 30 * 60      # a question asked longer ago than this with no answer is not "answering" any more
TRANSCRIPT_TAIL = 2 * 1024 * 1024

ABOUT = re.compile(r"^\[About: ([^\]\n]{1,300})\]\s*\n?")
WEB_HINT = ("This question came from the system-diff web page; name parts by their names and use point_to to show "
            "them.")


def _dir(root: Path) -> Path:
    from .sources import git_dir
    return git_dir(root)


def _replace(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _lines(path: Path, most: int = 0) -> List[Dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in raw[-most:] if most else raw:
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            out.append(got)
    return out


def _json(path: Path, default: Any) -> Any:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return got if isinstance(got, type(default)) else default


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def split_about(text: str) -> tuple:
    """("Service Layer › chat.py", the question) from a prompt sent with what the person was looking at."""
    m = ABOUT.match(text or "")
    return (m.group(1).strip(), (text or "")[m.end():].strip()) if m else ("", (text or "").strip())


def with_about(about: str, text: str) -> str:
    """The text sent to Claude: the question, after a short line naming what the person is looking at."""
    about = " ".join((about or "").replace("]", ")").split())[:200]
    return f"[About: {about}]\n{text}" if about else text


# ── the way of a question: queued → delivered → answering → answered ─────────────────────────────────────────────

def delivered(root: Path, items: Iterable[Dict[str, Any]]) -> None:
    """The pane took these from the outbox, to submit them as the person's prompts. Never raises."""
    items = [i for i in items if isinstance(i, dict) and i.get("text")]
    if not items:
        return
    try:
        f = _dir(root) / DELIVERED
        now = round(time.time(), 3)
        with open(f, "a", encoding="utf-8", newline="\n") as out:
            for i in items:
                out.write(json.dumps({"id": str(i.get("id") or ""), "text": i["text"], "at": now},
                                     ensure_ascii=False) + "\n")
        if f.stat().st_size > 256 * 1024 or len(_lines(f)) > DELIVERED_KEEP * 2:
            keep = _lines(f, DELIVERED_KEEP)
            _replace(f, "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in keep))
    except Exception:  # noqa: BLE001 - a record only
        pass


def _from_page(root: Path, prompt: str) -> Optional[Dict[str, Any]]:
    """The item the web page sent that this prompt is, if any: the same text (spaces aside), delivered lately."""
    said = _norm(prompt)
    if not said:
        return None
    for d in reversed(_lines(_dir(root) / DELIVERED, DELIVERED_KEEP)):
        text = _norm(d.get("text") or "")
        if text and (text == said or (len(text) > 40 and said.startswith(text[:200]))):
            return d
    return None


def asked(root: Path, prompt: str, session: str = "", at: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """A prompt was submitted (UserPromptSubmit): keep it as what this session is answering. Returns the record; its
    "web" says whether it came from the web page. Never raises."""
    try:
        page = _from_page(root, prompt)
        about, question = split_about(prompt)
        at = round(at if at is not None else time.time(), 3)
        rec = {"key": (page or {}).get("id") or f"p:{at}", "id": (page or {}).get("id") or "",
               "prompt": question[:2000], "about": about, "at": at, "web": bool(page) or bool(about),
               "session": session or ""}
        f = _dir(root) / ASKING
        held = _json(f, {})
        held[session or "_"] = rec
        if len(held) > 10:
            held = dict(sorted(held.items(), key=lambda kv: (kv[1] or {}).get("at") or 0)[-10:])
        _replace(f, json.dumps(held, ensure_ascii=False))
        return rec
    except Exception:  # noqa: BLE001 - a hook never fails a turn
        return None


def asking(root: Path, session: str = "") -> Optional[Dict[str, Any]]:
    """What a session (else the latest one) is answering now."""
    held = _json(_dir(root) / ASKING, {})
    if session and isinstance(held.get(session), dict):
        return held[session]
    recs = [v for v in held.values() if isinstance(v, dict)]
    return max(recs, key=lambda r: r.get("at") or 0) if recs else None


def _texts(content: Any) -> List[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return []


def _is_prompt(content: Any) -> bool:
    """A user line the person typed (not a tool's result handed back)."""
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        kinds = {b.get("type") for b in content if isinstance(b, dict)}
        return "tool_result" not in kinds and "text" in kinds
    return False


def transcript_answer(path: str) -> str:
    """The turn's final assistant text from a Claude Code transcript (JSONL): the text of the assistant's lines after
    the last prompt, from the last tool result on (the reply the person reads); "" if none. Reads the tail only."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TRANSCRIPT_TAIL))
            raw = f.read().decode("utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return ""
    turn: List[tuple] = []   # (kind, texts) since the last prompt
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("isSidechain") or d.get("type") not in ("user", "assistant"):
            continue
        content = (d.get("message") or {}).get("content")
        if d["type"] == "user":
            if _is_prompt(content) and not d.get("isMeta"):
                turn = []
            else:
                turn.append(("tool", []))
        else:
            turn.append(("said", [t for t in _texts(content) if t.strip()]))
    cut = max((i for i, (kind, _t) in enumerate(turn) if kind == "tool"), default=-1)
    last = [t for kind, ts in turn[cut + 1:] if kind == "said" for t in ts]
    if not last:   # nothing after the last tool result: the last thing said at all
        said = [t for k, ts in turn if k == "said" for t in ts]
        last = said[-1:]
    return "\n\n".join(t.strip() for t in last).strip()


def answered(root: Path, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The turn ended (Stop): keep its final assistant text with the prompt it answers. From the payload's
    last_assistant_message, else the transcript. A later Stop of the same prompt replaces the answer. Never raises."""
    try:
        text = payload.get("last_assistant_message")
        if not isinstance(text, str) or not text.strip():
            text = transcript_answer(payload.get("transcript_path") or "")
        if not text or not text.strip():
            return None
        session = payload.get("session_id") or ""
        q = asking(root, session) or {}
        rec = {"key": q.get("key") or f"p:{round(time.time(), 3)}", "id": q.get("id") or "",
               "prompt": q.get("prompt") or "", "about": q.get("about") or "", "at": q.get("at") or 0,
               "answered": round(time.time(), 3), "text": text.strip()[:ANSWER_MAX], "session": session,
               "web": bool(q.get("web"))}
        f = _dir(root) / ANSWERS
        kept = [a for a in _lines(f, ANSWERS_KEEP * 2) if a.get("key") != rec["key"]][-(ANSWERS_KEEP - 1):]
        kept.append(rec)
        body = "".join(json.dumps(a, ensure_ascii=False) + "\n" for a in kept)
        while len(body.encode("utf-8")) > ANSWERS_BYTES and len(kept) > 1:
            kept = kept[1:]
            body = "".join(json.dumps(a, ensure_ascii=False) + "\n" for a in kept)
        _replace(f, body)
        return rec
    except Exception:  # noqa: BLE001 - a hook never fails a turn
        return None


def answers(root: Path) -> List[Dict[str, Any]]:
    return _lines(_dir(root) / ANSWERS, ANSWERS_KEEP)


# ── pointing and notes (the MCP tools) ───────────────────────────────────────────────────────────────────────────

def point(root: Path, parts: List[str], note: str = "") -> Dict[str, Any]:
    """Claude points at these parts (names, ids or file paths): the web page lights them now."""
    from .extract import redact
    clean = [str(p).strip()[:200] for p in parts if isinstance(p, str) and p.strip()][:POINT_MOST]
    q = asking(root) or {}
    rec = {"at": round(time.time(), 3), "parts": clean, "note": redact(str(note or ""))[:NOTE_MAX],
           "question": q.get("key") or ""}
    _replace(_dir(root) / POINTING, json.dumps(rec, ensure_ascii=False))
    return rec


def pointing(root: Path) -> Optional[Dict[str, Any]]:
    got = _json(_dir(root) / POINTING, {})
    return got if got.get("parts") else None


def _redact_text(text: str) -> str:
    """A note with what could be a secret masked: each word, and each key=value, through extract.redact."""
    from .extract import redact
    out = []
    for word in re.split(r"(\s+)", text):
        if not word or word.isspace():
            out.append(word)
            continue
        lead = re.match(r"^[\"'`(\[]*", word).group(0)
        tail = re.search(r"[\"'`)\].,;:!?]*$", word[len(lead):]).group(0)
        core = word[len(lead):len(word) - len(tail)] if tail else word[len(lead):]
        out.append(lead + (redact(core) if core else "") + tail)
    return "".join(out)


def add_note(root: Path, part: str, text: str) -> Dict[str, Any]:
    """A short finding about a block, kept with the time and the question it came from. Secrets are masked."""
    part = str(part or "").strip()[:200]
    text = _redact_text(" ".join(str(text or "").split()))
    if not part or not text:
        raise ValueError("a note needs a part and a text")
    if len(text) > NOTE_MAX:
        text = text[:NOTE_MAX - 1].rstrip() + "…"
    q = asking(root) or {}
    rec = {"nid": uuid.uuid4().hex[:12], "part": part, "id": "", "text": text, "at": round(time.time(), 3),
           "question": (q.get("prompt") or "")[:200]}
    f = _dir(root) / NOTES
    held = _json(f, {})
    items = [i for i in held.get("items") or [] if isinstance(i, dict)][-(NOTES_KEEP - 1):]
    items.append(rec)
    _replace(f, json.dumps({"items": items}, ensure_ascii=False))
    return rec


def notes(root: Path) -> List[Dict[str, Any]]:
    return [i for i in _json(_dir(root) / NOTES, {}).get("items") or [] if isinstance(i, dict) and i.get("text")]


def clear_note(root: Path, nid: str) -> bool:
    f = _dir(root) / NOTES
    items = notes(root)
    kept = [i for i in items if i.get("nid") != nid]
    if len(kept) == len(items):
        return False
    _replace(f, json.dumps({"items": kept}, ensure_ascii=False))
    return True


def _resolve_notes(root: Path, items: List[Dict[str, Any]], index: "Index") -> None:
    """Each note's block id, found once and kept (so the pane, which has no index, can use it)."""
    changed = False
    for i in items:
        if not i.get("id"):
            got = index.resolve(i.get("part") or "")
            if got.get("id"):
                i["id"] = got["id"]
                changed = True
    if changed:
        try:
            _replace(_dir(root) / NOTES, json.dumps({"items": items}, ensure_ascii=False))
        except OSError:
            pass


def notes_by_block(items: List[Dict[str, Any]], most: int = NOTES_SHOWN) -> Dict[str, List[Dict[str, Any]]]:
    """{"b:<id>": [note, ...]} newest first, a few each. A note not resolved yet goes by its part as written."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for i in sorted(items, key=lambda x: -(x.get("at") or 0)):
        bid = i.get("id") or i.get("part") or ""
        if not bid:
            continue
        bid = bid if bid.startswith("b:") else "b:" + bid
        lst = out.setdefault(bid, [])
        if len(lst) < most:
            lst.append({"nid": i.get("nid"), "text": i.get("text"), "at": i.get("at"),
                        "question": i.get("question") or ""})
    return out


# ── names of the system → its blocks ─────────────────────────────────────────────────────────────────────────────

FILE_TOKEN = re.compile(r"(?<![\w/.\-])((?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z]\w{0,5})(?::\d+(?:-\d+)?)?")
CODE_SPAN = re.compile(r"`([^`\n]{1,200})`")
ROUTE_TOKEN = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|ANY)\s+(/[\w./{}:\-]*)")


class Index:
    """The system's names (parts, outside systems, entry points, tables, files) and the block each stands for, from
    a blueprint (extract's) and the config's Code (which part a file is in)."""

    def __init__(self, bp: Optional[Dict[str, Any]], code: Any = None, root: Optional[Path] = None,
                 titles: Optional[Dict[str, str]] = None):
        bp = bp or {}
        self.code, self.root = code, Path(root) if root else None
        self.ids: Dict[str, str] = {}          # id (lower) → block id
        self.labels: Dict[str, str] = {}       # label (as written) → block id
        self.routes: Dict[str, str] = {}       # "GET /x" → block id
        self.paths: Dict[str, str] = {}        # "/x" → block id
        self.tables: Dict[str, str] = {}       # "public.orders" → block id
        self.short_tables: Dict[str, str] = {}  # "orders" → block id
        for cid, c in (bp.get("components") or {}).items():
            self.ids[cid.lower()] = cid
            if isinstance(c, dict) and c.get("label"):
                self.labels[str(c["label"])] = cid
        for xid, x in (bp.get("externals") or {}).items():
            bid = "x:" + xid
            self.ids[xid.lower()] = bid
            self.ids[bid.lower()] = bid
            if isinstance(x, dict) and x.get("label"):
                self.labels[str(x["label"])] = bid
        for route, owner in (bp.get("routes") or {}).items():
            if isinstance(owner, str):
                self.routes[route] = owner
                self.paths.setdefault(route.split(" ", 1)[-1], owner)
        owner_of: Dict[str, str] = {}
        for part, ts in (bp.get("part_tables") or {}).items():
            for t in ts if isinstance(ts, (list, tuple, set, dict)) else []:
                owner_of.setdefault(str(t), part)
        store = bp.get("store")
        for t in bp.get("tables") or {}:
            bid = owner_of.get(t) or owner_of.get(t.split(".")[-1]) or (("x:" + store) if store else "")
            if bid:
                self.tables[t] = bid
                self.short_tables.setdefault(t.split(".")[-1], bid)
        for bid, title in (titles or {}).items():     # what the diagram on the page names its boxes
            raw = bid[2:] if bid.startswith("b:") else bid
            if title and title not in self.labels:
                self.labels[str(title)] = raw
            self.ids.setdefault(raw.lower(), raw)

    def _file(self, path: str) -> Optional[Dict[str, Any]]:
        path = path.replace("\\", "/").strip()
        while path.startswith("./"):
            path = path[2:]
        if not path or ".." in path.split("/"):
            return None
        if self.root is not None:
            try:
                if not (self.root / path).is_file():
                    return None
            except OSError:
                return None
        part = ""
        if self.code is not None:
            try:
                from .activity import part_of
                part = part_of(path, self.code) or ""
                if not self.code.is_code(path):
                    return None
            except Exception:  # noqa: BLE001
                part = ""
        return {"id": part, "file": path}

    def resolve(self, text: str) -> Dict[str, Any]:
        """{"text", "id", "file"} for one name as Claude or the person wrote it: an id, a label, an entry point, a
        table or a file of the system; "id" "" when nothing is found."""
        t = str(text or "").strip().strip("`").strip()
        out = {"text": t, "id": "", "file": ""}
        if not t:
            return out
        raw = t[2:] if t.startswith("b:") else t
        m = re.match(r"^(.*?)(?::\d+(?:-\d+)?)?$", raw)
        if "/" in raw and "." in raw.rsplit("/", 1)[-1] and not raw.startswith("/"):
            f = self._file(m.group(1) if m else raw)
            if f:
                return {**out, **f}
        low = raw.lower()
        if low in self.ids:
            return {**out, "id": self.ids[low]}
        for label, bid in self.labels.items():
            if label.lower() == low:
                return {**out, "id": bid}
        if raw in self.routes:
            return {**out, "id": self.routes[raw]}
        r = ROUTE_TOKEN.fullmatch(raw.upper().split(" ", 1)[0] + " " + raw.split(" ", 1)[-1]) if " " in raw else None
        if r and r.group(2) in self.paths:
            return {**out, "id": self.paths[r.group(2)]}
        if raw in self.paths:
            return {**out, "id": self.paths[raw]}
        if raw in self.tables or low in self.short_tables:
            return {**out, "id": self.tables.get(raw) or self.short_tables[low]}
        for label, bid in self.labels.items():   # a label in other words: one contains the other
            if len(low) >= 4 and (low in label.lower() or label.lower() in low):
                return {**out, "id": bid}
        return out

    def links(self, text: str) -> List[Dict[str, Any]]:
        """The names of the system an answer mentions, each once: [{"text", "id", "file", "code"}]. "code": only as
        inline code (an id like `core` is a word too)."""
        found: Dict[tuple, Dict[str, Any]] = {}

        def add(t: str, bid: str, file: str = "", code: bool = False) -> None:
            if t and (bid or file) and (t, code) not in found:
                found[(t, code)] = {"text": t, "id": bid, "file": file, "code": code}
        spans = set(CODE_SPAN.findall(text or ""))
        for m in FILE_TOKEN.finditer(text or ""):
            f = self._file(m.group(1))
            if f:
                add(m.group(0), f["id"], f["file"])
        for label, bid in self.labels.items():
            if len(label) >= 3 and re.search(r"(?<![\w])" + re.escape(label) + r"(?![\w])", text or ""):
                add(label, bid)
        for m in ROUTE_TOKEN.finditer(text or ""):
            route = f"{m.group(1)} {m.group(2).rstrip('.,;:')}"
            bid = self.routes.get(route) or self.paths.get(m.group(2).rstrip(".,;:"))
            if bid:
                add(route, bid)
        for t, bid in self.tables.items():
            if re.search(r"(?<![\w.])" + re.escape(t) + r"(?![\w])", text or ""):
                add(t, bid)
        for s in spans:
            s = s.strip()
            low = s.lower()
            if low in self.ids:
                add(s, self.ids[low], code=True)
            elif s in self.paths:
                add(s, self.paths[s], code=True)
            elif low in self.short_tables:
                add(s, self.short_tables[low], code=True)
            elif "/" in s and "." in s:
                f = self._file(s.split(":")[0])
                if f:
                    add(s, f["id"], f["file"], code=True)
        return list(found.values())


# ── what the page shows ─────────────────────────────────────────────────────────────────────────────────────────

def stamp(root: Path) -> str:
    """Changes when anything the panel shows does (a few stats, no reads)."""
    d = _dir(root)
    parts = []
    for name in (OUTBOX, DELIVERED, ASKING, ANSWERS, POINTING, NOTES):
        try:
            s = os.stat(d / name)
            parts.append(f"{s.st_mtime_ns:x}.{s.st_size:x}")
        except OSError:
            parts.append("-")
    return "_".join(parts)


def _outbox(root: Path) -> List[Dict[str, Any]]:
    return _lines(_dir(root) / OUTBOX, 50)


def view(root: Path, index: Optional[Index] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """The conversation panel: {"stamp", "items", "pointing", "notes"}. Items oldest first, each {"key", "from"
    ("web"/"chat"), "question", "about", "status" (queued/delivered/answering/answered/no answer), "at", "answer",
    "links"}."""
    now = time.time() if now is None else now
    index = index or Index(None)
    st = stamp(root)
    items: Dict[str, Dict[str, Any]] = {}
    for a in answers(root):
        about, question = a.get("about") or "", a.get("prompt") or ""
        items[a.get("key") or ""] = {"key": a.get("key") or "", "from": "web" if a.get("web") else "chat",
                                     "question": question, "about": about, "status": "answered",
                                     "at": a.get("at") or a.get("answered") or 0, "answered": a.get("answered") or 0,
                                     "answer": a.get("text") or "", "links": index.links(a.get("text") or "")}
    held = _json(_dir(root) / ASKING, {})
    latest = {}
    for s, q in held.items():
        if isinstance(q, dict) and q.get("key") not in items:
            fresh = now - float(q.get("at") or 0) < ANSWERING_FOR
            items[q["key"]] = {"key": q["key"], "from": "web" if q.get("web") else "chat",
                               "question": q.get("prompt") or "", "about": q.get("about") or "",
                               "status": "answering" if fresh else "no answer", "at": q.get("at") or 0,
                               "answer": "", "links": []}
            latest[s] = q
    seen = {i.get("key") for i in items.values()} | {q.get("id") for q in held.values() if isinstance(q, dict)}
    for d in _lines(_dir(root) / DELIVERED, DELIVERED_KEEP):
        if d.get("id") and d["id"] not in seen and now - float(d.get("at") or 0) < 24 * 3600:
            about, question = split_about(d.get("text") or "")
            items[d["id"]] = {"key": d["id"], "from": "web", "question": question, "about": about,
                              "status": "delivered", "at": d.get("at") or 0, "answer": "", "links": []}
            seen.add(d["id"])
    for o in _outbox(root):
        if o.get("id") and o["id"] not in seen:
            about, question = split_about(o.get("text") or "")
            items[o["id"]] = {"key": o["id"], "from": "web", "question": question, "about": about,
                              "status": "queued", "at": o.get("at") or 0, "answer": "", "links": []}
    shown = sorted(items.values(), key=lambda i: float(i.get("at") or 0))[-ITEMS_SHOWN:]
    p = pointing(root)
    point_out = None
    if p:
        point_out = {"at": p.get("at") or 0, "note": p.get("note") or "", "question": p.get("question") or "",
                     "parts": [index.resolve(x) for x in p.get("parts") or []]}
    ns = notes(root)
    _resolve_notes(root, ns, index)
    return {"stamp": st, "items": shown, "pointing": point_out, "notes": notes_by_block(ns)}
