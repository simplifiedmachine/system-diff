"""What the extractor captures beyond the parts: what each connection carries (`uses`), each part's packages (`stack`),
what a deploy configuration sets (`configured`), a front end's column, and, with no system-diff.toml at all, a fair
first map of a FastAPI + React repository: routes under the prefixes they are mounted with, the front end's calls to
them, the tables, the outside systems, and no tests or tooling drawn as parts."""
from __future__ import annotations

import json
from collections import Counter

from system_diff import compare, config, graph
from system_diff.extract import Code, alembic_order, api_path, extract, path_matches, py_imports, ts_imports
from system_diff.sources import DirSource

FULLSTACK = {
    "backend/app/__init__.py": "",
    "backend/app/main.py": ('import sentry_sdk\nfrom fastapi import FastAPI\n\nfrom app.api.main import api_router\n'
                            'from app.core.config import settings\n\napp = FastAPI(title="shop")\n'
                            'app.include_router(api_router, prefix=settings.API_V1_STR)\n'),
    "backend/app/models.py": ('from sqlmodel import Field, SQLModel\n\n\nclass ItemBase(SQLModel):\n    title: str\n\n\n'
                              'class Item(ItemBase, table=True):\n    id: int = Field(primary_key=True)\n'),
    "backend/app/crud.py": "from app.models import Item\n\n\ndef items(session):\n    return session.query(Item)\n",
    "backend/app/api/__init__.py": "",
    "backend/app/api/main.py": ("from fastapi import APIRouter\n\nfrom app.api.routes import items\n\n"
                                "api_router = APIRouter()\napi_router.include_router(items.router)\n"),
    "backend/app/api/routes/__init__.py": "",
    "backend/app/api/routes/items.py": ('from fastapi import APIRouter\n\nfrom app import crud\nfrom app.core import security\n\n'
                                        'router = APIRouter(prefix="/items", tags=["items"])\n\n\n@router.get("/")\n'
                                        'def read_items():\n    return crud.items(None)\n\n\n@router.delete("/{id}")\n'
                                        'def delete_item(id: int):\n    security.check()\n'),
    "backend/app/core/__init__.py": "",
    "backend/app/core/config.py": ('from pydantic_settings import BaseSettings\n\n\nclass Settings(BaseSettings):\n'
                                   '    API_V1_STR: str = "/api/v1"\n    DATABASE_URL: str\n'
                                   '    SMTP_HOST: str | None = None\n    model_config = {"env_file": ".env"}\n\n'
                                   '    def emails_enabled(self) -> bool:\n        LOCAL: int = 1\n'
                                   '        return bool(self.SMTP_HOST)\n\n\nsettings = Settings()\n'),
    "backend/app/core/security.py": "def check():\n    return True\n",
    "backend/app/alembic/env.py": "from app.models import SQLModel\n",
    # named against the order they run in: the down_revision chain decides
    "backend/app/alembic/versions/a_second.py": ('revision = "bbb"\ndown_revision = "aaa"\n\n\ndef upgrade():\n'
                                                 '    op.add_column("item", sa.Column("note", sa.String()))\n\n\n'
                                                 'def downgrade():\n    op.drop_column("item", "note")\n'),
    "backend/app/alembic/versions/z_first.py": ('revision = "aaa"\ndown_revision = None\n\n\ndef upgrade():\n'
                                                '    op.create_table(\n        "item",\n        sa.Column("id", sa.Integer()),\n'
                                                '    )\n    op.create_table("audit")\n\n\ndef downgrade():\n'
                                                '    op.drop_table("item")\n'),
    "backend/tests/test_items.py": "from app.api.routes import items\n",
    "frontend/package.json": json.dumps({"dependencies": {"react": "19", "axios": "1"}}),
    "frontend/tsconfig.json": '{\n  "compilerOptions": {\n    /* bundler mode */\n    "paths": {"@/*": ["./src/*"]},\n  },\n}\n',
    "frontend/vite.config.ts": 'import { defineConfig } from "vite"\nexport default defineConfig({})\n',
    "frontend/src/main.tsx": 'import { App } from "@/components/App"\nimport "./index.css"\n',
    "frontend/src/components/App.tsx": ('import { ItemsService } from "@/client"\nimport * as icons from "lucide-react"\n'
                                        'export const App = () => ItemsService.readItems()\n'),
    "frontend/src/components/App.test.tsx": "import { App } from './App'\n",
    "frontend/src/client/index.ts": "export { ItemsService } from './sdk.gen'\n",
    "frontend/src/client/sdk.gen.ts": ("export class ItemsService {\n"
                                       "  static readItems() { return request({ method: 'GET', url: '/api/v1/items/' }) }\n"
                                       "  static deleteItem(id: string) {\n"
                                       "    return request({ method: 'DELETE', url: `/api/v1/items/${id}?force=1` })\n  }\n}\n"),
    "scripts/release.py": "import subprocess\n",
    "compose.yml": ("services:\n  db:\n    image: postgres:18\n    environment:\n"
                    "      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}\n  backend:\n    image: backend:latest\n"
                    "    environment:\n      DATABASE_URL: postgresql://db/app\n      SMTP_HOST: ${SMTP_HOST}\n"),
    ".env": "POSTGRES_PASSWORD=changethis\nSECRET_KEY=changethis\n",
}


