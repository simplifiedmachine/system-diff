"""The pane beside Claude Code in a browser: `system-diff watch` serves its pages as JSON and a page that paints them,
and says where it listens in .git/system-diff/web.json."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from system_diff import watch
from system_diff.sources import git_dir


def _get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.status, r.read().decode()


@pytest.fixture
def served(repo):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server)},
                         daemon=True)
    t.start()
    deadline = time.time() + 15
    while "url" not in up and time.time() < deadline:
        time.sleep(0.05)
    yield up["url"]
    up["server"].shutdown()
    t.join(5)


@pytest.mark.parametrize("view", ["now", "review", "agents", "topics"])
def test_each_view_is_data_for_the_page(served, view):
    status, body = _get(served + f"web?view={view}&zoom=1")
    page = json.loads(body)
    assert status == 200 and page["view"] == view
    for key in ("part", "title", "headline", "diagram", "sections", "send"):
        assert key in page
    assert page["send"] == []
    if view == "now":
        assert set(page["status"]) >= {"working", "doing", "part", "prompt"}
    d = page["diagram"]
    assert set(d) >= {"svg", "title", "aside", "lights", "details", "enter", "pick"}
    assert isinstance(d["svg"], str) and isinstance(d["lights"], dict) and isinstance(d["details"], dict)
    kinds = {"notes", "steps", "lanes", "chips", "lines"}
    for s in page["sections"]:
        assert s["kind"] in kinds and isinstance(s["items"], list) and "title" in s


def test_the_web_data_refuses_what_it_does_not_know(served):
    page = json.loads(_get(served + "web")[1])
    assert page["view"] == "now"
    for bad in ("web?view=nowhere", "web?view=map", "web?zoom=x", "web?zoom=7"):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(served + bad)
        assert e.value.code == 400


def test_the_web_data_leaves_what_was_sent_for_the_pane(served, repo):
    watch.outbox_put(repo, "please add a health check")
    assert json.loads(_get(served + "web?view=now")[1])["send"] == []
    assert "please add a health check" in (git_dir(repo) / "outbox.jsonl").read_text()


def test_the_page_is_one_app_that_draws_the_views(served):
    status, html = _get(served)
    assert status == 200
    for word in ("Now", "Review", "Agents", "Topics", "Follow Claude", "Back", "Send to Claude"):
        assert word in html
    assert 'id="dia"' in html and 'fetch("web?' in html and "data-id" in html
    assert "prefers-color-scheme: dark" in html
    assert "<script>" in html and "src=" not in html and "href=\"http" not in html   # all inline, nothing external
    for gone in ("classic", "pane"):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(served + gone)
        assert e.value.code == 404


def test_where_it_listens_is_written_while_it_serves(repo):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server)},
                         daemon=True)
    t.start()
    deadline = time.time() + 15
    while "url" not in up and time.time() < deadline:
        time.sleep(0.05)
    f = git_dir(repo) / "web.json"
    info = json.loads(f.read_text())
    assert info["url"] == up["url"] and info["pid"] == os.getpid() and up["url"].endswith(f":{info['port']}/")
    assert watch.web_url(repo) == up["url"].replace("127.0.0.1", "localhost")   # what a browser opens
    up["server"].shutdown()
    t.join(5)
    assert not f.exists() and watch.web_url(repo) is None


def test_a_stale_file_says_nothing_and_is_removed(repo):
    with socket.socket() as s:          # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    f = git_dir(repo) / "web.json"
    f.write_text(json.dumps({"url": f"http://127.0.0.1:{port}/", "port": port, "pid": os.getpid()}))
    assert watch.web_url(repo) is None and not f.exists()
    f.write_text("not json")
    assert watch.web_url(repo) is None and not f.exists()
    assert watch.web_url(Path("/")) is None   # not a repository: no page, no error


def _post(url: str, body: bytes, headers: dict):
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None


def _token(url: str) -> str:
    html = _get(url)[1]
    return html.split('const TOKEN = "', 1)[1].split('"', 1)[0]


def test_send_queues_what_was_typed_for_claude(served, repo):
    token, origin = _token(served), served.rstrip("/")
    outbox = git_dir(repo) / "outbox.jsonl"
    body = json.dumps({"text": "  please add a test  "}).encode()
    status, out = _post(served + "send", body, {"X-Token": token, "Origin": origin,
                                                 "Content-Type": "application/json"})
    assert status == 200 and out["queued"] is True and len(out["id"]) == 32
    lines = [json.loads(x) for x in outbox.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["id"] == out["id"] and lines[0]["text"] == "please add a test"
    assert abs(lines[0]["at"] - time.time()) < 60
    status, _ = _post(served + "send", json.dumps({"text": "again"}).encode(),
                      {"X-Token": token, "Referer": served})   # no Origin: the Referer stands in
    assert status == 200 and len(outbox.read_text().splitlines()) == 2


@pytest.mark.parametrize("who", ["wrong token", "foreign origin", "no token", "no origin"])
def test_send_refuses_anyone_but_the_page(served, repo, who):
    token, origin = _token(served), served.rstrip("/")
    headers = {"wrong token": {"X-Token": "nope", "Origin": origin},
               "foreign origin": {"X-Token": token, "Origin": "http://evil.example"},
               "no token": {"Origin": origin},
               "no origin": {"X-Token": token}}[who]
    status, _ = _post(served + "send", json.dumps({"text": "rm -rf /"}).encode(), headers)
    assert status == 403
    assert not (git_dir(repo) / "outbox.jsonl").exists()


@pytest.mark.parametrize("body", [{"text": "   "}, {"text": ""}, {}, {"text": "x" * 8001}, ["text"]])
def test_send_needs_some_text(served, repo, body):
    status, _ = _post(served + "send", json.dumps(body).encode(), {"X-Token": _token(served), "Origin": served.rstrip("/")})
    assert status == 400
    assert not (git_dir(repo) / "outbox.jsonl").exists()


def test_the_page_has_a_send_box_that_keeps_shortcuts_out(served):
    html = _get(served)[1]
    assert 'id="say"' in html and "Send to Claude" in html and 'fetch("send"' in html
    assert "TEXTAREA" in html   # the page's single-key shortcuts skip typing in the box


def test_the_web_page_refreshing_never_swallows_what_was_sent_to_claude(repo):
    from system_diff import pane, watch
    watch.outbox_put(repo, "please add a health check")
    assert pane.show(repo, 120, "now", take=False)["send"] == []        # the page's own look: left for the pane
    assert [m["text"] for m in pane.show(repo, 120, "now")["send"]] == ["please add a health check"]


def test_in_a_codespace_the_page_is_linked_and_sent_from_its_forwarded_address():
    from system_diff import watch
    env = {"CODESPACE_NAME": "fancy-name", "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN": "app.github.dev"}
    assert watch.forwarded(8765, env) == "https://fancy-name-8765.app.github.dev/"
    assert "https://fancy-name-8765.app.github.dev" in watch.origins(8765, env)
    assert watch.forwarded(8765, {}) == "" and watch.origins(8765, {}) == {"http://127.0.0.1:8765", "http://localhost:8765"}
