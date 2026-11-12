"""The resident engine (live.py): the working tree held in memory, told of edits (POST /touch) or finding them itself
(git status), answering pages by version with a long poll; and the web page's hover cards."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from system_diff import compare, extract, live, watch
from system_diff.sources import git_dir

from conftest import git_, write


def _fresh(repo: Path, base: str = "checkpoint"):
    """compare() as it is without an engine: a snapshot of the working tree."""
    saved = dict(compare.LIVE)
    compare.LIVE.clear()
    try:
        return compare.compare(repo, base, ".")
    finally:
        compare.LIVE.update(saved)


@pytest.fixture
def engine(repo):
    e = live.Engine(repo, interval=3600).start()   # ticks only when a test says so
    assert e.done.wait(30)
    yield e
    e.stop()


def test_the_engine_compares_as_a_snapshot_does(repo, engine):
    write(repo, {"app/api/ready.py": 'from fastapi import APIRouter\nr = APIRouter()\n\n@r.get("/ready")\ndef ready():\n'
                                     '    return 1\n',
                 "app/core/rules.py": "import os\nX = os.getenv('PAYMENT_KEY')\n",
                 "ignored.log": "x"})
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "app/db/repo.py").unlink()
    engine.sync()
    held = compare.live(repo)
    assert held is not None and held.known is None and "ignored.log" not in held.source.files
    for base in ("checkpoint", "HEAD~1"):
        mine, theirs = compare.compare(repo, base, "."), _fresh(repo, base)
        assert mine.head_sha == held.sha and mine.after == theirs.after and mine.before == theirs.before
        assert mine.diff == theirs.diff
    assert "GET /ready" in mine.diff["plan"]
    # the commit it makes when one is needed (a checkpoint): the same tree as git add -A would make
    assert git_(repo, "rev-parse", f"{engine.commit}^{{tree}}").strip() == compare.worktree_tree(repo)
    assert git_(repo, "rev-parse", f"{engine.commit}^").strip() == git_(repo, "rev-parse", "HEAD").strip()


def test_touch_reads_only_the_files_that_changed(repo, engine, monkeypatch):
    seen = []
    code = extract.SECTIONS["code"]
    monkeypatch.setitem(extract.SECTIONS, "code", lambda path, text: (seen.append(path), code(path, text))[1])
    v = engine.version
    write(repo, {"app/core/rules.py": "import os\nX = os.getenv('PAYMENT_KEY')\n"})
    assert engine.touch([str(repo / "app/core/rules.py"), "app/api/routes.py", "/elsewhere/x.py"]) == v + 1
    assert seen == ["app/core/rules.py"]                              # one file parsed again, not the others
    assert "PAYMENT_KEY" in compare.compare(repo, "checkpoint", ".").diff["plan"]
    assert engine.touch(["app/core/rules.py"]) == v + 1 and seen == ["app/core/rules.py"]   # unchanged: nothing
    (repo / ".gitignore").write_text("secret.py\n")
    write(repo, {"secret.py": "import os\nos.getenv('X')\n"})
    engine.touch(["secret.py"])
    assert "secret.py" not in engine.entries                          # ignored files stay out, as git add -A
    (repo / "app/core/rules.py").unlink()
    engine.touch(["app/core/rules.py"])
    assert "app/core/rules.py" not in compare.live(repo).source.files


def test_git_status_finds_an_edit_made_outside_claude(repo, engine):
    v = engine.version
    write(repo, {"app/jobs/new.py": "import os\nos.getenv('QUEUE_URL')\n"})
    assert engine.tick() and engine.version > v
    assert "app/jobs/new.py" in engine.entries
    v = engine.version
    assert not engine.tick() and engine.version == v                 # nothing new: no new version
    git_(repo, "add", "-A")
    git_(repo, "commit", "-q", "-m", "third")                          # the index and HEAD move
    engine.tick()
    assert engine.head == git_(repo, "rev-parse", "HEAD").strip()
    assert compare.compare(repo, "HEAD", ".").diff["changes"] == []
    write(repo, {"app/jobs/new.py": "x = 1\n"})
    git_(repo, "checkout", "-q", "--", "app/jobs/new.py")             # back as committed: the engine follows
    engine.tick()
    assert engine.entries["app/jobs/new.py"].sha == git_(repo, "rev-parse", "HEAD:app/jobs/new.py").strip()


def test_a_cold_start_publishes_partial_blueprints(repo, monkeypatch):
    for i in range(30):
        write(repo, {f"app/many/m{i}.py": f"import os\nV{i} = os.getenv('V{i}')\n"})
    code = extract.SECTIONS["code"]
    monkeypatch.setitem(extract.SECTIONS, "code", lambda path, text: (time.sleep(0.03), code(path, text))[1])
    e = live.Engine(repo, interval=3600, slice_seconds=0.1).start()
    try:
        assert e.ready.wait(10)
        first = compare.live(repo)
        assert first.known is not None and first.blueprint["partial"]["done"] < first.blueprint["partial"]["total"]
        assert e.done.wait(30) and "partial" not in compare.live(repo).blueprint and e.version > 1
    finally:
        e.stop()


@pytest.fixture
def served(repo):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)},
                         daemon=True)
    t.start()
    deadline = time.time() + 15
    while "w" not in up and time.time() < deadline:
        time.sleep(0.05)
    assert up["w"].engine.done.wait(30)
    yield up
    up["server"].shutdown()
    t.join(5)


def _call(url, method="GET", headers=None, body=None, timeout=40):
    req = urllib.request.Request(url, method=method, headers=headers or {},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def test_page_and_touch_need_the_token(served, repo):
    url, token = served["url"], served["w"].token
    info = json.loads((git_dir(repo) / "web.json").read_text())
    assert info["token"] == token
    assert _call(url + "page?view=now&width=90")[0] == 403
    assert _call(url + "page?view=now&width=90", headers={"X-Token": "nope"})[0] == 403
    assert _call(url + "page?view=now", headers={"Origin": "https://evil.example"})[0] == 403
    assert _call(url + "touch", "POST", body={"paths": ["a.py"]})[0] == 403
    assert _call(url + "touch", "POST", {"X-Token": token, "Origin": "https://evil.example"},
                 {"paths": ["a.py"]})[0] == 403
    status, got = _call(url + "page?view=now&width=90", headers={"X-Token": token})
    assert status == 200 and got["version"] >= 1 and isinstance(got["page"], dict) and got["page"]["send"] == []
    origin = url.rstrip("/")
    assert _call(url + "page?view=review&width=90", headers={"Origin": origin})[0] == 200   # its own page
    assert _call(url + "page?view=nope", headers={"X-Token": token})[0] == 400
    assert _call(url + "touch", "POST", {"X-Token": token}, {"paths": "a.py"})[0] == 400


def test_the_long_poll_answers_when_the_version_moves(served, repo):
    url, w = served["url"], served["w"]
    first = live.client_page(repo, "now", 90)
    assert first is not None and first["version"] == w.engine.version
    t0 = time.monotonic()
    again = live.client_page(repo, "now", 90)
    assert again["version"] == first["version"] and again["page"] == first["page"]
    assert time.monotonic() - t0 < 1.0                                  # answered from memory
    got = {}
    t = threading.Thread(target=lambda: got.update(r=live.client_page(repo, "now", 90, since=first["version"])))
    t.start()
    time.sleep(0.3)
    assert t.is_alive()                                                 # waiting for something new
    write(repo, {"app/core/rules.py": "import os\nX = os.getenv('PAYMENT_KEY')\n"})
    t0 = time.monotonic()
    live.touch(repo, ["app/core/rules.py"])
    t.join(10)
    assert not t.is_alive() and got["r"]["version"] > first["version"] and time.monotonic() - t0 < 5
    # another width, not drawn at this version yet: answered at once, though `since` is current
    t0 = time.monotonic()
    wide = live.client_page(repo, "now", 140, since=got["r"]["version"])
    assert wide["version"] == got["r"]["version"] and time.monotonic() - t0 < 5
    # the pane's long poll returns as soon as the web page sends something to Claude
    out = {}
    t = threading.Thread(target=lambda: out.update(r=live.client_page(repo, "now", 140, since=wide["version"],
                                                                       take=True)))
    t.start()
    time.sleep(0.3)
    assert t.is_alive()
    origin = url.rstrip("/")
    assert _call(url + "send", "POST", {"X-Token": w.token, "Origin": origin, "Content-Type": "application/json"},
                 {"text": "why a new table?"})[0] == 200
    t.join(5)
    assert not t.is_alive() and [s["text"] for s in out["r"]["page"]["send"]] == ["why a new table?"]
    status, web = _call(url + "web?view=now&zoom=1&since=-5")
    assert status == 200 and web["version"] == w.engine.version and web["view"] == "now"


def test_no_engine_no_page(tmp_path):
    assert live.client_page(tmp_path) is None
    live.touch(tmp_path, ["x.py"])                                      # never raises


def test_the_page_renders_hover_cards_as_text_only():
    page = watch.PAGE
    start = page.index("// ---- hover cards")
    code = page[start: page.index("function section(", start)]
    assert "innerHTML" not in code
    assert "el(\"div\", String(card.title)" in code and "el(\"div\", String(line)" in code   # el() sets textContent
    assert re.search(r"function el\(tag, text, cls\) \{[^}]*textContent = text", page)
    assert page.count("innerHTML") == 1 and "box.innerHTML = g.svg" in page          # only the engine's own SVG
    assert "}, 150);" in code and "focusin" in code and "360px" in page
    assert "Ask Claude" not in page                                     # the page only shows: no action buttons
    assert 'fetch("send"' in page and "/accept" not in page


def test_an_engine_an_upgrade_left_running_is_replaced_not_asked(served, repo, monkeypatch):
    """Its code was loaded before the upgrade: clients do not take pages from it, and the next start retires it."""
    from system_diff import live as live_mod
    f = git_dir(repo) / "web.json"
    info = json.loads(f.read_text())
    assert info["version"]                               # an engine says which system-diff it is
    assert live_mod.client_page(repo, "now", 80) is not None
    f.write_text(json.dumps({**info, "version": "0.0.1"}))   # as if it were an older one
    assert live_mod.client_page(repo, "now", 80) is None
    assert watch.web_url(repo) is None                   # retired (POST /quit) ...
    deadline = time.time() + 5
    while time.time() < deadline and watch._whoami(info["port"], info["id"]):
        time.sleep(0.05)
    assert not watch._whoami(info["port"], info["id"])   # ... and gone


def test_quit_needs_the_token_and_no_browser(served):
    url = served["url"]
    assert _call(url + "quit", "POST", body={})[0] == 403
    assert _call(url + "quit", "POST", headers={"X-Token": served["w"].token, "Origin": url.rstrip("/")},
                 body={})[0] == 403


def test_the_shared_pages_cache_keeps_complete_pages_and_marks_what_it_serves(repo):
    key = ("pane", "now", 100, -1, "")
    page = {"protocol": 2, "view": "now", "rows": [[["SYSTEM", "heading"]]], "blocks": [{"id": "b:api", "y": 0,
            "slot": [3, 2]}], "wide": 100, "send": [{"id": "x", "text": "secret for Claude"}], "version": 7}
    live._saved_at.clear()
    live.save_page(repo, ("pane", "now", 100, -1, "partial"), {**page, "reading": {"done": 1, "total": 9}})
    assert live.saved_page(repo, ("pane", "now", 100, -1, "partial")) is None      # a partial page is never kept
    live.save_page(repo, key, page)
    got = live.saved_page(repo, key, "12 of 430 files")
    assert got["reading"]["stale"] and got["send"] == [] and "version" not in got
    assert got["rows"][0][0][0].startswith("◐ As you last saw it") and "12 of 430 files" in got["rows"][0][0][0]
    assert got["rows"][3] == [["SYSTEM", "heading"]] and got["blocks"][0]["y"] == 3   # the blocks move down with it
    assert "secret" not in (git_dir(repo) / "pages").joinpath(live._page_name(key) + ".json").read_text()
    other = live.saved_page(repo, ("pane", "now", 140, -1, ""))                     # another width: the newest of
    assert other is not None and other["reading"]["stale"]                            # the same view


def test_while_the_engine_reads_again_it_serves_the_page_last_kept(repo):
    from system_diff import panel
    key = ("pane", "now", 80, -1, "")
    live._saved_at.clear()
    live.save_page(repo, key, {"protocol": 2, "view": "now", "rows": [[["last time", ""]]], "blocks": [], "wide": 80})

    class Busy:   # an engine after a restart: started, not done reading
        root = repo
        version = 3
        ready, done, stopped = threading.Event(), threading.Event(), threading.Event()

        def progress(self):
            return {"stage": "Reading the code", "done": 40, "total": 430, "seconds": 2}
    pages = live.Pages(Busy(), build=lambda k: {"rows": [], "fresh": True}, placeholder=lambda k: {"placeholder": 1})
    served = pages.build(key)
    assert served["reading"]["stale"] and served["rows"][3] == [["last time", ""]]
    Busy.done.set(); Busy.ready.set()
    assert pages.build(key) == {"rows": [], "fresh": True}

    # the pane's client, while an engine answers but has no page yet: the same cache
    import system_diff.live as lv
    real = lv.client_progress
    lv.client_progress = lambda root: {"stage": "Reading the code", "done": 40, "total": 430, "seconds": 2}
    try:
        page = panel.waiting(repo, 80, "now", None)
    finally:
        lv.client_progress = real
    assert page["reading"]["stale"] and page["rows"][3] == [["last time", ""]]
