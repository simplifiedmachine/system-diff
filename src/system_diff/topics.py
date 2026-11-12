"""The topics system-diff can teach: technologies, known by the names code imports them by, and the ideas behind a
system's structure, each in a sentence or two of plain words with a place to read more. learn.py picks the ones that
matter for the part being worked on and says why they matter there.

A topic: key -> (title, what it is, link). Keys of technologies are import names: a Python module, dotted where its
package matters (`http.server`), or an npm package (`@prisma/client`); ALIASES maps other names to them.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

PY = "https://docs.python.org/3/library/"

TECH: Dict[str, Tuple[str, str, str]] = {
    # ── Python's standard library: the parts worth knowing ──
    "subprocess": ("Subprocesses", "Running another program (git, a compiler, a browser) from Python and reading what "
                   "it prints. Each call starts a new process: slower than a function call, and it can fail on its own.",
                   PY + "subprocess.html"),
    "threading": ("Threads", "Doing several things at once in one program, e.g. answering requests while a loop "
                  "watches files. Data the threads share needs a lock, or they can see it half-changed.",
                  PY + "threading.html"),
    "asyncio": ("Async I/O (asyncio)", "Many waits (network, disk) handled at once in one thread: an `async` function "
                "pauses at each `await` while others run.", PY + "asyncio.html"),
    "multiprocessing": ("Processes (multiprocessing)", "Work spread over several processes to use more than one CPU "
                        "core; data is copied between them, not shared.", PY + "multiprocessing.html"),
    "concurrent.futures": ("Worker pools", "Hands tasks to a pool of threads or processes and collects their results.",
                           PY + "concurrent.futures.html"),
    "http.server": ("Python's HTTP server", "A small web server in the standard library: right for local tools and "
                    "tests, not built to face the open internet.", PY + "http.server.html"),
    "urllib.request": ("HTTP requests (urllib)", "The standard library's way to call a URL and read the answer, with "
                       "nothing to install.", PY + "urllib.request.html"),
    "socket": ("Sockets", "The lowest level of network code: a connection to a host and port that bytes go over.",
               PY + "socket.html"),
    "ssl": ("TLS (ssl)", "Encrypts network connections and checks that the server is who it claims to be (its "
            "certificate). Turning the check off turns that protection off.", PY + "ssl.html"),
    "sqlite3": ("SQLite", "A whole SQL database in one file, built into Python: no server to run.", PY + "sqlite3.html"),
    "argparse": ("Command-line parsing (argparse)", "Turns the words typed after a command into options and arguments, "
                 "and writes its --help.", PY + "argparse.html"),
    "tomllib": ("TOML", "A config format of `key = value` lines under `[sections]`; Python reads it with tomllib.",
                "https://toml.io/"),
    "json": ("JSON", "Plain-text data (objects, lists, strings, numbers): how most programs and web APIs exchange data.",
             PY + "json.html"),
    "logging": ("Logging", "Messages a running program writes about what it does, at levels from debug to error, to "
                "find out later what went wrong.", PY + "logging.html"),
    "tempfile": ("Temporary files", "Files and folders made for the moment in the system's temp folder, and removed "
                 "after.", PY + "tempfile.html"),
    "hashlib": ("Hashing", "A short fingerprint of some data (e.g. SHA-256): the same data always gives the same "
                "fingerprint, and any change gives another.", PY + "hashlib.html"),
    "secrets": ("Secure random values", "Random tokens too hard to guess, for keys and session ids; the `random` "
                "module is not safe for that.", PY + "secrets.html"),
    "html": ("HTML escaping", "Turning <, > and & into entities so text shows as text instead of running as markup: "
             "what stops text from a user from injecting code into a page.", PY + "html.html"),
    "re": ("Regular expressions", "Patterns that find and pull pieces out of text.", PY + "re.html"),
    "pickle": ("pickle", "Saves Python objects as bytes and loads them back. Loading pickle data from someone else can "
               "run their code.", PY + "pickle.html"),
    "csv": ("CSV files", "Tables as plain text, one row a line, values separated by commas.", PY + "csv.html"),
    "smtplib": ("Sending email (SMTP)", "Hands an email to a mail server to deliver.", PY + "smtplib.html"),
    "unittest": ("unittest", "Python's built-in way to write tests.", PY + "unittest.html"),
    "xml.etree": ("XML", "Reading and writing XML, the tag-based data format.", PY + "xml.etree.elementtree.html"),
    # ── Python packages ──
    "fastapi": ("FastAPI", "A Python web framework for HTTP APIs: each endpoint is a function with a decorator such "
                "as `@router.get(\"/orders\")`, and type hints describe and check what goes in and out.",
                "https://fastapi.tiangolo.com/"),
    "starlette": ("Starlette", "The small async web toolkit FastAPI is built on.", "https://www.starlette.io/"),
    "flask": ("Flask", "A small Python web framework: each URL is a function with an `@app.route` decorator.",
              "https://flask.palletsprojects.com/"),
    "django": ("Django", "A Python web framework with everything included: URLs, views, a database ORM, migrations, "
               "an admin site and logins.", "https://docs.djangoproject.com/"),
    "uvicorn": ("Uvicorn", "The server that runs an async Python web app (FastAPI, Starlette) and answers its HTTP "
                "requests.", "https://www.uvicorn.org/"),
    "gunicorn": ("Gunicorn", "Runs a Python web app in several worker processes, so it can answer requests in "
                 "parallel.", "https://gunicorn.org/"),
    "aiohttp": ("aiohttp", "An async HTTP client and server for Python.", "https://docs.aiohttp.org/"),
    "requests": ("Requests", "The most used Python library for calling HTTP APIs: `requests.get(url)`, then read the "
                 "response.", "https://requests.readthedocs.io/"),
    "httpx": ("HTTPX", "An HTTP client for Python like Requests, that can also be async.", "https://www.python-httpx.org/"),
    "pydantic": ("Pydantic", "Describes data as Python classes with type hints and checks incoming data against them, "
                 "with clear errors when it does not fit.", "https://docs.pydantic.dev/"),
    "sqlalchemy": ("SQLAlchemy", "Python's most used database toolkit: queries written in Python, tables mapped to "
                   "classes (an ORM), many databases spoken to the same way.", "https://docs.sqlalchemy.org/"),
    "alembic": ("Alembic", "Migrations for SQLAlchemy: versioned scripts that change the database's tables step by "
                "step.", "https://alembic.sqlalchemy.org/"),
    "psycopg": ("Psycopg", "The PostgreSQL driver for Python: it opens the connection and runs the SQL.",
                "https://www.psycopg.org/"),
    "asyncpg": ("asyncpg", "An async PostgreSQL driver for asyncio.", "https://magicstack.github.io/asyncpg/"),
    "pymongo": ("PyMongo", "The MongoDB driver for Python: documents in, documents out.",
                "https://pymongo.readthedocs.io/"),
    "redis": ("Redis", "An in-memory data store, used as a cache, a queue or for counters: very fast, and what it "
              "holds can be lost unless it is set up to keep it.", "https://redis.io/docs/"),
    "celery": ("Celery", "Runs slow work (emails, reports) in background workers fed by a queue, so a request does not "
               "wait for it.", "https://docs.celeryq.dev/"),
    "boto3": ("AWS SDK (boto3)", "Python's client for Amazon Web Services: S3 storage, queues and the rest.",
              "https://boto3.amazonaws.com/v1/documentation/api/latest/index.html"),
    "google.cloud": ("Google Cloud client libraries", "Python clients for Google Cloud services such as storage and "
                     "databases.", "https://cloud.google.com/python/docs/reference"),
    "stripe": ("Stripe", "Payments through Stripe's API; its webhooks tell the system what happened to each payment.",
               "https://docs.stripe.com/"),
    "anthropic": ("Anthropic SDK", "The client for Claude models: send messages, get replies.",
                  "https://docs.anthropic.com/"),
    "openai": ("OpenAI SDK", "The client for OpenAI's models.", "https://platform.openai.com/docs"),
    "mcp": ("Model Context Protocol (MCP)", "An open protocol that lets AI apps such as Claude Code call tools on a "
            "server: the server lists its tools, and the AI decides when to call which.",
            "https://modelcontextprotocol.io/"),
    "click": ("Click", "Command-line tools in Python from decorated functions.", "https://click.palletsprojects.com/"),
    "typer": ("Typer", "Command-line tools in Python from functions with type hints.", "https://typer.tiangolo.com/"),
    "textual": ("Textual", "Apps that run in the terminal, with widgets, layout, keyboard and mouse.",
                "https://textual.textualize.io/"),
    "rich": ("Rich", "Colour, tables and formatting for terminal output.", "https://rich.readthedocs.io/"),
    "pytest": ("pytest", "Python's most used test runner: functions named test_… with plain asserts.",
               "https://docs.pytest.org/"),
    "numpy": ("NumPy", "Fast arrays of numbers and the maths on them; the base of Python's data tools.",
              "https://numpy.org/doc/"),
    "pandas": ("pandas", "Tables of data (DataFrames) in Python: load, filter, group and join.",
               "https://pandas.pydata.org/docs/"),
    "jwt": ("JSON Web Tokens (PyJWT)", "Signed tokens that say who a request comes from; the server checks the "
            "signature instead of looking up a session.", "https://pyjwt.readthedocs.io/"),
    "yaml": ("YAML (PyYAML)", "A config format of indented `key: value` lines; `safe_load` is the safe way to read it.",
             "https://pyyaml.org/wiki/PyYAMLDocumentation"),
    "jinja2": ("Jinja", "Templates: HTML or text with {{ placeholders }} filled in from data.",
               "https://jinja.palletsprojects.com/"),
    "playwright": ("Playwright", "Drives a real browser from code, for tests, screenshots and rendering pages.",
                   "https://playwright.dev/python/"),
    "docker": ("Docker SDK", "Starts and manages containers from Python.", "https://docker-py.readthedocs.io/"),
    "kafka": ("Kafka", "A log of events that many services write to and read from, in order, each at its own pace.",
              "https://kafka.apache.org/documentation/"),
    "pika": ("RabbitMQ (pika)", "A message queue: one part puts messages in, workers take them out.",
             "https://www.rabbitmq.com/docs"),
    "grpc": ("gRPC", "Calls between services defined in .proto files, sent as compact binary.", "https://grpc.io/docs/"),
    "websockets": ("WebSockets", "A connection kept open between browser and server, so either side can send at any "
                   "time.", "https://websockets.readthedocs.io/"),
    "cryptography": ("cryptography", "Encryption, signatures and certificates for Python.", "https://cryptography.io/"),
    "bcrypt": ("Password hashing (bcrypt)", "Stores passwords as slow, salted hashes, so a stolen database does not "
               "give the passwords away.", "https://pypi.org/project/bcrypt/"),
    "PIL": ("Pillow", "Opening, changing and saving images in Python.", "https://pillow.readthedocs.io/"),
    "bs4": ("Beautiful Soup", "Pulls data out of HTML pages.", "https://www.crummy.com/software/BeautifulSoup/bs4/doc/"),
    "torch": ("PyTorch", "Builds and trains neural networks.", "https://pytorch.org/docs/"),
    "sklearn": ("scikit-learn", "Classic machine learning in Python: classify, predict, cluster.",
                "https://scikit-learn.org/stable/"),
    # ── JavaScript and TypeScript packages ──
    "react": ("React", "Builds user interfaces from components: functions that return what to show for the data "
              "they are given; React redraws what changed.", "https://react.dev/"),
    "next": ("Next.js", "A React framework: pages from files, rendering on the server, and API routes.",
             "https://nextjs.org/docs"),
    "vue": ("Vue", "A framework for user interfaces built from components.", "https://vuejs.org/"),
    "svelte": ("Svelte", "Builds user interfaces from components, compiled to plain JavaScript.",
               "https://svelte.dev/docs"),
    "@angular/core": ("Angular", "A framework for large web apps: components, services and routing.",
                      "https://angular.dev/"),
    "express": ("Express", "A minimal web server for Node.js: `app.get('/path', handler)` answers requests.",
                "https://expressjs.com/"),
    "fastify": ("Fastify", "A fast web server framework for Node.js.", "https://fastify.dev/"),
    "axios": ("Axios", "An HTTP client for browsers and Node.js.", "https://axios-http.com/"),
    "zod": ("Zod", "Describes the shape of data in TypeScript and checks values against it while the program runs.",
            "https://zod.dev/"),
    "@prisma/client": ("Prisma", "A database toolkit for TypeScript: a schema file, typed queries generated from it, "
                       "and migrations.", "https://www.prisma.io/docs"),
    "mongoose": ("Mongoose", "Models for MongoDB documents in Node.js.", "https://mongoosejs.com/docs/"),
    "pg": ("node-postgres", "The PostgreSQL client for Node.js.", "https://node-postgres.com/"),
    "jsonwebtoken": ("JSON Web Tokens", "Signed tokens that say who a request comes from; the server checks the "
                     "signature instead of looking up a session.", "https://jwt.io/introduction"),
    "socket.io": ("Socket.IO", "Live two-way messages between browser and server.", "https://socket.io/docs/"),
    "@tanstack/react-query": ("TanStack Query", "Fetches, caches and refreshes server data in a front end.",
                              "https://tanstack.com/query"),
    "graphql": ("GraphQL", "A query language for APIs: the caller asks for exactly the fields it needs.",
                "https://graphql.org/learn/"),
    "react-router": ("React Router", "Which page a React app shows for which URL.", "https://reactrouter.com/"),
    "redux": ("Redux", "Keeps a front end's state in one store, changed only by actions.", "https://redux.js.org/"),
    "d3": ("D3", "Charts and data pictures drawn from data in the browser.", "https://d3js.org/"),
    "three": ("three.js", "3D graphics in the browser.", "https://threejs.org/docs/"),
}

ALIASES = {
    "psycopg2": "psycopg", "botocore": "boto3", "confluent_kafka": "kafka", "redis.asyncio": "redis",
    "react-dom": "react", "react-router-dom": "react-router", "@reduxjs/toolkit": "redux", "ioredis": "redis",
    "socket.io-client": "socket.io", "@apollo/client": "graphql", "@anthropic-ai/sdk": "anthropic",
    "@modelcontextprotocol/sdk": "mcp", "starlette.applications": "starlette", "xml.dom": "xml.etree",
    "passlib": "bcrypt", "aiosqlite": "sqlite3",
}

CONCEPT: Dict[str, Tuple[str, str, str]] = {
    "entry-point": ("Entry points and APIs", "Where the outside world reaches a system: HTTP endpoints, a command line, "
                    "a queue consumer, a server for AI tools. Each is a contract: callers rely on what it accepts and "
                    "returns.", "https://en.wikipedia.org/wiki/API"),
    "http-endpoint": ("HTTP endpoints", "A method and a path (`GET /api/orders`) that a server answers. GET reads; "
                      "POST, PUT, PATCH and DELETE change something; the path names what.",
                      "https://developer.mozilla.org/en-US/docs/Web/HTTP/Methods"),
    "front-end": ("Front ends", "Code that runs in a browser or app: it draws what people see and calls the system's "
                  "API for the data.", "https://developer.mozilla.org/en-US/docs/Learn_web_development"),
    "layers": ("Layers", "A system split into what people use, entry points, logic and data access, each calling the "
               "next, so a change in one stays in it.",
               "https://martinfowler.com/bliki/PresentationDomainDataLayering.html"),
    "data-access": ("Data access", "Code that hides how data is stored (SQL, files, an API) behind plain functions, so "
                    "the rest of the system asks for what it needs without knowing where it lives.",
                    "https://en.wikipedia.org/wiki/Data_access_layer"),
    "settings": ("Settings from the environment", "Values such as database addresses and API keys are read from "
                 "environment variables, set differently wherever the system runs, never written in the code.",
                 "https://12factor.net/config"),
    "outside-systems": ("Calling outside systems", "Anything reached over the network can be slow or down: give each "
                        "call a timeout, retry with care, and decide what happens when it fails.",
                        "https://martinfowler.com/bliki/CircuitBreaker.html"),
    "databases": ("Databases and SQL", "A database keeps data safe across restarts and lets many requests read and "
                  "write it at once; SQL describes its tables and asks for rows.",
                  "https://www.postgresql.org/docs/current/tutorial-concepts.html"),
    "migrations": ("Schema migrations", "Numbered scripts that change a database's tables step by step, run once, in "
                   "order, on every copy of the database, so its shape follows the code.",
                   "https://en.wikipedia.org/wiki/Schema_migration"),
    "coupling": ("Coupling", "A part that imports another depends on it: the more parts depend on one, the further a "
                 "change in it spreads.", "https://en.wikipedia.org/wiki/Coupling_(computer_programming)"),
    "breaking-changes": ("Breaking changes", "Removing or changing what callers rely on breaks them, unless it is done "
                         "in steps: add the new, move the callers, then remove the old.", "https://semver.org/"),
    "tests": ("Automated tests", "Code that runs the system's code and checks the result, so a change that breaks "
              "something is caught before it ships.", "https://martinfowler.com/articles/practical-test-pyramid.html"),
    "deployment": ("Deployment", "How code becomes a running system: what is built, which containers run, and with "
                   "which settings.", "https://12factor.net/build-release-run"),
    "containers": ("Containers", "A program packed with everything it needs to run, so it runs the same on a laptop "
                   "and in production; a compose file says which containers run together.",
                   "https://docs.docker.com/get-started/docker-overview/"),
}


# A package as the blueprint's "stack" records it (only the top name: `concurrent` for `concurrent.futures`) -> its
# topic, where that topic is the one the package is imported for. Left out where the top name says too little
# (`http` may be http.client, `urllib` urllib.parse, `google` any of Google's): the blueprint's "imported" names,
# when it has them, say which.
TOP_PACKAGE = {"concurrent": "concurrent.futures", "xml": "xml.etree"}

def tech_key(name: str) -> Optional[str]:
    """The topic of an imported name: itself, an alias, or its nearest package (`sqlalchemy.orm` -> sqlalchemy)."""
    parts = name.split(".")
    while parts:
        key = ".".join(parts)
        key = ALIASES.get(key, key)
        if key in TECH:
            return key
        parts.pop()
    return None


# What a technology does in a system's design, in a phrase: the role it plays, not what it is.
_ROLES = {
    "the HTTP layer of an entry point: routing, request parsing and validation": (
        "fastapi", "starlette", "flask", "django", "express", "fastify", "aiohttp"),
    "the server process that runs the web app (ASGI/WSGI)": ("uvicorn", "gunicorn"),
    "data contracts: checks what crosses a boundary": ("pydantic", "zod"),
    "data access: maps code to the database and back": ("sqlalchemy", "@prisma/client", "mongoose", "pg", "psycopg",
                                                        "asyncpg", "pymongo", "sqlite3"),
    "schema migrations: versioned changes to stored data": ("alembic",),
    "shared state outside the process: cache, locks, queues": ("redis",),
    "asynchronous work: queues and workers off the request path": ("celery", "kafka", "pika"),
    "outbound calls to other services over HTTP": ("requests", "httpx", "axios", "urllib.request"),
    "the UI layer: components and rendering": ("react", "vue", "svelte", "@angular/core", "next"),
    "client-side routing between screens": ("react-router",),
    "client-side data: fetching, caching, state": ("@tanstack/react-query", "redux"),
    "identity and secrets: tokens, hashes, keys": ("jwt", "jsonwebtoken", "bcrypt", "cryptography", "secrets"),
    "a process boundary: runs other programs, with their own failures and latency": ("subprocess",),
    "the concurrency model: what runs at the same time, and what they share": (
        "threading", "asyncio", "multiprocessing", "concurrent.futures"),
    "a network interface others call": ("http.server", "websockets", "socket.io", "grpc", "graphql", "socket"),
    "a tool interface for AI agents": ("mcp",),
    "the terminal interface: commands, screens, output": ("textual", "rich", "click", "typer", "argparse"),
    "tests: what checks the system keeps working": ("pytest", "unittest", "playwright"),
    "an outside service's client: a runtime dependency with its own limits and costs": (
        "stripe", "anthropic", "openai", "boto3", "google.cloud"),
    "containers: how the system is packaged and run": ("docker",),
    "configuration: read at start, set where the system runs": ("tomllib", "yaml"),
}
ROLE_OF: Dict[str, str] = {key: role for role, keys in _ROLES.items() for key in keys}
