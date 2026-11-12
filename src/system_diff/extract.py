"""The system's structure as one JSON-able dict (a "blueprint"), read from the code of one commit or directory.

Observed, not declared: the imports between parts and what each brings over (`uses`: the names one part takes from
another, the API paths the front end calls on the back end), the outside packages each part imports (`stack`), the
routes (FastAPI and Flask decorators, Express-style servers, Next.js route files, each under the prefixes its routers
are mounted with), the settings each part reads (os.environ, process.env, pydantic settings classes), the settings a
deploy configuration sets (`configured`), the deployed containers (a compose file) and the tables (SQL migrations;
with none configured, also Alembic, SQLAlchemy, SQLModel and Django models, and Prisma schemas).

Declared in the config: what the parts are called, which column each belongs in, and which settings mean which outside
system. A declared outside system is drawn only when some deploy configuration sets one of its settings; read by the
code but configured nowhere, it is DORMANT: listed, not drawn. With none declared, the outside systems are found
instead: the databases, queues and mail servers the compose file runs, and the services whose client libraries the
code imports.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import progress
from .config import segment_rx

EXTRACT_VERSION = "3"  # what a blueprint records: a blueprint cached by another version is read again


def glob_rx(pattern: str) -> re.Pattern:
    """* stays within one directory, ** crosses them, ? is one character."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def matches(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_rx(p).match(path) for p in patterns)


PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+\(?([^)#\n]+)", re.M)
PY_FROM_LIST = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]*\(([^)]*)\)", re.M)  # (a,\n b) too
PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]*,[ \t]*[\w.]+)*)", re.M)
TS_IMPORT = re.compile(r"""(?:from\s+|import\s*\(\s*|require\(\s*)["']([^"']+)["']""")
TS_CLAUSE = re.compile(r"""\b(?:import|export)\s+(?:type\s+)?([\w$*{}\s,]*?)\s*\bfrom\s*["']([^"']+)["']""")
TS_REQUIRE = re.compile(r"""\b(?:const|let|var)\s+(\{[^}]*\}|[\w$]+)\s*=\s*require\(\s*["']([^"']+)["']""")
ENV_PY = re.compile(r"""(?:getenv|environ\.get|environ\.setdefault)\(\s*["']([A-Z][A-Z0-9_]+)["']|environ\[\s*["']([A-Z][A-Z0-9_]+)["']\s*\]""")
ENV_TS = re.compile(r"(?:import\.meta\.env|process\.env)\.([A-Z][A-Z0-9_]+)")
API_STR = re.compile(r"""["'`](/api/[^"'`\s]*)""")
CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)", re.I)
ADD_COLUMN = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([\w.\"]+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?(\w+)", re.I)
SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
CONFIG_KEY = re.compile(r"^\s*(?:-\s*)?\"?(?:export\s+)?([A-Z][A-Z0-9_]+)\s*[=:]", re.M)
TS_EXT = (".ts", ".tsx", ".js", ".jsx")

# Routes. A Python router or app (`router = APIRouter(prefix=...)`), a decorator on it, one router mounted in another
# (`app.include_router(api_router, prefix=settings.API_V1_STR)`), and the string constants such a prefix may name.
PY_ROUTE = re.compile(r"""^[ \t]*@(\w+)\.(get|post|put|patch|delete)\(\s*[rfb]?["']((?:/[^"']*)?)["']""", re.M)
FLASK_ROUTE = re.compile(r"""^[ \t]*@(\w+)\.route\(\s*[rfb]?["'](/[^"']*)["']([^)]*)\)""", re.M)
ROUTER = re.compile(r"""^[ \t]*(\w+)\s*(?::[^=\n]+)?=\s*(?:\w+\.)?(APIRouter|FastAPI|Blueprint|APIBlueprint|Flask)\(([^)]*)\)""",
                    re.M)
MOUNT = re.compile(r"""\b(\w+)\.(include_router|register_blueprint)\(\s*([\w.]+)\s*(?:,([^)]*))?\)""")
PREFIX_ARG = re.compile(r"""\b(url_prefix|prefix)\s*=\s*([^,)\n]+)""")
CONSTANT = re.compile(r"""^[ \t]*([A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*[rbu]?["']([^"'\n]*)["'][ \t]*(?:#.*)?$""", re.M)
# The same in a JavaScript server (Express, Hono, Koa…), and Next.js, whose routes are files.
JS_SERVERS = ("express", "fastify", "koa", "koa-router", "@koa/router", "hono", "elysia", "@hapi/hapi", "restify",
              "polka")
JS_ROUTE = re.compile(r"""\b(\w+)\.(get|post|put|patch|delete|all)\(\s*["'`](/[^"'`]*)["'`]""")
JS_MOUNT = re.compile(r"""\b\w+\.(?:use|route)\(\s*["'`](/[^"'`]*)["'`]\s*,\s*([\w$]+)""")
NEXT_ROUTE = re.compile(r"(?:^|/)app/((?:[^/]+/)*)route\.(?:ts|js|tsx|jsx|mts|mjs)$")
NEXT_PAGES_API = re.compile(r"(?:^|/)pages/(api(?:/[^/]+)*?)(?:/index)?\.(?:ts|js|tsx|jsx)$")
NEXT_METHOD = re.compile(r"\bexport\s+(?:async\s+)?(?:function\s+|const\s+|let\s+)(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b")
# What a front end calls: besides "/api/…" strings, a client's `url: "/…"` and the first argument of an HTTP call.
URL_PROP = re.compile(r"""\burl\s*:\s*["'`](/[^"'`\s]*)["'`]""")
HTTP_CALL = re.compile(r"""(?:\bfetch|\baxios|\bky|\$fetch|\bofetch|\.(?:get|post|put|patch|delete|request))\(\s*["'`](/[^"'`\s]*)["'`]""")

# Settings a pydantic settings class reads: its UPPER_CASE fields, under its env_prefix.
CLASS = re.compile(r"^([ \t]*)class\s+(\w+)\s*(?:\(([^)]*)\))?\s*:", re.M)
FIELD = re.compile(r"[ \t]*([A-Za-z_]\w*)\s*:")
ENV_PREFIX = re.compile(r"""\benv_prefix\s*=\s*["']([A-Za-z0-9_]*)["']""")

# Tables, besides SQL migrations: Alembic's operations, ORM models, Prisma models.
OP_CREATE = re.compile(r"""\bop\.create_table\(\s*["'](\w+)["']""")
OP_DROP = re.compile(r"""\bop\.drop_table\(\s*["'](\w+)["']""")
OP_ADD_COLUMN = re.compile(r"""\bop\.add_column\(\s*["'](\w+)["']\s*,\s*(?:\w+\.)?Column\(\s*["'](\w+)["']""")
OP_RENAME_COLUMN = re.compile(r"""\bop\.alter_column\(\s*["'](\w+)["']\s*,\s*["'](\w+)["'][^)]*?\bnew_column_name\s*=\s*["'](\w+)["']""")
OP_DROP_COLUMN = re.compile(r"""\bop\.drop_column\(\s*["'](\w+)["']\s*,\s*["'](\w+)["']""")
REVISION = re.compile(r"""^revision\s*(?::[^=\n]+)?=\s*["'](\w+)["']""", re.M)
DOWN_REVISION = re.compile(r"""^down_revision\s*(?::[^=\n]+)?=\s*(?:["'](\w+)["']|\(([^)]*)\))""", re.M)
ALEMBIC_FILE = re.compile(r"(?:^|/)versions/[^/]+\.py$")
TABLENAME = re.compile(r"""^[ \t]+__tablename__\s*(?::[^=\n]+)?=\s*["'](\w+)["']""", re.M)
DB_TABLE = re.compile(r"""^[ \t]+db_table\s*=\s*["'](\w+)["']""", re.M)
SA_TABLE = re.compile(r"""\bTable\(\s*["'](\w+)["']\s*,\s*[\w.]*metadata\b""", re.I)
PRISMA_MODEL = re.compile(r"""^model\s+(\w+)\s*\{([^}]*)\}""", re.M)
PRISMA_MAP = re.compile(r"""@@map\(\s*["'](\w+)["']\s*\)""")
SQL_MIGRATIONS = "**/migrations/**/*.sql"  # where SQL migrations are looked for when the config names none

# Outside systems nobody declared, known by the compose images that run them, the client libraries that reach them
# ("@scope/*": any package of that scope; "a.b": that module and below) and the settings that name them.
OUTSIDE = (  # id, label, shape, compose images, client libraries, settings (prefixes)
    ("postgres", "PostgreSQL", "db", ("postgres", "postgresql", "postgis", "timescaledb", "pgvector"),
     ("psycopg", "psycopg2", "asyncpg", "pg8000", "pg", "postgres", "@neondatabase/serverless", "@vercel/postgres"),
     ("POSTGRES_", "PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGPORT")),
    ("mysql", "MySQL", "db", ("mysql",), ("pymysql", "MySQLdb", "mysql", "aiomysql", "asyncmy", "mysql2"), ("MYSQL_",)),
    ("mariadb", "MariaDB", "db", ("mariadb",), ("mariadb",), ("MARIADB_",)),
    ("sqlite", "SQLite", "db", (), ("sqlite3", "aiosqlite", "better-sqlite3"), ("SQLITE_",)),
    ("mongodb", "MongoDB", "db", ("mongo", "mongodb"),
     ("pymongo", "motor", "beanie", "mongoengine", "mongoose", "mongodb"), ("MONGO",)),
    ("redis", "Redis", "db", ("redis", "valkey", "redis-stack", "redis-stack-server", "keydb", "dragonfly"),
     ("redis", "aioredis", "valkey", "ioredis", "@upstash/redis"), ("REDIS_", "VALKEY_")),
    ("elasticsearch", "Elasticsearch", "db", ("elasticsearch",), ("elasticsearch", "@elastic/elasticsearch"),
     ("ELASTIC",)),
    ("opensearch", "OpenSearch", "db", ("opensearch",), ("opensearchpy", "@opensearch-project/opensearch"),
     ("OPENSEARCH_",)),
    ("minio", "MinIO", "db", ("minio",), ("minio",), ("MINIO_",)),
    ("memcached", "Memcached", "db", ("memcached",), ("pymemcache", "memcache", "memjs"), ("MEMCACHE",)),
    ("rabbitmq", "RabbitMQ", "", ("rabbitmq",), ("pika", "aio_pika", "amqplib", "amqp-connection-manager"),
     ("RABBITMQ_", "AMQP_")),
    ("kafka", "Kafka", "", ("kafka", "cp-kafka", "redpanda"), ("kafka", "confluent_kafka", "aiokafka", "kafkajs"),
     ("KAFKA_",)),
    ("nats", "NATS", "", ("nats",), ("nats",), ("NATS_",)),
    ("mail", "Mail server", "", ("mailpit", "mailhog", "mailcatcher", "maildev"),
     ("smtplib", "aiosmtplib", "emails", "fastapi_mail", "flask_mail", "nodemailer"), ("SMTP_", "MAIL_", "EMAIL_HOST")),
    ("sendgrid", "SendGrid", "", (), ("sendgrid", "@sendgrid/mail"), ("SENDGRID_",)),
    ("resend", "Resend", "", (), ("resend",), ("RESEND_",)),
    ("postmark", "Postmark", "", (), ("postmarker", "postmark"), ("POSTMARK_",)),
    ("stripe", "Stripe", "", (), ("stripe", "@stripe/stripe-js", "@stripe/react-stripe-js"), ("STRIPE_",)),
    ("twilio", "Twilio", "", (), ("twilio",), ("TWILIO_",)),
    ("slack", "Slack", "", (), ("slack_sdk", "slack_bolt", "@slack/web-api", "@slack/bolt"), ("SLACK_",)),
    ("sentry", "Sentry", "", (), ("sentry_sdk", "@sentry/*"), ("SENTRY_",)),
    ("openai", "OpenAI API", "", (), ("openai",), ("OPENAI_",)),
    ("anthropic", "Anthropic API", "", (), ("anthropic", "@anthropic-ai/sdk"), ("ANTHROPIC_",)),
    ("gemini", "Gemini API", "", (), ("google.generativeai", "google.genai", "@google/generative-ai", "@google/genai"),
     ("GEMINI_",)),
    ("firebase", "Firebase", "", (), ("firebase_admin", "firebase", "firebase-admin"), ("FIREBASE_",)),
    ("supabase", "Supabase", "", (), ("supabase", "@supabase/supabase-js", "@supabase/ssr"), ("SUPABASE_",)),
    ("gcp", "Google Cloud", "", (), ("google.cloud", "@google-cloud/*"), ("GOOGLE_CLOUD_", "GCP_", "GCS_")),
    ("azure", "Azure", "", (), ("azure", "@azure/*"), ("AZURE_",)),
    ("aws", "AWS", "", ("localstack",), ("boto3", "aioboto3", "botocore", "@aws-sdk/*", "aws-sdk"), ("AWS_",)),
    ("github", "GitHub API", "", (), ("github", "@octokit/*", "octokit"), ("GITHUB_TOKEN",)),
    ("auth0", "Auth0", "", (), ("auth0", "@auth0/*"), ("AUTH0_",)),
    ("clerk", "Clerk", "", (), ("clerk_backend_api", "@clerk/*"), ("CLERK_",)),
    ("posthog", "PostHog", "", (), ("posthog", "posthog-js", "posthog-node"), ("POSTHOG_",)),
)
AWS = {"s3": ("Amazon S3", "db"), "dynamodb": ("Amazon DynamoDB", "db"), "sqs": ("Amazon SQS", ""),
       "sns": ("Amazon SNS", ""), "ses": ("Amazon SES", ""), "lambda": ("AWS Lambda", ""),
       "secretsmanager": ("AWS Secrets Manager", ""), "bedrock-runtime": ("Amazon Bedrock", ""),
       "kinesis": ("Amazon Kinesis", ""), "cognito-idp": ("Amazon Cognito", ""),
       "eventbridge": ("Amazon EventBridge", "")}
AWS_CALL = re.compile(r"""\.(?:client|resource)\(\s*(?:service_name\s*=\s*)?["']([a-z0-9-]+)["']""")
RELATIONAL = ("postgres", "mysql", "mariadb", "sqlite")  # the database an ORM's tables live in, the first found
ORMS = ("sqlalchemy", "sqlmodel", "django.db", "tortoise", "peewee", "pony", "prisma", "@prisma/client", "typeorm",
        "sequelize", "drizzle-orm", "knex", "kysely", "objection", "@mikro-orm/*")
DB_SETTINGS = ("DATABASE_URL", "DATABASE_", "DB_", "SQLALCHEMY_DATABASE_URI")

# A part nobody declared is what people use when its files are pages and components, or it imports a UI library, or
# its package does (and no server framework).
UI_EXT = (".tsx", ".jsx", ".vue", ".svelte")
UI_LIBS = ("react", "react-dom", "vue", "svelte", "@angular/core", "preact", "solid-js", "next", "nuxt",
           "@sveltejs/kit", "@remix-run/react", "astro")


def _resolver(path: str, files: set, python_paths: Iterable[str]) -> Tuple[Callable, Callable, Callable, List[str]]:
    """How a Python file's imports find files: absolute (`a.b`), relative (`.b`), and the nearest file that exists
    (`import a.b.c` where only a/b.py does)."""
    package = path.split("/")[:-1]

    def module_file(dotted: str) -> Optional[str]:
        stem = dotted.replace(".", "/")
        return next((c for base in python_paths for c in (base + stem + ".py", base + stem + "/__init__.py")
                     if c in files), None)

    def relative_file(dotted: str) -> Optional[str]:
        stem = dotted.replace(".", "/")
        return next((c for c in (stem + ".py", stem + "/__init__.py") if c in files), None)

    def nearest(dotted: str, find=module_file) -> Optional[str]:
        parts = dotted.split(".")
        while parts:
            hit = find(".".join(parts))
            if hit:
                return hit
            parts.pop()
        return None
    return module_file, relative_file, nearest, package


def _listed(names: str) -> List[Tuple[str, str]]:
    """`a, b as c` as (name, the name it is bound to here)."""
    out = []
    for item in re.sub(r"#[^\n]*", "", names).split(","):
        name, _, alias = item.strip().partition(" as ")
        name = name.strip()
        if name:
            out.append((name, alias.strip() or name))
    return out


def _attributes(text: str, local: str) -> Counter:
    """What a file uses of a module it imports whole: `rules.approve(…)` gives approve."""
    found = Counter(re.findall(rf"(?<![\w.]){re.escape(local)}\.([A-Za-z_]\w*)", text))
    return Counter({n: k for n, k in found.items() if not n.startswith("__")})


CHAIN = re.compile(r"(?<![\w.])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")  # a.b.c: an attribute reached through a name
DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def _attribute_table(text: str, heads: Optional[set] = None) -> Dict[str, Counter]:
    """_attributes for every name at once (or every name starting with one of `heads`), in one pass: `a.b.c` counts
    b for a and c for a.b."""
    table: Dict[str, Counter] = defaultdict(Counter)
    for m in CHAIN.finditer(text):
        chain = m.group(0)
        if heads is not None and chain[: chain.index(".")] not in heads:
            continue
        segments = chain.split(".")
        for i in range(1, len(segments)):
            table[".".join(segments[:i])][segments[i]] += 1
    return table


def py_import_facts(text: str) -> List[list]:
    """A Python file's imports as written, before they are looked up among the repository's files (which takes the
    other files): ["from", dots, module, [[name, bound as, {attribute: uses}]]] for each `from` import, then
    ["import", [dotted names], alias or None, [{attribute: uses} for each]] for each plain one. What a file uses of
    a module it imports whole (`rules.approve(…)`) is counted here, from its text."""
    table = None
    heads: set = set()

    def attributes(local: str) -> Dict[str, int]:
        nonlocal table
        if not DOTTED.fullmatch(local):
            return dict(_attributes(text, local))
        if table is None:
            table = _attribute_table(text, heads)
        found = table.get(local) or {}
        return {n: k for n, k in found.items() if not n.startswith("__")}

    lists = {m.start(): m.group(3) for m in PY_FROM_LIST.finditer(text)}
    froms, imports = [], []
    for m in PY_FROM.finditer(text):
        dots, mod, names = m.groups()
        froms.append((len(dots), mod, _listed(lists.get(m.start(), names))))
    for m in PY_IMPORT.finditer(text):
        dotted_names = [d.strip() for d in m.group(1).split(",")]
        alias = re.match(r"[ \t]+as[ \t]+(\w+)", text[m.end(): m.end() + 80])
        bound = [alias.group(1) if alias and i == len(dotted_names) - 1 else d for i, d in enumerate(dotted_names)]
        imports.append((dotted_names, alias.group(1) if alias else None, bound))
    heads.update(local.split(".")[0] for _d, _m, listed in froms for _n, local in listed)
    heads.update(b.split(".")[0] for _d, _a, bound in imports for b in bound)
    out: List[list] = [["from", dots, mod, [[n, local, attributes(local) if n != "*" else {}] for n, local in listed]]
                       for dots, mod, listed in froms]
    out += [["import", dotted_names, alias, [attributes(b) for b in bound]] for dotted_names, alias, bound in imports]
    return out


def _py_reached(path: str, facts: List[list], files: set, python_paths: Iterable[str]) -> List[Tuple[str, Counter]]:
    """py_imports from a file's py_import_facts."""
    module_file, relative_file, nearest, package = _resolver(path, files, python_paths)
    out: List[Tuple[str, Counter]] = []
    for entry in facts:
        if entry[0] == "from":
            _, dots, mod, listed = entry
            base = package[: len(package) - (dots - 1)] if dots else []
            stem = ".".join(base + ([mod] if mod else []))
            find = relative_file if dots else module_file
            modules = [(find(f"{stem}.{n}"), n, attrs) for n, _local, attrs in listed if n != "*"]
            modules = [(f, n, attrs) for f, n, attrs in modules if f]
            if modules:  # `from pkg import mod`: the modules themselves
                out += [(f, Counter(attrs) or Counter([n])) for f, n, attrs in modules]
                continue
            target = nearest(stem, find)
            if target:
                took = [n for n, _local, _attrs in listed if n != "*"]
                out.append((target, Counter(took or [stem.rsplit(".", 1)[-1] or "*"])))
        else:
            _, dotted_names, _alias, attrs = entry
            for dotted, used in zip(dotted_names, attrs):
                target = nearest(dotted)
                if target:
                    out.append((target, Counter(used) or Counter([dotted.rsplit(".", 1)[-1]])))
    return out


def py_imports(path: str, text: str, files: set, python_paths: Iterable[str] = ("",)) -> List[Tuple[str, Counter]]:
    """Each import of a Python file that reaches one of `files`, in the order written: the file, and the names it
    takes from it (`from x import a, b`), or what it uses of a module it imports whole. Imports of anything else (the
    standard library, packages) reach nothing and are left out."""
    return _py_reached(path, py_import_facts(text), files, python_paths)


def py_targets(path: str, text: str, files: set, python_paths: Iterable[str] = ("",)) -> List[str]:
    """Files a Python file imports, among `files`; imports of anything else (the standard library,
    third-party packages) resolve to nothing and are ignored."""
    return [f for f, _ in py_imports(path, text, files, python_paths)]


def _py_bound(path: str, facts: List[list], files: set,
              python_paths: Iterable[str]) -> Dict[str, Tuple[str, Optional[str]]]:
    """_py_bindings from a file's py_import_facts."""
    module_file, relative_file, nearest, package = _resolver(path, files, python_paths)
    out: Dict[str, Tuple[str, Optional[str]]] = {}
    for entry in facts:
        if entry[0] == "from":
            _, dots, mod, listed = entry
            base = package[: len(package) - (dots - 1)] if dots else []
            stem = ".".join(base + ([mod] if mod else []))
            find = relative_file if dots else module_file
            for n, local, _attrs in listed:
                module = find(f"{stem}.{n}")
                if module:
                    out[local] = (module, None)
                else:
                    owner = find(stem) if stem else None
                    if owner:
                        out[local] = (owner, n)
        else:
            _, dotted_names, alias, _attrs = entry
            if alias and module_file(dotted_names[-1]):
                out[alias] = (module_file(dotted_names[-1]), None)
    return out


def _py_bindings(path: str, text: str, files: set, python_paths: Iterable[str]) -> Dict[str, Tuple[str, Optional[str]]]:
    """What each name a Python file imports is bound to: (file, None) for a module, (file, name) for a name in one."""
    return _py_bound(path, py_import_facts(text), files, python_paths)


def _clause_names(clause: str, local: bool = False) -> List[str]:
    """The names an import clause takes (`Default, { a, b as c }, * as ns` gives Default, a, b, ns), or with `local`
    the names it binds here (Default, a, c, ns)."""
    out: List[str] = []
    rest = clause
    brace = re.search(r"\{([^}]*)\}", clause)
    if brace:
        for item in brace.group(1).split(","):
            item = re.sub(r"^\s*type\s+", "", item).strip()
            pieces = [p.strip() for p in re.split(r"\s+as\s+|:", item)]
            name = pieces[-1] if (local or pieces[0] == "default") and len(pieces) > 1 else pieces[0]
            if name:
                out.append(name)
        rest = clause[: brace.start()] + clause[brace.end():]
    star = re.search(r"\*\s*as\s+([\w$]+)", rest)
    if star:
        out.append(star.group(1))
        rest = rest[: star.start()] + rest[star.end():]
    first = re.match(r"\s*([\w$]+)", rest)
    if first and first.group(1) != "type":
        out.insert(0, first.group(1))
    return out


def _ts_resolve(path: str, spec: str, files: set, aliases: Optional[Dict[str, str]]) -> Optional[str]:
    base = path.rsplit("/", 1)[0] if "/" in path else ""
    alias = next((a for a in (aliases or {}) if spec.startswith(a)), None)
    if alias:
        stem = aliases[alias] + spec[len(alias):]
    elif spec.startswith("."):
        stem = os.path.normpath(os.path.join(base, spec)).replace(os.sep, "/")
    else:
        return None
    return next((c for c in (stem, *(stem + e for e in TS_EXT), *(stem + "/index" + e for e in TS_EXT)) if c in files),
                None)


def ts_import_facts(text: str) -> List[list]:
    """A JavaScript or TypeScript file's imports as written: [specifier, the names its clause takes or None]."""
    named = {m.start(2): _clause_names(m.group(1)) for m in TS_CLAUSE.finditer(text)}
    named.update({m.start(2): _clause_names(m.group(1)) for m in TS_REQUIRE.finditer(text)})
    return [[m.group(1), named.get(m.start(1))] for m in TS_IMPORT.finditer(text)]


def _ts_reached(path: str, facts: List[list], files: set, aliases: Optional[Dict[str, str]]) -> List[Tuple[str, Counter]]:
    out = []
    for spec, names in facts:
        target = _ts_resolve(path, spec, files, aliases)
        if target:
            stem = target.rsplit("/", 1)[-1].split(".")[0]
            if stem == "index" and "/" in target:  # a folder's index: the folder's name
                stem = target.rsplit("/", 2)[-2]
            out.append((target, Counter(names or [stem])))
    return out


def ts_imports(path: str, text: str, files: set, aliases: Optional[Dict[str, str]] = None) -> List[Tuple[str, Counter]]:
    """Each import of a JavaScript or TypeScript file that reaches one of `files`: the file and the names it takes
    (named imports, a default import's name, a namespace's name; else the file's own name)."""
    return _ts_reached(path, ts_import_facts(text), files, aliases)


def ts_targets(path: str, text: str, files: set, aliases: Optional[Dict[str, str]] = None) -> List[str]:
    return [f for f, _ in ts_imports(path, text, files, aliases)]


def ts_binding_facts(text: str) -> List[list]:
    """[specifier, the names its clause binds here] for each import clause and require of a JavaScript file."""
    return [[m.group(2), _clause_names(m.group(1), local=True)] for rx in (TS_CLAUSE, TS_REQUIRE) for m in rx.finditer(text)]


def _ts_bound(path: str, facts: List[list], files: set, aliases: Optional[Dict[str, str]]) -> Dict[str, str]:
    out = {}
    for spec, names in facts:
        target = _ts_resolve(path, spec, files, aliases)
        if target:
            out.update({name: target for name in names})
    return out


def _ts_bindings(path: str, text: str, files: set, aliases: Optional[Dict[str, str]]) -> Dict[str, str]:
    """The file each name a JavaScript file imports comes from."""
    return _ts_bound(path, ts_binding_facts(text), files, aliases)


def python_roots(files: Iterable[str]) -> List[str]:
    """Folders Python code is imported from without being named (backend/ for `import app` reaching backend/app/):
    each folder holding a package (a folder with an __init__.py) that is no package itself."""
    files = set(files)
    out = set()
    for f in files:
        if f.endswith("/__init__.py") and f.count("/") >= 2:
            package = f[: -len("/__init__.py")]
            parent = package.rsplit("/", 1)[0]
            if f"{parent}/__init__.py" not in files:
                out.add(parent + "/")
    return sorted(out)


def import_paths(files: Iterable[str], python_paths: Iterable[str]) -> List[str]:
    """Where `import a.b` is looked up: the configured places, then the folders that hold packages."""
    paths = list(python_paths)
    return paths + [r for r in python_roots(files) if r not in paths]


TSCONFIG = re.compile(r"(?:^|/)(?:tsconfig|jsconfig)[^/]*\.json$")


def _jsonc(text: str) -> Any:
    """JSON with the comments and trailing commas tsconfig.json allows."""
    text = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', lambda m: m.group(0) if m.group(0)[0] == '"' else "",
                  text, flags=re.S)
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))


