"""Extensions: another package's views in system-diff's own engine, page and pane (research-diff's Evidence, Paper,
Auto-research), its own routes under /x/<name>/, what it watches moving the version, its files in the viewer, its
MCP tools; and a broken one never breaking system-diff."""
from __future__ import annotations

import json
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

from system_diff import canvas, extensions, pane, panel, watch


def fake_extension(seen):
    ext = types.SimpleNamespace(NAME="fake", WATCH=["fake/state.json"])
    ext.VIEWS = [("evidence", "Evidence", "e"), ("now", "Clash", "x"), ("paper", "Paper", "n")]

    def pane_(ctx):
        seen.append(("pane", ctx.view, ctx.part, ctx.live is None))
        page = canvas.Page(ctx.width)
        g = canvas.layered([{"id": "C1", "col": "claim", "title": "C1 Small batches win", "type": "component"},
                            {"id": "R1", "col": "ev", "title": "R1 acc 0.87", "type": "store"}],
                           [{"from": "R1", "to": "C1", "kind": "data", "status": "same"}],
                           [("ev", "Evidence"), ("claim", "Claims")], width=ctx.width)
        page.grid(g, slot=2, on={"b:C1": {"enter": {"zoom": "C1"}}})
        return page.json(view=ctx.view, part=ctx.part)
    ext.pane = pane_
    ext.web = lambda ctx: {"title": "Evidence", "headline": "1 claim", "diagram": {"svg": "<svg></svg>"},
                           "sections": [{"title": "Interactive", "aside": "", "kind": "frame",
                                         "items": [{"src": "/x/fake/graph", "height": 400}]}]}
    ext.route = lambda ctx, method, path, query, body: (200, "text/plain", f"{method} {path} {body.decode()}".encode())
    ext.file_ok = lambda path: path.endswith(".tex")
    ext.mcp = lambda server: seen.append(("mcp", server is not None))
    return ext


@pytest.fixture
def ext(monkeypatch):
    seen = []
    monkeypatch.setattr(extensions, "_LOADED", [fake_extension(seen)])
    return seen


def test_extension_views_join_system_diffs_without_clashes(ext):
    assert extensions.views() == [("evidence", "Evidence", "e"), ("paper", "Paper", "")]   # "now" and key n taken
    assert extensions.owner("evidence").NAME == "fake" and extensions.owner("now") is None


def test_the_pane_draws_an_extension_view_and_lists_its_tabs(ext, repo):
    page = pane.show(repo, 100, "evidence", "C1", take=False)
    assert page["view"] == "evidence" and any(b["id"] == "b:C1" for b in page["blocks"])
    assert page["views"] == [["evidence", "Evidence", "e"], ["paper", "Paper", ""]]
    assert ("pane", "evidence", "C1", True) in ext
    assert panel.fast(["panel", "--protocol", "2", "--view", "evidence", "--width", "100"]) is not False


def test_a_failing_extension_view_shows_its_error(monkeypatch, repo):
    bad = types.SimpleNamespace(NAME="bad", VIEWS=[("boom", "Boom", "o")])
    bad.pane = lambda ctx: 1 / 0
    bad.web = lambda ctx: 1 / 0
    monkeypatch.setattr(extensions, "_LOADED", [bad])
    assert "ZeroDivisionError" in json.dumps(pane.show(repo, 80, "boom", take=False))
    assert "ZeroDivisionError" in pane.web_view(repo, "boom")["headline"]


def test_the_page_serves_extension_tabs_views_and_routes(ext, repo):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)}, daemon=True)
    t.start()
    deadline = time.time() + 15
    while "url" not in up and time.time() < deadline:
        time.sleep(0.05)
    url = up["url"]
    try:
        page = urllib.request.urlopen(url, timeout=30).read().decode()
        assert 'data-view="evidence">Evidence<kbd>e</kbd>' in page and '"e": "evidence"' in page
        assert "RENDER.frame = frame" in page
        engine = getattr(up["w"], "engine", None)
        if engine is not None:
            assert engine.done.wait(60)
        web = json.loads(urllib.request.urlopen(url + "web?view=evidence", timeout=30).read())
        assert web["view"] == "evidence" and web["sections"][0]["kind"] == "frame"
        with pytest.raises(urllib.error.HTTPError) as anyone:                # the repository's content: not to anyone
            urllib.request.urlopen(url + "x/fake/graph", timeout=30)
        assert anyone.value.code == 403
        own = urllib.request.Request(url + "x/fake/graph", headers={"Referer": url})   # the page's own frame
        assert urllib.request.urlopen(own, timeout=30).read() == b"GET graph "
        evil = urllib.request.Request(url + "x/fake/graph", headers={"Referer": url, "Host": "evil.example:80"})
        with pytest.raises(urllib.error.HTTPError) as rebound:              # DNS rebinding: another host name
            urllib.request.urlopen(evil, timeout=30)
        assert rebound.value.code == 403
        req = urllib.request.Request(url + "x/fake/act", data=b"go", method="POST")
        with pytest.raises(urllib.error.HTTPError) as no:                    # neither the token nor the page
            urllib.request.urlopen(req, timeout=30)
        assert no.value.code == 403
        req = urllib.request.Request(url + "x/fake/act", data=b"go", method="POST", headers={"X-Token": up["w"].token})
        assert urllib.request.urlopen(req, timeout=30).read() == b"POST act go"
        with pytest.raises(urllib.error.HTTPError) as none:
            urllib.request.urlopen(urllib.request.Request(url + "x/nobody/x", headers={"X-Token": up["w"].token}),
                                   timeout=30)
        assert none.value.code == 404
    finally:
        up["server"].shutdown()
        t.join(5)


def test_extension_files_and_state_and_tools(ext, repo, files):
    files(repo, {"paper/main.tex": "\\section{Results}\n"})
    assert watch.read_code_file(repo, "paper/main.tex")["text"] == "\\section{Results}\n"
    assert watch.read_code_file(repo, "paper/notes.md")["text"] is None
    from system_diff.sources import git_dir
    g = git_dir(repo).parent
    before = extensions.watched(g)
    (g / "fake").mkdir(exist_ok=True)
    (g / "fake" / "state.json").write_text("{}")
    assert extensions.watched(g) != before
    pytest.importorskip("mcp")
    from system_diff import mcp_server
    mcp_server.build()
    assert ("mcp", True) in ext
