"""Where starting any program costs seconds (endpoint security on a Windows laptop: 1-5 s for git or Python alike), the
plugin must still answer in time: run.sh starts one program, a hook makes no git call and starts at most one process
(the engine, when none runs), the engine records why it stops, and the MCP server answers whatever happens."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from system_diff import agent, cli, compare, doctor, live, watch
from system_diff.sources import git_dir

PLUGIN = Path(__file__).resolve().parent.parent / "mods" / "system-diff"
SPAWN_COST = 2.0


# ── a machine where every process start costs SPAWN_COST seconds ───────────────────────────────────────────────────

class Slow:
    """subprocess.run and subprocess.Popen made slow (SPAWN_COST each) and counted, by the thread that started them:
    the hook runs in the test's thread, the engine in its own."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.here = threading.get_ident()
        run, popen = subprocess.run, subprocess.Popen
        slow = self

        def slow_run(*a, **k):
            slow._note(a[0] if a else k.get("args"))
            return run(*a, **k)

        class SlowPopen(popen):
            def __init__(self, *a, **k):
                slow._note(a[0] if a else k.get("args"))
                super().__init__(*a, **k)
        monkeypatch.setattr(subprocess, "run", slow_run)
        monkeypatch.setattr(subprocess, "Popen", SlowPopen)

    def _note(self, argv) -> None:
        self.calls.append((threading.get_ident(), list(argv) if isinstance(argv, (list, tuple)) else [str(argv)]))
        time.sleep(SPAWN_COST)

    def mine(self):
        return [c for t, c in self.calls if t == self.here]


@pytest.fixture
def engine(repo):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)},
                         daemon=True)
    t.start()
    deadline = time.time() + 15
    while "w" not in up and time.time() < deadline:
        time.sleep(0.05)
    assert up["w"].engine.done.wait(60)
    yield up
    up["server"].shutdown()
    t.join(10)


def _hooks(repo):
    path = str(repo / "app" / "api" / "routes.py")
    return [
        ("SessionStart", {"source": "startup"}),
        ("UserPromptSubmit", {"prompt": "add a health endpoint"}),
        ("PostToolUse", {"tool_name": "Read", "tool_input": {"file_path": str(repo / "app" / "db" / "repo.py")}}),
        ("PostToolUse", {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}}),
        ("Stop", {"last_assistant_message": "done"}),
    ]


def _run(repo, event, fields, env):
    out = []
    began = time.monotonic()
    assert agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": event, "session_id": "s", **fields}),
                          out.append, env=env) == 0
    return time.monotonic() - began, out


def test_with_an_engine_each_hook_is_quick_and_runs_no_git(repo, engine, monkeypatch):
    env = {"CLAUDE_PROJECT_DIR": str(repo)}
    slow = Slow(monkeypatch)
    took = {}
    for event, fields in _hooks(repo):
        if fields.get("tool_name") == "Write":
            (repo / "app" / "api" / "health.py").write_text(
                'from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n@router.get("/health")\n'
                'def health():\n    return {}\n', encoding="utf-8")
            fields = {**fields, "tool_input": {"file_path": str(repo / "app" / "api" / "health.py"), "content": "x"}}
        seconds, _out = _run(repo, event, fields, env)
        took[f"{event} {fields.get('tool_name', '')}".strip()] = seconds
        assert seconds < 3.0, (event, seconds)
    assert slow.mine() == [], slow.mine()      # not one process from the hook: no git, no Python
    print("hook seconds with an engine, 2 s per process start:", {k: round(v, 2) for k, v in took.items()})
    runs = agent.hook_runs(repo)
    assert [r["event"] for r in runs][-5:] == ["SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUse", "Stop"]
    assert all(r["mode"].startswith("engine") for r in runs[-4:] if r["event"] != "SessionStart"), runs
    # the engine marked the turn itself (from memory, one update-ref in its own thread)
    deadline = time.time() + 20
    while time.time() < deadline and agent.turn_marked_at(repo) == 0:
        time.sleep(0.1)
    assert agent.turn_marked_at(repo) > 0 and compare.turn_sha(repo)


