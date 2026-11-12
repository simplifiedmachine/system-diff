"""What the person is optimising for: which of their words are read, what of the model's answer is kept, how it merges
with what is stored and what they cleared, what Claude is told, which changes touch an objective, and the command.
No network: the model is mocked."""
from __future__ import annotations

import json
import time

import pytest

from system_diff import activity, cli, narrate, objectives
from system_diff.compare import compare


def _iso(t):
    return objectives._iso(t)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Claude Code's config dir, with no transcripts until a test writes some."""
    d = tmp_path / "claude-home"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return d


def _transcript(home, repo, lines):
    d = objectives.transcripts_dir(repo)
    assert d.parent == home / "projects"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "s1.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return f


def _user(text, ts="2026-11-10T10:00:00Z", **kw):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **kw}


REQS = ["make it fast on large repos please", "it is still slow on a large repo, make it fast",
        "must work on Windows too", "the pane must work on Windows without a console window",
        "show it, don't act for me", "make it fast on large repos, the cold start is slow"]


def _reqs(start=1_700_000_000.0):
    return [{"at": start + i * 60, "text": t} for i, t in enumerate(REQS)]


ANSWER = {"objectives": [
    {"id": "Fast on Large Repos", "text": "Fast on large repositories", "count": 3, "last": "2020-01-01T00:00:00Z",
     "evidence": ["make it fast on large repos", "invented quote nobody said"], "parts": ["core", "ghost"],
     "check": "a change that reads every file on each edit"},
    {"id": "windows", "text": "Works on Windows", "count": 2, "evidence": ["must work on Windows"],
     "parts": [], "check": "x" * 400},
    {"id": "dreamt", "text": "Uses Rust", "count": 5, "evidence": ["rewrite it in rust"]},
    {"id": "once", "text": "Shows, never acts", "count": 1, "evidence": ["show it, don't act"]},
    "garbage", {"text": ""}]}


# ---------------------------------------------------------------------------------------------------- reading

def test_only_what_the_person_typed_is_read(repo, home):
    _transcript(home, repo, [
        _user("make it fast on large repos", "2026-11-10T10:00:00Z"),
        _user([{"type": "tool_result", "tool_use_id": "t", "content": "secret file contents"}]),
        _user("<system-reminder>be nice</system-reminder>"),
        _user("<task-notification>\n<task-id>x</task-id>"),
        _user("[Request interrupted by user for tool use]"),
        _user("skill text", isMeta=True),
        _user("summary of before", isCompactSummary=True),
        _user("from a subagent", isSidechain=True),
        {"type": "assistant", "timestamp": "2026-11-10T10:01:00Z", "message": {"content": "I will"}},
        _user([{"type": "text", "text": "must work on Windows\n```\nprint('code')\n```\n"
                "<pasted_content id=\"1\">\nTraceback lines\n</pasted_content>"}], "2026-11-10T10:02:00Z"),
        _user("look at this:\n    def f(): pass\n$ ls -la\nand fix it", "2026-11-10T10:03:00Z"),
    ])
    got = objectives.from_transcripts(repo)
    assert [r["text"] for r in got] == ["make it fast on large repos", "must work on Windows",
                                        "look at this: and fix it"]
    assert got[0]["at"] == objectives._unix("2026-11-10T10:00:00Z")
    activity.record(repo, {"event": "prompt", "text": "make it fast on large repos"})    # the same, once
    activity.record(repo, {"event": "prompt", "text": "<task-notification> done"})
    activity.record(repo, {"event": "edit", "path": "app/core/rules.py"})
    activity.record(repo, {"event": "prompt", "text": "keep the pane quiet"})
    texts = [r["text"] for r in objectives.requests(repo)]
    assert texts.count("make it fast on large repos") == 1 and "keep the pane quiet" in texts
    assert not any("task-notification" in t or "secret" in t or "print(" in t for t in texts)


def test_a_long_request_is_cut_and_a_huge_transcript_is_read_from_its_end(repo, home, monkeypatch):
    assert len(objectives.clean("word " * 500)) == objectives.PROMPT_MAX
    monkeypatch.setattr(objectives, "TAIL_BYTES", 400)
    _transcript(home, repo, [_user(f"request number {i} " + "x" * 40, f"2026-11-10T10:{i:02d}:00Z")
                             for i in range(30)])
    got = objectives.from_transcripts(repo)
    assert got and got[-1]["text"].startswith("request number 29") and len(got) < 10


