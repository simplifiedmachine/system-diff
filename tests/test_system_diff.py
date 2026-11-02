"""system-diff on a small made-up project: an API, its logic and data access, a web app, a migration and a compose file,
committed twice so the second commit has something to show."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from system_diff import config, graph, readme
from system_diff.cli import main
from system_diff.diff import diff, numstat
from system_diff.extract import Code, extract, glob_rx, py_targets, ts_targets
from system_diff.sources import DirSource, GitSource

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


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def _write(repo: Path, files: dict) -> None:
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "shop"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _write(r, FIRST)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "first")
    _write(r, SECOND)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "second: delete, a worker, a note column")
    return r


def _bp(repo: Path, ref: str) -> dict:
    src = GitSource(ref, repo)
    return extract(src, config.load(src, repo))


def test_the_map_reads_parts_routes_connections_outside_systems_and_tables(repo):
    bp = _bp(repo, "HEAD~1")
    assert set(bp["components"]) == {"api", "core", "store", "web"}
    assert bp["routes"] == {"GET /api/orders": "api", "POST /api/orders/{id}/approve": "api"}
    assert bp["flows"] == {"api>core": 1, "core>store": 1}
    assert bp["api"] == {"web>api": 1}                                   # the web app's fetch("/api/orders")
    assert list(bp["externals"]) == ["postgres"]                         # set by the compose file
    assert list(bp["dormant"]) == ["mail"]                               # read, but no deploy file sets SMTP_HOST
    assert bp["tables"] == {"public.orders": []}                         # the comment's CREATE TABLE is not one
    assert set(bp["containers"]) == {"api", "web"} and bp["tests"] == {"tests/test_rules.py": 1}


def test_the_diff_names_every_structural_change(repo):
    a, b = _bp(repo, "HEAD~1"), _bp(repo, "HEAD")
    cfg = config.load(GitSource("HEAD", repo), repo)
    d = diff(a, b, numstat(repo, "HEAD~1", "HEAD", cfg), cfg["deploy"], Code(cfg))
    assert d["headline"] == ("Adds 1 part (app/jobs), 1 endpoint (DELETE /api/orders/{id}), 1 migration "
                             "(002_note.sql) and 1 connection; changes 2 parts (API, Core), 1 table (public.orders) "
                             "and 1 set of settings (Core).")
    assert "+ route DELETE /api/orders/{id}   [new-interface]" in d["plan"]
    assert "    + column note" in d["plan"] and "    MAIL_FROM" in d["plan"]
    assert d["impacts"]["test-gap"] == 1 and d["undeclared"] == ["app/jobs"]


def test_the_pictures_are_the_same_every_time_and_show_what_changed(repo):
    cfg = config.load(GitSource("HEAD", repo), repo)
    a, b = _bp(repo, "HEAD~1"), _bp(repo, "HEAD")
    svg = graph.system_svg(b, cfg)
    assert svg == graph.system_svg(_bp(repo, "HEAD"), cfg)
    assert svg.startswith("<svg") and "PostgreSQL" in svg and "Web app" in svg
    d = diff(a, b, numstat(repo, "HEAD~1", "HEAD", cfg), cfg["deploy"], Code(cfg))
    picture = graph.diff_svg(d, a, b, cfg)
    assert picture and "DELETE /api/orders/{id}" in picture and "+ note" in picture
    assert graph.diff_svg(diff(b, b), b, b, cfg) is None                # nothing structural, no picture


def test_a_repository_with_no_config_is_drawn_by_its_folders(tmp_path):
    _write(tmp_path, {"pkg/a/one.py": "from pkg.b import two\n", "pkg/b/two.py": "x = 1\n", "main.py": "import pkg.a.one\n"})
    cfg = config.load(root=tmp_path)
    bp = extract(DirSource(tmp_path, config.segment_rx(cfg["code"]["skip"])), cfg)
    assert set(bp["components"]) == {"pkg/a", "pkg/b", "main"}
    assert bp["flows"] == {"main>pkg/a": 1, "pkg/a>pkg/b": 1}
    assert all(c["layer"] == "undeclared" for c in bp["components"].values())


def test_config_comes_from_toml_json_or_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.system-diff]\ntests = ["t/**"]\n[tool.system-diff.code]\nroots = ["lib/"]\n',
                                             encoding="utf-8")
    cfg = config.load(root=tmp_path)
    assert cfg["code"]["roots"] == ["lib/"] and cfg["tests"] == ["t/**"]
    assert cfg["code"]["extensions"] == config.DEFAULTS["code"]["extensions"]   # the rest stays default
    (tmp_path / "x.json").write_text(json.dumps({"compose": "deploy.yml"}), encoding="utf-8")
    assert config.load(root=tmp_path, path=str(tmp_path / "x.json"))["compose"] == "deploy.yml"
    assert config.load(root=tmp_path / "nothing-here") == config.DEFAULTS


def test_imports_resolve_relative_aliased_and_src_layout():
    files = {"src/pkg/a.py", "src/pkg/b.py", "web/src/lib/api.ts", "web/src/App.tsx"}
    assert py_targets("src/pkg/a.py", "from . import b\n", files) == ["src/pkg/b.py"]
    assert py_targets("x.py", "import pkg.b\n", files, ("", "src/")) == ["src/pkg/b.py"]
    assert ts_targets("web/src/App.tsx", 'import { x } from "@/lib/api";', files, {"@/": "web/src/"}) == ["web/src/lib/api.ts"]
    assert glob_rx("app/**/*.py").match("app/a/b/c.py") and not glob_rx("app/*.py").match("app/a/b.py")


def test_the_readme_section_is_rewritten_between_its_markers(tmp_path):
    cfg = config.load(root=tmp_path)
    path = tmp_path / "README.md"
    path.write_text("# x\n\n<!-- system-map:begin -->\nold\n<!-- system-map:end -->\n\nmore\n", encoding="utf-8")
    assert readme.update_readme(path, {}, cfg) is True
    text = path.read_text(encoding="utf-8")
    assert "![System map, generated from the code](docs/diagrams/system.png)" in text and "old" not in text
    assert text.endswith("<!-- system-map:end -->\n\nmore\n")
    assert readme.update_readme(path, {}, cfg) is False                 # already current
    stale = readme.readme_section({}, cfg, ["docs/diagrams/system.png"])
    assert "Older than the code:** `docs/diagrams/system.png`" in stale


def test_the_command_line_writes_the_map_and_the_diff_and_checks_them(repo, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["--repo", str(repo), "map", "--ref", "HEAD", "--out", str(out)]) == 0
    assert (out / "system.svg").is_file() and json.loads((out / "system.json").read_text(encoding="utf-8"))["routes"]
    assert main(["--repo", str(repo), "map", "--ref", "HEAD", "--out", str(out), "--check"]) == 0
    assert main(["--repo", str(repo), "map", "--ref", "HEAD~1", "--out", str(out), "--check"]) == 1
    assert main(["--repo", str(repo), "diff", "--base", "HEAD~1", "--head", "HEAD", "--out", str(out)]) == 0
    assert (out / "diff.svg").is_file() and "DELETE /api/orders/{id}" in (out / "plan.txt").read_text(encoding="utf-8")
    assert "Adds 1 part (app/jobs)" in capsys.readouterr().out


def test_uncommitted_work_is_diffed_against_a_commit(repo, tmp_path):
    (repo / "app" / "api" / "health.py").write_text('from fastapi import APIRouter\nrouter = APIRouter()\n\n\n'
                                                    '@router.get("/health")\ndef health():\n    return "ok"\n', encoding="utf-8")
    out = tmp_path / "wt"
    assert main(["--repo", str(repo), "diff", "--base", "HEAD", "--head", ".", "--out", str(out)]) == 0
    assert "+ route GET /health" in (out / "plan.txt").read_text(encoding="utf-8")


def test_a_hand_made_spec_is_drawn_beside_itself(tmp_path):
    spec = {"picture": "stages", "label": "Release", "stages": [{"id": "build", "title": "Build"},
                                                                 {"id": "ship", "title": "Ship"}]}
    path = tmp_path / "release.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert main(["draw", str(path)]) == 0
    assert (tmp_path / "release.svg").read_text(encoding="utf-8").startswith("<svg")