def test_without_an_engine_a_hook_starts_one_and_skips_the_work(repo, monkeypatch):
    monkeypatch.delenv("SYSTEM_DIFF_WEB_START", raising=False)
    started = []

    class Child:
        pid = os.getpid()

        def poll(self):
            return None

    here = threading.get_ident()
    real = subprocess.Popen

    def fake_popen(argv, **kw):
        if threading.get_ident() != here:   # another test's engine, still winding down: not this hook's
            return real(argv, **kw)
        started.append(list(argv))
        time.sleep(SPAWN_COST)
        return Child()
    slow = Slow(monkeypatch)
    monkeypatch.setattr(watch.subprocess, "Popen", fake_popen)
    env = {"CLAUDE_PROJECT_DIR": str(repo)}
    for event, fields in _hooks(repo):
        before = len(started) + len(slow.mine())
        seconds, _out = _run(repo, event, fields, env)
        assert seconds < 3.0, (event, seconds)
        assert len(started) + len(slow.mine()) - before <= 1, (event, started, slow.mine())
    assert slow.mine() == []                       # no git at all
    assert len(started) == 1 and started[0][1:4] == ["-m", "system_diff", "--repo"]   # one engine, started once
    assert compare.turn_sha(repo) is None          # the turn is the engine's to mark, when it comes up
    assert agent.hook_runs(repo)[-1]["mode"].startswith("engine started")


def test_an_engine_that_starts_later_marks_the_turn_it_missed(repo, monkeypatch):
    monkeypatch.setattr(agent, "start_web", lambda root: None)
    agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "go"}),
                   lambda s: None, env={"CLAUDE_PROJECT_DIR": str(repo)})
    assert compare.turn_sha(repo) is None
    prompt_at = [e for e in __import__("system_diff.activity").activity.events(repo) if e["event"] == "prompt"][-1]["at"]
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(server=server, w=w)},
                         daemon=True)
    t.start()
    try:
        deadline = time.time() + 30
        # the engine marks the turn, then notes which prompt it was for: wait for both (a slow runner can look between)
        while time.time() < deadline and not (compare.turn_sha(repo) and agent.turn_marked_at(repo) == prompt_at):
            time.sleep(0.1)
        assert compare.turn_sha(repo) and agent.turn_marked_at(repo) == prompt_at
    finally:
        deadline = time.time() + 10
        while "server" not in up and time.time() < deadline:
            time.sleep(0.05)
        up["server"].shutdown()
        t.join(10)


def test_what_the_engine_finds_after_the_hook_stopped_waiting_comes_with_the_next_hook(repo):
    agent.say_later(repo, "a1", {"message": "System diff: a new endpoint", "context": "from api: hello"})
    agent.say_later(repo, "a1", {"message": "", "context": ""})   # nothing: nothing kept
    got = agent.on_tool(repo, {"tool_name": "Bash", "agent_id": "a1"}, env={"CLAUDE_PROJECT_DIR": str(repo)})
    assert got["message"] == "System diff: a new endpoint" and "from api: hello" in got["context"]
    assert agent.take_said(repo, "a1") == []       # said once


# ── the engine says why it stopped ─────────────────────────────────────────────────────────────────────────────────

def test_an_engine_thread_that_keeps_failing_stops_the_server_cleanly_and_says_why(repo, monkeypatch):
    def broken(self):
        raise RuntimeError("the disk went away")
    monkeypatch.setattr(watch.Watcher, "loop", broken)
    monkeypatch.setattr(watch, "RESTARTS", 2)
    monkeypatch.setattr(watch, "FATAL_GRACE", 0.2)
    monkeypatch.setattr(watch.time, "sleep", lambda s: threading.Event().wait(min(s, 0.05)))
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2}, daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive()                                       # stopped, not half alive
    assert not (git_dir(repo) / "web.json").exists()              # so the next look starts a fresh engine
    text = (git_dir(repo) / "engine.log").read_text(encoding="utf-8")
    assert "ERROR in the watcher thread: RuntimeError: the disk went away" in text
    assert "Traceback" in text and "restarting the watcher thread" in text and "stopping after an error" in text
    assert live.last_error(repo).startswith("ERROR in the")
    lines = []
    doctor.log_report(repo, lines.append)
    assert any("engine's log" in x for x in lines) and any("the disk went away" in x for x in lines)


