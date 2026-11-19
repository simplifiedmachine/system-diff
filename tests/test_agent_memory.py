"""Each planned agent keeps a memory (its notes, what it read and edited, the messages it had, its last answer), so
when a stopped session is continued, resume_plan relaunches the unfinished ones from where they were."""
import json
import time

from system_diff import activity, agent, agents
from system_diff.sources import git_dir


def _plan(repo):
    data = agents._empty()
    data["plan"] = {"task": "t", "at": time.time() - 600, "after": 0, "order": "staged", "agents": [
        {"name": "api", "area": "API", "stage": 1, "waits_for": []},
        {"name": "web", "area": "Web", "stage": 1, "waits_for": []},
        {"name": "docs", "area": "Docs", "stage": 2, "waits_for": ["api", "web"]}]}
    for g in data["plan"]["agents"]:
        a = agents._agent(data, g["name"])
        a["brief"] = f'You are the {g["area"]} agent in a team working on: t\nFirst call the system-diff `claim` tool ' \
                     f'with agent "{g["name"]}" and these blocks.'
        a["topics"] = [f'agent:{g["name"]}']
    agents._save(repo, data)


def _runs(repo, *rows):
    with open(git_dir(repo) / agents.RUNS, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_notes_are_kept_and_capped(repo):
    _plan(repo)
    for i in range(agents.NOTES_KEEP + 5):
        agents.agent_note(repo, "api", done=f"step {i}", next_=f"step {i + 1}")
    notes = agents.memory(repo)["api"]["notes"]
    assert len(notes) == agents.NOTES_KEEP and notes[-1]["done"] == f"step {agents.NOTES_KEEP + 4}"
    try:
        agents.agent_note(repo, "nobody", done="x")
        assert False, "an agent outside the plan"
    except ValueError as exc:
        assert "api" in str(exc)
    agents.agent_note(repo, "api", notes="token ghp_" + "a" * 36)
    assert "ghp_" + "a" * 36 not in json.dumps(agents.memory(repo))     # secrets never kept


def test_resume_plan_says_who_is_done_interrupted_or_not_launched(repo):
    _plan(repo)
    data = agents.state(repo)
    data["ids"].update({"a1": "api", "w1": "web"})
    agents._save(repo, data)
    old = time.time() - 500
    _runs(repo, {"at": old, "event": "start", "agent_id": "a1"}, {"at": old + 60, "event": "stop", "agent_id": "a1"},
          {"at": old + 5, "event": "start", "agent_id": "w1"})                        # web never stopped
    got = agents.resume_plan(repo)
    st = {a["name"]: a["status"] for a in got["agents"]}
    assert st == {"api": "done", "web": "interrupted", "docs": "not launched"}
    assert got["unfinished"] == ["web", "docs"] and "web" in got["how"] and "docs" not in got["how"].split("prompt:")[1].split(".")[0]
    assert "resume_brief" not in next(a for a in got["agents"] if a["name"] == "api")
    # a launch whose Task call failed
    data["ids"]["w2"] = "web"
    agents._save(repo, data)
    _runs(repo, {"at": time.time() - 200, "event": "launch", "agent_id": "w2", "status": "failed"})
    assert next(a for a in agents.resume_plan(repo)["agents"] if a["name"] == "web")["status"] == "failed"


def test_a_resume_brief_carries_notes_edited_files_and_unread_messages(repo):
    _plan(repo)
    data = agents.state(repo)
    data["ids"]["w1"] = "web"
    agents._save(repo, data)
    _runs(repo, {"at": time.time() - 500, "event": "start", "agent_id": "w1"})
    with open(git_dir(repo) / activity.LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.time() - 400, "event": activity.EDIT, "path": "web/src/App.tsx", "agent": "w1"}) + "\n")
    agents.agent_note(repo, "web", done="the org tree component", next_="wire it to /api/org")
    agents.publish(repo, "agent:web", "the /api/org route is ready", sender="api")
    brief = agents.resume_plan(repo)["agents"][1]["resume_brief"]
    assert brief.startswith("You are the Web agent") and "You are resuming (attempt 2)" in brief
    assert "done: the org tree component" in brief and "next: wire it to /api/org" in brief
    assert "web/src/App.tsx" in brief and "the /api/org route is ready" in brief
    assert brief.rstrip().endswith("don't redo finished steps.")


