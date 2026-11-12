"""`system-diff watch`: a live page of what changed in the system since the checkpoint, for a person to keep beside the
editor while they, or an AI coding agent, change the code. It redraws when a file changes, and zooms in on the part
being worked on (the one the agent last edited or read, else the one whose files last changed), with what that part
is, what is changing in it and the topics behind it. "Mark as seen" moves the checkpoint, so the page then shows only
what is new to them.

Local only: the server listens on 127.0.0.1, and "Mark as seen" needs the page's own token, so no other web page open in
the same browser can press it. "Send to Claude" (POST /send) needs the token and the page's own origin too, and appends
what was typed to .git/system-diff/outbox.jsonl, where the pane picks it up and submits it to the live session.

The server is also the repository's resident engine (live.Engine): it holds the working tree in memory and every view
asks it for pages. GET /page?view=&width=&zoom=&part=&since=&take= answers {"version", "page"} (pane.show's page),
waiting for a newer version than `since` (a long poll); GET /web gives the web page's data the same way. POST /touch
{"paths": [...]} tells it which files Claude just edited. /page and /touch need the token, which web.json (readable
on this machine only, under the git dir) keeps for the pane and the hooks; /page also answers this server's own page
(same origin).
"""
from __future__ import annotations

import http.client
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

from . import activity, graph, learn, live, pictures
from .compare import Comparison, checkpoint, checkpoint_sha, compare_worktree, turn_sha, worktree_tree
from .diff import diff
from .extract import Code
from .sources import GitSource, git, git_dir

# The one web page: the pane's views (pane.web_view, GET /web) as cards, with the system diagram as inline SVG whose
# parts light up on hover and zoom in on a click; refreshed every 3 s, re-rendering only what changed.
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>System diff</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: light;
  --ink:#161c24; --ink2:#3b4655; --muted:#5f6b7c; --line:#e1e5eb; --bg:#f8f9fb; --card:#ffffff; --paper:#f8f9fb;
  --work:#1f5fbf; --work-bg:#e8f0fc; --ok:#157347; --ok-bg:#e6f4ec; --warn:#8a4d00; --warn-bg:#fff3dd;
  --warn-line:#e9c98f; --attn:#c4321c; --attn-bg:#fdecea; --on-work:#ffffff; --shadow:0 1px 2px rgba(22,28,36,.05);
  --c0:#1f5fbf; --c1:#157347; --c2:#9b3fc2; --c3:#b4540a; --c4:#0b7f8a;
  --sans:'Segoe UI', FreeSans, 'Helvetica Neue', Arial, sans-serif;
  --mono:Consolas, Inconsolata, 'DejaVu Sans Mono', monospace; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark;
  --ink:#e4e9f0; --ink2:#c2cad6; --muted:#97a2b3; --line:#2a3240; --bg:#0f1319; --card:#161b23; --paper:#13171d;
  --work:#6ea8ff; --work-bg:#16243a; --ok:#5fd28a; --ok-bg:#12291c; --warn:#f0c46a; --warn-bg:#2a2214;
  --warn-line:#5c4720; --attn:#ff8a7a; --attn-bg:#2e1714; --on-work:#0f1319; --shadow:none;
  --c0:#6ea8ff; --c1:#5fd28a; --c2:#d08bff; --c3:#ffa95e; --c4:#4fd3dc; } }
:root[data-theme="dark"] { color-scheme: dark;
  --ink:#e4e9f0; --ink2:#c2cad6; --muted:#97a2b3; --line:#2a3240; --bg:#0f1319; --card:#161b23; --paper:#13171d;
  --work:#6ea8ff; --work-bg:#16243a; --ok:#5fd28a; --ok-bg:#12291c; --warn:#f0c46a; --warn-bg:#2a2214;
  --warn-line:#5c4720; --attn:#ff8a7a; --attn-bg:#2e1714; --on-work:#0f1319; --shadow:none;
  --c0:#6ea8ff; --c1:#5fd28a; --c2:#d08bff; --c3:#ffa95e; --c4:#4fd3dc; }
* { box-sizing:border-box; }
html, body { margin:0; background:var(--bg); color:var(--ink); }
body { font:14px/1.5 var(--sans); }
button { font:inherit; font-size:13px; padding:5px 12px; border-radius:8px; border:1px solid var(--line);
         background:var(--card); color:var(--ink); cursor:pointer; }
button:hover:not(:disabled) { border-color:var(--work); }
button:focus-visible, a:focus-visible, textarea:focus-visible { outline:2px solid var(--work); outline-offset:2px; }
button:disabled { opacity:.5; cursor:default; }
button.primary { background:var(--work); border-color:var(--work); color:var(--on-work); font-weight:600; }
a { color:var(--work); }
header.top { position:sticky; top:0; z-index:5; display:flex; flex-wrap:wrap; gap:10px; align-items:center;
             padding:10px 20px; background:var(--card); border-bottom:1px solid var(--line); }
header.top h1 { font-size:15px; margin:0 6px 0 0; font-weight:650; }
.tabs { display:flex; gap:2px; padding:3px; background:var(--bg); border:1px solid var(--line); border-radius:10px; }
.tabs button { border:0; background:transparent; color:var(--muted); padding:4px 12px; border-radius:7px; }
.tabs button[aria-selected="true"] { background:var(--card); color:var(--ink); font-weight:600;
                                      box-shadow:0 0 0 1px var(--line), var(--shadow); }
.tabs kbd, .hint kbd { font:11px var(--mono); color:var(--muted); margin-left:4px; }
.zoom { display:flex; align-items:center; gap:4px; color:var(--muted); font-size:12.5px; }
.zoom button { padding:3px 9px; }
.spacer { flex:1; }
#since { color:var(--muted); font-size:12.5px; }
main { max-width:1240px; margin:0 auto; padding:18px 20px 28px; display:grid; gap:16px; }
main > * { min-width:0; }
pre.lines { white-space:pre-wrap; overflow-wrap:anywhere; }
[hidden] { display:none !important; }
.headline, .card > h2 { overflow-wrap:anywhere; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; box-shadow:var(--shadow); }
.card > h2 { font-size:13px; margin:0 0 10px; color:var(--muted); font-weight:600; letter-spacing:.04em; text-transform:uppercase;
             display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }
.card > h2 .aside { text-transform:none; letter-spacing:0; font-weight:400; }
.headline { font-size:18px; font-weight:600; margin:0; }
.sub { color:var(--muted); font-size:13px; }
.error { color:var(--attn); white-space:pre-wrap; }
.error:empty { display:none; }
#status { display:grid; gap:4px; border-left:4px solid var(--line); }
#status.working { border-left-color:var(--work); }
#status .doing { font-size:15px; font-weight:600; }
#status.working .doing { color:var(--work); }
#status .pulse { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--work); margin-right:8px;
                 vertical-align:middle; animation:pulse 1.6s ease-in-out infinite; }
