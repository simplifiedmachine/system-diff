"""The Agents board as Claude runs a plan: a subagent is tied to its planned agent as it starts (never drawn as an
unknown agent first), the board says what Claude did with the plan (launched how many, skipped which, started which
early), and it never loses the plan to a read that failed while the state file was replaced."""
import json
import time
from pathlib import Path

from system_diff import agent, agents, watch
from system_diff.sources import git_dir


def _plan(repo):
    data = agents._empty()
    data["plan"] = {"task": "t", "at": time.time() - 600, "order": "staged", "agents": [
        {"name": "backend-main", "area": "Backend main", "stage": 1, "waits_for": []},
        {"name": "backend-routers", "area": "Backend routers", "stage": 1, "waits_for": []},
        {"name": "shared-libraries", "area": "Shared libraries", "stage": 1, "waits_for": []},
        {"name": "docs-and-tools", "area": "Docs and tools", "stage": 2,
         "waits_for": ["backend-main", "backend-routers", "shared-libraries"]}]}
    agents._save(repo, data)


def _sub(tmp_path, aid, prompt, description=""):
    d = tmp_path / "sess" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    t = d / f"agent-{aid}.jsonl"
    t.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n", encoding="utf-8")
    (d / f"agent-{aid}.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": description}),
                                              encoding="utf-8")
    return tmp_path / "sess.jsonl"


def test_a_subagent_is_tied_to_its_planned_agent_as_it_starts(repo, tmp_path):
    _plan(repo)
    main = _sub(tmp_path, "aa5db6", 'You are the Shared libraries agent in a team working on: t\n'
                                    'First call the system-diff `claim` tool with agent "shared-libraries" and these blocks.')
    said = []
    agent.run_hook(json.dumps({"hook_event_name": "SubagentStart", "agent_id": "aa5db6", "agent_type": "general-purpose",
                               "transcript_path": str(main), "cwd": str(repo)}), said.append,
                   {"CLAUDE_PROJECT_DIR": str(repo)})
    assert agents.state(repo)["ids"]["aa5db6"] == "shared-libraries"
    wf = agents.workflow(repo)
    assert "sub:aa5db6" not in [a["name"] for a in wf["agents"]]       # never drawn as an unknown agent first
    assert next(a for a in wf["agents"] if a["name"] == "shared-libraries")["status"] == "running"
    # by the description alone, when it names one agent; an unplanned one stays ad hoc
    assert agents.match_planned(repo, "bb1", {"description": "Docs and tools: write the guide"}) == "docs-and-tools"
    assert agents.match_planned(repo, "cc2", {"description": "Survey files", "prompt": "list files"}) == ""


def test_the_board_says_what_claude_did_with_the_plan(repo):
    _plan(repo)
    data = agents.state(repo)
    data["ids"].update({"s1": "shared-libraries", "d1": "docs-and-tools"})
    agents._save(repo, data)
    now = time.time()
    with open(git_dir(repo) / agents.RUNS, "a", encoding="utf-8") as f:
        for r in ({"at": now - 400, "event": "start", "agent_id": "s1"},
                  {"at": now - 390, "event": "start", "agent_id": "d1"}):
            f.write(json.dumps(r) + "\n")
    wf = agents.workflow(repo)
    by = {a["name"]: a for a in wf["agents"]}
    assert wf["planned"] == 4 and wf["launched"] == 2
    assert sorted(wf["skipped"]) == ["backend-main", "backend-routers"]
    assert by["docs-and-tools"]["early"] and not by["shared-libraries"]["early"]
    page = watch.PAGE
    assert "not launched (Claude skipped it?)" in page and "Have Claude launch the missing ones" in page
    assert "waiting: agent not running" in page and "started early" in page


def test_plans_tell_claude_to_launch_every_agent_of_a_stage(repo, monkeypatch):
    from system_diff import compare
    c = compare.compare(repo, "HEAD", ".")
    got = agents.plan(repo, c, "change the api and the web app", 4, [], order="staged")
    if len(got["agents"]) > 1:
        assert "none skipped" in got["how"]


def test_a_failed_read_never_loses_the_plan(repo, monkeypatch):
    _plan(repo)
    assert agents.state(repo)["plan"]["agents"]
    real = Path.read_text

    def busy(self, *a, **k):   # Windows: the file is being replaced by another process
        if self.name == agents.STATE:
            raise PermissionError("in use")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", busy)
    monkeypatch.setattr(agents.time, "sleep", lambda s: None)
    assert len(agents.state(repo)["plan"]["agents"]) == 4               # the copy last read whole


def test_a_view_shown_stays_while_it_is_drawn_again():
    p = watch.PAGE
    assert "keep it on screen, say it is updating" in p and "· updating…" in p


