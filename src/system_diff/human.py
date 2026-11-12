"""What a person should look at first, and whether it is what they asked for.

  decisions  each structural change of a comparison as one decision, in plain words: a title ("Claude made Core talk
             to the Mail server"), why it matters in one line, what it touches, the request it came under, and an
             impact score; ranked by consequence: an outside system first, then stored data, entry points, a
             dependency between parts or a boundary crossed, a part added or removed, settings, and code that changed
             inside a part last.
  intent     the person's latest request against what changed: the parts changed, those the request does not name
             (a simple word match, said as "not named in your request", never as wrong), and whether it changed
             anything structural at all.

Facts come only from the diff and design.Reading's notes; nothing is read again. Observational only: nothing here acts.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Optional, Set

from . import activity, design

# consequence tiers, highest first; a note's design weight (0..10) orders changes inside a tier
TIER = {"outside": 600, "data": 500, "entry": 400, "deploy": 350, "dependency": 300, "part": 200, "settings": 100,
        "internals": 0}
LOOK = TIER["settings"]   # this consequential or more: worth a look; below it, code changed inside a part


def decision_id(entity: str, op: str, subject: str, detail: Iterable[str] = ()) -> str:
    """Stable while the change stands: its kind and subject (and, for a table or settings change, what it adds)."""
    key = f"{entity}:{op}:{subject}" + ("|" + "|".join(sorted(detail)) if entity in ("table", "settings") else "")
    return "d" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _cut_why(text: str) -> str:
    return text if len(text) <= 110 else text[:109].rsplit(" ", 1)[0] + "…"


def _flow(r: design.Reading, ch: dict, note: Optional[dict]) -> tuple:
    src, dst = ch["id"].split(">")
    S, D = r.name(src), r.name(dst)
    if dst not in r.b["components"] and dst not in r.a["components"]:   # another deployed unit
        if ch["op"] == "add":
            return "dependency", f"Claude made {S} reach the {D} container", \
                "a runtime dependency on another deployed unit: it has to be up and reachable"
        return "dependency", f"Claude removed {S}'s link to the {D} container", "one fewer runtime dependency"
    if ch["op"] == "remove":
        return "dependency", f"Claude removed {S}'s dependency on {D}", \
            "decoupled: each can now change and be tested alone"
    text = (note or {}).get("text", "")
    if ch["id"] in r.b["api"]:
        return "dependency", f"Claude added a call from {S} to {D} over HTTP", \
            f"a new API contract: {D} must keep these paths and payloads stable"
    if "depend on each other" in text:
        return "dependency", f"Claude made {S} and {D} depend on each other", \
            "a cycle: neither can now change, be tested or be reused without the other"
    if "past the logic layer" in text:
        return "dependency", f"Claude made {S} reach {D} directly", \
            "a boundary crossed: an entry point reaching data past the logic layer"
    if (note or {}).get("status") == "warn":
        return "dependency", f"Claude made {S} depend on {D}", \
            f"a boundary crossed: the dependency points up the layers, so changes in {D} ripple into {S}"
    return "dependency", f"Claude made {S} depend on {D}", f"a new dependency: a change in {D} can now break {S}"


def _describe(r: design.Reading, ch: dict, note: Optional[dict]) -> Optional[tuple]:
    """(tier, title, why) for one change of the diff; None for one that is no decision."""
    op, entity, label, detail = ch["op"], ch["entity"], ch["label"], ch.get("detail") or []
    if entity == "external":
        user, ext = ch["id"].split(">")
        U, X = r.name(user), r.name("x:" + ext)
        db = (r.b["externals"].get(ext) or r.a["externals"].get(ext) or {}).get("shape") == "db"
        if op == "remove":
            return "outside", f"Claude made {U} stop using {X}", \
                "one fewer outside system to keep up, pay for and secure"
        if db:
            return "outside", f"Claude made {U} read and write {X}", \
                f"the system now keeps state in {X}: its data, migrations and access are now your concern"
        return "outside", f"Claude made {U} talk to {X}", \
            "a new outside dependency: its failures, latency and costs now reach your system"
    if entity in ("table", "migration"):
        if entity == "migration":
            return "data", f"Claude added the migration {label}", \
                "it changes stored data when it runs, once, against real data"
        if op == "add":
            return "data", f"Claude added the table {label}", \
                "stored data has a new shape: its migration runs once against real data"
        if op == "remove":
            return "data", f"Claude dropped the table {label}", "irreversible on real data"
        cols = ", ".join(x.replace("+ column ", "") for x in detail)
        return "data", f"Claude added {cols} to the table {label}", \
            "stored data changes shape: old and new code must both work while it rolls out"
    if entity == "route":
        owner = r.b["routes"].get(ch["id"]) or r.a["routes"].get(ch["id"]) or ""
        O = r.name(owner) if owner else "the system"
        if op == "remove":
            return "entry", f"Claude removed the endpoint {label} from {O}", \
                "a way in is gone: anything that still calls it breaks"
        method = label.split(" ", 1)[0]
        why = ("a new way in that changes state: who may call it, and how, is now a question"
               if method in design.STATE_CHANGING else "a new way in: anything that reaches the system can call it")
        return "entry", f"Claude added the endpoint {label} to {O}", why
    if entity == "flow":
        return _flow(r, ch, note)
    if entity == "container":
        if op == "add":
            return "deploy", f"Claude added the container {label}", \
                "a new deployed unit to run, scale, secure and watch"
        if op == "remove":
            return "deploy", f"Claude removed the container {label}", \
                "whatever reached it over the network now fails to connect"
        return "deploy", f"Claude changed the container {label}" + (f": {detail[0]}" if detail else ""), \
            "takes effect when deployed, not when merged"
    if entity == "deploy":
        return "settings", f"Claude changed {label}", "it changes what is deployed and how"
    if entity == "component":
        if op == "add":
            return "part", f"Claude added a new part, {label}", \
                "a new boundary in the system: something more to name, own and test"
        if op == "remove":
            return "part", f"Claude removed the part {label}", "anything that used it must now do without"
        return "internals", f"Claude changed code inside {label}" + (f" ({detail[0]})" if detail else ""), \
            "code changed inside the part; its structure is the same"
    if entity == "settings":
        names = detail[0] if detail else ""
        why = (note or {}).get("why", "")
        if (note or {}).get("status") == "warn":
            return "settings", f"Claude made {label} read {names}", _cut_why(why.split(":")[0]) or \
                "a setting no deploy configuration sets yet"
        return "settings", f"Claude made {label} read {names}", "a new setting every environment must provide"
    return None


def _parts(r: design.Reading, ch: dict, note: Optional[dict]) -> List[str]:
    if note and note.get("parts"):
        return [p for p in dict.fromkeys(note["parts"]) if p]
    entity = ch["entity"]
    if entity == "route":
        owner = r.b["routes"].get(ch["id"]) or r.a["routes"].get(ch["id"])
        return [owner] if owner else []
    if entity in ("component", "settings"):
        return [ch["id"]]
    if entity in ("table", "migration"):
        store = ((r.cfg.get("migrations") or {}).get("store") or "")
        if store:
            return ["x:" + store]
        return ["x:" + x for x, e in r.b["externals"].items() if e.get("shape") == "db"][:1]
    if entity in ("flow", "external"):
        src, dst = ch["id"].split(">")
        return [src, dst if entity == "flow" else "x:" + dst]
    return []


def edits_by_part(events: List[dict], code) -> Dict[str, tuple]:
    """Part -> (the person's request in effect when Claude last edited a file in it, or "", when that edit was)."""
    out: Dict[str, tuple] = {}
    prompt = ""
    for e in sorted(events, key=lambda e: e["at"]):
        kind = e.get("event")
        if kind == activity.PROMPT and not activity.is_system(e.get("text", "")):
            prompt = e.get("text", "")
        elif kind == activity.EDIT and e.get("path"):
            part = activity.part_of(e["path"], code)
            if part:
                out[part] = (prompt, e["at"])
    return out


def requests_by_part(events: List[dict], code) -> Dict[str, str]:
    """Part -> the person's request in effect when Claude last edited a file in it."""
    return {p: r for p, (r, _at) in edits_by_part(events, code).items() if r}


def decisions(c, events: Optional[List[dict]] = None, code=None, reading: Optional[design.Reading] = None
              ) -> List[Dict[str, Any]]:
    """The comparison's structural changes as decisions, the most consequential first: {"id", "kind" (the tier),
    "entity", "op", "subject", "title", "why", "more" (design.py's longer reading), "sign", "status", "impact",
    "look" (worth a look: not only code inside a part), "parts", "names", "request", "at" (Claude's last edit in its
    parts, 0 when unknown), "when" (that edit, said as "2 min ago"; "" when unknown)}."""
    r = reading or design.Reading(c)
    notes = {(n["change"]["entity"], n["change"]["id"], n["change"]["op"]): n for n in r.notes() if n.get("change")}
    edits = edits_by_part(events or [], code) if code is not None else {}
    out = []
    for ch in c.diff["changes"]:
        note = notes.get((ch["entity"], ch["id"], ch["op"]))
        said = _describe(r, ch, note)
        if not said:
            continue
        tier, title, why = said
        parts = _parts(r, ch, note)
        weight = int((note or {}).get("weight", 0))
        sign = {"add": "+", "remove": "−"}.get(ch["op"], "~")
        status = (note or {}).get("status") or {"add": "added", "remove": "removed"}.get(ch["op"], "changed")
        request = next((edits[p][0] for p in parts if edits.get(p, ("",))[0]), "")
        at = max((edits[p][1] for p in parts if p in edits), default=0.0)
        out.append({"id": decision_id(ch["entity"], ch["op"], ch["id"], ch.get("detail") or ()), "kind": tier,
                    "entity": ch["entity"], "op": ch["op"], "subject": ch["id"], "title": title, "why": why,
                    "more": (note or {}).get("why", ""),
                    "sign": "▲" if status == "warn" else sign, "status": status,
                    "impact": TIER[tier] + max(0, min(10, weight)), "look": TIER[tier] >= LOOK,
                    "parts": parts, "names": [r.name(p) for p in parts], "request": request, "at": at,
                    "when": activity.ago(at) if at else ""})
    return sorted(out, key=lambda d: (-d["impact"], d["title"]))


# ── intent against effect ─────────────────────────────────────────────────────

GENERIC = {"app", "apps", "src", "lib", "libs", "pkg", "packages", "source", "sources", "internal", "code", "the",
           "and", "for", "server", "service", "services", "part", "module", "modules", "main", "index"}


def _words(text: str) -> Set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) >= 3 and w not in GENERIC:
            out.add(w)
            out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


