"""The thin clients of the resident engine: `system-diff panel` (what the pane runs), the hooks that tell the engine
what Claude edited, and the terminal view. The engine itself (live.py) is faked here: these tests are about the
clients asking it, and doing as before when it is not there."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import system_diff
from system_diff import agent, panel, tui


@pytest.fixture
def live(monkeypatch):
    """The engine's client module, as the clients import it (`from . import live`): the real one when it exists, its
    calls replaced; else a stand-in. `live.pages` answers client_page by (view, since); `live.asked` and
    `live.touched` record the calls."""
    try:
        from system_diff import live as mod   # type: ignore[attr-defined]
    except ImportError:
        mod = types.ModuleType("system_diff.live")
        monkeypatch.setitem(sys.modules, "system_diff.live", mod)
        monkeypatch.setattr(system_diff, "live", mod, raising=False)
    mod.asked, mod.touched, mod.answer = [], [], None

    def client_page(root, view, width, zoom, part, since=None, take=False, timeout=30.0):
        mod.asked.append({"root": root, "view": view, "width": width, "zoom": zoom, "part": part, "since": since,
                          "take": take, "timeout": timeout})
        return mod.answer(view, since) if mod.answer else None

    def touch(root, paths):
        mod.touched.append((root, list(paths)))

    monkeypatch.setattr(mod, "client_page", client_page, raising=False)
    monkeypatch.setattr(mod, "touch", touch, raising=False)
    return mod


def a_page(view="now", text="from the engine"):
    return {"protocol": 2, "view": view, "part": "", "width": 90, "wide": 20, "rows": [[[text, "bold"]]], "blocks": [],
            "send": []}


def no_git(monkeypatch):
    """Any git (or other) process started from here fails the test: the thin client reads nothing itself."""
    def refuse(*a, **k):
        raise AssertionError(f"a process was started: {a[:1]}")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


# --- system-diff panel: the thin client --------------------------------------------------------------------------


def test_the_panel_answers_from_the_engine_with_its_version_and_reads_nothing(tmp_path, monkeypatch, live, capsys):
    (tmp_path / ".git").mkdir()                     # a repository, as far as finding the root goes; no git in it
    monkeypatch.chdir(tmp_path)
    live.answer = lambda view, since: {"version": 7, "page": a_page(view)}
    no_git(monkeypatch)
    assert panel.fast(["panel", "--protocol", "2", "--width", "90", "--view", "review", "--part", "api",
                       "--zoom", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == 7 and out["rows"] == [[["from the engine", "bold"]]] and out["view"] == "review"
    asked = live.asked[-1]
    assert (asked["root"], asked["view"], asked["width"], asked["zoom"], asked["part"]) == \
        (tmp_path.resolve(), "review", 90, 1, "api")
    assert asked["since"] is None and asked["take"] is True and asked["timeout"] <= panel.ASK_TIMEOUT


def test_since_is_a_long_poll_and_the_cli_flags_parse(tmp_path, monkeypatch, live, capsys):
    (tmp_path / ".git").mkdir()
    live.answer = lambda view, since: {"version": since + 1, "page": a_page(view)}
    no_git(monkeypatch)
    assert panel.fast(["--repo", str(tmp_path), "panel", "--protocol", "2", "--since", "4", "--width=70",
                       "--view", "now", "--part", "", "--no-take"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == 5
    asked = live.asked[-1]
    assert asked["since"] == 4 and asked["take"] is False and asked["width"] == 70 and asked["zoom"] == -1
    assert asked["timeout"] > panel.LONG_POLL   # it waits longer than the engine holds a long poll
    # what it does not know, the CLI parses (and says what is wrong)
    assert panel.fast(["panel", "--colour", "red"]) is None
    assert panel.fast(["panel", "--view", "nowhere"]) is None
    assert panel.fast(["panel", "--since", "soon"]) is None
    assert panel.fast(["diff"]) is None


def test_with_no_engine_the_panel_starts_one_and_answers_from_its_own_read(repo, monkeypatch, live, capsys):
    from system_diff import watch
    started = []
    monkeypatch.delenv("SYSTEM_DIFF_WEB_START", raising=False)
    monkeypatch.setattr(watch, "start_detached", lambda root, port=8765, wait=3.0: started.append((root, wait)),
                        raising=False)
    monkeypatch.setattr(watch, "web_url", lambda root: None, raising=False)
    live.answer = None                               # no engine answers
    assert panel.fast(["--repo", str(repo), "panel", "--protocol", "2", "--width", "100"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "version" not in out and out["rows"]      # the page read here, as before
    assert (repo, 0) in started                      # and an engine on its way, never waited for


def test_without_the_engine_module_the_panel_reads_as_before(repo, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "system_diff.live", None)   # `from . import live` fails: an older install
    monkeypatch.delattr(system_diff, "live", raising=False)
    assert panel.from_engine(repo, 100, "now", None, -1) is None
    assert panel.main(repo, 100, protocol=2) == 0
    out = json.loads(capsys.readouterr().out)
    assert "version" not in out and out["rows"]


def test_an_engine_that_fails_is_not_the_panels_failure(repo, live, capsys):
    def broken(view, since):
        raise ConnectionResetError("gone")
    live.answer = broken
    assert panel.from_engine(repo, 100, "now", None, -1) is None
    live.answer = lambda view, since: {"version": 1}           # not a page
    assert panel.from_engine(repo, 100, "now", None, -1) is None


def test_the_module_entry_point_takes_the_thin_path_without_loading_the_cli(tmp_path):
    """`python -m system_diff panel ...` answers through panel.fast: the CLI (and with it every extractor) is not
    imported. With no engine module answering in that process, it falls back to the CLI's own read."""
    code = ("import sys, runpy; sys.argv = ['system_diff', 'panel', '--protocol', '2', '--view', 'nowhere'];\n"
            "from system_diff import panel; called = []\n"
            "real = panel.fast\n"
            "panel.fast = lambda argv: called.append(list(argv)) or 0\n"
            "try:\n    runpy.run_module('system_diff', run_name='__main__')\nexcept SystemExit as e:\n"
            "    print(called, e.code, 'system_diff.cli' in sys.modules)\n")
    got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert got.stdout.strip() == "[['panel', '--protocol', '2', '--view', 'nowhere']] 0 False", got.stderr


