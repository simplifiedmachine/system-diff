"""The team's workflow: stages (in parallel within one, one stage after another), who waits for whom, each agent's
and extension step's status from the hooks, its session log; agents added from the chat; workflows saved and run
again; extensions' steps, waits, actions, agent kinds and templates."""
from __future__ import annotations

import json
import socket
import threading
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from system_diff import activity, agent, agents, compare, extensions, watch, workflows


def test_stages_and_circles():
    w = {"a": set(), "b": {"a"}, "c": {"b"}, "d": {"e"}, "e": {"d"}, "f": {"d", "c"}}
    assert agents.stages_of(w) == {"a": 1, "b": 2, "c": 3, "d": 1, "e": 1, "f": 4}
    assert agents.untangle(w)["d"] == set() and agents.untangle(w)["f"] == {"c", "d"}


def _plan(repo, **kw):
    c = compare.compare(repo, "HEAD", ".")
    return agents.plan(repo, c, "add order notes end to end", 4, **kw)


def test_a_team_runs_in_parallel_or_in_stages(repo):
    flat = _plan(repo)
    assert {g["stage"] for g in flat["agents"]} == {1} and "in parallel" in flat["how"]
    staged = _plan(repo, order="staged")
    by = {g["name"]: g for g in staged["agents"]}
    assert max(g["stage"] for g in staged["agents"]) > 1 and "stage 1" in staged["how"]
    for g in staged["agents"]:
        for w in g["waits_for"]:
            assert by[w]["stage"] < g["stage"]
            assert f"You start after" in g["brief"]
    names = sorted(by)
    told = _plan(repo, after={names[0]: [names[1]]})
    first = next(g for g in told["agents"] if g["name"] == names[0])
    assert first["waits_for"] == [names[1]] and first["stage"] == 2


def test_the_workflow_follows_the_hooks(repo, tmp_path):
    got = _plan(repo, order="staged")
    names = [g["name"] for g in got["agents"]]
    first = next(g["name"] for g in got["agents"] if g["stage"] == 1)
    later = next(g for g in got["agents"] if g["waits_for"])
    wf = {a["name"]: a for a in agents.workflow(repo)["agents"]}
    assert wf[first]["status"] == "queued" and wf[later["name"]]["status"] == "waiting"
    main = tmp_path / "s1.jsonl"
    payload = {"hook_event_name": "SubagentStart", "cwd": str(repo), "agent_id": "abc", "agent_type": "general-purpose",
               "transcript_path": str(main)}
    agent.run_hook(json.dumps(payload), lambda _l: None, {})
    agents.tie(repo, "abc", first)
    activity.record(repo, {"event": activity.EDIT, "path": "app/core/x.py", "tool": "Edit", "session": "s", "agent": "abc"})
    wf = {a["name"]: a for a in agents.workflow(repo)["agents"]}
    assert wf[first]["status"] == "running" and wf[first]["edits"] == 1 and wf[first]["last"] == "edited app/core/x.py"
    sub = Path(agents.subagent_transcript(str(main), "abc"))
    assert sub == tmp_path / "s1" / "subagents" / "agent-abc.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(json.dumps({"type": "user", "timestamp": "2026-11-18T20:00:00Z",
                               "message": {"content": "You are the agent. token=sk-ant-api03-SECRETSECRETSECRET"}}) + "\n"
                   + json.dumps({"type": "assistant", "timestamp": "2026-11-18T20:00:05Z",
                                 "message": {"content": [{"type": "tool_use", "name": "Edit",
                                                          "input": {"file_path": "app/core/x.py"}}]}}) + "\n",
                   encoding="utf-8")
    log = agents.agent_log(repo, first)
    assert [e["kind"] for e in log["transcript"]] == ["prompt", "tool"] and "SECRETSECRET" not in json.dumps(log)
    assert log["files"][0]["path"] == "app/core/x.py"
    agent.run_hook(json.dumps({**payload, "hook_event_name": "SubagentStop", "agent_transcript_path": str(sub)}),
                   lambda _l: None, {})
    wf = {a["name"]: a for a in agents.workflow(repo)["agents"]}
    assert wf[first]["status"] == "done" and wf[later["name"]]["status"] in ("queued", "waiting")
    assert set(names) == set(wf)