@keyframes pulse { 50% { opacity:.25; } }
@media (prefers-reduced-motion: reduce) { #status .pulse { animation:none; } * { transition:none !important; } }
.asked { color:var(--muted); }
.asked q { color:var(--ink2); font-style:italic; }
.items { list-style:none; margin:0; padding:0; display:grid; gap:8px; }
.item { display:grid; grid-template-columns:22px 1fr; gap:2px 8px; padding:8px 10px; border-radius:8px;
        border:1px solid var(--line); background:var(--card); }
.item .sign { font-weight:700; text-align:center; color:var(--muted); }
.item .why { grid-column:2; color:var(--muted); font-size:13px; }
.item.st-added .sign, .item.st-ok .sign { color:var(--ok); }
.item.st-changed .sign, .item.st-warn .sign { color:var(--warn); }
.item.st-removed .sign, .item.st-bad .sign { color:var(--attn); }
.attn .item { background:var(--warn-bg); border-color:var(--warn-line); }
.attn .item .sign { color:var(--warn); }
.attn { border-color:var(--warn-line); }
.attn > h2 { color:var(--warn); }
#hovercard { position:fixed; z-index:20; max-width:min(360px, calc(100vw - 16px)); padding:10px 12px; border-radius:10px;
             background:var(--card); color:var(--ink); border:1px solid var(--line); font-size:13px; line-height:1.45;
             box-shadow:0 6px 24px rgba(0,0,0,.18); pointer-events:none; overflow-wrap:anywhere; }
#hovercard .t { font-weight:650; margin-bottom:4px; }
#hovercard .l { color:var(--ink2); }
#hovercard .l + .l { margin-top:3px; }
.lit-ui { box-shadow:0 0 0 2px var(--work) !important; }
.dia { padding:0; overflow:hidden; }
.dia > h2 { padding:14px 16px 0; }
#dia { background:var(--paper); border-top:1px solid var(--line); border-bottom:1px solid var(--line); padding:10px; overflow:auto; }
#dia svg { display:block; width:100%; height:auto; max-width:100%; }
#dia.empty { color:var(--muted); font-size:13px; padding:24px; text-align:center; }
#dia.enterable [data-id].can-enter { cursor:zoom-in; }
#detail { padding:12px 16px; min-height:44px; white-space:pre-wrap; color:var(--ink2); font-size:13.5px; }
#detail .what { color:var(--muted); }
#detail b { color:var(--ink); }
.timeline { list-style:none; margin:0; padding:0 0 0 4px; }
.timeline li { position:relative; display:grid; grid-template-columns:76px 20px 1fr; gap:8px; padding:6px 0; }
.timeline li::before { content:""; position:absolute; left:95px; top:0; bottom:0; width:1px; background:var(--line); }
.timeline .at { color:var(--muted); font-size:12.5px; text-align:right; font-variant-numeric:tabular-nums; }
.timeline .sign { position:relative; z-index:1; background:var(--card); text-align:center; font-weight:700; color:var(--muted); }
.timeline .k-change .sign, .timeline .k-edit .sign { color:var(--warn); }
.timeline .text { font-weight:500; }
.timeline .k-change .text { font-weight:650; }
.timeline .why { color:var(--muted); font-size:13px; }
.timeline code, .chip code { font:12px var(--mono); color:var(--muted); }
pre.lines { margin:0; padding:12px 14px; background:var(--bg); border:1px solid var(--line); border-radius:8px; overflow:auto;
            font:12.5px/1.55 var(--mono); }
pre.lines .s-added { color:var(--ok); } pre.lines .s-changed { color:var(--warn); }
pre.lines .s-removed { color:var(--attn); } pre.lines .s-muted, pre.lines .s-dim { color:var(--muted); }
pre.lines .s-attention { color:var(--attn); font-weight:600; } pre.lines .s-accent { color:var(--work); }
.lanes { display:grid; gap:8px; }
.swim { display:grid; grid-template-columns:minmax(120px, 200px) 1fr; gap:12px; align-items:center; padding:8px 10px;
        border:1px solid var(--line); border-left:4px solid var(--lane); border-radius:8px; cursor:default; }
.swim .who { display:grid; gap:1px; min-width:0; }
.swim .name { font-weight:650; color:var(--lane); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.swim .area, .swim .doing { color:var(--muted); font-size:12.5px; overflow:hidden; text-overflow:ellipsis; }
.swim .track { position:relative; height:34px; background:var(--bg); border-radius:6px; overflow:hidden; }
.swim .done { position:absolute; left:0; top:0; bottom:0; background:var(--lane); opacity:.14; }
.swim .mark { position:absolute; top:50%; transform:translate(-50%, -50%); font-size:15px; line-height:1; color:var(--lane); }
.swim .badges { position:absolute; right:6px; top:50%; transform:translateY(-50%); display:flex; gap:6px; font-size:12px; }
.badge { padding:1px 7px; border-radius:999px; background:var(--card); border:1px solid var(--line); color:var(--muted); }
.badge.unread { color:var(--warn); border-color:var(--warn-line); background:var(--warn-bg); }
.badge.live { color:var(--ok); border-color:var(--ok); }
.lc0 { --lane:var(--c0); } .lc1 { --lane:var(--c1); } .lc2 { --lane:var(--c2); } .lc3 { --lane:var(--c3); } .lc4 { --lane:var(--c4); }
.legend { color:var(--muted); font-size:12.5px; display:flex; gap:14px; flex-wrap:wrap; }
.layer { margin:0 0 12px; } .layer:last-child { margin:0; }
.layer h3 { font-size:12.5px; margin:0 0 6px; color:var(--ink2); font-weight:600; }
.chips { display:flex; flex-wrap:wrap; gap:6px; }
.chip { font:inherit; font-size:13px; padding:4px 11px; border-radius:999px; border:1px solid var(--line); background:var(--bg);
        color:var(--ink); }
.chip.learning { border-color:var(--work); color:var(--work); background:var(--work-bg); }
.chip .std { color:var(--muted); font-size:11px; margin-left:4px; }
#chipinfo { margin-top:10px; padding:10px 12px; border-radius:8px; background:var(--bg); font-size:13.5px; display:grid; gap:4px; }
#chipinfo:empty { display:none; }
.empty-note { color:var(--muted); font-size:13px; }
form.send { position:sticky; bottom:0; z-index:5; display:flex; gap:10px; align-items:flex-end; padding:10px 20px;
            background:var(--card); border-top:1px solid var(--line); }
form.send label { font-weight:600; font-size:13px; align-self:center; white-space:nowrap; }
#say { flex:1; font:inherit; font-size:13.5px; min-height:2.6em; max-height:30vh; resize:vertical; padding:7px 10px;
       border-radius:8px; border:1px solid var(--line); background:var(--bg); color:var(--ink); }
