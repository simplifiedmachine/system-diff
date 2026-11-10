"""`system-diff watch`: a live page of what changed in the system since the checkpoint, for a person to keep beside the
editor while they, or an AI coding agent, change the code. It redraws when a file changes, and zooms in on the part
being worked on (the one the agent last edited or read, else the one whose files last changed), with what that part
is, what is changing in it and the topics behind it. "Mark as seen" moves the checkpoint, so the page then shows only
what is new to them. The terminal view (`system-diff tui`) watches with the same Watcher.

Local only: the server listens on 127.0.0.1, and "Mark as seen" needs the page's own token, so no other web page open in
the same browser can press it. "Send to Claude" (POST /send) needs the token and the page's own origin too, and appends
what was typed to .git/system-diff/outbox.jsonl, where the pane picks it up and submits it to the live session.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from . import activity, graph, learn, pictures
from .compare import Comparison, checkpoint, checkpoint_sha, compare_worktree, turn_sha, worktree_tree
from .diff import diff
from .extract import Code
from .sources import GitSource, git, git_dir

CLASSIC = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>System diff</title>
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

# The pane beside Claude Code, in a browser: the same page (pane.show), its rows painted as coloured monospace text,
# its blocks picked by hovering and zoomed into by a click.
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>System diff</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: dark;
  --bg:#0e1117; --panel:#161b24; --line:#262d3a; --fg:#d8dee8; --muted:#7c8697; --blue:#5ea4ff; --yellow:#e8c25a;
  --green:#5fd283; --red:#ff6f6f; --magenta:#d482ff; --cyan:#4fd3dc; --pickbg:rgba(94,164,255,.10); --inv:#0e1117; }
@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) { color-scheme: light;
  --bg:#fbfbfd; --panel:#ffffff; --line:#e1e5eb; --fg:#1b2330; --muted:#7a8494; --blue:#1f5fbf; --yellow:#9a6400;
  --green:#1d8a3c; --red:#c4321c; --magenta:#9b3fc2; --cyan:#0b7f8a; --pickbg:rgba(31,95,191,.08); --inv:#ffffff; } }
* { box-sizing: border-box; }
html, body { margin:0; background:var(--bg); color:var(--fg); }
body { font:14px/1.4 system-ui, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif; }
header { position:sticky; top:0; z-index:2; display:flex; flex-wrap:wrap; gap:6px; align-items:center;
         padding:8px 16px; background:var(--panel); border-bottom:1px solid var(--line); }
h1 { font-size:14px; margin:0 10px 0 0; font-weight:650; }
nav { display:flex; gap:2px; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:2px; }
nav button { border:0; background:transparent; color:var(--muted); }
nav button[aria-selected="true"] { background:var(--panel); color:var(--fg); box-shadow:0 0 0 1px var(--line); }
button { font:inherit; font-size:13px; padding:4px 10px; border-radius:6px; border:1px solid var(--line);
         background:var(--panel); color:var(--fg); cursor:pointer; }
button:hover:not(:disabled) { border-color:var(--blue); }
button:disabled { opacity:.45; cursor:default; }
.gap { flex:1; }
#status { color:var(--muted); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:40vw; }
#seen { background:var(--blue); border-color:var(--blue); color:var(--inv); font-weight:600; }
a { color:var(--blue); font-size:12px; }
main { padding:12px 16px 40px; }
#send { position:sticky; bottom:0; z-index:2; display:flex; gap:8px; align-items:flex-end; padding:8px 16px;
        background:var(--panel); border-top:1px solid var(--line); }
#say { flex:1; font:inherit; font-size:13px; min-height:2.6em; max-height:30vh; resize:vertical; padding:6px 8px;
       border-radius:6px; border:1px solid var(--line); background:var(--bg); color:var(--fg); }
#say:focus { outline:none; border-color:var(--blue); }
#sendnote { color:var(--muted); font-size:12px; max-width:30vw; }
#sendnote.bad { color:var(--red); }
#stage { position:relative; overflow-x:auto; }
#rows { position:relative; font-family:'DejaVu Sans Mono', 'Cascadia Mono', Menlo, Consolas, 'Liberation Mono', monospace;
        font-size:13px; white-space:pre; font-variant-ligatures:none; }
.r { height:var(--lh); line-height:var(--lh); }
.g { display:inline-block; width:var(--cw); text-align:center; overflow:visible; }
#hl { position:absolute; pointer-events:none; background:var(--pickbg); border-radius:3px; display:none; }
#error { color:var(--red); white-space:pre-wrap; font-size:13px; }
.c-blue { color:var(--blue); } .c-yellow { color:var(--yellow); } .c-green { color:var(--green); }
.c-red { color:var(--red); } .c-magenta { color:var(--magenta); } .c-cyan { color:var(--cyan); }
.b { font-weight:700; } .d { color:var(--muted); } .d.c-blue, .d.c-yellow, .d.c-green, .d.c-red { opacity:.75; }
.inv { background:var(--blue); color:var(--inv); }
.hot { cursor:pointer; }
</style></head><body>
<header><h1>System diff</h1>
<nav id="tabs" role="tablist">
<button role="tab" data-view="now" title="n">Now</button><button role="tab" data-view="review" title="r">Review</button>
<button role="tab" data-view="agents" title="a">Agents</button><button role="tab" data-view="topics" title="t">Topics</button>
</nav>
<button id="back" type="button" title="b: back">← Back</button>
<button id="follow" type="button" title="f: follow what Claude works on">Follow Claude</button>
<button id="out" type="button" title="−: smaller boxes, more of the system">−</button>
<button id="in" type="button" title="+: bigger boxes">+</button>
<span id="status"></span><span class="gap"></span>
<a href="classic">classic page</a>
<button id="seen" type="button" title="Move the checkpoint here: the pages then show only what is new">Mark as seen</button>
</header>
<main><div id="error"></div><div id="stage"><div id="rows"><div id="hl"></div></div></div></main>
<form id="send"><textarea id="say" maxlength="8000" placeholder="Send to Claude (Ctrl/Cmd+Enter)"
  aria-label="Send to Claude"></textarea><span id="sendnote" role="status"></span>
<button id="sendbtn" type="submit" title="Ctrl/Cmd+Enter">Send to Claude</button></form>
<script>
const TOKEN = "__TOKEN__";
const COLORS = { accent:"blue", focus:"blue", near:"blue", data:"blue", attention:"yellow", changed:"yellow",
  good:"green", added:"green", removed:"red", network:"magenta",
  agent0:"cyan", agent1:"magenta", agent2:"green", agent3:"yellow", agent4:"blue" };
const BOLD = ["bold", "focus", "heading", "attention"];
// Characters some monospace fonts draw wider than a cell: each is given exactly one cell, so the boxes stay aligned.
const ODD = /[‐-⓿■-⯿\u{1f000}-\u{1faff}]/u;
const ui = { view:"now", part:"", zoom:-1, sel:"", mine:false, history:[], page:null, busy:false, seq:0 };
let cw = 8, lh = 18;
const $ = id => document.getElementById(id);

function measure() {
  const probe = document.createElement("span");
  probe.textContent = "M".repeat(100);
  probe.style.visibility = "hidden"; probe.style.position = "absolute"; probe.style.lineHeight = "normal";
  $("rows").append(probe);
  cw = probe.getBoundingClientRect().width / 100 || 8;
  lh = Math.round(probe.getBoundingClientRect().height) || 18;  // no taller: box lines meet row to row
  probe.remove();
  document.documentElement.style.setProperty("--cw", cw + "px");
  document.documentElement.style.setProperty("--lh", lh + "px");
}
function columns() { return Math.max(40, Math.floor(($("stage").clientWidth - 2) / cw)); }

async function load() {
  if (ui.busy) return;
  ui.busy = true;
  const seq = ++ui.seq;
  const q = new URLSearchParams({ view:ui.view, part:ui.part, width:String(columns()), zoom:String(ui.zoom) });
  try {
    const r = await fetch("pane?" + q, { cache:"no-store" });
    const body = await r.json();
    if (seq !== ui.seq) return;
    if (!r.ok) throw new Error(body.error || r.statusText);
    $("error").textContent = "";
    ui.page = body;
    if (ui.zoom < 0) ui.zoom = body.zoom;
    $("status").textContent = "updated " + new Date().toLocaleTimeString();
    paint();
  } catch (e) {
    $("error").textContent = "The watcher does not answer (" + e.message + "): start system-diff watch again.";
  } finally { ui.busy = false; }
}

function look(run, sel, lit) {
  const words = (run[1] || "").split(" ");
  const ids = (run[2] || "").split(" ").filter(Boolean);
  const picked = sel !== "" && ids.includes(sel);
  const isLit = !picked && ids.some(id => lit.has(id));
  const color = picked ? "blue" : words.map(w => COLORS[w]).find(Boolean);
  const muted = words.includes("muted") || (words.includes("heading") && !color);
  const cls = [];
  if (color) cls.push("c-" + color);
  if (picked || isLit || BOLD.some(w => words.includes(w))) cls.push("b");
  if (muted && !picked && !isLit) cls.push("d");
  if (picked && words.includes("title")) cls.push("inv");
  return cls.join(" ");
}
function text(span, s) {
  if (!ODD.test(s)) { span.textContent = s; return; }
  let plain = "";
  for (const ch of s) {
    if (ODD.test(ch)) {
      if (plain) { span.append(plain); plain = ""; }
      const g = document.createElement("span"); g.className = "g"; g.textContent = ch; span.append(g);
    } else plain += ch;
  }
  if (plain) span.append(plain);
}

function paint() {
  const page = ui.page;
  if (!page) return;
  const sx = $("stage").scrollLeft, sy = window.scrollY;
  const byId = new Map(page.blocks.map(b => [b.id, b]));
  const sel = ui.mine && byId.has(ui.sel) ? ui.sel : byId.has(page.pick) ? page.pick : "";
  ui.sel = sel;
  const picked = byId.get(sel);
  const rows = page.rows.slice();
  if (picked && picked.slot) {
    const [at, n] = picked.slot, detail = page.details[sel] || [];
    for (let i = 0; i < n; i++) rows[at + i] = detail[i] || [];
  }
  const lit = new Set(page.lights[sel] || []);
  const box = $("rows"), hl = $("hl");
  const out = [hl];
  for (const row of rows) {
    const div = document.createElement("div");
    div.className = "r";
    let prev = null, prevCls = null;
    for (const run of row) {
      if (!run[0]) continue;
      const cls = look(run, sel, lit);
      if (prev && cls === prevCls) { text(prev, run[0]); continue; }
      prev = document.createElement("span"); prevCls = cls;
      if (cls) prev.className = cls;
      text(prev, run[0]);
      div.append(prev);
    }
    if (!div.childNodes.length) div.textContent = " ";
    out.push(div);
  }
  box.replaceChildren(...out);
  box.style.width = (page.wide * cw + 2) + "px";
  if (picked) {
    Object.assign(hl.style, { display:"block", left:(picked.x * cw - 2) + "px", top:(picked.y * lh) + "px",
                              width:(picked.w * cw + 4) + "px", height:(picked.h * lh) + "px" });
  } else hl.style.display = "none";
  $("stage").scrollLeft = sx; window.scrollTo(window.scrollX, sy);
  for (const b of document.querySelectorAll("#tabs button"))
    b.setAttribute("aria-selected", String(b.dataset.view === ui.view));
  $("back").disabled = !ui.history.length;
  $("follow").disabled = ui.view === "now" && !ui.part;
  $("out").disabled = ui.zoom >= 2; $("in").disabled = ui.zoom <= 0;
}

function hit(e) {
  const page = ui.page;
  if (!page) return null;
  const r = $("rows").getBoundingClientRect();
  const x = (e.clientX - r.left) / cw, y = (e.clientY - r.top) / lh;
  let best = null;
  for (const b of page.blocks)
    if (x >= b.x && x < b.x + b.w && y >= b.y && y < b.y + b.h && (!best || b.w * b.h < best.w * best.h)) best = b;
  return best;
}
function go(view, part) {
  if (view === ui.view && part === ui.part) return;
  ui.history.push({ view:ui.view, part:ui.part });
  if (ui.history.length > 50) ui.history.shift();
  ui.view = view; ui.part = part; ui.mine = false; ui.sel = "";
  ui.seq++; ui.busy = false; window.scrollTo(0, 0); paint(); load();
}
function back() {
  const h = ui.history.pop();
  if (!h) return;
  ui.view = h.view; ui.part = h.part; ui.mine = false; ui.sel = "";
  ui.seq++; ui.busy = false; paint(); load();
}
function zoomBy(d) {
  const z = Math.max(0, Math.min(2, (ui.zoom < 0 ? 0 : ui.zoom) + d));
  if (z === ui.zoom) return;
  ui.zoom = z; ui.seq++; ui.busy = false; paint(); load();
}

$("rows").addEventListener("mousemove", e => {
  const b = hit(e);
  $("rows").classList.toggle("hot", !!(b && b.enter && b.enter.zoom));
  if (b && b.id !== ui.sel) { ui.sel = b.id; ui.mine = true; paint(); }
});
$("rows").addEventListener("click", e => {
  const b = hit(e);
  if (b && b.enter && b.enter.zoom) go("now", b.enter.zoom);
});
for (const b of document.querySelectorAll("#tabs button")) b.onclick = () => go(b.dataset.view, "");
$("back").onclick = back;
$("follow").onclick = () => go("now", "");
$("out").onclick = () => zoomBy(1);
$("in").onclick = () => zoomBy(-1);
$("seen").onclick = async () => {
  const r = await fetch("seen", { method:"POST", headers:{ "X-Token":TOKEN } });
  if (r.ok) load();
};
let noted = 0;
function note(msg, bad) {
  $("sendnote").textContent = msg; $("sendnote").classList.toggle("bad", !!bad);
  clearTimeout(noted);
  if (!bad) noted = setTimeout(() => { $("sendnote").textContent = ""; }, 5000);
}
async function send() {
  const text = $("say").value.trim();
  if (!text) { note("type something to send", true); return; }
  $("sendbtn").disabled = true;
  try {
    const r = await fetch("send", { method:"POST", headers:{ "X-Token":TOKEN, "Content-Type":"application/json" },
                                    body:JSON.stringify({ text }) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || r.status + " " + r.statusText);
    $("say").value = "";
    note("sent: it reaches Claude through the pane within a few seconds");
  } catch (e) { note("not sent: " + e.message, true); }
  finally { $("sendbtn").disabled = false; }
}
$("send").addEventListener("submit", e => { e.preventDefault(); send(); });
$("say").addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
});
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(TEXTAREA|INPUT|SELECT)$/.test(t.tagName))) return;   // typing, not a shortcut
  const tab = { n:"now", r:"review", a:"agents", t:"topics" }[e.key];
  if (tab) go(tab, "");
  else if (e.key === "b" || e.key === "Backspace") back();
  else if (e.key === "f") go("now", "");
  else if (e.key === "-") zoomBy(1);
  else if (e.key === "+" || e.key === "=") zoomBy(-1);
});
let resized = 0;
window.addEventListener("resize", () => {
  clearTimeout(resized);
  resized = setTimeout(() => { if (ui.page && columns() !== ui.page.width) { ui.seq++; ui.busy = false; load(); } }, 250);
});
measure();
paint();
load();
setInterval(() => { if (!document.hidden) load(); }, 3000);
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
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.replace("__TOKEN__", w.token).encode(), "text/html; charset=utf-8")
            if path == "/classic":
                return self._send(200, CLASSIC.replace("__TOKEN__", w.token).encode(), "text/html; charset=utf-8")
            if path == "/pane":
                return self._pane()
            if self.path.startswith("/state"):
                return self._send(200, json.dumps(w.snapshot_state()).encode(), "application/json")
            return self._send(404, b"not found", "text/plain")

        def _pane(self):
            q = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
            view = q.get("view", "now")
            if view not in VIEWS:
                return self._send(400, json.dumps({"error": f"no view {view!r}"}).encode(), "application/json")
            try:
                width = max(30, min(400, int(q.get("width") or 140)))
                zoom = int(q.get("zoom") or -1)
            except ValueError:
                return self._send(400, json.dumps({"error": "width and zoom are numbers"}).encode(), "application/json")
            try:
                from . import pane   # here: pane may ask this module where the page is
                with PANE_LOCK:  # pane.show sets the module's zoom: one page at a time
                    page = pane.show(w.root, width, view, q.get("part", ""), zoom, take=False)  # the pane delivers
            except Exception as exc:  # noqa: BLE001 - the page shows it and keeps polling
                return self._send(500, json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                                  "application/json")
            return self._send(200, json.dumps(page).encode(), "application/json")

        def _json(self, code: int, obj: Dict[str, Any]) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _same_origin(self) -> bool:
            """The request comes from this server's own page: its Origin (else its Referer) is this origin. A page
            on another site the person visits can reach localhost, but its browser sends that site's origin."""
            own = f"http://127.0.0.1:{self.server.server_address[1]}"
            origin = self.headers.get("Origin")
            if origin is not None:
                return origin.rstrip("/") == own
            referer = self.headers.get("Referer")
            if not referer:
                return False
            r = urlsplit(referer)
            return f"{r.scheme}://{r.netloc}" == own

        def _post_send(self):
            if self.headers.get("X-Token") != w.token or not self._same_origin():
                return self._json(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0 or n > SEND_BODY_MAX:
                    return self._json(400, {"error": "the body is too big"})
                body = json.loads(self.rfile.read(n).decode("utf-8") or "null")
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "the body is JSON: {\"text\": \"...\"}"})
            text = body.get("text") if isinstance(body, dict) else None
            text = text.strip() if isinstance(text, str) else ""
            if not 1 <= len(text) <= SEND_MAX:
                return self._json(400, {"error": f"text is 1 to {SEND_MAX} characters"})
            try:
                line = outbox_put(w.root, text)
            except OSError as exc:
                return self._json(500, {"error": f"could not queue it: {exc}"})
            return self._json(200, {"id": line["id"], "queued": True})

        def do_POST(self):
            if urlsplit(self.path).path == "/send":
                return self._post_send()
            if self.path != "/seen" or self.headers.get("X-Token") != w.token:
                return self._send(403, b"forbidden", "text/plain")
            w.mark_seen()
            return self._send(200, json.dumps(w.snapshot_state()).encode(), "application/json")

    return Handler