def blueprint(folder, files, layout):
    files(folder, layout)
    cfg = config.load(root=folder)
    return extract(DirSource(folder, config.segment_rx(cfg["code"]["skip"])), cfg), cfg


def test_a_fullstack_repository_with_no_config_gets_a_first_map(tmp_path, files):
    bp, cfg = blueprint(tmp_path, files, FULLSTACK)
    kinds = {cid: graph.part_kind(cid, c, bp, cfg) for cid, c in bp["components"].items()}
    assert kinds == {"backend/app": "logic", "backend/app/api": "interface", "backend/app/core": "logic",
                     "frontend/src": "ui", "frontend/src/client": "ui", "frontend/src/components": "ui"}
    assert bp["routes"] == {"DELETE /api/v1/items/{id}": "backend/app/api", "GET /api/v1/items/": "backend/app/api"}
    assert bp["api"] == {"frontend/src/client>backend/app/api": 2}
    assert bp["uses"]["frontend/src/client>backend/app/api"] == ["/api/v1/items/", "/api/v1/items/{id}"]
    assert bp["flows"] == {"backend/app/api>backend/app": 1, "backend/app/api>backend/app/core": 1,
                           "backend/app>backend/app/api": 1, "backend/app>backend/app/core": 1,
                           "frontend/src/components>frontend/src/client": 1, "frontend/src>frontend/src/components": 1}
    assert bp["uses"]["backend/app/api>backend/app"] == ["items"]           # crud.items(...)
    assert bp["uses"]["backend/app>backend/app/api"] == ["api_router"]
    assert bp["uses"]["frontend/src/components>frontend/src/client"] == ["ItemsService"]
    assert set(bp["uses"]) == set(bp["flows"]) | set(bp["api"])
    assert bp["stack"]["frontend/src/components"] == {"lucide-react": 1}    # @/client is the repository's own
    assert bp["stack"]["backend/app/api"] == {"fastapi": 2}
    assert bp["env"]["backend/app/core"] == ["API_V1_STR", "DATABASE_URL", "SMTP_HOST"]
    assert bp["configured"] == ["DATABASE_URL", "POSTGRES_PASSWORD", "SECRET_KEY", "SMTP_HOST"]
    assert set(bp["containers"]) == {"db", "backend"}                       # compose.yml, no docker-compose.yml
    assert bp["tables"] == {"public.audit": [], "public.item": ["note"]}    # Alembic in order, and the SQLModel table
    assert bp["migrations"] == ["z_first.py", "a_second.py"] and bp["store"] == "postgres"
    assert set(bp["externals"]) == {"postgres", "sentry"}                   # SMTP_HOST alone names no mail server
    assert bp["externals"]["postgres"]["used_by"] == ["backend/app", "backend/app/core"]   # models first
    assert bp["externals"]["postgres"]["found"] == ["compose runs postgres:18"]
    assert bp["externals"]["sentry"]["used_by"] == ["backend/app"]
    assert bp["tests"] == {"backend": 1, "frontend": 1}


