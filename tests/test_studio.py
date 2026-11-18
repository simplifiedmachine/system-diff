"""The Studio (system-diff app): Claude Code sessions run from the page (against a fake client: no network), the
settings and models the dropdowns offer (secrets masked), the agents worth keeping, and the app's routes and guards."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from system_diff import agent, agentlib, app, profiles, sessions, watch


# ── a fake Claude Code: messages named as the SDK names them ────────────────────────────────────────────────────
class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


class AssistantMessage:
    def __init__(self, content, model="claude-fake"):
        self.content, self.model, self.parent_tool_use_id = content, model, None


class SystemMessage:
    def __init__(self, data):
        self.subtype, self.data = "init", data


class StreamEvent:
    def __init__(self, text):
        self.event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}


class ResultMessage:
    def __init__(self, reason="completed"):
        self.session_id, self.total_cost_usd, self.usage = "claude-123", 0.0123, {"input_tokens": 10, "output_tokens": 5}
        self.terminal_reason, self.is_error, self.num_turns, self.duration_ms = reason, False, 1, 42


class FakeClient:
    def __init__(self, session):
        self.session, self.prompts, self.stopped = session, [], False

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def query(self, text):
        self.prompts.append(text)
        self.stopped = False

    async def interrupt(self):
        self.stopped = True

    async def receive_response(self):
        text = self.prompts[-1]
        yield SystemMessage({"session_id": "claude-123", "model": "claude-fake", "tools": [1, 2], "mcp_servers": []})
        yield StreamEvent("Hel")
        yield StreamEvent("lo")
        yield AssistantMessage([TextBlock("Hello"), ToolUseBlock("t1", "Edit", {"file_path": "app/x.py",
                                                                                  "old_string": "a", "new_string": "b"})])
        if "edit" in text:
            decision = await self.session.ask("Edit", {"file_path": "app/x.py", "old_string": "a", "new_string": "b"})
            yield AssistantMessage([TextBlock("allowed" if decision["allow"] else "denied: " + decision["message"])])
        if "wait" in text:
            for _ in range(100):
                if self.stopped:
                    break
                await asyncio.sleep(0.02)
            yield ResultMessage("interrupted" if self.stopped else "completed")
            return
        yield ResultMessage()


def until(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def kinds(s, since=0):
    return [e["type"] for e in s.events_since(since)["events"]]


@pytest.fixture
def manager():
    m = sessions.Manager(client_factory=FakeClient)
    yield m
    m.stop()


def test_a_session_streams_answers_and_keeps_them(manager, repo):
    s = manager.start(repo, {"permission_mode": "default"})
    assert until(lambda: s.status == "idle")
    s.send("hi")
    assert until(lambda: "result" in kinds(s) and s.status == "idle")
    k = kinds(s)
    assert k.index("user") < k.index("delta") < k.index("text") < k.index("tool_use") < k.index("result")
    res = next(e for e in s.events_since(0)["events"] if e["type"] == "result")
    assert res["cost"] == 0.0123 and res["model"] == "claude-fake" and s.claude_id == "claude-123"
    tool = next(e for e in s.events_since(0)["events"] if e["type"] == "tool_use")
    assert tool["view"]["file"] == "app/x.py" and tool["view"]["diff"] == [{"old": "a", "new": "b"}]
    logged = [json.loads(x)["type"] for x in s._log.read_text(encoding="utf-8").splitlines()]
    assert "text" in logged and "delta" not in logged          # the log keeps the answer, not each streamed piece
    later = s.events_since(len(s.events_since(0)["events"]), wait=0.2)
    assert later["events"] == [] and s.title == "hi"


def test_a_tool_call_waits_for_the_person(manager, repo):
    s = manager.start(repo, {})
    assert until(lambda: s.status == "idle")
    s.send("please edit")
    assert until(lambda: s.status == "waiting" and s.pending)
    ask = next(e for e in s.events_since(0)["events"] if e["type"] == "permission")
    assert ask["tool"] == "Edit" and ask["view"]["file"] == "app/x.py"
    with pytest.raises(ValueError):
        s.answer("nope", True)
    s.answer(ask["request"], False, message="not that file")
    assert until(lambda: any(e["type"] == "text" and e["text"] == "denied: not that file" for e in s.events_since(0)["events"]))
    s.send("edit again")
    assert until(lambda: len(s.pending) == 1)
    s.answer(next(iter(s.pending)), True, always=True)
    assert until(lambda: any(e["type"] == "text" and e["text"] == "allowed" for e in s.events_since(0)["events"]))


def test_interrupt_and_close(manager, repo):
    s = manager.start(repo, {})
    assert until(lambda: s.status == "idle")
    s.send("wait for it")
    assert until(lambda: s.status == "working")
    s.interrupt()
    assert until(lambda: any(e["type"] == "result" and e["terminal_reason"] == "interrupted" for e in s.events_since(0)["events"]))
    s.close()
    assert until(lambda: s.status == "closed")
    with pytest.raises(ValueError):
        s.send("more")


def test_send_to_claude_from_the_page_reaches_the_open_session(manager, repo):
    s = manager.start(repo, {})
    assert until(lambda: s.status == "idle")
    watch.outbox_put(repo, "what does the API do?")
    assert manager.deliver_outbox([repo]) == 1
    assert until(lambda: any(e["type"] == "user" and "what does the API do?" in e["text"] for e in s.events_since(0)["events"]))


def test_the_sdk_options_a_session_runs_with(manager, repo, monkeypatch, tmp_path):
    pytest.importorskip("claude_agent_sdk")
    monkeypatch.setattr(sessions, "plugin_enabled", lambda *a, **k: False)
    s = sessions.Session(manager, repo, {"model": "sonnet", "permission_mode": "plan", "setting_sources": [],
                                         "effort": "high"},
                         resume="claude-9", fork=True)
    o = s.sdk_options()
    assert (o.model, o.permission_mode, o.resume, o.fork_session, o.cwd) == ("sonnet", "plan", "claude-9", True, str(repo))
    assert o.effort == "high"
    assert o.setting_sources == [] and "system-diff" in o.mcp_servers and o.can_use_tool is not None
    assert "mcp__system-diff__ui_view" in o.allowed_tools and "mcp__system-diff__ui_show" not in o.allowed_tools
    assert o.system_prompt["preset"] == "claude_code" and "ui_view" in o.system_prompt["append"]
    assert set(o.hooks) >= {"UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop"}


def test_the_plugins_hooks_stand_aside_in_an_app_session(repo):
    said = []
    assert agent.run_hook(json.dumps({"hook_event_name": "Stop", "cwd": str(repo)}), said.append,
                          {"SYSTEM_DIFF_APP_SESSION": "1"}) == 0 and said == []


# ── settings and models ──────────────────────────────────────────────────────────────────────────────────────────
def test_settings_profiles_never_show_a_secret(tmp_path, repo):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"model": "opus", "env": {"ANTHROPIC_API_KEY": "sk-secret",
                                                                              "CLAUDE_CODE_USE_BEDROCK": "1"},
                                                    "permissions": {"allow": ["Bash(ls)"]}}), encoding="utf-8")
    (home / "settings.bedrock.json").write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "claude-haiku-5-5",
                                                                    "AWS_SECRET_ACCESS_KEY": "x"}}), encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
    (home / "keybindings.json").write_text("{}", encoding="utf-8")
    got = profiles.choices(repo, home)
    names = [(p["scope"], p["name"]) for p in got["settings"]]
    assert names == [("", "Claude Code's own (user + project + local)"), ("user", "bedrock"), ("user", "user"),
                     ("local", "local")]
    assert "sk-secret" not in json.dumps(got) and "x" not in [v for p in got["settings"] for v in p["env"].values()]
    user = next(p for p in got["settings"] if p["name"] == "user")
    assert user["provider"] == "Bedrock" and user["model"] == "opus" and user["permissions"] == {"allow": 1}
    assert got["models"][0] == "" and set(got["models"][1:3]) == {"opus", "claude-haiku-5-5"}
    assert "bypassPermissions" in got["danger"]
    o = profiles.options_for(repo, {"settings_profile": "user:settings.bedrock.json"}, home)
    assert o["settings"].endswith("settings.bedrock.json") and o["model"] == "claude-haiku-5-5" and o["setting_sources"] == []
    assert profiles.options_for(repo, {}, home)["setting_sources"] == ["user", "project", "local"]
    with pytest.raises(ValueError):
        profiles.options_for(repo, {"permission_mode": "yolo"}, home)


# ── agents worth keeping ─────────────────────────────────────────────────────────────────────────────────────────
def test_roles_that_come_back_are_worth_keeping_and_saved(repo, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    agentlib.planned(repo, [{"name": "api", "area": "API", "brief": "You are the API agent."}])
    assert agentlib.worth_keeping(repo) == []
    agentlib.planned(repo, [{"name": "api", "area": "API", "brief": "You are the API agent."}])
    agent.run_hook(json.dumps({"hook_event_name": "SubagentStop", "cwd": str(repo), "agent_type": "code-reviewer"}),
                   lambda _l: None, {})
    keep = {r["name"]: r for r in agentlib.worth_keeping(repo)}
    assert keep["api"]["planned"] == 2 and keep["code-reviewer"]["why"] == "a standing role"
    got = agentlib.save_agent(repo, "api", "Use for the HTTP API.", "You are the API agent.", tools="Read, Edit")
    f = Path(got["path"])
    assert f == repo / ".claude" / "agents" / "api.md" and agentlib.MARKER in f.read_text(encoding="utf-8")
    assert "api" not in {r["name"] for r in agentlib.worth_keeping(repo)}
    lib = agentlib.saved(repo)
    assert lib[0]["name"] == "api" and lib[0]["planned"] == 2 and lib[0]["tools"] == "Read, Edit" and lib[0]["ours"]
    mine = repo / ".claude" / "agents" / "mine.md"
    mine.write_text("---\nname: mine\n---\nmy own\n", encoding="utf-8")
    with pytest.raises(ValueError):
        agentlib.save_agent(repo, "mine", "d", "p")
    with pytest.raises(ValueError):
        agentlib.forget(repo, "mine")
    assert agentlib.match(repo, "x", "API")["name"] == "api"
    agentlib.save_skill(repo, "run-tests", "How this repository runs its tests.", "python -m pytest -q")
    assert (repo / ".claude" / "skills" / "run-tests" / "SKILL.md").is_file()
    agentlib.forget(repo, "api")
    assert not f.exists()


# ── the app ──────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def studio(tmp_path, manager, repo):
    eng = {}
    t1 = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                      "ready": lambda url, server, w: eng.update(server=server, w=w)},
                          daemon=True)
    t1.start()
    assert until(lambda: "w" in eng, 15)
    up = {}
    folder = tmp_path / "state"
    t2 = threading.Thread(target=app.serve, kwargs={"port": 0, "folder": folder, "manager": manager,
                                                    "ready": lambda url, server, st: up.update(url=url, server=server,
                                                                                               studio=st)},
                          daemon=True)
    t2.start()
    assert until(lambda: "url" in up, 15)
    up["folder"] = folder
    yield up
    up["server"].shutdown()
    eng["server"].shutdown()
    t1.join(5)
    t2.join(5)


def call(url, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        return json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw.decode()


def test_the_app_guards_its_routes(studio, repo):
    url, tok = studio["url"], studio["studio"].token
    page = call(url)
    assert f'const TOKEN = "{tok}"' in page and "System diff Studio" in page
    for args in ((url + "api/projects", None, {}),                               # a read: neither token nor page
                 (url + "api/projects", {"path": str(repo)}, {"X-Token": tok}),  # a change: the token alone
                 (url + "api/projects", {"path": str(repo)}, {"Origin": url.rstrip("/")}),   # the page alone
                 (url + "api/projects", None, {"X-Token": tok, "Host": "evil.example:80"})):
        with pytest.raises(urllib.error.HTTPError) as no:
            call(*args)
        assert no.value.code == 403
    got = call(url + "api/projects", {"path": str(repo)}, {"X-Token": tok, "Origin": url.rstrip("/")})
    assert got["id"] == app.pid_of(str(repo))
    assert json.loads((studio["folder"] / "projects.json").read_text())["projects"] == [str(repo.resolve())]
    assert (studio["folder"] / "app.json").stat().st_mode & 0o077 == 0 or app.os.name == "nt"
    projects = call(url + "api/projects", headers={"Referer": url})["projects"]
    assert projects[0]["path"] == str(repo.resolve()) and projects[0]["port"]
    assert app.running(studio["folder"])["port"] == studio["server"].server_address[1]
    assert app.serve(port=0, folder=studio["folder"]) == 0      # one app per user: a second one just says where


def test_the_app_serves_the_projects_page_through_its_engine(studio, repo):
    url, tok = studio["url"], studio["studio"].token
    pid = call(url + "api/projects", {"path": str(repo)}, {"X-Token": tok, "Origin": url.rstrip("/")})["id"]
    page = call(url + f"p/{pid}/?studio=1")
    assert f'<base href="/p/{pid}/">' in page and "const STUDIO" in page
    with pytest.raises(urllib.error.HTTPError) as no:
        call(url + f"p/{pid}/web?view=now")                       # its data: only for the app's own page
    assert no.value.code == 403
    web = call(url + f"p/{pid}/web?view=now", headers={"Referer": url + f"p/{pid}/"})
    assert web["view"] == "now" and "diagram" in web
    sent = call(url + f"p/{pid}/ui/state", {"page": "s1", "view": "review"}, {"Origin": url.rstrip("/"), "X-Token": "x"})
    assert sent == {"ok": True}


def test_a_session_from_the_page(studio, repo):
    url, tok = studio["url"], studio["studio"].token
    w = {"X-Token": tok, "Origin": url.rstrip("/")}
    pid = call(url + "api/projects", {"path": str(repo)}, w)["id"]
    choices = call(url + f"api/choices?project={pid}", headers={"Referer": url})
    assert "default" in choices["permission_modes"] and choices["settings"][0]["id"] == "claude-code"
    s = call(url + "api/sessions", {"project": pid, "permission_mode": "acceptEdits", "model": "sonnet"}, w)
    assert s["options"]["permission_mode"] == "acceptEdits"
    assert until(lambda: studio["studio"].manager.get(s["id"]).status == "idle")
    call(url + f"api/sessions/{s['id']}/send", {"text": "please edit"}, w)
    got = {"next": 0, "events": []}
    assert until(lambda: any(e["type"] == "permission" for e in got["events"]) or got.update(
        call(url + f"api/sessions/{s['id']}/events?since=0", headers={"Referer": url})) or False, 15)
    ask = next(e for e in got["events"] if e["type"] == "permission")
    call(url + f"api/sessions/{s['id']}/permission", {"request": ask["request"], "allow": True}, w)
    assert until(lambda: any(e["type"] == "text" and e["text"] == "allowed"
                             for e in call(url + f"api/sessions/{s['id']}/events?since=0", headers={"Referer": url})["events"]))
    assert call(url + f"api/sessions?project={pid}", headers={"Referer": url})["sessions"][0]["id"] == s["id"]
    assert studio["studio"].default_for(repo.resolve())["permission_mode"] == "acceptEdits"
    t = call(url + f"api/sessions/{s['id']}/switch", {"project": pid, "permission_mode": "plan"}, w)
    assert t["id"] != s["id"] and studio["studio"].manager.get(t["id"]).resume == "claude-123"
    f = call(url + f"api/sessions/{s['id']}/fork", {}, w)
    forked = studio["studio"].manager.get(f["id"])
    assert forked.resume == "claude-123" and forked.fork and forked.options["permission_mode"] == "acceptEdits"
    e = call(url + "api/sessions", {"project": pid, "effort": "xhigh"}, w)
    assert e["options"]["effort"] == "xhigh"
    with pytest.raises(urllib.error.HTTPError) as bad:
        call(url + "api/sessions", {"project": pid, "effort": "eleven"}, w)
    assert bad.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as gone:
        call(url + "api/sessions/nope/send", {"text": "x"}, w)
    assert gone.value.code == 404


def test_one_system_diff_server_when_the_plugin_is_on(manager, repo, tmp_path):
    pytest.importorskip("claude_agent_sdk")
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"enabledPlugins": {"system-diff@system-diff": True}}), encoding="utf-8")
    assert sessions.plugin_enabled(repo, ["user"], home) and not sessions.plugin_enabled(repo, ["project"], home)
    assert not sessions.plugin_enabled(repo, [], home)


# ── extensions' own MCP servers in a session ─────────────────────────────────────────────────────────────────────
def _research_ext(applies=True):
    import types
    ext = types.ModuleType("research_fake.system_diff_ext")
    ext.NAME = "research"
    ext.MCP_SERVER = {"command": "research-diff", "args": ["mcp"]}
    ext.applies = lambda root: applies

    def mcp(server):
        server.tool(name="research_autoresearch_start")(lambda: {})
        from mcp.types import ToolAnnotations
        server.tool(name="research_evidence", annotations=ToolAnnotations.model_validate({"readOnlyHint": True}))(
            lambda claim="": {"claim": claim})
    ext.mcp = mcp
    return ext


def test_an_extensions_own_server_joins_a_session_once(monkeypatch, repo, tmp_path):
    pytest.importorskip("mcp")
    from system_diff import extensions
    monkeypatch.setattr(extensions, "_LOADED", [_research_ext()])
    sessions._READ_ONLY.clear()
    got = sessions.extension_servers(repo, set(), sd_ours=True)
    assert got["servers"]["research"] == {"type": "stdio", "command": "research-diff", "args": ["mcp"]}
    assert got["off"] == ["research"] and "mcp__research__research_evidence" in got["allowed"]
    assert "mcp__research__research_autoresearch_start" not in got["allowed"]
    assert sessions.extension_servers(repo, {"research-diff"}, True)["servers"] == {}   # claude mcp add research-diff
    assert sessions.extension_servers(repo, set(), sd_ours=False)["servers"] == {}      # the plugin's server has them
    monkeypatch.setattr(extensions, "_LOADED", [_research_ext(applies=False)])
    assert sessions.extension_servers(repo, set(), True)["servers"] == {}               # not a research repository
    home = tmp_path / "h"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"system-diff": {}},
                                                   "projects": {str(repo.resolve()): {"mcpServers": {"research": {}}}}}),
                                       encoding="utf-8")
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"db": {}}}), encoding="utf-8")
    assert sessions.configured_servers(repo, ["user", "project", "local"], home) == {"system-diff", "research", "db"}
    assert sessions.configured_servers(repo, ["project"], home) == {"db"}


def test_the_session_options_carry_the_extensions_server(monkeypatch, manager, repo):
    pytest.importorskip("claude_agent_sdk")
    from system_diff import extensions
    monkeypatch.setattr(extensions, "_LOADED", [_research_ext()])
    monkeypatch.setattr(sessions, "plugin_enabled", lambda *a, **k: False)
    monkeypatch.setattr(sessions, "configured_servers", lambda *a, **k: set())
    o = sessions.Session(manager, repo, {}).sdk_options()
    assert set(o.mcp_servers) == {"system-diff", "research"}
    assert o.mcp_servers["system-diff"]["env"] == {"SYSTEM_DIFF_EXT_TOOLS_OFF": "research"}
    assert "mcp__research__research_evidence" in o.allowed_tools


def test_system_diffs_server_leaves_out_tools_another_server_brings(monkeypatch):
    pytest.importorskip("mcp")
    import asyncio as aio
    from system_diff import extensions, mcp_server
    monkeypatch.setattr(extensions, "_LOADED", [_research_ext()])
    names = lambda: {t.name for t in aio.run(mcp_server.build().list_tools())}   # noqa: E731
    assert "research_evidence" in names()
    monkeypatch.setenv("SYSTEM_DIFF_EXT_TOOLS_OFF", "research")
    assert "research_evidence" not in names() and "ui_view" in names()
