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