#sendnote { color:var(--muted); font-size:12.5px; max-width:30vw; }
#sendnote.bad { color:var(--attn); }
.hint { color:var(--muted); font-size:12px; text-align:center; padding:0 0 10px; }
@media (max-width:720px) {
  header.top, main, form.send { padding-left:16px; padding-right:16px; }
  .swim { grid-template-columns:1fr; }
  .timeline li { grid-template-columns:56px 20px 1fr; } .timeline li::before { left:75px; }
  form.send label, #sendnote { display:none; }
}
</style></head><body>
<header class="top">
  <h1>System diff</h1>
  <div class="tabs" role="tablist" id="tabs">
    <button role="tab" data-view="now">Now<kbd>n</kbd></button>
    <button role="tab" data-view="review">Review<kbd>r</kbd></button>
    <button role="tab" data-view="agents">Agents<kbd>a</kbd></button>
    <button role="tab" data-view="topics">Topics<kbd>t</kbd></button>
  </div>
  <button id="follow" type="button" title="Go to what Claude is working on (f)">Follow Claude</button>
  <button id="back" type="button" title="Back (b)">Back</button>
  <div class="zoom" title="How much of the system the diagram shows">
    <button id="in" type="button" aria-label="Zoom in">+</button><span id="zoomlabel">auto</span>
    <button id="out" type="button" aria-label="Zoom out">−</button></div>
  <span class="spacer"></span>
  <button id="theme" type="button" title="Light, dark, or as your system is">◐ auto</button>
</header>
<main>
  <div><p class="headline" id="headline">Reading the code…</p><div class="error" id="error"></div></div>
  <section class="card" id="status" hidden></section>
  <div id="before" style="display:grid;gap:16px"></div>
  <section class="card dia" id="diacard" hidden><h2 id="diatitle"></h2>
    <div class="diawrap" id="diawrap"><div id="dia" role="img"></div><div id="detail" aria-live="polite"></div></div></section>
  <div id="after" style="display:grid;gap:16px"></div>
</main>
<div class="hint"><kbd>n</kbd> <kbd>r</kbd> <kbd>a</kbd> <kbd>t</kbd> views · <kbd>f</kbd> follow Claude · <kbd>b</kbd> back · <kbd>+</kbd> <kbd>−</kbd> zoom · hover the diagram to see how parts connect, click to zoom in</div>
<form class="send" id="send"><label for="say">Send to Claude</label>
  <textarea id="say" placeholder="Ask or tell Claude something about this change (Ctrl+Enter sends)"></textarea>
  <span id="sendnote" aria-live="polite"></span><button id="sendbtn" class="primary" type="submit">Send</button></form>
