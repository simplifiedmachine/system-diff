"""Each block of a diagram explained the way a good engineer explains a system to someone new to it: what it is FOR,
how it works and fits, what is worth knowing about it, and the facts behind that, summarised.

    explain.blocks(c, model)  {"b:<id>": explanation} for every box and item of a diagram's model: a part, a container,
                              an actor, an outside system, or a file of a part zoomed in on (its node's "facts",
                              inside.py). Cheap: from the blueprint and the model only, nothing is read again.
    explain.ensure(root, cfg, explained)
                              starts the background job that asks the person's own LLM for better words for the
                              blocks that have none yet (rate-limited, locked, detached; never waits, never raises).

An explanation:

    {"kind": "part|container|actor|outside|file", "title", "headline": one plain sentence on what it is for,
     "how": [1-3 sentences: who uses it, what it does with what, where data goes],
     "insights": [0-3 "worth knowing" points], "facts": [{"title", "summary", "items"}], "inside": "Click to look
     inside …" (when it can be zoomed into), "by": "" (rule-based) or the model that wrote headline/how/insights}

Deterministic first: every sentence is built by rules from the structure, in words someone who does not know the
codebase can read (no block ids, no lists of setting names in the main text; settings summarised by pattern, secrets
counted, never named or shown). Then, in the background, an LLM (narrate.complete: Claude Code's gateway, one set in
the shell, or the `claude` CLI; none with SYSTEM_DIFF_LLM=off or SYSTEM_DIFF_EXPLAIN=0) is sent each block's structure
(names, roles, endpoints, tables, setting group labels, outside systems, file names, one-line descriptions already
read; never code, never a setting's value), ten blocks a question, and asked for a teacher's headline, how and
insights. An answer is checked (lengths, no ids, nothing named that the structure does not have) and kept in
<git dir>/system-diff/explain/cache.json by a hash of the block's structure: asked again only when the block changes.
LLM-written text is marked ✦ where it is shown; the rule-based text stands until it exists.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import design, graph
from .extract import secret_name

MARK = "✦"
BATCH = 10               # blocks per question
BATCHES = 6              # questions per job
MIN_GAP = 120            # seconds between two jobs for one repository
RETRY = 24 * 3600        # a block asked about is asked again only after this long (it failed, say)
LOCK_STALE = 900
TIMEOUT = 150
KEEP = 2000              # explanations kept in the cache
HEADLINE_MAX = 220
HOW_MAX = 520
INSIGHT_MAX = 220
FACT_ITEMS = 400         # items a fact keeps (the page shows them behind "show all")

# ── words ────────────────────────────────────────────────────────────────────────────────────────────────────────

and_list = design.and_list


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _sentence(text: str) -> str:
    text = " ".join(str(text).split()).strip()
    if not text:
        return ""
    text = _cap(text)
    return text if text[-1] in ".!?…" else text + "."


def _plural(n: int, one: str, many: str = "") -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _short_table(t: str) -> str:
    return t.split(".", 1)[1] if t.startswith("public.") else t


# What a part is for, by the role its folder or name says (names.ROLE's words, and words found in its id).
PURPOSE = (
    (("api routes", "api", "routers", "router", "routes", "endpoints", "controllers", "handlers", "views"),
     "The system's door for web requests: it takes each request and passes it to the code that does the work"),
    (("service layer", "service_layer", "services", "service", "usecases", "use_cases", "application"),
     "Coordinates the work behind each request, asking the other parts for their piece"),
    (("domain logic", "domain", "entities", "business"),
     "Holds the system's core ideas and rules, apart from how they are stored or served"),
    (("database access", "db", "database", "repositories", "repository", "repos", "dal", "persistence"),
     "Reads and writes the data the system keeps"),
    (("storage", "files", "uploads", "blobs", "attachments"), "Puts and gets files for the rest of the system"),
    (("data models", "models", "model", "schemas", "schema", "dto"),
     "Describes the shape of the data the system works with"),
    (("migrations", "alembic"), "Changes the database's structure step by step, as the code evolves"),
    (("llm client", "llms", "llm", "ai", "genai", "prompts"),
     "Talks to language models (LLMs) on behalf of the rest of the system"),
    (("settings", "config", "configuration", "conf"), "Gathers the system's configuration in one place"),
    (("auth", "authentication", "security", "permissions"), "Checks who is calling and what they are allowed to do"),
    (("logging", "log", "logs", "telemetry", "monitoring", "observability"),
     "Records what the system does, for debugging and monitoring"),
    (("main program", "main", "server", "entrypoint", "__main__", "asgi", "wsgi"),
     "Starts the system: it creates the application and plugs its parts together"),
    (("workers", "worker", "jobs", "tasks", "celery", "scheduler", "cron"),
     "Does work in the background, outside the time of a request"),
    (("middleware",), "Runs on every request, before and after it reaches the code that handles it"),
    (("search", "index", "retrieval", "rag"), "Finds things: it looks up matching documents or records"),
    (("cache", "caching"), "Keeps recent answers close at hand so repeated work is fast"),
    (("utils", "util", "helpers", "common", "shared", "lib", "libs", "core"),
     "Small shared building blocks the other parts reuse"),
    (("cli", "commands", "scripts", "setup", "bootstrap", "tools", "manage"),
     "Commands someone runs by hand, for setting up or looking after the system"),
    (("web app", "frontend", "web", "ui", "client", "pages", "screens", "components"),
     "The screens people use in their browser"),
    (("tests", "test"), "Automated checks that the system keeps working"),
    (("__init__", "init"), "The package's own entry file: what the package offers when it is imported"),
    (("types", "interfaces", "contracts"), "Shared type definitions other parts agree on"),
)
KIND_PURPOSE = {"ui": "The part of the system people see and use", "interface": "A way into the system from outside",
                "data": "Looks after the data the system keeps", "logic": "Part of the system's inner logic"}

# What an outside system is, for a newcomer: by its label's words, then by its kind.
OUTSIDE_WHAT = (
    (("postgres",), "A relational database: where the system keeps its records, in tables, safely across restarts"),
    (("mysql", "mariadb"), "A relational database: where the system keeps its records, in tables"),
    (("sqlite",), "A database kept in a single file, with no server to run"),
    (("mongo",), "A document database: it keeps records as JSON-like documents"),
    (("redis", "valkey"), "An in-memory store: very fast, usually a cache, a queue or shared state between processes"),
    (("elasticsearch", "opensearch"),
     "A search engine: the system sends it questions in words and gets back the matching documents, ranked, fast"),
    (("openai", "anthropic", "claude", "bedrock", "vertex", "gemini", "mistral", "llm", "azure openai"),
     "A hosted language-model service: the system sends it prompts and gets generated text back"),
    (("s3", "minio", "blob", "gcs", "storage", "bucket"), "File storage: the system puts and gets files (objects) there"),
    (("kafka", "rabbitmq", "sqs", "sns", "nats", "pubsub", "queue", "broker", "kinesis"),
     "A message broker: parts hand each other work through it without waiting for one another"),
    (("smtp", "mail", "sendgrid", "ses", "postmark", "mailgun", "resend"), "An email service the system sends mail through"),
    (("stripe", "paypal", "adyen"), "A payments service"),
    (("sentry", "datadog", "newrelic", "honeycomb", "otel", "opentelemetry"),
     "A monitoring service: errors and measurements are sent there"),
)
OUTSIDE_KIND_WHAT = {"db": "A store of data outside the code", "search": "A search service",
                     "queue": "A message broker", "mail": "An email service", "storage": "File storage",
                     "": "An outside service the system calls over the network"}

# Settings summarised by what their names say: (label, words that end or make up the name), first match wins.
SETTING_KINDS = (
    ("addresses of other services", ("URL", "URI", "HOST", "HOSTNAME", "ENDPOINT", "ADDR", "ADDRESS", "BASE", "DSN",
                                     "SERVER", "DOMAIN")),
    ("timeouts and limits", ("TIMEOUT", "TTL", "RETRIES", "RETRY", "LIMIT", "MAX", "MIN", "SIZE", "BATCH",
                             "CONCURRENCY", "WORKERS", "INTERVAL", "DELAY", "THRESHOLD", "TOKENS", "RATE", "QUOTA")),
    ("ports", ("PORT",)),
    ("file and folder locations", ("DIR", "PATH", "FILE", "FOLDER", "DIRECTORY")),
    ("names of databases, indexes, buckets and queues", ("INDEX", "DB", "DATABASE", "BUCKET", "TABLE", "QUEUE",
                                                         "TOPIC", "COLLECTION", "SCHEMA", "CONTAINER")),
    ("feature switches", ("FEATURE", "ENABLE", "ENABLED", "DISABLE", "DISABLED", "FLAG", "USE")),
    ("logging and debugging", ("LOG", "LOGGING", "DEBUG", "VERBOSE", "TRACE", "SENTRY", "LEVEL")),
    ("model and deployment names", ("MODEL", "DEPLOYMENT", "ENGINE", "VERSION", "TEMPERATURE")),
    ("where it runs (environment, stage, region)", ("ENV", "ENVIRONMENT", "STAGE", "REGION", "ZONE", "PROFILE")),
)


def _tokens(name: str) -> List[str]:
    return [t for t in re.split(r"[^A-Z0-9]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).upper()) if t]


def _kind_of_setting(name: str) -> str:
    toks = _tokens(name)
    for label, words in SETTING_KINDS:
        if toks and toks[-1] in words:
            return label
    for label, words in SETTING_KINDS:
        if set(toks) & set(words):
            return label
    return ""


def settings_groups(names: Iterable[str], configured: Iterable[str] = ()) -> Dict[str, Any]:
    """Settings summarised by pattern: {"total", "secrets" (a count: never named), "deploy" (how many have no value in
    the repository), "groups": [{"label", "names"}], "others": [names]}. A group shares what its names say they are
    (addresses, timeouts…), with the ending they share when most share one ("…_APIM_URL")."""
    names = sorted(set(names))
    secrets = [n for n in names if secret_name(n)]
    rest = [n for n in names if n not in secrets]
    kinds: Dict[str, List[str]] = defaultdict(list)
    for n in rest:
        kinds[_kind_of_setting(n)].append(n)
    groups = []
    others = list(kinds.pop("", []))
    for label, held in kinds.items():
        if len(held) < 2:
            others += held
            continue
        ends = Counter("_".join(_tokens(n)[-2:]) for n in held if len(_tokens(n)) >= 3)
        end, k = ends.most_common(1)[0] if ends else ("", 0)
        tail = f" (…_{end})" if end and k * 2 > len(held) else ""
        groups.append({"label": f"{len(held)} {label}{tail}", "names": held})
    # what is left, by a first word several share (ES_INDEX, ES_HOST… -> "4 ES_… settings")
    firsts: Dict[str, List[str]] = defaultdict(list)
    for n in others:
        toks = _tokens(n)
        if len(toks) > 1:
            firsts[toks[0]].append(n)
    for head, held in sorted(firsts.items(), key=lambda kv: -len(kv[1])):
        if len(held) >= 3:
            groups.append({"label": f"{len(held)} {head}_… settings", "names": held})
            others = [n for n in others if n not in held]
    groups.sort(key=lambda g: -len(g["names"]))
    have = set(configured)
    return {"total": len(names), "secrets": len(secrets), "deploy": sum(1 for n in names if n not in have),
            "groups": groups, "others": sorted(others)}


def settings_line(s: Dict[str, Any], most: int = 3) -> str:
    """"36 settings: 12 addresses of other services (…_APIM_URL), 9 secrets (not shown), 6 timeouts and limits and
    9 others"."""
    bits = [g["label"] for g in s["groups"][:most]]
    if s["secrets"]:
        bits.insert(min(1, len(bits)), _plural(s["secrets"], "secret") + " (API keys, tokens, passwords: not shown)")
    shown = sum(len(g["names"]) for g in s["groups"][:most]) + s["secrets"]
    if s["total"] - shown > 0 and bits:
        bits.append(f"{s['total'] - shown} other{'' if s['total'] - shown == 1 else 's'}")
    head = _plural(s["total"], "setting")
    if s["total"] <= 3:
        named = [n for g in s["groups"] for n in g["names"]] + s["others"]
        bits = named + ([_plural(s["secrets"], "secret") + " (not shown)"] if s["secrets"] else [])
        return head + ": " + and_list(bits, 4)
    return head + (": " + and_list(bits, 6) if bits and (len(bits) > 1 or s["total"] > 1) else "")


