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
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

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
    cycles: set = set()
    for ch in c.diff["changes"]:
        note = notes.get((ch["entity"], ch["id"], ch["op"]))
        said = _describe(r, ch, note)
        if not said:
            continue
        tier, title, why = said
        if "depend on each other" in title:   # a cycle is one change, though the diff has both of its directions
            pair = frozenset(ch["id"].split(">")[:2])
            if pair in cycles:
                continue
            cycles.add(pair)
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


# ── what a block is, does and connects to, in plain words ───────────────────────────────────────────────────────
#
# For every box of a diagram (a part, a container of parts, who uses the system, an outside system or store): a
# SHORT line for the pane's two rows under the diagram (the most useful first), a CARD of about eight lines for the
# web page's hover, and MORE, everything, for its details strip. From the blueprint and the diagram's model only (how
# an outside system is reached is extract.py's `connections`, read once per file by content and already redacted):
# nothing is read again. A box with no change says nothing about changes, or when it last changed if the history
# knows.

ROLE_WORDS = {"ui": "a front end people use", "interface": "an entry point", "logic": "logic inside the system",
              "data": "data access", "store": "an outside store of data", "external": "an outside system"}
CARD = 8   # lines a hover card holds before the rest goes to the details strip


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _short_table(t: str) -> str:
    return t.split(".", 1)[1] if t.startswith("public.") else t


def where_it_is(conn: Dict[str, Any]) -> str:
    """"db:5432, database flows (postgresql)": host, port, what is in it and the protocol; "" when nothing says."""
    if not conn:
        return ""
    at = conn.get("host", "")
    if conn.get("port"):
        at = f"{at}:{conn['port']}" if at else f"port {conn['port']}"
    bits = [at] if at else []
    if conn.get("database"):
        bits.append(f"database {conn['database']}")
    if conn.get("bucket"):
        bits.append(("bucket " if len(conn["bucket"]) == 1 else "buckets ") + ", ".join(conn["bucket"][:3]))
    if conn.get("index"):
        bits.append(("index " if len(conn["index"]) == 1 else "indexes ") + ", ".join(conn["index"][:3]))
    if conn.get("region"):
        bits.append(f"region {conn['region']}")
    text = ", ".join(bits)
    if text and conn.get("scheme"):
        text += f" ({conn['scheme']})"
    return text


def _setting_words(s: Dict[str, Any]) -> str:
    if s.get("secret"):
        return f"{s['name']} (a secret: not shown)"
    if s.get("value") is None:
        return f"{s['name']} (set at deploy time)"
    return f"{s['name']} = {s['value']}" + (f" in {s['where'].rsplit('/', 1)[-1]}" if s.get("where") else "")


def connection_lines(conn: Dict[str, Any], name=lambda p: p) -> List[str]:
    """How the system reaches one outside system, line by line, most useful first: where (host, port, database,
    bucket, index), the setting that says so and where it is set, the service that runs it, the other settings, the
    client libraries, and which parts use which. Never a secret: the values were redacted when read."""
    if not conn:
        return []
    out = []
    where = where_it_is(conn)
    setting = next((s for s in conn.get("settings") or [] if s.get("value") and s["value"] == conn.get("url")), None)
    if where:
        said = f"Reached at {where}"
        if setting:
            said += f", set by {setting['name']} in {setting['where'].rsplit('/', 1)[-1]}"
        elif conn.get("url") and not conn.get("from"):
            said += ", written in the code"
        out.append(said)
    else:
        read = [s["name"] for s in conn.get("settings") or [] if not s.get("secret")]
        out.append("Where it is: set at deploy time" + (f" ({', '.join(read[:3])}; no value in the repository)"
                                                        if read else " (no setting or address in the repository)"))
    svc = conn.get("service")
    if svc:
        line = f"Runs as service {svc['name']}" + (f" ({svc['image']})" if svc.get("image") else "")
        published = [p for p in svc.get("ports") or [] if ":" in p]
        if published:
            line += ", published on localhost:" + ", ".join(p.rsplit(":", 1)[0].rsplit(":", 1)[-1] for p in published[:2])
        out.append(line + f" in {svc.get('where', '').rsplit('/', 1)[-1]}" if svc.get("where") else line)
    rest = [s for s in conn.get("settings") or [] if s is not setting
            and (where or s.get("secret") or s.get("value") is not None)]   # those set at deploy time: said above
    if rest:
        out.append("Settings: " + "; ".join(_setting_words(s) for s in rest[:4])
                   + (f"; {len(rest) - 4} more" if len(rest) > 4 else ""))
    if conn.get("clients"):
        out.append("Through " + design.and_list(conn["clients"], 4))
    parts = conn.get("parts") or {}
    if parts:
        said = []
        for p, how in parts.items():
            what = list(how.get("clients") or []) + [f"reads {s}" for s in how.get("settings") or []]
            said.append(name(p) + (f" ({', '.join(what[:3])})" if what else ""))
        out.append("Used by " + design.and_list(said, 4))
    return out