# --- the hooks: Claude's edits reach the engine at once --------------------------------------------------------------


def test_an_edit_tells_the_engine_which_files_changed(repo, live):
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "cwd": str(repo),
               "tool_input": {"file_path": str(repo / "app" / "api" / "routes.py"), "old_string": "a",
                              "new_string": "b"}}
    agent.on_tool(repo, payload)
    assert live.touched == [(repo, ["app/api/routes.py"])]
    live.touched.clear()
    agent.on_tool(repo, {"tool_name": "Read", "tool_input": {"file_path": str(repo / "app" / "db" / "repo.py")}})
    assert live.touched == []                                   # a read changes nothing
    note = {"tool_name": "NotebookEdit", "tool_input": {"notebook_path": str(repo / "nb" / "a.ipynb")}}
    assert agent.edited_paths(repo, note) == ["nb/a.ipynb"]
    multi = {"tool_name": "MultiEdit", "tool_input": {"file_path": str(repo / "app" / "core" / "rules.py")},
             "tool_response": {"filePath": str(repo / "app" / "core" / "rules.py")}}
    assert agent.edited_paths(repo, multi) == ["app/core/rules.py"]
    assert agent.edited_paths(repo, {"tool_name": "Write", "tool_input": {"file_path": "/elsewhere/x.py"}}) == []


def test_the_hooks_never_fail_or_wait_on_the_engine(repo, live, monkeypatch):
    def broken(root, paths):
        raise OSError("refused")
    monkeypatch.setattr(live, "touch", broken)
    agent.touch(repo, ["app/api/routes.py"])                   # nothing raised
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Write", "cwd": str(repo),
               "tool_input": {"file_path": str(repo / "app" / "api" / "routes.py"), "content": "x = 1\n"}}
    agent.run_hook(json.dumps(payload), emit=lambda s: None)  # nor through the hook's whole run

    def stuck(root, paths):
        time.sleep(5)
    monkeypatch.setattr(live, "touch", stuck)
    began = time.monotonic()
    agent.touch(repo, ["app/api/routes.py"])
    assert time.monotonic() - began < 1.0                      # at most half a second, whatever the engine does
    monkeypatch.setitem(sys.modules, "system_diff.live", None)  # no engine module at all
    monkeypatch.delattr(system_diff, "live", raising=False)
    agent.touch(repo, ["app/api/routes.py"])


# --- the terminal view --------------------------------------------------------------------------------------------


