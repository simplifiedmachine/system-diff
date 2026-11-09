"""The made-up project every test reads: an API, its logic and data access, a web app, a migration and a compose file,
committed twice so the second commit has something to show."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

CONFIG = """\
tests = ["tests/**"]
deploy = ["Dockerfile"]

[code]
roots = ["app/", "web/src/"]
aliases = { "@/" = "web/src/" }

[[components]]
id = "api"
label = "API"
kind = "interface"
paths = ["app/api/**"]

[[components]]
id = "core"
label = "Core"
kind = "logic"
paths = ["app/core/**"]

[[components]]
id = "store"
label = "Store"
kind = "data"
paths = ["app/db/**"]

[[components]]
id = "web"
label = "Web app"
container = "web"
paths = ["web/src/**"]

[[externals]]
id = "postgres"
label = "PostgreSQL"
shape = "db"
env = ["DATABASE_URL"]

[[externals]]
id = "mail"
label = "Mail server"
env = ["SMTP_HOST"]

[[api]]
prefix = "/api/"
component = "api"

[migrations]
glob = "migrations/*.sql"
store = "postgres"
"""

FIRST = {
    "system-diff.toml": CONFIG,
    "app/api/routes.py": ('from fastapi import APIRouter\nfrom app.core import rules\n\n'
                          'router = APIRouter(prefix="/api/orders")\n\n\n@router.get("")\ndef list_orders():\n'
                          '    return rules.everything()\n\n\n@router.post("/{id}/approve")\ndef approve(id):\n'
                          '    return rules.approve(id)\n'),
    "app/core/rules.py": ('import os\n\nfrom app.db import repo\n\nMAIL = os.getenv("SMTP_HOST")\n\n\n'
                          'def everything():\n    return repo.orders()\n'),
    "app/db/repo.py": 'import os\n\nURL = os.environ["DATABASE_URL"]\n\n\ndef orders():\n    return []\n',
    "web/src/App.tsx": 'import { orders } from "@/lib/api";\n\nexport const App = () => orders();\n',
    "web/src/lib/api.ts": 'export const orders = () => fetch("/api/orders");\n',
    "migrations/001_orders.sql": "-- CREATE TABLE in a comment is not a table\nCREATE TABLE orders (id int);\n",
    "docker-compose.yml": ("services:\n  api:\n    image: shop/api:1\n    environment:\n      DATABASE_URL: postgres://db\n"
                           "  web:\n    image: shop/web:1\n    ports:\n      - \"80:80\"\n"),
    "tests/test_rules.py": "def test_nothing():\n    assert True\n",
}

SECOND = {
    "app/api/routes.py": FIRST["app/api/routes.py"] + '\n\n@router.delete("/{id}")\ndef remove(id):\n    return None\n',
    "app/core/rules.py": FIRST["app/core/rules.py"].replace('MAIL = os.getenv("SMTP_HOST")',
                                                            'MAIL = os.getenv("SMTP_HOST")\nSENDER = os.getenv("MAIL_FROM")'),
    "app/jobs/worker.py": "from app.core import rules\nfrom app.core import rules as r2\nfrom app.core import rules as r3\n",
    "migrations/002_note.sql": "ALTER TABLE orders ADD COLUMN note text;\n",
}


def git_(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def write(repo: Path, files: dict) -> None:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "shop"
    r.mkdir()
    git_(r, "init", "-q", "-b", "main")
    write(r, FIRST)
    git_(r, "add", "-A")
    git_(r, "commit", "-q", "-m", "first")
    write(r, SECOND)
    git_(r, "add", "-A")
    git_(r, "commit", "-q", "-m", "second: delete, a worker, a note column")
    return r


@pytest.fixture
def files():
    """Write files into a folder: files(folder, {path: text})."""
    return write


@pytest.fixture(autouse=True)
def plain_terminal(monkeypatch):
    """No test sees the terminal it runs in (VS Code, tmux…), so none opens a real pane or depends on where it runs."""
    for var in ("TMUX", "ZELLIJ", "WEZTERM_PANE", "KITTY_WINDOW_ID", "WT_SESSION", "TERM_PROGRAM",
                "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ATTENDED"):
        monkeypatch.delenv(var, raising=False)
