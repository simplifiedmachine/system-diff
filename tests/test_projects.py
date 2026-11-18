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
    assert extensions.home(repo) == {"view": "research", "system": "collapsed"}
    page = watch.page_html(repo)
    assert 'const HOME = {"view": "research", "system": "collapsed"}' in page and "System ▸" in page
    assert pane.show(repo, 80, "now", take=False)["home"]["view"] == "research"
    ext.home = lambda ctx: {"view": "not-a-view"}
    assert extensions.home(repo) is None
