"""A slow page never holds the person up: a first drawing answers within moments with a drawing page and finishes in
the background; an extension's view is drawn outside the pane's lock; the Studio never answers 502 for a slow one."""
import threading
import time

from system_diff import live, watch


class _Engine:
    def __init__(self, root):
        self.root, self.version = root, 1
        self.done, self.ready, self.stopped = threading.Event(), threading.Event(), threading.Event()
        self.done.set(); self.ready.set()

    def wait(self, seen, timeout=5.0):
        self.stopped.wait(timeout)
        return self.version

    def progress(self):
        return {}


def test_a_slow_first_drawing_answers_at_once_and_finishes_later(monkeypatch, tmp_path):
    monkeypatch.setattr(live, "save_page", lambda *a, **k: None)
    monkeypatch.setattr(live, "FIRST_WAIT", 0.5)
    monkeypatch.setattr(live, "BUSY_WAIT", 0.2)
    slow = threading.Event()

    def build(key):
        if key[1] == "slow":
            slow.wait(5)
        return {"view": key[1], "drawn": True}
    pages = live.Pages(_Engine(tmp_path), build, drawing=lambda key: {"view": key[1], "drawing": True})
    t = time.monotonic()
    version, page = pages.get(("web", "slow", 0, ""))
    assert time.monotonic() - t < 2 and version == -1 and page["drawing"]
    t = time.monotonic()
    assert pages.get(("web", "fast", 0, ""))[1]["drawn"] and time.monotonic() - t < 1   # another view, at once
    assert pages.get(("web", "slow", 0, ""))[1].get("drawing")                          # still drawing
    slow.set()
    time.sleep(0.3)
    assert pages.get(("web", "slow", 0, ""))[1]["drawn"]


def test_an_extensions_view_is_drawn_outside_the_panes_lock(monkeypatch, tmp_path):
    from system_diff import extensions, pane

    class W:
        root, engine = tmp_path, None
    monkeypatch.setattr(extensions, "owner", lambda view: object() if view == "research" else None)
    got = {}
    monkeypatch.setattr(pane, "web_view", lambda root, view, part, zoom, scope=None: got.update(scope=scope) or {"view": view})
    with watch.PANE_LOCK:   # a system view drawing: the extension's still draws
        page = watch._build(W(), ("web", "research", 0, "", "2_study"))
    assert page["scope"] == "2_study" and got["scope"] == "2_study"


def test_the_left_column_is_one_list_and_nav_targets_carry_the_project():
    p = watch.PAGE
    assert "if (r.within && !r.current) continue;" in p
    assert 'typeof tg.scope === "string" ? openScope(tg.scope' in p
    assert "Projects in this repository\")); fam.firstChild.remove()" not in p   # no longer folded below
    assert "scope:ui.scope" in p.split("function go(")[1].split("}")[0]           # Back returns to the project


def test_the_studio_shows_a_slow_view_as_drawing_not_502():
    import inspect
    from system_diff import app
    src = inspect.getsource(app)
    assert "except (TimeoutError, socket.timeout):" in src and "def _slow(" in src


def test_plan_agents_asks_the_engine_and_never_hangs(monkeypatch, tmp_path):
    """plan_agents is answered by the running engine (it holds the system); with none, a bounded read here."""
    import time as _t
    from system_diff import uitools
    assert uitools.within(0.2, lambda: _t.sleep(2) or {"ok": 1}, "plan_agents")["error"].startswith("plan_agents is still")
    assert uitools.within(2, lambda: {"ok": 1}, "plan_agents") == {"ok": 1}
    assert uitools.engine_post(tmp_path, "agents/plan", {"task": "x"}) is None   # no engine: the caller reads itself
    assert '"/agents/plan"' in watch.__loader__.get_source("system_diff.watch")