def test_a_team_row_opens_any_recorded_subagents_log(repo, monkeypatch):
    """The team lists a subagent as agent-<the start of its id>, also one the board does not show (a plan came
    after it): a double click opens its log, named by its task, with its prompt as the brief."""
    from system_diff import agents
    monkeypatch.setattr(agents, "workflow", lambda root: {"agents": [], "at": 0})
    rows = [{"at": 1.0, "event": "start", "agent_id": "af47b005051c5ea85", "agent_type": "general-purpose", "transcript": ""},
            {"at": 2.0, "event": "launch", "agent_id": "af47b005051c5ea85", "agent_type": "general-purpose",
             "transcript": "", "description": "Survey files in api", "prompt": "List the files under api/"},
            {"at": 3.0, "event": "stop", "agent_id": "af47b005051c5ea85", "agent_type": "general-purpose", "transcript": ""}]
    monkeypatch.setattr(agents, "runs", lambda root, since=0: rows)
    monkeypatch.setattr(agents, "_load", lambda root: {"agents": {}, "ids": {}})
    monkeypatch.setattr(agents, "messages", lambda root: [])
    got = agents.agent_log(repo, "agent-af47b0")
    assert got["agent"]["title"] == "Survey files in api" and got["agent"]["status"] == "done"
    assert got["brief"] == "List the files under api/"


def test_a_tie_to_an_agent_of_an_earlier_plan_does_not_break_the_board(repo, monkeypatch):
    """A subagent tied to 'agent-a-guardrails' under an earlier plan, then a new plan without that name: the board
    (and the Studio's Agents tab) still reads ("the team could not be read: KeyError" before)."""
    from system_diff import agents
    monkeypatch.setattr(agents, "_load", lambda root: {"plan": {"at": 0, "task": "t", "agents": [{"name": "api", "blocks": [], "waits_for": []}]},
                                                       "ids": {"a1": "agent-a-guardrails"}, "agents": {}})
    monkeypatch.setattr(agents, "runs", lambda root, since=0: [{"at": 1.0, "event": "start", "agent_id": "a1", "agent_type": "general-purpose"}])
    wf = agents.workflow(repo)
    assert [a["name"] for a in wf["agents"]] == ["api"]


def test_claude_is_told_each_turn_to_hand_work_to_the_projects_saved_agents(repo):
    """pr-creation was saved, yet the PR was made in the main chat: each prompt now names the saved agents."""
    from system_diff import agent
    d = repo / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pr-creation.md").write_text("---\nname: pr-creation\ndescription: Use when a branch is ready to open an Azure "
                                      "DevOps pull request.\n---\nOpen the PR.\n", encoding="utf-8")
    said = agent.saved_agents_context(repo)
    assert "pr-creation: Use when a branch is ready" in said and "subagent_type" in said
    out = agent.on_prompt(repo, {"prompt": "sure", "session_id": "s"}, {})
    assert "pr-creation" in (out or {}).get("context", "")


def test_a_subagent_started_in_the_background_is_running_not_done(repo, monkeypatch):
    """The Task call of a background subagent comes back at once ("async_launched"): the board said "done in 0s"
    while it worked. Only its stop (or a launch that ran to the end) finishes it."""
    from system_diff import agents
    plan = {"at": 0, "task": "t", "agents": [{"name": "web", "blocks": [], "waits_for": []},
                                              {"name": "api", "blocks": [], "waits_for": []}]}
    monkeypatch.setattr(agents, "_load", lambda root: {"plan": plan, "ids": {"a1": "web", "a2": "api"}, "agents": {}, "claims": {}})
    monkeypatch.setattr(agents, "runs", lambda root, since=0: [
        {"at": 10.0, "event": "start", "agent_id": "a1"}, {"at": 10.1, "event": "launch", "agent_id": "a1", "status": "async_launched"},
        {"at": 11.0, "event": "start", "agent_id": "a2"}, {"at": 30.0, "event": "launch", "agent_id": "a2", "status": "completed"}])
    st = {a["name"]: a["status"] for a in agents.workflow(repo)["agents"]}
    assert st == {"web": "running", "api": "done"}
    t = agents.team_brief(repo)
    assert t["agents"] == 2 and t["running"] == 1 and t["unfinished"] == 1


def test_claude_is_told_the_running_plan_and_its_part(repo, monkeypatch):
    from system_diff import agent, agents
    monkeypatch.setattr(agents, "team_brief", lambda root: {"task": "fix the control center", "agents": 2, "running": 1,
                                                             "unfinished": 2, "stages": [[{"name": "web", "status": "running"}],
                                                                                        [{"name": "pr-creation", "status": "waiting"}]]})
    said = agent.team_context(repo)
    assert "stage 1: web running; stage 2: pr-creation waiting" in said and "You orchestrate it" in said


def test_the_page_and_the_studio_lead_to_the_board_while_a_team_works():
    from system_diff import app_page, watch
    assert '"agents" : "now"' in watch.PAGE and 'id="teamchip"' in watch.PAGE and 'e.data.studio !== "view"' in watch.PAGE
    assert "function paintTeam(" in app_page.PAGE and 'studio:"view", view:"agents"' in app_page.PAGE


def test_an_extension_that_does_not_apply_is_not_asked_for_steps(repo, monkeypatch):
    """resume_plan ran 3 min in a software repository: the board asked research-diff (installed, not applying) for
    its steps, and it read the repository to answer. Only extensions that apply are asked."""
    from system_diff import agents, extensions
    asked = []

    class Ext:
        NAME = "research"
        def workflow(self, ctx):
            asked.append(1)
            return {"steps": []}
    monkeypatch.setattr(extensions, "load", lambda: [Ext()])
    monkeypatch.setattr(extensions, "applies", lambda ext, root: False)
    agents._ext_steps(repo)
    assert asked == []
