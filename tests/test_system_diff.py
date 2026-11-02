"""system-diff on the made-up project of conftest.py: the map, the diff, the pictures, the config, the README section
and the command line."""
from __future__ import annotations

import json
from pathlib import Path

from system_diff import config, graph, readme
from system_diff.cli import main
from system_diff.diff import diff, numstat
from system_diff.extract import Code, extract, glob_rx, py_targets, ts_targets
from system_diff.sources import DirSource, GitSource


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


def test_a_repository_with_no_config_is_drawn_by_its_folders(tmp_path, files):
    files(tmp_path, {"pkg/a/one.py": "from pkg.b import two\n", "pkg/b/two.py": "x = 1\n", "main.py": "import pkg.a.one\n"})
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
