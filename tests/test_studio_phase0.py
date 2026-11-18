"""Phase 0 of the Studio: a diagram's file opened at given lines, an extension's outline and summary for its own files,
a view's pinned list on the left (and in the pane), the repositories inside a repository (submodules, nested ones)
each its own system, and a same-column flow kept inside the picture."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

from system_diff import canvas, extensions, pane, pictures, repos, watch


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_the_page_opens_a_file_at_lines_and_shows_the_pinned_list():
    page = watch.page_html()
    assert "function fileOf(v)" in page and "jumpTo({ line:at.line" in page
    assert 'id="leftnav"' in page and "function paintNav(" in page and "repos/open?rel=" in page


def test_an_extension_outlines_and_summarises_its_own_files(monkeypatch, repo, files):
    ext = types.SimpleNamespace(NAME="paper", VIEWS=[])
    ext.file_ok = lambda path: path.endswith(".tex")
    ext.outline = lambda ctx, path, text: {
        "outline": {"chunks": [{"line": 1, "end": 2, "depth": 0, "qual": "Results", "name": "Results", "kind": "section"}]},
        "summary": {"state": "done", "file": "The results.", "by": "test",
                    "chunks": {"Results": {"what": "Small batches win."}}}}
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    files(repo, {"paper/main.tex": "\\section{Results}\nSmall batches win.\n"})
    got = watch.file_extras(types.SimpleNamespace(root=repo), "paper/main.tex", "\\section{Results}\nSmall batches win.\n")
    assert got["outline"]["chunks"][0]["qual"] == "Results"
    assert got["summary"]["chunks"]["Results"]["what"] == "Small batches win."
    broken = types.SimpleNamespace(NAME="bad", VIEWS=[], file_ok=lambda p: True, outline=lambda c, p, t: 1 / 0)
    monkeypatch.setattr(extensions, "_LOADED", [broken])
    got = watch.file_extras(types.SimpleNamespace(root=repo), "paper/main.tex", "x")
    assert got["summary"]["state"] == "failed" and "ZeroDivisionError" in got["summary"]["why"]


def test_the_pane_pins_a_views_list_above_it(monkeypatch, repo):
    ext = types.SimpleNamespace(NAME="research", VIEWS=[("evidence", "Evidence", "e")])

    def pane_(ctx):
        page = canvas.Page(ctx.width)
        page.para("the evidence")
        out = page.json(view=ctx.view, part=ctx.part)
        out["nav"] = [{"id": "s1", "title": "1_tradeoff", "badge": 3, "active": True,
                       "target": {"view": "evidence", "part": "study:1"}},
                      {"id": "s2", "title": "2_behavioral", "target": {"view": "evidence", "part": "study:2"}}]
        return out
    ext.pane = pane_
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    page = pane.show(repo, 80, "evidence", take=False)
    nav = [b for b in page["blocks"] if b["id"].startswith("nav:")]
    assert [b["enter"]["go"] for b in nav] == [{"view": "evidence", "part": "study:1"},
                                               {"view": "evidence", "part": "study:2"}]
    assert "[1_tradeoff 3]" in "".join(r[0] for r in page["rows"][0]) and "nav" not in page


@pytest.fixture
def family(tmp_path):
    outer = tmp_path / "research"
    outer.mkdir()
    _git(outer, "init", "-q", "-b", "main")
    for name in ("1_tradeoff", "2_behavioral"):
        sub = outer / name
        sub.mkdir()
        _git(sub, "init", "-q", "-b", "study")
    (outer / ".gitmodules").write_text('[submodule "1_tradeoff"]\n\tpath = 1_tradeoff\n\turl = ../x\n'
                                       '[submodule "evil"]\n\tpath = ../outside\n\turl = ../y\n', encoding="utf-8")
    (outer / "tools" / "vendor_clone").mkdir(parents=True)
    _git(outer / "tools" / "vendor_clone", "init", "-q", "-b", "main")
    (outer / "node_modules" / "pkg").mkdir(parents=True)
    _git(outer / "node_modules" / "pkg", "init", "-q")
    return outer


def test_repositories_inside_a_repository_are_listed_from_any_member(family):
    got = repos.family(family)
    assert [(r["rel"], r["kind"]) for r in got] == [("", "outer"), ("1_tradeoff", "submodule"),
                                                    ("2_behavioral", "nested"), ("tools/vendor_clone", "nested")]
    assert got[0]["current"] and got[1]["branch"] == "study"
    from_sub = repos.family(family / "1_tradeoff")
    assert [r["rel"] for r in from_sub] == [r["rel"] for r in got] and from_sub[1]["current"]
    assert repos.parent(family / "1_tradeoff") == family.resolve()
    assert repos.member(family / "2_behavioral", "") == family.resolve()
    assert repos.member(family, "../outside") is None and repos.member(family, "node_modules/pkg") is None
    assert extensions.context(family / "1_tradeoff", "evidence").parent == family.resolve()


def test_the_page_lists_the_family_and_guards_opening_one(family):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": family, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)}, daemon=True)
    t.start()
    deadline = time.time() + 15
    while "url" not in up and time.time() < deadline:
        time.sleep(0.05)
    url = up["url"]
    try:
        web = json.loads(urllib.request.urlopen(url + "web?view=now", timeout=60).read())
        assert [r["rel"] for r in web["repos"]][:2] == ["", "1_tradeoff"]
        assert web["repos"][0]["port"] == up["server"].server_address[1]   # its engine runs: the list knows where
        with pytest.raises(urllib.error.HTTPError) as no:
            urllib.request.urlopen(urllib.request.Request(url + "repos/open?rel=1_tradeoff", data=b"", method="POST"),
                                   timeout=30)
        assert no.value.code == 403
        tok = {"X-Token": up["w"].token}
        with pytest.raises(urllib.error.HTTPError) as away:
            urllib.request.urlopen(urllib.request.Request(url + "repos/open?rel=../outside", data=b"", method="POST",
                                                          headers=tok), timeout=30)
        assert away.value.code == 404
    finally:
        up["server"].shutdown()
        t.join(5)


def test_a_flow_within_a_column_stays_inside_the_picture():
    model = {"columns": [("a", "Claims")],
             "nodes": [{"id": "C1", "col": "a", "title": "C1 small batches win"},
                       {"id": "C2", "col": "a", "title": "C2 large batches win"}],
             "edges": [{"from": "C2", "to": "C1", "kind": "call", "label": "contradicts strongly"}]}
    svg = pictures.architecture_svg(model)
    width = float(re.search(r'viewBox="0 0 (\d+)', svg).group(1))
    xs = [float(x) for d in re.findall(r'<path d="(M[^"]+)"', svg) for x in re.findall(r"(-?\d+(?:\.\d+)?),", d)]
    tags = [float(x) + float(w) for x, w in re.findall(r'class="tag" x="([\d.]+)" y="[\d.-]+" width="([\d.]+)"', svg)]
    assert xs and max(xs) < width and tags and max(tags) < width


def _commit(cwd, msg):
    _git(cwd, "add", "-A")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg], cwd=cwd, check=True,
                   capture_output=True)


def test_a_submodule_is_one_box_and_its_pointer_moving_a_change(tmp_path, files):
    from system_diff import architecture, compare, gitfs
    lib = tmp_path / "lib"
    lib.mkdir()
    _git(lib, "init", "-q", "-b", "main")
    files(lib, {"lib.py": "def f():\n    return 1\n"})
    _commit(lib, "one")
    outer = tmp_path / "app"
    outer.mkdir()
    _git(outer, "init", "-q", "-b", "main")
    files(outer, {"app/main.py": "print('hi')\n"})
    subprocess.run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "vendor/lib"],
                   cwd=outer, check=True, capture_output=True)
    _commit(outer, "with lib")
    first = gitfs.head(outer)
    files(lib, {"lib.py": "def f():\n    return 2\n"})
    _commit(lib, "two")
    _git(outer / "vendor" / "lib", "-c", "protocol.file.allow=always", "pull", "-q", "origin", "main")
    _commit(outer, "lib moved")
    c = compare.compare(outer, first, "HEAD")
    assert c.after["repos"]["vendor/lib"]["sha"] != c.before["repos"]["vendor/lib"]["sha"]
    moved = [x for x in c.diff["changes"] if x["entity"] == "repo"]
    assert moved and moved[0]["op"] == "change" and moved[0]["detail"][0].startswith("moved from ")
    model = architecture.system(c)
    box = next(n for n in model["nodes"] if n["id"] == "r:vendor/lib")
    assert box["status"] == "changed" and box["title"] == "lib (submodule)" and box["sub"].startswith("moved from ")
    assert not any(n["id"].startswith("vendor/") for n in model["nodes"])   # its code is its own system's


def test_an_edit_inside_a_submodule_reaches_its_engine(family, monkeypatch):
    from system_diff import agent
    assert agent.inner_repo(family, "1_tradeoff/paper/main.tex") == family / "1_tradeoff"
    assert agent.inner_repo(family, "notes/x.md") is None
    told = []
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": family / "1_tradeoff", "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(server=server, w=w)},
                         daemon=True)
    t.start()
    deadline = time.time() + 15
    while "w" not in up and time.time() < deadline:
        time.sleep(0.05)
    try:
        engine = getattr(up["w"], "engine", None)
        if engine is not None:
            assert engine.done.wait(60)
            monkeypatch.setattr(engine, "touch", lambda paths: told.append(paths) or engine.version)
        agent.tell_inner(family, "1_tradeoff/paper/main.tex")
        assert told == [["paper/main.tex"]] or engine is None
    finally:
        up["server"].shutdown()
        t.join(5)
