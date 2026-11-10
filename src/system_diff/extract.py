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
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import progress
from .config import segment_rx

EXTRACT_VERSION = "2"  # what a blueprint records: a blueprint cached by another version is read again


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


def py_imports(path: str, text: str, files: set, python_paths: Iterable[str] = ("",)) -> List[Tuple[str, Counter]]:
    """Each import of a Python file that reaches one of `files`, in the order written: the file, and the names it
    takes from it (`from x import a, b`), or what it uses of a module it imports whole. Imports of anything else (the
    standard library, packages) reach nothing and are left out."""
    module_file, relative_file, nearest, package = _resolver(path, files, python_paths)
    lists = {m.start(): m.group(3) for m in PY_FROM_LIST.finditer(text)}
    out: List[Tuple[str, Counter]] = []
    for m in PY_FROM.finditer(text):
        dots, mod, names = m.groups()
        listed = _listed(lists.get(m.start(), names))
        base = package[: len(package) - (len(dots) - 1)] if dots else []
        stem = ".".join(base + ([mod] if mod else []))
        find = relative_file if dots else module_file
        modules = [(find(f"{stem}.{n}"), n, local) for n, local in listed if n != "*"]
        modules = [(f, n, local) for f, n, local in modules if f]
        if modules:  # `from pkg import mod`: the modules themselves
            out += [(f, _attributes(text, local) or Counter([n])) for f, n, local in modules]
            continue
        target = nearest(stem, find)
        if target:
            took = [n for n, _ in listed if n != "*"]
            out.append((target, Counter(took or [stem.rsplit(".", 1)[-1] or "*"])))
    for m in PY_IMPORT.finditer(text):
        dotted_names = [d.strip() for d in m.group(1).split(",")]
        alias = re.match(r"[ \t]+as[ \t]+(\w+)", text[m.end(): m.end() + 80])
        for i, dotted in enumerate(dotted_names):
            target = nearest(dotted)
            if target:
                local = alias.group(1) if alias and i == len(dotted_names) - 1 else dotted
                out.append((target, _attributes(text, local) or Counter([dotted.rsplit(".", 1)[-1]])))
    return out


def py_targets(path: str, text: str, files: set, python_paths: Iterable[str] = ("",)) -> List[str]:
    """Files a Python file imports, among `files`; imports of anything else (the standard library,
    third-party packages) resolve to nothing and are ignored."""
    return [f for f, _ in py_imports(path, text, files, python_paths)]


def _py_bindings(path: str, text: str, files: set, python_paths: Iterable[str]) -> Dict[str, Tuple[str, Optional[str]]]:
    """What each name a Python file imports is bound to: (file, None) for a module, (file, name) for a name in one."""
    module_file, relative_file, nearest, package = _resolver(path, files, python_paths)
    lists = {m.start(): m.group(3) for m in PY_FROM_LIST.finditer(text)}
    out: Dict[str, Tuple[str, Optional[str]]] = {}
    for m in PY_FROM.finditer(text):
        dots, mod, names = m.groups()
        base = package[: len(package) - (len(dots) - 1)] if dots else []
        stem = ".".join(base + ([mod] if mod else []))
        find = relative_file if dots else module_file
        for n, local in _listed(lists.get(m.start(), names)):
            module = find(f"{stem}.{n}")
            if module:
                out[local] = (module, None)
            else:
                owner = find(stem) if stem else None
                if owner:
                    out[local] = (owner, n)
    for m in PY_IMPORT.finditer(text):
        alias = re.match(r"[ \t]+as[ \t]+(\w+)", text[m.end(): m.end() + 80])
        dotted = m.group(1).split(",")[-1].strip()
        if alias and module_file(dotted):
            out[alias.group(1)] = (module_file(dotted), None)
    return out


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