def test_the_web_page_says_why_the_engine_stopped_and_how_it_comes_back():
    assert "The watcher stopped: start system-diff watch again." not in watch.PAGE
    assert "last error: " in watch.PAGE and "starts it again at its next look" in watch.PAGE


def test_the_log_stays_small(repo, monkeypatch):
    monkeypatch.setattr(live, "LOG_MAX", 2000)
    for i in range(200):
        live.log(repo, f"line {i} " + "x" * 50)
    f = git_dir(repo) / "engine.log"
    assert f.stat().st_size < 4000 and (git_dir(repo) / "engine.log.1").exists()


def test_doctor_shows_how_long_the_hooks_took(repo):
    agent.note_hook(repo, "UserPromptSubmit", 0.42, "engine")
    lines = []
    doctor.hooks_report(repo, lines.append)
    assert lines[0].startswith("hooks: 1 recent runs") and "0.42s (engine)" in lines[0]
    assert "not on PATH" in doctor.launch_line() or "starts from" in doctor.launch_line()


# ── the MCP server starts and answers, whatever happens ────────────────────────────────────────────────────────────

def test_the_mcp_server_is_started_without_a_shell():
    spec = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["system-diff"]
    assert spec["command"] not in ("sh", "bash", "cmd", "powershell", "pwsh") and "/" not in spec["command"]
    assert spec["command"] == "system-diff" and spec["args"] == ["mcp"]


def test_an_mcp_server_that_cannot_start_still_answers_and_logs_why(repo, monkeypatch):
    from system_diff import mcp_server

    def broken():
        raise ImportError("No module named 'mcp'")
    monkeypatch.setattr(mcp_server, "build", broken)
    monkeypatch.chdir(repo)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    slow = Slow(monkeypatch)
    asks = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "system_diff_status"}}]
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(json.dumps(a) + "\n" for a in asks)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    began = time.monotonic()
    assert cli.main(["mcp"]) == 0
    assert time.monotonic() - began < 1.0 and slow.mine() == []     # no git before (or while) answering
    got = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [g["id"] for g in got] == [1, 2, 3]
    assert got[0]["result"]["serverInfo"]["name"] == "system-diff"
    assert got[1]["result"]["tools"][0]["name"] == "system_diff_status"
    assert got[2]["result"]["isError"] and "mcp" in got[2]["result"]["content"][0]["text"]
    assert "ERROR at startup" in (git_dir(repo) / "mcp.log").read_text(encoding="utf-8")


def test_the_mcp_tools_find_the_repository_without_git(repo, monkeypatch):
    from system_diff import mcp_server
    monkeypatch.chdir(repo / "app")
    mcp_server._ROOTS.clear()
    slow = Slow(monkeypatch)
    assert mcp_server._root() == repo.resolve() and slow.mine() == []


# ── run.sh: one program started, the runner kept ──────────────────────────────────────────────────────────────────

SH = shutil.which("dash") or shutil.which("sh")


def _fake(bin_dir: Path, name: str, body: str) -> None:
    f = bin_dir / name
    f.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    f.chmod(0o755)


@pytest.fixture
def launcher(tmp_path):
    """A clean world for run.sh: PATH holds only fake programs, each recording its start in starts.log."""
    bin_dir, data, project = tmp_path / "bin", tmp_path / "data", tmp_path / "project"
    for d in (bin_dir, data, project):
        d.mkdir()
    log = tmp_path / "starts.log"

    def call(*args, stdin="{}"):
        env = {"PATH": str(bin_dir), "HOME": str(tmp_path), "CLAUDE_PLUGIN_DATA": str(data),
               "CLAUDE_PROJECT_DIR": str(project), "LOG": str(log)}
        got = subprocess.run([SH, str(PLUGIN / "scripts" / "run.sh"), *args], input=stdin, capture_output=True,
                             text=True, env=env, cwd=project, timeout=30)
        starts = log.read_text().splitlines() if log.exists() else []
        if log.exists():
            log.unlink()
        return got, starts
    return {"bin": bin_dir, "data": data, "project": project, "call": call, "log": log}


