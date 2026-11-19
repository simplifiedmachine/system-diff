"""The person writes while Claude works: the message waits for Claude's next step (editable, withdrawable), is read
there, and its line says what became of it (answered, handed to a subagent). A subagent that ran is kept as an agent
from the chat (keep_agent)."""
import asyncio

import pytest

from system_diff import agentlib, agents, app_page, sessions


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


class ToolResultBlock:
    def __init__(self, tool_use_id, content="ok", is_error=False):
        self.tool_use_id, self.content, self.is_error = tool_use_id, content, is_error


class AssistantMessage:
    def __init__(self, content, parent=None):
        self.content, self.model, self.parent_tool_use_id = content, "haiku", parent


class UserMessage:
    def __init__(self, content, parent=None):
        self.content, self.parent_tool_use_id = content, parent


class FakeClient:
    def __init__(self):
        self.asked = []

    async def query(self, text):
        self.asked.append(text)


def _types(s):
    return [e["type"] for e in s.events]


def test_a_message_written_mid_turn_is_held_then_read_at_the_next_step(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEM_DIFF_APP_STATE", str(tmp_path / "state"))
    s = sessions.Session(None, tmp_path, {})
    s.client = FakeClient()
    s.set_status("working")
    first = s.send("what is 7 times 6?")["queued"]
    second = s.send("and also check the tests")["queued"]
    assert first and second and [e.get("queued") for e in s.events if e["type"] == "user"] == [first, second]
    s.edit_queued(first, "what is 7 times 7?")
    s.edit_queued(second, withdraw=True)
    with pytest.raises(ValueError):
        s.edit_queued(second, "too late")

    async def turn():
        s.record(AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "sleep 8"})]))   # a step: read with it
        await asyncio.sleep(0)
        s.record(UserMessage([ToolResultBlock("t1")]))
        s.record(AssistantMessage([ToolUseBlock("t2", "Task", {"description": "Run the tests", "prompt": "…"})]))
        s.record(AssistantMessage([TextBlock("7 times 7 is 49.")]))
    asyncio.run(turn())
    assert s.client.asked == ["[Sent while you were working] what is 7 times 7?"]
    got = {e["type"]: e for e in s.events if e["type"] in ("delivered", "handed", "replied", "queued_withdrawn")}
    assert got["delivered"]["id"] == first and got["queued_withdrawn"]["id"] == second
    assert got["handed"] == {**got["handed"], "ids": [first], "to": "Run the tests"}
    assert got["replied"]["ids"] == [first] and s.read_by == []
    with pytest.raises(ValueError):
        s.edit_queued(first, "read already")


def test_a_message_written_while_a_tool_runs_is_given_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEM_DIFF_APP_STATE", str(tmp_path / "state"))
    calls = []

    class M:
        def call(self, coro):
            calls.append(coro.__qualname__)
            coro.close()
    s = sessions.Session(M(), tmp_path, {})
    s.set_status("working")
    s.record(AssistantMessage([ToolUseBlock("t1", "Bash", {"command": "sleep 8"})]))
    s.send("what is 7 times 6?")
    assert calls == ["Session._deliver_soon"]


def test_interrupt_and_send_starts_the_next_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEM_DIFF_APP_STATE", str(tmp_path / "state"))
    calls = []

    class M:
        def call(self, coro):
            calls.append(coro)
            coro.close()
    s = sessions.Session(M(), tmp_path, {})
    s.client = object()
    s.set_status("idle")
    assert s.send("hello") == {"queued": ""}          # idle: a turn of its own, as before
    monkeypatch.setattr(s, "interrupt", lambda: calls.append("interrupt"))
    s.set_status("working")
    assert s.send("stop that, do this", interrupt=True) == {"queued": ""} and "interrupt" in calls


def test_the_prompt_says_what_a_mid_turn_message_is():
    assert "[Sent while you were working]" in sessions.APPEND and "SendMessage" in sessions.APPEND


def test_the_chat_shows_where_a_queued_message_is():
    p = app_page.PAGE
    for needed in ("function queuedBubble(", "queued: Claude reads it at its next step", '"Add to turn"',
                   'id="isend"', "→ handed to ", "answered below", '"/queued"'):
        assert needed in p, needed


def _ran(repo, aid, description, prompt, kind="general-purpose"):
    agents.record_run(repo, "launch", aid, kind, description=description, prompt=prompt)
    agents.record_run(repo, "stop", aid, kind)


def test_keep_agent_saves_a_subagent_that_ran(repo):
    _ran(repo, "aaa111", "Survey files in api", "Read every file under api/ and list what each one does, in a table.")
    _ran(repo, "bbb222", "Survey the files of web", "Read every file under web/ and list what each one does, in a table.")
    _ran(repo, "ccc333", "Run the tests", "Run pytest and report the failures with their causes, briefly please.")
    got = agentlib.keep_ran(repo, "survey")
    assert got["kept"] and got["runs_of_this_role"] == 2 and got["from"] == "sub:bbb222"
    f = repo / ".claude" / "agents" / "survey-file.md"
    assert f.exists() and "web/" in f.read_text(encoding="utf-8")
    assert agentlib.keep_ran(repo, "sub:ccc333", name="tester")["kept"]
    assert (repo / ".claude" / "agents" / "tester.md").exists()


def test_keep_agent_lists_them_when_several_match_and_never_guesses(repo):
    _ran(repo, "aaa111", "Check the api", "Check the api folder for unused functions and report them in a list.")
    _ran(repo, "bbb222", "Review the api docs", "Review the api documentation for drift from the code, in a list.")
    got = agentlib.keep_ran(repo, "api")
    assert not got["kept"] and {c["which"] for c in got["choose"]} == {"sub:aaa111", "sub:bbb222"}
    assert not (repo / ".claude" / "agents").exists()
    none = agentlib.keep_ran(repo, "nothing like this")
    assert not none["kept"] and none["ran"]
