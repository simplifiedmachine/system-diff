"""How the agent is shaping the system, read the way a system designer reads a change: each structural change as a
design decision (a dependency, an endpoint, a table, an outside system, a part, a container), what it trades off, and
the question worth putting to the agent before it goes on. And the shape of one part: how coupled it is, which entry
points reach it, what it reaches outside the system.

Facts come only from the blueprints and their diff (the parts' kinds, every connection and what flows on it, the
routes, settings, tables, containers and outside systems); the design knowledge is in the wording. No LLM.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import graph

RANK = {"ui": 0, "interface": 1, "logic": 2, "data": 3}   # the layers, outermost first
ATTENTION = 7          # a note this weighty is worth a look before the agent goes on
STATE_CHANGING = ("POST", "PUT", "PATCH", "DELETE")
VERB_SEGMENTS = ("create", "get", "update", "delete", "add", "remove", "list", "fetch", "set", "do", "make")


def kinds(bp: dict, cfg: Dict[str, Any]) -> Dict[str, str]:
    """Each part's column (ui, interface, logic, data) and each outside system's (store or external)."""
    out = {cid: graph.part_kind(cid, c, bp, cfg) for cid, c in bp["components"].items()}
    for x, e in bp["externals"].items():
        out["x:" + x] = "store" if e.get("shape") == "db" else "external"
    return out


def depends(bp: dict) -> Dict[str, Set[str]]:
    """Part -> the parts it depends on (imports and API calls); outside systems are left out."""
    out: Dict[str, Set[str]] = defaultdict(set)
    for (src, dst), (kind, _n) in graph.connections(bp).items():
        if kind == "call" and src != dst and not dst.startswith("x:"):
            out[src].add(dst)
    return out


def outside(bp: dict) -> Dict[str, Set[str]]:
    """Part -> the outside systems it reaches itself."""
    out: Dict[str, Set[str]] = defaultdict(set)
    for x, e in bp["externals"].items():
        for user in e["used_by"]:
            out[user].add("x:" + x)
    return out


def path(adj: Dict[str, Set[str]], a: str, b: str) -> Optional[List[str]]:
    """The shortest chain of dependencies from a to b, both ends included; None when a does not reach b."""
    seen, queue = {a: None}, deque([a])
    while queue:
        n = queue.popleft()
        if n == b:
            out = [n]
            while seen[out[-1]] is not None:
                out.append(seen[out[-1]])
            return out[::-1]
        for m in sorted(adj.get(n, ())):
            if m not in seen:
                seen[m] = n
                queue.append(m)
    return None


def reach(adj: Dict[str, Set[str]], a: str) -> Set[str]:
    seen, queue = {a}, deque([a])
    while queue:
        for m in adj.get(queue.popleft(), ()):
            if m not in seen:
                seen.add(m)
                queue.append(m)
    return seen - {a}


def fan_in(adj: Dict[str, Set[str]]) -> Dict[str, int]:
    out: Dict[str, int] = defaultdict(int)
    for src, dsts in adj.items():
        for d in dsts:
            out[d] += 1
    return out


def name_of(nid: str, *bps: dict) -> str:
    for bp in bps:
        if nid.startswith("x:") and nid[2:] in bp["externals"]:
            return bp["externals"][nid[2:]]["label"]
        if nid in bp["components"]:
            return bp["components"][nid]["label"]
        if nid in bp.get("container_labels", {}):
            return bp["container_labels"][nid]
    return nid


def and_list(names: Iterable[str], most: int = 3) -> str:
    names = list(names)
    if len(names) > most:
        names = names[:most] + [f"{len(names) - most} more"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def _note(sign: str, status: str, text: str, why: str = "", ask: str = "", parts: Iterable[str] = (),
          weight: int = 1) -> Dict[str, Any]:
    return {"sign": sign, "status": status, "text": text, "why": why, "ask": ask, "parts": list(parts),
            "weight": weight}


class Reading:
    """One comparison, read for design: the kinds, dependencies and fan-in on both sides, said once for every note."""

    def __init__(self, cmp) -> None:
        self.a, self.b, self.cfg = cmp.before, cmp.after, cmp.cfg
        self.diff = cmp.diff
        self.kind = {**kinds(self.a, self.cfg), **kinds(self.b, self.cfg)}
        self.adj_a, self.adj = depends(self.a), depends(self.b)
        self.ext = outside(self.b)
        self.into = fan_in(self.adj)
        self.uses = self.b.get("uses", {})
        self.heading = {k: h.lower() for k, h in self.cfg["picture"]["columns"]}
        self.entries = sorted(c for c in self.b["components"] if self.kind.get(c) in ("ui", "interface"))
        self.configured = set(self.b.get("configured", ()))

    def name(self, nid: str) -> str:
        return name_of(nid, self.b, self.a)

    def layer(self, nid: str) -> str:
        return self.heading.get(self.kind.get(nid, ""), "")

    def entry_paths(self, part: str) -> List[str]:
        """The entry points (and front ends) whose requests can reach `part`."""
        return [e for e in self.entries if e != part and part in reach(self.adj, e)]

    # ── each kind of change ──

    def flow(self, c: dict) -> Optional[Dict[str, Any]]:
        src, dst = c["id"].split(">")
        if dst not in self.b["components"] and dst not in self.a["components"]:   # a container, by a setting
            return _note("+" if c["op"] == "add" else "−", "added" if c["op"] == "add" else "removed",
                         f"{self.name(src)} {'now reaches' if c['op'] == 'add' else 'no longer reaches'} the "
                         f"{self.name(dst)} container", "a runtime dependency on another deployed unit: it has to be "
                         "up, reachable and compatible" if c["op"] == "add" else "", parts=[src], weight=3)
        S, D = self.name(src), self.name(dst)
        if c["op"] == "remove":
            was_cycle = path(self.adj_a, dst, src) is not None
            return _note("−", "removed", f"{S} no longer depends on {D}",
                         "decoupled: the cycle between them is gone" if was_cycle else
                         "decoupled: each can now change, be tested and be reused alone", parts=[src, dst],
                         weight=4 if was_cycle else 2)
        names = self.uses.get(c["id"], [])
        over_http = c["id"] in self.b["api"]
        what = f" ({and_list(names, 3)})" if names else ""
        if over_http:
            return _note("+", "added", f"{S} now calls {D} over HTTP{what}",
                         f"the API contract between them grows: {D} must keep these paths and payloads stable, or "
                         "change both together", parts=[src, dst], weight=3)
        back = path(self.adj, dst, src)
        if back:
            loop = " → ".join(self.name(n) for n in [src] + back)
            return _note("+", "warn", f"{S} and {D} now depend on each other{what}",
                         f"a cycle ({loop}): neither can change, be tested or be reused without the other, and "
                         "import order starts to matter", "Which way should the dependency point? Could the "
                         "shared piece move into a part both use?", parts=[src, dst], weight=8)
        k1, k2 = self.kind.get(src, ""), self.kind.get(dst, "")
        if k1 in RANK and k2 in RANK and RANK[k2] < RANK[k1]:
            return _note("+", "warn", f"{S} ({self.layer(src)}) now depends on {D} ({self.layer(dst)}){what}",
                         f"the dependency points up the layers: {self.layer(src)} that knows about "
                         f"{self.layer(dst)} turns the layering around, and changes in {D} now ripple down into {S}",
                         f"Could what {S} needs from {D} move down a layer, or be passed in?", parts=[src, dst],
                         weight=7)
        if k1 == "interface" and k2 == "data":
            logic = sorted(d for d in self.adj.get(src, ()) if self.kind.get(d) == "logic")
            if logic:
                return _note("+", "changed", f"{S} now reaches {D} directly, past the logic layer{what}",
                             f"fine for a plain read; rules then end up in the entry point, beside the ones "
                             f"{self.name(logic[0])} holds", f"Should this go through {self.name(logic[0])}?",
                             parts=[src, dst], weight=4)
        users = self.into.get(dst, 0)
        out = len(self.adj.get(src, ()))
        most_out = max((len(v) for v in self.adj.values()), default=0)
        if out >= 6 and out == most_out:
            why = (f"{S} now depends on {out} parts, the most in the system: a hub that a change in any of them "
                   "can break")
            return _note("+", "changed", f"{S} now depends on {D}{what}", why, f"Should {S} be split?",
                         parts=[src, dst], weight=4)
        if users >= 5:
            return _note("+", "added", f"{S} now depends on {D}{what}",
                         f"{D} is used by {users} parts now: its interface is load-bearing, keep it narrow",
                         parts=[src, dst], weight=3)
        return _note("+", "added", f"{S} now depends on {D}{what}",
                     f"a change in {D} can now break {S}; they no longer change apart", parts=[src, dst], weight=2)

    def route(self, c: dict) -> Dict[str, Any]:
        route = c["id"]
        method, _, url = route.partition(" ")
        owner = self.b["routes"].get(route) or self.a["routes"].get(route) or ""
        O = self.name(owner) if owner else "The system"
        callers = self.callers_of(url)
        if c["op"] == "remove":
            if callers:
                return _note("−", "removed", f"{O} no longer answers {route}",
                             f"{and_list(self.name(x) for x in callers)} still call{'s' if len(callers) == 1 else ''} "
                             f"it: those calls fail from the next deploy", "Keep it until its callers move, or "
                             "change both in this turn?", parts=[owner, *callers], weight=10)
            return _note("−", "removed", f"{O} no longer answers {route}",
                         "a breaking change for anything outside this repository that calls it (another service, a "
                         "script, a saved link); nothing inside calls it", "Is anything outside this repository still "
                         "calling it?", parts=[owner], weight=8)
        segs = [s for s in url.strip("/").split("/") if s and not s.startswith("{")]
        rpc = next((s for s in segs if s.lower().split("-")[0].split("_")[0] in VERB_SEGMENTS), "")
        called = (f"{and_list(self.name(x) for x in callers)} call{'s' if len(callers) == 1 else ''} it"
                  if callers else "nothing in the repository calls it yet")
        if method in STATE_CHANGING:
            why = {"POST": "it creates or acts: validate the body, authorize the caller, and decide what a retried "
                           "request does",
                   "PUT": "it replaces state: authorize per object; a PUT is expected to be idempotent",
                   "PATCH": "it updates state: authorize per object and validate partial input",
                   "DELETE": "it destroys state: authorize per object; retries should be harmless"}[method]
            ask = f"Who may call {route}, and how is it authenticated?"
            weight = 7
        else:
            why = "a read: cache it if it is hot, paginate it if it lists"
            ask, weight = "", 5
        if rpc:
            why += f"; '/{rpc}' names an action, not a resource, unlike a REST path"
        return _note("+", "added", f"{O} now answers {route}", f"{why} ({called})", ask, parts=[owner], weight=weight)

    def callers_of(self, url: str) -> List[str]:
        """The parts whose API calls (bp["uses"] of an API edge) name this path, path parameters matching anything."""
        want = [s for s in url.strip("/").split("/") if s]
        out = []
        for key, paths in self.uses.items():
            if key not in self.b["api"]:
                continue
            for p in paths:
                have = [s for s in p.split("?")[0].strip("/").split("/") if s]
                if len(have) >= len(want) and all(w.startswith("{") or h.startswith("{") or h == w
                                                  for w, h in zip(want[::-1], have[::-1])):
                    out.append(key.split(">")[0])
                    break
        return sorted(set(out))

    def external(self, c: dict) -> Dict[str, Any]:
        user, ext = c["id"].split(">")
        U, X = self.name(user), self.name("x:" + ext)
        db = (self.b["externals"].get(ext) or self.a["externals"].get(ext) or {}).get("shape") == "db"
        if c["op"] == "remove":
            return _note("−", "removed", f"{U} stops using {X}", "one fewer runtime dependency to keep up, pay for "
                         "and secure", parts=[user], weight=3)
        entries = self.entry_paths(user) if self.kind.get(user) not in ("ui", "interface") else [user]
        if db:
            sharing = sorted(u for u in (self.b["externals"].get(ext, {}).get("used_by") or []) if u != user)
            why = (f"shared database: {and_list(self.name(s) for s in sharing)} use it too, so who owns which "
                   "tables, and who may change them, is now a question" if sharing else
                   f"{U} now holds state in {X}: transactions, connection pooling and migrations are its concern")
            return _note("+", "added", f"{U} now reads and writes {X}", why, "Which part owns this data?" if sharing
                         else "", parts=[user, "x:" + ext], weight=6 if sharing else 5)
        on_path = (f"inside requests to {and_list(self.name(e) for e in entries)}: its latency and outages become "
                   "theirs" if entries else "off the request path, so a failure there stays contained")
        return _note("+", "warn" if entries else "added", f"{U} now talks to {X}",
                     f"a new runtime dependency, {on_path}; it needs a timeout, retries with backoff, and its "
                     "secrets set wherever the system runs", f"What should happen when {X} is slow or down?",
                     parts=[user, "x:" + ext], weight=7 if entries else 6)

    def table(self, c: dict) -> Dict[str, Any]:
        t = c["label"]
        if c["op"] == "add":
            return _note("+", "added", f"New table {t}", "a contract with stored data from its first row: its "
                         "migration runs once against production, so plan defaults, indexes and who owns it",
                         "Which part owns this table?", weight=6)
        if c["op"] == "remove":
            return _note("−", "removed", f"Table {t} dropped", "irreversible on real data, and code still running "
                         "during the deploy that reads it fails", "Is its data migrated or backed up first?",
                         weight=9)
        cols = ", ".join(x.replace("+ column ", "") for x in c["detail"])
        return _note("~", "changed", f"{t} gains {cols}", "additive and safe if the column is nullable or has a "
                     "default; old and new code must both work while it rolls out", weight=5)

    def part(self, c: dict) -> Dict[str, Any]:
        cid, P = c["id"], c["label"]
        if c["op"] == "remove":
            was_used = sorted(s for s, ds in self.adj_a.items() if cid in ds and s in self.b["components"])
            return _note("−", "removed", f"The part {P} is gone",
                         f"{and_list(self.name(s) for s in was_used)} used it" if was_used else
                         "nothing else used it", parts=[cid], weight=7)
        if c["op"] == "change":
            return None
        uses = sorted(self.adj.get(cid, ()))
        used = sorted(s for s, ds in self.adj.items() if cid in ds)
        where = (f"between {and_list(self.name(u) for u in used)} and {and_list(self.name(u) for u in uses)}"
                 if used and uses else f"used by {and_list(self.name(u) for u in used)}" if used
                 else f"on top of {and_list(self.name(u) for u in uses)}, nothing uses it yet" if uses
                 else "standing alone: nothing uses it yet")
        return _note("+", "added", f"A new part: {P} ({self.layer(cid)})", f"a new boundary, {where}",
                     parts=[cid], weight=5)

    def settings(self, c: dict) -> Dict[str, Any]:
        new = [v.strip() for v in c["detail"][0].split(",")] if c["detail"] else []
        unset = [v for v in new if self.configured and v not in self.configured]
        if unset:
            return _note("+", "warn", f"{c['label']} now reads {and_list(new, 4)}",
                         f"no deploy configuration sets {and_list(unset, 3)}: where the system runs it is missing, "
                         "so the code falls back to a default or fails at start", "Where will "
                         f"{unset[0]} be set?", parts=[c["id"]], weight=7)
        return _note("+", "added", f"{c['label']} now reads {and_list(new, 4)}", "more configuration surface: "
                     "each environment must set it, and a wrong value only shows when the code runs",
                     parts=[c["id"]], weight=3)

    def container(self, c: dict) -> Dict[str, Any]:
        if c["op"] == "add":
            return _note("+", "added", f"A new container: {c['label']}", "a new deployed unit: its own health "
                         "checks, scaling, logs and secrets, and a network hop to everything it talks to",
                         weight=5)
        if c["op"] == "remove":
            return _note("−", "removed", f"The {c['label']} container goes", "whatever reached it over the "
                         "network now fails to connect", weight=6)
        ports = any(x.startswith("ports") for x in c["detail"])
        return _note("~", "changed", f"The {c['label']} container changes: {'; '.join(c['detail'][:2])}",
                     "its exposed ports are its public surface" if ports else "takes effect when deployed, not "
                     "when merged", weight=5 if ports else 4)

    def notes(self) -> List[Dict[str, Any]]:
        out = []
        for c in self.diff["changes"]:
            entity = c["entity"]
            note = (self.flow(c) if entity == "flow" else self.route(c) if entity == "route" else
                    self.external(c) if entity == "external" else self.table(c) if entity == "table" else
                    self.part(c) if entity == "component" else self.settings(c) if entity == "settings" else
                    self.container(c) if entity == "container" else None)
            if note:
                note["change"] = {k: c[k] for k in ("op", "entity", "id", "label", "detail")}
                out.append(note)
        return sorted(out, key=lambda n: -n["weight"])

    # ── one part's shape ──

    def shape(self, part: str) -> Dict[str, Any]:
        """How one part sits in the system: what it depends on and what depends on it (and whether that is the most
        in the system), the cycles it is in, the entry points whose requests reach it, the outside systems it
        reaches itself or through what it uses, and the dependencies that point up the layers."""
        if part.startswith("x:"):
            users = sorted(u for u, xs in self.ext.items() if part in xs)
            return {"uses": [], "used_by": users, "cycle": [], "entries": [], "outside": [], "upward": [],
                    "hub_out": False, "hub_in": False}
        uses = sorted(self.adj.get(part, ()))
        used_by = sorted(s for s, ds in self.adj.items() if part in ds)
        mine = reach(self.adj, part)
        cycle = sorted(p for p in mine if part in reach(self.adj, p))
        far = set(self.ext.get(part, ()))
        for p in mine:
            far |= self.ext.get(p, set())
        k = self.kind.get(part, "")
        upward = sorted(d for d in uses if k in RANK and self.kind.get(d) in RANK and RANK[self.kind[d]] < RANK[k])
        most_out = max((len(v) for v in self.adj.values()), default=0)
        most_in = max(self.into.values(), default=0)
        return {"uses": uses, "used_by": used_by, "cycle": cycle, "entries": self.entry_paths(part),
                "outside": sorted(far), "direct_outside": sorted(self.ext.get(part, ())), "upward": upward,
                "hub_out": len(uses) >= 5 and len(uses) == most_out,
                "hub_in": len(used_by) >= 4 and len(used_by) == most_in}

    def shape_notes(self, part: str) -> List[Dict[str, Any]]:
        """What a designer would point out about a part as it stands, the weightiest first (no change needed)."""
        s, P = self.shape(part), self.name(part)
        out = []
        if s["cycle"]:
            out.append(_note("◆", "warn", f"{P} is in a cycle with {and_list(self.name(p) for p in s['cycle'])}",
                             "they can only change, be tested and be released together", "Which way should these "
                             "dependencies point?", parts=[part, *s["cycle"]], weight=6))
        for d in s["upward"]:
            out.append(_note("◆", "warn", f"{P} ({self.layer(part)}) depends on {self.name(d)} ({self.layer(d)})",
                             "a dependency pointing up the layers", parts=[part, d], weight=5))
        if s.get("hub_out"):
            out.append(_note("◆", "info", f"{P} depends on {len(s['uses'])} parts, the most in the system",
                             "a hub: a change in any of them can break it; often a sign it does more than one job",
                             parts=[part], weight=4))
        if s.get("hub_in"):
            out.append(_note("◆", "info", f"{len(s['used_by'])} parts depend on {P}, the most in the system",
                             "its interface is load-bearing: a change to it ripples to all of them", parts=[part],
                             weight=4))
        return sorted(out, key=lambda n: -n["weight"])


    def principle(self, part: str) -> Optional[Tuple[str, str]]:
        """The design principle a part's place in the system calls for: (its role, what that asks of it)."""
        s, P = self.shape(part), self.name(part)
        k = self.kind.get(part, "")
        if part.startswith("x:"):
            return ("outside the system", f"{P} fails, slows down and changes on its own schedule; the parts that "
                    "use it own the timeouts, retries and fallbacks")
        if s.get("direct_outside"):
            far = and_list(self.name(x) for x in s["direct_outside"])
            return ("an adapter", f"{P} talks to {far}: keep that API, its errors and its limits inside {P}, so "
                    "the rest of the system never depends on them directly")
        if k in ("ui", "interface"):
            return ("an edge of the system", f"requests arrive at {P}: check input and identity here, turn errors "
                    "into answers callers understand, and keep the rules a layer in")
        if s.get("hub_in"):
            return ("a shared foundation", f"{len(s['used_by'])} parts depend on {P}: its interface is a contract, "
                    "easy to add to and expensive to change")
        if s.get("hub_out"):
            return ("a coordinator", f"{P} pulls {len(s['uses'])} parts together; if it also holds rules of its own, "
                    "it may be doing two jobs")
        if k == "data":
            return ("data access", f"{P} should be the only place that knows how the data is stored, so a change "
                    "of storage stays in it")
        if not s["uses"]:
            return ("a leaf", f"{P} depends on nothing in the system: the easiest part to change and to test alone")
        return ("logic", f"{P} holds rules between the entry points and the data: independent of how requests arrive "
                "and how data is kept, which is what keeps it testable")