<script>
const TOKEN = "__TOKEN__";
const $ = id => document.getElementById(id);
const VIEWS = ["now", "review", "agents", "topics"];
const ui = { view:"now", part:"", zoom:-1, history:[], data:null, last:{}, pick:"", mine:false, hover:null, seq:0 };
(function fromHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (VIEWS.includes(h.get("view"))) ui.view = h.get("view");
  ui.part = h.get("part") || "";
  const z = parseInt(h.get("zoom"), 10); if (z >= -1 && z <= 2) ui.zoom = z;
})();
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null && text !== "") e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function ago(at) {
  if (typeof at !== "number") return at || "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - at));
  return s < 5 ? "just now" : s < 60 ? s + "s ago" : s < 3600 ? Math.floor(s / 60) + " min ago" : Math.floor(s / 3600) + " h ago";
}
function card(title, aside, cls) {
  const c = el("section", null, "card" + (cls ? " " + cls : "")), h = el("h2", title);
  const said = $("headline").textContent.trim();   // already the page's headline: not said twice
  if (aside && !(said && aside.replace(/^plan:\s*/, "").trim() === said)) h.append(el("span", aside, "aside"));
  c.append(h);
  return c;
}
function safeLink(href) { return /^(https?:)?\/\//i.test(href || "") || /^[\w./#?=&-]+$/.test(href || "") ? href : ""; }

// ---- sections, one renderer per kind ----
function isAttention(s) { return s.kind === "notes" && /attention|needs you|warn/i.test(s.title || ""); }
function notes(s) {
  const c = card(s.title, s.aside, isAttention(s) ? "attn" : ""), ul = el("ul", null, "items");
  for (const n of s.items || []) {
    const li = el("li", null, "item st-" + (n.status || "same"));
    li.append(el("span", n.sign || "•", "sign"), el("span", n.text, "text"));
    if (n.why) li.append(el("span", n.why, "why"));
    if (n.id) { li.dataset.hid = n.id; if (hoverOf(n.id)) li.tabIndex = 0; }
    ul.append(li);
  }
  if (!(s.items || []).length) c.append(el("div", "Nothing here yet.", "empty-note")); else c.append(ul);
  return c;
}
function steps(s) {
  const c = card(s.title, s.aside), ol = el("ol", null, "timeline");
  for (const st of s.items || []) {
    const li = el("li", null, "k-" + (st.kind || "read")), body = el("div");
    body.append(el("div", st.text, "text"));
    if (st.why || st.file) {
      const w = el("div", st.why || "", "why");
      if (st.file) { if (st.why) w.append(" · "); w.append(el("code", st.file)); }
      body.append(w);
    }
    li.append(el("span", ago(st.at), "at"), el("span", st.sign || "·", "sign"), body);
    ol.append(li);
  }
  if (!(s.items || []).length) c.append(el("div", "Nothing yet.", "empty-note")); else c.append(ol);
  return c;
}
const MARKS = { read:"◎", look:"◎", edit:"✎", write:"✎", change:"▲", warn:"▲", collision:"▲", clash:"▲", message:"✉", msg:"✉",
                publish:"✉", mail:"✉" };
function lanes(s) {
  const c = card(s.title, s.aside), box = el("div", null, "lanes");
  for (const l of s.items || []) {
    const row = el("div", null, "swim lc" + ((l.colour | 0) % 5)), who = el("div", null, "who");
    row.dataset.blocks = JSON.stringify(l.blocks || []); row.dataset.pid = "ag:" + l.name;
    row.tabIndex = 0;
    who.append(el("span", l.name, "name"));
    if (l.area) who.append(el("span", l.area, "area"));
    if (l.doing) who.append(el("span", l.doing, "doing"));
    const track = el("div", null, "track"), p = l.progress;
    if (typeof p === "number" && p >= 0 && p <= 1) { const d = el("div", null, "done"); d.style.width = (p * 100) + "%"; track.append(d); }
    for (const m of l.marks || []) {
      const k = String(m.kind || ""), icon = MARKS[k.toLowerCase()] || (k.length <= 2 ? k : "•");
      const mk = el("span", icon, "mark");
      mk.style.left = (Math.max(0.01, Math.min(0.99, +m.pos || 0)) * 100) + "%";
      mk.title = k;
      track.append(mk);
    }
    const badges = el("div", null, "badges");
    if (l.live) badges.append(el("span", "live", "badge live"));
    if (typeof p === "string" && p) badges.append(el("span", p, "badge"));
    else if (typeof p === "number" && p > 1) badges.append(el("span", String(p), "badge"));
    if (l.unread) badges.append(el("span", "✉ " + l.unread + " unread", "badge unread"));
    track.append(badges);
    row.append(who, track);
    const on = () => { ui.hover = { ids:lightsOf("ag:" + l.name, l.blocks), from:row }; applyPick(); };
    const off = () => { ui.hover = null; applyPick(); };
    row.addEventListener("mouseenter", on); row.addEventListener("focus", on);
    row.addEventListener("mouseleave", off); row.addEventListener("blur", off);
    box.append(row);
  }
  if (!(s.items || []).length) c.append(el("div", "No agents in this team.", "empty-note"));
  else {
    c.append(box);
    c.append(el("div", "◎ read · ✎ edit · ▲ changed the system · ✉ message", "legend"));
  }
  return c;
}
function chips(s) {
  const c = card(s.title, s.aside), layers = new Map();
  for (const t of s.items || []) {
    const k = t.layer || "Other";
    if (!layers.has(k)) layers.set(k, []);
    layers.get(k).push(t);
  }
  const info = el("div"); info.id = "chipinfo";
  for (const [name, list] of layers) {
    const box = el("div", null, "layer"), row = el("div", null, "chips");
    box.append(el("h3", name));
    for (const t of list) {
      const b = el("button", t.title, "chip" + (t.learning ? " learning" : ""));
      b.type = "button"; b.dataset.parts = JSON.stringify(t.parts || []); b.dataset.pid = "t:" + t.key;
      if (t.stdlib) b.append(el("span", "stdlib", "std"));
      const on = () => {
        ui.hover = { ids:lightsOf("t:" + t.key, t.parts), from:b }; applyPick();
        info.replaceChildren();
        const head = el("div"); head.append(el("b", t.title));
        if (t.role) head.append(" · " + t.role);
        info.append(head);
        if (t.what) info.append(el("div", t.what));
        const href = safeLink(t.link);
        if (href) { const a = el("a", "Read more"); a.href = href; a.target = "_blank"; a.rel = "noopener"; info.append(a); }
      };
      const off = () => { ui.hover = null; applyPick(); };
      b.addEventListener("mouseenter", on); b.addEventListener("focus", on);
      b.addEventListener("mouseleave", off); b.addEventListener("blur", off);
      row.append(b);
    }
    box.append(row); c.append(box);
  }
  if (!(s.items || []).length) c.append(el("div", "No topics yet.", "empty-note")); else c.append(info);
  return c;
}
function lines(s) {
  const c = card(s.title, s.aside), pre = el("pre", null, "lines");
  for (const ln of s.items || []) {
    const t = typeof ln === "string" ? { text:ln } : ln;
    pre.append(el("span", t.text, t.style ? "s-" + t.style : ""), "\n");
  }
  c.append(pre);
  return c;
}
const RENDER = { notes, steps, lanes, chips, lines };

// ---- hover cards: the whole story of a box, an item or an agent (the view's "hover", by the id the page shows) ----
function hoverOf(id) {
  const h = ui.data && ui.data.hover;
  if (!h || !id || typeof h !== "object") return null;
  const c = Object.prototype.hasOwnProperty.call(h, id) ? h[id] : null;
  return c && typeof c === "object" ? c : null;
}
function hoverId(n) { return n.dataset.hid || n.dataset.pid || n.getAttribute("data-id") || ""; }
const tip = { timer:0, for:null, box:null };
function tipBox() {
  if (!tip.box) { tip.box = el("div"); tip.box.id = "hovercard"; tip.box.setAttribute("role", "tooltip");
                  tip.box.hidden = true; document.body.append(tip.box); }
  return tip.box;
}
function hideTip() { clearTimeout(tip.timer); tip.for = null; if (tip.box) tip.box.hidden = true; }
function showTipSoon(n) {
  const card = hoverOf(hoverId(n));
  if (!card) { hideTip(); return; }
  if (tip.for === n && !tip.box.hidden) return;
  clearTimeout(tip.timer); tip.for = n;
  tip.timer = setTimeout(() => {
    if (tip.for !== n || !n.isConnected) return;
    const box = tipBox();
    box.replaceChildren();
    if (card.title) box.append(el("div", String(card.title), "t"));
    for (const line of Array.isArray(card.lines) ? card.lines : []) box.append(el("div", String(line), "l"));
    box.hidden = false;
    const r = n.getBoundingClientRect(), w = box.offsetWidth, h = box.offsetHeight, vw = innerWidth, vh = innerHeight;
    let x = Math.min(Math.max(8, r.left), vw - w - 8), y = r.bottom + 8;
    if (y + h > vh - 8) y = r.top - h - 8;          // no room below: above
    if (y < 8) y = Math.max(8, Math.min(vh - h - 8, r.top + 8));
    box.style.left = Math.max(8, x) + "px"; box.style.top = y + "px";
  }, 150);
}
function tipTarget(t) { return t && t.closest ? t.closest("[data-hid],[data-pid],#dia [data-id]") : null; }
document.addEventListener("mouseover", e => { const n = tipTarget(e.target); if (n) showTipSoon(n); else if (tip.for) hideTip(); });
document.addEventListener("focusin", e => { const n = tipTarget(e.target); if (n) showTipSoon(n); else hideTip(); });
document.addEventListener("focusout", () => hideTip());
addEventListener("scroll", () => hideTip(), { passive:true });
function section(s) { return (RENDER[s.kind] || notes)(s); }
function goesBefore(s) { return s.kind === "lanes" || s.kind === "chips" || isAttention(s); }

// ---- the diagram: hover picks, lights and dims; a click zooms in ----
function lightsOf(pid, own) {
  const g = (ui.data && ui.data.diagram) || {};
  return (g.lights && g.lights[pid]) || own || [];
}
function rawId(d) { return d && d.startsWith("b:") ? d.slice(2) : d; }
function keyed(map, d) { return map ? (map[d] !== undefined ? map[d] : map[rawId(d)]) : undefined; }
const THEMES = ["auto", "light", "dark"];
function setTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  $("theme").textContent = { auto: "◐ auto", light: "☀ light", dark: "☾ dark" }[t];
  try { localStorage.setItem("sd-theme", t); } catch (e) {}
}
(function () { let t = "auto"; try { t = localStorage.getItem("sd-theme") || "auto"; } catch (e) {}
  setTheme(THEMES.includes(t) ? t : "auto"); })();