RECORD = 'echo "${0##*/} $*" >> "$LOG"\n'
STDIN = 'while IFS= read -r l || [ -n "$l" ]; do echo "stdin $l" >> "$LOG"; done\n'


@pytest.mark.skipif(SH is None, reason="no sh")
def test_run_sh_starts_system_diff_on_path_and_nothing_else(launcher):
    _fake(launcher["bin"], "system-diff", RECORD + STDIN)
    _fake(launcher["bin"], "python3", RECORD)
    for _ in range(2):
        got, starts = launcher["call"]("hook", stdin='{"hook_event_name": "Stop"}')
        assert got.returncode == 0 and starts == ["system-diff hook", 'stdin {"hook_event_name": "Stop"}']
    assert (launcher["data"] / "runner").read_text().split() == ["exe", "system-diff"]


@pytest.mark.skipif(SH is None, reason="no sh")
def test_run_sh_asks_the_pythons_once_and_keeps_the_answer(launcher):
    _fake(launcher["bin"], "python", RECORD + 'case "$1" in -c) exit 1;; esac\n')   # no system_diff here
    _fake(launcher["bin"], "python3", RECORD + 'case "$1" in -c) exit 0;; esac\n' + STDIN)
    got, starts = launcher["call"]("hook", stdin="x")
    assert got.returncode == 0 and starts == ["python3 -c import system_diff", "python3 -m system_diff hook", "stdin x"]
    got, starts = launcher["call"]("hook", stdin="y")
    assert starts == ["python3 -m system_diff hook", "stdin y"]                 # no probe: the kept runner, at once
    # the kept runner fails to start (its system_diff uninstalled): forgotten, never run twice; found again next time
    _fake(launcher["bin"], "python3", RECORD + 'echo "No module named system_diff" >&2; exit 1\n')
    got, starts = launcher["call"]("hook")
    assert got.returncode == 0 and starts == ["python3 -m system_diff hook"]
    assert (launcher["data"] / "runner").read_text() == ""
    _fake(launcher["bin"], "system-diff", RECORD)
    got, starts = launcher["call"]("hook")
    assert starts == ["system-diff hook"]


@pytest.mark.skipif(SH is None, reason="no sh")
def test_run_sh_prefers_the_projects_venv_by_file_and_drops_one_without_system_diff(launcher):
    venv = launcher["project"] / ".venv" / "bin"
    venv.mkdir(parents=True)
    _fake(venv, "python", RECORD + 'echo "No module named system_diff" >&2; exit 1\n')
    _fake(launcher["bin"], "system-diff", RECORD)
    got, starts = launcher["call"]("hook")
    assert starts == ["python -m system_diff hook"]                   # no import probe: run, once
    got, starts = launcher["call"]("hook")
    assert starts == ["system-diff hook"]                             # that .venv is remembered as without it
    other = launcher["project"] / ".venv" / "bin" / "python"
    _fake(venv, "python", RECORD)
    (launcher["data"] / "no-venv").unlink()
    got, starts = launcher["call"]("mcp")
    assert starts == ["python -m system_diff mcp"] and other.exists()


@pytest.mark.skipif(SH is None, reason="no sh")
def test_run_sh_without_system_diff_lets_the_hook_go_and_does_not_ask_again(launcher):
    _fake(launcher["bin"], "python3", RECORD + "exit 1\n")
    got, starts = launcher["call"]("hook", stdin="a long hook input\n" * 100)
    assert got.returncode == 0 and starts == ["python3 -c import system_diff"]
    got, starts = launcher["call"]("hook")
    assert got.returncode == 0 and starts == []                       # no Python started to ask again
    got, starts = launcher["call"]("panel")
    assert got.returncode == 127 and "pip install" in got.stderr
