"""The Studio's sessions: past conversations like /resume (the ones left open when it stopped first), a resumed
conversation's past messages in its feed, and what Claude Code is doing now while it works."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from system_diff import app_page, sessions


def _transcript(home: Path, project: Path, cid: str, lines: list) -> Path:
    import re
    d = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project.resolve()))
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{cid}.jsonl"
    f.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return f


def _turn(n: int) -> list:
    return [
        {"type": "user", "message": {"role": "user", "content": f"question {n}"}, "timestamp": "2026-11-18T10:00:00Z"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{n}", "name": "Bash",
                                                        "input": {"command": "ls", "description": "List files"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": "a.py"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": f"**answer** {n}"}]}},
    ]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: h)
    monkeypatch.setenv("SYSTEM_DIFF_APP_STATE", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    sessions._SCANS.clear()
    return h


CID = "0b5e2a8c-1111-4222-8333-944455556666"


def test_past_conversations_like_resume_with_counts_and_the_ones_left_off_first(home, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _transcript(home, proj, CID, [{"type": "user", "isMeta": True, "message": {"content": "<command-name>x"}}] + _turn(1))
    other = "1c6f3b9d-2222-4333-8444-a55566667777"
    f = _transcript(home, proj, other, _turn(2) + _turn(3))
    import os
    os.utime(f, (f.stat().st_mtime + 100, f.stat().st_mtime + 100))   # the newer one
    _transcript(home, proj, "2d7a4cae-3333-4444-8555-b66677778888", [])   # nothing said: not listed
    got = sessions.past(proj)
    assert [p["id"] for p in got] == [other, CID]
    assert got[0]["first_prompt"] == "question 2" and got[0]["messages"] == 4 and not got[0]["left_off"]

    class S:   # a Studio session of that conversation, open when the Studio stopped
        claude_id, project, id, title, status = CID, proj, "abc", "question 1", "idle"
        def public_options(self):
            return {"model": "haiku"}
    sessions.note_open(S())
    got = sessions.past(proj)
    assert got[0]["id"] == CID and got[0]["left_off"] and got[0]["options"] == {"model": "haiku"}
    assert sessions.past(proj, live=[CID])[0]["id"] == other     # open now: not "left off"
    sessions.drop_open(CID)
    assert not sessions.left_off(proj)


def test_a_resumed_conversation_shows_its_past_messages_newest_first_with_earlier_on_request(home, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    lines = []
    for n in range(120):
        lines += _turn(n)
    _transcript(home, proj, CID, lines)
    got = sessions.history(proj, CID)
    assert len(got["entries"]) == sessions.HISTORY_MOST and got["total"] == 480 and got["earlier"] == 280
    first = got["entries"][0]
    assert all(e["past"] for e in got["entries"]) and got["entries"][-1]["text"] == "**answer** 119"
    kinds = {e["type"] for e in got["entries"]}
    assert kinds == {"user", "tool_use", "tool_result", "text"}
    tool = next(e for e in got["entries"] if e["type"] == "tool_use")
    assert tool["label"] == "List files" and tool["view"]["command"] == "ls"
    more = sessions.history(proj, CID, before=first["i"])
    assert more["entries"][-1]["i"] == first["i"] - 1 and more["earlier"] == 80
    with pytest.raises(ValueError):
        sessions.history(proj, "../../etc/passwd")


def test_a_transcript_is_read_once_while_it_is_unchanged(home, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    f = _transcript(home, proj, CID, _turn(1))
    reads = []
    real = sessions._parse
    monkeypatch.setattr(sessions, "_parse", lambda p: reads.append(p) or real(p))
    sessions.past(proj)
    sessions.past(proj)
    sessions.history(proj, CID)
    assert len(reads) == 1
    with open(f, "a", encoding="utf-8") as out:
        out.write(json.dumps(_turn(2)[0]) + "\n")
    assert sessions.past(proj)[0]["messages"] == 3 and len(reads) == 2


class StreamEvent:
    def __init__(self, event, parent=None):
        self.event, self.parent_tool_use_id = event, parent


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id, self.name, self.input = id, name, input


class AssistantMessage:
    def __init__(self, content, parent=None):
        self.content, self.model, self.parent_tool_use_id = content, "m", parent


class ToolResultBlock:
    def __init__(self, tool_use_id, content="ok", is_error=False):
        self.tool_use_id, self.content, self.is_error = tool_use_id, content, is_error


class UserMessage:
    def __init__(self, content, parent=None):
        self.content, self.parent_tool_use_id = content, parent


def test_what_claude_code_is_doing_now(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEM_DIFF_APP_STATE", str(tmp_path / "state"))
    s = sessions.Session(None, tmp_path, {})
    assert s.now() == {}
    s.set_status("working")
    assert s.now()["what"] == "thinking"
    s.record(StreamEvent({"type": "content_block_start", "content_block": {"type": "text"}}))
    assert s.now()["what"] == "writing"
    s.record(AssistantMessage([ToolUseBlock("t1", "Task", {"description": "Map the API", "prompt": "…"})]))
    assert s.now()["what"] == "tool" and s.now()["detail"] == "Map the API"
    s.record(AssistantMessage([ToolUseBlock("t2", "mcp__system-diff__ui_view", {})], parent="t1"))
    now = s.now()
    assert now["agent"] == "Map the API" and now["tool"] == "mcp__system-diff__ui_view" and now["detail"] == "system-diff: ui_view"
    s.record(UserMessage([ToolResultBlock("t2")], parent="t1"))
    assert s.now()["tool"] == "Task"      # the subagent's own call ended: its Task still runs
    s.record(UserMessage([ToolResultBlock("t1")]))
    assert s.now()["what"] == "thinking"
    done = [e for e in s.events if e["type"] == "tool_result"]
    assert all(isinstance(e["ms"], int) for e in done)
    assert s.now()["turn_for"] >= 0 and s.now()["quiet_for"] >= 0
    states = [e for e in s.events if e["type"] == "state"]
    assert len(states) >= 5 and all("now" in e for e in states)
    s.set_status("idle")
    assert s.now() == {} and s.head()["now"] == {}


def test_the_column_shows_past_conversations_history_and_live_state():
    page = app_page.PAGE
    for needed in ("Continue where you left off", "Past conversations", "function paintPast", "function paintHistory",
                   "Show earlier", "function doingText", "no word from Claude Code for", "spin run", 'id="pastbtn"'):
        assert needed in page, needed
    if shutil.which("node") is None:
        return
    js = page.split("<script>")[1].split("</script>")[0]
    src = js[js.index("function clock("):js.index("setInterval(() => { const s = S.session")]
    out = subprocess.run(["node", "-e", src + """