def settings_items(s: Dict[str, Any]) -> List[str]:
    """The non-secret names, by group, for the details' "show all"."""
    out = []
    for g in s["groups"]:
        out.append(f"{g['label']}: " + ", ".join(g["names"]))
    if s["others"]:
        out.append("Others: " + ", ".join(s["others"]))
    if s["secrets"]:
        out.append(_plural(s["secrets"], "secret") + ": names and values not shown")
    return out


def route_groups(routes: List[str]) -> List[Tuple[str, List[str]]]:
    """Endpoints by the first segment of their path: [("/chat", [...]), ("/health", [...])], largest first."""
    by: Dict[str, List[str]] = defaultdict(list)
    for r in routes:
        path = r.split(" ", 1)[-1]
        segs = [s for s in path.split("/") if s]
        lead = []
        for s_ in segs:                       # /api/v1/orders/{id}: grouped under /api/v1/orders
            lead.append(s_)
            if s_.startswith(("{", ":")):
                lead.pop()
                break
            if not re.fullmatch(r"api|v\d+|rest|public|internal", s_, re.I):
                break
        by["/" + "/".join(lead)].append(r)
    return sorted(by.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def routes_line(routes: List[str]) -> str:
    groups = route_groups(routes)
    head = _plural(len(routes), "endpoint")
    if len(groups) == 1:
        prefix, held = groups[0]
        return f"{head} under {prefix}: " + ", ".join(held[:3]) + (", …" if len(held) > 3 else "")
    bits = [f"{len(h)} under {p}" if len(h) > 1 else f"{h[0]}" for p, h in groups[:4]]
    return f"{head}: " + and_list(bits, 4)


def _topic_words(routes: List[str]) -> List[str]:
    """What the endpoints are about, as words: "chat", "health", "orders"."""
    out = []
    for prefix, _held in route_groups(routes):
        word = prefix.strip("/").rsplit("/", 1)[-1].replace("_", " ").replace("-", " ")
        if not word or re.fullmatch(r"api|v\d+|rest|public|internal", word, re.I):
            continue
        out.append(word)
    return out[:3]


# ── what the blueprint says about a block ─────────────────────────────────────────────────────────────────────────

class Facts:
    """Everything the rules need, worked out once per comparison and model."""

    def __init__(self, c, model: Dict[str, Any]):
        from . import names as names_
        from .architecture import protocol
        self.c, self.model = c, model
        self.bp, self.before, self.cfg = c.after, c.before, c.cfg
        bp = self.bp
        self.kind = design.kinds(bp, self.cfg)
        self.conns = graph.connections(bp)
        try:
            self.about = names_.about(bp)
        except Exception:  # noqa: BLE001
            self.about = {}
        self.labels: Dict[str, str] = {}
        for n in model.get("nodes") or []:
            self.labels[n["id"]] = str(n.get("title", n["id"])).split("  @")[0]
            for i in n.get("items") or []:
                self.labels[i["id"]] = str(i.get("title", i["id"])).split("  @")[0]
        self.via: Dict[tuple, str] = {}
        for e in model.get("edges") or []:
            for pair in e.get("pairs") or [[e["from"], e["to"]]]:
                if e.get("label") and not str(e["label"]).endswith("+"):
                    self.via.setdefault(tuple(pair), e["label"])
        for x, e in bp["externals"].items():
            for u in e.get("used_by") or []:
                self.via.setdefault((u, "x:" + x), protocol(e["label"], e.get("shape", "")))
        for key in bp.get("api") or {}:
            self.via.setdefault(tuple(key.split(">")), "HTTP")
        self.actor_flows = [(e["from"], e["to"], e.get("label", "")) for e in model.get("edges") or []
                            if str(e.get("from", "")).startswith("a:")]
        comps = bp["components"]
        self.callers: Dict[str, Set[str]] = defaultdict(set)
        self.callees: Dict[str, Set[str]] = defaultdict(set)
        for (s, t), (k, _n) in self.conns.items():
            if s != t and s in comps and t in comps:
                self.callers[t].add(s)
                self.callees[s].add(t)
        self.reach: Dict[str, Set[str]] = defaultdict(set)
        for x, e in bp["externals"].items():
            for u in e.get("used_by") or []:
                self.reach[u].add("x:" + x)
        self.running = [p for p, c_ in comps.items() if not c_.get("quiet")]

    def name(self, nid: str) -> str:
        return self.labels.get(nid) or design.name_of(nid, self.bp, self.before)

    def routes(self, inside: Set[str]) -> List[str]:
        return sorted((r for r, o in self.bp["routes"].items() if o in inside), key=lambda r: (r.split(" ", 1)[-1], r))

    def tables(self, inside: Iterable[str], k: Tuple[str, ...] = ("defines", "queries")) -> List[str]:
        pt = self.bp.get("part_tables") or {}
        return sorted({_short_table(t) for p in inside for kk in k for t in (pt.get(p) or {}).get(kk, [])})

    def settings(self, inside: Iterable[str]) -> List[str]:
        return sorted({s for p in inside for s in (self.bp.get("env") or {}).get(p, [])})

    def techs(self, inside: Iterable[str], most: int = 5) -> List[str]:
        """The technologies that define it: packages first (the standard library only when notable)."""
        from .learn import BASIC, STDLIB
        from .topics import TECH, TOP_PACKAGE, tech_key
        found: Counter = Counter()
        for p in inside:
            for name, n in ((self.bp.get("imported") or self.bp.get("stack") or {}).get(p) or {}).items():
                key = tech_key(name) or tech_key(TOP_PACKAGE.get(name, ""))
                if key and key not in BASIC and key != "os":
                    found[key] += int(n or 1)
        order = sorted(found, key=lambda k: (k.split(".")[0] in STDLIB, -found[k], k))
        return [TECH[k][0] for k in order if k in TECH][:most]

    def where(self, x: str) -> str:
        from .human import where_it_is
        conn = (self.bp.get("connections") or {}).get(x[2:]) or {}
        return where_it_is({k: v for k, v in conn.items() if k in ("host", "port", "database")})

    def how_reached(self, inside: Set[str], o: str) -> str:
        return next((self.via[k] for k in self.via if k[0] in inside and k[1] == o), "")


def _outside_kind(f: Facts, x: str) -> str:
    from .human import outside_kind
    e = f.bp["externals"].get(x[2:]) or f.before["externals"].get(x[2:]) or {}
    return outside_kind(x[2:], e.get("label", ""), e.get("shape", ""))


OUTSIDE_DOES = {"db": "keeps data in {n}", "search": "searches with {n}", "queue": "sends messages through {n}",
                "mail": "sends email through {n}", "storage": "stores files in {n}", "": "calls {n}"}


def _does(f: Facts, inside: Set[str], names_only: bool = False) -> List[str]:
    """What a part (or a container's parts) does, as short verb phrases, the most telling first."""
    out = []
    routes = f.routes(inside)
    if routes:
        topics = _topic_words(routes)
        out.append(f"answers {and_list(topics, 3)} requests" if topics else f"answers {_plural(len(routes), 'request')}"
                   f" from outside")
    defined = f.tables(inside, ("defines",))
    for o in sorted({x for p in inside for x in f.reach.get(p, ())}):
        k = _outside_kind(f, o)
        if k == "db" and defined and o[2:] == f.bp.get("store"):
            out.append(f"keeps {and_list(defined, 2)} in {f.name(o)}")
        else:
            out.append(OUTSIDE_DOES.get(k, OUTSIDE_DOES[""]).format(n=f.name(o)))
    if defined and not any(p.startswith("keeps ") for p in out):
        out.append(f"defines the {and_list(defined, 2)} table{'s' if len(defined) > 1 else ''}")
    return out


BASE_VERB = {"answers": "answer", "keeps": "keep", "searches": "search", "calls": "call", "sends": "send",
             "stores": "store", "defines": "define", "reads": "read", "talks": "talk"}


def _base_verb(phrase: str) -> str:
    """"keeps data in X" -> "keep data in X": what several parts do together."""
    head, _, rest = phrase.partition(" ")
    return (BASE_VERB.get(head, head) + (" " + rest if rest else "")).strip()


def _role_purpose(f: Facts, nid: str, title: str) -> str:
    """What a part is for, by its name (whole, then its folder, then any word of it)."""
    from .names import WRAPPERS
    segs = [s_.lower() for s_ in re.sub(r"\.(py|ts|tsx|js|jsx)$", "", nid).replace("-", "_").split("/") if s_]
    while len(segs) > 1 and segs[-1] in WRAPPERS:
        segs.pop()
    for words in ({title.lower()}, set(segs[-1:]), set(re.split(r"[\s_]+", title.lower()))):
        for keys, said in PURPOSE:
            if words & set(keys):
                return said
    return ""


# ── the explanations ─────────────────────────────────────────────────────────────────────────────────────────────

def _fact(title: str, summary: str, items: Iterable[str] = ()) -> Dict[str, Any]:
    return {"title": title, "summary": summary, "items": list(items)[:FACT_ITEMS]}


def _ex(kind: str, title: str, headline: str, how: List[str], insights: List[str], facts: List[dict],
        ask: Dict[str, Any], inside: str = "") -> Dict[str, Any]:
    return {"kind": kind, "title": title, "headline": _sentence(headline),
            "how": [_sentence(h) for h in how if h][:3], "insights": [_sentence(i) for i in insights if i][:3],
            "facts": [x for x in facts if x["summary"] or x["items"]], "inside": inside, "by": "", "_ask": ask}


def _part(f: Facts, nid: str) -> Dict[str, Any]:
    bp = f.bp
    comp = bp["components"].get(nid) or f.before["components"].get(nid) or {}
    title = f.name(nid)
    mine = {nid}
    routes = f.routes(mine)
    defined = f.tables(mine, ("defines",))
    queried = [t for t in f.tables(mine) if t not in defined]
    settings = f.settings(mine)
    S = settings_groups(settings, bp.get("configured") or [])
    outs = sorted(f.reach.get(nid, ()))
    callers = sorted(f.callers.get(nid, ()))
    callees = sorted(f.callees.get(nid, ()))
    actors = sorted({a for a, t, _l in f.actor_flows if t == nid})
    said = f.about.get(nid) or next((x.get("about", "") for x in f.cfg.get("components", [])
                                     if isinstance(x, dict) and x.get("id") == nid), "")
    purpose = said if said and said.lower() != title.lower() else ""
    generic = _role_purpose(f, nid, title)
    does = _does(f, mine)
    if not purpose:
        if settings and len(settings) >= 8 and not routes and not defined and generic.startswith("Gathers"):
            purpose = generic
        elif generic:
            purpose = generic
        elif does:
            purpose = _cap(does[0])
            does = does[1:]
        else:
            purpose = KIND_PURPOSE.get(f.kind.get(nid, ""), "A part of the system")
    spec = ""
    if purpose.startswith("Gathers") and settings:
        spec = f"{_plural(len(settings), 'setting')} read from the environment"
    elif does and not purpose.lower().startswith(does[0][:12].lower()):
        spec = "it " + and_list(does, 2)
    headline = purpose + (f": {spec}" if spec and ":" not in purpose else f" ({spec})" if spec else "")

    how = []
    users = [f.name(a) for a in actors] + [f.name(p) for p in callers]
    over = next((lab for a, t, lab in f.actor_flows if t == nid and lab), "")
    inner = and_list([f.name(p) for p in callers], 3)
    uses_it = f"{inner} use{'s' if len(callers) == 1 else ''} it"
    if actors:
        way = (" in a browser" if over.lower() == "browser" else f" over {over}") if over else ""
        how.append(f"{and_list([f.name(a) for a in actors], 2)} reach it from outside{way}"
                   + (f", and {uses_it}" if callers else ""))
    elif callers:
        how.append(uses_it)
    if callees:
        how.append(f"To do its job it relies on {and_list([f.name(p) for p in callees], 3)}")
    if outs:
        bits = []
        for o in outs[:3]:
            proto = f.how_reached(mine, o)
            where = f.where(o)
            inner = ([f"over {proto}"] if proto and proto.lower() not in f.name(o).lower() else []) + (
                [f"at {where}"] if where else [])
            bits.append(f.name(o) + (f" ({', '.join(inner)})" if inner else ""))
        how.append(f"Outside the code, it talks to {and_list(bits, 3)}")
    if not users and not callees and not outs:
        how.append("Nothing else in the system uses it and it uses nothing: it runs on its own (a script or a "
                   "tool) or is not used yet")
    elif not users and not actors and f.kind.get(nid) != "interface":
        how.append("Nothing in the system calls it: it is started on its own (a command, a script or the server)")

    insights = _insights(f, mine, title, routes, defined, S, outs, callers, actors, comp)
    facts = _facts(f, mine, routes, defined, queried, S, outs, callers + actors, callees, comp)
    ask = {"kind": "part", "name": title, "says_about_itself": said, "role": generic, "does": _does(f, mine),
           "files": comp.get("files", 0), "lines": comp.get("lines", 0), "endpoints": routes[:12],
           "endpoint_count": len(routes), "tables": defined + queried, "settings": _ask_settings(S),
           "outside": [f"{f.name(o)} ({_outside_kind(f, o) or 'service'})" for o in outs],
           "used_by": users, "uses": [f.name(p) for p in callees], "technologies": f.techs(mine)}
    return _ex("part", title, headline, how, insights, facts, ask, inside=f"Click to look inside {title}")


def _insights(f: Facts, inside: Set[str], title: str, routes: List[str], defined: List[str], S: Dict[str, Any],
              outs: List[str], callers: List[str], actors: List[str], comp: Dict[str, Any]) -> List[str]:
    """What a senior engineer would point out, most important first."""
    found: List[Tuple[int, str]] = []
    n_parts = max(1, len(f.running))
    if S["total"] >= 10:
        top = S["groups"][0]["label"] if S["groups"] else ""
        found.append((80, f"Configuration-heavy: it reads {S['total']} settings"
                          + (f", among them {top}" if top else "")
                          + ". A missing or wrong value only shows up when it runs"))
    if S["total"] and S["deploy"] * 2 >= S["total"] and S["deploy"] >= 3:
        found.append((70, f"{S['deploy']} of its {S['total']} settings have no value in the repository: they are set "
                          "where it is deployed, so running it on your machine means getting them from someone"))
    if S["secrets"]:
        found.append((60, f"It needs {_plural(S['secrets'], 'secret')} (keys, tokens or passwords) from the "
                          "environment: never put them in the code or print them in logs"))
    if actors and routes:
        entry = [p for p in f.running if any(t == p for _a, t, _l in f.actor_flows)]
        if len(entry) == 1:
            found.append((90, f"The single front door: every outside request comes in through its "
                              f"{_plural(len(routes), 'endpoint')}, so a change here affects every caller"))
        else:
            found.append((75, f"Exposed to the outside: its {_plural(len(routes), 'endpoint')} can be called by "
                              "anyone who reaches the server, so each one needs its input checked"))
    if len(callers) >= 3 and len(callers) * 3 >= n_parts:
        found.append((85, f"A shared foundation: {len(callers)} other parts depend on it, so a change here ripples "
                          "through most of the system"))
    elif len(callers) >= 3:
        found.append((50, f"{len(callers)} other parts depend on it: change what it offers with care"))
    if len(outs) >= 3:
        found.append((65, f"Depends on {len(outs)} outside systems ({and_list([f.name(o) for o in outs], 3)}): if "
                          "any of them is slow or down, this is where it shows"))
    if defined:
        store = f.name("x:" + f.bp["store"]) if f.bp.get("store") else "the database"
        found.append((68, f"The data is owned here: the {and_list(defined, 3)} table{'s are' if len(defined) > 1 else ' is'}"
                          f" defined here, so changing {'their' if len(defined) > 1 else 'its'} shape means a "
                          f"migration of {store}"))
    cycle = _cycle_partners(f, inside)
    if cycle:
        if len(inside) > 1:
            found.append((45, f"Its parts and {and_list([f.name(p) for p in cycle], 2)} call into each other both "
                              "ways: a change on either side can break the other"))
        else:
            found.append((72, f"It and {and_list([f.name(p) for p in cycle], 2)} depend on each other (a cycle): "
                              "neither can be changed or tested on its own"))
    files, lines = comp.get("files", 0), comp.get("lines", 0)
    if lines >= 3000 or files >= 30:
        found.append((40, f"Large: {_plural(files, 'file')} and {lines:,} lines. Zoom in to see how it is organised"))
    found.sort(key=lambda t: -t[0])
    return [t for _s, t in found[:3]]


def _cycle_partners(f: Facts, inside: Set[str]) -> List[str]:
    out = set()
    for o in inside:
        for p in f.callees.get(o, ()):
            if p not in inside and inside & f.callees.get(p, set()):
                out.add(p)
    return sorted(out)


def _facts(f: Facts, inside: Set[str], routes: List[str], defined: List[str], queried: List[str], S: Dict[str, Any],
           outs: List[str], users: List[str], uses: List[str], comp: Dict[str, Any]) -> List[dict]:
    from .human import connection_short
    facts = []
    if routes:
        facts.append(_fact("Endpoints", routes_line(routes), routes if len(routes) > 3 else []))
    if defined or queried:
        bits = ([f"defines {and_list(defined, 4)}"] if defined else []) + (
            [f"reads and writes {and_list(queried, 4)}"] if queried else [])
        facts.append(_fact("Data", "; ".join(bits), defined + queried if len(defined + queried) > 4 else []))
    if outs:
        lines = []
        for o in outs:
            conn = (f.bp.get("connections") or {}).get(o[2:]) or {}
            what = _outside_what(f, o).split(":")[0]
            where = connection_short(conn) or "where: set at deploy time"
            lines.append(f"{f.name(o)}: {what.lower()}, {where}")
        facts.append(_fact("Outside systems", lines[0] if len(lines) == 1 else _plural(len(lines), "outside system")
                           + ": " + and_list([f.name(o) for o in outs], 4), lines if len(lines) > 1 else []))
    if S["total"]:
        facts.append(_fact("Settings", settings_line(S), settings_items(S) if S["total"] > 3 or S["secrets"] else []))
    techs = f.techs(inside)
    if techs:
        facts.append(_fact("Built with", and_list(techs, 5)))
    if users:
        facts.append(_fact("Used by", and_list([f.name(p) for p in users], 4),
                           [f.name(p) for p in users] if len(users) > 4 else []))
    if uses:
        facts.append(_fact("Uses", and_list([f.name(p) for p in uses], 4),
                           [f.name(p) for p in uses] if len(uses) > 4 else []))
    files, lines = comp.get("files", 0), comp.get("lines", 0)
    if files:
        facts.append(_fact("Size", f"{_plural(files, 'file')}" + (f", {lines:,} lines" if lines else "")))
    return facts


def _ask_settings(S: Dict[str, Any]) -> List[str]:
    """The settings as the LLM sees them: group labels and a few names, never a secret's name, never a value."""
    out = [g["label"] + " e.g. " + ", ".join(g["names"][:2]) for g in S["groups"][:6]]
    if S["secrets"]:
        out.append(_plural(S["secrets"], "secret"))
    if S["others"]:
        out.append(f"{len(S['others'])} others e.g. " + ", ".join(S["others"][:4]))
    return out


def _outside_what(f: Facts, x: str) -> str:
    e = f.bp["externals"].get(x[2:]) or f.before["externals"].get(x[2:]) or {}
    words = f"{x[2:]} {e.get('label', '')}".lower()
    for keys, said in OUTSIDE_WHAT:
        if any(k in words for k in keys):
            return said
    return OUTSIDE_KIND_WHAT.get(_outside_kind(f, x), OUTSIDE_KIND_WHAT[""])


def _outside(f: Facts, nid: str) -> Dict[str, Any]:
    from .human import connection_lines, where_it_is
    bp = f.bp
    x = nid[2:]
    e = bp["externals"].get(x) or f.before["externals"].get(x) or {}
    conn = (bp.get("connections") or {}).get(x) or {}
    title = f.name(nid)
    users = sorted(e.get("used_by") or [])
    tables = list(bp["tables"]) if x == bp.get("store") else []
    what = _outside_what(f, nid)
    headline = what
    how = []
    if users:
        proto = next((f.via[k] for k in f.via if k[1] == nid and k[0] in users), "")
        how.append(f"{and_list([f.name(u) for u in users], 3)} talk{'s' if len(users) == 1 else ''} to it"
                   + (f" over {proto}" if proto and proto.lower() not in title.lower() else ""))
    if tables:
        how.append(f"The system keeps {_plural(len(tables), 'table')} in it: "
                   + and_list([_short_table(t) for t in tables], 3))
    where = where_it_is(conn)
    svc = conn.get("service") or {}
    if where and svc:
        how.append(f"For local runs it starts as the {svc.get('name')} service of "
                   f"{(svc.get('where') or 'the compose file').rsplit('/', 1)[-1]}, reached at {where}")
    elif where:
        how.append(f"It is reached at {where}")
    else:
        how.append("Where it runs is not in the repository: it is set where the system is deployed")
    insights: List[Tuple[int, str]] = []
    if not where:
        insights.append((70, "Its address is not in the repository: whoever deploys the system sets it, so ask them "
                             "where it is before running the system yourself"))
    secret = [s for s in conn.get("settings") or [] if s.get("secret")]
    if secret:
        insights.append((60, f"Reached with {_plural(len(secret), 'secret')} from the environment (a key or "
                             "password), not from the code"))
    if len(users) == 1:
        insights.append((50, f"Only {f.name(users[0])} talks to it directly: one place to change if it is ever "
                             "replaced"))
    elif len(users) >= 3:
        insights.append((65, f"{len(users)} parts talk to it directly: replacing it, or changing how it is reached, "
                             "touches each of them"))
    if svc:
        insights.append((40, "It runs locally from the compose file (docker compose up), so you can try the system "
                             "without the real one"))
    if tables:
        insights.append((55, "It holds the system's data: losing it or changing its tables affects everything that "
                             "reads them"))
    insights.sort(key=lambda t: -t[0])
    facts = []
    lines = connection_lines(conn, f.name) if conn else []
    if lines:
        facts.append(_fact("Where and how", lines[0], lines[1:]))
    if users:
        facts.append(_fact("Used by", and_list([f.name(u) for u in users], 4),
                           [f.name(u) for u in users] if len(users) > 4 else []))
    if tables:
        facts.append(_fact("Data", _plural(len(tables), "table") + ": "
                           + and_list([_short_table(t) for t in tables], 4),
                           [_short_table(t) for t in tables] if len(tables) > 4 else []))
    names = sorted({s["name"] for s in conn.get("settings") or []})
    if names:
        S = settings_groups(names, bp.get("configured") or [])
        facts.append(_fact("Settings", settings_line(S), settings_items(S) if S["total"] > 3 or S["secrets"] else []))
    if conn.get("clients"):
        facts.append(_fact("Through", and_list(conn["clients"], 4)))
    if e.get("found"):
        facts.append(_fact("How it was found", "; ".join(e["found"][:2]), e["found"] if len(e["found"]) > 2 else []))
    ask = {"kind": "outside system", "name": title, "what": what, "used_by": [f.name(u) for u in users],
           "tables": [_short_table(t) for t in tables][:20], "where": where or "set at deploy time",
           "local_service": bool(svc), "clients": conn.get("clients") or []}
    ex = _ex("outside", title, headline, how, [t for _s, t in insights[:3]], facts, ask,
             inside=f"Click to look at what talks to {title}")
    ex["where"] = where
    return ex


def _actor(f: Facts, nid: str, box: Dict[str, Any]) -> Dict[str, Any]:
    title = f.name(nid)
    uses = [(t, lab) for a, t, lab in f.actor_flows if a == nid]
    sub = str(box.get("sub") or "")
    over = next((lab for _t, lab in uses if lab), "")
    who = ("People" if "person" in sub or "people" in title.lower() or "user" in title.lower() else "Programs"
           if "program" in sub or "client" in title.lower() else "Someone")
    way = (" in a browser" if over.lower() == "browser" else f", over {over}") if over else ""
    headline = (f"{who} outside the system that use it{way}" if who != "Someone"
                else f"{title}: who uses the system from outside")
    how = [f"They come in through {and_list([f.name(t) for t, _l in uses], 3)}"] if uses else []
    routes = f.routes({t for t, _l in uses})
    insights = [f"All they can do goes through {_plural(len(routes), 'endpoint')}: that list is the system's "
                "surface towards them"] if routes else []
    facts = [_fact("Comes in through", and_list([f.name(t) + (f" over {lab}" if lab else "") for t, lab in uses], 4))
             ] if uses else []
    if routes:
        facts.append(_fact("Can call", routes_line(routes), routes if len(routes) > 3 else []))
    ask = {"kind": "who uses the system", "name": title, "comes_in_through": [f.name(t) for t, _l in uses],
           "over": over, "endpoint_count": len(routes)}
    return _ex("actor", title, headline, how, insights, facts, ask)


def _container(f: Facts, n: Dict[str, Any]) -> Dict[str, Any]:
    from .names import FOLD_TITLE
    nid = n["id"]
    title = f.name(nid)
    items = [i["id"] for i in n.get("items") or []]
    inside = set(items)
    bp = f.bp
    parts = [p for p in items if p in bp["components"]]
    ov = getattr(f.c, "regroup", None) or {}
    proposed = (ov.get("groups") or {}).get(nid[2:], "") if isinstance(ov, dict) else ""
    does = _does(f, inside)
    routes = f.routes(inside)
    defined = f.tables(inside, ("defines",))
    queried = [t for t in f.tables(inside) if t not in defined]
    S = settings_groups(f.settings(inside), bp.get("configured") or [])
    outs = sorted({o for p in inside for o in f.reach.get(p, ())})
    callers = sorted({s for p in inside for s in f.callers.get(p, ()) if s not in inside})
    callees = sorted({t for p in inside for t in f.callees.get(p, ()) if t not in inside})
    actors = sorted({a for a, t, _l in f.actor_flows if t in inside})
    if title == FOLD_TITLE:
        headline = "Documentation, examples and developer tools: kept in the repository, not part of the running system"
    elif proposed:
        headline = proposed
    elif does:
        headline = f"Groups the {len(items)} parts that together " + and_list([_base_verb(d) for d in does], 4)
    else:
        roles = [r for r in (_role_purpose(f, p, f.name(p)) for p in parts) if r]
        headline = (f"Groups {len(items)} parts that work together" + (f"; the main one {roles[0][0].lower()}"
                                                                         f"{roles[0][1:]}" if roles else ""))
    how = []
    if actors or callers:
        how.append(f"Requests come in from {and_list([f.name(a) for a in actors + callers], 3)}")
    # the part most others inside lean on, and the way in
    inner_callers = Counter(t for p in inside for t in f.callees.get(p, ()) if t in inside)
    entry = [p for p in parts if any(t == p for _a, t, _l in f.actor_flows) or f.routes({p})]
    if entry:
        how.append(f"The way in is {and_list([f.name(p) for p in entry], 2)}, which hands the work to the other parts")
    if inner_callers:
        hub, k = inner_callers.most_common(1)[0]
        if k >= 2:
            how.append(f"{f.name(hub)} is what most of the others inside rely on ({k} of them)")
    if outs and len(how) < 3:
        how.append(f"It reaches {and_list([f.name(o) for o in outs], 3)} outside the code")
    if callees and len(how) < 3:
        how.append(f"Outside this group it uses {and_list([f.name(p) for p in callees], 3)}")
    comp = {"files": sum((bp["components"].get(p) or {}).get("files", 0) for p in parts),
            "lines": sum((bp["components"].get(p) or {}).get("lines", 0) for p in parts)}
    insights = _insights(f, inside, title, routes, defined, S, outs, callers, actors, comp)
    hubs = [p for p, k in inner_callers.items() if k * 2 >= max(2, len(parts) - 1) and k >= 2]
    if hubs and len(insights) < 3:
        insights.append(f"{f.name(hubs[0])} is a choke point inside it: {inner_callers[hubs[0]]} of its parts go "
                        "through it")
    roles = []
    for p in items:
        r = f.about.get(p) or _role_purpose(f, p, f.name(p)) or (_cap(_does(f, {p})[0]) if _does(f, {p}) else "")
        roles.append(f"{f.name(p)}" + (f": {r.split(':')[0].split(';')[0]}" if r else ""))
    facts = [_fact("Parts", _plural(len(items), "part") + ": " + and_list([f.name(p) for p in items], 4), roles)]
    facts += _facts(f, inside, routes, defined, queried, S, outs, callers + actors, callees, comp)
    ask = {"kind": "container (a group of parts)", "name": title, "proposed_about": proposed,
           "parts": roles[:20], "does": does, "endpoint_count": len(routes), "endpoints": routes[:8],
           "tables": (defined + queried)[:20], "settings": _ask_settings(S),
           "outside": [f.name(o) for o in outs], "used_by": [f.name(a) for a in actors + callers],
           "uses": [f.name(p) for p in callees], "technologies": f.techs(inside)}
    return _ex("container", title, headline, how, insights, facts, ask, inside=f"Click to look inside {title}")


def _file(f: Facts, n: Dict[str, Any]) -> Dict[str, Any]:
    nid = n["id"]
    F = n.get("facts") or {}
    path = F.get("path") or nid[2:]
    title = str(n.get("title") or path.rsplit("/", 1)[-1])
    base = path.rsplit("/", 1)[-1]
    doc = (F.get("doc") or "").strip()
    classes = [x for x in F.get("classes") or [] if not x.startswith("_")]
    functions = [x for x in F.get("functions") or [] if not x.startswith("_")]
    routes = list(F.get("routes") or [])
    settings = list(F.get("settings") or [])
    imports = list(F.get("imports") or [])
    importers = list(F.get("importers") or [])
    lines = int(F.get("lines") or 0)
    outs = sorted({e["to"] for e in f.model.get("edges") or [] if e.get("from") == nid and str(e["to"]).startswith("x:")})
    S = settings_groups(settings, f.bp.get("configured") or [])
    defs = classes + functions
    if doc:
        headline = doc
    elif routes:
        topics = _topic_words(routes)
        many = "s" if len(routes) > 1 else ""
        headline = (f"Defines the {and_list(topics, 2)} endpoint{many}" if topics else f"Defines endpoint{many}") + ": " + \
            ", ".join(routes[:3]) + (", …" if len(routes) > 3 else "")
    elif base == "__init__.py" and not defs:
        headline = "Marks its folder as a Python package" + (" (and gathers what the package offers)" if lines > 2
                                                             else "; it holds no code of its own")
    elif S["total"] >= 5 and len(defs) <= 3:
        headline = f"Reads the configuration: {_plural(S['total'], 'setting')} from the environment"
    elif classes and not functions:
        headline = f"Defines {and_list(classes, 3)}"
    elif functions and not classes:
        headline = f"Helpers for {and_list([_spaced(x) for x in functions], 3)}"
    elif defs:
        headline = f"Defines {and_list(classes, 2)}, with {and_list(functions, 2)}"
    else:
        headline = f"A {'small ' if lines < 30 else ''}file of {_plural(lines, 'line')}"
    how = []
    callers = [(e["from"], e.get("label", "")) for e in f.model.get("edges") or []
               if e.get("to") == nid and str(e.get("from", "")).startswith("a:")]
    if callers:
        over = next((lab for _a, lab in callers if lab), "")
        how.append(f"{and_list([f.name(a) for a, _l in callers], 2)} reach it from outside"
                   + (f" over {over}" if over and over.lower() != "browser" else " in a browser" if over else ""))
    if importers:
        how.append(f"Used by {and_list(importers, 3)}")
    if imports:
        how.append(f"It builds on {and_list(imports, 3)}")
    if outs:
        how.append(f"It talks to {and_list([f.name(o) for o in outs], 3)}")
    if routes and doc:
        how.append(f"It serves {routes_line(routes)}")
    if S["total"] and len(how) < 3:
        how.append(f"It reads {settings_line(S, 2)}")
    if not how:
        how.append("Nothing else in its part imports it, and it imports nothing from the system")
    insights: List[Tuple[int, str]] = []
    if routes:
        insights.append((70, f"Exposed to the outside: {_plural(len(routes), 'endpoint')} anyone reaching the server "
                             "can call"))
    if len(importers) >= 3:
        insights.append((65, f"{len(importers)} files or parts depend on it: a change here spreads"))
    if lines >= 500:
        insights.append((50, f"A big file ({lines:,} lines): it probably does several jobs and could be split"))
    if S["secrets"]:
        insights.append((60, f"It reads {_plural(S['secrets'], 'secret')} from the environment"))
    if S["total"] >= 10:
        insights.append((55, f"Configuration-heavy: {S['total']} settings"))
    insights.sort(key=lambda t: -t[0])
    facts = []
    if defs:
        facts.append(_fact("Defines", and_list(defs, 5), defs if len(defs) > 5 else []))
    if routes:
        facts.append(_fact("Endpoints", routes_line(routes), routes if len(routes) > 3 else []))
    if imports:
        facts.append(_fact("Imports from the system", and_list(imports, 4), imports if len(imports) > 4 else []))
    if importers:
        facts.append(_fact("Imported by", and_list(importers, 4), importers if len(importers) > 4 else []))
    if S["total"]:
        facts.append(_fact("Settings", settings_line(S), settings_items(S) if S["total"] > 3 or S["secrets"] else []))
    facts.append(_fact("Size", _plural(lines, "line") + f" · {path}"))
    ask = {"kind": "file", "name": base, "docstring": doc, "classes": classes[:12], "functions": functions[:16],
           "endpoints": routes[:10], "imports": imports[:10], "imported_by": importers[:10],
           "settings": _ask_settings(S), "outside": [f.name(o) for o in outs], "lines": lines}
    return _ex("file", title, headline, how, [t for _s, t in insights[:3]], facts, ask)


def _spaced(name: str) -> str:
    """A function's name as words: "save_turn" -> "save turn", "getUser" -> "get user"."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").strip().lower() or name


# ── every block of a model, with the LLM's words where they exist ───────────────────────────────────────────────

_MEMO: List[Tuple[Any, Any, Dict[str, Dict[str, Any]]]] = []


def key_of(ask: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(ask, sort_keys=True, default=str).encode()).hexdigest()[:16]


def blocks(c, model: Dict[str, Any], root: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """{"b:<id>": explanation} for every box and item of `model` (see the top of this module), the LLM's words
    over the rules' where they were written for exactly this structure. Never raises for one block: a block the rules
    cannot explain is left out."""
    for mc, mm, got in _MEMO:
        if mc is c and mm is model:
            return got
    f = Facts(c, model)
    out: Dict[str, Dict[str, Any]] = {}
    for n in model.get("nodes") or []:
        for box in [n] + list(n.get("items") or []):
            nid = box["id"]
            try:
                if nid.startswith("a:"):
                    ex = _actor(f, nid, box)
                elif nid.startswith("x:"):
                    ex = _outside(f, nid)
                elif nid.startswith("f:"):
                    ex = _file(f, box)
                elif box is n and n.get("items"):
                    ex = _container(f, n)
                elif nid in f.bp["components"] or nid in f.before["components"]:
                    ex = _part(f, nid)
                else:
                    continue
            except Exception:  # noqa: BLE001 - one block's words; the others still come
                continue
            ex["key"] = key_of(ex["_ask"])
            out["b:" + nid] = ex
    root = root or getattr(c, "root", None)
    if root is not None:
        cache = _cached(Path(root))
        for ex in out.values():
            got = cache.get(ex["key"])
            if got:
                ex.update({"headline": got["headline"], "how": got["how"], "insights": got["insights"],
                           "by": got.get("by") or "an LLM", "rules": {"headline": ex["headline"]}})
    _MEMO.append((c, model, out))
    del _MEMO[:-4]
    return out


def public(ex: Dict[str, Any]) -> Dict[str, Any]:
    """An explanation as a page shows it: without what the LLM is sent."""
    return {k: v for k, v in ex.items() if not k.startswith("_") and k != "rules"}


def card_lines(ex: Dict[str, Any], most: int = 8) -> List[str]:
    """An explanation as plain lines (a hover card's fallback, the terminal): headline, how, insights as bullets."""
    mark = f" {MARK}" if ex.get("by") else ""
    lines = [ex["headline"] + mark] + [" ".join(ex["how"])] if ex.get("how") else [ex["headline"] + mark]
    lines += ["• " + i for i in ex.get("insights") or []]
    return [x for x in lines if x][:most]


def pane_text(ex: Dict[str, Any]) -> str:
    """The pane's detail: the headline, then the best insight (else how it works)."""
    mark = f" {MARK}" if ex.get("by") else ""
    more = (ex.get("insights") or ex.get("how") or [""])[0]
    if ex.get("kind") == "outside":    # an outside system: what it is, and where, first
        return (ex["headline"].split(":")[0].rstrip(".") + mark + ", "
                + (f"reached at {ex['where']}." if ex.get("where") else "set at deploy time.")
                + (" " + ex["insights"][0] if ex.get("insights") else ""))
    return (ex["headline"] + mark + (" " + more if more else "")).strip()


# ── the LLM's words: cache, job ───────────────────────────────────────────────────────────────────────────────────

SYSTEM = (
    "You are a senior engineer explaining a software system to a newcomer who is not an engineer of this codebase. "
    "For each block of its architecture diagram you are given only its structure, read from the code by a tool "
    "(names, roles, endpoints, tables, groups of setting names, outside systems, file names, one-line descriptions) "
    "and a rule-based draft. Write, in plain words a smart non-specialist understands: headline: one sentence on what "
    "the block is FOR, its job in the system (not its technology); how: one to three sentences on how it works and "
    "fits (who uses it, what it does with what, where the data goes); insights: zero to three short points worth "
    "knowing, the kind a senior engineer tells a newcomer (a risk, a design choice, where to be careful), each "
    "grounded in the structure given. Never name a component, file, table, endpoint, technology or outside system "
    "that is not in the block's structure; never invent numbers; no ids, no code, no setting values; no markdown. "
    "Answer with JSON only: {\"<block id>\": {\"headline\": \"...\", \"how\": \"...\", \"insights\": [\"...\"]}, ...}."
)

_READ: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_LOCK = threading.Lock()
_STARTED: Dict[str, float] = {}


def _dir(root: Path) -> Path:
    from .sources import git_dir
    return git_dir(Path(root), "explain")


def _cached(root: Path) -> Dict[str, Any]:
    """The cache (key -> {headline, how, insights, by, at}), read again only when its file changed."""
    try:
        f = _dir(root) / "cache.json"
        mtime = f.stat().st_mtime
    except OSError:
        return {}
    hit = _READ.get(str(f))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    _READ[str(f)] = (mtime, data)
    return data


def save(root: Path, got: Dict[str, Dict[str, Any]]) -> None:
    """Keep explanations by key, merged with what is there; the oldest let go past KEEP."""
    d = _dir(root)
    f = d / "cache.json"
    with _LOCK:
        try:
            have = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
            have = have if isinstance(have, dict) else {}
        except (OSError, ValueError):
            have = {}
        have.update(got)
        if len(have) > KEEP:
            have = dict(sorted(have.items(), key=lambda kv: kv[1].get("at", 0))[-KEEP:])
        tmp = d / f".cache.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(have, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)


def enabled() -> bool:
    from . import narrate
    return not narrate.off() and os.environ.get("SYSTEM_DIFF_EXPLAIN", "").strip().lower() not in (
        "0", "off", "false", "no")


def _in_test() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and os.environ.get("SYSTEM_DIFF_EXPLAIN_JOBS") != "1"


def _state(d: Path) -> Dict[str, Any]:
    try:
        s = json.loads((d / "state.json").read_text(encoding="utf-8"))
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def wanted(root: Path, explained: Dict[str, Dict[str, Any]], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The blocks with no LLM words for their structure that were not asked about lately: [{"id", "key", "ask",
    "draft"}], one per key."""
    now = time.time() if now is None else now
    cache = _cached(root)
    asked = _state(_dir(root)).get("asked") or {}
    out, seen = [], set()
    for bid, ex in explained.items():
        k = ex.get("key")
        if not k or k in cache or k in seen or now - float(asked.get(k, 0)) < RETRY:
            continue
        seen.add(k)
        out.append({"id": k, "key": k, "ask": ex["_ask"],
                    "draft": {"headline": ex["headline"], "how": " ".join(ex["how"]), "insights": ex["insights"]}})
    return out


def ensure(root: Path, cfg: Dict[str, Any], explained: Dict[str, Dict[str, Any]]) -> bool:
    """Start the background job for the blocks with no LLM words yet (see the top of this module). Never waits for
    the LLM, never raises. True if a job was started."""
    try:
        root = Path(root)
        if not enabled() or _in_test():
            return False
        now = time.time()
        if now - _STARTED.get(str(root), 0) < MIN_GAP:
            return False
        todo = wanted(root, explained, now)
        if not todo:
            return False
        _STARTED[str(root)] = now
        d = _dir(root)
        if now - float(_state(d).get("last", 0)) < MIN_GAP:
            return False
        from . import narrate
        if not narrate.configured(root, cfg.get("narrate", {}).get("settings", [])):
            return False
        if not _lock(d):
            return False
        try:
            job = d / f"job-{os.getpid()}-{int(now)}.json"
            job.write_text(json.dumps({"root": str(root), "blocks": todo[:BATCH * BATCHES],
                                       "settings": cfg.get("narrate", {}).get("settings", [])}, ensure_ascii=False),
                           encoding="utf-8")
            st = _state(d)
            st["last"] = now
            (d / "state.json").write_text(json.dumps(st), encoding="utf-8")
            threading.Thread(target=_spawn, args=(root, job), name="system-diff explain job").start()
            return True
        except Exception:  # noqa: BLE001
            _unlock(d)
            return False
    except Exception:  # noqa: BLE001 - only better words
        return False


def _lock(d: Path) -> bool:
    f = d / "lock"
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
                f.unlink()
            except OSError:
                return False
        except OSError:
            return False
    return False


def _unlock(d: Path) -> None:
    try:
        (d / "lock").unlink()
    except OSError:
        pass


def _spawn(root: Path, job: Path) -> None:
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                          "cwd": str(root), "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-m", "system_diff.explain", str(job)], **kw)
    except Exception:  # noqa: BLE001
        _unlock(job.parent)


def prompt(batch: List[Dict[str, Any]]) -> str:
    items = [{"id": b["id"], "structure": b["ask"], "draft": b["draft"]} for b in batch]
    return "Blocks:\n" + json.dumps(items, ensure_ascii=False, indent=1)


# names a reply may not use unless the block's structure has them: outside systems and technologies people know
_KNOWN_NAMES: Optional[Set[str]] = None


def _catalogue() -> Set[str]:
    global _KNOWN_NAMES
    if _KNOWN_NAMES is None:
        from .extract import OUTSIDE
        from .topics import TECH
        names = set()
        for row in OUTSIDE:
            if isinstance(row, (list, tuple)) and len(row) > 1 and isinstance(row[1], str):
                names.add(row[1])
        names |= {t[0] for t in TECH.values()}
        names |= {"PostgreSQL", "MySQL", "Redis", "MongoDB", "Kafka", "RabbitMQ", "Elasticsearch", "OpenSearch",
                  "OpenAI", "Anthropic", "Claude", "Bedrock", "Gemini", "S3", "DynamoDB", "SQS", "Celery", "Stripe",
                  "Django", "Flask", "FastAPI", "React", "Vue", "Angular", "GraphQL", "gRPC", "Kubernetes", "Docker"}
        _KNOWN_NAMES = {n for n in names if n and len(n) >= 3}
    return _KNOWN_NAMES


def _clean(v: Any, most: int) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = " ".join(v.replace("**", "").replace("`", "").split()).strip()
    if not v or len(v) > most:
        return None
    return _sentence(v)


ID_LIKE = re.compile(r"(?<![\w/])[gxaf]:[\w/]|\b\w+/\w+/\w+")


def validate(answer: Any, batch: List[Dict[str, Any]], by: str = "", now: Optional[float] = None
             ) -> Dict[str, Dict[str, Any]]:
    """The reply's explanations that pass, by key: a headline, how and up to three insights, each of a sane length,
    no ids, and nothing named (a known outside system or technology, a file) that the block's structure lacks."""
    if isinstance(answer, str):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer.strip())
        start, end = text.find("{"), text.rfind("}")
        try:
            answer = json.loads(text[start:end + 1]) if 0 <= start < end else {}
        except ValueError:
            answer = {}
    if not isinstance(answer, dict):
        return {}
    now = time.time() if now is None else now
    out: Dict[str, Dict[str, Any]] = {}
    for b in batch:
        got = answer.get(b["id"])
        if not isinstance(got, dict):
            continue
        headline = _clean(got.get("headline"), HEADLINE_MAX)
        how_in = got.get("how")
        how_in = " ".join(x for x in how_in if isinstance(x, str)) if isinstance(how_in, list) else how_in
        how = _clean(how_in, HOW_MAX)
        ins = got.get("insights") or []
        ins = [x for x in (_clean(i, INSIGHT_MAX) for i in (ins if isinstance(ins, list) else [ins])[:3]) if x]
        if not headline or not how:
            continue
        said = " ".join([headline, how] + ins)
        if ID_LIKE.search(said):
            continue
        known = json.dumps(b["ask"], ensure_ascii=False).lower() + " " + json.dumps(b["draft"]).lower()
        if any(re.search(r"(?<!\w)" + re.escape(n.lower()) + r"(?!\w)", said.lower()) and n.lower() not in known
               for n in _catalogue()):
            continue
        if any(fn.lower() not in known for fn in re.findall(r"\b[\w-]+\.(?:py|ts|tsx|js|jsx|go|rb|java)\b", said)):
            continue
        out[b["key"]] = {"headline": headline, "how": _split(how), "insights": ins, "by": by, "at": round(now, 3)}
    return out


def _split(how: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z])", how) if p.strip()]
    return parts[:3] or [how]


def ask_batch(root: Path, batch: List[Dict[str, Any]], settings: List[str] = (), ask=None
              ) -> Dict[str, Dict[str, Any]]:
    """One question for up to BATCH blocks; the explanations that pass, by key."""
    from . import narrate
    if ask is None:
        ask = lambda user: narrate.complete(user, SYSTEM, root, {"narrate": {"settings": list(settings)}},  # noqa
                                            TIMEOUT, limit=60000)
    try:
        got = ask(prompt(batch))
    except Exception:  # noqa: BLE001 - no answer: the rules' words stay
        got = None
    if not got:
        return {}
    return validate(got[0], batch, got[1])


def run(job: Path, ask=None, locked: bool = True) -> int:
    """The background job: ask for its blocks, ten a question; keep what passes; mark every block asked; free the lock."""
    d = job.parent
    try:
        spec = json.loads(job.read_text(encoding="utf-8"))
        root = Path(spec["root"])
        todo = spec.get("blocks") or []
        st = _state(d)
        now = time.time()
        asked = {k: t for k, t in (st.get("asked") or {}).items() if now - float(t) < RETRY}
        asked.update({b["key"]: now for b in todo})
        st["asked"] = asked
        (d / "state.json").write_text(json.dumps(st), encoding="utf-8")
        kept = 0
        for i in range(0, len(todo), BATCH):
            got = ask_batch(root, todo[i:i + BATCH], spec.get("settings", []), ask)
            if got:
                save(root, got)
                kept += len(got)
        return 0 if kept else 1
    finally:
        try:
            job.unlink()
        except OSError:
            pass
        if locked:
            _unlock(d)


def now(root: Path, c, model: Dict[str, Any], ask=None) -> int:
    """Ask now, in this process, for the model's blocks with no LLM words (a command, or a test): how many were kept."""
    root = Path(root)
    if not enabled():
        return 0
    explained = blocks(c, model, root)
    todo = wanted(root, explained)
    if not todo:
        return 0
    d = _dir(root)
    job = d / f"job-now-{os.getpid()}.json"
    job.write_text(json.dumps({"root": str(root), "blocks": todo[:BATCH * BATCHES]}, ensure_ascii=False),
                   encoding="utf-8")
    before = len(_cached(root))
    run(job, ask, locked=False)
    _MEMO.clear()
    return max(0, len(_cached(root)) - before)


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1])) if len(sys.argv) > 1 else 2)