def path_prefixes(files: Iterable[str], read: Callable[[str], str]) -> Dict[str, Dict[str, str]]:
    """The import prefixes each tsconfig.json or jsconfig.json maps to folders of the repository (`"@/*":
    ["./src/*"]` in frontend/ is @/ -> frontend/src/), by the folder it applies under."""
    out: Dict[str, Dict[str, str]] = {}
    for f in sorted(f for f in files if TSCONFIG.search(f)):
        try:
            options = (_jsonc(read(f) or "{}") or {}).get("compilerOptions") or {}
        except (ValueError, AttributeError):
            continue
        folder = f.rsplit("/", 1)[0] if "/" in f else ""
        base = os.path.normpath(os.path.join(folder or ".", str(options.get("baseUrl") or ".")))
        for key, targets in sorted((options.get("paths") or {}).items()):
            target = targets[0] if isinstance(targets, list) and targets else ""
            if key.endswith("/*") and isinstance(target, str) and target.endswith("/*"):
                where = os.path.normpath(os.path.join(base, target[:-1])).replace(os.sep, "/")
                out.setdefault(folder, {}).setdefault(key[:-1], "" if where == "." else where + "/")
    return out


def first_party(files: Iterable[str], python_paths: Iterable[str] = ("",)) -> set:
    """The top-level Python names this repository's own files answer to (`app`, `system_diff`), under the configured
    import paths and the folders that hold packages (backend/app/__init__.py makes `app` one)."""
    files = list(files)
    out = set()
    for f in files:
        if f.endswith(".py"):
            for base in python_paths:
                if f.startswith(base):
                    out.add(f[len(base):].split("/")[0].removesuffix(".py"))
    roots = tuple(python_roots(files))
    for f in files:
        if f.endswith("/__init__.py") and f.startswith(roots):
            rest = f[len(next(r for r in roots if f.startswith(r))):]
            if rest.count("/") == 1:
                out.add(rest.split("/")[0])
    return out