def ts_imports(path: str, text: str, files: set, aliases: Optional[Dict[str, str]] = None) -> List[Tuple[str, Counter]]:
    """Each import of a JavaScript or TypeScript file that reaches one of `files`: the file and the names it takes
    (named imports, a default import's name, a namespace's name; else the file's own name)."""
    named = {m.start(2): _clause_names(m.group(1)) for m in TS_CLAUSE.finditer(text)}
    named.update({m.start(2): _clause_names(m.group(1)) for m in TS_REQUIRE.finditer(text)})
    out = []
    for m in TS_IMPORT.finditer(text):
        target = _ts_resolve(path, m.group(1), files, aliases)
        if target:
            stem = target.rsplit("/", 1)[-1].split(".")[0]
            if stem == "index" and "/" in target:  # a folder's index: the folder's name
                stem = target.rsplit("/", 2)[-2]
            out.append((target, Counter(named.get(m.start(1)) or [stem])))
    return out


def ts_targets(path: str, text: str, files: set, aliases: Optional[Dict[str, str]] = None) -> List[str]:
    return [f for f, _ in ts_imports(path, text, files, aliases)]


def _ts_bindings(path: str, text: str, files: set, aliases: Optional[Dict[str, str]]) -> Dict[str, str]:
    """The file each name a JavaScript file imports comes from."""
    out = {}
    for rx in (TS_CLAUSE, TS_REQUIRE):
        for m in rx.finditer(text):
            target = _ts_resolve(path, m.group(2), files, aliases)
            if target:
                out.update({name: target for name in _clause_names(m.group(1), local=True)})
    return out


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


def outside_imports(path: str, text: str, own: set, aliases: Optional[Dict[str, str]] = None) -> List[str]:
    """What a file imports from outside the repository: packages and the standard library, by the name it imports
    them by (`http.server`, `fastapi.responses`, `@prisma/client`). `own`: first_party's names."""
    found = set()
    if path.endswith(".py"):
        names = [mod for dots, mod, _ in PY_FROM.findall(text) if mod and not dots]
        names += [m.strip() for mods in PY_IMPORT.findall(text) for m in mods.split(",")]
        found = {n for n in names if n and n.split(".")[0] not in own and n != "__future__"}
    else:
        for spec in TS_IMPORT.findall(text):
            if spec.startswith(".") or any(spec.startswith(a) for a in (aliases or {})):
                continue
            parts = spec.split("/")
            found.add("/".join(parts[:2]) if spec.startswith("@") else parts[0])
    return sorted(found)


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
    seen: Dict[str, set] = defaultdict(set)
    for text in texts:
        for name, value in CONSTANT.findall(text):
            seen[name].add(value)
    return {name: next(iter(values)) for name, values in seen.items() if len(values) == 1}


