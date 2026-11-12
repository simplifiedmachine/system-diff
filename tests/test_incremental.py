"""Reading a repository file by file: each file's facts kept by its git blob id, so an edit reads only that file again
and the blueprint is exactly the one a whole read gives; and a first read of a large repository spread over the
pane's looks, each drawing a partial diagram from what is read so far."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from system_diff import compare, config, extract, panel
from system_diff.sources import GitSource

from conftest import git_, write

PROJECT = {
    "backend/app/__init__.py": "",
    "backend/app/main.py": ("from fastapi import FastAPI\nfrom app.api import items\nfrom app.core.config import settings\n\n"
                            "app = FastAPI()\napp.include_router(items.router, prefix=settings.API_PREFIX)\n"),
    "backend/app/api/__init__.py": "",
    "backend/app/api/items.py": ("import os\nfrom fastapi import APIRouter\nfrom app.core import store\n\n"
                                 "router = APIRouter(prefix=\"/items\")\nKEY = os.getenv(\"STRIPE_KEY\")\n\n\n"
                                 "@router.get(\"/{item_id}\")\ndef one(item_id: int):\n    return store.load(item_id)\n"),
    "backend/app/core/__init__.py": "",
    "backend/app/core/config.py": ("from pydantic_settings import BaseSettings\n\n\nclass Settings(BaseSettings):\n"
                                   "    API_PREFIX: str = \"/api/v1\"\n    DATABASE_URL: str = \"\"\n\n\n"
                                   "settings = Settings()\n"),
    "backend/app/core/store.py": ("import sqlalchemy\nfrom sqlmodel import SQLModel, Field\n\n\n"
                                  "class Item(SQLModel, table=True):\n    id: int = Field(primary_key=True)\n\n\n"
                                  "def load(item_id):\n    return sqlalchemy.text(str(item_id))\n"),
    "backend/app/alembic/versions/0001_items.py": ("revision = \"0001\"\ndown_revision = None\n\n\ndef upgrade():\n"
                                                   "    op.create_table(\"item\")\n    op.add_column(\"item\", "
                                                   "sa.Column(\"name\", sa.String()))\n\n\ndef downgrade():\n"
                                                   "    op.drop_table(\"item\")\n"),
    "frontend/tsconfig.json": '{ "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["./src/*"] } } }\n',
    "frontend/package.json": '{ "dependencies": { "react": "18.3.1" } }\n',
    "frontend/src/main.tsx": ("import React from \"react\";\nimport { ItemView } from \"@/components/ItemView\";\n\n"
                              "export const Root = () => <ItemView />;\n"),
    "frontend/src/components/ItemView.tsx": ("import { getItem } from \"../client/api\";\n\n"
                                             "export const ItemView = () => { getItem(1); return null; };\n"),
    "frontend/src/client/api.ts": ("const BASE = import.meta.env.VITE_API_URL;\n\n"
                                   "export const getItem = (id: number) => fetch(`/api/v1/items/${id}`);\n"),
    "compose.yml": "services:\n  db:\n    image: postgres:16\n    environment:\n      DATABASE_URL: postgres://db\n",
}


def project(tmp_path: Path) -> Path:
    r = tmp_path / "shop"
    r.mkdir()
    git_(r, "init", "-q", "-b", "main")
    write(r, PROJECT)
    git_(r, "add", "-A")
    git_(r, "commit", "-q", "-m", "first")
    return r


def commit(r: Path, files: dict, message: str = "edit") -> str:
    write(r, files)
    git_(r, "add", "-A")
    git_(r, "commit", "-q", "-m", message)
    return git_(r, "rev-parse", "HEAD").strip()


@pytest.fixture
def parsed(monkeypatch):
    """The code files read (their facts worked out from their text), in order."""
    seen = []
    real = extract.code_facts

    def counting(path, text):
        seen.append(path)
        return real(path, text)
    monkeypatch.setattr(extract, "code_facts", counting)
    monkeypatch.setattr(extract, "_STORES", {})   # a fresh store object per test, as in a new process
    return seen


@pytest.fixture(autouse=True)
def no_time_box():
    compare.time_box(None)
    yield
    compare.time_box(None)


def whole(r: Path, ref: str = "HEAD") -> dict:
    """The blueprint every file read afresh, no facts kept or used."""
    src = GitSource(ref, r)
    return extract.extract(src, config.load(src, r, None), cache=False)


def test_an_edit_reads_one_file_again_and_gives_exactly_the_whole_read(tmp_path, parsed):
    r = project(tmp_path)
    src = GitSource("HEAD", r)
    cfg = config.load(src, r, None)
    first = extract.extract(src, cfg)
    code = [f for f in src.files if f.endswith((".py", ".ts", ".tsx")) and "alembic" not in f]
    assert sorted(parsed) == sorted(code)
    assert first == whole(r)
    # what the project says, so the equality below covers every kind of fact
    assert first["flows"] and first["routes"] == {"GET /api/v1/items/{item_id}": "backend/app/api"}
    assert first["api"] == {"frontend/src/client>backend/app/api": 1}   # the call the route serves
    assert first["flows"]["frontend/src>frontend/src/components"] == 1   # through tsconfig's @/
    assert first["env"]["backend/app/api"] == ["STRIPE_KEY"] and first["env"]["frontend/src/client"] == ["VITE_API_URL"]
    assert first["tables"] == {"public.item": ["name"]} and first["migrations"] == ["0001_items.py"]
    assert first["containers"]["db"]["image"] == "postgres:16" and "DATABASE_URL" in first["configured"]
    assert first["imported"]["backend/app/core"] == {"pydantic_settings": 1, "sqlalchemy": 1, "sqlmodel": 1}
    assert first["components"]["frontend/src"]["kind"] == "ui"

    parsed.clear()
    edited = PROJECT["backend/app/api/items.py"].replace("@router.get", "@router.delete(\"/{item_id}\")\n"
                                                         "def drop(item_id: int):\n    return None\n\n\n@router.get")
    commit(r, {"backend/app/api/items.py": edited + "TOKEN = os.getenv(\"OTHER_TOKEN\")\n"})
    again = extract.extract(GitSource("HEAD", r), cfg)
    assert parsed == ["backend/app/api/items.py"]
    assert again == whole(r)
    assert "DELETE /api/v1/items/{item_id}" in again["routes"] and "OTHER_TOKEN" in again["env"]["backend/app/api"]

    parsed.clear()   # an edit that changes what another file's import reaches: worked out again, not kept
    commit(r, {"frontend/src/client/api.ts": PROJECT["frontend/src/client/api.ts"].replace("getItem", "fetchItem")})
    again = extract.extract(GitSource("HEAD", r), cfg)
    assert parsed == ["frontend/src/client/api.ts"] and again == whole(r)

    parsed.clear()   # a file removed, a file added
    (r / "backend/app/core/store.py").unlink()
    commit(r, {"backend/app/core/cache.py": "import redis\nfrom app.core.config import settings\n"})
    again = extract.extract(GitSource("HEAD", r), cfg)
    assert parsed == ["backend/app/core/cache.py"] and again == whole(r)


def test_the_checkpoint_and_the_working_tree_share_what_each_file_says(tmp_path, parsed, monkeypatch):
    r = project(tmp_path)
    monkeypatch.setattr(compare, "_BLUEPRINTS", {})
    compare.checkpoint(r)
    c = compare.compare(r, "checkpoint", ".")
    assert len(parsed) == len(set(parsed))   # the same content on both sides: read once
    parsed.clear()
    write(r, {"backend/app/core/store.py": PROJECT["backend/app/core/store.py"] + "\nimport boto3\n"})
    c = compare.compare(r, "checkpoint", ".")
    assert parsed == ["backend/app/core/store.py"]
    assert c.after == {**whole(r, compare.snapshot(r)), "ref": "working tree"}


def test_a_new_facts_version_reads_every_file_again(tmp_path, parsed, monkeypatch):
    r = project(tmp_path)
    src = GitSource("HEAD", r)
    cfg = config.load(src, r, None)
    extract.extract(src, cfg)
    n = len(parsed)
    extract.extract(GitSource("HEAD", r), cfg)
    assert len(parsed) == n
    monkeypatch.setattr(extract, "FACTS_VERSION", "next")
    monkeypatch.setattr(extract, "_STORES", {})
    extract.extract(GitSource("HEAD", r), cfg)
    assert len(parsed) == 2 * n
    assert [p.name for p in (r / ".git/system-diff/files").iterdir()] == ["vnext"]


def slow_reads(monkeypatch, seconds: float) -> None:
    real = extract.code_facts

    def slow(path, text):
        time.sleep(seconds)
        return real(path, text)
    monkeypatch.setattr(extract, "code_facts", slow)
    monkeypatch.setattr(extract, "_STORES", {})


def kept_blueprints(r: Path) -> list:
    folder = r / ".git/system-diff/blueprints"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_a_cold_start_is_read_a_few_seconds_at_a_time_and_draws_what_it_has(tmp_path, monkeypatch):
    r = project(tmp_path)
    compare.checkpoint(r)
    write(r, {"backend/app/api/items.py": PROJECT["backend/app/api/items.py"] + "\nX = os.getenv(\"NEW_SETTING\")\n"})
    monkeypatch.setattr(compare, "_BLUEPRINTS", {})
    slow_reads(monkeypatch, 0.05)
    full = whole(r, compare.snapshot(r))
    looks = []
    for _ in range(40):
        compare.time_box(0.12)   # what panel.main does, with extract.COLD_BUDGET
        c = compare.compare(r, "checkpoint", ".")
        looks.append(compare.partial())
        if looks[-1] is None:
            break
        p = looks[-1]
        assert c.after["partial"] == p and 0 < p["done"] < p["total"]
        # every part is there from the first look, its files counted, though not every file is read yet
        assert {k: v["files"] for k, v in c.after["components"].items()} == \
            {k: v["files"] for k, v in full["components"].items()}
        # like with like: nothing the head has not read yet shows as removed
        assert not [ch for ch in c.diff["changes"] if ch["op"] == "remove"], c.diff["plan"]
        assert kept_blueprints(r) == []   # a partial blueprint is never kept as its commit's
    assert len(looks) > 2 and looks[-1] is None
    assert [p["done"] for p in looks[:-1]] == sorted(p["done"] for p in looks[:-1])   # it goes on from where it was
    assert c.after == {**full, "ref": "working tree"}
    assert c.before == {**whole(r, compare.checkpoint_sha(r)), "ref": "checkpoint"}
    assert "NEW_SETTING" in c.diff["plan"]
    assert len(kept_blueprints(r)) == 2   # whole now: kept


def test_a_partial_head_against_a_whole_base_is_compared_on_the_same_files(tmp_path, monkeypatch):
    r = project(tmp_path)
    compare.checkpoint(r)
    monkeypatch.setattr(compare, "_BLUEPRINTS", {})
    compare.compare(r, "checkpoint", ".")   # the checkpoint's blueprint kept whole...
    for shard in (r / ".git/system-diff/files").rglob("*.json"):
        shard.unlink()   # ...but no file's facts (as when they were read by an older system-diff)
    write(r, {"backend/app/core/store.py": PROJECT["backend/app/core/store.py"] + "\nimport stripe\n"})
    slow_reads(monkeypatch, 0.05)
    compare.time_box(0.12)
    c = compare.compare(r, "checkpoint", ".")
    assert compare.partial() and c.before.get("partial")
    assert not [ch for ch in c.diff["changes"] if ch["op"] == "remove"], c.diff["plan"]


def test_the_page_says_the_code_is_still_being_read():
    page = {"rows": [[["A", "bold"]], [["box", ""]]], "wide": 10,
            "blocks": [{"id": "b:x", "x": 0, "y": 1, "w": 3, "h": 1, "slot": [2, 3]}]}
    panel.still_reading(page, None)
    assert len(page["rows"]) == 2
    panel.still_reading(page, {"done": 400, "total": 2300})
    assert page["rows"][0] == [["◐ Still reading the code: 400 of 2,300 files so far — the diagram fills in as it goes",
                                "attention"]]
    shift = len(page["rows"]) - 2
    assert page["rows"][shift] == [["A", "bold"]]
    assert page["blocks"][0]["y"] == 1 + shift and page["blocks"][0]["slot"] == [2 + shift, 3]
    assert page["reading"] == {"done": 400, "total": 2300} and page["wide"] >= len(page["rows"][0][0][0])


def test_the_pane_page_while_reading(tmp_path, monkeypatch, capsys):
    r = project(tmp_path)
    slow_reads(monkeypatch, 0.05)
    monkeypatch.setattr(extract, "COLD_BUDGET", 0.1)
    import json
    panel.main(r, 100, protocol=2)
    page = json.loads(capsys.readouterr().out)
    assert page["rows"][0][0][0].startswith("◐ Still reading the code: ") and page["rows"][0][0][1] == "attention"
    assert compare._deadline is None   # the time box is the panel's only
