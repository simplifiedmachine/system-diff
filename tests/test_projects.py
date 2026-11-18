"""Projects inside one repository (folders with their own manifest, numbered folders), each its own diagram from the
same engine; the person's one/split switch; and an extension's home view (a research repository opens on research)."""
from __future__ import annotations

import json
import subprocess
import threading
import time
import types
import urllib.request

import pytest

from system_diff import extensions, pane, projects, watch


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def mono(tmp_path, files):
    r = tmp_path / "mono"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    files(r, {
        "pyproject.toml": "[project]\nname='mono'\n",                    # the root's own: not a project
        "api/pyproject.toml": "[project]\nname='api'\n",
        "api/app/main.py": "import fastapi\napp = fastapi.FastAPI()\n@app.get('/health')\ndef health():\n    return 1\n",
        "web/package.json": '{"name": "web", "dependencies": {"react": "18"}}',
        "web/src/index.ts": "export const a = 1;\n",
        "worker/go.mod": "module worker\n",
        "worker/main.go": "package main\nfunc main() {}\n",
        "scripts/pyproject.toml": "[project]\nname='s'\n",                # shared: never a project
        "docs/package.json": "{}",
        "libs/shared.py": "X = 1\n",
    })
    _git(r, "add", "-A")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "one"], cwd=r, check=True,
                   capture_output=True)
    return r


def test_projects_are_found_by_their_manifests_and_numbers(mono, tmp_path, files):
    got = projects.detect(mono)
    assert [p["path"] for p in got] == ["api", "web", "worker"] and got[0]["why"] == "its own pyproject.toml"
    research = tmp_path / "research"
    research.mkdir()
    _git(research, "init", "-q", "-b", "main")
    files(research, {"1_tradeoff/notes.md": "x", "2_behavioral/notes.md": "y", "3_serving/README.md": "z",
                     "results/a.csv": "1", "README.md": "r"})
    _git(research, "add", "-A")
    assert [p["path"] for p in projects.detect(research)] == ["1_tradeoff", "2_behavioral", "3_serving"]
    solo = tmp_path / "solo"
    solo.mkdir()
    _git(solo, "init", "-q")
    files(solo, {"api/pyproject.toml": "", "api/x.py": ""})
    _git(solo, "add", "-A")
    assert projects.detect(solo) == []                       # one project: no list


def test_the_config_names_projects_or_turns_them_off(mono, files):
    files(mono, {"system-diff.toml": 'projects = ["api", "worker"]\n'})
    assert [p["path"] for p in projects.detect(mono)] == ["api", "worker"]
    files(mono, {"system-diff.toml": "projects = false\n"})
    assert projects.detect(mono) == []


def test_one_project_or_split(mono):
    assert projects.listed(mono) and not projects.as_one(mono)
    projects.set_one(mono, True)
    assert projects.listed(mono) == [] and projects.detect(mono)    # still known: the switch offers to split again
    projects.set_one(mono, False)
    assert len(projects.listed(mono)) == 3


def test_a_projects_diagram_is_its_folders(mono):
    pane.SCOPE = "api"
    try:
        c = pane.compare(mono, "checkpoint", ".")
    finally:
        pane.SCOPE = ""
    whole = pane.compare(mono, "checkpoint", ".")
    parts = set(c.after["components"])
    assert parts and all(not p.startswith(("web", "worker")) for p in parts)
    assert any(p.startswith("api") for p in parts) and any(p.startswith("web") for p in whole.after["components"])
    assert all(o in parts for o in c.after["routes"].values())


def test_the_page_lists_the_projects_and_draws_one(mono):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": mono, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)}, daemon=True)
    t.start()
    deadline = time.time() + 15
    while "w" not in up and time.time() < deadline:
        time.sleep(0.05)
    tok = {"X-Token": up["w"].token}
    get = lambda q: json.loads(urllib.request.urlopen(urllib.request.Request(up["url"] + q, headers=tok), timeout=60).read())  # noqa: E731
    try:
        engine = getattr(up["w"], "engine", None)
        if engine is not None:
            assert engine.done.wait(60)
        web = get("web?view=now")
        folders = [r for r in web["repos"] if r["kind"] == "folder"]
        assert [r["scope"] for r in folders] == ["api", "web", "worker"] and web["projects"] == {"one": False, "found": 3}
        api = get("web?view=now&scope=api")
        assert api.get("scope") == "api"
        nodes = {n["id"] for n in api["diagram"]["graph"]["nodes"]}
        assert not any(n.startswith("web") for n in nodes)
        assert "scope" not in get("web?view=now&scope=nowhere")          # not a project: the whole repository
        req = urllib.request.Request(up["url"] + "projects/mode?one=1", data=b"", method="POST", headers=tok)
        assert json.loads(urllib.request.urlopen(req, timeout=30).read())["one"] is True
        assert [r for r in get("web?view=now")["repos"] if r["kind"] == "folder"] == []
    finally:
        up["server"].shutdown()
        t.join(5)
    page = watch.page_html()
    assert "Projects in this repository" in page and "Show as one project" in page and "function openScope" in page


def test_a_research_repository_opens_on_research(monkeypatch, repo):
    ext = types.SimpleNamespace(NAME="research", VIEWS=[("research", "Research", "s")])
    ext.home = lambda ctx: {"view": "research", "system": "collapsed"}
    ext.pane = lambda ctx: {"protocol": 2, "rows": [[["research", ""]]], "blocks": [], "width": 80, "wide": 80}
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    assert extensions.home(repo) == {"view": "research", "system": "collapsed", "set": "research", "title": "Research"}
    page = watch.page_html(repo)
    assert '"view": "research", "system": "collapsed"' in page and "System ▸" in page
    assert pane.show(repo, 80, "now", take=False)["home"]["view"] == "research"
    ext.home = lambda ctx: {"view": "not-a-view"}
    assert extensions.home(repo) is None