def test_declared_outside_systems_keep_their_place(tmp_path, files):
    layout = {"app/pay.py": "import stripe\nimport os\n\nKEY = os.getenv('STRIPE_KEY')\n",
              "app/cache.py": "import redis\n", "compose.yml": "services:\n  cache:\n    image: redis:7\n"}
    toml = '[[externals]]\nid = "payments"\nlabel = "Stripe"\nenv = ["STRIPE_KEY"]\n'
    bp, _ = blueprint(tmp_path, files, {**layout, "system-diff.toml": toml})
    assert list(bp["externals"]) == [] and list(bp["dormant"]) == ["payments"]   # declared: nothing found besides
    files(tmp_path, {"system-diff.toml": toml + "[detect]\nexternals = true\n"})
    bp, _ = blueprint(tmp_path, files, {})
    assert list(bp["externals"]) == ["redis"] and bp["externals"]["redis"]["shape"] == "db"  # Stripe is declared
    files(tmp_path, {"system-diff.toml": "[detect]\nexternals = false\n"})
    bp, _ = blueprint(tmp_path, files, {})
    assert bp["externals"] == {}


def test_outside_systems_are_found_by_their_clients(tmp_path, files):
    bp, _ = blueprint(tmp_path, files, {
        "svc/store.py": "import boto3\n\ns3 = boto3.client('s3')\nqueue = boto3.client(\"sqs\")\n",
        "svc/ai.py": "from anthropic import Anthropic\n",
        "svc/mail.py": "import smtplib\nimport os\nHOST = os.environ['SMTP_HOST']\n",
        "web/src/pay.ts": "import { loadStripe } from '@stripe/stripe-js'\nimport * as Sentry from '@sentry/react'\n",
    })
    assert {x: e["label"] for x, e in bp["externals"].items()} == {
        "anthropic": "Anthropic API", "aws-s3": "Amazon S3", "aws-sqs": "Amazon SQS", "mail": "Mail server",
        "sentry": "Sentry", "stripe": "Stripe"}
    assert bp["externals"]["aws-s3"]["used_by"] == ["svc/store"] and bp["externals"]["aws-s3"]["shape"] == "db"


def test_routes_of_flask_express_and_nextjs(tmp_path, files):
    bp, _ = blueprint(tmp_path, files, {
        "shop/views.py": ('from flask import Blueprint\n\nbp = Blueprint("shop", __name__, url_prefix="/old")\n\n\n'
                          '@bp.route("/carts/<int:cart_id>", methods=["GET", "POST"])\ndef cart(cart_id):\n    pass\n'),
        "shop/app.py": ('from flask import Flask\n\nfrom shop.views import bp\n\napp = Flask(__name__)\n'
                        'app.register_blueprint(bp, url_prefix="/shop")\n\n\n@app.get("/health")\ndef health():\n    pass\n'),
        "server/index.js": "const express = require('express')\nconst users = require('./users')\nconst app = express()\n"
                           "app.use('/api', users)\n",
        "server/users.js": "const express = require('express')\nconst router = express.Router()\n"
                           "router.get('/users/:id', (req, res) => res.send())\nmodule.exports = router\n",
        "web/app/api/orders/[id]/route.ts": "export async function GET() {}\nexport const DELETE = async () => {}\n",
        "web/app/(shop)/page.tsx": "export default function Page() { return null }\n",
    })
    assert bp["routes"] == {
        "DELETE /api/orders/{id}": "web/app/api", "GET /api/orders/{id}": "web/app/api", "GET /api/users/{id}": "server/users",
        "GET /health": "shop/app", "GET /shop/carts/{cart_id}": "shop/views", "POST /shop/carts/{cart_id}": "shop/views"}


def test_what_an_import_carries():
    files = {"app/core/rules.py", "app/core/__init__.py", "app/db/repo.py", "app/db/__init__.py", "app/models.py",
             "web/src/api/client.ts", "web/src/api/index.ts", "web/src/App.tsx"}
    text = ("from app.core import rules\nfrom app.models import (\n    Order,  # the order\n    Line,\n)\n"
            "import app.db.repo as repo\n\nrules.approve(1)\nrules.approve(2)\nrules.reject(3)\nrepo.save()\n")
    got = py_imports("app/api/x.py", text, files)
    assert got == [("app/core/rules.py", Counter({"approve": 2, "reject": 1})),
                   ("app/models.py", Counter({"Order": 1, "Line": 1})), ("app/db/repo.py", Counter({"save": 1}))]
    ts = ("import App, { a, b as c, type T } from './api/client'\nimport * as api from './api'\n"
          "const { x } = require('./api/client')\nexport * from './api/client'\n")
    assert ts_imports("web/src/main.tsx", ts, files) == [
        ("web/src/api/client.ts", Counter({"App": 1, "a": 1, "b": 1, "T": 1})), ("web/src/api/index.ts", Counter({"api": 1})),
        ("web/src/api/client.ts", Counter({"x": 1})), ("web/src/api/client.ts", Counter({"client": 1}))]