def test_parallel_writers_in_one_process_never_share_a_temporary_file(tmp_path):
    """Parallel agents' hooks run in one engine: two threads writing one state file at once must not collide
    (Windows: PermissionError on the shared temporary name)."""
    from system_diff import activity
    target = tmp_path / "state.json"
    errors = []

    def write(i):
        try:
            for _ in range(50):
                activity._replace(target, f'{{"n": {i}}}\n')
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    ts = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and target.read_text().startswith('{"n"')
    assert not [p for p in tmp_path.iterdir() if p.name != "state.json"]   # no temporary file left


def test_the_system_tools_ask_the_engine_and_never_hang():
    """system_diff, system_map and explain read the whole system: the running engine answers them (POST /tool); with
    none, a bounded read here that says it timed out."""
    from system_diff import mcp_server, uitools
    src = watch.__loader__.get_source("system_diff.watch")
    assert '"/tool"' in src and "mcp_server.tool_diff" in src
    assert uitools.within(0.1, lambda: time.sleep(1), "system_diff")["error"].endswith("tell the person it timed out.")
    assert callable(mcp_server._via_engine)


def test_a_page_drawn_before_is_shown_at_once_and_drawn_again_behind_it(monkeypatch, tmp_path):
    """Switching to a tab drawn at an older version (Agents, Topics, History while agents work): the old page at
    once, marked stale; the drawing goes on behind it and a long poll waits for that drawing."""
    monkeypatch.setattr(live, "save_page", lambda *a, **k: None)
    gate = threading.Event()
    e = _Engine(tmp_path)

    def build(key):
        if e.version > 1:
            gate.wait(5)
        return {"view": key[1], "v": e.version}
    pages = live.Pages(e, build, drawing=lambda key: {"drawing": True})
    key = ("web", "agents", 0, "")
    assert pages.get(key)[1]["v"] == 1
    e.version = 2                                   # agents worked: a new version
    t = time.monotonic()
    version, page = pages.get(key)
    assert time.monotonic() - t < 0.5 and version == 1 and page["stale"] and page["v"] == 1
    job = pages.job(key)
    assert job is not None and not job.is_set()
    gate.set(); job.wait(2)
    assert pages.get(key)[1]["v"] == 2 and "stale" not in pages.get(key)[1]


def test_a_log_is_parsed_once_then_only_what_was_added(tmp_path):
    import json as _j
    from system_diff import jsonl
    f = tmp_path / "runs.jsonl"
    f.write_text("".join(_j.dumps({"n": i}) + "\n" for i in range(3)), encoding="utf-8")
    assert [x["n"] for x in jsonl.read(f)] == [0, 1, 2]
    with open(f, "a", encoding="utf-8") as out:
        out.write(_j.dumps({"n": 3}) + "\n" + '{"n": 4')      # a line still being written
    assert [x["n"] for x in jsonl.read(f)] == [0, 1, 2, 3]
    with open(f, "a", encoding="utf-8") as out:
        out.write("}\n")
    assert [x["n"] for x in jsonl.read(f)] == [0, 1, 2, 3, 4]
    f.write_text(_j.dumps({"n": 9}) + "\n", encoding="utf-8")   # trimmed: read again whole
    assert [x["n"] for x in jsonl.read(f)] == [9]


def test_a_slow_tool_never_holds_the_other_tools_up():
    """system-diff's tools run in worker threads (the MCP SDK 1.x ran them in its one loop: a slow call made
    resume_plan wait behind it, "running" with no word)."""
    import asyncio
    import inspect
    from system_diff import mcp_server
    pytest = __import__("pytest")
    try:
        server = mcp_server.build()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no MCP SDK here: {exc}")
    tools = {t.name: t for t in server._tool_manager.list_tools()}
    assert inspect.iscoroutinefunction(tools["resume_plan"].fn) and callable(tools["resume_plan"].fn.__wrapped__)
    assert "properties" in tools["save_setting"].parameters and "name" in tools["save_setting"].parameters["properties"]