$("theme").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme || "auto";
  setTheme(THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]);
});
function applyPick() {
  const g = (ui.data && ui.data.diagram) || {}, nodes = $("dia").querySelectorAll("[data-id]");
  let picked = "", ids = [];
  if (ui.hover) ids = ui.hover.ids;
  else if (ui.pick) { picked = ui.pick; ids = keyed(g.lights, picked) || []; }
  const lit = new Set();
  for (const x of ids) { lit.add(x); lit.add("b:" + x); }
  const active = !!ui.hover || (!!picked && ui.over);   // dim the rest only while the mouse is on the diagram
  for (const n of nodes) {
    const d = n.getAttribute("data-id");
    n.classList.toggle("picked", !!picked && d === picked);
    n.classList.toggle("lit", lit.has(d) && d !== picked);
    n.classList.toggle("dim", active && d !== picked && !lit.has(d));
  }
  // a container holding what is picked or lit is not dimmed: its dimming would fade them too
  for (const n of $("dia").querySelectorAll(".dim")) if (n.querySelector(".picked, .lit")) n.classList.remove("dim");
  // the lanes and chips that hold the picked block (or are picked, or lit by it) light up too
  for (const e of document.querySelectorAll("[data-pid]")) {
    const own = JSON.parse(e.dataset.blocks || e.dataset.parts || "[]"), pid = e.dataset.pid;
    const on = ui.hover ? ui.hover.from === e
      : !!picked && (pid === picked || lit.has(pid) || own.includes(picked) || own.includes(rawId(picked)));
    e.classList.toggle("lit-ui", on);
  }
  const det = $("detail"); det.replaceChildren();
  if (picked) {
    const text = keyed(g.details, picked), target = keyed(g.enter, picked);
    if (text) det.append(el("span", text));
    if (target) det.append(el("div", "Click to zoom into " + target + ".", "what"));
    if (!text && !target) det.append(el("span", rawId(picked), "what"));
  } else det.append(el("span", g.aside || "Hover a part to see what it is and what it connects to.", "what"));
}
function pickAt(target) {
  const n = target && target.closest && target.closest("#dia [data-id]");
  return n ? n.getAttribute("data-id") : "";
}
$("dia").addEventListener("mouseenter", () => { ui.over = true; applyPick(); });
$("dia").addEventListener("mouseleave", () => { ui.over = false; applyPick(); });
$("dia").addEventListener("mouseover", e => {
  const d = pickAt(e.target);
  if (d && d !== ui.pick) { ui.pick = d; ui.mine = true; applyPick(); }
});
$("dia").addEventListener("focusin", e => {
  const d = pickAt(e.target);
  if (d) { ui.pick = d; ui.mine = true; ui.over = true; applyPick(); }
});
$("dia").addEventListener("focusout", () => { ui.over = false; applyPick(); });
$("dia").addEventListener("click", e => {
  const d = pickAt(e.target), g = (ui.data && ui.data.diagram) || {}, t = d && keyed(g.enter, d);
  if (t) go("now", t);
});
function drawDiagram(g) {
  const box = $("dia");
  box.innerHTML = g.svg || "";
  box.classList.toggle("empty", !g.svg);
  if (!g.svg) box.textContent = "No diagram for this view.";
  box.setAttribute("aria-label", g.title || "System diagram");
  const svg = box.querySelector("svg");
  if (svg) {
    svg.removeAttribute("width"); svg.removeAttribute("height");
    const vb = (svg.getAttribute("viewBox") || "").split(/[ ,]+/).map(Number);
    if (vb.length === 4 && vb[2] > 0) {
      svg.style.maxWidth = Math.round(vb[2] * 1.1) + "px"; svg.style.margin = "0";
    }
  }
  for (const n of box.querySelectorAll("[data-id]")) {
    if (keyed(g.enter, n.getAttribute("data-id"))) n.classList.add("can-enter");
    if (hoverOf(n.getAttribute("data-id"))) n.setAttribute("tabindex", "0");
  }
  box.classList.toggle("enterable", !!(g.enter && Object.keys(g.enter).length));
}