def connection_short(conn: Dict[str, Any]) -> str:
    """One phrase on how it is reached, for the pane: "db:5432, database flows (postgresql) via DATABASE_URL"."""
    if not conn:
        return ""
    where = where_it_is(conn)
    named = next((s["name"] for s in conn.get("settings") or [] if s.get("value") and s["value"] == conn.get("url")),
                 "")
    if where:
        return f"at {where}" + (f" via {named}" if named else "")
    read = [s["name"] for s in conn.get("settings") or [] if not s.get("secret")]
    return "where: set at deploy time" + (f" ({read[0]})" if read else "")


def _routes_words(routes: List[str], most: int = 3) -> str:
    n = len(routes)
    return f"{n} endpoint{'' if n == 1 else 's'}" + (f": {', '.join(routes[:most])}" + (", …" if n > most else "")
                                                       if routes else "")


def _techs(bp: dict, parts: Iterable[str]) -> List[str]:
    from .topics import TECH, TOP_PACKAGE, tech_key
    found: Dict[str, int] = {}
    for p in parts:
        for name, n in ((bp.get("imported") or bp.get("stack") or {}).get(p) or {}).items():
            key = tech_key(name) or tech_key(TOP_PACKAGE.get(name, ""))
            if key:
                found[key] = found.get(key, 0) + int(n or 1)
    order = sorted(found, key=lambda k: (-found[k], k))
    return [TECH[k][0] for k in order if k in TECH]


