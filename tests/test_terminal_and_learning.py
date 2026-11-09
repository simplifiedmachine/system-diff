"""Following an AI coding agent as it works: the turn, the files it reads and edits, the part it is on (the focus), the
status line; and what the person sees and learns: the map and a part zoomed in on, in the terminal, with what the part
is, what is changing in it, and the topics behind it."""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest

from system_diff import activity, agent, config, graph, learn, statusline, textmap
from system_diff import compare as cmp
from system_diff.cli import main
from system_diff.diff import short
from system_diff.extract import Code, outside_imports
from system_diff.sources import GitSource

HEALTH = 'from fastapi import APIRouter\nrouter = APIRouter()\n\n\n@router.get("/health")\ndef health():\n    return "ok"\n'


def _hook(repo: Path, said: list, event: str, **fields) -> int:
    return agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": event, "session_id": "s1", **fields}),
                          said.append)


def test_the_hooks_follow_a_turn_and_say_only_what_it_changed(repo, files):
    said: list = []
    assert _hook(repo, said, "UserPromptSubmit", prompt="Add a  health\ncheck") == 0
    assert cmp.turn_sha(repo)
    files(repo, {"app/api/health.py": HEALTH})
    _hook(repo, said, "PostToolUse", tool_name="Write", tool_input={"file_path": str(repo / "app/api/health.py")})
    assert [json.loads(s)["systemMessage"] for s in said] == [   # the edit that changed the structure says so
        "System diff: API now answers GET /health · a new way in: it needs the same checks as the others"]
    _hook(repo, said, "PostToolUse", tool_name="Read", tool_input={"file_path": str(repo / "app/core/rules.py")})
    _hook(repo, said, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"})            # not a file tool
    _hook(repo, said, "PostToolUse", tool_name="Read", tool_input={"file_path": str(repo.parent / "x.py")})  # outside
    events = activity.events(repo)
    assert [(e["event"], e.get("path")) for e in events] == [
        ("prompt", None), ("edit", "app/api/health.py"), ("read", "app/core/rules.py")]
    assert events[0]["text"] == "Add a health check"
    now = activity.now(events, Code(config.load(root=repo)))
    assert (now["part"], now["doing"], now["working"]) == ("api", "editing", True)   # the edit, not the later read

    state = activity.read_state(repo)                                     # the edit's hook already looked
    assert state["turn"]["story"][0]["text"] == "API now answers GET /health."
    brief = activity.load_brief(repo, "api")
    assert brief["role"].startswith("an entry point") and [t[0] for t in brief["topics"]] == [
        "Entry points and APIs", "FastAPI"]

    assert _hook(repo, said, "Stop", last_assistant_message="Added GET /health.") == 0
    assert len(said) == 2 and json.loads(said[1])["systemMessage"] == (
        "System diff, this turn: API now answers GET /health (a new way in: it needs the same checks as the others). "
        "`system-diff tui` explains them.")
    assert activity.read_state(repo)["since"] == "HEAD (no checkpoint yet)"

    _hook(repo, said, "UserPromptSubmit", prompt="Tidy the rules")       # code edited, structure the same
    files(repo, {"app/core/rules.py": (repo / "app/core/rules.py").read_text() + "# tidied\n"})
    _hook(repo, said, "PostToolUse", tool_name="Edit", tool_input={"file_path": str(repo / "app/core/rules.py")})
    _hook(repo, said, "Stop")
    assert len(said) == 3 and json.loads(said[2])["systemMessage"] == (
        "System diff, this turn: Claude edited Core; nothing in the system's structure changed.")

    _hook(repo, said, "UserPromptSubmit", prompt="Explain the API")      # a turn that edits no code
    assert activity.read_state(repo)["turn"]["story"] == []
    _hook(repo, said, "Stop", last_assistant_message="The API is …")
    assert len(said) == 3                                                  # says nothing
    line: list = []
    assert statusline.statusline(json.dumps({"workspace": {"current_dir": str(repo)}}), line.append, color=False) == 0
    rows = line[0].splitlines()
    assert rows[0] == "Last worked on Core · logic: the rules and steps of what the system does, between its entry " \
                      "points and its data"
    assert rows[1].startswith("Learn: ") and len(rows) == 2


def test_the_status_line_tells_what_is_going_on_and_teaches_a_topic_at_a_time():
    now = {"part": "api", "doing": "editing", "working": True}
    state = {"turn": {"story": [{"text": "API now answers GET /health.", "why": "a new way in"},
                                {"text": "Core now reads MAIL_FROM.", "why": "set it where it runs"}]}}
    brief = {"label": "API", "role": "an entry point", "topics": [["FastAPI", "A web framework: routes."],
                                                                  ["HTTP endpoints", "A method and a path. More."]]}
    rows = [textmap.plain([r]) for r in statusline.status_lines(now, state, brief, {}, clock=0)]
    assert rows == ["✎ Editing API · an entry point",
                    "This turn: API now answers GET /health · a new way in (and 1 more)",
                    "Learn: FastAPI · A web framework."]
    later = textmap.plain([statusline.status_lines(now, state, brief, {}, clock=statusline.TIP_EVERY)[2]])
    assert later == "Learn: HTTP endpoints · A method and a path."     # a new topic every TIP_EVERY seconds
    quiet = statusline.status_lines({"part": None}, {}, {}, {})
    assert len(quiet) == 1 and "will show here" in textmap.plain(quiet)
    long = statusline.status_lines(now, state, {**brief, "role": "x" * 400}, {}, clock=0)
    assert len(textmap.plain([long[0]])) == statusline.WIDE


def test_without_the_turn_the_stop_hook_says_everything_since_the_checkpoint(repo, files):
    said: list = []
    files(repo, {"app/api/health.py": HEALTH})
    _hook(repo, said, "Stop")
    assert json.loads(said[0])["systemMessage"].startswith("System diff since HEAD (no checkpoint yet): Adds 1 endpoint")
    with pytest.raises(ValueError, match="no turn has started"):
        cmp.compare(repo, "turn", ".")


def test_the_status_line_is_quiet_outside_a_repository_and_colours_inside(repo, tmp_path):
    line: list = []
    outside = tmp_path / "plain-folder"
    outside.mkdir()
    assert statusline.statusline(json.dumps({"cwd": str(outside)}), line.append) == 0 and line == []
    route = {"op": "add", "entity": "route", "id": "GET /x", "label": "GET /x", "detail": ["API"],
             "impact": ["new-interface"]}
    activity.write_state(repo, {"changes": [route], "headline": "x"}, turn={"changes": [route], "headline": "x"})
    statusline.statusline(json.dumps({"cwd": str(repo)}), line.append)
    assert "Last turn: \x1b[0m\x1b[1mAPI now answers GET /x\x1b[0m" in line[0]


def test_the_focus_is_the_latest_edit_of_the_turn_else_its_latest_read():
    code = Code(config.merge(config.DEFAULTS, {"code": {"roots": ["app/"]}, "tests": ["tests/**"]}))
    ev = lambda at, kind, path="", **k: {"at": at, "event": kind, "path": path, **k}  # noqa: E731
    seen = [ev(1, "edit", "app/old/a.py"), ev(2, "prompt", text="next"), ev(3, "read", "app/x/b.py"),
            ev(4, "read", "README.md"), ev(5, "edit", "tests/test_b.py")]
    now = activity.now(seen, code)
    assert (now["part"], now["doing"]) == ("app/x", "reading")          # tests and docs are not parts
    assert now["latest"]["path"] == "tests/test_b.py" and now["prompt"] == "next" and now["working"]
    now = activity.now(seen + [ev(6, "edit", "app/x/c.py"), ev(7, "read", "app/y/d.py"), ev(8, "stop", text="done")],
                       code)
    assert (now["part"], now["doing"], now["said"], now["working"]) == ("app/x", "editing", "done", False)
    assert activity.now([], code)["part"] is None


def test_an_edit_tells_only_what_it_added_even_past_the_first_six(repo):
    flows = [{"op": "add", "entity": "flow", "id": f"a>b{i}", "label": f"A → B{i}", "detail": [],
              "impact": ["new-flow"]} for i in range(9)]
    activity.write_state(repo, None, turn={"changes": flows, "headline": "x"})
    turn = activity.read_state(repo)["turn"]
    assert (turn["count"], len(turn["story"]), len(turn["told"])) == (9, 6, 9)
    route = {"op": "add", "entity": "route", "id": "GET /y", "label": "GET /y", "detail": ["A"],
             "impact": ["new-interface"]}
    assert agent.step_message(turn["told"], learn.story({"changes": flows + [route]})) == (
        "System diff: A now answers GET /y · a new way in: it needs the same checks as the others")
    assert agent.step_message(turn["told"], learn.story({"changes": flows})) is None   # nothing new: nothing said
    rows = statusline.status_lines({"part": None, "working": True}, {"turn": turn}, {}, {})
    assert textmap.plain([rows[0]]).endswith("(and 8 more)")


def test_the_live_views_tell_what_just_happened_step_by_step(repo, files):
    from system_diff import watch
    w = watch.Watcher(repo, interval=60)
    w.tick()
    activity.record(repo, {"event": "prompt", "text": "add a health check"})
    for name in ("rules.py", "__init__.py"):
        activity.record(repo, {"event": "read", "path": f"app/core/{name}"})
    files(repo, {"app/api/health.py": HEALTH})
    activity.record(repo, {"event": "edit", "path": "app/api/health.py"})
    w.tick()
    feed = w.snapshot_state()["feed"]
    assert [(f["kind"], f["text"]) for f in feed] == [
        ("change", "API now answers GET /health."),               # what the step did to the system, newest first
        ("edit", "Claude edits API (app/api/health.py)"),
        ("read", "Claude reads Core (2 files)"),                  # a run of reads in one part, told once
        ("prompt", "You asked: add a health check")]
    assert feed[0]["why"].startswith("a new way in") and w.snapshot_view().feed == feed


def test_a_change_said_plainly_with_why_it_matters():
    def said(op, entity, label, detail=(), impact=""):
        return learn.say({"op": op, "entity": entity, "label": label, "detail": list(detail),
                          "impact": [impact] if impact else []})
    assert said("add", "flow", "API → Billing", impact="new-flow") == {
        "text": "API now uses Billing.", "why": "a change in Billing can now break API", "impact": "new-flow",
        "weight": learn.WEIGHT["new-flow"]}
    assert said("remove", "route", "GET /x", ["API"], "breaking-interface")["text"] == "API no longer answers GET /x."
    assert said("add", "external", "Store → PostgreSQL", impact="new-external")["text"] == (
        "Store now talks to PostgreSQL, outside the system.")
    assert said("change", "table", "public.orders", ["+ column note"], "data-schema-change")["text"] == (
        "The table public.orders changes: + column note.")
    told = learn.story({"changes": [
        {"op": "add", "entity": "flow", "label": "A → B", "detail": [], "impact": ["new-flow"]},
        {"op": "change", "entity": "component", "label": "A", "detail": ["+1 −0 lines in 1 file"], "impact": []},
        {"op": "remove", "entity": "route", "label": "GET /x", "detail": ["A"], "impact": ["breaking-interface"]}],
        "impacts": {"test-gap": 1}}, final=True)
    assert [t["text"] for t in told] == ["A no longer answers GET /x.", "A now uses B.", "Code changed and no test did."]


def test_the_activity_log_keeps_the_newest_events(repo, monkeypatch):
    monkeypatch.setattr(activity, "MAX_BYTES", 2000)
    monkeypatch.setattr(activity, "KEEP", 10)
    for i in range(60):
        activity.record(repo, {"event": "read", "path": f"app/core/f{i}.py"})
    paths = [e["path"] for e in activity.events(repo)]
    assert paths[-1] == "app/core/f59.py" and len(paths) < 60


def test_the_map_and_a_part_zoomed_in_on_in_the_terminal(repo):
    c = cmp.compare(repo, "HEAD~1", "HEAD")
    marks = graph.change_marks(c.diff, c.before, c.after)
    picture = textmap.plain(textmap.render_map(graph.overview(c.after, c.cfg, marks, c.before), 100,
                                               focus="api", picked="core"))
    assert picture.splitlines()[0] == "WHAT PEOPLE USE" and "┃ ~ API" in picture and "│ + app/jobs" in picture
    assert "Core calls Store; called by API, app/jobs" in picture and "┏━┓ being worked on" in picture
    zoom = graph.neighbourhood(c.after, c.cfg, "api", marks, c.before)
    assert (zoom["callers"], zoom["callees"]) == (["web"], ["core"])
    wide = textmap.plain(textmap.render_zoom(zoom, 100))
    assert "│ Web app    ├────▸┨ ~ API" in wide and "┃ + DELETE /api/orders/{id}" in wide
    narrow = textmap.plain(textmap.render_zoom(zoom, 40))
    assert max(map(len, narrow.splitlines())) <= 40 and narrow.index("Web app") < narrow.index("~ API")
    db = textmap.plain(textmap.render_zoom(graph.neighbourhood(c.after, c.cfg, "x:postgres", marks, c.before), 100))
    assert "USED BY" in db and "~ public.orders: + note" in db and "+ 002_note.sql" in db
    assert "\x1b[33m" in textmap.ansi(textmap.render_zoom(zoom, 100))   # changed, in yellow
    assert graph.neighbourhood(c.after, c.cfg, "nowhere", marks, c.before) is None


def test_a_lesson_says_what_a_part_is_what_is_changing_and_what_to_learn(repo):
    c = cmp.compare(repo, "HEAD~1", "HEAD")
    les = learn.lesson(c, "api", GitSource(c.head_sha, repo))
    what = " ".join(les["what"])
    assert what.startswith("API is an entry point") and "It serves 3 endpoints" in what and "Web app calls it" in what
    changing = {x["text"]: x["why"] for x in les["changing"]}
    assert "a new way into the system" in changing["+ endpoint DELETE /api/orders/{id}"]
    assert "! no test file changed with it" in changing
    keys = [t["key"] for t in les["topics"]]
    assert keys[:2] == ["http-endpoint", "tests"] and "fastapi" in keys      # what the change touches comes first
    fastapi = next(t for t in les["topics"] if t["key"] == "fastapi")
    assert fastapi["here"] == "API imports it in 1 file." and fastapi["link"].startswith("https://")
    db = learn.lesson(c, "x:postgres", GitSource(c.head_sha, repo))
    (schema,) = db["changing"]                    # the table and its migration: one change, one reason
    assert schema["text"] == "~ table public.orders: + column note; + migration 002_note.sql"
    assert "## API" in learn.to_markdown(les) and "[Read more](https://fastapi.tiangolo.com/)" in learn.to_markdown(les)
    assert [learn.find_part(c, n, repo) for n in ("web app", "app/core/rules.py", "PostgreSQL", "nope")] == [
        "web", "core", "x:postgres", None]
    topics = learn.system_topics(GitSource(c.head_sha, repo), c.after, c.cfg)
    assert [(t["title"], t["used_by"]) for t in topics] == [("FastAPI", ["API"])]


def test_outside_imports_are_packages_and_the_standard_library_not_the_repository():
    own = {"app", "system_diff"}
    text = ("import os, json\nfrom http.server import X\nfrom app.core import rules\nfrom . import sibling\n"
            "import numpy as np\nfrom __future__ import annotations\n")
    assert outside_imports("app/a.py", text, own) == ["http.server", "json", "numpy", "os"]
    ts = 'import React from "react";\nimport { z } from "zod/v4";\nimport x from "@/lib/x";\nimport { q } from "@tanstack/react-query";\n'
    assert outside_imports("web/a.tsx", ts, own, {"@/": "web/src/"}) == ["@tanstack/react-query", "react", "zod"]


def test_the_diff_in_a_few_words():
    changes = [{"op": "add", "entity": "route"}, {"op": "add", "entity": "route"}, {"op": "change", "entity": "table"},
               {"op": "remove", "entity": "component"}, {"op": "add", "entity": "flow"}]
    assert short(changes) == "+2 endpoints +1 connection ~1 table …"
    assert short(changes, most=9) == "+2 endpoints +1 connection ~1 table −1 part" and short([]) == ""


def test_each_worktree_keeps_its_own_checkpoint_and_an_old_shared_one_still_counts(repo, tmp_path):
    from conftest import git_
    other = tmp_path / "other"
    git_(repo, "worktree", "add", "-q", "--detach", str(other), "HEAD~1")
    git_(repo, "update-ref", cmp.OLD_CHECKPOINT_REF, "HEAD~1")              # what an older version marked
    assert cmp.checkpoint_sha(repo) == cmp.checkpoint_sha(other) == git_(repo, "rev-parse", "HEAD~1").strip()
    cmp.checkpoint(repo, "HEAD")                                             # marked as seen in one worktree...
    assert cmp.checkpoint_sha(repo) == git_(repo, "rev-parse", "HEAD").strip()
    assert cmp.checkpoint_sha(other) == git_(repo, "rev-parse", "HEAD~1").strip()   # ...not in the other


def test_the_pane_beside_the_chat_gets_its_rows_as_json(repo, files, capsys):
    activity.record(repo, {"event": "prompt", "text": "add a health check"})
    files(repo, {"app/api/health.py": HEALTH})
    activity.record(repo, {"event": "edit", "path": "app/api/health.py"})
    assert main(["--repo", str(repo), "panel", "--width", "90"]) == 0
    out = json.loads(capsys.readouterr().out)
    got = out["head"] + out["rows"]
    text = ["".join(run[0] for run in row) for row in got]
    assert text[0] == "● Claude is working  ✎ Editing API" and "  You asked  add a health check" in text
    urgent = text.index("▲ NEEDS YOUR ATTENTION")                           # a new way in: the person should know
    assert text[urgent + 1] == " + API now answers GET /health" and "same checks" in text[urgent + 2]
    styles = {style for row in got for _, style in row}
    assert {"accent bold", "attention", "heading", "muted"} <= styles
    zoom = out["diagram"]                                                   # the pane draws it, blocks to pick
    assert zoom["kind"] == "zoom" and zoom["part"]["title"] == "API" and zoom["part"]["agent"]
    assert [b["title"] for b in zoom["callers"]] == ["Web app"] and [b["id"] for b in zoom["callees"]] == ["core"]
    assert ["GET /health", "added"] in zoom["part"]["lines"]                # its own endpoints, what is new marked
    assert "WHAT JUST HAPPENED" in text
    assert all(isinstance(run, list) and len(run) == 2 for row in got for run in row)

    def view(*args):
        assert main(["--repo", str(repo), "panel", "--width", "90", *args]) == 0
        out = json.loads(capsys.readouterr().out)
        return out, ["".join(run[0] for run in row) for row in out["head"] + out["rows"]]
    now, text = view("--part", "core")                                     # zoomed in where the person picked
    assert now["part"] == "core" and {p["id"] for p in now["picks"]} == {"api", "store", "app/jobs"}
    assert "ZOOMED IN ON CORE  (you picked it)" in text and any(t.startswith("Core is logic") for t in text)
    the_map, text = view("--view", "map")
    assert {p["id"] for p in the_map["picks"]} >= {"api", "core", "store", "web"}
    assert the_map["diagram"]["columns"][0]["heading"] == "What people use"
    _, text = view("--view", "changes")
    assert text[0] == "SINCE HEAD" and any("API now answers GET /health" in t for t in text)
    _, text = view("--view", "topics")
    assert text[0] == "WHAT THIS SYSTEM IS BUILT WITH" and any("FastAPI" in t for t in text)


def test_the_working_tree_is_read_once_against_several_points(repo, files):
    files(repo, {"app/api/health.py": HEALTH})
    cmp.mark_turn(repo)
    files(repo, {"app/api/ready.py": HEALTH.replace("health", "ready")})
    since, turn = cmp.compare_worktree(repo, ["HEAD", "turn"])
    assert since.head_sha == turn.head_sha and turn.base == "the start of this turn"
    assert "GET /health" in since.diff["plan"] and "GET /health" not in turn.diff["plan"]
    assert "GET /ready" in turn.diff["plan"] and since.after["ref"] == turn.after["ref"] == "working tree"
    again = cmp.compare(repo, "HEAD", "HEAD")
    assert again.before["ref"] == again.after["ref"] != "working tree"     # a cached blueprint keeps its own label


def test_show_prints_the_map_and_a_part_with_its_lesson(repo, capsys):
    assert main(["--repo", str(repo), "show", "--plain", "--width", "100", "--base", "HEAD~1"]) == 0
    out = capsys.readouterr().out
    assert "ENTRY POINTS" in out and "Since HEAD~1: Adds 1 part (app/jobs)" in out
    assert main(["--repo", str(repo), "show", "API", "--plain", "--width", "100", "--base", "HEAD~1"]) == 0
    out = capsys.readouterr().out
    assert "CALLED BY" in out and "## API" in out and "FastAPI" in out
    assert main(["--repo", str(repo), "show", "nothing-like-it", "--plain"]) == 1
    assert "the parts: API, Core, Store, Web app" in capsys.readouterr().err


def test_setup_wires_three_hooks_and_a_status_line_and_leaves_someone_elses_alone(repo, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(agent.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "system-diff" else None)
    said: list = []
    agent.setup_claude(repo, "local", out=said.append)
    path = repo / ".claude" / "settings.local.json"
    data = json.loads(path.read_text())
    assert data["hooks"]["PostToolUse"] == [   # reads noted in the background; an edit waited for, so it can speak
        {"matcher": "Read", "hooks": [{"type": "command", "command": "system-diff hook", "async": True}]},
        {"matcher": "Edit|Write|MultiEdit|NotebookEdit", "hooks": [
            {"type": "command", "command": "system-diff hook", "timeout": 15}]}]
    assert data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["async"] is True
    assert data["statusLine"] == {"type": "command", "command": "system-diff statusline"}
    agent.setup_claude(repo, "local", undo=True, out=said.append)
    assert json.loads(path.read_text()) == {}

    older = {"hooks": {"PostToolUse": [{"matcher": "Edit|Write|Read", "hooks": [   # what an older setup wrote
        {"type": "command", "command": "system-diff hook", "async": True}]}, {"hooks": [{"command": "mine"}]}]}}
    path.write_text(json.dumps(older))
    said.clear()
    agent.setup_claude(repo, "local", out=said.append)
    post = json.loads(path.read_text())["hooks"]["PostToolUse"]
    assert post[0] == {"hooks": [{"command": "mine"}]} and [e["matcher"] for e in post[1:]] == [
        "Read", "Edit|Write|MultiEdit|NotebookEdit"]
    assert any(s.startswith("updated the PostToolUse hook") for s in said)
    agent.setup_claude(repo, "local", undo=True, out=said.append)

    user = home / ".claude" / "settings.json"
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-line"}}))
    said.clear()
    agent.setup_claude(repo, "project", out=said.append)
    assert "statusLine" not in json.loads((repo / ".claude" / "settings.json").read_text())
    assert any("left your status line" in s and "settings.json" in s for s in said)


def test_a_session_opens_the_terminal_view_beside_claude_where_the_terminal_can(repo, monkeypatch):
    from system_diff import companion
    monkeypatch.setattr(companion.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(companion.importlib.util, "find_spec", lambda name: object())   # as if Textual were there
    ran, old_tmux = [], []

    def run(cmd):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if old_tmux and "-l" in cmd else 0)
    monkeypatch.setattr(companion, "launch", run)
    start = {"cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"}
    assert agent.on_start(repo, start, env={}) is None and not ran               # a plain terminal: nothing to do
    tmux = {"TMUX": "/tmp/tmux-1/default,1,0"}
    said = agent.on_start(repo, start, env=tmux)
    assert said.startswith("system-diff: the terminal view is open in a tmux pane beside Claude")
    assert ran[0][:6] == ["tmux", "split-window", "-h", "-d", "-l", "42%"] and ran[0][-1].endswith("system_diff tui")
    for skip in ({**start, "source": "compact"}, start):
        env = tmux if skip is not start else {**tmux, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}   # `claude -p`
        assert agent.on_start(repo, skip, env=env) is None and len(ran) == 1
    companion.beat(repo)                                                          # a view is already open
    assert companion.running(repo) and agent.on_start(repo, start, env=tmux) is None and len(ran) == 1
    companion.gone(repo)
    ran.clear()
    old_tmux.append(True)                                                         # a tmux older than 3.1
    assert companion.open_beside(repo, tmux)
    assert ran[1][:6] == ["tmux", "split-window", "-h", "-d", "-p", "42"]
    assert companion.pane_command("wezterm", ["x"], repo)[:3] == ["wezterm", "cli", "split-pane"]
    assert companion.where({"TERM_PROGRAM": "vscode"}) == "vscode" and companion.where({}) is None


def test_in_vscode_setup_adds_a_task_that_opens_the_view_with_the_folder(repo, tmp_path, monkeypatch):
    from system_diff import companion
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/system-diff" if name == "system-diff" else None)
    said: list = []
    agent.setup_claude(repo, "local", out=said.append)
    tasks = json.loads((repo / ".vscode" / "tasks.json").read_text())["tasks"]
    assert tasks == [companion.vscode_task("system-diff tui")]
    assert tasks[0]["runOptions"] == {"runOn": "folderOpen"}
    assert "/.vscode/tasks.json" in (repo / ".git" / "info" / "exclude").read_text()   # kept out of git status
    agent.setup_claude(repo, "local", out=said.append)
    assert any("VS Code task is already" in s for s in said)
    agent.setup_claude(repo, "local", undo=True, out=said.append)
    assert not (repo / ".vscode").exists()
    theirs = repo / ".vscode" / "tasks.json"
    theirs.parent.mkdir()
    theirs.write_text('{\n  // mine\n  "version": "2.0.0", "tasks": []\n}\n')
    agent.setup_claude(repo, "local", out=said.append)
    assert "// mine" in theirs.read_text() and any("left" in s and "tasks.json alone" in s for s in said)


def test_the_terminal_view_follows_the_agent_and_zooms_where_you_pick(repo, files):
    pytest.importorskip("textual")
    from system_diff import companion
    from system_diff.tui import SystemDiffApp

    async def until(pilot, ok, seconds=15.0):
        end = time.time() + seconds
        while not ok() and time.time() < end:
            await pilot.pause(0.1)
        assert ok()

    async def drive():
        app = SystemDiffApp(repo, interval=0.2)
        async with app.run_test(size=(120, 50)) as pilot:
            await until(pilot, lambda: app.w.snapshot_view().looks > 0)
            assert companion.running(repo)                            # its heartbeat: no session opens another
            files(repo, {"app/api/health.py": HEALTH})
            activity.record(repo, {"event": "edit", "path": "app/api/health.py"})
            await until(pilot, lambda: app.part == "api")             # it follows the agent
            assert "Editing API" in str(app.query_one("#doing").render())
            await until(pilot, lambda: "API now answers GET /health" in str(app.query_one("#feed").render()))
            await pilot.press("m")
            app.map_pick = "core"
            await pilot.press("enter")                                # zoom in on a part picked on the map
            assert (app.part, app.following, app.query_one("TabbedContent").active) == ("core", False, "now")
            await pilot.press("down")
            await pilot.press("enter")                                # then on one of its neighbours
            assert app.part != "core" and app.history == ["api", "core"]
            await pilot.press("escape")
            assert app.part == "core"
            await pilot.press("f")
            assert app.following and app.part == "api"
            await pilot.press("s")                                    # mark as seen
            await until(pilot, lambda: app.w.snapshot_state()["changes"] == 0)
            await pilot.press("t")
            await until(pilot, lambda: "FastAPI" in app.topics_text)
    asyncio.run(drive())
    assert not companion.running(repo)                                # closed: a new session may open one again


def test_the_mcp_server_explains_a_part_with_a_diagram(repo, files, monkeypatch):
    pytest.importorskip("mcp")
    from system_diff import mcp_server
    monkeypatch.chdir(repo)
    files(repo, {"app/api/health.py": HEALTH})
    got = mcp_server.tool_explain(repo, "api")
    assert got["part"] == "API" and "┏" in got["diagram"] and "GET /health" in got["diagram"]
    assert any(t["title"] == "FastAPI" for t in got["topics"])
    assert "parts" in mcp_server.tool_explain(repo) and "error" in mcp_server.tool_explain(repo, "nope")
    activity.record(repo, {"event": "read", "path": "app/core/rules.py"})
    assert mcp_server.tool_explain(repo)["part"] == "Core"                 # the part being worked on
    assert "ENTRY POINTS" in mcp_server.tool_map(repo, diagram=True)["diagram"]
    assert "+ GET /health" in mcp_server.tool_diff(repo, diagram=True)["diagram"]


def test_setup_moves_an_older_skill_out_of_the_way_of_the_panes_command(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(agent.shutil, "which", lambda name: None)
    skills = repo / ".claude" / "skills"
    old = skills / "system-diff" / "SKILL.md"                        # what an older setup wrote: it held /system-diff
    old.parent.mkdir(parents=True)
    old.write_text(agent.SKILL.replace("name: system-diff-explain", "name: system-diff"))
    said: list = []
    agent.setup_claude(repo, "local", out=said.append)
    assert not (skills / "system-diff").exists()
    assert (skills / "system-diff-explain" / "SKILL.md").read_text() == agent.SKILL
    mine = skills / "system-diff" / "SKILL.md"                       # one someone else wrote is never touched
    mine.parent.mkdir()
    mine.write_text("---\nname: system-diff\n---\nmine\n")
    agent.setup_claude(repo, "local", undo=True, out=said.append)
    assert mine.read_text().endswith("mine\n") and not (skills / "system-diff-explain").exists()


def test_the_pane_plugin_ships_with_the_package_at_the_same_version():
    import system_diff
    root = Path(__file__).resolve().parent.parent
    plugin = json.loads((root / "mods/system-diff-pane/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    market = json.loads((root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    assert plugin["version"] == system_diff.__version__        # a release bumps both: Claude Code sees the update
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [("system-diff-pane", "./mods/system-diff-pane")]
