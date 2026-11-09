"""`system-diff watch`: a live page of what changed in the system since the checkpoint, for a person to keep beside the
editor while they, or an AI coding agent, change the code. It redraws when a file changes, and zooms in on the part
being worked on (the one the agent last edited or read, else the one whose files last changed), with what that part
is, what is changing in it and the topics behind it. "Mark as seen" moves the checkpoint, so the page then shows only
what is new to them. The terminal view (`system-diff tui`) watches with the same Watcher.

Local only: the server listens on 127.0.0.1, and "Mark as seen" needs the page's own token, so no other web page open in
the same browser can press it.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import activity, graph, learn, pictures
from .compare import Comparison, checkpoint, checkpoint_sha, compare_worktree, turn_sha, worktree_tree
from .diff import diff
from .extract import Code
from .sources import GitSource, git, git_dir

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>System diff</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { --ink:#161c24; --muted:#667385; --line:#e1e5eb; --bg:#f8f9fb; --work:#1f5fbf; }
* { box-sizing: border-box; }
body { margin:0; font:14px/1.45 'Segoe UI', FreeSans, 'Helvetica Neue', Arial, sans-serif; color:var(--ink); background:var(--bg); }
header { position:sticky; top:0; display:flex; flex-wrap:wrap; gap:12px; align-items:center; padding:12px 20px;
         background:#fff; border-bottom:1px solid var(--line); }
h1 { font-size:15px; margin:0; } .since { color:var(--muted); font-size:12.5px; }
button { margin-left:auto; border:1px solid var(--work); background:var(--work); color:#fff; border-radius:8px;
         padding:6px 14px; font:inherit; font-weight:600; cursor:pointer; } button:disabled { opacity:.5; cursor:default; }
main { padding:16px 20px; display:grid; gap:14px; }
.headline { font-size:16px; font-weight:600; } .error { color:#c4321c; white-space:pre-wrap; }
.picture { overflow:auto; background:#fff; border:1px solid var(--line); border-radius:10px; padding:8px; }
.picture svg { display:block; max-width:none; }
pre { margin:0; padding:12px 14px; background:#fff; border:1px solid var(--line); border-radius:10px; overflow:auto;
      font:12.5px/1.5 Consolas, Inconsolata, 'DejaVu Sans Mono', monospace; }
.quiet { color:var(--muted); }
#now { display:grid; gap:10px; padding:12px 14px; background:#fff; border:1px solid var(--line); border-radius:10px; }
.doing { font-weight:600; color:var(--work); } .asked { color:var(--muted); font-size:12.5px; }
.lesson h2 { font-size:15px; margin:4px 0; } .lesson h3 { font-size:13px; margin:10px 0 4px; }
.lesson ul { margin:0; padding-left:20px; } .lesson li { margin:3px 0; } .lesson code { font-size:12px; }
.lesson .here { color:var(--muted); font-style:italic; }
</style></head><body>
<header><h1>System diff</h1><span class="since" id="since"></span>
<button id="seen" type="button" title="Move the checkpoint here: the page then shows only what is new">Mark as seen</button></header>
<main><section id="now" hidden><div class="doing" id="doing"></div><div class="asked" id="asked"></div>
<div class="asked" id="turn"></div><div class="picture" id="zoom" hidden></div>
<div class="lesson"><h3 id="feed-head" hidden>What just happened</h3><ul id="feed"></ul></div>
<div class="lesson" id="lesson"></div></section>
<div class="headline" id="headline">Reading the code…</div><div class="error" id="error"></div>
<div class="picture" id="picture" hidden></div><pre id="plan" hidden></pre></main>
<script>
const TOKEN = "__TOKEN__";
let version = -1;
function show(s) {
  version = s.version;
  document.getElementById("since").textContent = "since " + s.base + " · updated " + s.at;
  document.getElementById("headline").textContent = s.headline;
  document.getElementById("error").textContent = s.error || "";
  const pic = document.getElementById("picture"), plan = document.getElementById("plan");
  pic.hidden = !s.svg; pic.innerHTML = s.svg || "";
  plan.hidden = !s.changes; plan.textContent = s.plan;
  document.getElementById("seen").disabled = !s.changes;
  showNow(s);
}
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function ago(at) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - at));
  return s < 5 ? "just now" : s < 60 ? s + "s ago" : s < 3600 ? Math.floor(s / 60) + " min ago" : Math.floor(s / 3600) + " h ago";
}
function showNow(s) {
  const f = s.focus, now = document.getElementById("now");
  now.hidden = !f && !s.prompt && !s.turn && !(s.feed || []).length;
  document.getElementById("doing").textContent = f ? (f.doing === "editing" ? "✎ Editing " : "Reading ") + f.label +
    " · " + f.path + " · " + ago(f.at) : "";
  document.getElementById("asked").textContent = s.prompt ? (s.working ? "Working on: " : "Last asked: ") + s.prompt : "";
  document.getElementById("turn").textContent = s.turn ? "This turn: " + s.turn : "";
  const zoom = document.getElementById("zoom");
  zoom.hidden = !s.zoom; zoom.innerHTML = s.zoom || "";
  const feed = document.getElementById("feed");
  feed.replaceChildren();
  document.getElementById("feed-head").hidden = !(s.feed || []).length;
  for (const f of s.feed || []) {
    const li = el("li");
    li.append(el("span", ago(f.at) + " · ", "here"), el(f.kind === "change" ? "b" : "span", f.text));
    if (f.why) li.append(el("span", " · " + f.why, "here"));
    feed.append(li);
  }
  const box = document.getElementById("lesson"), l = s.lesson;
  box.replaceChildren();
  if (!l) return;
  box.append(el("h2", l.title), el("p", l.what.join(" ")));
  if (l.changing.length) {
    const ul = el("ul");
    for (const c of l.changing) { const li = el("li"); li.append(el("code", c.text), " " + c.why); ul.append(li); }
    box.append(el("h3", "What is changing"), ul);
  }
  if (l.topics.length) {
    const ul = el("ul");
    for (const t of l.topics) {
      const li = el("li"), a = el("a", "Read more");
      a.href = t.link; a.target = "_blank"; a.rel = "noopener";
      li.append(el("b", t.title), " " + t.what + " ", el("span", t.here, "here"), " ", a);
      ul.append(li);
    }
    box.append(el("h3", "Topics"), ul);
  }
}
async function poll() {
  try {
    const r = await fetch("state?since=" + version, { cache: "no-store" });
    if (r.status === 200) show(await r.json());
  } catch (e) { document.getElementById("error").textContent = "The watcher stopped: start system-diff watch again."; }
  setTimeout(poll, 1500);
}
document.getElementById("seen").onclick = async () => {
  const r = await fetch("seen", { method: "POST", headers: { "X-Token": TOKEN } });
  if (r.status === 200) show(await r.json());
};
poll();
</script></body></html>
"""


