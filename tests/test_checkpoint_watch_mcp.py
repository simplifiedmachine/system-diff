"""Working with a person in the loop: the working tree as a snapshot, the checkpoint of what they have seen, the live
page, and the MCP tools an AI coding agent calls."""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from system_diff import compare as cmp
from system_diff import watch
from system_diff.sources import GitSource

HEALTH = 'from fastapi import APIRouter\nrouter = APIRouter()\n\n\n@router.get("/health")\ndef health():\n    return "ok"\n'
READY = 'from fastapi import APIRouter\nrouter = APIRouter()\n\n\n@router.get("/ready")\ndef ready():\n    return "ok"\n'


def _state(repo: Path) -> tuple:
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout  # noqa: E731
    return run("status", "--porcelain"), run("rev-parse", "HEAD"), (repo / ".git" / "index").read_bytes()


def test_a_snapshot_reads_untracked_files_and_changes_nothing_the_person_uses(repo, files):
    files(repo, {"app/api/health.py": HEALTH, "app/core/rules.py": "# edited, not staged\n"})
    before = _state(repo)
    sha = cmp.snapshot(repo)
    assert _state(repo) == before                                        # index, HEAD and status untouched
    src = GitSource(sha, repo)
    assert "app/api/health.py" in src.files and src.read("app/core/rules.py") == "# edited, not staged\n"
    assert cmp.snapshot(repo) and cmp.worktree_tree(repo) == cmp.worktree_tree(repo)


def test_the_checkpoint_keeps_only_what_came_after_it(repo, files):
    files(repo, {"app/api/health.py": HEALTH})
    first = cmp.compare(repo, "checkpoint", ".")
    assert first.base == "HEAD (no checkpoint yet)" and "GET /health" in first.diff["plan"]
    cmp.checkpoint(repo)
    assert cmp.compare(repo, "checkpoint", ".").diff["changes"] == []    # seen: nothing new
    files(repo, {"app/api/ready.py": READY})
    after = cmp.compare(repo, "checkpoint", ".").diff
    assert "GET /ready" in after["plan"] and "GET /health" not in after["plan"]
    assert after["from"] == "checkpoint" and after["to"] == "working tree"


def _get(url: str, **kw):
    req = urllib.request.Request(url, **kw)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode()


def test_the_live_page_redraws_and_marks_seen_only_with_its_token(repo, files):
    files(repo, {"app/api/health.py": HEALTH})
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server, w=w)},
                         daemon=True)
    t.start()
    deadline = time.time() + 15
    while "url" not in up and time.time() < deadline:
        time.sleep(0.05)
    url, w = up["url"], up["w"]
    try:
        status, page = _get(url)
        assert status == 200 and w.token in page and "Mark as seen" in page
        while time.time() < deadline and "GET /health" not in json.loads(_get(url + "state")[1])["plan"]:
            time.sleep(0.1)
        state = json.loads(_get(url + "state")[1])
        assert state["changes"] and state["svg"].startswith("<svg")
        with pytest.raises(urllib.error.HTTPError) as refused:
            _get(url + "seen", method="POST")                                # another page cannot press it
        assert refused.value.code == 403
        status, body = _get(url + "seen", method="POST", headers={"X-Token": w.token})
        assert status == 200 and json.loads(body)["changes"] == 0
    finally:
        up["server"].shutdown()


def test_the_mcp_tools_answer_with_the_plan_and_the_picture(repo, files, monkeypatch):
    pytest.importorskip("mcp")
    import asyncio

    from system_diff import mcp_server
    monkeypatch.chdir(repo)
    files(repo, {"app/api/health.py": HEALTH})
    names = {t.name for t in asyncio.run(mcp_server.build().list_tools())}
    assert names == {"system_map", "system_diff", "draw", "checkpoint"}
    d = mcp_server.tool_diff(repo)
    assert "GET /health" in d["plan"] and Path(d["picture"]).is_file() and ".git" in Path(d["picture"]).parts
    m = mcp_server.tool_map(repo)
    assert {p["id"] for p in m["parts"]} >= {"api", "core", "store", "web"} and m["outside_systems"] == ["PostgreSQL"]
    spec = mcp_server.Spec.model_validate({"picture": "stages", "stages": [{"title": "Build"}, {"title": "Ship"}],
                                           "loops": [{"from": 1, "to": 0, "label": "again"}]})
    assert Path(mcp_server.tool_draw(repo, spec, "release")["picture"]).name == "release.svg"
    broken = mcp_server.Spec.model_validate({"picture": "stages", "stages": [{"title": "Build"}],
                                             "loops": [{"from": 3, "to": 0}]})
    assert "could not be drawn" in mcp_server.tool_draw(repo, broken)["error"]


def test_the_hook_says_in_one_line_what_changed_and_never_fails(repo, files, tmp_path):
    from system_diff import agent
    said = []
    assert agent.run_hook(json.dumps({"cwd": str(repo)}), said.append) == 0 and said == []   # nothing changed yet
    files(repo, {"app/api/health.py": HEALTH})
    assert agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "Stop"}), said.append) == 0
    (message,) = said
    assert "GET /health" in json.loads(message)["systemMessage"]
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    assert agent.run_hook(json.dumps({"cwd": str(outside)}), said.append) == 0 and len(said) == 1
    assert agent.run_hook("not json", said.append) == 0


def test_setup_wires_the_hook_and_the_skill_and_undo_takes_them_back(repo, tmp_path, monkeypatch):
    from system_diff import agent
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))                    # no `claude`, no `system-diff` on PATH
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                                     "command": "echo mine"}]}]}}))
    said = []
    assert agent.setup_claude(repo, "user", out=said.append) == 0
    data = json.loads(settings.read_text())
    commands = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert data["theme"] == "dark" and commands[0] == "echo mine" and commands[1].endswith("-m system_diff hook")
    skill = home / ".claude" / "skills" / "system-diff" / "SKILL.md"
    assert skill.read_text().startswith("---\nname: system-diff\n")
    assert any("not on PATH" in s for s in said)
    agent.setup_claude(repo, "user", out=said.append)                     # twice changes nothing
    assert len(json.loads(settings.read_text())["hooks"]["Stop"]) == 2
    assert agent.setup_claude(repo, "user", undo=True, out=said.append) == 0
    assert json.loads(settings.read_text()) == {"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                                                 "command": "echo mine"}]}]}}
    assert not skill.exists()


def test_setup_registers_the_mcp_server_with_claude_code_in_the_same_scope(repo, tmp_path, monkeypatch):
    from system_diff import agent
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    calls = []

    def fake_claude(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[:2] == ["mcp", "get"] else 0, "", "")

    monkeypatch.setattr(agent, "_claude", fake_claude)
    monkeypatch.setattr(agent.shutil, "which", lambda name: f"/usr/bin/{name}")   # claude and system-diff on PATH
    agent.setup_claude(repo, "project", out=lambda s: None)
    assert ["mcp", "add", "--scope", "project", "--transport", "stdio", "system-diff", "--", "system-diff", "mcp"] in calls
    hook = json.loads((repo / ".claude" / "settings.json").read_text())["hooks"]["Stop"][0]["hooks"][0]
    assert hook == {"type": "command", "command": "system-diff hook", "timeout": 30}
    assert (repo / ".claude" / "skills" / "system-diff" / "SKILL.md").is_file()
