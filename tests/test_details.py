"""What a block is, does and connects to, in plain words; and how the system reaches each outside system, where the
repository says so (compose, example env files, the code's defaults), with every secret kept out."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from system_diff import architecture, config, extract, human, pane
from system_diff.compare import compare
from system_diff.sources import GitSource, git_dir

from conftest import git_

PG_PASSWORD = "pg-hunter2-secret"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
SECRETS = (PG_PASSWORD, AWS_SECRET, "minio-root-pw-123", "changeme-example-pw", "default-pw-in-code",
           "example-secret-value-xyz", "REAL-DOTENV-PASSWORD", "dotenv-only-value-42", "prod.db.internal")

FILES = {
    "docker-compose.yml": f"""\
services:
  db:
    image: postgres:16
    ports:
      - "5433:5432"
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: {PG_PASSWORD}
      POSTGRES_DB: flows
  search:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.13.0
    environment:
      - discovery.type=single-node
    ports: ["9200:9200"]
  minio:
    image: minio/minio:latest
    command: server /data
    ports:
      - "9000:9000"
    environment:
      MINIO_ROOT_USER: minio
      MINIO_ROOT_PASSWORD: minio-root-pw-123
  backend:
    build: .
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://app:{PG_PASSWORD}@db:5432/flows
      - ELASTICSEARCH_URL=http://search:9200
      - AWS_SECRET_ACCESS_KEY={AWS_SECRET}
      - S3_ENDPOINT_URL=http://minio:9000
      - S3_BUCKET=flow-uploads