def outside_names(path: str, text: str) -> List[str]:
    """Every name a file imports by, before the repository's own ones are told apart (outside_imports does that)."""
    if path.endswith(".py"):
        names = [mod for dots, mod, _ in PY_FROM.findall(text) if mod and not dots]
        return names + [m.strip() for mods in PY_IMPORT.findall(text) for m in mods.split(",")]
    return TS_IMPORT.findall(text)


def _outside(path: str, names: Iterable[str], own: set, aliases: Optional[Dict[str, str]] = None) -> List[str]:
    found = set()
    if path.endswith(".py"):
        found = {n for n in names if n and n.split(".")[0] not in own and n != "__future__"}
    else:
        for spec in names:
            if spec.startswith(".") or any(spec.startswith(a) for a in (aliases or {})):
                continue
            parts = spec.split("/")
            found.add("/".join(parts[:2]) if spec.startswith("@") else parts[0])
    return sorted(found)


def outside_imports(path: str, text: str, own: set, aliases: Optional[Dict[str, str]] = None) -> List[str]:
    """What a file imports from outside the repository: packages and the standard library, by the name it imports
    them by (`http.server`, `fastapi.responses`, `@prisma/client`). `own`: first_party's names."""
    return _outside(path, outside_names(path, text), own, aliases)


def top_package(name: str) -> str:
    """The package an imported name belongs to: `fastapi.responses` is fastapi, `@tanstack/react-query` stays."""
    return "/".join(name.split("/")[:2]) if name.startswith("@") else name.split(".")[0]


def _library(name: str, libraries: Iterable[str]) -> bool:
    for lib in libraries:
        if lib.endswith("/*"):
            if name.startswith(lib[:-1]):
                return True
        elif name == lib or name.startswith(lib + ".") or name.startswith(lib + "/"):
            return True
    return False


def compose_services(text: str) -> Dict[str, dict]:
    """The services of a compose file: just enough YAML to see what changed in the deployment."""
    services: Dict[str, dict] = {}
    current, in_services, key = None, False, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^\S", line):
            in_services, current = line.startswith("services:"), None
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m:
            current = services.setdefault(m.group(1), {"image": "", "command": "", "ports": [], "replicas": ""})
            continue
        if current is None:
            continue
        m = re.match(r"^\s{4}(image|command|ports):\s*(.*)$", line)
        if m:
            key = m.group(1)
            if key != "ports" and m.group(2):
                current[key] = m.group(2).strip().strip("\"'")
            continue
        m = re.match(r"^\s+replicas:\s*(\S+)", line)
        if m:
            current["replicas"] = m.group(1)
            continue
        m = re.match(r"^\s{4,}-\s*\"?([^\"#]+?)\"?\s*$", line)
        if m and key == "ports":
            current["ports"].append(m.group(1))
        elif re.match(r"^\s{4}\w", line):
            key = None
    return services


def compose_file(cfg: Dict[str, Any], files: set) -> str:
    """The compose file read: the configured one, else the first of the usual names the repository has."""
    named = cfg.get("compose", "")
    if named in files:
        return named
    return next((n for n in ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml") if n in files),
                named)


def image_name(image: str) -> str:
    """`docker.io/bitnami/postgresql:16@sha256:…` is postgresql."""
    return image.split("@")[0].rsplit("/", 1)[-1].split(":")[0].lower()


def settings_fields(text: str) -> List[str]:
    """The settings a pydantic settings class reads: its UPPER_CASE fields, each under the class's env_prefix."""
    out: List[str] = []
    classes = list(CLASS.finditer(text))
    for i, m in enumerate(classes):
        if "BaseSettings" not in (m.group(3) or ""):
            continue
        indent = len(m.group(1).expandtabs())
        end = next((c.start() for c in classes[i + 1:] if len(c.group(1).expandtabs()) <= indent), len(text))
        body = text[m.end(): end]
        prefix = (ENV_PREFIX.search(body) or [None, ""])[1].upper()
        inner = None
        for line in body.splitlines():
            if not line.strip() or line.lstrip().startswith(("#", "@")):
                continue
            depth = len(line) - len(line.lstrip())
            if depth <= indent:
                break
            inner = depth if inner is None else inner
            field = FIELD.match(line)
            if depth == inner and field and field.group(1).isupper():
                out.append(prefix + field.group(1))
    return out


def api_path(path: str) -> str:
    """A path as called or served, its parameters written one way: `/users/${id}?x=1`, `/users/:id` and
    `/users/<int:id>` are all /users/{id}."""
    path = re.split(r"[?#]", path, maxsplit=1)[0]
    path = re.sub(r"\$\{([^}]*)\}", lambda m: "{" + (re.findall(r"\w+", m.group(1)) or ["param"])[-1] + "}", path)
    path = re.sub(r"(?<=/):(\w+)", r"{\1}", path)
    path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)
    path = re.sub(r"/{2,}", "/", path)
    return path or "/"


def _segments(path: str) -> List[str]:
    return [s for s in path.split("/") if s]


def _param(segment: str) -> bool:
    return segment == "*" or (segment.startswith("{") and segment.endswith("}"))


def path_matches(called: str, served: str) -> bool:
    """Whether a path a front end calls is one a route serves, a parameter on either side matching any segment."""
    a, b = _segments(called), _segments(served)
    return len(a) == len(b) and all(x == y or _param(x) or _param(y) for x, y in zip(a, b))


def _value(expr: str, constants: Dict[str, str]) -> Optional[str]:
    """A prefix written in the code: a string, a constant or a settings attribute holding one (`settings.API_V1_STR`),
    an f-string of those, or a sum of them; None when it cannot be read."""
    total = ""
    for piece in expr.split("+"):
        piece = piece.strip()
        literal = re.fullmatch(r"""([rbuRBUfF]*)["']([^"']*)["']""", piece)
        if literal:
            text = literal.group(2)
            if "f" in literal.group(1).lower():
                parts = [(_value(m.group(1), constants), m) for m in re.finditer(r"\{([^{}]+)\}", text)]
                if any(v is None for v, _ in parts):
                    return None
                for v, m in reversed(parts):
                    text = text[: m.start()] + v + text[m.end():]
            total += text
            continue
        name = re.fullmatch(r"[\w.]+", piece)
        if name and piece.split(".")[-1] in constants:
            total += constants[piece.split(".")[-1]]
            continue
        return None
    return total


def _constants(texts: Iterable[str]) -> Dict[str, str]:
    """String constants and class attributes (`API_V1_STR: str = "/api/v1"`) that hold one value everywhere."""
    return _one_value(CONSTANT.findall(text) for text in texts)


def _one_value(found: Iterable[Iterable[Tuple[str, str]]]) -> Dict[str, str]:
    seen: Dict[str, set] = defaultdict(set)
    for pairs in found:
        for name, value in pairs:
            seen[name].add(value)
    return {name: next(iter(values)) for name, values in seen.items() if len(values) == 1}


def _prefix_arg(text: str) -> Optional[List[str]]:
    arg = PREFIX_ARG.search(text)
    return [arg.group(1), arg.group(2)] if arg else None


def find_routes(code: List[str], texts: Dict[str, str], owner: Dict[str, str], files: set,
                python_paths: Iterable[str], aliases_of: Callable[[str], Dict[str, str]]) -> Dict[str, str]:
    """{"METHOD /path": part} for every route the code serves, each under the prefixes its routers are mounted with."""
    return _routes(code, {f: code_facts(f, texts[f]) for f in code}, owner, files, python_paths, aliases_of)


