"""The terminal view: the pane's page drawn as ANSI lines, the pick moved as the pane moves it, the loop's keys."""
import re
import threading
from pathlib import Path

from system_diff import tui

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def page(part="", width=40, view="now", zoom=1):
    """Developer -> API -> Core, as `pane.show` lays it out (the shape the pane's own test uses)."""
    return {
        "protocol": 2, "view": view, "part": part, "width": width, "wide": width, "pick": "b:api", "zoom": zoom,
        "web": "http://localhost:8765/", "send": [],
        "rows": [
            [["● Claude is working  ", "accent"], ["✎ Editing ", "accent"], ["API", "accent bold"]],
            [["┌─────────┐   ┌─────┐   ┌──────┐", "muted", "b:dev b:api b:g:Core"]],
            [["│Developer├─▸", "muted", "b:dev"], ["│ API ├", "title bold accent", "b:api"],
             ["─▸", "muted", "e:api>g:Core"], ["│ Core │", "title", "b:core"]],
            [],
            [],
            [["WHAT JUST HAPPENED", "heading"]],
            [["API now answers GET /health", "attention"]],
        ],
        "blocks": [
            {"id": "b:dev", "x": 0, "y": 1, "w": 11, "h": 3, "enter": {"zoom": "a:dev"}, "slot": [3, 2]},
            {"id": "b:api", "x": 14, "y": 1, "w": 7, "h": 3, "enter": {"zoom": "api"}, "slot": [3, 2]},
            {"id": "b:core", "x": 24, "y": 2, "w": 8, "h": 1, "enter": {"zoom": "core"}, "slot": [3, 2]},
        ],
        "lights": {"b:api": ["b:core", "e:api>g:Core"]},
        "details": {"b:api": [[["API  ", "bold"], ["uses Core · used by Developer", "muted"]]]},
    }


def plain(line):
    return ANSI.sub("", line)


def test_render_draws_the_page_at_a_width_with_the_panes_colours():
    st = tui.State(view="now", sel="b:api")
    lines = tui.render(page(), st, 60, 14, version="9.9.9")
    assert len(lines) == 14
    text = [plain(x) for x in lines]
    assert all(tui.cells(t) <= 60 for t in text)
    assert text[0].startswith("Now  Review  Agents  Topics")
    assert "\x1b[1;4;34mNow" in lines[0]                       # the view: blue, bold, underlined
    assert "● Claude is working" in text[1]
    assert "\x1b[34m● Claude is working" in lines[1]           # accent: blue
    assert "\x1b[1;7;34m│ API ├" in lines[3]                  # the picked box's title: blue, inverse
    assert "\x1b[1;2m│ Core │" not in lines[3] and "\x1b[1m│ Core │" in lines[3]   # what it lights: bold
    assert "uses Core · used by Developer" in text[4]           # its detail in the slot under the picture
    assert "\x1b[1;33mAPI now answers" in lines[7]             # attention: yellow, bold
    assert text[-1].endswith("🌐 http://localhost:8765/  v9.9.9")  # the web page and the version at the bottom
    assert "q quit" in text[-1]


def test_render_scrolls_across_a_wide_page_and_cuts_rows_to_the_width():
    p = page(width=40)
    p["wide"] = 40
    st = tui.State(sel="b:core", sx=10)
    lines = [plain(x) for x in tui.render(p, st, 25, 12)]
    assert lines[1].startswith("◂ columns 11–35 of 40")
    assert all(tui.cells(x) <= 25 for x in lines)
    assert lines[4].startswith("├─▸│ API ├") and "│ Core │" in lines[4]


def test_the_arrows_move_the_pick_spatially_as_the_pane_does():
    blocks = page()["blocks"]
    by = {b["id"]: b for b in blocks}
    assert tui.move(blocks, by["b:api"], "right")["id"] == "b:core"
    assert tui.move(blocks, by["b:api"], "left")["id"] == "b:dev"
    assert tui.move(blocks, by["b:dev"], "up")["id"] == "b:dev"   # nothing above: it stays
    assert tui.move(blocks, None, "down")["id"] == "b:dev"
    grid = [{"id": f"{c}{r}", "x": c * 20, "y": r * 4, "w": 10, "h": 3} for c in range(2) for r in range(3)]
    at = {b["id"]: b for b in grid}
    assert tui.move(grid, at["00"], "down")["id"] == "01"         # down a column first
    assert tui.move(grid, at["01"], "right")["id"] == "11"        # across to the box level with it


class Fake:
    """A Tui whose pages come from here, at a fixed size, on a clock the test moves."""

    def __init__(self, view="now"):
        self.asked = []
        self.now = 0.0
        self.built = threading.Event()
        self.app = tui.Tui(Path("."), view, build=self.build, size=lambda: (60, 20), clock=lambda: self.now,
                           progress=lambda: "")

    def build(self, width, view, part, zoom):
        self.asked.append((width, view, part, zoom))
        self.built.set()
        return page(part, width, view)

    def settle(self):
        for _ in range(200):
            if not self.app.busy():
                break
            threading.Event().wait(0.01)
        self.app.tick()


def test_the_loop_draws_and_quits_on_q():
    f = Fake()
    keys = iter([None, "right", "return", "b", "r", "q", "never read"])
    written = []

    def read(_timeout):
        f.settle()
        return next(keys)

    assert tui.loop(f.app, read, written.append, poll=0) == 0
    assert next(keys) == "never read"
    screen = "".join(written)
    assert "\x1b[2J" in screen and "Now  Review" in plain(screen)
    assert ("now" in [a[1] for a in f.asked]) and f.asked[-1][1] == "review"
    assert any(a[2] == "core" for a in f.asked)                    # Enter on Core zoomed in on it
    assert f.app.st.part == "" and f.app.st.back == []             # and b came back


def test_keys_zoom_scroll_and_follow():
    f = Fake()
    f.app.start()
    f.settle()
    assert f.app.page is not None and f.app.st.sel == "b:api"     # the page's pick, Claude's part
    f.app.handle("-")
    assert f.app.st.zoom == 2
    f.app.handle("=")
    f.app.handle("=")
    assert f.app.st.zoom == 0
    f.app.handle("return")
    f.settle()
    assert f.app.st.part == "api" and f.app.st.back == [""]
    f.app.handle("f")
    assert f.app.st.part == "" and f.app.st.back == []
    f.app.handle("t")
    assert f.app.st.view == "topics"
    assert f.app.handle("q") is False
    f.app.stop()


def test_parse_reads_the_keys_a_terminal_sends():
    keys, rest = tui.parse("\x1b[A\x1b[1;2Bq\r\x7f\x1b[5~\x1b[6~[")
    assert keys == ["up", "shift-down", "q", "return", "backspace", "pageup", "pagedown", "["]
    assert rest == ""
    keys, rest = tui.parse("x\x1b[1;")
    assert keys == ["x"] and rest == "\x1b[1;"


def test_frame_rewrites_only_what_changed():
    first = tui.frame(["a", "b"], None, 2)
    assert first.startswith("\x1b[2J") and "\x1b[1;1H" in first and "\x1b[2;1H" in first
    again = tui.frame(["a", "c"], ["a", "b"], 2)
    assert "\x1b[1;1H" not in again and "\x1b[2;1H\x1b[2Kc" in again