# ---------------------------------------------------------------------------------------------------- the answer

def test_the_answer_is_validated_hard():
    reqs, parts = _reqs(), {"core": "Core", "api": "API"}
    got = objectives.validate("```json\n" + json.dumps(ANSWER) + "\n```", reqs, parts)
    assert [o["id"] for o in got] == ["fast-on-large-repos", "windows"]     # unquoted, single-request ones dropped
    fast, win = got
    assert fast["evidence"] == ["make it fast on large repos"]              # an invented quote is dropped
    assert fast["parts"] == ["core"] and fast["count"] == 3                 # an unknown part is dropped
    assert fast["last"] == _iso(reqs[5]["at"])                              # the newest request it is quoted from
    assert len(win["check"]) <= objectives.CHECK_MAX
    for bad in ("not json", "[1]", '{"objectives": "x"}', ""):
        assert objectives.validate(bad, reqs, parts) == []
    many = {"objectives": [{"id": f"o{i}", "text": f"objective {i}", "count": 2,
                            "evidence": ["make it fast on large repos"]} for i in range(12)]}
    assert len(objectives.validate(many, reqs, parts)) == objectives.MAX_OBJECTIVES


def test_merge_keeps_ids_and_respects_what_was_cleared():
    reqs = _reqs()
    found = objectives.validate(ANSWER, reqs, {"core": "Core"})
    store = objectives._empty()
    store["objectives"] = [{"id": "speed", "text": "Fast on large repositories", "evidence": ["older quote"],
                            "count": 2, "last": "2020-01-01T00:00:00Z", "parts": [], "check": ""}]
    store["cleared"] = [{"id": "windows", "text": "Works on Windows", "at": _iso(reqs[0]["at"] - 10)}]
    got = objectives.merge(store, found)
    assert [o["id"] for o in got["objectives"]] == ["speed"]                # the same objective keeps its id
    speed = got["objectives"][0]
    assert speed["text"] == "Fast on large repositories" and speed["count"] == 3
    assert speed["evidence"][0] == "make it fast on large repos" and "older quote" in speed["evidence"]
    assert speed["parts"] == ["core"] and speed["last"] > "2020"
    assert [c["id"] for c in got["cleared"]] == ["windows"]                 # 2 requests since the clear: not back
    # three requests after the clear clearly ask for it again: proposed as new, no longer cleared
    reqs += [{"at": reqs[-1]["at"] + 60 * i, "text": f"it must work on Windows, take {i}"} for i in (1, 2, 3)]
    again = {"objectives": [{"id": "win", "text": "Works on Windows", "count": 5,
                             "evidence": ["work on Windows, take 1", "work on Windows, take 2",
                                          "work on Windows, take 3"]}]}
    store["cleared"][0]["at"] = _iso(reqs[-4]["at"] + 1)
    got = objectives.merge(store, objectives.validate(again, reqs, {}))
    assert {o["id"] for o in got["objectives"]} == {"speed", "win"} and got["cleared"] == []


# ---------------------------------------------------------------------------------------------------- the store

def _seed(repo, n=2):
    data = objectives._empty()
    data["objectives"] = [
        {"id": "fast", "text": "Fast on large repositories", "evidence": ["make it fast on large repos"],
         "count": 4, "last": "2026-11-10T10:00:00Z", "parts": ["core"], "check": "reading every file each edit"},
        {"id": "windows", "text": "Works on Windows", "evidence": ["must work on Windows"], "count": 2,
         "last": "2026-11-10T10:00:00Z", "parts": [], "check": ""}][:n]
    objectives.save(repo, data)


def test_clear_and_restore(repo):
    _seed(repo)
    assert [o["id"] for o in objectives.current(repo)] == ["fast", "windows"]
    assert objectives.clear(repo, "windows") and not objectives.clear(repo, "windows")
    assert [o["id"] for o in objectives.current(repo)] == ["fast"]
    data = objectives.load(repo)
    assert data["cleared"][0]["id"] == "windows" and data["cleared"][0]["at"]
    assert objectives.restore(repo, "windows") and not objectives.restore(repo, "windows")
    assert objectives.current(repo)[1]["text"] == "Works on Windows" and objectives.load(repo)["cleared"] == []
    f = repo / ".git" / "system-diff" / "objectives.json"
    assert json.loads(f.read_text())["version"] == 1