def _routes(code: List[str], facts: Dict[str, dict], owner: Dict[str, str], files: set,
            python_paths: Iterable[str], aliases_of: Callable[[str], Dict[str, str]]) -> Dict[str, str]:
    """find_routes from the code files' facts (code_facts); a file without facts (not read yet) serves nothing."""
    routes: Dict[str, str] = {}
    py = sorted(f for f in code if f.endswith(".py") and f in facts)
    constants = _one_value(facts[f]["constants"] for f in py)
    defs: Dict[Tuple[str, str], str] = {}  # (file, router variable) -> its own prefix
    for f in py:
        for var, arg in facts[f]["routers"]:
            defs[(f, var)] = (_value(arg[1], constants) or "") if arg else ""
    bindings = {f: _py_bound(f, facts[f]["imports"], files, python_paths) for f in py}

    def node(f: str, expr: str) -> Optional[Tuple[str, str]]:
        head, _, attr = expr.partition(".")
        bound = bindings[f].get(head)
        if not attr:
            if (f, head) in defs:
                return f, head
            return (bound[0], bound[1]) if bound and bound[1] and (bound[0], bound[1]) in defs else None
        return (bound[0], attr) if bound and bound[1] is None and (bound[0], attr) in defs else None

    mounted: Dict[Tuple[str, str], Tuple[Tuple[str, str], str, bool]] = {}
    for f in py:
        for parent_expr, child_expr, arg in facts[f]["mounts"]:
            parent, child = node(f, parent_expr), node(f, child_expr)
            if not parent or not child or child in mounted or parent == child:
                continue
            prefix = (_value(arg[1], constants) or "") if arg else ""
            replaces = bool(arg) and arg[0] == "url_prefix"  # Flask: the blueprint's own prefix gives way
            mounted[child] = (parent, prefix, replaces)

    def full(n: Tuple[str, str], depth: int = 0) -> str:
        if n in mounted and depth < 12:
            parent, prefix, replaces = mounted[n]
            return full(parent, depth + 1) + prefix + ("" if replaces else defs[n])
        return defs[n]

    for f in py:
        own = [k for k in defs if k[0] == f]
        for var, method, path in facts[f]["routes"]:
            n = node(f, var) or (own[0] if len(own) == 1 else None)
            routes[f"{method.upper()} {api_path((full(n) if n else '') + path)}"] = owner[f]
        for var, path, methods in facts[f]["flask"]:
            n = node(f, var) or (own[0] if len(own) == 1 else None)
            for method in methods or ["GET"]:
                routes[f"{method.upper()} {api_path((full(n) if n else '') + path)}"] = owner[f]

    js = sorted(f for f in code if not f.endswith(".py") and f in facts)
    servers = [f for f in js if any(_library(n, JS_SERVERS) for n in
                                    _outside(f, [spec for spec, _ in facts[f]["imports"]], set(), aliases_of(f)))]
    mounts: Dict[str, Tuple[str, str]] = {}  # a file of routes -> (the file that mounts it, under this prefix)
    for f in servers:
        names = _ts_bound(f, facts[f]["bindings"], files, aliases_of(f))
        for prefix, child in facts[f]["js_mounts"]:
            if names.get(child) and names[child] not in mounts and names[child] != f:
                mounts[names[child]] = (f, prefix)

    def js_prefix(f: str, depth: int = 0) -> str:
        return js_prefix(mounts[f][0], depth + 1) + mounts[f][1] if f in mounts and depth < 12 else ""

    for f in servers:
        for method, path in facts[f]["js_routes"]:
            routes[f"{method.upper()} {api_path(js_prefix(f) + path)}"] = owner[f]
    for f in js:
        m = NEXT_ROUTE.search(f)
        if m:
            segments = [s for s in m.group(1).split("/") if s and not (s[0] == "(" and s[-1] == ")") and s[0] != "@"]
            path = "/" + "/".join(_next_segment(s) for s in segments)
            for method in facts[f]["next"]:
                routes[f"{method} {api_path(path)}"] = owner[f]
            continue
        m = NEXT_PAGES_API.search(f)
        if m:
            routes[f"ANY {api_path('/' + '/'.join(_next_segment(s) for s in m.group(1).split('/')))}"] = owner[f]
    return dict(sorted(routes.items()))


def _next_segment(segment: str) -> str:
    """A Next.js folder name as a path segment: [id] and [...slug] are parameters."""
    m = re.fullmatch(r"\[+(?:\.\.\.)?(\w+)\]+", segment)
    return "{" + m.group(1) + "}" if m else segment


def called_paths(text: str) -> List[str]:
    """The API paths a front-end file calls, each once per place it is written."""
    at: Dict[int, str] = {}
    for rx in (API_STR, URL_PROP, HTTP_CALL):
        for m in rx.finditer(text):
            at.setdefault(m.start(1), m.group(1))
    return [api_path(at[k]) for k in sorted(at)]


def _upgrade(text: str) -> str:
    """An Alembic revision's upgrade(), what running it does (its downgrade() undoes it)."""
    start = re.search(r"^def upgrade\(", text, re.M)
    if not start:
        return ""
    end = re.search(r"^def ", text[start.end():], re.M)
    return text[start.start(): start.end() + end.start()] if end else text[start.start():]


def alembic_facts(text: str) -> Optional[dict]:
    """What an Alembic revision file says: its revision, the ones it follows, and what its upgrade() does to the
    tables in order ([operation, arguments]); None for a file that is no revision."""
    if not ("op." in text and "def upgrade" in text):
        return None
    return {**_alembic_heads(text), "ops": alembic_ops(_upgrade(text))}


def _alembic_heads(text: str) -> dict:
    r = REVISION.search(text)
    d = DOWN_REVISION.search(text)
    down = set(re.findall(r"\w+", d.group(2) or "")) | {d.group(1)} - {None} if d else set()
    return {"revision": r.group(1) if r else None, "down": sorted(down)}


def alembic_order(revisions: Dict[str, str]) -> List[str]:
    """Alembic revision files in the order they run, by each one's down_revision; by name where that cannot tell."""
    return _alembic_order({f: _alembic_heads(text) for f, text in revisions.items()})


def _alembic_order(facts: Dict[str, dict]) -> List[str]:
    rev = {f: x["revision"] or f for f, x in facts.items()}
    down = {f: set(x["down"]) for f, x in facts.items()}
    by_rev = {r: f for f, r in rev.items()}
    order, done = [], set()
    pending = sorted(facts)
    while pending:
        ready = [f for f in pending if all(by_rev.get(d) in done or d not in by_rev for d in down[f])]
        if not ready:  # a cycle or a gap: the rest by name
            ready = pending[:1]
        order += ready
        done |= set(ready)
        pending = [f for f in pending if f not in done]
    return order


def alembic_ops(upgrade: str) -> List[list]:
    """What one revision's upgrade() does, in the order it does it: [operation, arguments]."""
    ops = sorted([(m.start(), "create", m.groups()) for m in OP_CREATE.finditer(upgrade)]
                 + [(m.start(), "drop", m.groups()) for m in OP_DROP.finditer(upgrade)]
                 + [(m.start(), "add", m.groups()) for m in OP_ADD_COLUMN.finditer(upgrade)]
                 + [(m.start(), "remove", m.groups()) for m in OP_DROP_COLUMN.finditer(upgrade)]
                 + [(m.start(), "rename", m.groups()) for m in OP_RENAME_COLUMN.finditer(upgrade)])
    return [[op, list(args)] for _at, op, args in ops]


def alembic_run(upgrade: str, tables: Dict[str, set]) -> None:
    """What one revision's upgrade() does to the tables, in the order it does it: a table made or dropped, a column
    added, dropped or renamed (the columns kept are those added after a table was made, as for SQL migrations)."""
    _alembic_apply(alembic_ops(upgrade), tables)


def _alembic_apply(ops: List[list], tables: Dict[str, set]) -> None:
    for op, args in ops:
        table = qualify(args[0], "public")
        if op == "create":
            tables[table] |= set()
        elif op == "drop":
            tables.pop(table, None)
        elif op == "add":
            tables[table].add(args[1])
        elif op == "remove" and table in tables:
            tables[table].discard(args[1])
        elif op == "rename" and args[1] in tables.get(table, ()):
            tables[table] = (tables[table] - {args[1]}) | {args[2]}