def test_a_second_attempt_is_tied_to_the_same_agent(repo, tmp_path):
    _plan(repo)
    first = agents.resume_brief(repo, "web")   # the resume brief keeps the brief's `with agent "web"`
    assert agents.match_planned(repo, "w1", {"prompt": first}) == "web"
    assert agents.match_planned(repo, "w2", {"prompt": first}) == "web"
    _runs(repo, {"at": time.time() - 500, "event": "start", "agent_id": "w1"},
          {"at": time.time() - 100, "event": "start", "agent_id": "w2"})
    tries = agents.attempts(repo)["web"]
    assert [t["id"] for t in tries] == ["w1", "w2"]
    node = next(a for a in agents.workflow(repo)["agents"] if a["name"] == "web")
    assert node["attempts"] == 2


def test_a_finished_task_keeps_its_answer(repo):
    _plan(repo)
    data = agents.state(repo)
    data["ids"]["a1"] = "api"
    agents._save(repo, data)
    said = []
    agent.run_hook(json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Task", "cwd": str(repo),
                               "tool_input": {"description": "API", "prompt": 'with agent "api"'},
                               "tool_response": {"agentId": "a1", "status": "completed",
                                                 "content": [{"type": "text", "text": "Added /api/org."}]}}),
                   said.append, {"CLAUDE_PROJECT_DIR": str(repo)})
    assert agents.memory(repo)["api"]["answer"] == "Added /api/org."
    log = agents.agent_log(repo, "api")
    assert log["memory"]["answer"] == "Added /api/org." and len(log["memory"]["attempts"]) == 1


def test_briefs_ask_for_notes_and_the_tools_exist():
    import asyncio
    from system_diff import mcp_server
    names = {t.name for t in asyncio.run(mcp_server.build().list_tools())}
    assert {"agent_note", "resume_plan"} <= names
    assert "resume_plan" in mcp_server.INSTRUCTIONS and "agent_note" in mcp_server.INSTRUCTIONS
    import inspect
    assert inspect.getsource(agents.plan).count("agent_note") == 1
    assert inspect.getsource(agents.add_agent).count("agent_note") == 1


def test_notes_mask_tokens_but_keep_paths_and_hashes():
    pat = "t2rsz7ztfkzz6ntvabcdefghijklmnopqrstuvwxyz234567abcd"
    out = agents._masked(f"saved the pat {pat} and pushed 3f2d5445c0ffee3f2d5445c0ffee3f2d5445c0ff\n"
                         "edited web/src/flow/OrgAccess.tsx; key=supersecretvalue123")
    assert pat not in out and "supersecretvalue123" not in out
    assert "3f2d5445c0ffee3f2d5445c0ffee3f2d5445c0ff" in out and "web/src/flow/OrgAccess.tsx" in out


def test_the_studio_offers_to_resume_a_plan():
    import inspect
    from system_diff import app, app_page
    assert '"/api/plan"' in inspect.getsource(app) and "resume_plan" in inspect.getsource(app)
    p = app_page.PAGE
    assert 'id="resumeplan"' in p and "function checkPlan()" in p and "if (extra && extra.resume) checkPlan();" in p
    assert "call resume_plan and relaunch the unfinished agents with their resume briefs" in p


def test_a_closed_session_marks_its_running_agents_interrupted(repo):
    _plan(repo)
    data = agents.state(repo)
    data["ids"].update({"w1": "web", "a1": "api"})
    agents._save(repo, data)
    now = time.time()
    _runs(repo, {"at": now - 30, "event": "start", "agent_id": "w1", "transcript": "/x/sess-123/subagents/agent-w1.jsonl"},
          {"at": now - 30, "event": "start", "agent_id": "a1", "transcript": "/x/other/subagents/agent-a1.jsonl"})
    assert agents.resume_plan(repo)["agents"][1]["status"] == "running"       # it acted a moment ago
    assert agents.interrupted(repo, "sess-123") == ["web"]                     # only that session's
    got = {a["name"]: a["status"] for a in agents.resume_plan(repo)["agents"]}
    assert got["web"] == "interrupted" and got["api"] == "running"
    assert next(a for a in agents.workflow(repo)["agents"] if a["name"] == "web")["status"] == "interrupted"
    data["ids"]["w2"] = "web"
    agents._save(repo, data)
    _runs(repo, {"at": time.time(), "event": "start", "agent_id": "w2", "transcript": "/x/sess-456/subagents/agent-w2.jsonl"})
    assert next(a for a in agents.resume_plan(repo)["agents"] if a["name"] == "web")["status"] == "running"   # relaunched