def test_context_is_short_and_names_them(repo):
    assert objectives.context(repo) == ""
    _seed(repo)
    text = objectives.context(repo)
    assert text.startswith(objectives.CONTEXT_HEAD)
    assert "1. Fast on large repositories." in text and "2. Works on Windows." in text
    assert text.endswith(objectives.CONTEXT_TAIL) and "check_objective" in text
    data = objectives.load(repo)
    data["objectives"] = [{**data["objectives"][0], "id": f"o{i}", "text": "a long objective " * 6}
                          for i in range(7)]
    objectives.save(repo, data)
    assert len(objectives.context(repo)) <= objectives.CONTEXT_MAX


def test_touches_marks_the_objectives_a_change_touches(repo):
    c = compare(repo, "HEAD~1", "HEAD")
    objs = [{"id": "fast", "text": "Fast order rules", "parts": ["core"]},
            {"id": "mail", "text": "Never lose a mail", "parts": []},
            {"id": "jobs", "text": "Background jobs stay simple", "parts": []},
            {"id": "ui", "text": "A pretty web page", "parts": ["web"]}]
    got = objectives.touches(objs, c)
    assert "fast" in got and "which it is about" in got["fast"][0]
    assert "jobs" in got and any("jobs" in r for r in got["jobs"])          # its words name a changed part
    assert "ui" not in got and "mail" not in got
    assert objectives.touches(objs, c.diff, c.after) == got                  # a diff and a blueprint: the same
    assert "ui" in objectives.touches(objs, {"changes": []}, paths=["web/src/pretty.tsx"])
    assert objectives.touches(objs, None) == {}


# ---------------------------------------------------------------------------------------------------- the job

@pytest.fixture
def an_llm(monkeypatch, home):
    monkeypatch.setenv("SYSTEM_DIFF_OBJECTIVES", "1")
    monkeypatch.setattr(narrate, "claude_llm", lambda root, extra=(): {"base_url": "http://x", "token": "t",
                                                                       "model": "m", "source": "test"})
    spawned = []
    monkeypatch.setattr(objectives, "_spawn", lambda root, job: spawned.append(job))
    return spawned


def _prompts(repo, texts):
    for t in texts:
        activity.record(repo, {"event": "prompt", "text": t})


def test_ensure_starts_once_in_the_background_and_the_job_learns(repo, an_llm, monkeypatch):
    _prompts(repo, REQS)
    c = compare(repo, "HEAD", ".")
    assert objectives.ensure(repo, c.after) and len(an_llm) == 1
    assert not objectives.ensure(repo, c.after) and len(an_llm) == 1           # rate-limited and locked
    job = an_llm[0]
    assert json.loads(job.read_text())["parts"]["core"] == "Core"
    sent = []
    monkeypatch.setattr(narrate, "complete", lambda user, system, root, cfg, timeout=120, limit=1500:
                        sent.append(user) or (json.dumps(ANSWER), "m (test)"))
    assert objectives.run(job) == 0
    assert not job.exists() and not (job.parent / objectives.LOCK).exists()
    assert "must work on Windows too" in sent[0] and "def list_orders" not in sent[0]
    data = objectives.load(repo)
    assert [o["id"] for o in data["objectives"]] == ["fast-on-large-repos", "windows"]
    assert data["from"] == len(REQS) and data["model"] == "m (test)" and data["seen"] and data["asked_at"]
    assert "_times" not in json.dumps(data)


def test_nothing_starts_off_under_pytest_without_an_llm_or_with_too_few_requests(repo, an_llm, monkeypatch):
    _prompts(repo, REQS[:2])
    assert not objectives.ensure(repo) and not an_llm                        # two requests: too few
    _prompts(repo, REQS[2:])
    monkeypatch.setenv("SYSTEM_DIFF_OBJECTIVES", "0")
    assert not objectives.ensure(repo) and not an_llm
    monkeypatch.delenv("SYSTEM_DIFF_OBJECTIVES")                             # under pytest: off unless asked
    assert not objectives.ensure(repo) and not an_llm
    monkeypatch.setenv("SYSTEM_DIFF_OBJECTIVES", "1")
    monkeypatch.setattr(narrate, "claude_llm", lambda root, extra=(): None)
    monkeypatch.setattr(narrate, "shell_llm", lambda: None)
    assert not objectives.ensure(repo) and not an_llm                        # no gateway: nothing, no error