def notes(cmp) -> List[Dict[str, Any]]:
    return Reading(cmp).notes()


# ── many changes, said as a few ──────────────────────────────────────────────

CLAUSES = (("new", ""), ("gone", ""), ("uses", "now uses"), ("unuses", "no longer uses"),
           ("answers", "now answers"), ("unanswers", "no longer answers"), ("talks", "now talks to"),
           ("stops", "stops using"), ("reads", "now reads"), ("table+", "new table"), ("table-", "drops table"),
           ("table~", ""), ("migration", "migration"), ("container", ""))


def _clause(ch: Dict[str, Any]) -> Tuple[str, str, str]:
    """(who it is about, what kind of change, its object), from the change a note was made from."""
    op, entity, label, detail = ch["op"], ch["entity"], ch["label"], ch.get("detail") or []
    a, _, b = label.partition(" → ")
    if entity == "flow":
        return a, "uses" if op == "add" else "unuses", b
    if entity == "route":
        return (detail[0] if detail else "The system"), "answers" if op == "add" else "unanswers", label
    if entity == "external":
        return a, "talks" if op == "add" else "stops", b
    if entity == "component":
        return label, {"add": "new", "remove": "gone"}.get(op, "changed"), ""
    if entity == "settings":
        return label, "reads", detail[0] if detail else ""
    if entity == "table":
        return "Stored data", {"add": "table+", "remove": "table-"}.get(op, "table~"), (
            label + " gains " + ", ".join(x.replace("+ column ", "") for x in detail) if op == "change" else label)
    if entity == "migration":
        return "Stored data", "migration", label
    return "Deployment", "container", {"add": "new container ", "remove": "drops container "}.get(op, "changes ") + label