VIEWS = ("now", "review", "agents", "topics", "map", "changes")
PANE_LOCK = threading.Lock()
WEB = "web.json"   # where a running watcher listens: {"url", "port", "pid"}, under .git/system-diff


OUTBOX = "outbox.jsonl"   # typed on the page for Claude: {"id", "at", "text"} per line; the pane takes and removes them
SEND_MAX = 8000
SEND_BODY_MAX = 256 * 1024
OUTBOX_LOCK = threading.Lock()


def outbox_put(root: Path, text: str) -> Dict[str, Any]:
    """Queue `text` for the live Claude Code session: append one line to .git/system-diff/outbox.jsonl (one write, so
    a reader never sees half a line) and return it."""
    line = {"id": uuid.uuid4().hex, "at": time.time(), "text": text}
    data = json.dumps(line, ensure_ascii=False) + "\n"
    with OUTBOX_LOCK, open(git_dir(root) / OUTBOX, "a", encoding="utf-8") as f:
        f.write(data)
        f.flush()
    return line


def _web_file(root: Path) -> Path:
    return git_dir(root) / WEB


def _alive(pid: int) -> bool:
    if os.name == "nt":   # os.kill(pid, 0) would end the process there: the port answering says enough
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _answers(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def web_url(root: Path) -> Optional[str]:
    """The URL of the page a watcher for this repository serves, if one is running (its process alive, its port
    answering); else None, and a file left by one that is gone is removed."""
    try:
        f = _web_file(root)
        if not f.exists():
            return None
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
            url, port, pid = str(info["url"]), int(info["port"]), int(info["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            url, port, pid = "", 0, 0
        if port and _alive(pid) and _answers(port):
            return url
        try:
            f.unlink()
        except OSError:
            pass
        return None
    except Exception:  # noqa: BLE001 - not a repository, git missing: no page
        return None


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start_detached(root: Path, port: int = 8765) -> Optional[str]:
    """The URL of this repository's watcher: the running one's, else one started in the background, on the first free
    port from `port` (ten tried), that outlives the caller. None if it could not be started. Never raises."""
    try:
        url = web_url(root)
        if url:
            return url
        free = next((p for p in range(port, port + 10) if _free(p)), None)
        if free is None:
            return None
        kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                              "cwd": str(root), "close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        else:
            kw["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "system_diff", "--repo", str(root), "watch", "--port", str(free)], **kw)
        deadline = time.time() + 3
        while time.time() < deadline:
            url = web_url(root)
            if url:
                return url
            time.sleep(0.1)
        return None
    except Exception:  # noqa: BLE001 - a convenience: never stop the caller
        return None


def _write_web(root: Path, url: str, port: int) -> Optional[Path]:
    try:
        f = _web_file(root)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"url": url, "port": port, "pid": os.getpid()}), encoding="utf-8")
        os.replace(tmp, f)
        return f
    except (OSError, subprocess.CalledProcessError):
        return None


def _forget_web(f: Optional[Path], port: int) -> None:
    """Remove web.json, if it still says this server (another may have started since)."""
    if f is None:
        return
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
        if info.get("port") == port and info.get("pid") == os.getpid():
            f.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def _interrupt(*_: Any) -> None:
    raise KeyboardInterrupt


def serve(root: Path, base: str = "checkpoint", port: int = 8765, config_path: Optional[str] = None,
          interval: float = 2.0, ready=None) -> None:
    """Run until interrupted. `ready(url)` is called once the server listens (tests use port 0). While it runs,
    .git/system-diff/web.json says where (web_url reads it)."""
    w = Watcher(root, base, config_path, interval)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(w))
    thread = threading.Thread(target=w.loop, daemon=True)
    thread.start()
    bound = server.server_address[1]
    url = f"http://127.0.0.1:{bound}/"
    web = _write_web(root, url, bound)
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _interrupt)   # stopped by kill: still forget web.json
    try:
        if ready:
            ready(url, server, w)
        else:
            print(f"system-diff watch: {url} (Ctrl-C to stop)", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        w.stop.set()
        server.server_close()
        _forget_web(web, bound)
