"""The page as tools for Claude: the page tells the engine what it shows (ui_view), Claude moves it (ui_show: the
page's long poll and the pane's look carry the request), reads a view's diagram as data (ui_graph), finds where a
block comes from (ui_source) and previews a system-diff.toml change without writing it (ui_config_preview)."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

from system_diff import chat, extensions, uitools, watch


@pytest.fixture
def served(repo):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)}, daemon=True)
    t.start()
    deadline = time.time() + 15
    while "url" not in up and time.time() < deadline:
        time.sleep(0.05)
    engine = getattr(up["w"], "engine", None)
    if engine is not None:
        assert engine.done.wait(90)
    yield up
    up["server"].shutdown()
    t.join(5)


def post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def test_the_page_says_what_it_shows_and_only_to_the_engine(served, repo):
    url, tok = served["url"], {"X-Token": served["w"].token}
    for h in ({}, {"X-Token": "wrong"}, {**tok, "Host": "evil.example:80"}):
        with pytest.raises(urllib.error.HTTPError) as no:
            post(url + "ui/state", {"page": "p1", "view": "now"}, h)
        assert no.value.code == 403
    post(url + "ui/state", {"page": "p1", "view": "now", "picked": "b:api", "hover": "b:core", "visible": True}, tok)
    post(url + "ui/state", {"page": "p1", "view": "review", "picked": "b:api"}, {"Referer": url})   # the page itself
    got = uitools.view(repo)
    assert [p["view"] for p in got["pages"]] == ["review"] and got["pages"][0]["picked"] == "b:api"
    assert any(v["view"] == "history" for v in got["views"])
    served["w"].ui_pages["p1"]["at"] -= watch.UI_KEEP + 1     # not heard from for long: closed
    assert uitools.view(repo)["pages"] == []


def test_claude_moves_the_page_and_the_pane(served, repo):
    url, tok = served["url"], {"X-Token": served["w"].token}
    assert uitools.show(repo, "nowhere")["error"].startswith("no view")
    got = uitools.show(repo, "now", "api", pick="core", note="the API calls Core", wait=0)
    assert got["page"].startswith("queued")
    web = json.loads(urllib.request.urlopen(urllib.request.Request(url + "web?view=now", headers=tok), timeout=60).read())
    assert web["chat"]["show"]["id"] == got["id"] and web["chat"]["show"]["part"] == "api"
    pane = json.loads(urllib.request.urlopen(urllib.request.Request(url + "page?view=now&width=80", headers=tok),
                                             timeout=60).read())
    assert pane["page"]["show"]["id"] == got["id"]
    post(url + "ui/state", {"page": "p2", "view": "now", "part": "api", "shown": got["id"], "moved": True}, tok)
    again = uitools.show(repo, "review", wait=0)
    states = {p["page"]: p for p in uitools.view(repo)["pages"]}
    assert states["p2"]["shown"] == got["id"] and states["p2"]["moved"] and states["pane"]["view"] == "now"
    assert again["id"] != got["id"]
    page = watch.page_html()
    assert "function takeShow(sh)" in page and "Let Claude move my page" in page and 'fetch("ui/state"' in page


def test_a_shown_request_goes_stale(repo):
    rec = chat.show_page(repo, "now", "api")
    assert chat.showing(repo)["id"] == rec["id"]
    assert chat.showing(repo, now=rec["at"] + chat.SHOW_FOR + 1) is None


def test_the_diagram_as_data_for_system_diff_and_an_extension(monkeypatch, repo):
    g = uitools.graph(repo, "now")
    ids = {n["id"] for n in g["nodes"]}
    assert {"api", "core"} <= ids and g["edges"] and "svg" not in json.dumps(g)
    assert all(e["from"] in ids and e["to"] in ids for e in g["edges"])
    ext = types.SimpleNamespace(NAME="research", VIEWS=[("evidence", "Evidence", "e")])
    ext.web = lambda ctx: {"title": "Evidence", "headline": "2 claims",
                           "diagram": {"svg": "<svg>secret drawing</svg>",
                                       "details": {"b:C1": {"title": "C1 small batches win", "headline": "supported"}},
                                       "enter": {"b:C1": "C1"}},
                           "sections": [{"title": "Claims", "kind": "notes", "items": [{"text": "C1", "svg": "<svg/>"}]}]}
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    e = uitools.graph(repo, "evidence")
    assert e["nodes"] == [{"id": "C1", "title": "C1 small batches win"}] and "svg" not in json.dumps(e)
    assert e["details"]["C1"]["headline"] == "supported" and e["sections"][0]["items"] == [{"text": "C1"}]


def test_where_a_block_comes_from(repo):
    got = uitools.source(repo, "b:api")
    assert got["kind"] == "part" and any(f["path"].startswith("app/api/") for f in got["files"])
    assert got["config"]["in"] == "system-diff.toml" and 'id = "api"' in got["config"]["toml"]
    assert uitools.source(repo, "nothing-here")["kind"] == "unknown"


def test_a_config_preview_writes_nothing(repo):
    before = (repo / "system-diff.toml").read_text(encoding="utf-8")
    got = uitools.config_preview(repo, '[[components]]\nid = "api"\nlabel = "Billing"\n')
    assert (repo / "system-diff.toml").read_text(encoding="utf-8") == before and got["written"] is False
    assert any(r["id"] == "api" and "Billing" in str(r["to"]) for r in got["differs"]["renamed"])
    assert "Billing" not in json.dumps(uitools.graph(repo, "now")["nodes"])
    assert uitools.config_preview(repo, "not = [toml")["error"].startswith("not TOML")


def test_the_page_tools_are_listed_once(monkeypatch):
    pytest.importorskip("mcp")
    from system_diff import mcp_server
    ext = types.SimpleNamespace(NAME="research", VIEWS=[])
    ext.mcp = lambda server: server.tool(name="research_explain")(lambda claim="": {"claim": claim})
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    names = [t.name for t in asyncio.run(mcp_server.build().list_tools())]
    for n in ("ui_view", "ui_graph", "ui_source", "ui_show", "ui_config_preview", "research_explain"):
        assert names.count(n) == 1