def part_words(part: str, label: str, cfg: Dict[str, Any]) -> Set[str]:
    """The words a request could name a part by: its id, its label, the folders of its paths."""
    words = _words(part.replace("x:", "").replace("/", " ").replace("_", " ").replace("-", " ")) | _words(label)
    for comp in cfg.get("components", []):
        if comp.get("id") == part:
            for p in comp.get("paths", []):
                words |= _words(p.replace("*", " ").replace("/", " "))
    return words


def latest_requests(events: List[dict]) -> List[str]:
    """The person's latest request, with any sent before it that Claude had not finished (since the last stop)."""
    out: List[str] = []
    ended = False
    for e in sorted(events, key=lambda e: e["at"]):
        kind = e.get("event")
        if kind == activity.PROMPT and not activity.is_system(e.get("text", "")) and e.get("text", "").strip():
            if ended:
                out, ended = [], False   # a stop ended the turn: this prompt starts the next
            out.append(e.get("text", "").strip())
        elif kind == activity.STOP:
            ended = True
    return out


def intent(c, events: List[dict], name=None) -> Optional[Dict[str, Any]]:
    """The latest request(s) against what the comparison changed: {"requests", "changed" (part ids, the structural
    ones first), "names" ({id: label}), "unnamed" (changed parts the request does not name), "structural" (whether
    any change was more than code inside a part)}; None with no request."""
    asked = latest_requests(events)
    if not asked:
        return None
    named = _words(" ".join(asked))
    name = name or (lambda p: design.name_of(p, c.after, c.before))
    changed: List[str] = []
    structural = False
    for ch in c.diff["changes"]:
        inside = ch["entity"] in ("component", "deploy") and ch["op"] == "change"
        structural |= not inside
        if ch["entity"] in ("component", "settings"):
            ps = [ch["id"]]
        elif ch["entity"] in ("flow", "external"):
            src, dst = ch["id"].split(">")
            ps = [src]                                 # the part that changed is the one that now reaches out
        elif ch["entity"] == "route":
            ps = [c.after["routes"].get(ch["id"]) or c.before["routes"].get(ch["id"]) or ""]
        else:
            ps = []
        for p in ps:
            if p and p not in changed:
                changed.append(p)
    names = {p: name(p) for p in changed}
    unnamed = [p for p in changed if not (part_words(p, names[p], c.cfg) & named)]
    return {"requests": asked, "changed": changed, "names": names, "unnamed": unnamed, "structural": structural}