def block_details(c, model: Dict[str, Any], decs: Optional[List[Dict[str, Any]]] = None, since: str = "this turn",
                  past: Optional[List[Dict[str, Any]]] = None, about: Optional[Dict[str, str]] = None,
                  clock: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """{"b:<id>": {"title", "short", "lines", "more"}} for every box and item of a diagram's model: what it is (its
    name, role, own description, files and lines), what it does (endpoints, tables, settings, technologies, the
    outside systems it reaches and how), how it connects (who uses it and what it uses, over what), and what changed
    in it `since` (`decs`: human.decisions), or else when it last changed (`past`: history entries, newest first)."""
    from . import graph, names as names_
    bp, before, cfg = c.after, c.before, c.cfg
    kind = design.kinds(bp, cfg)
    about = about if about is not None else names_.about(bp)
    conns = graph.connections(bp)
    labels: Dict[str, str] = {}
    for n in model.get("nodes") or []:
        labels[n["id"]] = str(n.get("title", n["id"])).split("  @")[0]
        for i in n.get("items") or []:
            labels[i["id"]] = str(i.get("title", i["id"])).split("  @")[0]

    def name(nid: str) -> str:
        return labels.get(nid) or design.name_of(nid, bp, before)

    via: Dict[tuple, str] = {}                      # (from, to) -> what travels on it, as the diagram labels it
    for e in model.get("edges") or []:
        for pair in e.get("pairs") or [[e["from"], e["to"]]]:
            if e.get("label") and not e["label"].endswith("+"):
                via.setdefault(tuple(pair), e["label"])
    from .architecture import protocol
    for x, e in bp["externals"].items():
        for u in e.get("used_by") or []:
            via.setdefault((u, "x:" + x), protocol(e["label"], e.get("shape", "")))
    for key in bp.get("api") or {}:
        via.setdefault(tuple(key.split(">")), "HTTP")
    actor_flows = [(e["from"], e["to"], e.get("label", "")) for e in model.get("edges") or []
                   if str(e.get("from", "")).startswith("a:")]

    def linked(ids: List[str], inside: Set[str]) -> List[str]:
        said = []
        for o in ids:
            how = next((via[k] for k in via if (k[0] in inside and k[1] == o) or (k[1] in inside and k[0] == o)), "")
            said.append(name(o) + (f" over {how}" if how and how not in ("call",) else ""))
        return said

    def host_port(o: str) -> str:
        conn = (bp.get("connections") or {}).get(o[2:]) or {}
        return where_it_is({k: v for k, v in conn.items() if k in ("host", "port")})

    def changes(inside: Set[str]) -> Tuple[List[str], str]:
        mine = [d for d in decs or [] if inside & set(d.get("parts") or [])]
        if mine:
            lines = [f"Changed {since}: " + mine[0]["title"]] + ["  " + d["title"] for d in mine[1:3]]
            if len(mine) > 3:
                lines.append(f"  and {len(mine) - 3} more")
            if mine[0].get("look") and mine[0].get("why"):
                lines.append("Why it matters: " + mine[0]["why"])
            asked = next((d["request"] for d in mine if d.get("request")), "")
            if asked:
                lines.append(f"Under your request “{asked if len(asked) <= 100 else asked[:99] + '…'}”")
            return lines, f"changed {since}: {mine[0]['title']}"
        for e in past or []:
            if inside & set(e.get("parts") or []):
                from .history import when
                asked = e.get("request") or ""
                tail = f": you asked “{asked if len(asked) <= 80 else asked[:79] + '…'}”" if asked else ""
                return [f"Last changed {when(e['at'], clock)}{tail}"], f"last changed {when(e['at'], clock)}"
        return [], ""

    def part_facts(inside: List[str]) -> Dict[str, Any]:
        mine = set(inside)
        routes = sorted((r for r, o in bp["routes"].items() if o in mine),
                        key=lambda r: (r.split(" ", 1)[-1], r))
        tables = sorted({_short_table(t) for p in inside for k in ("defines", "queries")
                         for t in (bp.get("part_tables") or {}).get(p, {}).get(k, [])})
        defines = sorted({_short_table(t) for p in inside for t in (bp.get("part_tables") or {}).get(p, {})
                          .get("defines", [])})
        settings = sorted({s for p in inside for s in (bp.get("env") or {}).get(p, [])})
        outs = sorted({t for (s, t) in conns if s in mine and t not in mine})
        ins = sorted({s for (s, t) in conns if t in mine and s not in mine})
        ins += sorted({a for a, t, _l in actor_flows if t in mine} - set(ins))
        return {"routes": routes, "tables": tables, "defines": defines, "settings": settings,
                "techs": _techs(bp, inside), "outs": outs, "ins": ins}

    out: Dict[str, Dict[str, Any]] = {}
    for n in model.get("nodes") or []:
        for box in [n] + list(n.get("items") or []):
            nid = box["id"]
            title = labels.get(nid, nid)
            group = box is n and bool(n.get("items"))
            inside = [i["id"] for i in n["items"]] if group else [nid]
            lines: List[str] = []
            short: List[str] = []
            more: List[str] = []
            if nid.startswith("a:"):
                uses = [(t, lab) for a, t, lab in actor_flows if a == nid]
                lines.append("Who uses the system" + (f": {box.get('sub')}" if box.get("sub") else ""))
                said = [name(t) + (f" over {lab}" if lab else "") for t, lab in uses]
                if said:
                    lines.append("Uses " + design.and_list(said, 4))
                    short.append("uses " + design.and_list(said, 3))
                out["b:" + nid] = {"title": title, "short": " · ".join(short) or "who uses the system",
                                   "lines": lines, "more": " · ".join(lines)}
                continue
            if nid.startswith("f:"):
                continue   # a module of a zoomed-in part: the pane says what it imports and serves
            if nid.startswith("x:"):
                x = nid[2:]
                e = bp["externals"].get(x) or before["externals"].get(x) or {}
                conn = (bp.get("connections") or {}).get(x) or {}
                role = ROLE_WORDS.get(kind.get(nid, ""), "an outside system")
                lines.append(_cap(role) + (f" · {len(bp['tables'])} tables" if x == bp.get("store") and bp["tables"]
                                           else ""))
                conn_lines = connection_lines(conn, name)
                users = [u for u in e.get("used_by") or []]
                if not conn_lines:
                    conn_lines = ["Where it is: not said in the repository (set at deploy time)"]
                    if users:
                        conn_lines.append("Used by " + design.and_list([name(u) for u in users], 4))
                lines += conn_lines
                short.append(connection_short(conn) or "where: set at deploy time")
                if conn.get("clients"):
                    short.append("through " + design.and_list(conn["clients"], 3))
                if users:
                    short.append("used by " + design.and_list([name(u) for u in users], 3))
                if x == bp.get("store") and bp["tables"]:
                    more.append("Tables: " + ", ".join(_short_table(t) for t in list(bp["tables"])[:12])
                                + (f" and {len(bp['tables']) - 12} more" if len(bp["tables"]) > 12 else ""))
                if e.get("found"):
                    more.append("Known by: " + "; ".join(e["found"]))
                changed, said = changes({nid} | {f"{u}>{x}" for u in users})
                lines[1:1] = changed
                if said:
                    short.insert(0, said)
                out["b:" + nid] = {"title": title, "short": " · ".join(short), "lines": lines[:CARD],
                                   "more": " · ".join(lines + more)}
                continue
            f = part_facts(inside)
            comp = bp["components"].get(nid) or before["components"].get(nid) or {}
            if group:
                files = sum((bp["components"].get(p) or {}).get("files", 0) for p in inside)
                lns = sum((bp["components"].get(p) or {}).get("lines", 0) for p in inside)
                lines.append(f"A container of {len(inside)} parts: " + design.and_list([name(p) for p in inside], 4))
            else:
                files, lns = comp.get("files", 0), comp.get("lines", 0)
                role = ROLE_WORDS.get(kind.get(nid, ""), "a part")
                said = about.get(nid) or (cfg_about(cfg, nid))
                lines.append(_cap(role) + (f": {said}" if said and said != title else "")
                             + f" · {graph.files_word(files)}" + (f", {lns} lines" if lns else "")
                             + (f" · {nid}" if nid != title else ""))
            if f["routes"]:
                lines.append("Serves " + _routes_words(f["routes"]))
                short.append("serves " + _routes_words(f["routes"], 2))
            reach = [o for o in f["outs"] if o.startswith("x:")]
            inner = [o for o in f["outs"] if not o.startswith("x:")]
            if reach:
                said = []
                for o in reach:
                    conn = (bp.get("connections") or {}).get(o[2:]) or {}
                    where = where_it_is(conn)
                    how = next((via[k] for k in via if k[0] in inside and k[1] == o), "")
                    said.append(name(o) + (f" over {how}" if how and how.lower() not in name(o).lower().split() else "")
                                + (f" at {where}" if where else " (where: set at deploy time)"))
                lines.append("Reaches " + "; ".join(said[:3]) + (f"; {len(said) - 3} more" if len(said) > 3 else ""))
            if f["ins"]:
                ins = linked(f["ins"], set(inside))
                short.append("used by " + design.and_list(ins, 2))
            if inner or reach:
                outs = [name(o) for o in inner] + [name(o) + (f" at {host_port(o)}" if host_port(o) else "")
                                                   for o in reach]
                short.append("uses " + design.and_list(outs, 3))
            connects = []
            if f["ins"]:
                connects.append("used by " + design.and_list(linked(f["ins"], set(inside)), 3))
            if inner:
                connects.append("uses " + design.and_list(linked(inner, set(inside)), 4))
            if connects:
                lines.insert(2 if f["routes"] else 1, _cap("; ".join(connects)))
            elif not group and not reach:
                lines.append("Connects to nothing else in the system")
            if f["tables"]:
                verb = "Keeps" if f["defines"] and set(f["defines"]) == set(f["tables"]) else "Tables"
                lines.append(f"{verb}: " + design.and_list(f["tables"], 5))
                short.append("tables " + design.and_list(f["tables"], 2))
            if f["settings"]:
                lines.append("Reads settings " + design.and_list(f["settings"], 4))
                short.append("reads " + design.and_list(f["settings"], 2))
            if f["techs"]:
                lines.append("Built with " + design.and_list(f["techs"], 5))
                short.append("built with " + design.and_list(f["techs"], 2))
            changed, said = changes(set(inside))
            if said:
                short.insert(0, said)
            for o in reach:
                conn = (bp.get("connections") or {}).get(o[2:]) or {}
                if conn:
                    more.append(f"{name(o)}: " + "; ".join(connection_lines(conn, name)[:3]))
            if f["routes"] and len(f["routes"]) > 3:
                more.append("Endpoints: " + ", ".join(f["routes"][:30]))
            if f["settings"] and len(f["settings"]) > 4:
                more.append("Settings: " + ", ".join(f["settings"]))
            if group:
                more.append("Parts: " + ", ".join(f"{name(p)} ({p})" for p in inside))
            body = lines[:1] + changed + lines[1:]
            out["b:" + nid] = {"title": title, "short": " · ".join(short) or "connects to nothing in the system",
                               "lines": body[:CARD], "more": " · ".join(body + more)}
    return out


def cfg_about(cfg: Dict[str, Any], cid: str) -> str:
    """What the config says a part does (`about`), if it says."""
    return next((c.get("about", "") for c in cfg.get("components", []) if c.get("id") == cid), "")