def model_tables(f: str, text: str) -> List[str]:
    """The tables an ORM module defines: SQLModel classes marked as tables, SQLAlchemy classes naming their table and
    the tables it declares on its metadata, Flask-SQLAlchemy models (db.Model), Django models."""
    out = []
    classes = list(CLASS.finditer(text))
    django = "django.db" in text and "models" in f
    sqlalchemy = re.search(r"^[ \t]*(?:from|import)[ \t]+(?:sqlalchemy|flask_sqlalchemy|sqlmodel)\b", text, re.M)
    for i, m in enumerate(classes):
        bases = m.group(3) or ""
        indent = len(m.group(1).expandtabs())
        end = next((c.start() for c in classes[i + 1:] if len(c.group(1).expandtabs()) <= indent), len(text))
        body = text[m.end(): end]
        named = TABLENAME.search(body)
        last = bases.split(",")[-1].strip()
        if re.search(r"\btable\s*=\s*True\b", bases):
            out.append(named.group(1) if named else m.group(2).lower())
        elif named:
            out.append(named.group(1))
        elif re.fullmatch(r"(?:\w+\.)?db\.Model", last) and "__abstract__" not in body:  # Flask-SQLAlchemy's own name
            out.append(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", m.group(2)).lower())
        elif django and re.search(r"(?:^|\W)Model$", last) and "abstract = True" not in body:
            table = DB_TABLE.search(body)
            app = f.split("/models")[0].rsplit("/", 1)[-1]
            out.append(table.group(1) if table else f"{app}_{m.group(2).lower()}")
    return out + (SA_TABLE.findall(text) if sqlalchemy else [])


class Code:
    """Which files are code, which are tests, and which part owns each: one answer for the map and the diff."""

    def __init__(self, cfg: Dict[str, Any]):
        c = cfg["code"]
        self.skip = segment_rx(c["skip"])
        self.roots = tuple(c["roots"])
        self.ext = tuple(c["extensions"])
        self.group = c["group"]
        split = c.get("split")
        self.split = tuple(split) if isinstance(split, (list, tuple)) else ("app", "src", "lib") if split is True else ()
        self.strip = tuple(c["strip"] if c.get("strip") is not None else c["extensions"])
        self.tests = [glob_rx(p) for p in cfg.get("tests", [])]
        self.tooling = [glob_rx(p) for p in c.get("tooling") or ()]
        self.declared = [(d, [glob_rx(p) for p in d.get("paths", [])]) for d in cfg.get("components", [])]

    def is_code(self, path: str) -> bool:
        """Code of the system: under a code root, with a code extension, and not build, test or release tooling (a
        vite.config.ts, a migration's script) unless a declared part names it."""
        if not (path.endswith(self.ext) and path.startswith(self.roots) and not self.skip.search(path)):
            return False
        return not (any(r.match(path) for r in self.tooling) and not self.claimed(path))

    def is_test(self, path: str) -> bool:
        return any(r.match(path) for r in self.tests)

    def claimed(self, path: str) -> bool:
        return any(any(r.match(path) for r in rs) for _d, rs in self.declared)

    def part_of(self, path: str) -> str:
        """The declared part claiming `path`; else one named by its directory: the first `group` segments, one more
        under a folder that only wraps the code (backend/app, frontend/src: the `split` names), so backend/app/api
        and backend/app/core are parts of their own and backend/app's loose modules one more."""
        hit = next((d["id"] for d, rs in self.declared if any(r.match(path) for r in rs)), None)
        if hit is not None:
            return hit
        parts = path.split("/")
        n = int(self.group.get(parts[0], self.group.get("default", 2)))
        if len(parts) >= n + 2 and parts[n - 1] in self.split and parts[0] not in self.group:
            n += 1
        name = "/".join(parts[:n])
        return next((name[: -len(s)] for s in self.strip if name.endswith(s)), name)


def _package_json(path: str, files: set, deps: Dict[str, Any]) -> Any:
    """The dependencies of the package.json nearest above `path` (`deps`: each package.json's, as read)."""
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    while True:
        name = f"{folder}/package.json" if folder else "package.json"
        if name in files:
            return deps.get(name) or {}
        if not folder:
            return {}
        folder = folder.rsplit("/", 1)[0] if "/" in folder else ""


def package_dependencies(text: str) -> Any:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    return data.get("dependencies") or {} if isinstance(data, dict) else {}


def find_outside(imported: Dict[str, Counter], env: Dict[str, Counter], containers: Dict[str, dict],
                 aws: Dict[str, Counter], data_parts: Iterable[str], has_tables: bool) -> Tuple[Dict[str, dict], str]:
    """The outside systems nobody declared, and the database the tables live in (""): each system a compose image
    runs or a client library the code imports reaches, used by the parts that import its client or read its
    settings; the relational database also by the parts that use an ORM or read a database URL."""
    found: Dict[str, list] = {}  # id -> [label, shape, users (Counter), evidence]
    for xid, label, shape, images, libraries, settings in OUTSIDE:
        ran = sorted(f"compose runs {c['image']}" for c in containers.values()
                     if image_name(c.get("image", "")) in images)
        users: Counter = Counter()
        took = set()
        for cid, names in imported.items():
            for name, n in names.items():
                if _library(name, libraries):
                    users[cid] += n
                    took.add(name)
        if xid == "aws" and aws:  # named by the services called; AWS itself only for parts that call none by name
            named = {cid for svc in aws.values() for cid in svc}
            users = Counter({cid: n for cid, n in users.items() if cid not in named})
        if not ran and not users:
            continue
        for cid, names in env.items():
            n = sum(k for v, k in names.items() if v.startswith(settings))
            if n:
                users[cid] += n
        found[xid] = [label, shape, users, ran + [f"imports {t}" for t in sorted(took)]]
    for svc, users in sorted(aws.items()):
        label, shape = AWS.get(svc, (f"AWS {svc}", ""))
        found[f"aws-{svc}"] = [label, shape, Counter(users), [f"calls {svc}"]]
    orm = Counter({cid: sum(n for name, n in names.items() if _library(name, ORMS)) for cid, names in imported.items()})
    generic = Counter({cid: sum(k for v, k in names.items() if v.startswith(DB_SETTINGS))
                       for cid, names in env.items()})
    reach = +orm + +generic + Counter(dict.fromkeys(data_parts, 1))
    primary = next((x for x in RELATIONAL if x in found), "")
    if not primary and (has_tables or +orm):
        primary = "database"
        found[primary] = ["Database", "db", Counter(), ["tables in the code" if has_tables else "an ORM"]]
    if primary:
        found[primary][2].update(reach)
    out = {x: {"label": label, "shape": shape, "used_by": sorted(users, key=lambda c: (-users[c], c)), "found": why[:3]}
           for x, (label, shape, users, why) in sorted(found.items())}
    return out, primary


# ── what each file says, read once per content ───────────────────────────────────────────────────────────────────
#
# Reading a large repository is mostly reading each file: its imports, routes, settings, tables. What a file says
# depends only on its content (and its path), so it is kept by the file's git blob id (a content address) and read
# again only when the content changes; what depends on the other files (an import reaching a file, a route a call
# matches, which part owns what, the counts) is worked out again each time from every file's facts (`_combine`), fast.
# An edit re-reads the one file; the base of a comparison and the working tree share every file they have in common.

FACTS_VERSION = "1"   # what a file's facts hold: facts kept by another version are read again
COLD_BUDGET = 4.0     # seconds one budgeted read (the pane's) spends reading files nobody read yet; the next goes on
PRUNE_AT = 256        # entries a shard of the facts store holds before the ones not used lately are let go


def code_facts(path: str, text: str) -> dict:
    """What a code file says, in itself: everything extract needs of it, before it is set among the other files."""
    # the AWS services called count only in a file that imports boto3 (or aioboto3)
    out: Dict[str, Any] = {"lines": text.count("\n"), "aws": sorted(set(AWS_CALL.findall(text))) if "boto" in text else []}
    if path.endswith(".py"):
        out["imports"] = py_import_facts(text)
        out["env"] = [a or b for a, b in ENV_PY.findall(text)] + settings_fields(text)
        out["constants"] = [list(c) for c in CONSTANT.findall(text)]
        out["routers"] = [[m.group(1), _prefix_arg(m.group(3))] for m in ROUTER.finditer(text)]
        out["mounts"] = [[m.group(1), m.group(3), _prefix_arg(m.group(4) or "")] for m in MOUNT.finditer(text)]
        out["routes"] = [list(r) for r in PY_ROUTE.findall(text)]
        flask = []
        for var, path_, rest in FLASK_ROUTE.findall(text):
            listed = re.search(r"methods\s*=\s*[\[(]([^\])]*)", rest)
            flask.append([var, path_, re.findall(r"""["'](\w+)["']""", listed.group(1)) if listed else []])
        out["flask"] = flask
        out["tables"] = model_tables(path, text)
    else:
        out["imports"] = ts_import_facts(text)
        out["bindings"] = ts_binding_facts(text)
        out["env"] = ENV_TS.findall(text)
        out["api"] = API_STR.findall(text)
        out["called"] = called_paths(text)
        out["js_mounts"] = [list(m) for m in JS_MOUNT.findall(text)]
        out["js_routes"] = [[method, path_] for _var, method, path_ in JS_ROUTE.findall(text)]
        out["next"] = sorted(set(NEXT_METHOD.findall(text)))
    return out


def _sql_facts(text: str) -> dict:
    text = SQL_COMMENT.sub("", text)  # "CREATE TABLE IF NOT EXISTS only." in a comment is not a table
    return {"create": CREATE_TABLE.findall(text), "add": [list(a) for a in ADD_COLUMN.findall(text)]}


def _prisma_facts(text: str) -> List[str]:
    out = []
    for model, body in PRISMA_MODEL.findall(text):
        mapped = PRISMA_MAP.search(body)
        out.append(mapped.group(1) if mapped else model)
    return out


SECTIONS: Dict[str, Callable[[str, str], Any]] = {  # what a file is read for, and what reading it for that gives
    "code": lambda path, text: code_facts(path, text),
    "tsconfig": lambda path, text: path_prefixes([path], lambda _f: text),
    "keys": lambda path, text: sorted(set(CONFIG_KEY.findall(text))),
    "compose": lambda path, text: compose_services(text),
    "sql": lambda path, text: _sql_facts(text),
    "alembic": lambda path, text: alembic_facts(text),
    "prisma": lambda path, text: _prisma_facts(text),
    "package": lambda path, text: package_dependencies(text),
}


class FactStore:
    """Each file's facts, by blob id and path, under the git dir: files/v<FACTS_VERSION>/<first two hex>.json, one JSON
    object per shard ({"<blob> <path>": {section: facts}}). Shards are read when first needed and written back
    whole (merged with what another process wrote meanwhile, then swapped in), so a lost race only loses cache."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.shards: Dict[str, dict] = {}
        self.stamps: Dict[str, float] = {}
        self.dirty: set = set()
        self.used: Dict[str, set] = defaultdict(set)
        self.lock = threading.Lock()

    def _load(self, name: str, again: bool = False) -> dict:
        """A shard as last read; `again`: read it again if another process wrote it since (and this one has nothing
        unwritten in it)."""
        if name in self.shards and not (again and name not in self.dirty):
            return self.shards[name]
        path = self.folder / f"{name}.json"
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = 0.0
        if name not in self.shards or stamp != self.stamps.get(name):
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if stamp else {}
            except (OSError, ValueError):
                data = {}
            self.shards[name], self.stamps[name] = data if isinstance(data, dict) else {}, stamp
        return self.shards[name]

    def get(self, blob: str, path: str) -> Optional[dict]:
        key = f"{blob} {path}"
        with self.lock:
            got = self._load(blob[:2]).get(key)
            if got is None:   # perhaps read by another process meanwhile (the pane's, the hook's, the web page's)
                got = self._load(blob[:2], again=True).get(key)
            if got is not None:
                self.used[blob[:2]].add(key)
            return got

    def put(self, blob: str, path: str, facts: dict) -> None:
        key = f"{blob} {path}"
        with self.lock:
            self._load(blob[:2])[key] = facts
            self.used[blob[:2]].add(key)
            self.dirty.add(blob[:2])

    def flush(self) -> None:
        with self.lock:
            for name in sorted(self.dirty):
                path = self.folder / f"{name}.json"
                mine = self.shards[name]
                try:
                    theirs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                    merged = {**(theirs if isinstance(theirs, dict) else {}), **mine}
                except ValueError:   # half a file: what this process has
                    merged = dict(mine)
                except OSError:      # held by another process (Windows) this moment: written at the next flush
                    continue
                if len(merged) > max(PRUNE_AT, 4 * len(self.used[name])):   # old contents nobody reads any more
                    merged = {k: v for k, v in merged.items() if k in self.used[name]}
                try:
                    self.folder.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_name(f"{name}.json.{os.getpid()}.{threading.get_ident()}")
                    tmp.write_text(json.dumps(merged, separators=(",", ":")), encoding="utf-8")
                    os.replace(tmp, path)
                    self.stamps[name] = path.stat().st_mtime
                except OSError:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    continue   # kept dirty: written at the next flush
                self.shards[name] = merged
                self.dirty.discard(name)


_STORES: Dict[str, FactStore] = {}
_STORES_LOCK = threading.Lock()


def fact_store(root: Path) -> Optional[FactStore]:
    """The repository's store of per-file facts (one per process, shared by every source of the repository)."""
    key = str(root)
    with _STORES_LOCK:
        if key not in _STORES:
            try:
                from .sources import git_dir
                files = git_dir(Path(root), "files")
            except Exception:  # noqa: BLE001 - no git dir: no store, every file read each time
                return None
            for old in files.iterdir():   # an older extractor's facts
                if old.is_dir() and old.name != f"v{FACTS_VERSION}":
                    shutil.rmtree(old, ignore_errors=True)
            _STORES[key] = FactStore(files / f"v{FACTS_VERSION}")
        return _STORES[key]


ENTRY_NAMES = ("main", "app", "server", "index", "routes", "router", "api", "urls", "views", "settings", "config",
               "__init__", "route", "page", "layout", "client", "models", "schema")


def _reading_order(paths: List[str], sections: Dict[str, set], owner: Dict[str, str]) -> List[str]:
    """The order files are read in when there is not time for all: what is not code first (the compose and config
    files, migrations, tsconfig.json: few and telling), then the code across every part in turn, each part's entry
    points and outer files first, so every part gets its connections early and the picture fills in evenly."""
    rest = sorted(p for p in paths if "code" not in sections[p])
    by_part: Dict[str, List[str]] = defaultdict(list)
    for p in paths:
        if "code" in sections[p]:
            by_part[owner.get(p, "")].append(p)
    queues = []
    for part in sorted(by_part):
        stem = lambda p: p.rsplit("/", 1)[-1].split(".")[0]  # noqa: E731
        queues.append(sorted(by_part[part], key=lambda p: (stem(p) not in ENTRY_NAMES, p.count("/"), p)))
    code: List[str] = []
    for i in range(max((len(q) for q in queues), default=0)):
        code += [q[i] for q in queues if i < len(q)]
    return rest + code


def _gather(source, sections: Dict[str, set], owner: Dict[str, str], cache: bool, deadline: Optional[float],
            only: Optional[Iterable[str]]) -> Dict[str, dict]:
    """Each file's facts, {path: {section: facts}}: kept ones by blob id, the rest read. With a `deadline`
    (time.monotonic()) reading stops there (after at least one file) and the files not read yet are left out; with
    `only`, the files outside it are left out and every one in it is read, whatever the time."""
    blobs = getattr(source, "blobs", None)
    store = fact_store(source.root) if cache and blobs is not None and getattr(source, "root", None) else None
    if only is not None:
        only = set(only)
        deadline = None
    known: Dict[str, dict] = {}
    want: List[str] = []
    kept: Dict[str, dict] = {}
    for path, need in sections.items():
        if only is not None and path not in only:
            continue
        blob = blobs.get(path) if store else None
        got = store.get(blob, path) if blob else None
        if got is not None and need <= got.keys():
            known[path] = got
        else:
            want.append(path)
            if got is not None:
                kept[path] = got
    total = len(sections)
    if not want:
        return known
    if store is None:
        deadline = None   # nowhere to keep what is read: a later read could not go on from here
    want = _reading_order(want, sections, owner)
    stage = progress._stage or "Reading the code"
    flushed = time.monotonic()
    i = 0
    while i < len(want):
        if deadline is not None and i and time.monotonic() >= deadline:
            break
        chunk = want[i: i + 64] if deadline is not None else want[i: i + 2000]   # fewer `git cat-file`s untimed
        texts = source.read_many(chunk)
        for path in chunk:
            if deadline is not None and i and time.monotonic() >= deadline:
                break
            if i % 25 == 0:
                progress.stage(stage, len(known), total)
            facts = dict(kept.get(path) or {})
            text = texts.get(path, "")
            for name in sorted(sections[path] - facts.keys()):
                facts[name] = SECTIONS[name](path, text)
            known[path] = facts
            if store is not None and blobs.get(path):
                store.put(blobs[path], path, facts)
            i += 1
        if store is not None and time.monotonic() - flushed > 2:   # a long read keeps what it has as it goes
            store.flush()
            flushed = time.monotonic()
    if store is not None:
        store.flush()
    progress.stage(stage, len(known), total)
    return known


def extract(source, cfg: Dict[str, Any], cache: bool = True) -> dict:
    """The blueprint of one source: every file read (each one's facts kept by content, with `cache`)."""
    return read_blueprint(source, cfg, cache=cache)[0]


def read_blueprint(source, cfg: Dict[str, Any], deadline: Optional[float] = None, only: Optional[Iterable[str]] = None,
                   cache: bool = True) -> Tuple[dict, Optional[set]]:
    """The blueprint of one source, and None; or, when some files were not read (the `deadline` came first, or
    `only` left them out), a PARTIAL blueprint and the paths it did read. A partial blueprint has every part (a code
    file not read yet still counts as a file of its part) but only the connections, routes, settings and tables of
    the files read, and says how far it got: "partial": {"done": files read, "total": files to read}. It is never
    to be kept as the blueprint of its commit."""
    code_of = Code(cfg)
    files = [f for f in source.files if not code_of.skip.search(f)]
    fileset = set(files)
    code = [f for f in files if code_of.is_code(f) and not code_of.is_test(f)]
    owner = {f: code_of.part_of(f) for f in code}

    sections: Dict[str, set] = defaultdict(set)   # what each file is read for
    for f in code:
        sections[f].add("code")
    if any(not f.endswith(".py") for f in files):
        for f in files:
            if TSCONFIG.search(f):
                sections[f].add("tsconfig")
    compose = compose_file(cfg, fileset)
    config_rx = [glob_rx(p) for p in cfg.get("config_files", [])]
    for f in files:
        if f == compose or any(r.match(f) for r in config_rx):
            sections[f].add("keys")
    if compose in fileset:
        sections[compose].add("compose")
    mig = cfg.get("migrations", {})
    mig_rx = glob_rx(mig.get("glob") or SQL_MIGRATIONS)
    mig_files = sorted(f for f in files if mig_rx.match(f))
    for f in mig_files:
        sections[f].add("sql")
    if not mig.get("glob"):
        for f in files:
            if ALEMBIC_FILE.search(f):
                sections[f].add("alembic")
            if f.endswith(".prisma"):
                sections[f].add("prisma")
    if any(not f.endswith(".py") for f in code):
        for f in files:
            if f == "package.json" or f.endswith("/package.json"):
                sections[f].add("package")

    facts = _gather(source, dict(sections), owner, cache, deadline, only)
    bp = _combine(source, cfg, code_of, files, fileset, code, owner, compose, mig_files, facts)
    if len(facts) < len(sections):
        bp["partial"] = {"done": len(facts), "total": len(sections)}
        return bp, set(facts)
    return bp, None


def _combine(source, cfg: Dict[str, Any], code_of: Code, files: List[str], fileset: set, code: List[str],
             owner: Dict[str, str], compose: str, mig_files: List[str], facts: Dict[str, dict]) -> dict:
    """The blueprint from every file's facts: what depends on the other files (an import reaching a file, a call a
    route serves, who owns what) worked out here, each time."""
    comps = cfg.get("components", [])
    python_paths = import_paths(files, cfg["code"]["python_paths"])
    found_prefixes: Dict[str, Dict[str, str]] = {}
    for f in sorted(f for f in facts if "tsconfig" in facts[f]):   # path_prefixes, file by file
        for folder, prefixes in facts[f]["tsconfig"].items():
            for key, where in prefixes.items():
                found_prefixes.setdefault(folder, {}).setdefault(key, where)

    def aliases_of(f: str) -> Dict[str, str]:
        """The import prefixes a file is written with: the nearest tsconfig.json's, then the configured ones."""
        folder = max((d for d in found_prefixes if not d or f.startswith(d + "/")), key=len, default=None)
        return {**(found_prefixes.get(folder) or {}), **cfg["code"]["aliases"]} if folder is not None \
            else cfg["code"]["aliases"]

    components: Dict[str, dict] = {}
    ui_files: Counter = Counter()
    for f in code:
        cid = owner[f]
        meta = next((c for c in comps if c["id"] == cid), None)
        entry = components.setdefault(cid, {
            "label": (meta or {}).get("label", cid), "layer": meta.get("layer", "") if meta else "undeclared",
            "kind": (meta or {}).get("kind", ""), "order": comps.index(meta) if meta else len(comps),
            "container": (meta or {}).get("container", ""), "declared": meta is not None, "files": 0, "lines": 0,
            "hub": bool((meta or {}).get("hub")), "quiet": bool((meta or {}).get("quiet"))})
        entry["files"] += 1
        ui_files[cid] += f.endswith(UI_EXT)

    code_facts_of = {f: facts[f]["code"] for f in code if f in facts}
    own = first_party(source.files, cfg["code"]["python_paths"])
    flows, api, env_by_comp = Counter(), Counter(), defaultdict(Counter)
    uses: Dict[str, Counter] = defaultdict(Counter)
    stack: Dict[str, Counter] = defaultdict(Counter)
    imported: Dict[str, Counter] = defaultdict(Counter)  # every outside name a part imports, by how many files
    aws: Dict[str, Counter] = defaultdict(Counter)
    for f in code:
        fx = code_facts_of.get(f)
        if fx is None:
            continue   # not read yet
        cid = owner[f]
        components[cid]["lines"] += fx["lines"]
        python = f.endswith(".py")
        reached = (_py_reached(f, fx["imports"], fileset, python_paths) if python
                   else _ts_reached(f, fx["imports"], fileset, aliases_of(f)))
        for t, names in reached:
            if t in owner and owner[t] != cid:
                flows[f"{cid}>{owner[t]}"] += 1
                uses[f"{cid}>{owner[t]}"].update(names)
        if python:
            names = [e[2] for e in fx["imports"] if e[0] == "from" and e[2] and not e[1]]
            names += [d for e in fx["imports"] if e[0] == "import" for d in e[1]]
        else:
            names = [spec for spec, _ in fx["imports"]]
        outside = _outside(f, names, own, aliases_of(f))
        imported[cid].update(outside)
        stack[cid].update({top_package(n) for n in outside})
        for n in outside:
            if n.startswith("@aws-sdk/client-"):
                aws[n.removeprefix("@aws-sdk/client-")][cid] += 1
        if any(_library(n, ("boto3", "aioboto3")) for n in outside):
            for svc in fx["aws"]:
                aws[{"sesv2": "ses"}.get(svc, svc)][cid] += 1
        env_by_comp[cid].update(fx["env"])
        if not python:
            for s in fx["api"] if cfg.get("api") else ():
                target = next((a["component"] for a in cfg.get("api", []) if s.startswith(a["prefix"])), None)
                if target and target != cid:
                    api[f"{cid}>{target}"] += 1
                    uses[f"{cid}>{target}"][api_path(s)] += 1

    routes = _routes(code, code_facts_of, owner, fileset, python_paths, aliases_of)
    if not cfg.get("api"):  # nothing says who serves what the front end calls: the routes do
        served = sorted(((r.split(" ", 1)[1], cid) for r, cid in routes.items()),
                        key=lambda rc: (sum(map(_param, _segments(rc[0]))), rc[0]))
        for f in code:
            if f.endswith(".py") or f not in code_facts_of:
                continue
            for path in code_facts_of[f]["called"]:
                target = next((cid for route, cid in served if path_matches(path, route)), None)
                if target and target != owner[f]:
                    api[f"{owner[f]}>{target}"] += 1
                    uses[f"{owner[f]}>{target}"][path] += 1

    config_rx = [glob_rx(p) for p in cfg.get("config_files", [])]
    configured = set()
    for f in files:
        if (f == compose or any(r.match(f) for r in config_rx)) and f in facts:
            configured |= set(facts[f]["keys"])
    externals, dormant = {}, {}
    for ext in cfg.get("externals", []):
        reads = {c: sum(n for v, n in vs.items() if matches(v, ext["env"])) for c, vs in env_by_comp.items()}
        users = sorted((c for c, n in reads.items() if n), key=lambda c: (-reads[c], c))  # heaviest reader first
        if not users:
            continue
        live = any(matches(v, ext["env"]) for v in configured)
        (externals if live else dormant)[ext["id"]] = {"label": ext["label"], "shape": ext.get("shape", ""), "used_by": users}
    internal = Counter()
    for var, container in cfg.get("internal_env", {}).items():
        for c, vs in env_by_comp.items():
            if var in vs:
                internal[f"{c}>{container}"] += 1

    containers = facts[compose]["compose"] if compose in fileset and compose in facts else {}

    tables, migrations = defaultdict(set), []
    mig = cfg.get("migrations", {})
    for f in mig_files:
        name = f.rsplit("/", 1)[1]
        migrations.append(name)
        if f not in facts:
            continue
        num = int(re.match(r"\d+", name).group(0)) if re.match(r"\d+", name) else 0
        schema = next((s for lo, hi, s in mig.get("schemas", []) if lo <= num <= hi), "public")
        for table in facts[f]["sql"]["create"]:
            tables[qualify(table, schema)] |= set()
        for table, col in facts[f]["sql"]["add"]:
            tables[qualify(table, schema)].add(col)
    data_parts = set()
    if not mig.get("glob"):  # where the config names no migrations, the tables the code itself makes and defines
        revisions = {f: facts[f]["alembic"] for f in files
                     if ALEMBIC_FILE.search(f) and f in facts and facts[f].get("alembic")}
        for f in _alembic_order(revisions):
            migrations.append(f.rsplit("/", 1)[1])
            _alembic_apply(revisions[f]["ops"], tables)
        for f in code:
            if f.endswith(".py") and f in code_facts_of:
                found = code_facts_of[f]["tables"]
                for table in found:
                    tables[qualify(table, "public")] |= set()
                if found:
                    data_parts.add(owner[f])
        for f in files:
            if f.endswith(".prisma") and f in facts:
                for table in facts[f]["prisma"]:
                    tables[qualify(table, "public")] |= set()

    store = mig.get("store", "postgres")
    detect = (cfg.get("detect") or {}).get("externals", "auto")
    if detect is True or (detect == "auto" and not cfg.get("externals")):
        found, primary = find_outside(imported, env_by_comp, containers, aws, sorted(data_parts), bool(tables))
        declared = {name for e in cfg.get("externals", []) for name in (e["id"], e["label"].casefold())}
        for xid, e in found.items():
            if xid not in declared and e["label"].casefold() not in declared:
                externals[xid] = e
        if primary and primary in externals and not mig.get("store"):
            store = primary

    serving = set(routes.values())
    deps = {f: x["package"] for f, x in facts.items() if "package" in x}
    for cid, entry in components.items():  # what people use, where nobody said: pages, or a UI library
        if entry["declared"] or entry["kind"] or cid in serving:
            continue
        sample = next(f for f in code if owner[f] == cid)
        package = {} if sample.endswith(".py") else _package_json(sample, fileset, deps)
        if (ui_files[cid] * 2 > entry["files"] or any(lib in stack[cid] for lib in UI_LIBS)
                or (any(lib in package for lib in UI_LIBS) and not any(lib in package for lib in JS_SERVERS))):
            entry["kind"] = "ui"

    test_files = Counter(("/".join(f.split("/")[:2]) if f.startswith("tests/") else f.split("/")[0])
                         for f in files if code_of.is_test(f) and f.endswith(code_of.ext))
    used = {k: [n for n, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))][:6] for k, c in sorted(uses.items())}
    return {
        "ref": source.label,
        "components": components,
        "flows": dict(sorted(flows.items())),
        "api": dict(sorted(api.items())),
        "uses": used,
        "routes": routes,
        "externals": externals,
        "dormant": dormant,
        "internal": dict(sorted(internal.items())),
        "env": {c: sorted(v) for c, v in sorted(env_by_comp.items()) if v},
        "configured": sorted(configured),
        "stack": {c: dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))) for c, v in sorted(stack.items()) if v},
        # every outside name each part imports (`fastapi.responses`, `@tanstack/react-query`), by how many files
        "imported": {c: dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0])))
                     for c, v in sorted(imported.items()) if v},
        "containers": containers,
        "container_labels": cfg.get("containers", {}),
        "tables": {t: sorted(c) for t, c in sorted(tables.items())},
        "store": store,   # the outside system the tables live in
        "migrations": migrations,
        "tests": dict(sorted(test_files.items())),
    }


def qualify(name: str, schema: str) -> str:
    name = name.replace('"', "").replace("`", "")
    return name if "." in name else f"{schema}.{name}"