def summarize(notes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Design notes said a part at a time, the way a person tells it: "New part Billing, using Orders and Store",
    "API now answers POST /refund; now talks to Stripe", each with the weightiest note's why and question, the
    weightiest part first. A note without its change is kept as it is."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for n in notes:
        key = _clause(n["change"])[0] if n.get("change") else "\0" + n["text"]
        groups.setdefault(key, []).append(n)
    out = []
    for who, ns in groups.items():
        top = max(ns, key=lambda n: n["weight"])
        if who.startswith("\0"):
            out.append({**top, "count": 1})
            continue
        found: Dict[str, List[str]] = {}
        for n in ns:
            _who, kind, obj = _clause(n["change"])
            if obj and obj not in found.setdefault(kind, []):
                found[kind].append(obj)
            found.setdefault(kind, [])
        clauses = []
        for kind, verb in CLAUSES:
            if kind in found and kind not in ("new", "gone"):
                objs = found[kind]
                most = 2 if kind in ("answers", "unanswers") else 3
                clauses.append((verb + " " if verb else "") + and_list(objs, most) if objs else verb)
        if "new" in found:
            text = f"New part {who}" + (", using " + and_list(found.get("uses", []), 3) if found.get("uses") else "")
            rest = [c for c in clauses if not c.startswith("now uses")]
            text += ("; " + "; ".join(rest)) if rest else ""
        elif "gone" in found:
            text = f"{who} is gone"
        elif who in ("Stored data", "Deployment"):
            text = f"{who}: " + "; ".join(clauses)
        else:
            text = f"{who} " + "; ".join(clauses)
        statuses = {n["status"] for n in ns}
        status = ("warn" if "warn" in statuses else "added" if statuses <= {"added"} else
                  "removed" if statuses <= {"removed"} else "changed")
        out.append({"sign": {"added": "+", "removed": "−", "warn": "▲"}.get(status, "~"), "status": status,
                    "text": text, "why": top["why"], "ask": top.get("ask", ""), "weight": top["weight"],
                    "count": len(ns), "parts": top.get("parts", [])})
    return sorted(out, key=lambda n: -n["weight"])