def test_an_agent_added_from_the_chat(repo, monkeypatch):
    got = _plan(repo)
    anchor = got["agents"][0]["name"]
    ext = types.SimpleNamespace(NAME="research", VIEWS=[], WORKFLOW_KINDS=[
        {"kind": "contradiction-reviewer", "title": "Contradiction reviewer",
         "brief": "Check the paper for statements that contradict each other, for: {task}"}])
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    assert "contradiction-reviewer" in agents.agent_kinds(repo)
    added = agents.add_agent(repo, "reviewer", waits_for=[anchor, "nobody"], kind="contradiction-reviewer")
    assert added["waits_for"] == [anchor] and added["unknown_waits"] == ["nobody"] and added["stage"] == 2
    assert "contradict each other, for: add order notes" in added["brief"] and "once" in added["how"]
    wf = {a["name"]: a for a in agents.workflow(repo)["agents"]}
    assert wf["reviewer"]["added"] and wf["reviewer"]["status"] == "waiting"
    with pytest.raises(ValueError):
        agents.add_agent(repo, "reviewer")
    with pytest.raises(ValueError):
        agents.add_agent(repo, "x", kind="no-such-kind")


def test_extensions_add_steps_waits_and_actions(repo, monkeypatch):
    got = _plan(repo)
    core = got["agents"][0]["name"]
    done = []
    ext = types.SimpleNamespace(NAME="research", VIEWS=[])
    ext.workflow = lambda ctx: {"steps": [{"id": "e1", "title": "Run E1", "kind": "run", "state": "running",
                                           "actions": ["pause", "stop", "explode"], "log": "epoch 1\nepoch 2"},
                                          {"id": "write", "title": "Write section 4", "kind": "agent", "state": "waiting"}],
                                "waits": [{"from": "e1", "to": "write", "why": "needs E1's numbers"},
                                          {"from": "e1", "to": core}]}
    ext.workflow_action = lambda ctx, sid, action: done.append((sid, action)) or {"ok": True, "said": "paused"}
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    wf = agents.workflow(repo)
    steps = {x["id"]: x for x in wf["steps"]}
    assert steps["e1"]["actions"] == ["pause", "stop"] and steps["write"]["waits_for"] == ["e1"]
    assert steps["write"]["status"] == "waiting" and steps["write"]["stage"] > steps["e1"]["stage"]
    me = next(a for a in wf["agents"] if a["name"] == core)
    assert "e1" in me["waits_for"] and me["status"] == "waiting"
    assert [e["text"] for e in agents.step_log(repo, "e1")["transcript"]] == ["epoch 1", "epoch 2"]
    assert agents.step_action(repo, "e1", "pause")["ok"] and done == [("e1", "pause")]
    with pytest.raises(ValueError):
        agents.step_action(repo, "e1", "retry")       # not offered
    with pytest.raises(KeyError):
        agents.step_action(repo, "nope", "pause")


