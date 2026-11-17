"""Several projects at once: each engine registers itself in a per-user folder, every engine's page shows one tab per
running project (GET /projects), and `system-diff hub` gives one address listing them all."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from system_diff import cli, hub, watch


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    folder = tmp_path / "engines"
    monkeypatch.setenv(hub.ENV, str(folder))
    return folder


def _repo(at: Path, files: dict) -> Path:
    at.mkdir(parents=True)
    for name, text in files.items():
        (at / name).write_text(text)
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=at, check=True)
    subprocess.run(g + ["add", "-A"], cwd=at, check=True)
    subprocess.run(g + ["commit", "-q", "-m", "first"], cwd=at, check=True)
    return at


def _start(root: Path):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": root, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)}, daemon=True)
    t.start()
    deadline = time.time() + 15
    while "url" not in up and time.time() < deadline:
        time.sleep(0.05)
    assert "url" in up
    return up, t


def _get(url: str, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read().decode()


def test_the_registry_folder_is_per_user_and_per_system():
    assert hub.engines_dir({"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, "win32") == \
        Path(r"C:\Users\me\AppData\Local") / "system-diff" / "engines"
    assert hub.engines_dir({}, "darwin") == Path.home() / "Library" / "Application Support" / "system-diff" / "engines"
    assert hub.engines_dir({"XDG_STATE_HOME": "/x/state"}, "linux") == Path("/x/state/system-diff/engines")
    assert hub.engines_dir({}, "linux") == Path.home() / ".local" / "state" / "system-diff" / "engines"
    assert hub.engines_dir({"PYTEST_CURRENT_TEST": "t"}, "linux") is None   # the suite never writes the person's own


def test_an_entry_is_written_read_and_removed_without_a_token(registry, tmp_path):
    root = _repo(tmp_path / "shop", {"a.py": "x = 1\n"})
    f = hub.register(root, "http://127.0.0.1:9/", 9, "ab" * 16, "1.2.3")
    assert f is not None and f.parent == registry and f.name == "ab" * 16 + ".json"
    e = json.loads(f.read_text(encoding="utf-8"))
    assert set(e) == {"root", "name", "branch", "url", "port", "pid", "id", "version", "started"}
    assert e["name"] == "shop" and e["branch"] == "main" and e["pid"] == os.getpid() and "token" not in f.read_text(encoding="utf-8")
    if os.name != "nt":
        assert (f.stat().st_mode & 0o077) == 0
    hub.unregister(f)
    assert not f.exists()
    assert hub.register(root, "u", 9, "not hex!", "1") is None   # an id is a file name: hex only


def test_stale_entries_are_dropped_by_whoever_reads(registry, tmp_path):
    root = _repo(tmp_path / "shop", {"a.py": "x = 1\n"})
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    gone = hub.register(root, "http://127.0.0.1:9/", 9, "aa" * 16, "1")
    gone.write_text(gone.read_text(encoding="utf-8").replace(f'"pid": {os.getpid()}', f'"pid": {dead.pid}'))

    class Foreign(BaseHTTPRequestHandler):   # something answering /whoami as another engine
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"app": "system-diff", "id": "ff" * 16, "pid": os.getpid()}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    other = ThreadingHTTPServer(("127.0.0.1", 0), Foreign)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        port = other.server_address[1]
        foreign = hub.register(root, f"http://127.0.0.1:{port}/", port, "bb" * 16, "1")
        junk = registry / "junk.json"
        junk.write_text("{not json")
        assert hub.projects() == []
        assert not gone.exists() and not foreign.exists() and not junk.exists()
    finally:
        other.shutdown()
        other.server_close()


ODD = "blog & co" if os.name == "nt" else "<b>blog&co"   # a name that is markup, where a folder may be called so


def test_two_engines_in_two_repositories_show_each_other_as_tabs(registry, tmp_path):
    a = _repo(tmp_path / "shop", {"app.py": "import os\nURL = os.environ['DATABASE_URL']\n"})
    b = _repo(tmp_path / ODD, {"main.py": "print('hi')\n"})
    subprocess.run(["git", "checkout", "-q", "-b", "feature/x"], cwd=b, check=True)
    (ua, ta), (ub, tb) = _start(a), _start(b)
    try:
        assert len(list(registry.glob("*.json"))) == 2
        for up, me, other in ((ua, "shop", ODD), (ub, ODD, "shop")):
            status, body = _get(up["url"] + "projects", {"X-Token": up["w"].token})
            got = json.loads(body)["projects"]
            assert status == 200 and [p["name"] for p in got] == sorted([me, other], key=str.lower)
            assert [p["name"] for p in got if p["current"]] == [me]
            assert {p["name"]: p["branch"] for p in got} == {"shop": "main", ODD: "feature/x"}
            assert "token" not in body and up["w"].token not in body
            assert {p["port"] for p in got} == {ua["server"].server_address[1], ub["server"].server_address[1]}
        # same origin (the page's own fetch) is let in; another site, or no proof at all, is not; no CORS
        port = ua["server"].server_address[1]
        status, _ = _get(ua["url"] + "projects", {"Referer": f"http://127.0.0.1:{port}/"})
        assert status == 200
        for headers in ({}, {"Origin": "https://evil.example"}, {"Host": "evil.example:%d" % port}):
            with pytest.raises(urllib.error.HTTPError) as e:
                _get(ua["url"] + "projects", headers)
            assert e.value.code == 403 and "Access-Control-Allow-Origin" not in e.value.headers
        # the dot: B's page looked at its latest model, then B's code changes
        for up in (ua, ub):
            assert up["w"].engine.done.wait(60)

        def b_changed(look: bool) -> bool:
            if look:
                _get(ub["url"] + "web?view=now")
            got = json.loads(_get(ua["url"] + "projects", {"X-Token": ua["w"].token})[1])["projects"]
            return [p["changed"] for p in got if not p["current"]][0]

        deadline = time.time() + 20
        while b_changed(look=True) and time.time() < deadline:
            time.sleep(0.3)
        assert not b_changed(look=False)
        (b / "main.py").write_text("import os\nprint(os.environ['SMTP_HOST'])\n")
        deadline = time.time() + 20
        while not b_changed(look=False) and time.time() < deadline:
            time.sleep(0.3)
        assert b_changed(look=False)
        # the list page, at every engine
        status, html = _get(ua["url"] + "projects/page")
        assert status == 200 and "function projectHref" in html and "/projects" in html
    finally:
        ub["server"].shutdown()
        tb.join(10)
    got = json.loads(_get(ua["url"] + "projects", {"X-Token": ua["w"].token})[1])["projects"]
    assert [p["name"] for p in got] == ["shop"] and len(list(registry.glob("*.json"))) == 1
    ua["server"].shutdown()
    ta.join(10)
    assert list(registry.glob("*.json")) == []


def test_the_page_has_a_tab_row_that_escapes_names_and_switches_with_brackets(tmp_path):
    root = _repo(tmp_path / "shop", {"a.py": "x = 1\n"})
    up, t = _start(root)
    try:
        html = _get(up["url"])[1]
        assert '<nav class="projects" id="projects"' in html and "function paintProjects" in html
        assert "function projectHref" in html and "__PROJECT_HREF__" not in html
        js = html[html.index("function paintProjects"):html.index("async function loadProjects")]
        assert "innerHTML" not in js and "el(\"span\", p.name" in js   # names go in as text, never as markup
        assert 'e.key === "["' in html and 'e.key === "]"' in html and "projectHref(p.port, ui.view)" in html
    finally:
        up["server"].shutdown()
        t.join(10)


def test_moved_says_how_far_the_model_moved_and_what_was_looked_at_to_the_token_only(tmp_path):
    root = _repo(tmp_path / "shop", {"a.py": "x = 1\n"})
    up, t = _start(root)
    try:
        assert set(json.loads(_get(up["url"] + "whoami")[1])) == {"app", "id", "pid"}
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(up["url"] + "moved", {"Referer": up["url"]})
        assert e.value.code == 403
        assert up["w"].engine.done.wait(60)
        _get(up["url"] + "web?view=now")
        got = json.loads(_get(up["url"] + "moved", {"X-Token": up["w"].token})[1])
        assert set(got) == {"id", "seq", "seen"} and got["seen"] > 0
    finally:
        up["server"].shutdown()
        t.join(10)


def test_system_diff_hub_prints_the_address_of_the_first_engine(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path / "shop", {"a.py": "x = 1\n"})
    up, t = _start(root)
    port = up["server"].server_address[1]
    try:
        monkeypatch.setattr(hub, "FIRST", port)
        monkeypatch.setattr(hub, "LAST", port)
        assert cli.main(["hub"]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0] == f"http://localhost:{port}/projects/page"
    finally:
        up["server"].shutdown()
        t.join(10)


def test_system_diff_hub_serves_the_list_itself_when_no_engine_runs(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "FIRST", 1)   # nothing listens there
    monkeypatch.setattr(hub, "LAST", 1)
    assert hub.find_hub() is None
    assert cli.main(["hub", "--no-serve"]) == 1
    up = {}
    th = threading.Thread(target=hub.serve_hub, args=(0, lambda url, server: up.update(url=url, server=server)),
                          daemon=True)
    th.start()
    deadline = time.time() + 10
    while "url" not in up and time.time() < deadline:
        time.sleep(0.05)
    try:
        url = up["url"]
        assert url.startswith("http://localhost:") and url.endswith("/projects/page")
        status, html = _get(url)
        assert status == 200 and "your projects" in html
        port = up["server"].server_address[1]
        status, body = _get(f"http://localhost:{port}/projects", {"Referer": url})
        assert status == 200 and json.loads(body) == {"projects": []}
        with pytest.raises(urllib.error.HTTPError):
            _get(f"http://localhost:{port}/projects")
    finally:
        up["server"].shutdown()
        th.join(5)