def test_the_terminal_view_follows_the_engine_with_a_long_poll(monkeypatch):
    version = {"n": 3}
    asked = []
    edit = threading.Event()

    def ask(width, view, part, zoom, since):
        asked.append((view, since))
        if since is not None:
            edit.wait(5)                                       # the engine holds it until Claude's edit lands
            edit.clear()
            version["n"] += 1
        return {**a_page(view, f"{view} at version {version['n']}"), "version": version["n"]}

    def never(*a, **k):
        raise AssertionError("read in-process while an engine answers")
    monkeypatch.setattr(panel, "build", never)
    app = tui.Tui(Path("."), ask=ask, size=lambda: (60, 20))
    app.start()
    try:
        def wait_for(text):
            for _ in range(300):
                app.tick()
                if app.page and app.page["rows"][0][0][0] == text:
                    return True
                time.sleep(0.01)
            return False
        assert wait_for("now at version 3")
        assert asked[0] == ("now", None)
        for _ in range(300):                                   # the long poll is waiting, since the version shown
            if ("now", 3) in asked:
                break
            time.sleep(0.01)
        assert ("now", 3) in asked
        edit.set()
        assert wait_for("now at version 4")
        app.handle("r")                                        # a key asks at once, beside the waiting long poll
        assert wait_for("review at version 4")
    finally:
        app.stop()
        edit.set()


def test_the_terminal_view_without_an_engine_starts_one_and_reads_as_before(monkeypatch):
    started, built = [], []
    monkeypatch.setattr(panel, "start_engine", lambda root: started.append(root))
    monkeypatch.setattr(panel, "build", lambda root, width, view, part, zoom, take=True:
                        built.append(take) or a_page(view, "read here"))
    app = tui.Tui(Path("."), ask=lambda *a: None, size=lambda: (60, 20))
    page = app._pane(60, "now", "", -1)
    assert page["rows"][0][0][0] == "read here" and built == [False]   # never taking what the web page sent
    app._pane(60, "now", "", -1)
    assert started == [Path(".")]                              # once
    assert app._version is None                                # so the clock goes on looking


def test_the_cli_takes_since_and_take_too_for_an_installed_system_diff(repo, live, capsys):
    """The `system-diff` console script goes through the CLI, not panel.fast: it long-polls as well."""
    from system_diff.cli import main
    live.answer = lambda view, since: {"version": (since or 0) + 1, "page": a_page(view)}
    assert main(["--repo", str(repo), "panel", "--protocol", "2", "--since", "8", "--no-take", "--view", "now",
                 "--width", "80"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 9
    assert live.asked[-1]["since"] == 8 and live.asked[-1]["take"] is False
    assert main(["--repo", str(repo), "panel", "--protocol", "2"]) == 0
    capsys.readouterr()
    assert live.asked[-1]["since"] is None and live.asked[-1]["take"] is True


# --- objectives: what the person optimises for, in Claude's context at each prompt ---------------------------------


def test_each_prompt_starts_learning_objectives_and_gives_claude_what_is_known(repo, monkeypatch):
    from system_diff import objectives
    ensured = []
    monkeypatch.setattr(objectives, "ensure", lambda root, *a, **k: ensured.append(root) or False)
    said = "What this person has been optimising for in this repository: 1. fast on large repos."
    monkeypatch.setattr(objectives, "context", lambda root: said)
    got = agent.on_prompt(repo, {"prompt": "make the pane faster", "session_id": "s"})
    assert ensured == [repo] and got["context"].endswith(said) and agent.agents.ORCHESTRATE in got["context"]
    sub = agent.on_prompt(repo, {"prompt": "a subagent's task", "agent_id": "a1"})
    assert sub == {"context": said}                       # a subagent is not asked to plan, but is told
    monkeypatch.setattr(objectives, "context", lambda root: "")
    assert agent.on_prompt(repo, {"prompt": "again"})["context"] == agent.agents.ORCHESTRATE
    assert agent.on_prompt(repo, {"prompt": "again", "agent_id": "a1"}) is None


def test_objectives_that_fail_never_fail_the_prompt(repo, monkeypatch):
    from system_diff import objectives

    def broken(*a, **k):
        raise RuntimeError("no model")
    monkeypatch.setattr(objectives, "ensure", broken)
    monkeypatch.setattr(objectives, "context", broken)
    assert agent.on_prompt(repo, {"prompt": "hello"})["context"] == agent.agents.ORCHESTRATE
    monkeypatch.setitem(sys.modules, "system_diff.objectives", None)
    monkeypatch.delattr(system_diff, "objectives", raising=False)
    assert agent.objectives_context(repo) == ""