def test_workflows_saved_by_kind_and_run_again(repo, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    _plan(repo, order="staged")
    with pytest.raises(ValueError):
        workflows.save(repo, "Bad Name!", scope="nowhere!")
    got = workflows.save(repo, "feature-team", "API, core and store, in order")
    assert got["scope"] == "project" and Path(got["path"]).is_file()
    workflows.save(repo, "paper-review", "for papers", scope="research")
    workflows.save(repo, "everyday", "anywhere", scope="everywhere")
    names = {w["name"]: w for w in workflows.listing(repo)}
    assert "feature-team" in names and "everyday" in names and "paper-review" not in names   # a software project
    ran = workflows.run(repo, compare.compare(repo, "HEAD", "."), "feature-team", "add order tags")
    assert ran["workflow"] == "feature-team" and ran["agents"] and agents.workflow(repo)["task"] == "add order tags"
    s = workflows.summary(names["feature-team"])
    assert s["stages"] > 1 and s["agents"]
    mine = workflows.folder(repo, "project") / "mine.json"
    mine.write_text(json.dumps({"name": "mine", "agents": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        workflows.save(repo, "mine")
    with pytest.raises(ValueError):
        workflows.forget(repo, "mine")
    assert workflows.forget(repo, "feature-team")["forgot"] == "feature-team"


def test_extension_templates_where_they_apply(repo, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    ext = types.SimpleNamespace(NAME="research", VIEWS=[], applies=lambda root: False, WORKFLOW_TEMPLATES=[
        {"name": "experiments-then-paper", "description": "run, analyse, write, check",
         "agents": [{"name": "writer", "area": "paper"}], "steps": [{"id": "run", "title": "Run the experiments"}],
         "waits": [{"from": "run", "to": "writer"}]}])
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    assert "experiments-then-paper" not in {w["name"] for w in workflows.listing(repo)}
    ext.applies = lambda root: True
    w = workflows.find(repo, "experiments-then-paper")
    assert w["by"] == "research" and w["agents"][0]["waits_for"] == ["run"]


def test_the_page_serves_logs_and_guards_actions(repo, monkeypatch):
    _plan(repo)
    ext = types.SimpleNamespace(NAME="research", VIEWS=[])
    ext.workflow = lambda ctx: [{"id": "e1", "title": "E1", "state": "running", "actions": ["stop"]}]
    ext.workflow_action = lambda ctx, sid, action: {"ok": True}
    monkeypatch.setattr(extensions, "_LOADED", [ext])
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)}, daemon=True)
    t.start()
    deadline = time.time() + 15
    while "w" not in up and time.time() < deadline:
        time.sleep(0.05)
    url, tok = up["url"], up["w"].token
    try:
        name = agents.workflow(repo)["agents"][0]["name"]
        got = json.loads(urllib.request.urlopen(urllib.request.Request(url + "agents/log?name=" + name,
                                                                       headers={"X-Token": tok}), timeout=30).read())
        assert got["agent"]["name"] == name
        with pytest.raises(urllib.error.HTTPError) as no:
            urllib.request.urlopen(url + "agents/log?name=" + name, timeout=30)
        assert no.value.code == 403
        act = lambda h: urllib.request.urlopen(urllib.request.Request(   # noqa: E731
            url + "agents/action", data=json.dumps({"step": "e1", "action": "stop"}).encode(), method="POST",
            headers={"Content-Type": "application/json", **h}), timeout=30)
        with pytest.raises(urllib.error.HTTPError) as no:
            act({"X-Token": tok})                                    # the token alone: not the page
        assert no.value.code == 403
        assert json.loads(act({"X-Token": tok, "Origin": url.rstrip("/")}).read())["ok"]
    finally:
        up["server"].shutdown()
        t.join(5)
    page = watch.page_html()
    assert "function workflowCard(s)" in page and 'id="agentlog"' in page and "STUDIO ? me.actions" in page


def test_a_taken_port_moves_the_engine_to_the_next(repo):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        up = {}
        t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": port, "interval": 0.2,
                                                         "ready": lambda url, server, w: up.update(server=server)},
                             daemon=True)
        t.start()
        deadline = time.time() + 15
        while "server" not in up and time.time() < deadline:
            time.sleep(0.05)
        try:
            assert up and up["server"].server_address[1] != port
        finally:
            if up:
                up["server"].shutdown()
            t.join(5)


def test_the_hooks_name_subagent_start():
    hooks = json.loads(Path(__file__).resolve().parents[1].joinpath("mods/system-diff/hooks/hooks.json").read_text())
    assert "SubagentStart" in hooks["hooks"] and any(h[0] == "SubagentStart" for h in agent.HOOKS)
