"""Projects found by what folders hold, never by their names; an extension names its own; a repository inside a
project's folder is part of that project (not a row of its own); ctx.scope reaches extensions."""
from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from system_diff import extensions, pane, projects, repos


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(projects, "_CACHE", {})
    monkeypatch.setattr(repos, "_CACHE", {})
    monkeypatch.setattr(extensions, "_APPLIES", {})
    monkeypatch.setattr(extensions, "_LOADED", [])


@pytest.fixture
def studies(tmp_path, files):
    """A research repository shaped like the person's: studies by number, each with its paper as a submodule."""
    r = tmp_path / "papers"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    files(r, {"1_tradeoff/run.py": "x = 1\n", "2_behavioral/run.py": "y = 2\n", "3_serving/notes.md": "z\n",
              "README.md": "r\n"})
    _git(r, "add", "-A")
    subs = ["1_tradeoff/paper", "2_behavioral/paper", "3_serving/paper", "shared/paper", "other/paper"]
    (r / ".gitmodules").write_text("".join(f'[submodule "{s}"]\n\tpath = {s}\n\turl = ../x\n' for s in subs),
                                   encoding="utf-8")
    for s in subs:
        (r / s).mkdir(parents=True)
        (r / s / ".git").write_text("gitdir: ../../.git/modules/x\n", encoding="utf-8")
    return r


def _ext(name="research", got=None, boom=False, applies=True):
    def projects_(ctx):
        if boom:
            raise RuntimeError("no studies today")
        return got if got is not None else []
    return types.SimpleNamespace(NAME=name, applies=lambda root: applies, projects=projects_)


def test_projects_are_found_by_content_never_by_name(studies, tmp_path, files):
    assert projects.detect(studies) == []                    # numbered folders without a manifest: not projects
    mono = tmp_path / "mono"
    mono.mkdir()
    _git(mono, "init", "-q")
    files(mono, {"1_api/pyproject.toml": "", "web/package.json": "{}", "notes/2_x.md": ""})
    _git(mono, "add", "-A")
    got = projects.detect(mono)
    assert [(p["path"], p["name"], p["why"]) for p in got] == [("1_api", "1_api", "its own pyproject.toml"),
                                                              ("web", "web", "its own package.json")]


def test_an_extensions_projects_are_listed_and_its_failure_hides_only_its_own(monkeypatch, studies):
    named = [{"path": "1_tradeoff", "title": "Trade-offs", "why": "a paper and its code"},
             {"path": "./2_behavioral/", "title": "Behaviour"}, {"path": "3_serving"},
             {"path": "../outside"}, {"path": "missing"}, "not a dict"]
    bad = _ext("broken", boom=True)
    elsewhere = _ext("elsewhere", got=[{"path": "3_serving", "title": "never"}], applies=False)
    module_named_projects = types.SimpleNamespace(NAME="mod", projects=types.ModuleType("projects"))
    monkeypatch.setattr(extensions, "_LOADED", [bad, _ext(got=named), elsewhere, module_named_projects])
    got = projects.detect(studies)
    assert [(p["path"], p["name"]) for p in got] == [("1_tradeoff", "Trade-offs"), ("2_behavioral", "Behaviour"),
                                                     ("3_serving", "3_serving")]
    assert got[0]["why"] == "a paper and its code" and got[1]["why"] == "named by research"


def test_an_extension_wins_over_a_manifest_and_the_outermost_folder_wins(monkeypatch, tmp_path, files):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    files(r, {"a/pyproject.toml": "", "b/inner/package.json": "{}", "c/pyproject.toml": ""})
    _git(r, "add", "-A")
    monkeypatch.setattr(extensions, "_LOADED", [_ext(got=[{"path": "a", "title": "Study A", "why": "named"},
                                                          {"path": "b", "title": "Study B"},
                                                          {"path": "c/deeper", "title": "never"}])])
    (r / "c" / "deeper").mkdir()
    got = projects.detect(r)
    assert [(p["path"], p["name"]) for p in got] == [("a", "Study A"), ("b", "Study B"), ("c", "c")]