def intent_line(it: Dict[str, Any], working: bool = False) -> List[tuple]:
    """The intent line as runs: "You asked: … · changed: A, B · not named in your request: C"."""
    request = it["requests"][-1] + (f" (and {len(it['requests']) - 1} earlier)" if len(it["requests"]) > 1 else "")
    runs = [("You asked: ", "muted"), (request, "")]
    if not it["structural"]:
        what = ("no change to the system's structure yet" if working else
                "this request changed nothing in the system's structure")
        if it["changed"]:
            what += f" (code inside {design.and_list([it['names'][p] for p in it['changed']], 3)})"
        return runs + [(" · " + what, "muted")]
    named = [p for p in it["changed"] if p not in it["unnamed"]]
    if named:
        runs += [(" · changed: ", "muted"), (design.and_list([it["names"][p] for p in named], 4), "bold")]
    if it["unnamed"]:
        runs += [(" · not named in your request: ", "muted"),
                 (design.and_list([it["names"][p] for p in it["unnamed"]], 4), "attention")]
    return runs


# ── what the person optimises for ─────────────────────────────────────────────

def mark_objectives(decs: List[Dict[str, Any]], c, objs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each decision that touches one of the person's objectives (objectives.touches, no LLM, one change at a time)
    gets "objectives" (the ids) and "touches" ([{"id", "text", "reason"}]), and ranks above the others of its tier
    (its impact goes up by TOUCHED, less than the gap between two tiers). Every other decision gets "objectives": []
    and "touches": []. Returns the decisions, ranked again."""
    for d in decs:
        d.setdefault("objectives", [])
        d.setdefault("touches", [])
    if not objs or not decs:
        return decs
    from types import SimpleNamespace
    from . import objectives
    by = {(ch["entity"], ch["op"], ch["id"]): ch for ch in (getattr(c, "diff", None) or {}).get("changes", [])}
    text = {o["id"]: o["text"] for o in objs}
    for d in decs:
        ch = by.get((d["entity"], d["op"], d["subject"]))
        if not ch:
            continue
        try:
            hit = objectives.touches(objs, SimpleNamespace(diff={"changes": [ch]}, after=c.after, before=c.before))
        except Exception:  # noqa: BLE001 - a mark beside the change, never in its way
            hit = {}
        if hit:
            ids = [o["id"] for o in objs if o["id"] in hit]
            d["objectives"] = ids
            d["touches"] = [{"id": i, "text": text[i], "reason": hit[i][0] if hit[i] else ""} for i in ids]
            d["impact"] += TOUCHED
    return sorted(decs, key=lambda d: (-d["impact"], d["title"]))


TOUCHED = 20   # a change touching an objective ranks above the others of its tier (tiers are 50 or more apart)


def touched_line(t: Dict[str, str]) -> str:
    """"touches objective: <text> (<reason>)"."""
    return f"touches objective: {t['text']}" + (f" ({t['reason']})" if t.get("reason") else "")
