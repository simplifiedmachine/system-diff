"""Topics from what the extraction recorded (never by reading every file again), and the pane's web link: written
before the server reads the system, found on a later look, and never another program's that took the port."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from system_diff import pane, sources, watch
from system_diff.compare import compare
from system_diff.sources import git_dir

PY_TS = {
    "backend/app/__init__.py": "",
    "backend/app/main.py": "from fastapi import FastAPI\nfrom app.api.orders import router\n\napp = FastAPI()\n"
                           "app.include_router(router)\n",
    "backend/app/api/__init__.py": "",
    "backend/app/api/orders.py": "from fastapi import APIRouter, Depends\nfrom sqlalchemy.orm import Session\n"
                                 "from pydantic import BaseModel\nfrom app.db.models import Order\n\n"
                                 "router = APIRouter()\n\n\n@router.get(\"/orders\")\ndef list_orders():\n"
                                 "    return []\n",
    "backend/app/db/__init__.py": "",
    "backend/app/db/models.py": "from sqlalchemy import Column, Integer\nfrom sqlalchemy.orm import declarative_base\n"
                                "from concurrent.futures import ThreadPoolExecutor\n\nBase = declarative_base()\n\n\n"
                                "class Order(Base):\n    __tablename__ = \"orders\"\n"
                                "    id = Column(Integer, primary_key=True)\n",
    "frontend/package.json": "{\"dependencies\": {\"react\": \"18.3.1\", \"@tanstack/react-query\": \"5.0.0\"}}\n",
    "frontend/tsconfig.json": "{\"compilerOptions\": {\"baseUrl\": \".\", \"paths\": {\"@/*\": [\"./src/*\"]}}}\n",
    "frontend/src/App.tsx": "import React from \"react\";\nimport { useQuery } from \"@tanstack/react-query\";\n"
                            "import { Button } from \"@/components/Button\";\n\nexport function App() {\n"
                            "  useQuery({ queryKey: [\"orders\"], queryFn: () => fetch(\"/orders\") });\n"
                            "  return <Button />;\n}\n",
    "frontend/src/components/Button.tsx": "import React from \"react\";\n\nexport const Button = () => <button />;\n",
}
WANT = {"fastapi", "sqlalchemy", "pydantic", "react", "@tanstack/react-query"}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=root, check=True,
                   capture_output=True)


def _project(root: Path, files: dict, newline: str = "\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.replace("\n", newline).encode())
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _topics(root: Path) -> set:
    page = pane.view_topics(root, 100)
    assert page["view"] == "topics"
    return {b["id"][2:] for b in page["blocks"] if b["id"].startswith("t:")}


# ── Topics ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_topics_of_a_python_and_typescript_system(tmp_path, newline):
    root = _project(tmp_path / "shop", PY_TS, newline)
    found = _topics(root)
    assert WANT <= found
    assert "concurrent.futures" in found   # a dotted topic, from the package the blueprint records
    web = pane.web_view(root, "topics")
    assert WANT <= {chip["key"] for s in web["sections"] for chip in s["items"]}


@pytest.mark.parametrize("marked", [False, True], ids=["no-checkpoint-yet", "checkpoint"])
def test_topics_of_files_not_committed_yet(tmp_path, marked):
    root = _project(tmp_path / "shop", {k: v for k, v in PY_TS.items() if k.startswith("backend/")})
    if marked:
        from system_diff.compare import checkpoint
        checkpoint(root)
    for name, text in PY_TS.items():
        if name.startswith("frontend/"):
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    assert WANT <= _topics(root)


def test_topics_come_from_the_blueprint_without_reading_the_code_again(tmp_path, monkeypatch):
    root = _project(tmp_path / "shop", PY_TS)
    compare(root, "checkpoint", ".")          # the extraction, as the diagram's first draw does it
    read = []
    real = sources.GitSource.read_many

    def counting(self, paths):
        paths = list(paths)
        read.extend(paths)
        return real(self, paths)
    monkeypatch.setattr(sources.GitSource, "read_many", counting)
    assert WANT <= _topics(root)
    pane.web_view(root, "topics")
    assert not [p for p in read if p.endswith((".py", ".ts", ".tsx"))]


def test_topics_name_the_parts_the_diagram_draws(tmp_path):
    root = _project(tmp_path / "shop", PY_TS)
    c = compare(root, "checkpoint", ".")
    techs = pane.stack(c.after)
    assert set(techs["react"]) <= set(c.after["components"])
    assert set(techs["fastapi"]) <= set(c.after["components"]) and techs["fastapi"]
    assert pane.stack({"components": {"a": {}}, "stack": {"a": {"json": 3, "re": 1}}}) == {}   # the basics: none
    assert pane.stack({"components": {}}) == {}                                                # nothing recorded


def test_topics_show_under_columns_the_config_renamed(tmp_path):
    files = dict(PY_TS)
    files["system-diff.toml"] = '[picture]\ncolumns = [["web", "Web"], ["api", "API"], ["store", "Stored"]]\n'
    root = _project(tmp_path / "shop", files)
    assert WANT <= _topics(root)


# ── the web link ──────────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_web_json_is_written_before_the_system_is_read(repo, monkeypatch):
    release, reading = threading.Event(), threading.Event()

    def slow(*a, **k):
        reading.set()
        release.wait(30)
        raise ValueError("stopped by the test")
    monkeypatch.setattr(watch, "compare_worktree", slow)
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server)},
                         daemon=True)
    began = time.time()
    t.start()
    f = git_dir(repo) / "web.json"
    try:
        while not f.exists() and time.time() - began < 5:
            time.sleep(0.02)
        assert f.exists() and time.time() - began < 5
        info = json.loads(f.read_text())
        assert len(info["id"]) == 32 and info["pid"] == os.getpid()
        assert reading.wait(5)                                 # the read has begun, and does not end
        assert watch.web_url(repo) == f"http://localhost:{info['port']}/"   # found while it reads
        assert pane.web_url(repo) == f"http://localhost:{info['port']}/"
    finally:
        release.set()
        while "server" not in up and time.time() - began < 10:
            time.sleep(0.02)
        up["server"].shutdown()
        t.join(5)
    assert not f.exists()


def test_whoami_says_only_who_it_is(repo):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)},
                         daemon=True)
    t.start()
    deadline = time.time() + 15
    while "url" not in up and time.time() < deadline:
        time.sleep(0.05)
    try:
        import urllib.request
        with urllib.request.urlopen(up["url"] + "whoami", timeout=10) as r:
            info = json.loads(r.read().decode())
        assert set(info) == {"app", "id", "pid"} and info["id"] == up["w"].ident
        assert up["w"].token not in json.dumps(info)          # not the token /seen and /send need
    finally:
        up["server"].shutdown()
        t.join(5)


def _foreign(handler) -> tuple:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class _Impostor(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"app": "system-diff", "id": "someone else's", "pid": 1}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.mark.parametrize("handler", [_Quiet, _Impostor], ids=["plain-http-server", "another-id"])
def test_web_url_refuses_another_program_on_the_recorded_port(repo, monkeypatch, handler):
    server, port = _foreign(handler)
    try:
        f = git_dir(repo) / "web.json"
        f.write_text(json.dumps({"url": f"http://127.0.0.1:{port}/", "port": port, "pid": os.getpid(),
                                 "id": "0" * 32}))
        assert watch.web_url(repo) is None and not f.exists()
        assert pane.web_url(repo) == ""
    finally:
        server.shutdown()
        server.server_close()


def test_web_url_on_windows_still_refuses_another_program(repo, monkeypatch):
    server, port = _foreign(_Quiet)
    monkeypatch.setattr(watch, "_alive", lambda pid: True)     # Windows: no process check, only /whoami
    try:
        f = git_dir(repo) / "web.json"
        f.write_text(json.dumps({"url": f"http://127.0.0.1:{port}/", "port": port, "pid": 999999, "id": "x" * 32}))
        assert watch.web_url(repo) is None and not f.exists()
    finally:
        server.shutdown()
        server.server_close()


class _Child:
    pid = 4242

    def poll(self):
        return None


def _fake_start(monkeypatch) -> list:
    """Starts of a watcher, recorded and not run; every other process (git) runs."""
    started, real = [], subprocess.Popen

    def popen(argv, *a, **kw):
        if isinstance(argv, list) and "watch" in argv:
            started.append(argv)
            return _Child()
        return real(argv, *a, **kw)
    monkeypatch.setattr(watch.subprocess, "Popen", popen)
    return started


def test_one_start_at_a_time_and_a_busy_server_is_left_be(repo, monkeypatch):
    started = _fake_start(monkeypatch)
    monkeypatch.setattr(watch, "_alive", lambda pid: True)
    assert watch.start_detached(repo, port=_free_port(), wait=0.2) is None
    assert len(started) == 1
    marker = git_dir(repo) / watch.STARTING
    assert json.loads(marker.read_text())["pid"] == 4242
    assert watch.start_detached(repo, port=_free_port(), wait=0.2) is None   # one is starting: no second
    assert len(started) == 1
    info = json.loads(marker.read_text())
    marker.write_text(json.dumps({**info, "at": time.time() - watch.STARTING_FOR - 1}))   # that start never came up
    assert watch.start_detached(repo, port=_free_port(), wait=0.2) is None
    assert len(started) == 2
    marker.unlink()
    monkeypatch.setattr(watch, "_whoami", lambda port, ident, timeout=1.0: None)   # a server too busy to answer
    (git_dir(repo) / "web.json").write_text(json.dumps({"url": "u", "port": 1, "pid": 1, "id": "y"}))
    assert watch.start_detached(repo, port=_free_port(), wait=0.2) is None
    assert len(started) == 2 and (git_dir(repo) / "web.json").exists()


def test_a_start_skips_the_port_another_program_holds(repo, monkeypatch):
    server, port = _foreign(_Quiet)
    started = _fake_start(monkeypatch)
    try:
        (git_dir(repo) / "web.json").write_text(json.dumps({"url": "u", "port": port, "pid": os.getpid(),
                                                            "id": "z" * 32}))
        watch.start_detached(repo, port=port, wait=0.1)
        assert len(started) == 1 and started[0][-1] != str(port)   # a new one, on the next free port
    finally:
        server.shutdown()
        server.server_close()


def test_the_server_says_its_start_is_done(repo):
    marker = git_dir(repo) / watch.STARTING
    marker.write_text(json.dumps({"pid": 1, "port": 5555, "at": time.time()}))
    watch._write_web(repo, "http://127.0.0.1:5555/", 5555, "i" * 32)
    assert not marker.exists()