# ---------------------------------------------------------------------------------------------------- the command

def test_the_command_lists_clears_and_restores(repo, capsys):
    assert cli.main(["--repo", str(repo), "objectives"]) == 0
    assert "No objectives learned yet" in capsys.readouterr().out
    _seed(repo)
    assert cli.main(["--repo", str(repo), "objectives"]) == 0
    out = capsys.readouterr().out
    assert "1. Fast on large repositories  [fast]" in out and "make it fast on large repos" in out
    assert "objectives clear <id>" in out
    assert cli.main(["--repo", str(repo), "objectives", "clear", "fast"]) == 0
    assert "cleared" in capsys.readouterr().out
    assert [o["id"] for o in objectives.current(repo)] == ["windows"]
    assert cli.main(["--repo", str(repo), "objectives", "clear", "fast"]) == 1
    assert cli.main(["--repo", str(repo), "objectives", "restore", "fast"]) == 0
    assert {o["id"] for o in objectives.current(repo)} == {"fast", "windows"}


def test_learn_runs_in_the_foreground(repo, an_llm, monkeypatch, capsys):
    _prompts(repo, REQS)
    monkeypatch.setattr(narrate, "complete", lambda *a, **k: (json.dumps(ANSWER), "m (test)"))
    assert cli.main(["--repo", str(repo), "objectives", "learn"]) == 0
    out = capsys.readouterr().out
    assert "read 6 requests" in out and "found 2 objectives" in out
    assert len(objectives.current(repo)) == 2 and not an_llm


# ---------------------------------------------------------------------------------------------------- checks

def test_agents_record_checks_and_the_bus_hears_them(repo):
    from system_diff import agents
    _seed(repo)
    objectives.record_check(repo, "fast", "tester", "checking")
    got = objectives.record_check(repo, "fast", "tester", "Holds", "cold start 2.1 s on 40k files")
    assert got["status"] == "holds" and got["agent"] == "tester"
    objectives.record_check(repo, "fast", "reviewer", "broken", "reads every file on each edit")
    for bad in (("ghost", "a", "holds"), ("fast", "a", "fine")):
        with pytest.raises(ValueError):
            objectives.record_check(repo, *bad)
    by = objectives.checks(repo)
    assert [(c["agent"], c["status"]) for c in by["fast"]] in ([("reviewer", "broken"), ("tester", "holds")],
                                                               [("tester", "holds"), ("reviewer", "broken")])
    assert "windows" not in by
    msgs = [m for m in agents.messages(repo) if m["topic"] == "objective:fast"]
    assert len(msgs) == 3 and msgs[-1]["from"] == "reviewer" and msgs[-1]["kind"] == "check"
    assert "broken" in msgs[-1]["text"] and "reads every file" in msgs[-1]["text"]
    for i in range(objectives.CHECK_HISTORY + 4):                            # a short history only
        objectives.record_check(repo, "fast", "tester", "holds", f"run {i}")
    kept = [c for c in objectives.load(repo)["checks"] if c["agent"] == "tester"]
    assert len(kept) == objectives.CHECK_HISTORY and kept[-1]["note"] == f"run {objectives.CHECK_HISTORY + 3}"
    objectives.clear(repo, "fast")
    assert objectives.checks(repo) == {}                                    # only the active objectives' checks


def test_the_brief_and_the_mcp_tools(repo):
    pytest.importorskip("mcp")
    from system_diff import mcp_server
    assert objectives.for_brief(repo) == "" and mcp_server.tool_objectives(repo)["objectives"] == []
    _seed(repo)
    brief = objectives.for_brief(repo)
    assert "check_objective" in brief and "- fast: Fast on large repositories (breaks if: reading every" in brief
    assert mcp_server.tool_check_objective(repo, "a1", "fast", "unclear", "no large repo here")["recorded"]
    assert "error" in mcp_server.tool_check_objective(repo, "a1", "nope", "holds")
    got = mcp_server.tool_objectives(repo)
    fast = got["objectives"][0]
    assert fast["id"] == "fast" and fast["check"] and fast["checks"][0]["status"] == "unclear"
    assert got["statuses"] == ["checking", "holds", "broken", "unclear"]