def find_routes(code: List[str], texts: Dict[str, str], owner: Dict[str, str], files: set,
                python_paths: Iterable[str], aliases_of: Callable[[str], Dict[str, str]]) -> Dict[str, str]:
    """{"METHOD /path": part} for every route the code serves, each under the prefixes its routers are mounted with."""
    routes: Dict[str, str] = {}
    py = sorted(f for f in code if f.endswith(".py"))
    constants = _constants(texts[f] for f in py)
    defs: Dict[Tuple[str, str], str] = {}  # (file, router variable) -> its own prefix
    for f in py:
        for m in ROUTER.finditer(texts[f]):
            arg = PREFIX_ARG.search(m.group(3))
            defs[(f, m.group(1))] = (_value(arg.group(2), constants) or "") if arg else ""
    bindings = {f: _py_bindings(f, texts[f], files, python_paths) for f in py}

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
        for m in MOUNT.finditer(texts[f]):
            parent, child = node(f, m.group(1)), node(f, m.group(3))
            if not parent or not child or child in mounted or parent == child:
                continue
            arg = PREFIX_ARG.search(m.group(4) or "")
            prefix = (_value(arg.group(2), constants) or "") if arg else ""
            replaces = bool(arg) and arg.group(1) == "url_prefix"  # Flask: the blueprint's own prefix gives way
            mounted[child] = (parent, prefix, replaces)

    def full(n: Tuple[str, str], depth: int = 0) -> str:
        if n in mounted and depth < 12:
            parent, prefix, replaces = mounted[n]
            return full(parent, depth + 1) + prefix + ("" if replaces else defs[n])
        return defs[n]

    for f in py:
        text = texts[f]
        own = [k for k in defs if k[0] == f]
        for var, method, path in PY_ROUTE.findall(text):
            n = node(f, var) or (own[0] if len(own) == 1 else None)
            routes[f"{method.upper()} {api_path((full(n) if n else '') + path)}"] = owner[f]
        for var, path, rest in FLASK_ROUTE.findall(text):
            n = node(f, var) or (own[0] if len(own) == 1 else None)
            listed = re.search(r"methods\s*=\s*[\[(]([^\])]*)", rest)
            for method in (re.findall(r"""["'](\w+)["']""", listed.group(1)) if listed else []) or ["GET"]:
                routes[f"{method.upper()} {api_path((full(n) if n else '') + path)}"] = owner[f]

    js = sorted(f for f in code if not f.endswith(".py"))
    servers = [f for f in js
               if any(_library(n, JS_SERVERS) for n in outside_imports(f, texts[f], set(), aliases_of(f)))]
    mounts: Dict[str, Tuple[str, str]] = {}  # a file of routes -> (the file that mounts it, under this prefix)
    for f in servers:
        names = _ts_bindings(f, texts[f], files, aliases_of(f))
        for prefix, child in JS_MOUNT.findall(texts[f]):
            if names.get(child) and names[child] not in mounts and names[child] != f:
                mounts[names[child]] = (f, prefix)

    def js_prefix(f: str, depth: int = 0) -> str:
        return js_prefix(mounts[f][0], depth + 1) + mounts[f][1] if f in mounts and depth < 12 else ""

    for f in servers:
        for _var, method, path in JS_ROUTE.findall(texts[f]):
            routes[f"{method.upper()} {api_path(js_prefix(f) + path)}"] = owner[f]
    for f in js:
        m = NEXT_ROUTE.search(f)
        if m:
            segments = [s for s in m.group(1).split("/") if s and not (s[0] == "(" and s[-1] == ")") and s[0] != "@"]
            path = "/" + "/".join(_next_segment(s) for s in segments)
            for method in sorted(set(NEXT_METHOD.findall(texts[f]))):
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


def alembic_order(revisions: Dict[str, str]) -> List[str]:
    """Alembic revision files in the order they run, by each one's down_revision; by name where that cannot tell."""
    rev, down = {}, {}
    for f, text in revisions.items():
        r = REVISION.search(text)
        d = DOWN_REVISION.search(text)
        rev[f] = r.group(1) if r else f
        down[f] = set(re.findall(r"\w+", d.group(2) or "")) | {d.group(1)} - {None} if d else set()
    by_rev = {r: f for f, r in rev.items()}
    order, done = [], set()
    pending = sorted(revisions)
    while pending:
        ready = [f for f in pending if all(by_rev.get(d) in done or d not in by_rev for d in down[f])]
        if not ready:  # a cycle or a gap: the rest by name
            ready = pending[:1]
        order += ready
        done |= set(ready)
        pending = [f for f in pending if f not in done]
    return order


def alembic_run(upgrade: str, tables: Dict[str, set]) -> None:
    """What one revision's upgrade() does to the tables, in the order it does it: a table made or dropped, a column
    added, dropped or renamed (the columns kept are those added after a table was made, as for SQL migrations)."""
    ops = sorted([(m.start(), "create", m.groups()) for m in OP_CREATE.finditer(upgrade)]
                 + [(m.start(), "drop", m.groups()) for m in OP_DROP.finditer(upgrade)]
                 + [(m.start(), "add", m.groups()) for m in OP_ADD_COLUMN.finditer(upgrade)]
                 + [(m.start(), "remove", m.groups()) for m in OP_DROP_COLUMN.finditer(upgrade)]
                 + [(m.start(), "rename", m.groups()) for m in OP_RENAME_COLUMN.finditer(upgrade)])
    for _at, op, args in ops:
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


