"""`system-diff tui`: the system in the terminal, in a pane beside Claude Code, following the part the agent works on.

Four views, a key each:
  Now      (n)  the part being worked on (the one the agent last edited, else read), zoomed in: what calls it, what it
                calls, its endpoints and settings; then what it is, what is changing in it and why that matters, and
                topics to learn. It follows the agent until you pick a part yourself; f follows again.
  Map      (m)  the whole system, a row of boxes per column; arrows pick a part, enter zooms in on it.
  Changes  (c)  exactly what changed since the checkpoint, and this turn; what you asked, and what the agent said.
  Topics   (t)  the technologies the system is built with, and which parts use each.

s marks what you have seen (the checkpoint moves), o opens the diff picture, e asks your LLM for a longer lesson on
the part (only its structure is sent, never its code), r looks again now, q quits. The [tui] extra (Textual).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Markdown, Static, TabbedContent, TabPane

from . import activity, companion, graph, learn, pictures, textmap
from .sources import git_dir
from .watch import View, Watcher


def rich_text(lines: List[textmap.Line]) -> Text:
    out = Text(no_wrap=True, overflow="ignore")
    for i, line in enumerate(lines):
        if i:
            out.append("\n")
        for text, style in line:
            out.append(text, textmap.rich_style(style) or None)
    return out


FEED_STYLE = {"prompt": "bold", "read": "dim", "edit": "cyan", "change": "bold yellow", "stop": "green"}


def feed_text(feed: List[Dict[str, Any]], most: int = 10) -> Text:
    """What just happened, newest first: each line its time, what happened and, for a change to the system, why it
    matters."""
    if not feed:
        return Text("What just happened will show here as the agent reads and edits the code.", style="dim")
    out = Text("What just happened", style="bold")
    for item in feed[:most]:
        out.append("\n")
        out.append(f"{time.strftime('%H:%M:%S', time.localtime(item['at']))}  ", style="dim")
        out.append(item["text"], style=FEED_STYLE.get(item["kind"], ""))
        if item.get("why"):
            out.append(f" · {item['why']}", style="dim")
    return out


class Picture(Static):
    """A box-drawing picture, scrolled when wider than the pane."""

    DEFAULT_CSS = "Picture { width: auto; height: auto; padding: 0 1; }"


class Scroll(VerticalScroll, can_focus=False):
    """A pane that scrolls with the mouse and page keys, leaving the arrows to pick parts."""


class SystemDiffApp(App):
    TITLE = "system-diff"
    CSS = """
    #doing { padding: 0 1; height: auto; }
    #doing.idle { color: $text-muted; }
    #feed { padding: 1 1 0 1; height: auto; }
    Markdown { padding: 0 1; }
    #map-help { padding: 0 1; color: $text-muted; height: auto; }
    """
    BINDINGS = [
        Binding("n", "view('now')", "Now"),
        Binding("m", "view('map')", "Map"),
        Binding("c", "view('changes')", "Changes"),
        Binding("t", "view('topics')", "Topics"),
        Binding("f", "follow", "Follow"),
        Binding("s", "seen", "Mark seen"),
        Binding("e", "explain", "Explain"),
        Binding("o", "open_picture", "Picture", show=False),
        Binding("r", "look", "Look again", show=False),
        Binding("q", "quit", "Quit"),
        Binding("left,up", "pick(-1)", "Pick", show=False, priority=True),
        Binding("right,down", "pick(1)", "Pick", show=False, priority=True),
        Binding("enter", "zoom", "Zoom in", priority=True),
        Binding("escape,backspace", "back", "Back", show=False, priority=True),
        Binding("pageup", "page(-1)", show=False),
        Binding("pagedown", "page(1)", show=False),
    ]

    def __init__(self, root: Path, base: str = "checkpoint", config_path: Optional[str] = None,
                 interval: float = 2.0, watcher: Optional[Watcher] = None):
        super().__init__()
        self.root = root
        self.w = watcher or Watcher(root, base, config_path, interval)
        self.sub_title = f"{root.name} · since {base}"
        self.following = True
        self.part: Optional[str] = None          # the part zoomed in on
        self.history: List[str] = []             # the parts zoomed in on before it, to go back to
        self.near = 0                            # which neighbour of the zoomed part is picked (0: none)
        self.map_pick: Optional[str] = None      # the part picked on the map
        self.shown: tuple = ()                   # what the views were last drawn from
        self.lessons: Dict[tuple, Optional[dict]] = {}
        self.extra: Dict[str, str] = {}          # part -> a longer lesson from the LLM
        self.topics_for = ""                     # the commit the Topics view was made from
        self.topics_text = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="now"):
            with TabPane("Now", id="now"):
                with Scroll(id="now-scroll"):
                    yield Static("Reading the code…", id="doing", classes="idle")
                    yield Picture(id="zoom")
                    yield Static(id="feed")
                    yield Markdown(id="lesson")
            with TabPane("Map", id="map"):
                with Scroll(id="map-scroll"):
                    yield Picture(id="map-picture")
                    yield Static("arrows pick a part · enter zooms in on it", id="map-help")
            with TabPane("Changes", id="changes"):
                with Scroll(id="changes-scroll"):
                    yield Markdown(id="changes-text")
            with TabPane("Topics", id="topics"):
                with Scroll(id="topics-scroll"):
                    yield Markdown("Reading the code…", id="topics-text")
        yield Footer()

    def on_mount(self) -> None:
        if not getattr(self.w, "external", False):
            threading.Thread(target=self.w.loop, daemon=True).start()
        self.set_interval(0.4, self.poll)
        self.set_interval(companion.BEAT, self.beat)
        self.beat()

    def beat(self) -> None:
        """Say the view is open, so a session that starts now does not open a second one."""
        try:
            companion.beat(self.root)
        except (OSError, ValueError):
            pass

    def on_unmount(self) -> None:
        self.w.stop.set()
        companion.gone(self.root)

    def on_resize(self) -> None:
        self.shown = ()

    def on_tabbed_content_tab_activated(self) -> None:
        self.shown = ()
        self.poll()

    # ── drawing ──────────────────────────────────────────────────────────────

    def poll(self) -> None:
        view = self.w.snapshot_view()
        state = self.w.snapshot_state()
        if view.since is None:
            if view.error or state.get("error"):
                self.query_one("#doing", Static).update(f"Could not read the code: {view.error or state['error']}")
            return
        if self.following and view.now.get("part") and view.now["part"] != self.part:
            self.part, self.near, self.history = view.now["part"], 0, []
        key = (state["version"], view.looks, self.part, self.near, self.map_pick, self.following,
               self.query_one(TabbedContent).active, self.size)
        if key == self.shown:
            return
        self.shown = key
        self.draw_now(view, state)
        self.draw_map(view)
        self.draw_changes(view, state)
        if self.query_one(TabbedContent).active == "topics":
            self.draw_topics(view)

    def width(self) -> int:
        return max(40, self.size.width - 4)

    def marks(self, view: View) -> dict:
        c = view.since
        return graph.change_marks(c.diff, c.before, c.after)

    def neighbours(self, view: View) -> List[str]:
        hood = self.hood(view)
        out: List[str] = []
        for nid in (hood["callers"] + hood["callees"]) if hood else []:
            if nid not in out:
                out.append(nid)
        return out

    def hood(self, view: View) -> Optional[dict]:
        if not self.part or view.since is None:
            return None
        c = view.since
        return graph.neighbourhood(c.after, c.cfg, self.part, self.marks(view), c.before)

    def lesson(self, view: View) -> Optional[dict]:
        key = (self.part, view.looks)
        if key not in self.lessons:
            self.lessons = {k: v for k, v in self.lessons.items() if k[1] == view.looks}
            self.lessons[key] = learn.lesson(view.since, self.part, view.source) if self.part else None
        return self.lessons[key]

    def draw_now(self, view: View, state: Dict[str, Any]) -> None:
        now = view.now
        c = view.since
        lines = []
        if self.part and self.following and now.get("part"):
            verb = "✎ Editing" if now["doing"] == "editing" else "Reading"
            lines.append(Text.assemble((f"{verb} {activity.label(c.cfg, self.part)}", "bold cyan"),
                                       (f"  {now['path']} · {activity.ago(now['at'])}", "dim")))
        elif self.part:
            lines.append(Text.assemble((f"Zoomed in on {activity.label(c.cfg, self.part)}", "bold"),
                                       ("  you picked it · f follows the agent again", "dim")))
        else:
            lines.append(Text("Nothing touched yet: this view zooms in on the part the agent edits or reads. "
                              "Pick a part on the map (m) to look at one now.", style="dim"))
        if now.get("prompt"):
            asked = now["prompt"] if len(now["prompt"]) <= 160 else now["prompt"][:159] + "…"
            lines.append(Text.assemble(("Working on: " if now.get("working") else "Last asked: ", "dim"), (asked, "")))
        if state.get("turn"):
            lines.append(Text.assemble(("This turn: ", "dim"), (state["turn"], "")))
        if now.get("said") and not now.get("working"):
            lines.append(Text.assemble(("The agent said: ", "dim"), (now["said"], "")))
        doing = self.query_one("#doing", Static)
        doing.set_class(not self.part, "idle")
        doing.update(Text("\n").join(lines))
        hood = self.hood(view)
        near = self.neighbours(view)
        picked = near[self.near - 1] if 0 < self.near <= len(near) else None
        zoom = textmap.render_zoom(hood, self.width(), picked=picked) if hood else []
        if hood and near:
            zoom += [[], [("arrows pick a neighbour · enter zooms in on it · esc goes back", "muted")]]
        self.query_one("#zoom", Picture).update(rich_text(zoom))
        self.query_one("#feed", Static).update(feed_text(view.feed))
        les = self.lesson(view) if self.part else None
        text = learn.to_markdown(les) if les else ""
        if les and self.part in self.extra:
            text += "\n**A longer lesson**\n\n" + self.extra[self.part] + "\n"
        self.query_one("#lesson", Markdown).update(text)

    def overview(self, view: View) -> dict:
        c = view.since
        return graph.overview(c.after, c.cfg, self.marks(view), c.before)

    def draw_map(self, view: View) -> None:
        g = self.overview(view)
        ids = [n["id"] for n in g["nodes"]]
        if self.map_pick not in ids:
            self.map_pick = self.part if self.part in ids else (ids[0] if ids else None)
        lines = textmap.render_map(g, self.width(), focus=view.now.get("part"), picked=self.map_pick)
        self.query_one("#map-picture", Picture).update(rich_text(lines))

    def draw_changes(self, view: View, state: Dict[str, Any]) -> None:
        c, now = view.since, view.now
        out = [f"## Since {c.base}", "", c.diff["headline"], "", "```", c.diff["plan"], "```", ""]
        if view.turn is not None:
            out += ["## This turn", "", view.turn.diff["headline"], ""]
            if view.turn.diff["changes"]:
                out += ["```", view.turn.diff["plan"], "```", ""]
        if now.get("prompt"):
            out += ["**You asked:** " + now["prompt"], ""]
        if now.get("said"):
            out += ["**The agent said:** " + now["said"], ""]
        latest = now.get("latest")
        if latest:
            out += [f"Last file touched: `{latest['path']}` ({latest['doing']}, {activity.ago(latest['at'])})", ""]
        out += ["`s` marks this as seen: the next diff shows only what comes after it."]
        self.query_one("#changes-text", Markdown).update("\n".join(out))

    def draw_topics(self, view: View) -> None:
        if self.topics_for == view.since.head_sha:
            return
        self.topics_for = view.since.head_sha
        try:
            text = learn.system_markdown(learn.system_topics(view.source, view.since.after, view.since.cfg))
        except (OSError, ValueError) as exc:
            text = f"Could not read the code's imports: {exc}"
        self.topics_text = text
        self.query_one("#topics-text", Markdown).update(text)

    # ── keys ─────────────────────────────────────────────────────────────────

    def action_view(self, name: str) -> None:
        self.query_one(TabbedContent).active = name

    def action_follow(self) -> None:
        self.following, self.history, self.near = True, [], 0
        part = self.w.snapshot_view().now.get("part")
        if part:
            self.part = part
        self.notify("Following the agent: the view zooms in on the part it works on.")
        self.poll()

    def action_pick(self, step: int) -> None:
        view = self.w.snapshot_view()
        if view.since is None:
            return
        tab = self.query_one(TabbedContent).active
        if tab == "map":
            ids = [n["id"] for n in self.overview(view)["nodes"]]
            if ids:
                at = ids.index(self.map_pick) if self.map_pick in ids else -1
                self.map_pick = ids[(at + step) % len(ids)]
        elif tab == "now":
            near = self.neighbours(view)
            if near:
                self.near = (self.near + step - 1) % len(near) + 1 if self.near else (1 if step > 0 else len(near))
        self.poll()

    def zoom_to(self, part: str) -> None:
        if self.part and self.part != part:
            self.history.append(self.part)
        self.part, self.near, self.following = part, 0, False
        self.query_one(TabbedContent).active = "now"
        self.poll()

    def action_zoom(self) -> None:
        tab = self.query_one(TabbedContent).active
        if tab == "map" and self.map_pick:
            self.zoom_to(self.map_pick)
        elif tab == "now":
            near = self.neighbours(self.w.snapshot_view())
            if 0 < self.near <= len(near):
                self.zoom_to(near[self.near - 1])

    def action_back(self) -> None:
        if self.history:
            self.part, self.near = self.history.pop(), 0
            self.poll()
        elif not self.following:
            self.action_follow()

    def action_page(self, step: int) -> None:
        pane = self.query_one(f"#{self.query_one(TabbedContent).active}-scroll", Scroll)
        (pane.scroll_page_down if step > 0 else pane.scroll_page_up)()

    def action_look(self) -> None:
        threading.Thread(target=self.w.tick, kwargs={"force": True}, daemon=True).start()

    def action_seen(self) -> None:
        def mark() -> None:
            self.w.mark_seen()
            self.call_from_thread(self.notify, "Marked as seen: from now on, only what is new to you.")
        self.notify("Marking as seen…")
        threading.Thread(target=mark, daemon=True).start()

    def action_open_picture(self) -> None:
        state = self.w.snapshot_state()
        if not state.get("svg"):
            self.notify("Nothing structural changed, so there is no picture.")
            return
        path = git_dir(self.root, "tui") / "diff.svg"
        pictures.write_if_changed(path, state["svg"])
        self.open_url(path.as_uri())
        self.notify(f"The picture: {path}")

    def action_explain(self) -> None:
        view = self.w.snapshot_view()
        les = self.lesson(view) if self.part and view.since is not None else None
        if not les:
            self.notify("Zoom in on a part first.")
            return
        part = self.part

        def ask() -> None:
            text = learn.ask_llm(les, self.root, view.since.cfg)
            if text:
                self.extra[part] = text
                self.shown = ()
            self.call_from_thread(
                self.notify, "The longer lesson is below the topics." if text else
                "No LLM answered from here (set ANTHROPIC_API_KEY, or SYSTEM_DIFF_LLM_*). You can also ask Claude in "
                f"your session to explain {les['title']}: its explain tool gives it these same facts.")
        self.notify(f"Asking your LLM about {les['title']} (its structure only, never its code)…")
        threading.Thread(target=ask, daemon=True).start()


def run(root: Path, base: str = "checkpoint", config_path: Optional[str] = None, interval: float = 2.0) -> int:
    SystemDiffApp(root, base, config_path, interval).run()
    return 0