""",
    ".env.example": ("DATABASE_URL=postgresql://app:changeme-example-pw@localhost:5432/flows\nAWS_REGION=eu-west-1\n"
                     "AWS_SECRET_ACCESS_KEY=example-secret-value-xyz\nELASTICSEARCH_INDEX=flows\n"),
    ".env": ("DATABASE_URL=postgresql://real:REAL-DOTENV-PASSWORD@prod.db.internal:5432/prod\n"
             "REAL_ONLY=dotenv-only-value-42\n"),
    "app/__init__.py": "",
    "app/settings.py": ('import os\n\nDATABASE_URL = os.getenv("DATABASE_URL", '
                        '"postgresql://app:default-pw-in-code@localhost:5432/flows")\n'
                        'ELASTICSEARCH_URL = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")\n'
                        'S3_BUCKET = os.getenv("S3_BUCKET")\nAWS_REGION = os.getenv("AWS_REGION", "us-east-1")\n'),
    "app/db/__init__.py": "",
    "app/db/session.py": ('from sqlalchemy import create_engine\nimport psycopg\nfrom app.settings import DATABASE_URL\n\n'
                          'engine = create_engine(DATABASE_URL)\n\n\ndef runs():\n'
                          '    return engine.execute("SELECT * FROM flow_runs")\n'),
    "app/db/models.py": ('from sqlalchemy import Column, Integer\nfrom sqlalchemy.orm import declarative_base\n\n'
                         'Base = declarative_base()\n\n\nclass Flow(Base):\n    __tablename__ = "flows"\n'
                         '    id = Column(Integer, primary_key=True)\n\n\nclass FlowRun(Base):\n'
                         '    __tablename__ = "flow_runs"\n    id = Column(Integer, primary_key=True)\n'),
    "app/search/__init__.py": "",
    "app/search/index.py": ('from elasticsearch import Elasticsearch\nfrom app.settings import ELASTICSEARCH_URL\n\n'
                            'es = Elasticsearch(ELASTICSEARCH_URL)\n\n\ndef find(q):\n'
                            '    return es.search(index="flows", q=q)\n'),
    "app/storage/__init__.py": "",
    "app/storage/files.py": ('import os\nimport boto3\n\ns3 = boto3.client("s3", endpoint_url=os.getenv("S3_ENDPOINT_URL"), '
                             'region_name="eu-west-1")\n\n\ndef put(key, body):\n'
                             '    s3.put_object(Bucket="flow-uploads", Key=key, Body=body)\n'),
    "app/cache/__init__.py": "",
    "app/cache/redis_cache.py": 'import os\nimport redis\n\nclient = redis.from_url(os.environ["REDIS_URL"])\n',
    "app/api/__init__.py": "",
    "app/api/routes.py": ('from fastapi import APIRouter\nfrom app.db import session\nfrom app.search import index\n'
                          'from app.storage import files\nfrom app.cache import redis_cache\n\n'
                          'router = APIRouter(prefix="/flows")\n\n\n@router.get("")\ndef list_flows():\n'
                          '    return session.runs()\n\n\n@router.post("/{id}/run")\ndef run(id):\n'
                          '    return files.put(id, b"")\n\n\n@router.get("/search")\ndef search(q):\n'
                          '    return index.find(q)\n'),
    "app/main.py": ('from fastapi import FastAPI\nfrom app.api.routes import router\n\napp = FastAPI()\n'
                    'app.include_router(router)\n'),
    "web/src/App.tsx": 'import React from "react";\n\nexport const App = () => fetch("/flows");\n',
    "web/package.json": '{"name": "web", "dependencies": {"react": "18"}}\n',
}


@pytest.fixture
def flows(tmp_path: Path, files) -> Path:
    r = tmp_path / "flows"
    r.mkdir()
    git_(r, "init", "-q", "-b", "main")
    files(r, FILES)
    git_(r, "add", "-A")
    git_(r, "add", "-f", ".env")   # even a .env someone committed: its values are never read
    git_(r, "commit", "-q", "-m", "flows")
    return r


def _bp(root: Path) -> dict:
    return compare(root, "checkpoint", ".").after


def test_each_outside_system_says_where_it_is_and_how_it_is_reached(flows):
    bp = _bp(flows)
    conn = bp["connections"]
    pg = conn["postgres"]
    assert (pg["host"], pg["port"], pg["database"], pg["scheme"]) == ("db", "5432", "flows", "postgresql")
    assert pg["url"] == "postgresql://app:***@db:5432/flows"
    assert pg["service"]["name"] == "db" and pg["service"]["image"] == "postgres:16"
    assert "5433:5432" in pg["service"]["ports"]
    assert {"SQLAlchemy", "psycopg"} <= set(pg["clients"])
    assert "app/db" in pg["parts"] and "psycopg" in pg["parts"]["app/db"]["clients"]
    names = {s["name"]: s for s in pg["settings"]}
    assert names["DATABASE_URL"]["where"] == "docker-compose.yml"
    assert names["POSTGRES_PASSWORD"]["secret"] and names["POSTGRES_PASSWORD"]["value"] is None

    es = conn["elasticsearch"]
    assert (es["host"], es["port"]) == ("search", "9200") and "flows" in es["index"]
    assert "elasticsearch-py" in es["clients"] and "app/search" in es["parts"]

    s3 = conn["aws-s3"]
    assert s3["bucket"] == ["flow-uploads"] and s3["region"] == "eu-west-1"
    assert (s3["host"], s3["port"]) == ("minio", "9000") and s3["service"]["name"] == "minio"
    assert "boto3" in s3["clients"] and "app/storage" in s3["parts"]
    assert "S3_ENDPOINT_URL" in s3["parts"]["app/storage"]["settings"]

    rd = conn["redis"]   # read by the code, set nowhere in the repository
    assert not rd.get("host") and {"REDIS_URL"} <= {s["name"] for s in rd["settings"]}
    assert next(s for s in rd["settings"] if s["name"] == "REDIS_URL")["value"] is None
    assert any("set at deploy time" in line for line in human.connection_lines(rd))

    assert bp["part_tables"]["app/db"]["defines"] == ["public.flow_runs", "public.flows"]


def test_no_secret_reaches_the_page_or_the_facts_kept_on_disk(flows):
    pages = [pane.web_view(flows, view, "", z) for view in ("now", "review", "agents", "topics") for z in (0, 1)]
    pages.append(pane.web_view(flows, "now", "x:postgres", 1))
    pages += [pane.show(flows, 100, view) for view in ("now", "review")]
    bp = _bp(flows)
    text = json.dumps([pages, bp], ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in text, secret
    extract.extract(GitSource("HEAD", flows), config.load(root=flows))   # a commit's files: their facts are kept
    stored = "".join(p.read_text(encoding="utf-8") for p in git_dir(flows, "files").rglob("*.json"))
    assert "docker-compose.yml" in stored          # the facts are kept…
    for secret in SECRETS:
        assert secret not in stored, secret        # …without a secret
    assert "REAL_ONLY" not in json.dumps(bp["connections"])   # a real .env says nothing about how things are reached


def test_secrets_are_masked_wherever_they_are_written():
    assert extract.redact("postgresql://u:p4ss@h:5432/d") == "postgresql://u:***@h:5432/d"
    assert extract.redact("jdbc:postgresql://u:p4ss@h/d") == "jdbc:postgresql://u:***@h/d"
    assert extract.redact("https://abcdef0123456789abcdef@o1.ingest.sentry.io/1") == "https://***@o1.ingest.sentry.io/1"
    assert extract.redact("https://h/x?token=abc&x=1") == "https://h/x?token=***&x=1"
    assert extract.redact("host=db password=hunter2 dbname=x") == "host=db password=*** dbname=x"
    assert extract.redact("sk-live1234567890abcdef") == "***"
    for name in ("DB_PASSWORD", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "GITHUB_TOKEN", "apiKey", "JWT_SECRET",
                 "GOOGLE_APPLICATION_CREDENTIALS"):
        assert extract.secret_name(name), name
    for name in ("DATABASE_URL", "S3_BUCKET", "AWS_REGION", "ELASTICSEARCH_URL", "POSTGRES_DB"):
        assert not extract.secret_name(name), name
    k8s = extract.k8s_conn("apiVersion: v1\nkind: Secret\nmetadata:\n  name: db\nstringData:\n  DB_URL: postgres://u:p@h/d\n"
                           "---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  template:\n"
                           "    spec:\n      containers:\n        - name: api\n          image: shop/api:1\n"
                           "          env:\n            - name: ES_HOST\n              value: search.internal\n"
                           "            - name: DB_PASS\n              valueFrom:\n                secretKeyRef:\n"
                           "                  name: db\n")
    assert ["DB_URL", None, True, "db"] in k8s["settings"]           # a Secret's value is never kept
    assert ["ES_HOST", "search.internal", False, "api"] in k8s["settings"]
    assert ["DB_PASS", None, True, "api"] in k8s["settings"]
    assert extract.endpoint("redis://cache:6380/0") == {"scheme": "redis", "host": "cache", "port": "6380", "path": "0"}


def test_a_part_says_what_it_serves_keeps_reads_is_built_with_and_connects_to(flows):
    c = compare(flows, "checkpoint", ".")
    model = architecture.system(c)
    said = human.block_details(c, model, [], "this turn")
    api = said["b:app/api"]
    card = "\n".join(api["lines"])
    assert "3 endpoints" in card and "GET /flows" in card and "POST /flows/{id}/run" in card
    assert "Used by Main program and Web app over HTTP; uses Cache, Database access, Search and Storage" in card
    assert "FastAPI" in card
    db = "\n".join(said["b:app/db"]["lines"])
    assert "flow_runs" in db and "SQLAlchemy" in db and "PostgreSQL over SQL at db:5432, database flows" in db
    settings = "\n".join(said["b:app/settings"]["lines"])
    assert "Reads settings AWS_REGION, DATABASE_URL, ELASTICSEARCH_URL and S3_BUCKET" in settings
    for d in said.values():
        assert len(d["lines"]) <= human.CARD
        assert "no change" not in (" ".join(d["lines"]) + d["short"] + d["more"]).lower()
    pg = said["b:x:postgres"]
    assert pg["short"].startswith("at db:5432, database flows (postgresql) via DATABASE_URL")
    assert any("Runs as service db (postgres:16), published on localhost:5433" in line for line in pg["lines"])


def test_the_page_and_the_pane_show_it_and_never_say_nothing_changed(flows):
    page = pane.web_view(flows, "review", "", 0)
    for card in page["hover"].values():
        assert not any("no change to its structure" in line.lower() for line in card["lines"])
    assert "db:5432" in " ".join(page["hover"]["b:x:postgres"]["lines"])
    assert "flow-uploads" in page["diagram"]["details"]["b:x:aws-s3"]
    zoomed = pane.web_view(flows, "review", "", 1)
    assert zoomed["hover"]   # the boxes of a zoomed-in view have their cards too
    shown = pane.show(flows, 100, "review")
    rows = shown["details"]["b:x:postgres"]
    assert len(rows) <= pane.SLOT
    assert "db:5432" in "".join(run[0] for row in rows for run in row)


def test_what_changed_or_when_it_last_did(flows):
    c = compare(flows, "checkpoint", ".")
    model = architecture.system(c)
    decs = [{"title": "Claude added POST /flows/{id}/run", "parts": ["app/api"], "look": True,
             "why": "a new entry point", "request": "let people run a flow"}]
    past = [{"at": time.time() - 2 * 86400, "parts": ["app/search"], "request": "add search"}]
    said = human.block_details(c, model, decs, "this turn", past)
    assert said["b:app/api"]["lines"][1] == "Changed this turn: Claude added POST /flows/{id}/run"
    assert said["b:app/api"]["short"].startswith("changed this turn")
    assert "Last changed 2 days ago: you asked “add search”" in said["b:app/search"]["lines"]
    assert not any("changed" in line.lower() for line in said["b:app/storage"]["lines"])