def test_the_files_come_from_gits_index_not_a_walk(mono, files):
    from system_diff import uitools
    files(mono, {"node_modules/big/package.json": "{}", "notes.txt": "not added"})
    listed = uitools._files(mono)
    assert "api/pyproject.toml" in listed and "notes.txt" not in listed and not any(f.startswith("node_modules") for f in listed)


def test_a_slow_extra_never_holds_the_page(monkeypatch):
    calls = []

    def slow():
        calls.append(1)
        time.sleep(1.5)
        return ["late"]
    t0 = time.time()
    assert watch.soon(("test", "slow"), slow, wait=0.2) is None and time.time() - t0 < 1.0   # not waited for
    time.sleep(1.6)
    assert watch.soon(("test", "slow"), lambda: ["fresh"], wait=0.5) == ["fresh"]              # done: the new value
    assert watch.soon(("test", "fast"), lambda: 42) == 42 and calls == [1]


def test_an_extensions_tabs_only_where_it_applies(monkeypatch, repo, files):
    ext = types.SimpleNamespace(NAME="research", VIEWS=[("research", "Research", "s")],
                                applies=lambda root: (root / "main.tex").exists())
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    assert extensions.views() == [("research", "Research", "s")]           # no repository: every extension
    assert extensions.views(repo) == []                                      # a software repository: none
    assert 'data-view="research"' not in watch.page_html(repo)
    assert pane.show(repo, 80, "now", take=False)["views"] == []
    files(repo, {"main.tex": "\\\\section{A}\\n"})
    subprocess.run(["git", "add", "main.tex"], cwd=repo, check=True, capture_output=True)   # the index moved: asked again
    assert extensions.views(repo) == [("research", "Research", "s")]
    cfg = (repo / "system-diff.toml").read_text(encoding="utf-8")
    files(repo, {"system-diff.toml": "extensions = false\n" + cfg})
    assert extensions.views(repo) == []                                      # the repository says no
    files(repo, {"system-diff.toml": 'extensions = ["research"]\n' + cfg})
    ext.applies = lambda root: False
    assert extensions.views(repo) == [("research", "Research", "s")]         # the repository says yes


def test_the_diagram_fits_its_box_and_hovering_moves_nothing():
    page = watch.page_html()
    assert "max-height:calc(100vh - 200px)" in page and "scrollbar-gutter:stable" in page
    assert "#detail { padding:12px 16px; height:12.5em; overflow:auto;" in page and "vb[2] * 1.8" in page


def test_research_and_system_tabs_one_set_at_a_time(monkeypatch, repo):
    ext = types.SimpleNamespace(NAME="research", TITLE="Research", applies=lambda root: True,
                                VIEWS=[("research", "Now", "s", "now"), ("evidence", "Evidence", "e"),
                                       ("check", "Review", "v", "review"), ("timeline", "History", "y", "history")])
    ext.home = lambda ctx: {"view": "research", "system": "collapsed"}
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    monkeypatch.setattr(extensions, "OPEN_VIEW", None)            # the Studio, a background engine: the home
    page = watch.page_html(repo)
    assert '"set": "research"' in page and '"title": "Research"' in page
    assert '["research", "Now", "s", "now"]' in page and '["evidence", "Evidence", "e", ""]' in page
    assert 'data-view="research">Now<kbd>n</kbd>' in page and 'data-view="check">Review<kbd>r</kbd>' in page
    assert "const OPEN = null;" in page and "function counterpart(view, to)" in page and "function keymap()" in page
    monkeypatch.setattr(extensions, "OPEN_VIEW", "now")           # system-diff watch, by hand: system-diff's Now
    assert 'const OPEN = "now";' in watch.page_html(repo)
    monkeypatch.setattr(extensions, "OPEN_VIEW", "research")      # system-diff watch --view research
    assert 'const OPEN = "research";' in watch.page_html(repo)
    monkeypatch.setattr(extensions, "OPEN_VIEW", "nowhere")
    assert "const OPEN = null;" in watch.page_html(repo)
    from system_diff import cli
    assert cli.parser().parse_args(["watch", "--view", "research"]).view == "research"
    assert extensions.views_full(repo)[0]["stands"] == "now" and extensions.views_full(repo)[1]["stands"] == ""


def test_the_page_has_one_ago_and_its_own_duration():
    page = watch.page_html()
    assert page.count("function ago(") == 1 and "function dur(sec)" in page and "go(histView()," in page


def test_two_starts_at_once_start_one_engine(monkeypatch, repo):
    """The start marker is claimed before it is written: a second caller in between waits for that start instead of
    taking the empty marker for a stale one and starting a second engine (two engines on one repository)."""
    from system_diff.sources import git_dir
    marker = git_dir(repo) / watch.STARTING
    marker.write_text("", encoding="utf-8")                 # claimed, not written yet
    assert watch._starting(repo) is not None and marker.exists()
    started = []
    monkeypatch.setattr(watch.subprocess, "Popen", lambda *a, **k: started.append(a) or None)
    assert watch.start_detached(repo, wait=0) is None and started == []
    old = time.time() - watch.STARTING_FOR - 5
    import os
    os.utime(marker, (old, old))                            # an empty marker left for long: stale, removed
    assert watch._starting(repo) is None and not marker.exists()