@dataclass
class View:
    """One look at the system, for the terminal view: the diff since the base and this turn's, the files they were read
    from, and what is going on (activity.now)."""
    since: Optional[Comparison] = None
    turn: Optional[Comparison] = None
    source: Any = None
    now: Dict[str, Any] = field(default_factory=dict)
    feed: List[Dict[str, Any]] = field(default_factory=list)
    looks: int = 0
    at: str = ""
    error: str = ""


class Watcher:
    """The current diff, recomputed in the background whenever the working tree, the checkpoint or the turn moves, and
    what is going on, looked at again whenever the agent's hooks note something or a file changes."""

    def __init__(self, root: Path, base: str = "checkpoint", config_path: Optional[str] = None, interval: float = 2.0):
        self.root, self.base, self.config_path, self.interval = root, base, config_path, interval
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.state: Dict[str, Any] = {"version": 0, "headline": "Reading the code…", "plan": "", "svg": "",
                                      "changes": 0, "base": base, "at": "", "error": "", "turn": "", "focus": None,
                                      "prompt": "", "working": False, "zoom": "", "lesson": None, "feed": []}
        self.view = View()
        self.seen_key: Optional[tuple] = None
        self.stop = threading.Event()
        # One look at the files at a time: otherwise a slow look that started before "Mark as seen" could finish
        # after it and put the old diff back on the page.
        self.busy = threading.Lock()
        self.tree: Optional[str] = None
        self.changed: List[dict] = []     # files seen changing on disk, as activity events
        self.steps: List[dict] = []       # what each change of the files did to the system, as `change` events
        self.last: Optional[dict] = None  # the working tree's blueprint at the last look
        self.log_mark: Optional[tuple] = None
        self.taught: tuple = ()           # (part, look) the zoomed picture and the lesson were made for

    def key(self) -> tuple:
        return worktree_tree(self.root), checkpoint_sha(self.root), turn_sha(self.root)

    def _note_changed_files(self, tree: str) -> None:
        if self.tree and tree != self.tree:
            names = git("diff-tree", "-r", "-z", "--name-only", "--no-renames", self.tree, tree, cwd=self.root)
            at = round(time.time(), 3)
            self.changed = (self.changed + [{"at": at, "event": activity.EDIT, "path": n, "source": "files"}
                                            for n in names.split("\0") if n])[-50:]
        self.tree = tree

    def _log_moved(self) -> bool:
        try:
            st = (git_dir(self.root) / activity.LOG).stat()
            mark: Optional[tuple] = (st.st_mtime_ns, st.st_size)
        except (OSError, subprocess.CalledProcessError):
            mark = None
        moved, self.log_mark = mark != self.log_mark, mark
        return moved

    def refresh(self) -> None:
        try:
            bases = [self.base] + (["turn"] if self.base != "turn" and turn_sha(self.root) else [])
            found = compare_worktree(self.root, bases, self.config_path)
            c, turn = found[0], (found[1] if len(found) > 1 else None)
            svg = graph.diff_svg(c.diff, c.before, c.after, c.cfg) or ""
            new = {"headline": c.diff["headline"], "plan": c.diff["plan"], "svg": svg,
                   "changes": len(c.diff["changes"]), "base": c.base, "error": "",
                   "turn": learn.one_line(learn.story(turn.diff)) if turn else ""}
            look = {"since": c, "turn": turn, "source": GitSource(c.head_sha, self.root), "error": ""}
            if self.last is not None:  # what this step, since the last look, did to the system
                told = learn.story(diff(self.last, c.after))
                if told:
                    self.steps = (self.steps + [{"at": round(time.time(), 3), "event": "change", "items": told[:4]}])[-30:]
            self.last = c.after
            if self.base == "checkpoint":
                try:
                    activity.write_state(self.root, c.diff, c.base, turn.diff if turn else None)
                except OSError:
                    pass  # the status line is a convenience: never let it stop the page
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
            new = {"error": f"{type(exc).__name__}: {exc}"}
            look = {"error": new["error"]}
        with self.lock:
            self.state.update(new, version=self.state["version"] + 1, at=time.strftime("%H:%M:%S"))
            for k, v in look.items():
                setattr(self.view, k, v)
            self.view.looks += 1
            self.view.at = self.state["at"]
        self.follow()

    def follow(self) -> None:
        """Look again at what is going on: the focus, and the zoomed picture and the lesson for it."""
        with self.lock:
            c, source, looks = self.view.since, self.view.source, self.view.looks
        if c is None:
            return
        events = sorted(activity.events(self.root) + self.changed + self.steps, key=lambda e: e["at"])
        now = activity.now(events, Code(c.cfg))
        feed = activity.feed(events, Code(c.cfg), c.cfg)
        part = now["part"]
        new: Dict[str, Any] = {"prompt": now["prompt"], "working": now["working"], "feed": feed}
        if part:
            new["focus"] = {"part": part, "label": activity.label(c.cfg, part), "doing": now["doing"],
                            "path": now["path"], "at": now["at"]}
        if (part, looks) != self.taught:
            self.taught = (part, looks)
            hood = graph.neighbourhood(c.after, c.cfg, part, graph.change_marks(c.diff, c.before, c.after),
                                       c.before) if part else None
            new["zoom"] = pictures.map_svg(hood, diff=True, label=f"{hood['nodes'][0]['title']}, zoomed in") if hood else ""
            try:
                new["lesson"] = learn.lesson(c, part, source) if part else None
            except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
                new["lesson"] = None
                new["error"] = f"lesson: {type(exc).__name__}: {exc}"
            if new.get("lesson") is not None:
                new["focus"]["label"] = new["lesson"]["title"]
        with self.lock:
            changed = any(self.state.get(k) != v for k, v in new.items()) or self.view.now != now
            self.view.now, self.view.feed = now, feed
            if changed:
                self.state.update(new, version=self.state["version"] + 1)

    def tick(self, force: bool = False) -> None:
        with self.busy:
            try:
                key = self.key()
                self._note_changed_files(key[0])
            except (OSError, subprocess.CalledProcessError) as exc:
                with self.lock:
                    self.state.update(error=f"git: {exc}", version=self.state["version"] + 1)
                return
            moved = self._log_moved()
            if force or key != self.seen_key:
                self.seen_key = key
                self.refresh()
            elif moved:
                self.follow()

    def loop(self) -> None:
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(self.interval)

    def mark_seen(self) -> None:
        with self.busy:
            checkpoint(self.root)
        self.tick(force=True)  # the page's answer is the diff from the new checkpoint, never an older look

    def snapshot_state(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.state)

    def snapshot_view(self) -> View:
        with self.lock:
            return View(**vars(self.view))


def handler(w: Watcher):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet: the terminal shows the URL, not every poll
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE.replace("__TOKEN__", w.token).encode(), "text/html; charset=utf-8")
            if self.path.startswith("/state"):
                return self._send(200, json.dumps(w.snapshot_state()).encode(), "application/json")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/seen" or self.headers.get("X-Token") != w.token:
                return self._send(403, b"forbidden", "text/plain")
            w.mark_seen()
            return self._send(200, json.dumps(w.snapshot_state()).encode(), "application/json")

    return Handler


def serve(root: Path, base: str = "checkpoint", port: int = 8765, config_path: Optional[str] = None,
          interval: float = 2.0, ready=None) -> None:
    """Run until interrupted. `ready(url)` is called once the server listens (tests use port 0)."""
    w = Watcher(root, base, config_path, interval)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(w))
    thread = threading.Thread(target=w.loop, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    if ready:
        ready(url, server, w)
    else:
        print(f"system-diff watch: {url} (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        w.stop.set()
        server.server_close()