def test_an_extension_asking_back_gets_no_loop(monkeypatch, studies):
    def asks_back(ctx):
        inner = projects.detect(ctx.root)                   # manifests only, here: none
        return [{"path": p, "title": p + str(len(inner))} for p in ("1_tradeoff", "2_behavioral")]
    monkeypatch.setattr(extensions, "_LOADED", [types.SimpleNamespace(NAME="r", projects=asks_back)])
    assert [p["name"] for p in projects.detect(studies)] == ["1_tradeoff0", "2_behavioral0"]


def test_a_submodule_inside_a_project_is_part_of_it_and_names_tell_apart(monkeypatch, studies):
    monkeypatch.setattr(extensions, "_LOADED", [_ext(got=[{"path": p} for p in
                                                          ("1_tradeoff", "2_behavioral", "3_serving")])])
    fam = {r["rel"]: r for r in repos.family(studies)}
    assert fam[""]["within"] == "" and fam[""]["kind"] == "outer"
    for s in ("1_tradeoff", "2_behavioral", "3_serving"):
        assert fam[s + "/paper"]["within"] == s
    assert fam["shared/paper"]["within"] == "" and fam["other/paper"]["within"] == ""
    apart = [r["name"] for r in fam.values() if r["rel"] and not r["within"]]
    assert sorted(apart) == ["other/paper", "shared/paper"]  # two "paper" rows would not tell them apart
    placed = {k["path"]: k for k in repos.placed(studies)}
    assert placed["1_tradeoff/paper"]["within"] == "1_tradeoff" and placed["other/paper"]["name"] == "other/paper"
    projects.set_one(studies, True)                          # shown as one project: every repository listed apart
    monkeypatch.setattr(repos, "_CACHE", {})
    assert all(not r["within"] for r in repos.family(studies))


def test_a_lone_repository_keeps_its_short_name(tmp_path):
    r = tmp_path / "solo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "vendor_x").mkdir()
    _git(r / "vendor_x", "init", "-q")
    fam = repos.family(r)
    assert [(m["rel"], m["name"], m["within"]) for m in fam] == [("", "solo", ""), ("vendor_x", "vendor_x", "")]


def test_ctx_scope_reaches_extensions(monkeypatch, repo):
    assert extensions.context(repo, "now").scope == ""
    assert extensions.context(repo, "now", "", 100, 0, "api").scope == "api"
    assert extensions.Context(Path("."), "now", "", 100, 0, None, None).scope == ""   # positional, as before
    seen = []
    ext = types.SimpleNamespace(NAME="research", VIEWS=[("evidence", "Evidence", "e")],
                                web=lambda ctx: seen.append(("web", ctx.scope)) or {"title": "E", "sections": []},
                                pane=lambda ctx: seen.append(("pane", ctx.scope)) or {"rows": [], "blocks": []})
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    monkeypatch.setattr(pane, "SCOPE", "api")
    pane.web_view(repo, "evidence")
    pane._show(repo, 100, "evidence")
    assert seen == [("web", "api"), ("pane", "api")]


def test_an_extension_that_failed_is_asked_again(monkeypatch, repo):
    """The first ask came while the engine was still starting and the extension failed: its empty answer is not kept
    (it was, until the index changed: the person's studies never reached the list)."""
    from system_diff import projects
    calls = {"n": 0}

    def named(root, failed=None):
        calls["n"] += 1
        if calls["n"] == 1:
            failed.append("research")
            return []
        return [{"path": "app", "name": "App", "why": "x", "by": "research"},
                {"path": "web", "name": "Web", "why": "x", "by": "research"}]
    monkeypatch.setattr(projects, "_from_extensions", named)
    (repo / "app").mkdir(exist_ok=True); (repo / "web").mkdir(exist_ok=True)
    projects._CACHE.clear()
    assert projects.detect(repo) == []
    assert [p["path"] for p in projects.detect(repo)] == ["app", "web"]


def test_inside_the_studio_the_page_does_not_list_the_repositories_again():
    from system_diff import watch
    assert "if (IN_STUDIO) break;" in watch.PAGE


def test_the_page_draws_a_views_filters_and_an_extensions_progress():
    from system_diff import watch
    p, src = watch.PAGE, watch.__loader__.get_source("system_diff.watch")
    assert "const EXT_VIEWS = __EXT_VIEWS__;" in p and 'fetch("ext/progress?"' in p and "Array.isArray(d.filters)" in p
    assert '"/ext/progress"' in src and '"/projects/folders"' in src