def _package_json(path: str, files: set, source, cache: Dict[str, dict]) -> dict:
    """The dependencies of the package.json nearest above `path`."""
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    while True:
        name = f"{folder}/package.json" if folder else "package.json"
        if name in files:
            if name not in cache:
                try:
                    data = json.loads(source.read(name) or "{}")
                    cache[name] = data.get("dependencies") or {} if isinstance(data, dict) else {}
                except ValueError:
                    cache[name] = {}
            return cache[name]
        if not folder:
            return {}
        folder = folder.rsplit("/", 1)[0] if "/" in folder else ""


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


def extract(source, cfg: Dict[str, Any]) -> dict:
    """The blueprint of one source."""
    code_of = Code(cfg)
    comps = cfg.get("components", [])
    files = [f for f in source.files if not code_of.skip.search(f)]
    fileset = set(files)
    code = [f for f in files if code_of.is_code(f) and not code_of.is_test(f)]
    python_paths = import_paths(files, cfg["code"]["python_paths"])
    found_prefixes = path_prefixes(files, source.read) if any(not f.endswith(".py") for f in files) else {}

    def aliases_of(f: str) -> Dict[str, str]:
        """The import prefixes a file is written with: the nearest tsconfig.json's, then the configured ones."""
        folder = max((d for d in found_prefixes if not d or f.startswith(d + "/")), key=len, default=None)
        return {**(found_prefixes.get(folder) or {}), **cfg["code"]["aliases"]} if folder is not None \
            else cfg["code"]["aliases"]

    owner: Dict[str, str] = {}
    components: Dict[str, dict] = {}
    ui_files: Counter = Counter()
    for n, f in enumerate(code):
        if n % 25 == 0:
            progress.stage(progress._stage or "Reading the code", n, len(code))
        cid = code_of.part_of(f)
        owner[f] = cid
        meta = next((c for c in comps if c["id"] == cid), None)
        entry = components.setdefault(cid, {
            "label": (meta or {}).get("label", cid), "layer": meta.get("layer", "") if meta else "undeclared",
            "kind": (meta or {}).get("kind", ""), "order": comps.index(meta) if meta else len(comps),
            "container": (meta or {}).get("container", ""), "declared": meta is not None, "files": 0, "lines": 0,
            "hub": bool((meta or {}).get("hub")), "quiet": bool((meta or {}).get("quiet"))})
        entry["files"] += 1
        ui_files[cid] += f.endswith(UI_EXT)

    texts = source.read_many(code)
    own = first_party(source.files, cfg["code"]["python_paths"])
    flows, api, env_by_comp = Counter(), Counter(), defaultdict(Counter)
    uses: Dict[str, Counter] = defaultdict(Counter)
    stack: Dict[str, Counter] = defaultdict(Counter)
    imported: Dict[str, Counter] = defaultdict(Counter)  # every outside name a part imports, by how many files
    aws: Dict[str, Counter] = defaultdict(Counter)
    for f in code:
        text, cid = texts[f], owner[f]
        components[cid]["lines"] += text.count("\n")
        python = f.endswith(".py")
        reached = (py_imports(f, text, fileset, python_paths) if python
                   else ts_imports(f, text, fileset, aliases_of(f)))
        for t, names in reached:
            if t in owner and owner[t] != cid:
                flows[f"{cid}>{owner[t]}"] += 1
                uses[f"{cid}>{owner[t]}"].update(names)
        outside = outside_imports(f, text, own, aliases_of(f))
        imported[cid].update(outside)
        stack[cid].update({top_package(n) for n in outside})
        for n in outside:
            if n.startswith("@aws-sdk/client-"):
                aws[n.removeprefix("@aws-sdk/client-")][cid] += 1
        if any(_library(n, ("boto3", "aioboto3")) for n in outside):
            for svc in set(AWS_CALL.findall(text)):
                aws[{"sesv2": "ses"}.get(svc, svc)][cid] += 1
        if python:
            env_by_comp[cid].update(a or b for a, b in ENV_PY.findall(text))
            env_by_comp[cid].update(settings_fields(text))
        else:
            env_by_comp[cid].update(ENV_TS.findall(text))
            for s in API_STR.findall(text) if cfg.get("api") else ():
                target = next((a["component"] for a in cfg.get("api", []) if s.startswith(a["prefix"])), None)
                if target and target != cid:
                    api[f"{cid}>{target}"] += 1
                    uses[f"{cid}>{target}"][api_path(s)] += 1

    routes = find_routes(code, texts, owner, fileset, python_paths, aliases_of)
    if not cfg.get("api"):  # nothing says who serves what the front end calls: the routes do
        served = sorted(((r.split(" ", 1)[1], cid) for r, cid in routes.items()),
                        key=lambda rc: (sum(map(_param, _segments(rc[0]))), rc[0]))
        for f in code:
            if f.endswith(".py"):
                continue
            for path in called_paths(texts[f]):
                target = next((cid for route, cid in served if path_matches(path, route)), None)
                if target and target != owner[f]:
                    api[f"{owner[f]}>{target}"] += 1
                    uses[f"{owner[f]}>{target}"][path] += 1

    compose = compose_file(cfg, fileset)
    config_rx = [glob_rx(p) for p in cfg.get("config_files", [])]
    configured = set()
    for f in files:
        if f == compose or any(r.match(f) for r in config_rx):
            configured |= set(CONFIG_KEY.findall(source.read(f)))
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

    containers = compose_services(source.read(compose)) if compose in fileset else {}

    tables, migrations = defaultdict(set), []
    mig = cfg.get("migrations", {})
    glob = mig.get("glob") or SQL_MIGRATIONS
    mig_files = sorted(f for f in files if glob_rx(glob).match(f))
    mig_texts = source.read_many(mig_files)
    for f in mig_files:
        name = f.rsplit("/", 1)[1]
        migrations.append(name)
        num = int(re.match(r"\d+", name).group(0)) if re.match(r"\d+", name) else 0
        schema = next((s for lo, hi, s in mig.get("schemas", []) if lo <= num <= hi), "public")
        text = SQL_COMMENT.sub("", mig_texts[f])  # "CREATE TABLE IF NOT EXISTS only." in a comment is not a table
        for table in CREATE_TABLE.findall(text):
            tables[qualify(table, schema)] |= set()
        for table, col in ADD_COLUMN.findall(text):
            tables[qualify(table, schema)].add(col)
    data_parts = set()
    if not mig.get("glob"):  # where the config names no migrations, the tables the code itself makes and defines
        revisions = {f: t for f, t in source.read_many([f for f in files if ALEMBIC_FILE.search(f)]).items()
                     if "op." in t and "def upgrade" in t}
        for f in alembic_order(revisions):
            migrations.append(f.rsplit("/", 1)[1])
            alembic_run(_upgrade(revisions[f]), tables)
        for f in code:
            if f.endswith(".py"):
                found = model_tables(f, texts[f])
                for table in found:
                    tables[qualify(table, "public")] |= set()
                if found:
                    data_parts.add(owner[f])
        prisma = [f for f in files if f.endswith(".prisma")]
        for f, text in source.read_many(prisma).items():
            for model, body in PRISMA_MODEL.findall(text):
                mapped = PRISMA_MAP.search(body)
                tables[qualify(mapped.group(1) if mapped else model, "public")] |= set()

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

    serving, packages = set(routes.values()), {}
    for cid, entry in components.items():  # what people use, where nobody said: pages, or a UI library
        if entry["declared"] or entry["kind"] or cid in serving:
            continue
        sample = next(f for f in code if owner[f] == cid)
        package = {} if sample.endswith(".py") else _package_json(sample, fileset, source, packages)
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