// ---- the page: re-render only what changed ----
function changed(key, value) {
  const s = JSON.stringify(value === undefined ? null : value);
  if (ui.last[key] === s) return false;
  ui.last[key] = s; return true;
}
function render(d) {
  ui.data = d;
  if (changed("head", [d.title, d.headline])) {
    $("headline").textContent = d.headline || d.title || "";
    document.title = "System diff" + (d.title ? " · " + d.title : "");
  }
  const st = d.status;
  if (changed("status", st)) {
    const box = $("status");
    box.replaceChildren();
    box.hidden = !st || !(st.doing || st.prompt || st.part);
    if (st) {
      box.classList.toggle("working", !!st.working);
      const doing = el("div", null, "doing");
      if (st.working) doing.append(el("span", null, "pulse"));
      doing.append(st.working ? "Claude is working" + (st.part ? " on " + st.part : "") : "Claude is idle" + (st.part ? " · last in " + st.part : ""));
      box.append(doing);
      if (st.doing) box.append(el("div", st.doing.charAt(0).toUpperCase() + st.doing.slice(1) + (st.part ? " " + st.part : ""), "sub"));
      if (st.prompt) { const a = el("div", st.working ? "You asked: " : "You last asked: ", "asked"); a.append(el("q", st.prompt)); box.append(a); }
    }
  }
  const secs = d.sections || [], before = secs.filter(goesBefore), after = secs.filter(s => !goesBefore(s));
  let redrawn = false;
  if (changed("before", before)) { $("before").replaceChildren(...before.map(section)); redrawn = true; }
  const g = d.diagram || {};
  if (changed("svg", [g.svg, d.hover ? Object.keys(d.hover).length : 0])) { drawDiagram(g); redrawn = true; }
  if (tip.for && !tip.for.isConnected) hideTip();
  if (changed("dia", [g.title, g.aside, g.lights, g.details, g.enter, g.pick])) {
    $("diacard").hidden = !g.svg;
    const h = $("diatitle"); h.replaceChildren(g.title || "Diagram");
    if (g.aside) h.append(el("span", g.aside, "aside"));
    if (!g.svg) $("diacard").hidden = true;
    redrawn = true;
  }
  if (!ui.mine) ui.pick = g.pick ? (g.pick.includes(":") ? g.pick : "b:" + g.pick) : "";
  if (changed("after", after)) { $("after").replaceChildren(...after.map(section)); redrawn = true; }
  if (ui.hover && !ui.hover.from.isConnected) ui.hover = null;   // its lane or chip was just redrawn
  if (redrawn || !ui.mine) applyPick();
}
function paintChrome() {
  for (const b of document.querySelectorAll("#tabs button")) b.setAttribute("aria-selected", String(b.dataset.view === ui.view));
  $("back").disabled = !ui.history.length;
  $("zoomlabel").textContent = ui.zoom < 0 ? "auto" : ["detailed", "compact", "outline"][ui.zoom];
  $("in").disabled = ui.zoom === 0; $("out").disabled = ui.zoom === 2;
  const h = new URLSearchParams({ view:ui.view }); if (ui.part) h.set("part", ui.part); if (ui.zoom >= 0) h.set("zoom", ui.zoom);
  history.replaceState(null, "", "#" + h);
}
// The engine numbers each state of the system; the page asks for the next one and the server answers when it comes
// (a long poll, ~25 s at most), so the page follows within a moment of a change and does nothing in between.
let polling = null;
async function load(wait) {
  const seq = ++ui.seq;
  if (polling) { polling.abort(); polling = null; }
  const q = new URLSearchParams({ view:ui.view, part:ui.part, zoom:String(ui.zoom) });
  if (wait && typeof ui.version === "number") q.set("since", String(ui.version));
  const ctl = new AbortController(); polling = ctl;
  try {
    const r = await fetch("web?" + q, { cache:"no-store", signal:ctl.signal, headers:{ "X-Token":TOKEN } });
    const d = await r.json().catch(() => ({}));
    if (seq !== ui.seq) return false;   // the person moved on while this loaded
    if (!r.ok) throw new Error(d.error || r.status + " " + r.statusText);
    $("error").textContent = "";
    if (typeof d.version === "number") ui.version = d.version;
    render(d);
    return true;
  } catch (e) {
    if (seq !== ui.seq || (e && e.name === "AbortError")) return false;
    $("error").textContent = e instanceof TypeError ? "The watcher stopped: start system-diff watch again." : String(e.message || e);
    throw e;
  } finally { if (polling === ctl) polling = null; }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function follow() {
  for (;;) {
    if (document.hidden) { await sleep(1000); continue; }
    try { await load(true); } catch (e) { await sleep(3000); }
  }
}
function go(view, part) {
  if (view === ui.view && part === ui.part) return;
  ui.history.push({ view:ui.view, part:ui.part });
  if (ui.history.length > 50) ui.history.shift();
  ui.view = view; ui.part = part; reset(true);
}
function reset(top) {
  ui.mine = false; ui.pick = ""; ui.hover = null; ui.last = {}; ui.version = null; hideTip();
  if (top) window.scrollTo(0, 0);
  paintChrome(); load();
}
function back() {
  const h = ui.history.pop();
  if (!h) return;
  ui.view = h.view; ui.part = h.part; reset(true);
}
function zoomBy(d) {
  const z = Math.max(0, Math.min(2, (ui.zoom < 0 ? 0 : ui.zoom) + d));
  if (z === ui.zoom) return;
  ui.zoom = z; ui.last = {}; ui.version = null; paintChrome(); load();
}
for (const b of document.querySelectorAll("#tabs button")) b.onclick = () => go(b.dataset.view, "");
$("back").onclick = back;
$("follow").onclick = () => go("now", "");
$("out").onclick = () => zoomBy(1);
$("in").onclick = () => zoomBy(-1);
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
  else if (e.key === "Escape") { hideTip(); ui.mine = false; ui.pick = ""; ui.last.dia = ""; ui.hover = null; render(ui.data || {}); }
});
paintChrome();
follow();
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
        # who this server is, for web_url to tell it from another program that took its port: kept in web.json and
        # answered at GET /whoami. It opens nothing (unlike the token, which /seen and /send need).
        self.ident = secrets.token_hex(16)
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
        self.engine: Optional[live.Engine] = None   # the resident engine, when serving: it knows the working tree
        self.pages: Optional[live.Pages] = None

    def key(self) -> tuple:
        e = self.engine
        if e is not None:   # no snapshot: the engine's tree, and how far its reading got
            e.ready.wait(live.FIRST_WAIT)
            if e.tree:
                return e.tree, checkpoint_sha(self.root), turn_sha(self.root), e.reads
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
        seen = -1
        while not self.stop.is_set():
            self.tick()
            if self.engine is not None:   # woken by the engine's next version, not by a timer only
                seen = self.engine.wait(self.engine.version if seen >= 0 else -1, self.interval)
                seen = max(seen, 0)
                if self.stop.is_set():
                    return
            else:
                self.stop.wait(self.interval)

    def mark_seen(self) -> None:
        with self.busy:
            e = self.engine
            if e is not None and e.commit:
                e.sync()   # what is on disk this moment, then the engine's commit of it: no snapshot
                checkpoint(self.root, e.commit)
            else:
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
            if path == "/web":
                return self._web()
            if path == "/page":
                return self._page()
            if path == "/whoami":   # this is system-diff's server for the repository whose web.json names this id
                return self._json(200, {"app": "system-diff", "id": w.ident, "pid": os.getpid()})
            if self.path.startswith("/state"):
                return self._send(200, json.dumps(w.snapshot_state()).encode(), "application/json")
            return self._send(404, b"not found", "text/plain")

        def _query(self) -> Dict[str, str]:
            return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

        def _since(self, q: Dict[str, str]) -> Optional[int]:
            try:
                return int(q["since"]) if q.get("since", "") != "" else None
            except ValueError:
                raise ValueError("since is a version number") from None

        def _wait(self, since: Optional[int], key: tuple, take: bool = False) -> None:
            """A long poll: while the engine's version is still `since` and this very page is drawn at it, wait for
            the next version (at most LONG_POLL). A page not drawn at it yet (another width, say) is answered at
            once; with `take`, also as soon as the web page sends something to Claude."""
            e = w.engine
            if e is None or since is None or since != e.version or w.pages is None or not w.pages.current(key):
                return
            box = e._sd / OUTBOX
            e.wait(since, live.LONG_POLL, (lambda: box.exists()) if take else None)

        def _web(self):
            """One view of the pane as data for the page: pane.web_view's JSON, with the engine's "version" it was
            drawn at; with `since` (the version the page has), a long poll. It never takes the outbox (the pane,
            which submits what was sent to Claude, does)."""
            q = self._query()
            view = q.get("view", "now")
            if view not in VIEWS:
                return self._json(400, {"error": f"no view {view!r}"})
            try:
                zoom = int(q.get("zoom") or -1)
                since = self._since(q)
            except ValueError as exc:
                return self._json(400, {"error": "zoom is a number" if "since" not in str(exc) else str(exc)})
            if not -1 <= zoom <= 2:
                return self._json(400, {"error": "zoom is -1 (auto), 0, 1 or 2"})
            part = q.get("part", "")[:200]
            try:
                key = ("web", view, zoom, part)
                self._wait(since, key)
                version, page = _page_of(w, key)
            except Exception as exc:  # noqa: BLE001 - the page shows it and keeps polling
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._json(200, {**page, "version": version})

        def _authorized(self) -> bool:
            """The token (the pane and the hooks read it from web.json), or this server's own page."""
            return live.token_ok(self.headers.get("X-Token"), w.token) or self._same_origin()

        def _page(self):
            """{"version", "page"}: pane.show's page for the view, drawn at that version of the engine's model (kept
            and answered again until the version moves). `since`: wait for a version newer than that first (at most
            ~25 s). `take=1` (the pane's): the page also hands over what the web page sent to Claude."""
            if not self._authorized():
                return self._json(403, {"error": "forbidden"})
            q = self._query()
            view = q.get("view", "now")
            if view not in VIEWS:
                return self._json(400, {"error": f"no view {view!r}"})
            try:
                width, zoom = int(q.get("width") or 80), int(q.get("zoom") or -1)
                since = self._since(q)
            except ValueError:
                return self._json(400, {"error": "width, zoom and since are numbers"})
            if not 10 <= width <= 1000 or not -1 <= zoom <= 2:
                return self._json(400, {"error": "width is 10 to 1000, zoom -1 (auto), 0, 1 or 2"})
            take = q.get("take", "0") == "1"
            part = q.get("part", "")[:200]
            try:
                key = ("pane", view, width, zoom, part)
                self._wait(since, key, take)
                version, page = _page_of(w, key)
                if take:   # drawn once per version; what was sent to Claude is taken fresh for each ask
                    from . import pane
                    page = {**page, "send": pane.take_outbox(w.root)}
            except Exception as exc:  # noqa: BLE001 - the pane shows it and asks again
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._json(200, {"version": version, "page": page})

        def _post_touch(self):
            """The files Claude just edited: {"paths": [...]}, relative to the repository or absolute. Token only."""
            if not live.token_ok(self.headers.get("X-Token"), w.token) or (
                    self.headers.get("Origin") is not None and not self._same_origin()):
                return self._json(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0 or n > SEND_BODY_MAX:
                    return self._json(400, {"error": "the body is too big"})
                body = json.loads(self.rfile.read(n).decode("utf-8") or "null")
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "the body is JSON: {\"paths\": [...]}"})
            paths = body.get("paths") if isinstance(body, dict) else None
            if not isinstance(paths, list) or not all(isinstance(x, str) for x in paths) or len(paths) > 1000:
                return self._json(400, {"error": "paths is a list of up to 1000 file paths"})
            if w.engine is None:
                return self._json(503, {"error": "no engine"})
            try:
                version = w.engine.touch(paths)
            except Exception as exc:  # noqa: BLE001 - the engine's next look finds the edit anyway
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._json(200, {"version": version})

        def _post_quit(self):
            """Stop this engine: a newer system-diff replacing it (its code is loaded once, so after an upgrade the
            running one would go on serving the old pages). Token only, never from a browser."""
            if not live.token_ok(self.headers.get("X-Token"), w.token) or self.headers.get("Origin") is not None:
                return self._json(403, {"error": "forbidden"})
            self._json(200, {"quitting": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def _json(self, code: int, obj: Dict[str, Any]) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _same_origin(self) -> bool:
            """The request comes from this server's own page: its Origin (else its Referer) is this origin. A page
            on another site the person visits can reach localhost, but its browser sends that site's origin."""
            own = origins(self.server.server_address[1])
            origin = self.headers.get("Origin")
            if origin is not None:
                return origin.rstrip("/") in own
            referer = self.headers.get("Referer")
            if not referer:
                return False
            r = urlsplit(referer)
            return f"{r.scheme}://{r.netloc}" in own

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
            if w.engine is not None:
                w.engine.poke()   # the pane's long poll takes it now
            return self._json(200, {"id": line["id"], "queued": True})

        def do_POST(self):
            if urlsplit(self.path).path == "/send":
                return self._post_send()
            if urlsplit(self.path).path == "/touch":
                return self._post_touch()
            if urlsplit(self.path).path == "/quit":
                return self._post_quit()
            if self.path != "/seen" or self.headers.get("X-Token") != w.token:
                return self._send(403, b"forbidden", "text/plain")
            w.mark_seen()
            return self._send(200, json.dumps(w.snapshot_state()).encode(), "application/json")

    return Handler


VIEWS = ("now", "review", "agents", "topics")
PANE_LOCK = threading.Lock()


def _build(w: "Watcher", key: tuple) -> Dict[str, Any]:
    """Draw one page: ("pane", view, width, zoom, part) is pane.show's (without the outbox), ("web", view, zoom,
    part) pane.web_view's. In this process compare() takes the working tree from the engine."""
    from . import pane   # here: pane may ask this module where the page is
    with PANE_LOCK:  # the pane's views set its module's zoom: one view at a time
        if key[0] == "pane":
            _k, view, width, zoom, part = key
            return pane.show(w.root, width, view, part or None, zoom, take=False)
        _k, view, zoom, part = key
        return pane.web_view(w.root, view, part, zoom)


def _page_of(w: "Watcher", key: tuple) -> tuple:
    """(version, page) for a page's key: from the engine's pages when there is an engine, else drawn now."""
    if w.pages is not None:
        return w.pages.get(key)
    return (w.engine.version if w.engine else 0), _build(w, key)
WEB = "web.json"   # where a running watcher listens: {"url", "port", "pid", "id"}, under .git/system-diff
STARTING = "web.starting"   # a watcher being started: {"pid", "port", "at"}, until it has written web.json
STARTING_FOR = 60.0         # how long a start is waited for (a large repository, a slow disk) before another is tried


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
    if os.name == "nt":   # os.kill(pid, 0) would end the process there: GET /whoami says enough
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def forwarded(port: int, env=os.environ) -> str:
    """The page's address in the person's own browser when this machine is a GitHub Codespace (the port forwarded
    to https://<codespace>-<port>.<domain>), else ""."""
    name, domain = env.get("CODESPACE_NAME", ""), env.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
    return f"https://{name}-{port}.{domain}/" if name and domain else ""


def origins(port: int, env=os.environ) -> set:
    """Where this server's own page can be opened from: on this machine, or forwarded to the person's browser."""
    out = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    if forwarded(port, env):
        out.add(forwarded(port, env).rstrip("/"))
    return out


def _whoami(port: int, ident: str, timeout: float = 1.0) -> Optional[bool]:
    """Whether the server on `port` is the one web.json names: True when its GET /whoami gives web.json's id; False
    when nothing listens there or what does is something else (another program, an older system-diff); None when it
    did not answer in time (a server busy reading a large repository is still ours, so not to be replaced)."""
    if _free(port):   # nothing listens there: say so at once (Windows retries a refused connection for seconds,
        return False  # which a short timeout would take for a busy server, and keep a dead one's file for ever)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)   # no proxy: this machine only
    try:
        conn.request("GET", "/whoami", headers={"Accept": "application/json"})
        r = conn.getresponse()
        body = r.read(4096)
        if r.status != 200:
            return False
        info = json.loads(body.decode("utf-8"))
        return bool(ident) and isinstance(info, dict) and info.get("app") == "system-diff" and info.get("id") == ident
    except (ConnectionError, OSError) as exc:
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return None
        return False
    except (ValueError, UnicodeDecodeError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _version_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except ValueError:
        return ()


def older(version: str) -> bool:
    """Whether an engine of `version` ("" for one from before engines said) is older than this system-diff."""
    from . import __version__
    return _version_key(version) < _version_key(__version__)


def _retire(port: int, pid: int, token: str) -> None:
    """Stop an older engine of this repository (it said it is ours, by /whoami): POST /quit, or, for one from before
    /quit, end its process. Never raises."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("POST", "/quit", body=b"{}", headers={"X-Token": token, "Content-Type": "application/json"})
        if conn.getresponse().status == 200:
            conn.close()
            time.sleep(0.3)
            return
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        os.kill(pid, signal.SIGTERM)   # Windows: TerminateProcess
        time.sleep(0.3)
    except Exception:  # noqa: BLE001
        pass


def _look(root: Path) -> tuple:
    """(url, state) of this repository's watcher: state "up" (url set), "busy" (web.json names a server that did not
    answer in time: leave it be), "starting" (one is being started) or "none". A web.json naming a server that is gone,
    or a port that another program now holds, is removed."""
    f = _web_file(root)
    if f.exists():
        try:
            raw = f.read_text(encoding="utf-8")
            info = json.loads(raw)
            port, pid, ident = int(info["port"]), int(info["pid"]), str(info.get("id") or "")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raw, port, pid, ident = None, 0, 0, ""
        ours = _whoami(port, ident) if port and _alive(pid) else False
        if ours and older(str(info.get("version") or "")):
            _retire(port, pid, str(info.get("token") or ""))   # an upgrade: the next look starts this version's
            ours = False
        if ours:
            return f"http://localhost:{port}/", "up"   # what a browser opens here, or through a forwarded port
        if ours is None:
            return None, "busy"
        try:
            if raw is None or f.read_text(encoding="utf-8") == raw:   # not one that has just started since
                f.unlink()
        except OSError:
            pass
    return None, ("starting" if _starting(root) else "none")


def web_url(root: Path) -> Optional[str]:
    """The URL of the page a watcher for this repository serves, if one is running: web.json names it, and the server
    on its port says it is that one (GET /whoami); else None, and a file left by one that is gone, or whose port another
    program now holds, is removed. Cheap enough to ask at every look of the pane: a server started earlier that came up
    since is found."""
    try:
        return _look(root)[0]
    except Exception:  # noqa: BLE001 - not a repository, git missing: no page
        return None


def _starting(root: Path) -> Optional[dict]:
    """The start under way, if any: web.starting written by start_detached less than STARTING_FOR ago, by a process
    that is still there (where that can be told). A stale one is removed."""
    f = git_dir(root) / STARTING
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
        if time.time() - float(info["at"]) < STARTING_FOR and _alive(int(info["pid"])):
            return info
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        f.unlink()
    except OSError:
        pass
    return None


def _free(port: int) -> bool:
    """Whether the server could listen on `port`: as it binds, a port whose last connections are still closing
    (TIME_WAIT, after a browser or a tunnel used it) counts as free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start_detached(root: Path, port: int = 8765, wait: float = 3.0) -> Optional[str]:
    """The URL of this repository's watcher: the running one's, else one started in the background, on the first free
    port from `port` (ten tried), that outlives the caller. None if it could not be started, or if it is not up yet
    after `wait` seconds: it writes web.json as soon as it listens, before reading the system, and web_url (the pane
    asks at every look) finds it then. Only one is started at a time: while one starts (web.starting) or a running one
    is busy, none is. Never raises."""
    try:
        url, state = _look(root)
        if url or state in ("busy", "starting"):
            return url
        free = next((p for p in range(port, port + 10) if _free(p)), None)
        if free is None:
            return None
        marker = git_dir(root) / STARTING
        try:   # claimed at once, so two callers at the same moment start one server, not two
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return None
        kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                              "cwd": str(root), "close_fds": True}
        if os.name == "nt":
            # a hidden console, not none: what it runs (git) inherits it, instead of opening a window each time
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        try:
            child = subprocess.Popen([sys.executable, "-m", "system_diff", "--repo", str(root), "watch",
                                      "--port", str(free)], **kw)
            os.write(fd, json.dumps({"pid": child.pid, "port": free, "at": time.time()}).encode())
        except Exception:
            os.close(fd)
            marker.unlink()
            raise
        os.close(fd)
        deadline = time.time() + wait
        while time.time() < deadline:
            url = web_url(root)
            if url:
                return url
            if child.poll() is not None:   # it could not start (the port taken meanwhile): another may try
                try:
                    marker.unlink()
                except OSError:
                    pass
                return None
            time.sleep(0.1)
        return None
    except Exception:  # noqa: BLE001 - a convenience: never stop the caller
        return None


def _write_web(root: Path, url: str, port: int, ident: str = "", token: str = "") -> Optional[Path]:
    """web.json: where this server listens, who it is (id, for /whoami) and its token (for /page and /touch: the
    pane and the hooks read it here). Readable by its owner only, where the file system can say so."""
    try:
        f = _web_file(root)
        tmp = f.with_suffix(f".{os.getpid()}.tmp")
        from . import __version__
        data = json.dumps({"url": url, "port": port, "pid": os.getpid(), "id": ident, "token": token,
                           "version": __version__})
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(data)
        os.replace(tmp, f)
    except (OSError, subprocess.CalledProcessError):
        return None
    try:   # started: the start under way is done (the one start_detached made for this port)
        marker = f.parent / STARTING
        if int(json.loads(marker.read_text(encoding="utf-8")).get("port", -1)) == port:
            marker.unlink()
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return f


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
    .git/system-diff/web.json says where (web_url reads it): written as soon as it listens, before the first read of
    the system, which in a large repository takes long (the page shows "Reading the code…" meanwhile)."""
    w = Watcher(root, base, config_path, interval)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(w))
    bound = server.server_address[1]
    url = f"http://127.0.0.1:{bound}/"
    web = _write_web(root, url, bound, w.ident, w.token)
    if config_path is None:   # the engine reads the repository's own config, as the views do
        try:
            w.engine = live.Engine(root, interval=min(interval, live.INTERVAL)).start()
            w.pages = live.Pages(w.engine, lambda key: _build(w, key)).start()
        except Exception:  # noqa: BLE001 - no engine: every look reads for itself, as before
            w.engine, w.pages = None, None
    thread = threading.Thread(target=w.loop, daemon=True)
    thread.start()
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
        if w.engine is not None:
            w.engine.stop()
        server.server_close()
        _forget_web(web, bound)
