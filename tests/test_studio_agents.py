"""The Studio's subagents: one line each in the chat (their own calls folded inside), the Agents tab listing them and
never failing as "no such session", and the Keys list showing what was stored (a setting's value, a secret on Show)."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from system_diff import app, app_page, sessions, vault


# SDK-shaped messages: record() goes by the class names
class AssistantMessage:
    def __init__(self, content, parent=None):
        self.content, self.parent_tool_use_id, self.model = content, parent, "m"


class UserMessage:
    def __init__(self, content, parent=None):
        self.content, self.parent_tool_use_id = content, parent


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


class ToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id, self.content, self.is_error = tool_use_id, content, is_error


class TextBlock:
    def __init__(self, text):
        self.text = text


def _session(tmp_path):
    s = sessions.Session.__new__(sessions.Session)
    s.__dict__.update({"id": "s1", "project": tmp_path, "options": {}, "masks": [], "events": [], "first": 0,
                       "cond": threading.Condition(), "_log": None, "tools": {}, "subs": {}, "model": "",
                       "read_by": [], "held": [], "turn_at": 0.0, "last_at": 0.0, "doing": {}, "_held": "",
                       "pending": {}, "status": "working"})
    s.state = lambda *a, **k: None
    return s


def test_a_subagents_calls_are_folded_into_its_one_line(tmp_path):
    s = _session(tmp_path)
    s.record(AssistantMessage([ToolUseBlock("T1", "Task", {"description": "Survey files in api",
                                                           "subagent_type": "general-purpose",
                                                           "prompt": "List the files under api/"})]))
    s.record(AssistantMessage([ToolUseBlock("c1", "Glob", {"pattern": "api/**"})], parent="T1"))
    s.record(UserMessage([ToolResultBlock("c1", "api/main.py")], parent="T1"))
    s.record(AssistantMessage([ToolUseBlock("c2", "Edit", {"file_path": "api/main.py", "old_string": "a",
                                                           "new_string": "b"})], parent="T1"))
    s.record(AssistantMessage([TextBlock("2 files")], parent="T1"))
    [sub] = s.subagents()
    assert sub["name"] == "Survey files in api" and sub["type"] == "general-purpose" and sub["status"] == "running"
    assert (sub["reads"], sub["edits"]) == (1, 1) and sub["latest"].startswith("Edit")
    s.record(UserMessage([ToolResultBlock("T1", "There are 2 files.")]))
    assert s.subagents()[0]["status"] == "done"
    evs = s.events
    task = next(e for e in evs if e["type"] == "tool_use" and e["id"] == "T1")
    assert task["parent"] == "" and task["view"]["description"] == "Survey files in api"
    assert all(e["parent"] == "T1" for e in evs if e["type"] == "tool_use" and e["id"] in ("c1", "c2"))
    assert next(e for e in evs if e["type"] == "text")["parent"] == "T1"
    assert [e["status"] for e in evs if e["type"] == "sub_done"] == ["done"]


def test_a_background_subagent_is_done_when_it_stops(tmp_path):
    s = _session(tmp_path)
    s.record(AssistantMessage([ToolUseBlock("T2", "Agent", {"description": "Review the plan",
                                                            "run_in_background": True})]))
    s.record(UserMessage([ToolResultBlock("T2", "Async agent launched successfully.")]))
    assert s.subagents()[0]["status"] == "running"
    s.sub_stopped()
    assert s.subagents()[0]["status"] == "done"


def test_the_page_draws_a_subagent_as_one_line():
    p = app_page.PAGE
    for needed in ("function subLine(", "function subCount(", "function subDone(", "details.subline",
                   'ev.type === "tool_use" && !ev.parent && SUB_TOOLS.includes(ev.name)', "Subagents in this project's sessions"):
        assert needed in p, needed


class _Fake:
    def __init__(self, session):
        self.session = session

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def query(self, text):
        pass

    async def receive_response(self):
        return
        yield


def _until(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _call(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_the_agents_tab_never_says_no_such_session_and_keys_show_what_was_stored(monkeypatch, repo, tmp_path):
    from system_diff import agents
    monkeypatch.setattr(vault, "backend", lambda: "file")
    folder = tmp_path / "state"
    m = sessions.Manager(client_factory=_Fake)
    up = {}
    t = threading.Thread(target=app.serve, kwargs={"port": 0, "folder": folder, "manager": m,
                                                   "ready": lambda url, server, st: up.update(url=url, server=server,
                                                                                              studio=st)},
                         daemon=True)
    t.start()
    try:
        assert _until(lambda: "url" in up)
        url, tok = up["url"], up["studio"].token
        w = {"X-Token": tok, "Origin": url.rstrip("/")}
        pid = _call(url + "api/projects", {"path": str(repo)}, w)["id"]

        def broken(root):
            raise KeyError("agents")
        monkeypatch.setattr(agents, "workflow", broken)   # an odd state file: the tab says so, and shows the rest
        got = _call(url + f"api/workflows?project={pid}", headers={"Referer": url})
        assert got["current"]["agents"] == 0 and "KeyError" in got["problems"][0]
        lib = _call(url + f"api/agents?project={pid}", headers={"Referer": url})
        assert lib["running"] == [] and "worth_keeping" in lib    # no session: still the tab, not an error

        _call(url + "api/secrets/save", {"project": pid, "name": "STAGING_HOST", "value": "10.1.2.3",
                                         "kind": "setting", "group": "staging server"}, w)
        _call(url + "api/secrets/save", {"project": pid, "name": "STAGING_PASSWORD", "value": "Xyz!23-not-shown",
                                         "group": "staging server"}, w)
        listed = {k["name"]: k for k in _call(url + f"api/secrets?project={pid}", headers={"Referer": url})["keys"]}
        assert listed["STAGING_HOST"]["value"] == "10.1.2.3" and "Xyz!23" not in json.dumps(listed)
        for headers in ({"X-Token": tok}, {"Origin": url.rstrip("/")}):   # Show needs the token and the page
            with pytest.raises(urllib.error.HTTPError) as no:
                _call(url + "api/secrets/reveal", {"project": pid, "name": "STAGING_PASSWORD"}, headers)
            assert no.value.code == 403
        shown = _call(url + "api/secrets/reveal", {"project": pid, "name": "STAGING_PASSWORD"}, w)
        assert shown["value"] == "Xyz!23-not-shown"
    finally:
        up["server"].shutdown()
        m.stop()


def test_the_agents_tab_is_drawn_off_the_page_and_swapped_in():
    """It reloads every 5 s while Claude works: emptied first, it went blank for as long as the data took."""
    from system_diff import app_page
    p = app_page.PAGE
    assert "async function drawAgents(box)" in p and "shown.replaceChildren(...box.childNodes)" in p