def test_which_files_are_parts_and_how_they_are_cut():
    code = Code(config.DEFAULTS)
    assert [code.part_of(p) for p in ("backend/app/main.py", "backend/app/api/routes/items.py", "frontend/src/main.tsx",
                                      "frontend/src/components/ui/button.tsx", "pkg/a/sub/x.py", "src/components/a/b.tsx",
                                      "main.py")] == [
        "backend/app", "backend/app/api", "frontend/src", "frontend/src/components", "pkg/a", "src/components", "main"]
    assert not any(code.is_code(p) for p in ("frontend/vite.config.ts", "scripts/release.py", "app/alembic/env.py",
                                             "web/src/vite-env.d.ts", "hooks/post_gen_project.py"))
    assert all(code.is_test(p) for p in ("backend/tests/test_x.py", "web/src/App.test.tsx", "web/e2e/login.spec.ts",
                                         "app/conftest.py", "app/orders_test.py"))
    explicit = Code(config.merge(config.DEFAULTS, {"code": {"group": {"backend": 2}}, "components": [
        {"id": "build", "paths": ["web/vite.config.ts"]}]}))
    assert explicit.part_of("backend/app/api/x.py") == "backend/app"        # a group set by hand is kept
    assert explicit.is_code("web/vite.config.ts")                           # a declared part names it


def test_tables_from_orm_models_and_prisma_and_a_database_for_them(tmp_path, files):
    bp, _ = blueprint(tmp_path, files, {
        "app/models.py": ("from sqlalchemy.orm import DeclarativeBase\nfrom sqlalchemy import Table, MetaData\n\n"
                          "metadata = MetaData()\naudit = Table('audit_log', metadata)\n\n\nclass Order(Base):\n"
                          "    __tablename__ = 'orders'\n"),
        "app/legacy.py": "from app import db\n\n\nclass OrderLine(db.Model):\n    pass\n",
        "shop/models.py": ("from django.db import models\n\n\nclass Cart(models.Model):\n    pass\n\n\n"
                           "class Base(models.Model):\n    class Meta:\n        abstract = True\n"),
        "prisma/schema.prisma": 'model User {\n  id Int @id\n  @@map("users")\n}\nmodel Post {\n  id Int @id\n}\n',
        "docs/notes.py": '"""Table("name", metadata) in prose is no table."""\n',
    })
    assert bp["tables"] == {"public.Post": [], "public.audit_log": [], "public.order_line": [], "public.orders": [],
                            "public.shop_cart": [], "public.users": []}
    assert bp["store"] == "database" and bp["externals"]["database"]["shape"] == "db"
    assert bp["externals"]["database"]["used_by"] == ["app/models", "shop/models", "app/legacy"]


def test_paths_called_and_served_are_written_one_way():
    assert api_path("/api/users/${user.id}/posts?limit=5") == "/api/users/{id}/posts"
    assert api_path("/carts/<int:cart_id>") == "/carts/{cart_id}" and api_path("/users/:id") == "/users/{id}"
    assert path_matches("/api/v1/items/{id}", "/api/v1/items/{item_id}") and path_matches("/a/b/", "/a/b")
    assert not path_matches("/api/v1/items", "/api/v1/items/{id}")
    texts = {"r2.py": 'revision = "r2"\ndown_revision = ("r1", "r0")\n', "r1.py": 'revision = "r1"\ndown_revision = None\n',
             "r0.py": 'revision: str = "r0"\ndown_revision: str | None = None\n'}
    assert alembic_order(texts) == ["r0.py", "r1.py", "r2.py"]


def test_a_blueprint_cached_by_another_extractor_is_read_again(monkeypatch):
    calls = []
    monkeypatch.setattr(compare, "extract", lambda source, cfg: calls.append(1) or {"ref": "x"})
    monkeypatch.setattr(compare, "_BLUEPRINTS", {})
    compare._blueprint("abc", {"a": 1}, object())
    compare._blueprint("abc", {"a": 1}, object())
    assert len(calls) == 1
    monkeypatch.setattr(compare, "EXTRACT_VERSION", "next")
    compare._blueprint("abc", {"a": 1}, object())
    assert len(calls) == 2
