"""The team, readable: a subagent by what it does (its Task description), its activity counted (not a row of marks),
the subagents Claude launches on its own drawn on the workflow board by their times, and a role that keeps coming
back offered to keep."""
import json
import time

from system_diff import activity, agent, agentlib, agents, pane
from system_diff.sources import git_dir


def _sub(tmp_path, aid, description, prompt="Look at every file under api/ and list what each one does, briefly.",
         kind="general-purpose"):
    d = tmp_path / "sess" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    t = d / f"agent-{aid}.jsonl"
    t.write_text(json.dumps({"agentId": aid, "type": "user", "message": {"role": "user", "content": prompt}}) + "\n",
                 encoding="utf-8")
    (d / f"agent-{aid}.meta.json").write_text(json.dumps({"agentType": kind, "description": description}),
                                              encoding="utf-8")
    return str(t)


def _runs(repo, rows):
    with open(git_dir(repo) / agents.RUNS, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_a_subagent_is_named_by_its_task_description(repo, tmp_path):
    t = _sub(tmp_path, "a03922aa", "Survey files in api")
    now = time.time()
    _runs(repo, [{"at": now - 5, "event": "start", "agent_id": "a03922aa", "agent_type": "general-purpose",
                  "transcript": t}])
    assert agents.title_of(repo, "a03922aa") == "Survey files in api"
    assert agents.title_of(repo, "") == "You (the main session)"
    wf = agents.workflow(repo)
    me = next(a for a in wf["agents"] if a["name"] == "sub:a03922aa")
    assert me["title"] == "Survey files in api" and me["status"] == "running" and me["adhoc"]
    # no description: the first words of its prompt, never agent-<id>
    t2 = _sub(tmp_path, "b77", "", prompt="Find every place the app sends email and say which service it uses")
    assert agents.readable(agents.subagent_info(t2), "general-purpose", "b77").startswith("Find every place the app")
    assert agents.agent_log(repo, "sub:a03922aa")["transcript_path"] == t


def test_activity_is_counted_not_repeated(repo):
    now = time.time()
    events = [{"event": activity.PROMPT, "at": now - 60, "text": "go"}]
    events += [{"event": activity.READ, "at": now - 50 + i, "path": f"src/f{i}.py", "agent": "abc"} for i in range(34)]
    events += [{"event": activity.EDIT, "at": now - 10 + i, "path": "src/users.py", "agent": "abc"} for i in range(2)]
    who = {"agent-abc": {"area": "", "blocks": [], "edits": [], "reads": [], "at": now, "title": "Survey files"}}
    lane = pane._lanes(repo, events, who)["lanes"]["agent-abc"]
    assert lane["counts"] == "34 reads · 2 edits"
    assert lane["recent"][-1]["path"] == "src/users.py" and len(lane["recent"]) == 36
    marks = pane.bucketed(lane["marks"], now - 60, 120)
    assert len(marks) <= pane.SLOTS and sum(m["n"] for m in marks) == 36
    assert any(m["kind"] == "edit" for m in marks)   # the strongest kind in its slot


def test_adhoc_subagents_get_stages_from_their_times(repo, tmp_path):
    now = time.time() - 100
    rows = []
    for aid, desc, start, stop in (("aa1", "Survey api", 0, 10), ("bb2", "Survey web", 2, 12), ("cc3", "Write summary", 15, 20)):
        t = _sub(tmp_path, aid, desc)
        rows += [{"at": now + start, "event": "start", "agent_id": aid, "agent_type": "general-purpose", "transcript": t},
                 {"at": now + stop, "event": "stop", "agent_id": aid, "agent_type": "general-purpose", "transcript": t}]
    _runs(repo, rows)
    wf = agents.workflow(repo)
    assert wf["stages"] == [["sub:aa1", "sub:bb2"], ["sub:cc3"]]
    c = next(a for a in wf["agents"] if a["name"] == "sub:cc3")
    assert sorted(c["waits_for"]) == ["sub:aa1", "sub:bb2"] and c["status"] == "done" and c["seconds"] == 5.0
    assert wf["task"]   # said, with no plan


def test_a_role_run_twice_is_offered_and_keeping_it_writes_the_agent(repo, tmp_path):
    prompt = "Read every file in the folder you are given and list what each one does, one line each."
    assert agentlib.role_of("Survey files in api") == agentlib.role_of("Survey the files of web") == "survey-file"
    assert agentlib.role_of("anything", "code-reviewer") == "code-reviewer"
    # through the hook, as Claude Code calls it when a subagent stops
    for aid, desc in (("r1", "Survey files in api"), ("r2", "Survey the files of web")):
        t = _sub(tmp_path, aid, desc, prompt=prompt)
        agent.run_hook(json.dumps({"hook_event_name": "SubagentStop", "cwd": str(repo), "agent_id": aid,
                                   "agent_type": "general-purpose", "agent_transcript_path": t}), lambda _s: None,
                       {"CLAUDE_PROJECT_DIR": str(repo)})
    hit = next(r for r in agentlib.worth_keeping(repo) if r["name"] == "survey-file")
    assert "ran 2 times" in hit["why"] and hit["brief"] == prompt and hit["description"].startswith("Use to survey")
    got = agentlib.save(hit["name"], hit["description"], hit["brief"], scope="project", root=repo)
    text = (repo / ".claude" / "agents" / "survey-file.md").read_text(encoding="utf-8")
    assert got["saved"] == "survey-file" and prompt in text
    assert not any(r["name"] == "survey-file" for r in agentlib.worth_keeping(repo))   # kept: no longer offered


def test_a_task_call_records_what_it_launched(repo):
    agent.note_launch(repo, {"tool_name": "Task", "tool_input": {"description": "Check the tests",
                                                                  "subagent_type": "general-purpose",
                                                                  "prompt": "Run the tests and say what fails."},
                             "tool_response": {"status": "async_launched", "agentId": "zz9"}})
    r = [x for x in agents.runs(repo) if x["event"] == "launch"][-1]
    assert r["agent_id"] == "zz9" and r["description"] == "Check the tests"
    assert agents.title_of(repo, "zz9") == "Check the tests"