console.log(JSON.stringify([clock(75), toolName("mcp__system-diff__ui_view"),
  doingText({what:"tool", tool:"Bash", detail:"git status"}, "working"), doingText({what:"writing"}, "working"),
  doingText({what:"tool"}, "waiting")]));"""], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert json.loads(out.stdout) == ["1 min 15 s", "system-diff › ui_view", "Running Bash: git status",
                                      "Writing a reply", "Waiting for your answer (above)"]


def test_past_conversations_are_labelled_by_what_was_asked(tmp_path):
    """"continue" says nothing: a conversation is labelled by its summary, else its first real request, with its last
    prompt beside it; the column has a Sessions button."""
    import json as _j
    from system_diff import app_page, sessions
    f = tmp_path / "abc.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": "continue"}},
            {"type": "user", "message": {"role": "user", "content": "Add PII guard rails and team access control"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "On it."}]}},
            {"type": "user", "message": {"role": "user", "content": "are you working?"}}]
    f.write_text("".join(_j.dumps(r) + "\n" for r in rows), encoding="utf-8")
    got = sessions.scan(f)
    assert got["asked"].startswith("Add PII") and got["last_prompt"] == "are you working?"
    assert sessions.trivial_prompt("continue") and sessions.trivial_prompt("Are you working?")
    assert not sessions.trivial_prompt("deploy the api")
    assert 'id="sessionsbtn"' in app_page.PAGE and "Last: " in app_page.PAGE


def test_a_conversation_open_in_a_session_is_not_resumed_twice():
    """Continue/Resume on a conversation a session already has: back to that session, not a second copy editing the
    same files (the person saw two sessions change the same files)."""
    import threading as _th
    from types import SimpleNamespace
    from system_diff import app_page, sessions
    m = sessions.Manager.__new__(sessions.Manager)
    m.lock, m.sessions = _th.Lock(), {"a": SimpleNamespace(claude_id="3ad4863d", status="working"),
                                      "b": SimpleNamespace(claude_id="94189708", status="closed")}
    assert m.open_with("3ad4863d") is m.sessions["a"]
    assert m.open_with("94189708") is None and m.open_with("") is None
    assert "Open now (" in app_page.PAGE


def test_each_running_call_shows_for_how_long():
    from system_diff import app_page
    p = app_page.PAGE
    assert 'sp.dataset.at = String(ev.at * 1000)' in p and '"running · " + clock(' in p


def test_each_call_says_queued_running_and_done_on_its_own():
    """Calls made together: each row says queued until Claude Code starts it, running then, and done with its own
    output when it ends (the hooks), not when the slowest of them ends."""
    from system_diff import app_page, sessions
    p = app_page.PAGE
    assert 'el("span", "queued", "spin wait")' in p and 'ev.type === "tool_start" || ev.type === "tool_done"' in p
    assert sessions._response_text({"stdout": "quick", "stderr": ""}) == "quick"
    assert sessions._response_text("x") == "x" and sessions._response_text(None) == ""
    assert '"pacts"' in p   # the open sessions' Open and Close side by side


def _a_bash():
    import os
    import shutil
    if os.name == "nt":   # Git's own bash, as Claude Code uses it on Windows (never WSL's bash.exe)
        for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if os.path.exists(p):
                return p
        return None
    return shutil.which("bash")


def test_a_commands_output_is_copied_live_and_handed_back_whole(tmp_path):
    """The Studio shows a command's output as it runs: it goes to a log, and at the end the shell prints the log
    back, so Claude gets the same output and exit code; a cd still carries on to what runs after it."""
    import subprocess
    import pytest
    from system_diff import sessions
    bash = _a_bash()
    if bash is None:
        pytest.skip("no bash here")
    log = (tmp_path / "run.log").as_posix()

    def run(cmd):
        p = subprocess.run([bash, "-c", sessions.live_prefix(log) + "\n" + cmd + f'\npwd -P > "{tmp_path.as_posix()}/cwd.txt"'],
                           capture_output=True, text=True, cwd=tmp_path)
        return p.returncode, p.stdout.replace("\r\n", "\n"), p.stderr
    assert run("echo quick") == (0, "quick\n", "")
    rc, out, _err = run("echo out; echo err >&2; exit 3")
    assert rc == 3 and out == "out\nerr\n"
    assert run("true") == (0, "", "")
    (tmp_path / "sub").mkdir()
    assert run("cd sub && echo in")[1] == "in\n" and (tmp_path / "cwd.txt").read_text().strip().endswith("sub")
    assert open(log).read().strip() == "in"   # what the page read while it ran


def test_the_card_shows_the_command_as_claude_wrote_it():
    from system_diff import sessions
    cmd = sessions.LIVE_MARK + sessions.live_prefix("/tmp/x.log") + "\n" + "git status"
    assert sessions.tool_input_view("Bash", {"command": cmd})["command"] == "git status"


def test_a_session_with_agents_at_work_is_not_restarted_for_a_key():
    """Saving a key restarted the open session (to give it the key), and that stopped its agents running in the
    background: a restart (or settings switch) is refused while one of its subagents runs."""
    import inspect
    from system_diff import app
    src = inspect.getsource(app)
    assert 'if act in ("restart", "switch"):' in src and 'x.get("status") == "running"' in src


def test_conversations_can_be_renamed_and_starred(tmp_path):
    import threading as _th
    from system_diff import app, app_page
    st = app.Studio.__new__(app.Studio)
    st.lock, st.folder = _th.Lock(), tmp_path
    assert st.mark("abc", name="Control Center fix") == {"name": "Control Center fix"}
    assert st.mark("abc", star=True) == {"name": "Control Center fix", "star": True}
    assert st.marks()["abc"]["star"] is True
    st.mark("abc", name="", star=False)
    assert "abc" not in st.marks()
    p = app_page.PAGE
    assert "function markButtons(" in p and '"Starred"' in p and "#optsbox[open] > .opts { max-height" in p
