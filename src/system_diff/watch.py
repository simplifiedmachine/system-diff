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

Several projects (hub.py): each engine writes itself to this user's registry while it runs (no token there); GET
/projects lists the running ones (token or same origin) and the page shows them as a row of project tabs, plain links
to each engine's own page; GET /projects/page lists them all, at the first engine from port 8765 (`system-diff hub`).
"""
from __future__ import annotations

import html
import http.client
import json
import os
import queue
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

from . import activity, chat, extensions, graph, hub, learn, live, pictures
from .compare import Comparison, checkpoint, checkpoint_sha, compare_worktree, turn_sha, worktree_tree
from .diff import diff
from .extract import Code
from .sources import LIVE_SOURCES, GitSource, git, git_dir, is_live

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
.withnav { display:grid; grid-template-columns:1fr; max-width:1520px; margin:0 auto; }
.withnav.on { grid-template-columns:minmax(180px, 240px) minmax(0, 1fr); }
.withnav.on main { max-width:none; margin:0; }
/* the page's column is as wide as its place, whatever it holds: a grid item with auto margins is sized by its content,
   so the hover details (or an update) widened and narrowed it, and the diagram, as wide as the column, with it */
.withnav > main { width:100%; }
#leftnav { position:sticky; top:8px; align-self:start; max-height:calc(100vh - 16px); overflow:auto; padding:18px 0 18px 16px;
  display:grid; gap:14px; }
#leftnav h3 { margin:0 0 4px; font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
#leftnav ul { list-style:none; margin:0; padding:0; display:grid; gap:2px; }
#leftnav button { width:100%; text-align:left; display:grid; gap:1px; padding:6px 8px; border-radius:8px; border:1px solid transparent;
  background:transparent; color:inherit; font:inherit; cursor:pointer; }
#leftnav button:hover, #leftnav button:focus-visible { background:var(--card); border-color:var(--line); }
#leftnav button[aria-current="true"] { background:var(--card); border-color:var(--accent); }
#leftnav .t { font-weight:600; font-size:13px; overflow-wrap:anywhere; } #leftnav .s { font-size:12px; color:var(--muted); }
#leftnav .badge { font-size:11px; margin-left:6px; padding:0 6px; border-radius:9px; background:var(--line); color:inherit; font-weight:500; }
#leftnav .dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--accent); margin-left:6px; vertical-align:middle; }
#leftnav button.switch { color:var(--muted); font-size:12px; border:1px dashed var(--line); margin-top:4px; }
#leftnav details summary { cursor:pointer; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }
#leftnav .tree { color:var(--muted); font-family:var(--mono, monospace); margin-right:2px; }
@media (max-width: 760px) {
  .withnav.on { grid-template-columns:1fr; }
  #leftnav { position:static; max-height:none; padding:10px 16px 0; }
  #leftnav ul { display:flex; flex-wrap:wrap; gap:6px; }
  #leftnav button { width:auto; border-color:var(--line); padding:4px 10px; }
  #leftnav .s { display:none; }
}
pre.lines { white-space:pre-wrap; overflow-wrap:anywhere; }
[hidden] { display:none !important; }
.headline, .card > h2 { overflow-wrap:anywhere; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; box-shadow:var(--shadow); }
.card > h2 { font-size:13px; margin:0 0 10px; color:var(--muted); font-weight:600; letter-spacing:.04em; text-transform:uppercase;
             display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }
.card > h2 .aside { text-transform:none; letter-spacing:0; font-weight:400; }
.headline { font-size:18px; font-weight:600; margin:0; }
.extprog { display:flex; align-items:center; gap:10px; margin-top:4px; color:var(--muted); font-size:13px; }
.extprog i { display:block; width:160px; height:6px; border-radius:3px; background:var(--line); overflow:hidden; }
.extprog i[hidden] { display:none; } .extprog b { display:block; height:100%; width:0; background:var(--accent, #3b6fd8); transition:width .4s; }
.filters { display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }
.filters button { border-radius:999px; padding:2px 10px; font-size:12.5px; }
.filters button[aria-pressed="true"] { background:var(--ink); color:var(--bg); border-color:var(--ink); }
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
#hovercard { position:fixed; z-index:40; max-width:min(360px, calc(100vw - 16px)); padding:10px 12px; border-radius:10px;
             background:var(--card); color:var(--ink); border:1px solid var(--line); font-size:13px; line-height:1.45;
             box-shadow:0 6px 24px rgba(0,0,0,.18); pointer-events:none; overflow-wrap:anywhere; }
#hovercard .t { font-weight:650; margin-bottom:4px; }
#hovercard .l { color:var(--ink2); }
#hovercard .l + .l { margin-top:3px; }
#hovercard .h { color:var(--ink); margin-bottom:4px; }
#hovercard ul { margin:4px 0 0; padding-left:16px; color:var(--ink2); }
#hovercard li + li { margin-top:2px; }
#hovercard .n { color:var(--muted); margin-top:4px; }
.llm { color:var(--work); font-size:.9em; margin-left:3px; }
.lit-ui { box-shadow:0 0 0 2px var(--work) !important; }
.dia { padding:0; overflow:hidden; }
.dia > h2 { padding:14px 16px 0; }
/* the diagram fits its box: as wide as the box (a tiny one not blown up), never taller than the window */
#dia { background:var(--paper); border-top:1px solid var(--line); border-bottom:1px solid var(--line); padding:10px; overflow:hidden; }
#dia svg { display:block; width:100%; height:auto; max-width:100%; max-height:calc(100vh - 200px); margin:0 auto; }
#dia.empty { color:var(--muted); font-size:13px; padding:24px; text-align:center; }
#dia.enterable [data-id].can-enter { cursor:zoom-in; }
/* one height whatever is hovered: the page never moves (and on Windows its scrollbar never comes and goes,
   which would narrow the page and rescale the diagram) */
#detail { padding:12px 16px; height:12.5em; overflow:auto; white-space:pre-wrap; color:var(--ink2); font-size:13.5px; }
html { scrollbar-gutter:stable; }
#detail .what { color:var(--muted); }
#detail b { color:var(--ink); }
#detail .ex { white-space:normal; display:grid; gap:10px; max-width:860px; }
#detail .ex h3 { margin:0; font-size:15px; color:var(--ink); }
#detail .ex h4 { margin:0 0 3px; font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted);
                 font-weight:650; }
#detail .ex p { margin:0; }
#detail .ex ul { margin:0; padding-left:18px; }
#detail .ex li + li { margin-top:3px; }
#detail .ex .facts { display:grid; gap:4px; }
#detail .ex .fact b { font-weight:600; }
#detail .ex details { margin:2px 0 0 0; }
#detail .ex summary { cursor:pointer; color:var(--work); font-size:12.5px; }
#detail .ex details ul { margin-top:4px; max-height:240px; overflow:auto; }
#code { border-top:1px solid var(--line); padding:10px 16px 14px; }
#code .head { display:flex; gap:10px; align-items:baseline; margin-bottom:6px; }
#code .head b { font:600 13px var(--mono); color:var(--ink); }
#code .head .sub { flex:1; }
#code pre { margin:0; max-height:520px; overflow:auto; background:var(--bg); border:1px solid var(--line);
            border-radius:8px; padding:8px 0; font:12.5px/1.5 var(--mono); counter-reset:ln; }
#code pre span { display:block; padding:0 12px 0 0; white-space:pre; }
#code pre span::before { counter-increment:ln; content:counter(ln); display:inline-block; width:3.5em; padding-right:1em;
                         text-align:right; color:var(--muted); user-select:none; }
#dia.enterable [data-id].can-read { cursor:pointer; }
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
.swim .kind { color:var(--muted); font-size:11px; font-weight:500; margin-left:6px; }
.swim .counts { color:var(--muted); font-size:12px; }
.swim { cursor:pointer; }
.swim-recent { margin:0 10px 8px; padding:6px 10px; max-height:220px; overflow:auto; list-style:none; font-size:12.5px;
  background:var(--bg); border-radius:6px; }
.swim-recent li { display:flex; gap:8px; color:var(--muted); }
.swim-recent li b { color:var(--fg); font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.swim .track { position:relative; height:34px; background:var(--bg); border-radius:6px; overflow:hidden; }
.swim .done { position:absolute; left:0; top:0; bottom:0; background:var(--lane); opacity:.14; }
.swim .mark { position:absolute; top:50%; transform:translate(-50%, -50%); font-size:15px; line-height:1; color:var(--lane); }
.swim .badges { position:absolute; right:6px; top:50%; transform:translateY(-50%); display:flex; gap:6px; font-size:12px; }
.badge { padding:1px 7px; border-radius:999px; background:var(--card); border:1px solid var(--line); color:var(--muted); }
.badge.unread { color:var(--warn); border-color:var(--warn-line); background:var(--warn-bg); }
.wfsum.warn { color:var(--warn); }
button.wfask { margin:6px 0 2px; }
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
.hist { list-style:none; margin:0; padding:0; display:grid; gap:8px; }
.hist li { display:grid; gap:2px; padding:9px 12px; border-radius:8px; border:1px solid var(--line); background:var(--card);
           cursor:pointer; }
.hist li:hover, .hist li:focus-visible { border-color:var(--work); }
.hist .when { color:var(--muted); font-size:12.5px; }
.hist .asked { color:var(--ink); font-weight:600; overflow-wrap:anywhere; }
.hist .asked.plain { font-weight:400; color:var(--ink2); }
.hist .got { color:var(--warn); overflow-wrap:anywhere; }
.hist .got b { color:var(--muted); font-weight:400; }
.hist .chg { color:var(--muted); font-size:12.5px; }
.hist .got .llm { color:var(--muted); margin-left:4px; cursor:help; }
.hist li.marked { border-color:var(--work); box-shadow:inset 3px 0 0 var(--work); }
.hist .row { display:flex; gap:8px; align-items:baseline; }
.hist .row .when { flex:1; }
.hist .pickc { display:inline-flex; gap:4px; align-items:center; color:var(--muted); font-size:12.5px; cursor:pointer; }
.hist .pickc input { margin:0; cursor:pointer; }
.hist .withnow { font:inherit; font-size:12px; padding:1px 8px; border-radius:999px; border:1px solid var(--line);
                 background:var(--bg); color:var(--muted); cursor:pointer; }
.hist .withnow:hover, .hist .withnow:focus-visible { color:var(--ink); border-color:var(--work); }
.cmpbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; color:var(--muted); font-size:13px; margin:-2px 0 10px; }
.cmpbar button { font:inherit; font-size:12.5px; }
.cmp { display:grid; grid-template-columns:auto 1fr; gap:6px 12px; align-items:baseline; }
.cmp .k { color:var(--muted); font-size:12.5px; text-transform:uppercase; letter-spacing:.04em; }
.cmp .v { overflow-wrap:anywhere; }
.cmp .v .when { color:var(--muted); }
.cmp .v .said { display:block; color:var(--muted); font-size:13px; }
.cmp .sum { grid-column:1 / -1; color:var(--warn); font-weight:600; margin-top:4px; overflow-wrap:anywhere; }
.cmp .note { grid-column:1 / -1; color:var(--muted); font-size:13px; }
.cmp .acts { grid-column:1 / -1; display:flex; gap:8px; flex-wrap:wrap; margin-top:4px; }
body { padding-bottom:var(--sendh, 76px); }   /* room under the page for the send box pinned over it */
form.send { position:fixed; left:0; right:0; bottom:0; z-index:30; display:flex; gap:10px; align-items:flex-end; padding:10px 20px;
            background:var(--card); border-top:1px solid var(--line); box-shadow:0 -4px 16px rgba(0,0,0,.06); }
form.send label { font-weight:600; font-size:13px; align-self:center; white-space:nowrap; }
#say { flex:1; font:inherit; font-size:13.5px; min-height:2.6em; max-height:30vh; resize:vertical; padding:7px 10px;
       border-radius:8px; border:1px solid var(--line); background:var(--bg); color:var(--ink); }
#sendnote { color:var(--muted); font-size:12.5px; max-width:30vw; }
#sendnote.bad { color:var(--attn); }
.hint { color:var(--muted); font-size:12px; text-align:center; padding:0 0 10px; }
/* the team's workflow: a column per stage, a card per agent or extension step, arrows for who waits for whom */
.wfwrap { position:relative; overflow-x:auto; padding:4px 2px 8px; }
.wf { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(210px, 1fr); gap:44px; position:relative; z-index:1; }
.wfcol { display:grid; align-content:start; gap:10px; }
.wfhead { display:flex; gap:8px; align-items:baseline; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
.wfhead b { color:var(--ink2); }
.wfnode { text-align:left; display:grid; gap:3px; padding:9px 11px; border-radius:12px; border:1px solid var(--line);
  background:var(--card); color:inherit; font:inherit; cursor:pointer; box-shadow:0 1px 2px rgba(0,0,0,.04);
  transition:border-color .15s ease, box-shadow .15s ease, transform .15s ease; }
.wfnode:hover, .wfnode:focus-visible { border-color:var(--work); box-shadow:0 4px 14px rgba(0,0,0,.08); transform:translateY(-1px); }
.wfnode .nm { font-weight:600; display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.wfnode .by { font-size:10.5px; font-weight:500; padding:0 6px; border-radius:9px; background:var(--line); color:var(--muted); }
.wfnode .st { font-size:12px; display:flex; gap:6px; align-items:center; }
.wfnode .st::before { content:""; width:8px; height:8px; border-radius:50%; background:var(--muted); flex:none; }
.wfnode.st-running { border-color:var(--work); } .wfnode.st-running .st::before { background:var(--work); animation:pulse 1.2s ease-in-out infinite; }
.wfnode.st-done .st::before { background:#2f9e5b; } .wfnode.st-done { opacity:.85; }
.wfnode.st-waiting .st::before { background:transparent; border:2px dashed var(--muted); width:6px; height:6px; }
.wfnode.st-failed .st::before { background:#d1453b; } .wfnode.st-paused .st::before { background:#c98a00; }
.wfnode .meta { font-size:11.5px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
@keyframes pulse { 50% { opacity:.35; } }
svg.wfedges { position:absolute; inset:0; z-index:0; pointer-events:none; overflow:visible; }
svg.wfedges path { fill:none; stroke:var(--line); stroke-width:2; }
svg.wfedges path.live { stroke:var(--work); stroke-dasharray:5 4; }
svg.wfedges path.done { stroke:#2f9e5b; opacity:.6; }
.wfsaved { margin-top:12px; } .wfsaved summary { cursor:pointer; color:var(--muted); font-size:13px; }
.wfsaved li { margin:4px 0; } .wfsaved .s { color:var(--muted); font-size:12.5px; }
dialog#agentlog { width:min(920px, 94vw); height:min(82vh, 900px); padding:0; border:1px solid var(--line); border-radius:16px;
  background:var(--card); color:var(--ink); box-shadow:0 20px 60px rgba(0,0,0,.25); }
dialog#agentlog[open] { display:grid; grid-template-rows:auto auto auto 1fr; }
dialog#agentlog::backdrop { background:rgba(10,14,20,.35); backdrop-filter:blur(2px); }
dialog#agentlog header { display:flex; gap:12px; align-items:flex-start; justify-content:space-between; padding:14px 18px 8px; border-bottom:1px solid var(--line); }
dialog#agentlog h2 { margin:0; font-size:17px; } .alogmeta { color:var(--muted); font-size:12.5px; margin-top:3px; }
.alogacts { display:flex; gap:8px; flex-wrap:wrap; padding:8px 18px 0; } .alogacts:empty { display:none; }
.wfnode .meta.note { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%; }
.wfnode .meta.resumed { color:var(--work); }
.alog.note div { margin:2px 0; }
.alogtabs { display:flex; gap:4px; padding:8px 18px 0; border-bottom:1px solid var(--line); }
.alogtabs button { border:0; background:transparent; padding:6px 10px; border-bottom:2px solid transparent; color:var(--muted); font:inherit; cursor:pointer; }
.alogtabs button[aria-selected="true"] { color:var(--ink); border-bottom-color:var(--work); font-weight:600; }
.alogbody { overflow:auto; padding:12px 18px 18px; display:grid; align-content:start; gap:8px; }
.alog { display:grid; gap:2px; } .alog .when { font-size:11px; color:var(--muted); }
.alog.text p { margin:0; white-space:pre-wrap; } .alog.tool .t { font:12.5px var(--mono, monospace); }
.alog pre { margin:0; font:12px/1.45 var(--mono, monospace); background:var(--paper); border-radius:8px; padding:7px 9px; white-space:pre-wrap; max-height:240px; overflow:auto; }
.alog.result.err pre { border-left:3px solid #d1453b; }
html.in-studio, html.in-studio body { background:transparent; }
/* inside the Studio its session column is the conversation: this page's own chat and Send box step aside */
html.in-studio form.send, html.in-studio #chatbtn, html.in-studio aside.chat { display:none !important; }
html.in-studio body.chat-open { padding-right:0 !important; }
html.in-studio header.top { background:color-mix(in srgb, var(--card) 62%, transparent); -webkit-backdrop-filter:saturate(180%) blur(20px); backdrop-filter:saturate(180%) blur(20px); }
#shown { position:sticky; top:0; z-index:30; display:flex; flex-wrap:wrap; gap:8px 12px; align-items:center; padding:8px 16px;
  background:var(--card); border-bottom:2px solid var(--accent); font-size:14px; }
#shown .why { flex:1 1 280px; min-width:0; overflow-wrap:anywhere; } #shown label { font-size:12px; color:var(--muted); }
#shown button { font:inherit; padding:3px 10px; border-radius:7px; border:1px solid var(--line); background:var(--bg); color:inherit; cursor:pointer; }
nav.projects { display:flex; gap:2px; align-items:flex-end; padding:6px 20px 0; background:var(--bg);
               border-bottom:1px solid var(--line); overflow-x:auto; scrollbar-width:thin; }
nav.projects a { display:flex; gap:7px; align-items:baseline; padding:5px 12px 6px; border:1px solid transparent;
                 border-bottom:0; border-radius:8px 8px 0 0; color:var(--muted); text-decoration:none; white-space:nowrap;
                 font-size:13px; max-width:280px; flex:0 0 auto; }
nav.projects a:hover { color:var(--ink); background:var(--card); }
nav.projects a[aria-current="page"] { background:var(--card); border-color:var(--line); color:var(--ink); font-weight:600;
                                      margin-bottom:-1px; padding-bottom:7px; }
nav.projects .nm { overflow:hidden; text-overflow:ellipsis; }
nav.projects .br { font:11.5px var(--mono); color:var(--muted); font-weight:400; overflow:hidden; text-overflow:ellipsis; }
nav.projects .dot { width:7px; height:7px; border-radius:50%; background:var(--c3); align-self:center; flex:none; }
nav.projects a.all { margin-left:auto; font-size:12.5px; padding:5px 4px 6px 12px; }
/* the conversation with Claude: pinned on the right (a bottom sheet on narrow screens) */
#chatbtn[aria-pressed="true"] { border-color:var(--work); color:var(--work); background:var(--work-bg); }
#chatbtn .count { font-size:11px; margin-left:4px; color:var(--muted); }
#chatbtn.llm-down::after { content:""; display:inline-block; width:7px; height:7px; border-radius:50%; background:#d1453b; margin-left:6px; vertical-align:1px; }
aside.chat li.llmerr { border-left:3px solid #d1453b; padding:8px 10px; margin:6px 10px; border-radius:8px; background:rgba(209,69,59,.07); list-style:none; overflow-wrap:anywhere; }
aside.chat li.llmerr .meta { color:var(--muted); font-size:12px; margin-top:3px; }
aside.chat { position:fixed; z-index:25; right:0; top:0; bottom:var(--sendh, 76px); width:min(400px, 34vw); display:flex;
             flex-direction:column; background:var(--card); border-left:1px solid var(--line); box-shadow:-4px 0 16px rgba(0,0,0,.05); }
body.chat-open { padding-right:min(400px, 34vw); }
aside.chat > header { display:flex; align-items:center; gap:8px; padding:10px 14px; border-bottom:1px solid var(--line); }
aside.chat > header h2 { font-size:13px; margin:0; flex:1; color:var(--muted); font-weight:600; letter-spacing:.04em;
                         text-transform:uppercase; }
aside.chat > header button { padding:2px 9px; }
#pointing { margin:8px 12px 0; padding:7px 10px; border-radius:8px; background:var(--work-bg); border:1px solid var(--work);
            font-size:12.5px; display:flex; gap:6px; align-items:flex-start; }
#pointing .what { flex:1; overflow-wrap:anywhere; }
#pointing .x, .note-x { border:0; background:transparent; padding:0 4px; color:var(--muted); font-size:14px; line-height:1; }
#chatlist { list-style:none; margin:0; padding:10px 12px 16px; overflow:auto; flex:1; display:grid; gap:12px; align-content:start; }
#chatlist .empty-note { padding:4px 2px; }
.q { display:grid; gap:5px; }
.q .ask { justify-self:end; max-width:92%; background:var(--work-bg); border-radius:10px 10px 2px 10px; padding:7px 10px;
          overflow-wrap:anywhere; white-space:pre-wrap; }
.q .meta { justify-self:end; color:var(--muted); font-size:11.5px; display:flex; gap:6px; align-items:center; flex-wrap:wrap; }
.q .about-tag { font-size:11.5px; color:var(--muted); justify-self:end; }
.q .st { display:inline-flex; gap:5px; align-items:center; }
.q .st::before { content:""; width:7px; height:7px; border-radius:50%; background:var(--muted); }
.q.st-answering .st::before { background:var(--work); animation:pulse 1.6s ease-in-out infinite; }
.q.st-delivered .st::before, .q.st-queued .st::before { background:var(--warn); }
.q.st-answered .st::before { background:var(--ok); }
.q .ans { border:1px solid var(--line); border-radius:10px 10px 10px 2px; padding:8px 11px; font-size:13.5px; overflow-wrap:anywhere; }
.md p, .md ul, .md ol, .md pre, .md h4 { margin:0 0 7px; } .md > :last-child { margin-bottom:0; }
.md ul, .md ol { padding-left:20px; } .md li + li { margin-top:2px; }
.md h4 { font-size:13.5px; }
.md code { font:12px var(--mono); background:var(--bg); border:1px solid var(--line); border-radius:4px; padding:0 3px; }
.md pre { background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:7px 9px; overflow:auto;
          font:12px/1.45 var(--mono); white-space:pre; }
.md pre code { border:0; padding:0; background:none; }
a.ref { color:var(--work); text-decoration:underline dotted; text-underline-offset:2px; cursor:pointer; }
a.ref:hover { text-decoration-style:solid; }
#dia [data-id].flash { animation:flash 1.2s ease-in-out 2; }
@keyframes flash { 50% { opacity:.35; } }
.found { margin-top:6px; border-top:1px dashed var(--line); padding-top:5px; }
.found .fh { font-size:11px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); font-weight:650; }
.found .fn { display:flex; gap:4px; align-items:flex-start; color:var(--ink2); }
.found .fn .q { color:var(--muted); font-size:12px; }
form.send .about { display:flex; gap:4px; align-items:center; font-size:12px; color:var(--work); background:var(--work-bg);
                   border:1px solid var(--work); border-radius:999px; padding:1px 4px 1px 10px; max-width:28vw; align-self:center; }
form.send .about span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
form.send .about button { border:0; background:transparent; padding:0 5px; color:var(--work); }
/* the code viewer: the outline beside the code (above it when narrow), a card over a chunk with what it does */
#code .cv { display:grid; grid-template-columns:minmax(200px, 280px) 1fr; gap:12px; align-items:start; }
#code nav.outline { max-height:520px; overflow:auto; font-size:12.5px; border:1px solid var(--line); border-radius:8px;
                    padding:8px; background:var(--bg); }
#code .inshort { color:var(--ink2); margin-bottom:8px; }
#code .inshort .wait { color:var(--muted); }
#code nav.outline ol { list-style:none; margin:0; padding:0; display:grid; gap:2px; }
#code nav.outline li { padding:3px 6px; border-radius:6px; cursor:pointer; }
#code nav.outline li:hover, #code nav.outline li:focus-visible { background:var(--card); }
#code nav.outline li.here { background:var(--work-bg); box-shadow:inset 2px 0 0 var(--work); }
#code nav.outline .nm { font:600 12px var(--mono); color:var(--ink); overflow-wrap:anywhere; }
#code nav.outline .k { color:var(--muted); font-size:11px; margin-left:4px; }
#code nav.outline .said { color:var(--muted); display:block; }
#code pre span.hl { background:var(--work-bg); }
#codecard { position:fixed; z-index:41; max-width:min(420px, calc(100vw - 16px)); padding:10px 12px; border-radius:10px;
            background:var(--card); border:1px solid var(--line); box-shadow:0 6px 24px rgba(0,0,0,.18); font-size:13px;
            line-height:1.45; pointer-events:none; overflow-wrap:anywhere; }
#codecard .t { font:600 12.5px var(--mono); margin-bottom:4px; }
#codecard .l + .l { margin-top:4px; } #codecard .l b { color:var(--muted); font-weight:600; }
.spin { display:inline-block; width:10px; height:10px; border:2px solid var(--line); border-top-color:var(--work);
        border-radius:50%; animation:spin .8s linear infinite; vertical-align:-1px; margin-right:6px; }
@keyframes spin { to { transform:rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin, .q.st-answering .st::before, #dia [data-id].flash { animation:none; } }
@media (max-width:1000px) {
  aside.chat { left:0; width:auto; top:auto; height:min(48vh, 460px); border-left:0; border-top:1px solid var(--line);
               border-radius:14px 14px 0 0; box-shadow:0 -6px 20px rgba(0,0,0,.10); }
  body.chat-open { padding-right:0; padding-bottom:calc(var(--sendh, 76px) + min(48vh, 460px)); }
  #code .cv { grid-template-columns:1fr; } #code nav.outline { max-height:200px; }
}
@media (max-width:720px) {
  header.top, main, form.send, nav.projects { padding-left:16px; padding-right:16px; }
  form.send .about { max-width:40vw; }
  .swim { grid-template-columns:1fr; }
  .timeline li { grid-template-columns:56px 20px 1fr; } .timeline li::before { left:75px; }
  form.send label, #sendnote { display:none; }
}
</style></head><body>
<nav class="projects" id="projects" aria-label="Projects" hidden></nav>
<header class="top">
  <h1>System diff</h1>
  <div class="tabs" role="tablist" id="tabs">
    <button role="tab" data-view="now">Now<kbd>n</kbd></button>
    <button role="tab" data-view="review">Review<kbd>r</kbd></button>
    <button role="tab" data-view="agents">Agents<kbd>a</kbd></button>
    <button role="tab" data-view="topics">Topics<kbd>t</kbd></button>
    <button role="tab" data-view="history">History<kbd>h</kbd></button>
    __EXT_TABS__
  </div>
  <button id="follow" type="button" title="Go to what Claude is working on (f)">Follow Claude</button>
  <button id="back" type="button" title="Back (b)">Back</button>
  <div class="zoom" title="How much of the system the diagram shows">
    <button id="in" type="button" aria-label="Zoom in">+</button><span id="zoomlabel">auto</span>
    <button id="out" type="button" aria-label="Zoom out">−</button></div>
  <span class="spacer"></span>
  <button id="chatbtn" type="button" aria-pressed="false" aria-controls="chat" title="Show or hide the conversation with Claude (c)">Conversation<span class="count" id="chatcount"></span></button>
  <button id="theme" type="button" title="Light, dark, or as your system is">◐ auto</button>
</header>
<div id="shown" role="status" hidden></div>
<div class="withnav">
<nav id="leftnav" aria-label="Repositories and lists" hidden></nav>
<main>
  <div><p class="headline" id="headline">Reading the code…</p><div class="extprog" id="extprog" hidden><span></span><i><b></b></i></div><div class="filters" id="filters" hidden></div><div class="error" id="error"></div></div>
  <section class="card" id="status" hidden></section>
  <div id="before" style="display:grid;gap:16px"></div>
  <section class="card dia" id="diacard" hidden><h2 id="diatitle"></h2>
    <div class="diawrap" id="diawrap"><div id="dia" role="img"></div><div id="detail" aria-live="polite"></div><div id="code" hidden></div></div></section>
  <div id="after" style="display:grid;gap:16px"></div>
</main>
</div>
<dialog id="agentlog" aria-labelledby="alogname">
  <header><div><h2 id="alogname"></h2><div class="alogmeta" id="alogmeta"></div></div>
    <form method="dialog"><button type="submit" aria-label="Close">Close</button></form></header>
  <div class="alogacts" id="alogacts"></div>
  <div class="alogtabs" role="tablist" id="alogtabs"></div>
  <div class="alogbody" id="alogbody"></div>
</dialog>
<div class="hint"><kbd>n</kbd> <kbd>r</kbd> <kbd>a</kbd> <kbd>t</kbd> <kbd>h</kbd> views · <kbd>h</kbd> on a box: its history · tick two changes in History to compare them · <kbd>f</kbd> follow Claude · <kbd>c</kbd> conversation · <kbd>[</kbd> <kbd>]</kbd> projects · <kbd>b</kbd> back · <kbd>+</kbd> <kbd>−</kbd> zoom · hover the diagram to see how parts connect, click to zoom in</div>
<aside class="chat" id="chat" aria-label="Conversation with Claude" hidden>
  <header><h2>Conversation with Claude</h2><button id="chatclose" type="button" aria-label="Hide the conversation">Hide</button></header>
  <div id="pointing" hidden aria-live="polite"></div>
  <ol id="chatlist" aria-live="polite"></ol>
</aside>
<form class="send" id="send"><label for="say">Send to Claude</label>
  <div class="about" id="about" hidden><span id="abouttext"></span><button type="button" id="aboutx" aria-label="Do not send what you are looking at">×</button></div>
  <textarea id="say" placeholder="Ask or tell Claude something about the system (Enter sends, Shift+Enter for a new line)"></textarea>
  <span id="sendnote" aria-live="polite"></span><button id="sendbtn" class="primary" type="submit">Send</button></form>
<script>
const TOKEN = "__TOKEN__";
// inside the Studio (system-diff app): told once, as the page loads (its address changes as you move around)
const IN_STUDIO = window.parent !== window && /(^|[?&])studio=1(&|$)/.test(location.search);
const $ = id => document.getElementById(id);
const VIEWS = ["now", "review", "agents", "topics", "history"].concat(__EXT_VIEWS__);
const EXT_VIEWS = __EXT_VIEWS__;   // the extensions' own views
const EXT_KEYS = __EXT_KEYS__;   // an extension's tab keys: {key: view}
const HOME = __HOME__;           // the view this repository opens on, when an extension says so (research: its own)
const SYSTEM_VIEWS = ["now", "review", "agents", "topics", "history"];
const SETS = __SETS__;           // where an extension is home: its tabs, a set of their own, [view, label, key, stands for]
const OPEN = __OPEN__;           // a page started by hand opens here (system-diff watch: Now; --view research: research's)
const PKEY = "__PKEY__";         // this project, for what is remembered per project
const SYSKEY = { now:"n", review:"r", agents:"a", topics:"t", history:"h" };
const SETMODE = !!(HOME && HOME.system === "collapsed" && SETS[HOME.set]);
function setOf(view) {
  if (SYSTEM_VIEWS.includes(view)) return "system";
  for (const [n, st] of Object.entries(SETS)) if (st.views.some(v => v[0] === view)) return n;
  return "";
}
function counterpart(view, to) {   // the place in the other set: research's Now for Now, and back
  if (to === "system") { const v = (SETS[setOf(view)] || { views:[] }).views.find(v => v[0] === view); return (v && v[3]) || "now"; }
  const v = SETS[to].views.find(v => v[3] === view); return v ? v[0] : HOME.view;
}
function rememberSet(n) { try { localStorage.setItem("sd-set:" + PKEY, n); } catch (e) {} }
function rememberedSet() { try { return localStorage.getItem("sd-set:" + PKEY) || ""; } catch (e) { return ""; } }
function histView() { return SYSTEM_VIEWS.includes(ui.view) ? "history" : ui.view; }
function keymap() {   // the keys of the row shown: an extension's tab takes the key of the tab it stands for
  if (SETMODE && setOf(ui.view) === HOME.set) {
    const m = {};
    for (const v of SETS[HOME.set].views) { if (v[2]) m[v[2]] = v[0]; if (v[3]) m[SYSKEY[v[3]]] = v[0]; }
    return m;
  }
  const m = { n:"now", r:"review", a:"agents", t:"topics", h:"history" };
  return SETMODE ? m : { ...m, ...EXT_KEYS };
}
const ui = { view:"now", part:"", scope:"", homed:false, zoom:-1, history:[], data:null, last:{}, pick:"", mine:false, hover:null, seq:0, cmp:[] };
const projects = { list:[] };
// the conversation with Claude: the panel, what Claude points at, what the person is looking at (sent with a question)
const talk = { open:false, data:null, stamp:null, point:[], pointAt:0, dismissed:0, aboutOff:"", file:"", seen:0 };   // every running project of this user (GET /projects), this one "current"
(function fromHash() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (VIEWS.includes(h.get("view"))) ui.view = h.get("view");
  ui.part = h.get("part") || "";
  ui.scope = h.get("scope") || "";
  ui.homed = !!h.get("view");   // a view in the address: it wins
  if (!ui.homed) {   // else the command's (a page started by hand), else the set chosen last here, else the home
    const set = rememberedSet();
    if (OPEN && VIEWS.includes(OPEN)) ui.view = OPEN;
    else if (SETMODE && set === "system") ui.view = "now";
    else if (HOME && VIEWS.includes(HOME.view) && (SETMODE || !set)) ui.view = HOME.view;
  }
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
const openLanes = new Set();   // the lanes whose actions are shown, kept as the page redraws
function lanes(s) {
  const c = card(s.title, s.aside), box = el("div", null, "lanes");
  for (const l of s.items || []) {
    const row = el("div", null, "swim lc" + ((l.colour | 0) % 5)), who = el("div", null, "who");
    row.dataset.blocks = JSON.stringify(l.blocks || []); row.dataset.pid = "ag:" + l.name;
    row.tabIndex = 0;
    const nm = el("span", l.title || l.name, "name"); nm.title = l.title || l.name;
    if (l.type && l.type !== l.title) nm.append(el("span", l.type, "kind"));
    who.append(nm);
    if (l.area && l.area !== l.type) who.append(el("span", l.area, "area"));
    if (l.counts) who.append(el("span", l.counts, "counts"));
    if (l.doing) who.append(el("span", l.doing.replace(/^[◎✎] /, ""), "doing"));
    const track = el("div", null, "track"), p = l.progress;
    if (typeof p === "number" && p >= 0 && p <= 1) { const d = el("div", null, "done"); d.style.width = (p * 100) + "%"; track.append(d); }
    for (const m of l.marks || []) {
      const k = String(m.kind || ""), icon = MARKS[k.toLowerCase()] || (k.length <= 2 ? k : "•");
      const mk = el("span", icon, "mark");
      mk.style.left = (Math.max(0.01, Math.min(0.99, +m.pos || 0)) * 100) + "%";
      mk.title = m.of ? Object.entries(m.of).map(([kk, n]) => n + " " + kk + (n > 1 ? "s" : "")).join(", ") : k;
      track.append(mk);
    }
    const badges = el("div", null, "badges");
    if (l.live) badges.append(el("span", "live", "badge live"));
    if (typeof p === "string" && p) badges.append(el("span", p, "badge"));
    else if (typeof p === "number" && p > 1) badges.append(el("span", String(p), "badge"));
    if (l.unread) {   // messages for an agent that never ran wait for it: they are not unread by a working agent
      const idle = !l.live && !l.counts;
      const u = el("span", "✉ " + l.unread + (idle ? " waiting: agent not running" : " unread"), "badge unread");
      if (idle) u.title = "Messages on the bus for this agent; it was never launched, so nobody has read them";
      badges.append(u);
    }
    track.append(badges);
    row.append(who, track);
    const on = () => { ui.hover = { ids:lightsOf("ag:" + l.name, l.blocks), from:row }; applyPick(); };
    const off = () => { ui.hover = null; applyPick(); };
    row.addEventListener("mouseenter", on); row.addEventListener("focus", on);
    row.addEventListener("mouseleave", off); row.addEventListener("blur", off);
    // a double click: the agent's whole story (brief, messages, its session log), as a click on the workflow board
    row.addEventListener("dblclick", e => { e.preventDefault(); openAgentLog(l.name, l.title); });
    row.title = "Click: what it did · double click: its brief, messages and session log";
    box.append(row);
    if ((l.recent || []).length) {   // every action, on click (newest first)
      const list = el("ul", null, "swim-recent"); list.hidden = !openLanes.has(l.name);
      for (const r of l.recent.slice().reverse()) {
        const li = el("li"); li.append(el("span", new Date(r.at * 1000).toLocaleTimeString()), el("span", r.kind === "edit" ? "edited" : "read"), el("b", r.path));
        list.append(li);
      }
      row.addEventListener("click", () => { list.hidden = !list.hidden; if (list.hidden) openLanes.delete(l.name); else openLanes.add(l.name); });
      row.addEventListener("keydown", e => { if (e.key === "Enter") row.click(); });
      box.append(list);
    }
  }
  if (!(s.items || []).length) c.append(el("div", "No agents in this team.", "empty-note"));
  else {
    c.append(box);
    c.append(el("div", "◎ read · ✎ edit · ▲ changed the system · ✉ message · click a row for everything it did, double click for its brief, messages and log", "legend"));
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
// The History view: each entry what was asked (or the commit) and, in one sentence, what the system gained or lost;
// hovering one lights the boxes it changed, a click shows that change on the diagram. In the list ("pick"), a tick on
// two entries (or one, then Compare with now) shows what changed between them: the part "[<block>#]compare:<A>..<B>".
function blockOf(part) {
  const p = part || "", i = p.indexOf("#");
  return i >= 0 ? p.slice(0, i) : p.startsWith("compare:") ? "" : p;
}
function compareKey(block, a, b) { return (block ? block + "#" : "") + "compare:" + a + (b ? ".." + b : ""); }
function historyList(s) {
  const c = card(s.title, s.aside), ul = el("ul", null, "hist"), block = blockOf(ui.part);
  const picking = !!s.pick;
  if (picking) {
    const marked = (s.items || []).find(h => h.marked);
    if (marked && !ui.cmp.length) ui.cmp = [marked.eid];
    ui.cmp = ui.cmp.filter(id => (s.items || []).some(h => h.eid === id));
    const bar = el("div", null, "cmpbar");
    if (ui.cmp.length) {
      const first = (s.items || []).find(h => h.eid === ui.cmp[0]);
      bar.append(el("span", "◆ from " + (first ? first.when : "") + ": tick another change to compare, or"));
      const now = el("button", "compare with now"); now.type = "button";
      now.onclick = () => go(histView(), compareKey(block, ui.cmp[0], "now"));
      const clear = el("button", "clear"); clear.type = "button";
      clear.onclick = () => { ui.cmp = []; ui.last.before = ""; render(ui.data); };
      bar.append(now, clear);
    } else bar.append(el("span", "Tick two changes to see what changed in the system between them, or use Compare with now."));
    c.append(bar);
  }
  for (const h of s.items || []) {
    const li = el("li"), head = el("div", null, "row");
    li.dataset.hid = h.id; li.tabIndex = 0;
    const ticked = picking && ui.cmp.includes(h.eid);
    if (ticked) li.classList.add("marked");
    head.append(el("span", h.when + (ticked ? " · ◆ from" : ""), "when"));
    if (picking) {
      const lab = el("label", null, "pickc"), box = el("input");
      box.type = "checkbox"; box.checked = ticked; box.setAttribute("aria-label", "compare " + h.when);
      lab.append(box, "compare");
      lab.addEventListener("click", e => e.stopPropagation());
      box.addEventListener("change", () => {
        if (box.checked) ui.cmp = [...ui.cmp.filter(x => x !== h.eid), h.eid]; else ui.cmp = ui.cmp.filter(x => x !== h.eid);
        if (ui.cmp.length >= 2) { const [a, b] = ui.cmp.slice(-2); ui.cmp = []; go(histView(), compareKey(block, a, b)); }
        else { ui.last.before = ""; render(ui.data); }
      });
      const now = el("button", "Compare with now", "withnow"); now.type = "button";
      now.addEventListener("click", e => { e.stopPropagation(); ui.cmp = []; go(histView(), compareKey(block, h.eid, "now")); });
      head.append(lab, now);
    }
    li.append(head, el("div", h.asked, "asked" + (h.request ? "" : " plain")));
    const got = el("div", null, "got"); got.append(el("b", "→ "), h.said || h.got);
    if (h.by) { const m = el("span", "✦", "llm"); m.title = "a summary written by " + h.by + " from the changes (hover for them)"; got.append(m); }
    li.append(got);
    const first = (h.changes || [])[0];
    if (first) li.append(el("div", first.sign + " " + first.text + ((h.changes || []).length > 1 ? " · and " +
                                (h.changes.length - 1) + " more" : ""), "chg"));
    const on = () => { ui.hover = { ids:lightsOf(h.id, h.parts), from:li }; applyPick(); };
    const off = () => { ui.hover = null; applyPick(); };
    li.addEventListener("mouseenter", on); li.addEventListener("focus", on);
    li.addEventListener("mouseleave", off); li.addEventListener("blur", off);
    const open = () => go(histView(), block + "#" + (h.eid || h.id.slice(3)));
    li.addEventListener("click", open);
    li.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); open(); } });
    ul.append(li);
  }
  if (!(s.items || []).length) c.append(el("div", "Nothing recorded yet.", "empty-note")); else c.append(ul);
  return c;
}
// Two points of the history compared: from, to, the sentence, and the way back.
function compareHead(s) {
  const c = card(s.title, s.aside), h = (s.items || [])[0] || {}, box = el("div", null, "cmp");
  const side = (k, p) => {
    const v = el("div", null, "v");
    if (!p || p.now) v.append(el("b", "now"), el("span", " · the system as it is", "when"));
    else { v.append(el("span", "just after " + p.when + " · ", "when"), p.asked); if (p.said) v.append(el("span", "→ " + p.said, "said")); }
    if (p && p.id) { v.dataset.hid = p.id; }
    box.append(el("span", k, "k"), v);
  };
  side("From", h.from); side("To", h.to);
  if (h.sentence) box.append(el("div", "→ " + h.sentence, "sum"));
  if (h.reading) box.append(el("div", "Reading the system at that point… this fills in when it is read.", "note"));
  else if (h.gone) box.append(el("div", "The system at that point is no longer kept: the requests made in between are below.", "note"));
  const acts = el("div", null, "acts"), back = el("button", "← Back to the history"); back.type = "button";
  back.onclick = () => go(histView(), h.block || "");
  acts.append(back);
  if (h.to && !h.to.now && h.from) {
    const now = el("button", "Compare “from” with now"); now.type = "button";
    now.onclick = () => go("history", compareKey(h.block || "", h.from.eid, "now"));
    acts.append(now);
  }
  box.append(acts);
  c.append(box);
  return c;
}
const RENDER = { notes, steps, lanes, chips, lines, history:historyList, compare:compareHead };

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
  if (tip.for === n && tip.box && !tip.box.hidden) return;   // the box is made on first use
  clearTimeout(tip.timer); tip.for = n;
  tip.timer = setTimeout(() => {
    if (tip.for !== n || !n.isConnected) return;
    const box = tipBox();
    box.replaceChildren();
    if (card.title) box.append(el("div", String(card.title), "t"));
    const ex = card.ex && typeof card.ex === "object" && card.ex.headline ? card.ex : null;
    if (ex) {               // what it is for, how it works, what is worth knowing; then what changed and who worked there
      const h = el("div", String(ex.headline), "h");
      if (ex.by) { const m = el("span", "✦", "llm"); m.title = "written by " + ex.by; h.append(m); }
      box.append(h);
      const how = Array.isArray(ex.how) ? ex.how.join(" ") : String(ex.how || "");
      if (how) box.append(el("div", how, "l"));
      const notes = Array.isArray(card.notes) ? card.notes.slice(0, 3) : [];
      const ins = Array.isArray(ex.insights) ? ex.insights.slice(0, notes.length > 1 ? 2 : 3) : [];
      if (ins.length) { const ul = el("ul"); for (const i of ins) ul.append(el("li", String(i))); box.append(ul); }
      for (const n of notes) box.append(el("div", String(n), "n"));
    } else for (const line of Array.isArray(card.lines) ? card.lines : []) box.append(el("div", String(line), "l"));
    const found = foundBox(hoverId(n), false);
    if (found) box.append(found);
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
// An extension's page of its own (research-diff's interactive evidence graph, its Auto-research controls), in a frame:
// only from this server's /x/ routes.
function frame(s) {
  const c = card(s.title, s.aside), src = String((s.items && s.items[0] && s.items[0].src) || "");
  if (!src.startsWith("/x/")) return c;
  const f = el("iframe", null, "ext-frame");
  f.src = src; f.loading = "lazy"; f.style.width = "100%"; f.style.border = "0";
  f.style.height = ((s.items[0].height | 0) || 720) + "px";
  c.append(f);
  return c;
}
RENDER.frame = frame;
// ---- the team's workflow: stages (in parallel within one), who waits for whom, each one's session on click ----
const SVGNS = "http://www.w3.org/2000/svg";
function dur(sec) { sec = Math.max(0, Math.round(sec)); return sec < 60 ? sec + "s" : sec < 3600 ? Math.floor(sec / 60) + "m " + (sec % 60) + "s" : Math.floor(sec / 3600) + "h " + Math.floor(sec % 3600 / 60) + "m"; }
function wfStatusText(x) {
  if (x.status === "running") return x.early ? "running · started early" : "running";
  if (x.status === "done") return "done" + (x.started && x.finished ? " in " + dur(x.finished - x.started) : "");
  if (x.status === "interrupted") return "interrupted: its session stopped" + (x.attempts > 1 ? " (attempt " + x.attempts + ")" : "");
  if (x.status === "failed") return "failed" + (x.started && x.finished ? " after " + dur(x.finished - x.started) : "");
  if (x.status === "waiting") return "waits for " + (x.waiting_on || x.waits_for || []).join(", ");
  if (x.status === "queued") return x.kind === "step" ? "next" : x.skipped ? "not launched (Claude skipped it?)" : "ready to launch";
  return x.status || "";
}
function wfNode(x) {
  const b = el("button", null, "wfnode st-" + (x.status || "queued")); b.type = "button"; b.dataset.wf = x.name || x.id;
  const nm = el("span", null, "nm"); nm.append(x.kind === "step" ? (x.title || x.id) : (x.title || x.name));
  if (x.adhoc && x.type) nm.append(el("span", x.type, "by"));
  if (x.by && x.kind === "step") nm.append(el("span", x.by, "by"));
  if (x.added) nm.append(el("span", "added", "by"));
  const st = el("span", wfStatusText(x), "st");
  if (x.status === "running" && x.started) { const t = el("span", " · " + dur(Date.now() / 1000 - x.started), "ticker"); t.dataset.since = x.started; st.append(t); }
  b.append(nm, st);
  const meta = x.kind === "step" ? (x.detail || (x.kind_of_step || "")) :
    [x.area !== x.name && x.area !== x.type ? x.area : "", x.edits ? x.edits + " edit" + (x.edits > 1 ? "s" : "") : "", x.reads ? x.reads + " read" + (x.reads > 1 ? "s" : "") : ""].filter(Boolean).join(" · ");
  if (meta) b.append(el("span", meta, "meta"));
  if (x.last) b.append(el("span", x.last, "meta"));
  if (x.attempts > 1) b.append(el("span", "attempt " + x.attempts + " (resumed)", "meta resumed"));
  if (x.note) { const n = el("span", "✎ " + x.note, "meta note"); n.title = x.note; b.append(n); }
  b.title = (x.kind === "step" ? "A step of " + x.by : x.adhoc ? "A subagent Claude launched: " + (x.title || x.name) : "Agent " + x.name) + ": click for its session";
  b.addEventListener("click", () => openAgentLog(x.name || x.id, x.title));
  return b;
}
function drawWfEdges(wrap, items) {
  let svg = wrap.querySelector("svg.wfedges");
  if (!svg) { svg = document.createElementNS(SVGNS, "svg"); svg.setAttribute("class", "wfedges"); svg.setAttribute("aria-hidden", "true"); wrap.prepend(svg); }
  svg.replaceChildren();
  const box = wrap.getBoundingClientRect(), w = wrap.scrollWidth, h = wrap.scrollHeight;
  svg.setAttribute("width", w); svg.setAttribute("height", h); svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const node = name => wrap.querySelector('.wfnode[data-wf="' + CSS.escape(name) + '"]');
  for (const x of items) {
    const to = node(x.name || x.id); if (!to) continue;
    for (const f of x.waits_for || []) {
      const from = node(f); if (!from) continue;
      const a = from.getBoundingClientRect(), b = to.getBoundingClientRect();
      const x1 = a.right - box.left + wrap.scrollLeft, y1 = a.top + a.height / 2 - box.top, x2 = b.left - box.left + wrap.scrollLeft - 4, y2 = b.top + b.height / 2 - box.top;
      const dx = Math.max(24, (x2 - x1) / 2), path = document.createElementNS(SVGNS, "path");
      path.setAttribute("d", `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`);
      const fromDone = from.classList.contains("st-done");
      path.setAttribute("class", fromDone ? "done" : to.classList.contains("st-running") || from.classList.contains("st-running") ? "live" : "");
      const tip = document.createElementNS(SVGNS, "path");
      tip.setAttribute("d", `M${x2 - 7},${y2 - 5} L${x2 + 1},${y2} L${x2 - 7},${y2 + 5}`);
      tip.setAttribute("class", path.getAttribute("class"));
      svg.append(path, tip);
    }
  }
}
function workflowCard(s) {
  const wf = s.workflow || { agents:[], steps:[], stages:[] }, c = card(s.title, s.aside, "wfcard");
  const items = (wf.agents || []).map(a => ({ ...a, kind:"agent" })).concat((wf.steps || []).map(x => ({ ...x, name:x.id })));
  if (items.length) {
    const by = Object.fromEntries(items.map(x => [x.name, x]));
    const wrap = el("div", null, "wfwrap"), board = el("div", null, "wf");
    (wf.stages || []).forEach((names, i) => {
      const col = el("div", null, "wfcol"), head = el("div", null, "wfhead");
      head.append(el("b", "Stage " + (i + 1)), el("span", names.length > 1 ? "in parallel" : i ? "after stage " + i : "first"));
      names = names.slice().sort((p, q) => ((by[p] || {}).started || 0) - ((by[q] || {}).started || 0));
      col.append(head);
      for (const n of names) if (by[n]) col.append(wfNode(by[n]));
      board.append(col);
    });
    if (wf.planned) {   // what Claude did with the plan: launched how many, skipped which, started which early
      const early = (wf.agents || []).filter(a => a.early).map(a => a.name);
      const line = el("div", "Claude launched " + wf.launched + " of " + wf.planned + " planned agents" +
        ((wf.skipped || []).length ? " · not launched: " + wf.skipped.join(", ") : "") +
        (early.length ? " · started early: " + early.join(", ") : ""), "sub wfsum" + ((wf.skipped || []).length || early.length ? " warn" : ""));
      c.append(line);
      if ((wf.skipped || []).length) {
        const ask = "Launch the planned agents you have not launched yet (" + wf.skipped.join(", ") +
          ") as subagents with their briefs from the plan, then continue the stages in order; no stage before the one it waits for has finished.";
        const b = el("button", STUDIO ? "Have Claude launch the missing ones" : "Copy the request for Claude", "wfask"); b.type = "button";
        b.addEventListener("click", async () => {
          if (!STUDIO) { try { await navigator.clipboard.writeText(ask); b.textContent = "Copied: paste it to Claude"; } catch (e) { b.textContent = ask; } return; }
          try {
            const r = await fetch("send", { method:"POST", headers:{ "X-Token":TOKEN, "Content-Type":"application/json" }, body:JSON.stringify({ text:ask, about:"" }) });
            b.textContent = r.ok ? "Sent to Claude" : "Not sent: " + r.status; b.disabled = r.ok;
          } catch (e) { b.textContent = "Not sent: " + e.message; }
        });
        c.append(b);
      }
    }
    wrap.append(board); c.append(wrap);
    if (wf.task) c.append(el("div", "Task: " + wf.task, "sub"));
    const redraw = () => drawWfEdges(wrap, items);
    requestAnimationFrame(redraw);
    if (window.ResizeObserver) new ResizeObserver(redraw).observe(wrap);
  } else c.append(el("div", "No team is working now. Claude plans one for a task with plan_agents (its agents can run in stages, one waiting for another), or runs a saved workflow.", "empty-note"));
  if ((s.saved || []).length) {
    const d = el("details", null, "wfsaved"); d.append(el("summary", "Saved workflows (" + s.saved.length + ")"));
    const ul = el("ul");
    for (const w of s.saved) {
      const li = el("li"); li.append(el("b", w.name), " ");
      li.append(el("span", [w.agents.length ? w.agents.length + " agent" + (w.agents.length > 1 ? "s" : "") : "", w.steps.length ? w.steps.length + " step" + (w.steps.length > 1 ? "s" : "") : "",
                            w.stages > 1 ? w.stages + " stages" : "", (w.by && w.by !== "system-diff" ? "from " + w.by : ""), w.scope].filter(Boolean).join(" · "), "s"));
      if (w.description) li.append(el("div", w.description, "s"));
      ul.append(li);
    }
    d.append(ul, el("div", "Claude runs one for a task with run_workflow (“run the " + s.saved[0].name + " workflow for …”), and saves the team that is working with save_workflow.", "s"));
    c.append(d);
  }
  return c;
}
RENDER.workflow = workflowCard;
setInterval(() => { for (const t of document.querySelectorAll(".ticker[data-since]")) t.textContent = " · " + dur(Date.now() / 1000 - +t.dataset.since); }, 1000);
const alog = { name:"", tab:"session", timer:0, data:null };
async function openAgentLog(name, title) {   // title: the readable name the caller shows
  alog.name = name; alog.title = title || ""; alog.tab = "session"; alog.data = null;
  $("alogname").textContent = title || name; $("alogmeta").textContent = "Loading…"; $("alogbody").replaceChildren(); $("alogacts").replaceChildren();
  const d = $("agentlog"); if (!d.open) d.showModal();
  await loadAgentLog();
}
$("agentlog").addEventListener("close", () => { clearTimeout(alog.timer); alog.name = ""; });
async function loadAgentLog() {
  clearTimeout(alog.timer);
  const name = alog.name; if (!name) return;
  let d;
  try { const r = await fetch("agents/log?name=" + encodeURIComponent(name), { cache:"no-store", headers:{ "X-Token":TOKEN } }); d = await r.json(); if (!r.ok) throw new Error(d.error || "HTTP " + r.status); }
  catch (e) { $("alogmeta").textContent = "Could not read it: " + e.message; return; }
  if (alog.name !== name) return;
  alog.data = d; paintAgentLog();
  const me = d.agent || d.step || {};
  if (me.status === "running" || me.status === "waiting") alog.timer = setTimeout(loadAgentLog, 2500);
}
function paintAgentLog() {
  const d = alog.data, me = d.agent || d.step || {};
  $("alogname").textContent = me.kind === "step" || d.step ? (me.title || me.id) : (me.title || alog.title || me.name);
  $("alogmeta").textContent = [d.step ? "a step of " + me.by : me.area !== me.name ? me.area : "", wfStatusText(me),
    me.started ? "started " + new Date(me.started * 1000).toLocaleTimeString() : "", me.stage ? "stage " + me.stage : "",
    (me.blocks || []).length ? "blocks: " + me.blocks.join(", ") : "",
    ((d.memory || {}).attempts || []).length > 1 ? "attempt " + d.memory.attempts.length + " of " + d.memory.attempts.length + ", resumed" : ""
  ].filter(Boolean).join(" · ");
  const acts = $("alogacts"); acts.replaceChildren();
  for (const a of STUDIO ? me.actions || [] : []) {   // acting on a step: in the Studio; this page only shows
    const b = el("button", a.charAt(0).toUpperCase() + a.slice(1)); b.type = "button";
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { const r = await fetch("agents/action", { method:"POST", headers:{ "X-Token":TOKEN, "Content-Type":"application/json" }, body:JSON.stringify({ step:me.id, action:a }) });
            const got = await r.json(); if (!r.ok) throw new Error(got.error || "HTTP " + r.status); }
      catch (e) { alert(e.message); }
      b.disabled = false; loadAgentLog();
    });
    acts.append(b);
  }
  if (d.session && STUDIO) { const b = el("button", "Open its session"); b.type = "button"; b.addEventListener("click", () => parent.postMessage({ studio:"session", id:d.session }, location.origin)); acts.append(b); }
  const tabs = $("alogtabs"); tabs.replaceChildren();
  const have = [["session", "Session (" + (d.transcript || []).length + ")"], ["messages", "Messages (" + (d.messages || []).length + ")"], ["files", "Files (" + (d.files || []).length + ")"], ["brief", "Brief"]];
  if (d.memory && ((d.memory.notes || []).length || d.memory.answer || (d.memory.attempts || []).length > 1))
    have.splice(1, 0, ["memory", "Memory (" + (d.memory.notes || []).length + ")"]);
  for (const [k, label] of have) {
    const b = el("button", label); b.type = "button"; b.setAttribute("role", "tab"); b.setAttribute("aria-selected", String(alog.tab === k));
    b.addEventListener("click", () => { alog.tab = k; paintAgentLog(); }); tabs.append(b);
  }
  const body = $("alogbody"), keep = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  body.replaceChildren();
  const when = at => at ? new Date(at * 1000).toLocaleTimeString() : "";
  if (alog.tab === "memory") {   // what it kept for a relaunch: its notes, its attempts, its last answer
    const m = d.memory || {};
    const tries = m.attempts || [];
    if (tries.length) body.append(el("div", tries.map((a, i) => "attempt " + (i + 1) + ": " + (a.finished ? "finished" : a.status || "stopped before it finished") + (a.started ? " · " + when(a.started) : "")).join("\n"), "alog meta"));
    if (!(m.notes || []).length) body.append(el("div", "No notes yet: an agent of a plan keeps them with agent_note at each step.", "empty-note"));
    for (const n of (m.notes || []).slice().reverse()) {
      const row = el("div", null, "alog note");
      row.append(el("div", when(n.at), "t"));
      for (const k of ["done", "next", "blockers", "notes"]) if (n[k]) row.append(el("div", k + ": " + n[k]));
      body.append(row);
    }
    if (m.answer) { body.append(el("div", "Its last answer", "t")); body.append(el("pre", m.answer, "alog text")); }
  } else if (alog.tab === "session") {
    if (!(d.transcript || []).length) body.append(el("div", d.step ? "Nothing logged for this step yet." : "No session yet: it shows as soon as the agent starts (Claude Code writes it as the agent works).", "empty-note"));
    for (const e of d.transcript || []) {
      const row = el("div", null, "alog " + e.kind + (e.error ? " err" : ""));
      if (e.at) row.append(el("span", when(e.at), "when"));
      if (e.kind === "prompt") { const det = el("details"); det.append(el("summary", "The brief it got"), el("pre", e.text)); row.append(det); }
      else if (e.kind === "text") { const p = el("p"); p.textContent = e.text; row.append(p); }
      else if (e.kind === "tool") row.append(el("span", (e.name || "") + "  " + (e.text || ""), "t"));
      else { const pre = el("pre", e.text || "(no output)"); if ((e.text || "").length > 400) { const det = el("details"); det.append(el("summary", e.error ? "error" : "output"), pre); row.append(det); } else row.append(pre); }
      body.append(row);
    }
  } else if (alog.tab === "messages") {
    if (!(d.messages || []).length) body.append(el("div", "No messages on the bus to or from it.", "empty-note"));
    for (const m of d.messages || []) { const row = el("div", null, "alog text"); row.append(el("span", when(m.at) + " · " + m.from + " → " + m.topic, "when")); const p = el("p"); p.textContent = m.text + (m.why ? " (" + m.why + ")" : ""); row.append(p); body.append(row); }
  } else if (alog.tab === "files") {
    if (!(d.files || []).length) body.append(el("div", "No files read or edited yet.", "empty-note"));
    for (const f of d.files || []) { const row = el("div", null, "alog tool"); row.append(el("span", (f.at ? when(f.at) + "  " : "") + (f.event || "") + "  " + (f.path || ""), "t")); body.append(row); }
  } else {
    body.append(d.brief ? el("pre", d.brief) : el("div", "No brief.", "empty-note"));
  }
  if (keep) body.scrollTop = body.scrollHeight;
}
function section(s) { return (RENDER[s.kind] || notes)(s); }
function goesBefore(s) {
  return s.kind === "workflow" || s.kind === "lanes" || s.kind === "chips" || s.kind === "compare" || (s.kind === "history" && !!s.pick) || isAttention(s);
}

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
  if (!ui.hover) for (const x of talk.point) { lit.add("b:" + x); }   // what Claude is pointing at
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
  const det = $("detail");
  // the strip is drawn again only when what it shows changes: a "show all" someone opened stays open
  paintAbout();
  const shows = picked ? [picked, keyed(g.details, picked), keyed(g.enter, picked), notesOf(picked)] : ["", g.aside || ""];
  const said = JSON.stringify(shows);
  if (det.dataset.shows === said) return;
  det.dataset.shows = said; det.replaceChildren();
  if (picked) {
    const text = keyed(g.details, picked), target = keyed(g.enter, picked);
    if (text && typeof text === "object") det.append(explained(text, !!target));
    else if (text) det.append(el("span", text));
    if (!target && keyed(g.files, picked) && !(text && typeof text === "object" && text.inside))
      det.append(el("div", "Click to read its code.", "what"));
    if (target && !(text && typeof text === "object" && text.inside))
      det.append(el("div", "Click to look inside " + ((text && text.title) || "it") + ".", "what"));
    if (!text && !target) det.append(el("span", rawId(picked), "what"));
    const found = foundBox(picked, true);
    if (found) det.append(found);
  } else det.append(el("span", g.aside || "Hover a part to see what it is and what it connects to.", "what"));
}
// the details strip for a block: What it is · How it works · Worth knowing · Facts, long lists behind "show all (N)"
function explained(x, canEnter) {
  const box = el("div", null, "ex");
  const part = (title, ...kids) => { const s = el("section"); s.append(el("h4", title), ...kids); box.append(s); };
  if (x.title) box.append(el("h3", String(x.title)));
  const p = el("p", String(x.headline || ""));
  if (x.by) { const m = el("span", "✦", "llm"); m.title = "written by " + x.by + " from the structure"; p.append(m); }
  part("What it is", p);
  const how = Array.isArray(x.how) ? x.how : [];
  if (how.length) part("How it works", el("p", how.join(" ")));
  const ins = Array.isArray(x.insights) ? x.insights : [];
  if (ins.length) { const ul = el("ul"); for (const i of ins) ul.append(el("li", String(i))); part("Worth knowing", ul); }
  const facts = Array.isArray(x.facts) ? x.facts : [];
  if (facts.length) {
    const list = el("div", null, "facts");
    for (const f of facts) {
      const row = el("div", null, "fact");
      row.append(el("b", String(f.title || "")), document.createTextNode(" " + String(f.summary || "")));
      const items = Array.isArray(f.items) ? f.items : [];
      if (items.length) {
        const d = el("details"), ul = el("ul");
        d.append(el("summary", "show all (" + items.length + ")"));
        for (const i of items) ul.append(el("li", String(i)));
        d.append(ul); row.append(d);
      }
      list.append(row);
    }
    part("Facts", list);
  }
  if (x.inside && (canEnter || x.kind === "file")) box.append(el("div", String(x.inside) + ".", "what"));
  return box;
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
  const f = d && keyed(((ui.data && ui.data.diagram) || {}).files, d);
  if (f) prefetch([f], true);
});
$("dia").addEventListener("focusin", e => {
  const d = pickAt(e.target);
  if (d) { ui.pick = d; ui.mine = true; ui.over = true; applyPick(); }
});
$("dia").addEventListener("focusout", () => { ui.over = false; applyPick(); });
$("dia").addEventListener("click", e => {
  const d = pickAt(e.target), g = (ui.data && ui.data.diagram) || {}, t = d && keyed(g.enter, d);
  if (t && typeof t === "object") go(t.view || ui.view, t.part || "");   // an extension's: {view, part}
  else if (t) go(ui.view === "history" ? "history" : "now", t);
  else if (d && keyed(g.files, d)) { const f = fileOf(keyed(g.files, d)); showCode(f.path, false, f); }
});
// the send box stays pinned to the bottom of the window, over the page; the page keeps room for it as it grows
(function () {
  const f = $("send");
  const room = () => document.body.style.setProperty("--sendh", (f.offsetHeight + 12) + "px");
  room();
  if (window.ResizeObserver) new ResizeObserver(room).observe(f); else addEventListener("resize", room);
})();
// a file box's click: its code, read-only, below the diagram (lines of code one click away, never the first thing),
// with an outline beside it to skim: one chunk per class and function, each with what Claude says it does (made in
// the background when a file is opened, kept per version of the file); hovering a chunk of the code shows that.
const cv = { path:"", chunks:[], summary:null, timer:0, card:null, spans:[], here:null, pinned:false };
// a diagram's file: a path, or {path, line, end} (an extension's: a claim's paragraph in the paper)
function fileOf(v) { return v && typeof v === "object" ? { path:String(v.path || ""), line:+v.line || 0, end:+v.end || +v.line || 0 }
                                                       : { path:String(v || ""), line:0, end:0 }; }
async function showCode(path, pinned, at) {
  cv.pinned = !!pinned;
  const box = $("code");
  box.hidden = false; box.replaceChildren(el("div", "Reading " + path + "…", "sub"));
  clearTimeout(cv.timer); cv.path = path; cv.chunks = []; cv.summary = null; cv.spans = []; cv.here = null;
  let got = null;
  try {
    const r = await fetch("file?path=" + encodeURIComponent(path), { cache:"no-store", headers:{ "X-Token":TOKEN } });
    got = await r.json();
  } catch (err) { got = { error: String(err) }; }
  if (cv.path !== path) return;
  box.replaceChildren();
  const head = el("div", null, "head"), close = el("button", "Close");
  close.type = "button"; close.addEventListener("click", closeCode);
  head.append(el("b", path), el("span", got && got.text != null ? (got.lines + " lines" + (got.cut ? ", cut" : "")
                                                               + " · as it is on disk now") : "", "sub"), close);
  box.append(head);
  if (!got || got.text == null) { box.append(el("div", "Could not show it: " + ((got && got.error) || "no answer"), "error")); return; }
  talk.file = path; paintAbout();
  cv.chunks = ((got.outline || {}).chunks || []).filter(c => c && c.line > 0);
  cv.summary = got.summary || null;
  const wrap = el("div", null, "cv"), nav = el("nav", null, "outline"), pre = el("pre");
  nav.setAttribute("aria-label", "Outline of " + path);
  pre.tabIndex = 0;
  for (const [i, line] of got.text.replace(/\n$/, "").split("\n").entries()) {
    const sp = el("span", line || " "); sp.dataset.ln = String(i + 1); pre.append(sp); cv.spans.push(sp);
  }
  wrap.append(nav, pre); box.append(wrap);
  paintOutline();
  pre.addEventListener("mouseover", e => { const sp = e.target.closest && e.target.closest("span[data-ln]");
                                            if (sp) showCodeCard(+sp.dataset.ln, sp); });
  pre.addEventListener("mouseleave", hideCodeCard);
  pre.addEventListener("scroll", () => { hideCodeCard(); markHere(firstVisibleLine(pre)); }, { passive:true });
  box.scrollIntoView({ block:"nearest", behavior:"smooth" });
  if (at && at.line > 0) jumpTo({ line:at.line, end:Math.max(at.line, at.end || at.line) });
  pollSummary(path);
}
// the pinned list on the left: the repositories of this family (a submodule or nested repository is its own system,
// with its own engine) and a view's own list (an extension's: the studies of a repository with several papers)
function paintNav(items, family, mode) {
  const nav = $("leftnav"); nav.replaceChildren();
  const fam = el("div");
  if (family.length || (mode && mode.one)) {
    const ul = el("ul");
    for (const r of family) {
      if (IN_STUDIO) break;   // the Studio's own column lists the repositories and their projects: not twice
      if (r.within && !r.current) continue;   // a repository inside a project's folder is part of that project
      const b = el("button"); b.type = "button";
      const here = r.kind === "folder" ? ui.scope === r.scope : r.current && !ui.scope;
      if (here) b.setAttribute("aria-current", "true");
      const t = el("span", null, "t");
      if (r.rel || r.kind === "folder") t.append(el("span", "├ ", "tree"));
      t.append(r.name + (r.kind === "folder" ? "" : r.kind === "outer" ? (r.current ? " (the whole repository)" : " (outer repository)")
                                       : r.kind === "nested" ? " (nested repository)" : " (submodule)"));
      const known = (projects.list || []).find(x => r.port && x.port === r.port);
      if (r.changed || (known && known.changed && !r.current)) t.append(el("span", null, "dot"));
      const sub = r.kind === "folder" ? (r.why || "") : (r.branch ? r.branch : "") + (r.url || r.current ? "" : (r.branch ? " · " : "") + "not open yet");
      b.append(t, el("span", sub, "s"));
      b.title = r.path;
      b.addEventListener("click", () => r.kind === "folder" ? openScope(r.scope) : r.current ? openScope("") : openRepo(r, b));
      { const li = el("li"); li.append(b); ul.append(li); }
    }
    if (mode && (mode.one || mode.found >= 2)) {   // the person's switch: detection can be wrong
      const b = el("button", mode.one ? "Split into " + mode.found + " projects" : "Show as one project", "switch"); b.type = "button";
      b.addEventListener("click", async () => {
        try { await fetch("projects/mode?one=" + (mode.one ? "0" : "1"), { method:"POST", headers:{ "X-Token":TOKEN } }); } catch (e) {}
        openScope("");
      });
      const li = el("li"); li.append(b); ul.append(li);
    }
    if (ul.childNodes.length) fam.append(el("h3", IN_STUDIO ? "This repository" : "Projects in this repository"), ul);
  }
  if (fam.childNodes.length) nav.append(...fam.childNodes);   // one list of projects, always open
  if (items.length) {   // a view's own list (an extension's, not of projects) below it
    const ul = el("ul");
    for (const it of items) {
      const b = el("button"); b.type = "button";
      if (it.active) b.setAttribute("aria-current", "true");
      const t = el("span", it.title || it.id || "", "t");
      if (it.badge !== undefined && it.badge !== "") t.append(el("span", String(it.badge), "badge"));
      b.append(t); if (it.sub) b.append(el("span", it.sub, "s"));
      const tg = it.target || {};
      b.addEventListener("click", () => typeof tg.scope === "string" ? openScope(tg.scope, tg.view || ui.view, tg.part || "")
                                                                   : go(tg.view || ui.view, tg.part || ""));
      { const li = el("li"); li.append(b); ul.append(li); }
    }
    nav.append(el("h3", (ui.data && ui.data.nav_title) || "In this view"), ul);
  }
  const on = !!nav.childNodes.length;
  nav.hidden = !on; nav.parentElement.classList.toggle("on", on);
}
function openScope(scope, view, part) {   // a project inside this repository: every tab drawn for its folder
  if (ui.scope === scope && (view === undefined || view === ui.view) && (part || "") === ui.part) return;
  ui.history.push({ view:ui.view, part:ui.part, scope:ui.scope });   // Back returns to the project before
  if (ui.history.length > 50) ui.history.shift();
  ui.scope = scope; if (view) ui.view = view; ui.part = part || ""; reset(true);
}
async function openRepo(r, b) {
  if (r.current) return;
  if (STUDIO) { parent.postMessage({ studio:"open", path:r.path }, location.origin); return; }
  const s = b.querySelector(".s"); if (s) s.textContent = "Opening…";
  for (let i = 0; i < 90; i++) {
    try {
      const q = await fetch("repos/open?rel=" + encodeURIComponent(r.rel), { method:"POST", headers:{ "X-Token":TOKEN } });
      const d = await q.json();
      if (q.ok && d.port) { location.href = projectHref(d.port, ui.view); return; }
      if (!q.ok) { if (s) s.textContent = d.error || "Could not open it"; return; }
    } catch (e) { /* try again */ }
    await new Promise(res => setTimeout(res, 1000));
  }
  if (s) s.textContent = "Its engine did not start: see system-diff doctor in it";
}
// What this page shows, told to the engine (Claude's ui_view reads it); Claude's ui_show requests, acted on.
const PAGE_ID = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)).slice(0, 36);
const seen = { said:"", at:0, hover:"", shown:"", moved:false, busyAt:0, loaded:Date.now(), handled:new Set() };
for (const ev of ["keydown", "wheel", "mousemove", "touchstart", "input"]) addEventListener(ev, () => { seen.busyAt = Date.now(); }, { passive:true, capture:true });
document.addEventListener("mouseover", e => { const n = e.target.closest && e.target.closest("#dia [data-id]"); seen.hover = n ? n.getAttribute("data-id") : ""; });
function tellState() {
  const st = { page:PAGE_ID, view:ui.view, part:ui.part, scope:ui.scope, zoom:ui.zoom, picked:ui.pick || "", hover:seen.hover,
               visible:document.visibilityState === "visible", shown:seen.shown, moved:seen.moved };
  const said = JSON.stringify(st);
  if (said === seen.said && Date.now() - seen.at < 60000) return;
  seen.said = said; seen.at = Date.now();
  fetch("ui/state", { method:"POST", headers:{ "X-Token":TOKEN, "Content-Type":"application/json" }, body:said }).catch(() => {});
}
setInterval(tellState, 300);
document.addEventListener("visibilitychange", tellState);
function movesAllowed() { try { return localStorage.getItem("sd-claude-moves") !== "off"; } catch (e) { return true; } }
function showBanner(text, buttons) {
  const b = $("shown"); b.replaceChildren(el("span", text, "why"));
  for (const [label, fn] of buttons) { const x = el("button", label); x.type = "button"; x.addEventListener("click", fn); b.append(x); }
  const lab = el("label"), box = el("input"); box.type = "checkbox"; box.checked = movesAllowed();
  box.addEventListener("change", () => { try { localStorage.setItem("sd-claude-moves", box.checked ? "on" : "off"); } catch (e) {} });
  lab.append(box, " Let Claude move my page"); b.append(lab); b.hidden = false;
}
function hideBanner() { $("shown").hidden = true; }
function takeShow(sh) {
  if (!sh || !sh.id || seen.handled.has(sh.id)) return;
  if ((+sh.at || 0) * 1000 < seen.loaded - 60000) { seen.handled.add(sh.id); return; }   // from before this page opened
  if (Date.now() - seen.busyAt < 3000) { setTimeout(() => takeShow(sh), 1000); return; }   // the person is busy here
  seen.handled.add(sh.id);
  const where = sh.view + (sh.part ? " · " + sh.part : "") + (sh.pick ? " · " + sh.pick : "");
  const why = "Claude showed you: " + where + (sh.note ? " — " + sh.note : "");
  const move = () => {
    const from = { view:ui.view, part:ui.part, scope:ui.scope };
    if (typeof sh.scope === "string" && sh.scope !== ui.scope) { ui.history.push({ view:ui.view, part:ui.part, scope:ui.scope }); ui.scope = sh.scope; ui.view = ""; }
    go(sh.view, sh.part || "");
    if (sh.pick) { ui.pick = sh.pick.includes(":") ? sh.pick : "b:" + sh.pick; ui.mine = true; }
    seen.shown = sh.id; seen.moved = true; tellState();
    showBanner(why, [["Back", () => { hideBanner(); if (from.scope !== ui.scope) { ui.scope = from.scope; ui.view = ""; } go(from.view, from.part); }], ["Stay here", hideBanner]]);
  };
  if (movesAllowed()) move();
  else {
    seen.shown = sh.id; seen.moved = false; tellState();
    showBanner("Claude wants to show you: " + where + (sh.note ? " — " + sh.note : ""), [["Go there", move], ["Not now", hideBanner]]);
  }
}
function closeCode() {
  clearTimeout(cv.timer); cv.path = ""; hideCodeCard();
  $("code").hidden = true; $("code").replaceChildren(); talk.file = ""; paintAbout();
}
function chunkAt(line) {   // the innermost class, function or method holding a line
  let best = null;
  for (const c of cv.chunks) if (c.line <= line && line <= c.end && (!best || c.depth >= best.depth)) best = c;
  return best;
}
function saidOf(c) { const s = cv.summary; return s && s.state === "done" && s.chunks ? s.chunks[c.qual] || null : null; }
function waitLine(s) {
  const d = el("div", null, "wait");
  if (!s || s.state === "summarizing" || s.state === "later") { d.append(el("span", null, "spin"), "Claude is reading this file…"); }
  else d.append(s.why || "No summary for this file.");
  return d;
}
function paintOutline() {
  const nav = $("code").querySelector("nav.outline");
  if (!nav) return;
  nav.replaceChildren();
  const s = cv.summary, top = el("div", null, "inshort");
  if (s && s.state === "done" && s.file) { top.append(el("b", "In short "), s.file); const m = el("span", "✦", "llm");
    m.title = "written by " + (s.by || "your LLM") + " from this file's code"; top.append(m); }
  else if (!s || s.state !== "done") top.append(waitLine(s));
  nav.append(top);
  if (!cv.chunks.length) { nav.append(el("div", "No classes or functions to outline.", "empty-note")); return; }
  const ol = el("ol");
  for (const c of cv.chunks) {
    const li = el("li"), said = saidOf(c);
    li.tabIndex = 0; li.dataset.line = c.line; li.dataset.end = c.end; li.style.paddingLeft = (6 + 14 * (c.depth || 0)) + "px";
    li.append(el("span", c.name, "nm"), el("span", c.kind + " · " + c.line + "–" + c.end, "k"));
    if (said && said.what) li.append(el("span", said.what, "said"));
    const go = () => jumpTo(c);
    li.addEventListener("click", go);
    li.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); go(); } });
    ol.append(li);
  }
  nav.append(ol);
  markHere(cv.here ? cv.here.line : 0);
}
function jumpTo(c) {
  const pre = $("code").querySelector("pre"), sp = cv.spans[c.line - 1];
  if (!pre || !sp) return;
  for (const x of pre.querySelectorAll("span.hl")) x.classList.remove("hl");
  for (let i = c.line - 1; i < Math.min(c.end, cv.spans.length); i++) cv.spans[i].classList.add("hl");
  // measured on screen (offsets are relative to whichever ancestor is positioned, which need not be the code box)
  if (pre.scrollHeight > pre.clientHeight + 4) pre.scrollTop += sp.getBoundingClientRect().top - pre.getBoundingClientRect().top - 8;
  else sp.scrollIntoView({ block:"center", behavior:"smooth" });   // the code box does not scroll: the page does
  markHere(c.line);
}
function firstVisibleLine(pre) {
  const top = pre.scrollTop + pre.offsetTop + 8;
  let lo = 0, hi = cv.spans.length - 1;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (cv.spans[mid].offsetTop < top) lo = mid + 1; else hi = mid; }
  return lo + 1;
}
function markHere(line) {
  const c = line ? chunkAt(line) : null; cv.here = c;
  for (const li of $("code").querySelectorAll("nav.outline li"))
    li.classList.toggle("here", !!c && +li.dataset.line === c.line && +li.dataset.end === c.end);
}
function hideCodeCard() { if (cv.card) cv.card.hidden = true; }
function showCodeCard(line, sp) {
  const c = chunkAt(line);
  if (!c) { hideCodeCard(); return; }
  if (!cv.card) { cv.card = el("div"); cv.card.id = "codecard"; cv.card.setAttribute("role", "tooltip"); document.body.append(cv.card); }
  const box = cv.card, said = saidOf(c);
  box.replaceChildren(el("div", c.qual, "t"));
  if (said) {
    for (const [k, label] of [["what", ""], ["how", "How: "], ["watch", "Watch: "]]) {
      if (!said[k]) continue;
      const l = el("div", null, "l"); if (label) l.append(el("b", label)); l.append(said[k]);
      if (k === "what") { const m = el("span", "✦", "llm"); m.title = "written by " + ((cv.summary || {}).by || "your LLM"); l.append(m); }
      box.append(l);
    }
  } else if (cv.summary && cv.summary.state === "done") box.append(el("div", "Claude said nothing about this one.", "l"));
  else box.append(waitLine(cv.summary));
  box.hidden = false;
  const r = sp.getBoundingClientRect(), pre = sp.parentElement.getBoundingClientRect(), w = box.offsetWidth, h = box.offsetHeight;
  let x = Math.min(pre.right - w - 12, innerWidth - w - 8), y = r.bottom + 6;
  if (y + h > innerHeight - 8) y = r.top - h - 6;
  box.style.left = Math.max(8, x) + "px"; box.style.top = Math.max(8, y) + "px";
  markHere(line);
}
document.addEventListener("selectionchange", () => {   // the keyboard's cursor in the code: the chunk it is in
  const sel = document.getSelection(), n = sel && sel.anchorNode;
  const sp = n && (n.nodeType === 1 ? n : n.parentElement);
  const span = sp && sp.closest && sp.closest("#code pre span[data-ln]");
  if (span) showCodeCard(+span.dataset.ln, span);
});
async function pollSummary(path) {
  const s = cv.summary;
  if (cv.path !== path || (s && !["summarizing", "later"].includes(s.state))) return;
  cv.timer = setTimeout(async () => {
    try {
      const r = await fetch("summary?path=" + encodeURIComponent(path), { cache:"no-store", headers:{ "X-Token":TOKEN } });
      const d = await r.json();
      if (cv.path !== path) return;
      if (r.ok && d.summary) { cv.summary = d.summary; if (d.summary.state !== "summarizing") { paintOutline(); hideCodeCard(); } }
    } catch (e) { /* the engine is away: try again */ }
    pollSummary(path);
  }, 2500);
}
// files the person is about to open (a file box hovered, a part zoomed into): summarised ahead, a few at a time
const fetched = new Set(), ahead = [];
let aheadBusy = false;
function prefetch(paths, now) {
  for (const p of paths) if (p && !fetched.has(p)) { fetched.add(p); now ? ahead.unshift(p) : ahead.push(p); }
  if (!aheadBusy) nextAhead();
}
async function nextAhead() {
  const p = ahead.shift();
  if (!p) { aheadBusy = false; return; }
  aheadBusy = true;
  try { await fetch("summary?prefetch=1&path=" + encodeURIComponent(p), { cache:"no-store", headers:{ "X-Token":TOKEN } }); }
  catch (e) { /* no matter */ }
  setTimeout(nextAhead, 700);
}

// ---- the conversation with Claude ----
function setTalk(open) {
  talk.open = !!open;
  $("chat").hidden = !talk.open; document.body.classList.toggle("chat-open", talk.open);
  $("chatbtn").setAttribute("aria-pressed", String(talk.open));
  try { localStorage.setItem("sd-chat", talk.open ? "1" : "0"); } catch (e) {}
  if (talk.open) { const l = $("chatlist"); l.scrollTop = l.scrollHeight; }
}
(function () { let o = "0"; try { o = localStorage.getItem("sd-chat") || "0"; } catch (e) {} setTalk(o === "1"); })();
$("chatbtn").addEventListener("click", () => setTalk(!talk.open));
$("chatclose").addEventListener("click", () => setTalk(false));
const STATUS = { queued:"Queued: the pane beside Claude picks it up within a few seconds",
                 delivered:"Delivered to the Claude Code session", answering:"Claude is answering…",
                 answered:"Answered", "no answer":"No answer recorded" };
function renderTalk(c) {
  talk.data = c;
  const list = $("chatlist"), atEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  list.replaceChildren();
  const items = Array.isArray(c.items) ? c.items : [];
  const llm = c.llm;
  $("chatbtn").classList.toggle("llm-down", !!(llm && llm.error));
  if (llm && llm.error) {   // the model system-diff's own LLM features use (summaries, descriptions) could not be reached
    const li = el("li", null, "llmerr");
    li.append(el("b", "Your model could not be reached"), el("span", " (" + new Date(llm.at * 1000).toLocaleTimeString() + "): "), el("span", llm.error));
    li.append(el("div", "File summaries and descriptions wait; system-diff tries again after " + new Date(llm.until * 1000).toLocaleTimeString() + ".", "meta"));
    list.append(li);
  }
  if (!items.length) list.append(el("li", "What you send to Claude below shows here with its answer, and the parts it names light up in the diagram.", "empty-note"));
  for (const it of items) {
    const li = el("li", null, "q st-" + String(it.status || "").replace(/\s+/g, "-"));
    if (it.about) li.append(el("div", "About: " + it.about, "about-tag"));
    li.append(el("div", it.question || "(a prompt)", "ask"));
    const meta = el("div", null, "meta");
    meta.append(el("span", it.from === "web" ? "from this page" : "typed in Claude Code"), el("span", "·"),
                el("span", ago(it.at)), el("span", "·"), el("span", STATUS[it.status] || it.status || "", "st"));
    li.append(meta);
    if (it.answer) { const a = el("div", null, "ans md"); a.append(...markdown(it.answer)); linkify(a, it.links || []); li.append(a); }
    list.append(li);
  }
  const n = items.filter(i => i.status !== "answered" && i.status !== "no answer").length;
  $("chatcount").textContent = n ? "· " + n : "";
  if (atEnd || items.length !== talk.seen) list.scrollTop = list.scrollHeight;
  talk.seen = items.length;
  paintPointing(c.pointing);
}
// what Claude points at (the point_to tool): lit in the diagram, named in a chip the person can clear
function paintPointing(p) {
  const box = $("pointing");
  const live = p && p.at && p.at !== talk.dismissed ? p : null;
  const ids = live ? (p.parts || []).map(x => x.id).filter(Boolean) : [];
  const fresh = live && p.at !== talk.pointAt;
  talk.point = ids; talk.pointAt = live ? p.at : 0;
  box.hidden = !live;
  box.replaceChildren();
  if (live) {
    const what = el("div", null, "what");
    what.append(el("b", "Claude is pointing at: "));
    (p.parts || []).forEach((x, i) => { if (i) what.append(", "); what.append(refLink(x.text, x.id, x.file)); });
    if (p.note) what.append(el("div", p.note, "sub"));
    const x = el("button", "×", "x"); x.type = "button"; x.setAttribute("aria-label", "Stop lighting these");
    x.addEventListener("click", () => { talk.dismissed = p.at; paintPointing(p); applyPick(); });
    box.append(what, x);
  }
  if (fresh && ids.length) { ui.last.dia = ""; applyPick(); const n = nodeOf(ids[0]); if (n) n.scrollIntoView({ block:"nearest", behavior:"smooth" }); }
}
function nodeOf(id) {
  const want = "b:" + id;
  for (const n of $("dia").querySelectorAll("[data-id]")) if (n.getAttribute("data-id") === want) return n;
  return null;
}
// a name of the system in an answer: hover shows its card, a click picks and lights it (or opens the part it is in)
function refLink(text, id, file) {
  const a = el("a", text, "ref"); a.href = "#";
  if (id) a.dataset.hid = "b:" + id;
  a.title = file ? "Show " + file + " in the diagram and read it" : "Show it in the diagram";
  a.addEventListener("click", e => { e.preventDefault(); pointAt(id, file); });
  return a;
}
function pointAt(id, file) {
  const n = id ? nodeOf(id) : null;
  if (n) {
    ui.pick = "b:" + id; ui.mine = true; ui.last.dia = ""; applyPick();
    n.scrollIntoView({ block:"center", behavior:"smooth" });
    n.classList.remove("flash"); void n.getBoundingClientRect(); n.classList.add("flash");
    if (file) showCode(file, true);
  } else if (id) { ui.openAfter = file || ""; go("now", id); }
  else if (file) showCode(file, true);
}
// the answer's text as DOM nodes, never as HTML: paragraphs, lists, headings, code blocks, `code` and **bold**
function inline(text) {
  const out = [], re = /`([^`\n]+)`|\*\*([^*\n]+)\*\*/g;
  let at = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > at) out.push(document.createTextNode(text.slice(at, m.index)));
    out.push(m[1] !== undefined ? el("code", m[1]) : el("strong", m[2]));
    at = re.lastIndex;
  }
  if (at < text.length) out.push(document.createTextNode(text.slice(at)));
  return out;
}
function markdown(src) {
  const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n"), out = [];
  let para = [], list = null;
  const flush = () => {
    if (para.length) { const p = el("p"); p.append(...inline(para.join(" "))); out.push(p); para = []; }
    if (list) { out.push(list); list = null; }
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      const code = [];
      for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
      const pre = el("pre"); pre.append(el("code", code.join("\n"))); out.push(pre);
      continue;
    }
    const h = /^\s{0,3}#{1,6}\s+(.*)$/.exec(line), li = /^\s*(?:[-*+]|(\d+)[.)])\s+(.*)$/.exec(line);
    if (!line.trim()) { flush(); continue; }
    if (h) { flush(); const e = el("h4"); e.append(...inline(h[1])); out.push(e); continue; }
    if (li) {
      if (para.length) { const p = el("p"); p.append(...inline(para.join(" "))); out.push(p); para = []; }
      const kind = li[1] ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== kind) { if (list) out.push(list); list = el(kind); }
      const item = el("li"); item.append(...inline(li[2])); list.append(item);
      continue;
    }
    if (list && /^\s+\S/.test(line)) { list.lastChild.append(" ", ...inline(line.trim())); continue; }
    if (list) { out.push(list); list = null; }
    para.push(line.trim());
  }
  flush();
  return out;
}
function escapeRe(t) { return t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
function linkify(box, links) {
  const whole = new Map(), plain = [];
  for (const l of links) { if (!l || !l.text) continue; whole.set(l.text, l); if (!l.code) plain.push(l); }
  for (const c of [...box.querySelectorAll("code")]) {   // `name` as inline code: the whole of it
    if (c.parentElement && c.parentElement.tagName === "PRE") continue;
    const l = whole.get(c.textContent.trim());
    if (l) { const a = refLink("", l.id, l.file); a.append(c.cloneNode(true)); c.replaceWith(a); }
  }
  if (!plain.length) return;
  plain.sort((a, b) => b.text.length - a.text.length);
  const re = new RegExp("(?<![\\w/])(" + plain.map(l => escapeRe(l.text)).join("|") + ")(?![\\w])", "g");
  const by = new Map(plain.map(l => [l.text, l]));
  const walk = document.createTreeWalker(box, NodeFilter.SHOW_TEXT), nodes = [];
  while (walk.nextNode()) { const t = walk.currentNode; if (!t.parentElement.closest("code, pre, a")) nodes.push(t); }
  for (const t of nodes) {
    const text = t.nodeValue, parts = []; let at = 0, m; re.lastIndex = 0;
    while ((m = re.exec(text))) {
      if (m.index > at) parts.push(document.createTextNode(text.slice(at, m.index)));
      const l = by.get(m[1]); parts.push(refLink(m[1], l.id, l.file)); at = re.lastIndex;
    }
    if (!parts.length) continue;
    if (at < text.length) parts.push(document.createTextNode(text.slice(at)));
    t.replaceWith(...parts);
  }
}
// what Claude found about a block while answering (the note tool): in its hover card and its details, newest first
function notesOf(id) {
  const n = talk.data && talk.data.notes;
  if (!n || !id || typeof n !== "object") return [];
  const got = Object.prototype.hasOwnProperty.call(n, id) ? n[id] : n["b:" + rawId(id)];
  return Array.isArray(got) ? got : [];
}
function foundBox(id, canClear) {
  const ns = notesOf(id);
  if (!ns.length) return null;
  const box = el("div", null, "found");
  box.append(el("div", "Found while answering", "fh"));
  for (const n of ns) {
    const row = el("div", null, "fn"), t = el("div");
    t.append(String(n.text || ""));
    if (n.question) t.append(el("div", "asked: " + n.question + " · " + ago(n.at), "q"));
    row.append(t);
    if (canClear) {
      const x = el("button", "×", "note-x"); x.type = "button"; x.title = "Clear this note";
      x.addEventListener("click", () => clearNote(n.nid));
      row.append(x);
    }
    box.append(row);
  }
  return box;
}
async function clearNote(nid) {
  try { await fetch("note/clear", { method:"POST", headers:{ "X-Token":TOKEN, "Content-Type":"application/json" },
                                    body:JSON.stringify({ nid }) }); } catch (e) { /* the next look shows it still */ }
}
// what the person is looking at goes with the question: "About: <block › file>", removable
function aboutText() {
  const parts = [];
  if (ui.mine && ui.pick && ui.pick.startsWith("b:")) {
    const card = hoverOf(ui.pick), g = (ui.data && ui.data.diagram) || {}, x = keyed(g.details, ui.pick);
    parts.push(String((card && card.title) || (x && x.title) || rawId(ui.pick)).split("  @")[0]);
  } else if (ui.part && SYSTEM_VIEWS.includes(ui.view)) {   // nothing picked: the part zoomed into is what you look at
    const card = hoverOf("b:" + ui.part);
    parts.push(String((card && card.title) || ui.part).split("  @")[0]);
  }
  if (talk.file && !parts.includes(talk.file)) parts.push(talk.file);
  return parts.join(" › ");
}
function paintAbout() {
  const t = aboutText(), box = $("about");
  if (IN_STUDIO) {
    // inside the Studio its session column is the chat: what you look at goes there, with your message
    try { parent.postMessage({ studio:"about", about:t || "" }, location.origin); } catch (e) {}
  }
  box.hidden = !t || talk.aboutOff === t;
  $("abouttext").textContent = t ? "About: " + t : "";
  box.title = t ? "Sent with your question, so Claude knows what you are looking at" : "";
}
$("aboutx").addEventListener("click", () => { talk.aboutOff = aboutText(); paintAbout(); });
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
      svg.style.maxWidth = Math.round(vb[2] * 1.8) + "px";   // fills the box; a tiny diagram is not blown up
    }
  }
  for (const n of box.querySelectorAll("[data-id]")) {
    if (keyed(g.enter, n.getAttribute("data-id"))) n.classList.add("can-enter");
    else if (keyed(g.files, n.getAttribute("data-id"))) n.classList.add("can-read");
    if (hoverOf(n.getAttribute("data-id"))) n.setAttribute("tabindex", "0");
  }
  box.classList.toggle("enterable", !!(g.enter && Object.keys(g.enter).length) || !!(g.files && Object.keys(g.files).length));
  const shown = $("code").querySelector(".head b");      // a file shown that this diagram no longer has: closed
  if (shown && !Object.values(g.files || {}).map(v => fileOf(v).path).includes(shown.textContent) && !ui.openAfter && !cv.pinned) closeCode();
}

// ---- the page: re-render only what changed ----
function changed(key, value) {
  const s = JSON.stringify(value === undefined ? null : value);
  if (ui.last[key] === s) return false;
  ui.last[key] = s; return true;
}
function render(d) {
  ui.data = d;
  {   // a view's own filters (an extension's "All · Needs attention · Changed"): chips; the part carries the choice
    const fb = $("filters"), fs = Array.isArray(d.filters) ? d.filters : [];
    fb.replaceChildren(); fb.hidden = !fs.length;
    for (const f of fs) {
      const b = el("button", f.title || f.id || ""); b.type = "button"; b.setAttribute("aria-pressed", String(!!f.active));
      const tg = f.target || {}; b.addEventListener("click", () => go(tg.view || ui.view, tg.part || "")); fb.append(b);
    }
  }
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
  if (d.chat && changed("chat", d.chat)) { renderTalk(d.chat); redrawn = true; }
  if (d.chat && d.chat.show) takeShow(d.chat.show);
  if (redrawn || !ui.mine) applyPick();
  if (ui.openAfter && g.svg) { const f = ui.openAfter; ui.openAfter = ""; showCode(f, true); }
  if (changed("files", g.files || {})) prefetch([...new Set(Object.values(g.files || {}).map(v => fileOf(v).path))].slice(0, 8), false);
  if (changed("nav", [d.nav || [], d.repos || [], d.projects || {}, ui.scope])) paintNav(d.nav || [], d.repos || [], d.projects || {});
}
function paintChrome() {
  // where an extension is home (a research repository): one set of tabs at a time, a switch both ways
  const cur = SETMODE ? (setOf(ui.view) || "system") : "";
  for (const b of document.querySelectorAll("#tabs button[data-view]")) {
    b.setAttribute("aria-selected", String(b.dataset.view === ui.view));
    const own = setOf(b.dataset.view);
    b.hidden = SETMODE && (own === "system" || own === HOME.set) && own !== cur;
  }
  let sw = $("settab");
  if (SETMODE && !sw) {
    sw = el("button"); sw.id = "settab"; sw.type = "button";
    sw.addEventListener("click", () => {
      const to = (setOf(ui.view) || "system") === "system" ? HOME.set : "system";
      rememberSet(to); go(counterpart(ui.view, to), "");
    });
    $("tabs").prepend(sw);
  }
  if (sw) {
    sw.textContent = cur === "system" ? SETS[HOME.set].title + " ▸" : "System ▸";
    sw.title = cur === "system" ? "Back to " + SETS[HOME.set].title + "'s tabs" : "The system's views: Now, Review, Agents, Topics, History";
  }
  $("back").disabled = !ui.history.length;
  $("zoomlabel").textContent = ui.zoom < 0 ? "auto" : ["detailed", "compact", "outline"][ui.zoom];
  $("in").disabled = ui.zoom === 0; $("out").disabled = ui.zoom === 2;
  const h = new URLSearchParams({ view:ui.view }); if (ui.part) h.set("part", ui.part); if (ui.scope) h.set("scope", ui.scope); if (ui.zoom >= 0) h.set("zoom", ui.zoom);
  history.replaceState(null, "", location.pathname + location.search + "#" + h);   // the query stays (?studio=1)
  paintProjects();
}
// ---- the project tabs: one per running engine, plain links to each engine's own page, keeping the view ----
__PROJECT_HREF__
// inside the Studio (system-diff app): its own column lists the projects
const STUDIO = IN_STUDIO;
if (STUDIO) document.documentElement.classList.add("in-studio");   // the Studio's glass shows through
function paintProjects() {
  const nav = $("projects"), key = JSON.stringify([projects.list, ui.view]);
  if (STUDIO) { nav.hidden = true; return; }
  if (nav.dataset.key === key) return;
  nav.dataset.key = key;
  const items = [];
  for (const p of projects.list) {
    const a = el("a"), here = !!p.current;
    a.href = projectHref(p.port, ui.view);
    a.title = (p.root || "") + (p.changed ? " · changed since you last looked" : "") + (here ? "" : " ([ and ] switch)");
    a.append(el("span", p.name || "(unnamed)", "nm"));
    if (p.branch) a.append(el("span", p.branch, "br"));
    if (p.changed) { const d = el("span", null, "dot"); d.setAttribute("aria-label", "changed"); a.append(d); }
    if (here) {
      a.setAttribute("aria-current", "page");
      a.addEventListener("click", e => e.preventDefault());
      document.title = (p.name || "System diff") + " · System diff";
    }
    items.push(a);
  }
  const all = el("a", "All projects ↗", "all");
  all.href = "projects/page"; all.title = "Every running project on one page, to bookmark (system-diff hub)";
  items.push(all);
  nav.replaceChildren(...items);
  nav.hidden = !projects.list.length;
}
async function loadProjects() {
  try {
    const r = await fetch("projects", { cache:"no-store", headers:{ "X-Token":TOKEN } });
    if (!r.ok) return;
    const d = await r.json();
    projects.list = Array.isArray(d.projects) ? d.projects : [];
    paintProjects();
  } catch (e) { /* the engine stopped: the page says so already */ }
}
function switchProject(by) {
  const n = projects.list.length, i = projects.list.findIndex(p => p.current);
  if (n < 2 || i < 0) return;
  location.href = projectHref(projects.list[(i + by + n) % n].port, ui.view);
}
// The engine numbers each state of the system; the page asks for the next one and the server answers when it comes
// (a long poll, ~25 s at most), so the page follows within a moment of a change and does nothing in between.
let polling = null;
async function load(wait) {
  const seq = ++ui.seq;
  if (polling) { polling.abort(); polling = null; }
  const q = new URLSearchParams({ view:ui.view, part:ui.part, zoom:String(ui.zoom) });
  if (ui.scope) q.set("scope", ui.scope);
  if (wait && typeof ui.version === "number") q.set("since", String(ui.version));
  if (wait && typeof talk.stamp === "string") q.set("chat", talk.stamp);
  const ctl = new AbortController(); polling = ctl;
  try {
    const r = await fetch("web?" + q, { cache:"no-store", signal:ctl.signal, headers:{ "X-Token":TOKEN } });
    const d = await r.json().catch(() => ({}));
    if (seq !== ui.seq) return false;   // the person moved on while this loaded
    if (!r.ok) throw new Error(d.error || r.status + " " + r.statusText);
    $("error").textContent = d.engine_error && /stopping|stopped/.test(d.engine_error) ? "The engine is stopping: " + d.engine_error : "";
    ui.lastError = d.engine_error || "";
    if (typeof d.version === "number") ui.version = d.version;
    if (d.chat && typeof d.chat.stamp === "string") talk.stamp = d.chat.stamp;
    if (d.drawing && ui.data && !ui.data.drawing && ui.data.view === d.view && (ui.data.scope || "") === (d.scope || "")) {
      // this view is shown already and is being drawn again: keep it on screen, say it is updating, ask again
      ui.version = -1;
      $("error").textContent = "";
      $("headline").dataset.updating = "1";
      $("headline").textContent = (ui.data.headline || ui.data.title || "") + "  · updating…";
      return true;
    }
    if ($("headline").dataset.updating) { delete $("headline").dataset.updating; ui.last.head = undefined; }
    render(d);
    if (d.stale) {   // the page drawn before, shown at once: the current one follows (the next ask waits for it)
      $("headline").dataset.updating = "1";
      $("headline").textContent = (d.headline || d.title || "") + "  · updating…";
    }
    return true;
  } catch (e) {
    if (seq !== ui.seq || (e && e.name === "AbortError")) return false;
    $("error").textContent = e instanceof TypeError ? stoppedText() : String(e.message || e);
    throw e;
  } finally { if (polling === ctl) polling = null; }
}
// While an extension's view is still drawing ("Drawing …", "· updating…"), its own word on what it is doing, once a
// second ("reading files 40% · 160 of 400 files"), with a bar when it says how far.
setInterval(async () => {
  const box = $("extprog"), busy = EXT_VIEWS.includes(ui.view) && (!!$("headline").dataset.updating || !!(ui.data && ui.data.drawing));
  if (!busy || document.hidden) { box.hidden = true; return; }
  try {
    const q = new URLSearchParams({ view:ui.view }); if (ui.scope) q.set("scope", ui.scope);
    const r = await fetch("ext/progress?" + q, { cache:"no-store", headers:{ "X-Token":TOKEN } });
    const t = r.ok ? String((await r.json()).progress || "") : "";
    box.hidden = !t; box.firstChild.textContent = t;
    const pct = /(\d{1,3})\s*%/.exec(t), bar = box.querySelector("i");
    bar.hidden = !pct; if (pct) bar.firstChild.style.width = Math.min(100, +pct[1]) + "%";
  } catch (e) { box.hidden = true; }
}, 1000);
// The engine is gone (it stopped, or its machine slept): say why, if it told us, and how it comes back. The pane's next
// look starts a fresh one; this page keeps asking and follows it when it comes back on the same port.
function stoppedText() {
  return "The engine stopped" + (ui.lastError ? " (last error: " + ui.lastError + ")" : "") +
    ". The pane beside Claude starts it again at its next look, or run `system-diff watch`; this page reconnects " +
    "by itself. `system-diff doctor` shows why it stopped (engine.log).";
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
  if (ui.view) ui.history.push({ view:ui.view, part:ui.part, scope:ui.scope });   // the project goes back too
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
  ui.view = h.view; ui.part = h.part; if (typeof h.scope === "string") ui.scope = h.scope; reset(true);
}
function zoomBy(d) {
  const z = Math.max(0, Math.min(2, (ui.zoom < 0 ? 0 : ui.zoom) + d));
  if (z === ui.zoom) return;
  ui.zoom = z; ui.last = {}; ui.version = null; paintChrome(); load();
}
for (const b of document.querySelectorAll("#tabs button[data-view]")) b.onclick = () => go(b.dataset.view, "");
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
    const about = aboutText();
    const r = await fetch("send", { method:"POST", headers:{ "X-Token":TOKEN, "Content-Type":"application/json" },
                                    body:JSON.stringify({ text, about:about && talk.aboutOff !== about ? about : "" }) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || r.status + " " + r.statusText);
    $("say").value = "";
    note("sent: it reaches Claude through the pane within a few seconds");
    setTalk(true);
  } catch (e) { note("not sent: " + e.message, true); }
  finally { $("sendbtn").disabled = false; }
}
$("send").addEventListener("submit", e => { e.preventDefault(); send(); });
// Enter sends; Shift+Enter is a new line (Ctrl/Cmd+Enter sends too); never while an IME is composing a word
$("say").addEventListener("keydown", e => {
  if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;
  if (e.shiftKey && !(e.ctrlKey || e.metaKey)) return;
  e.preventDefault(); send();
});
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(TEXTAREA|INPUT|SELECT)$/.test(t.tagName))) return;   // typing, not a shortcut
  const tab = keymap()[e.key];   // the keys of the row shown
  if (tab === "history") go("history", ui.mine && ui.pick && ui.pick.startsWith("b:") ? rawId(ui.pick) : "");
  else if (tab) go(tab, "");
  else if (e.key === "b" || e.key === "Backspace") back();
  else if (e.key === "f") go(SETMODE && setOf(ui.view) === HOME.set ? HOME.view : "now", "");
  else if (e.key === "c") setTalk(!talk.open);
  else if (e.key === "[") switchProject(-1);
  else if (e.key === "]") switchProject(1);
  else if (e.key === "-") zoomBy(1);
  else if (e.key === "+" || e.key === "=") zoomBy(-1);
  else if (e.key === "Escape") { hideTip(); hideCodeCard(); ui.mine = false; ui.pick = ""; ui.last.dia = ""; ui.hover = null; render(ui.data || {}); }
});
paintChrome();
follow();
loadProjects();
setInterval(() => { if (!document.hidden) loadProjects(); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadProjects(); });
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
        self.looked = 0                   # the engine's version the web page last showed (the project tabs' dot)
        self.fatal = ""                   # why this engine is stopping (an engine thread that died): the page says it
        self.jobs: "queue.Queue[dict]" = queue.Queue()   # the hooks' work (POST /hook), done one at a time, in order

    def hook_job(self, body: Dict[str, Any], wait: float) -> Optional[Dict[str, Any]]:
        """Queue a hook's work (agent.hook_work) and wait up to `wait` seconds for it: its answer, or None when it
        is not done by then (it is done all the same, and what it finds is kept for the agent's next hook)."""
        job = {"body": body, "done": threading.Event(), "result": None, "late": False, "lock": threading.Lock()}
        self.jobs.put(job)
        job["done"].wait(max(0.0, min(wait, 5.0)))
        with job["lock"]:
            if job["done"].is_set():
                return job["result"] or {}
            job["late"] = True
            return None

    def work(self) -> None:
        """The hooks' work, one job at a time in the order the hooks sent it (a prompt's turn mark before the edits
        that follow it), once the engine holds the working tree. An error is logged and the next job goes on."""
        from . import agent
        while not self.stop.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            result: Dict[str, Any] = {}
            try:
                if self.engine is not None:
                    self.engine.ready.wait(live.FIRST_WAIT)
                    if job["body"].get("paths"):
                        self.engine.touch(job["body"]["paths"])
                result = agent.hook_work(self.root, job["body"]) or {}
            except Exception as exc:  # noqa: BLE001 - logged; the hook's work is lost, the engine goes on
                live.log_error(self.root, f"a hook's work ({job['body'].get('event')})", exc)
            with job["lock"]:
                job["result"] = result
                job["done"].set()
                late = job["late"]
            if late and job["body"].get("event") == "tool":   # a turn's summary after the turn is stale: not kept
                try:
                    agent.say_later(self.root, str(job["body"].get("agent") or ""), result)
                except Exception as exc:  # noqa: BLE001
                    live.log_error(self.root, "keeping a hook's answer", exc)

    def last_error(self) -> str:
        """Why the engine is stopping, or the error it last met ("" when none), for the page."""
        e = self.engine
        return self.fatal or (e.error if e is not None else "") or ""

    def moved(self) -> Dict[str, Any]:
        """GET /moved (token): for the other projects' tabs, how far this engine's model has moved ("seq") and how far
        its web page has looked ("seen"): a dot on its tab while seq > seen."""
        seq = self.engine.version if self.engine is not None else self.state["version"]
        return {"id": self.ident, "seq": seq, "seen": self.looked}

    def key(self) -> tuple:
        e = self.engine
        if e is not None:   # no snapshot: the engine's files (by their identity), and how far its reading got
            e.ready.wait(live.FIRST_WAIT)
            held = e.held
            if held is not None:
                return held.sha, checkpoint_sha(self.root), turn_sha(self.root), e.reads
        return worktree_tree(self.root), checkpoint_sha(self.root), turn_sha(self.root)

    def _note_changed_files(self, tree: str) -> None:
        if self.tree and tree != self.tree:
            if is_live(tree) or is_live(self.tree):   # the engine's: its files by blob id, compared here
                a, b = LIVE_SOURCES.get(self.tree), LIVE_SOURCES.get(tree)
                ba, bb = (getattr(a, "blobs", None) or {}), (getattr(b, "blobs", None) or {})
                names = "\0".join(sorted(p for p in set(ba) | set(bb) if ba.get(p) != bb.get(p))) if a and b else ""
            else:
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
            # with the engine: what is on disk this moment, its commit written from memory (compare.snapshot asks
            # the engine in this process), one git call for the ref
            checkpoint(self.root)
        self.tick(force=True)  # the page's answer is the diff from the new checkpoint, never an older look

    def snapshot_state(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.state)

    def snapshot_view(self) -> View:
        with self.lock:
            return View(**vars(self.view))


FILE_MAX = 400_000   # bytes of a file the page shows; the rest is cut


def read_code_file(root: Path, rel: str) -> Dict[str, Any]:
    """A code file of the repository as it is on disk, for the page's file viewer: {"path", "text", "lines",
    "cut"}; {"path", "text": None, "error"} for anything else. Only a code file the system's diagram can show (by
    the config's code extensions, not skipped, not a test): never an env file, a key or anything outside the
    repository."""
    from .config import lenient
    from .extract import Code
    rel = (rel or "").replace("\\", "/").strip()
    bad = {"path": rel, "text": None}
    if not rel or rel.startswith("/") or ".." in rel.split("/") or ":" in rel or "\0" in rel:
        return {**bad, "error": "not a path in the repository"}
    try:
        code = Code(lenient(root))
        if (not code.is_code(rel) or code.skip.search(rel)) and not extensions.file_ok(rel):
            return {**bad, "error": "not a code file of the system"}
        base = Path(root).resolve()
        f = (base / rel).resolve()
        if base not in f.parents or not f.is_file():
            return {**bad, "error": "no such file"}
        with open(f, "rb") as fh:
            data = fh.read(FILE_MAX + 1)
    except Exception as exc:  # noqa: BLE001
        return {**bad, "error": type(exc).__name__}
    text = data[:FILE_MAX].decode("utf-8", errors="replace")
    return {"path": rel, "text": text, "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
            "cut": len(data) > FILE_MAX}


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
                return self._send(200, page_html(w.root).replace("__TOKEN__", w.token).encode(), "text/html; charset=utf-8")
            if path.startswith("/x/"):   # an extension's own pages: the repository's content, so as /file guards it
                if not (hub.host_ok(self.headers.get("Host"), self.server.server_address[1]) and self._authorized()):
                    return self._json(403, {"error": "forbidden"})
                return self._ext("GET", path, b"")
            if path == "/ext/progress":   # an extension's own word on what it is doing (its view still drawing)
                if not (hub.host_ok(self.headers.get("Host"), self.server.server_address[1]) and self._authorized()):
                    return self._json(403, {"error": "forbidden"})
                q = self._query()
                ext = extensions.owner(q.get("view", ""))
                fn = getattr(ext, "progress", None) if ext is not None else None
                said = ""
                if callable(fn):
                    try:
                        said = str(fn(extensions.context(w.root, q.get("view", ""), "", 160, 0, q.get("scope", "")[:300])) or "")
                    except Exception as exc:  # noqa: BLE001 - its failure is only its own
                        extensions.failed(str(getattr(ext, "NAME", "extension")) + " progress", exc)
                return self._json(200, {"progress": said[:300]})
            if path == "/projects/folders":   # the Studio's list: the folder projects, worked out where the system is
                if not (hub.host_ok(self.headers.get("Host"), self.server.server_address[1]) and self._authorized()):
                    return self._json(403, {"error": "forbidden"})
                from . import projects
                return self._json(200, {"folders": projects.listed(w.root)})
            if path == "/check":   # Claude's ui_check, from the working tree this engine holds
                if not (hub.host_ok(self.headers.get("Host"), self.server.server_address[1]) and self._authorized()):
                    return self._json(403, {"error": "forbidden"})
                q = self._query()
                from . import diagcheck
                return self._json(200, diagcheck.check(w.root, q.get("part", "")[:300], q.get("view", "now")[:40]))
            if path == "/web":
                return self._web()
            if path == "/page":
                return self._page()
            if path == "/whoami":   # this is system-diff's server for the repository whose web.json names this id
                return self._json(200, {"app": "system-diff", "id": w.ident, "pid": os.getpid()})
            if path == "/moved":   # token only: another engine of this user's, which read it from web.json
                if not live.token_ok(self.headers.get("X-Token"), w.token):
                    return self._json(403, {"error": "forbidden"})
                return self._json(200, w.moved())
            if path in ("/projects", "/projects/page"):
                return self._projects(path)
            if path == "/file":       # one code file of the system, read-only: what a file box's click shows
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                got = read_code_file(w.root, self._query().get("path", ""))
                if got.get("text") is not None:
                    got.update(file_extras(w, got["path"], got["text"]))
                return self._json(200 if got.get("text") is not None else 404, got)
            if path == "/summary":    # a file's LLM summary as it is now (started if not yet: prefetch=1 when hovered)
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                q = self._query()
                got = read_code_file(w.root, q.get("path", ""))
                if got.get("text") is None:
                    return self._json(404, got)
                extra = file_extras(w, got["path"], got["text"], prefetch=q.get("prefetch") == "1")
                return self._json(200, {"path": got["path"], "summary": extra["summary"]})
            if path == "/agents/log":   # one agent's (or extension step's) session, for the workflow's pop-up
                if not (hub.host_ok(self.headers.get("Host"), self.server.server_address[1]) and self._authorized()):
                    return self._json(403, {"error": "forbidden"})
                name = self._query().get("name", "")[:120]
                try:
                    return self._json(200, agents_mod().agent_log(w.root, name))
                except KeyError:
                    try:
                        return self._json(200, agents_mod().step_log(w.root, name))
                    except KeyError:
                        return self._json(404, {"error": f"no agent or step {name!r} in the workflow"})
            if path == "/ui/state":   # what each open page (and the pane) shows: for Claude's ui_view tool
                if not (hub.host_ok(self.headers.get("Host"), self.server.server_address[1]) and self._authorized()):
                    return self._json(403, {"error": "forbidden"})
                return self._json(200, {"pages": ui_pages(w, full=self._query().get("full") == "1")})
            if path == "/progress":   # where the engine is, at once however busy it is (the pane shows it meanwhile)
                if not self._authorized():
                    return self._json(403, {"error": "forbidden"})
                from . import __version__
                e = w.engine
                got = e.progress() if e is not None else {"stage": "No engine", "done": 0, "total": 0,
                                                           "version": 0, "ready": False, "complete": False}
                return self._json(200, {**got, "app_version": __version__})
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

        def _wait(self, since: Optional[int], key: tuple, take: bool = False, also=None) -> None:
            """A long poll: while the engine's version is still `since` and this very page is drawn at it, wait for
            the next version (at most LONG_POLL). A page not drawn at it yet (another width, say) is answered at
            once; with `take`, also as soon as the web page sends something to Claude; with `also`, as soon as it
            says so (the conversation panel changed)."""
            e = w.engine
            job = w.pages.job(key) if w.pages is not None and hasattr(w.pages, "job") else None
            if job is not None and since is not None:   # this page is being drawn again: wait for it, not ask again
                job.wait(live.LONG_POLL)
                return
            if e is None or since is None or since != e.version or w.pages is None or not w.pages.current(key):
                return
            if also is not None and also():
                return
            box = e._sd / OUTBOX
            took = (lambda: box.exists()) if take else None
            e.wait(since, live.LONG_POLL, (lambda: (took is not None and took()) or also()) if also else took)

        def _web(self):
            """One view of the pane as data for the page: pane.web_view's JSON, with the engine's "version" it was
            drawn at; with `since` (the version the page has), a long poll. It never takes the outbox (the pane,
            which submits what was sent to Claude, does)."""
            q = self._query()
            view = q.get("view", "now")
            if view not in VIEWS and extensions.owner(view) is None:
                return self._json(400, {"error": f"no view {view!r}"})
            try:
                zoom = int(q.get("zoom") or -1)
                since = self._since(q)
            except ValueError as exc:
                return self._json(400, {"error": "zoom is a number" if "since" not in str(exc) else str(exc)})
            if not -1 <= zoom <= 2:
                return self._json(400, {"error": "zoom is -1 (auto), 0, 1 or 2"})
            part = q.get("part", "")[:200]
            scope = q.get("scope", "")[:300]
            if scope:
                from . import projects
                if not projects.valid(w.root, scope):
                    scope = ""   # a project no longer listed: the whole repository
            had = q.get("chat")   # the conversation panel's stamp the page has: a change to it answers the poll too
            try:
                key = ("web", view, zoom, part) + ((scope,) if scope else ())
                self._wait(since, key, also=(lambda: chat.stamp(w.root) != had) if had is not None else None)
                version, page = _page_of(w, key, since)
                w.looked = max(w.looked, version)   # the page shows this: no dot on this project's tab
            except Exception as exc:  # noqa: BLE001 - the page shows it and keeps polling
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._json(200, {**page, "version": version, "engine_error": w.last_error(),
                                    "chat": chat_view(w, page),
                                    "repos": soon(("repos", str(w.root)), lambda: repos_view(w)) or [],
                                    "projects": soon(("mode", str(w.root)), lambda: projects_mode(w))
                                    or {"one": False, "found": 0}})

        def _projects(self, path: str):
            """The project tabs (GET /projects: every running engine of this user, from the registry cleaned of
            stale entries; token or same origin) and the page listing them (GET /projects/page). Plain links between
            engines: no CORS, no tokens. The Host must be this machine's (no DNS rebinding)."""
            bound = self.server.server_address[1]
            if not hub.host_ok(self.headers.get("Host"), bound):
                return self._json(403, {"error": "forbidden"})
            if path == "/projects/page":
                return self._send(200, hub.hub_page(w.token), "text/html; charset=utf-8")
            if not self._authorized():
                return self._json(403, {"error": "forbidden"})
            try:
                got = hub.projects(w.ident, w.moved())
            except Exception as exc:  # noqa: BLE001 - the tabs are a convenience
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            if not any(p["current"] for p in got):   # no registry (or not written): this one at least
                got.insert(0, {"root": str(w.root), "name": hub.project_name(w.root), "branch": hub.branch(w.root),
                               "url": f"http://127.0.0.1:{bound}/", "port": bound, "id": w.ident, "version": "",
                               "started": 0, "current": True, "changed": False, "busy": False})
            return self._json(200, {"projects": got, "self": w.ident})

        def _ext(self, method: str, path: str, body: bytes):
            """GET or POST /x/<extension>/<path>: the extension answers (status, content type, bytes)."""
            name, _, rest = path[3:].partition("/")
            ext = extensions.by_name(name)
            route = getattr(ext, "route", None) if ext is not None else None
            if route is None:
                return self._json(404, {"error": f"no extension {name!r}"})
            try:
                status, ctype, data = route(extensions.context(w.root, "", "", 160, 0, self._query().get("scope", "")[:300]), method, rest, self._query(),
                                            body)
            except Exception as exc:  # noqa: BLE001
                extensions.failed(name, exc)
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._send(int(status), data if isinstance(data, bytes) else str(data).encode(), ctype)

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
            if view not in VIEWS and extensions.owner(view) is None:
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
                version, page = _page_of(w, key, since)
                if take:   # drawn once per version; what was sent to Claude is taken fresh for each ask
                    from . import pane
                    page = {**page, "send": pane.take_outbox(w.root)}
                ui_note(w, {"view": view, "part": part, "zoom": zoom}, page="pane")
                asked = chat.showing(w.root)   # Claude asked to show something: the pane goes there too
                if asked:
                    page = {**page, "show": asked}
            except Exception as exc:  # noqa: BLE001 - the pane shows it and asks again
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._json(200, {"version": version, "page": page})

        def _post_touch(self):
            """The files Claude just edited: {"paths": [...]}, relative to the repository or absolute. Token only."""
            if not live.token_ok(self.headers.get("X-Token"), w.token) or (
                    self.headers.get("Origin") is not None and not self._same_origin()):
                self._drain()
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

        def _drain(self) -> None:
            """Read the request's body before answering: answering a refused POST first makes Windows abort the
            connection while the client still sends (WinError 10053)."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if 0 < n <= SEND_BODY_MAX:
                    self.rfile.read(n)
            except (ValueError, OSError):
                pass

        def _post_quit(self):
            """Stop this engine: a newer system-diff replacing it (its code is loaded once, so after an upgrade the
            running one would go on serving the old pages). Token only, never from a browser."""
            self._drain()
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

        def _post_ui_state(self):
            """POST /ui/state {"page", "view", "part", "picked", "hover", "study", "zoom", "visible", "shown", "moved"}:
            what one open page shows now (memory only, the latest per page)."""
            if not (hub.host_ok(self.headers.get("Host"), self.server.server_address[1]) and self._authorized()):
                self._drain()
                return self._json(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= 8192:
                    self._drain()
                    return self._json(400, {"error": "a small JSON object"})
                got = json.loads(self.rfile.read(n).decode("utf-8"))
                if not isinstance(got, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "a small JSON object"})
            ui_note(w, got)
            return self._json(200, {"ok": True})

        def _post_repo(self):
            """POST /repos/open?rel=<path in the family>: that repository's engine (started if it is not running):
            {"port", "url"} when it listens, {"starting": true} meanwhile (the page asks again)."""
            self._drain()
            if not (hub.host_ok(self.headers.get("Host"), self.server.server_address[1]) and self._authorized()):
                return self._json(403, {"error": "forbidden"})
            from . import repos
            target = repos.member(w.root, self._query().get("rel", "")[:400])
            if target is None:
                return self._json(404, {"error": "not a repository of this one's family"})
            got = repos._engine(target)
            if got.get("port"):
                return self._json(200, got)
            try:
                start_detached(target, wait=0)
            except Exception as exc:  # noqa: BLE001
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._json(200, {"starting": True})

        def _post_send(self):
            if self.headers.get("X-Token") != w.token or not self._same_origin():
                self._drain()
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
            about = body.get("about") if isinstance(body.get("about"), str) else ""
            text = chat.with_about(about, text)   # what the person is looking at, as a short first line
            try:
                line = outbox_put(w.root, text)
            except OSError as exc:
                return self._json(500, {"error": f"could not queue it: {exc}"})
            if w.engine is not None:
                w.engine.poke()   # the pane's long poll takes it now
            return self._json(200, {"id": line["id"], "queued": True})

        def _post_clear_note(self):
            """The person clears a note Claude kept about a block ({"nid"}): the page's token and origin only."""
            if self.headers.get("X-Token") != w.token or not self._same_origin():
                self._drain()
                return self._json(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(max(0, min(n, SEND_BODY_MAX))).decode("utf-8") or "null")
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "the body is JSON: {\"nid\": \"...\"}"})
            nid = body.get("nid") if isinstance(body, dict) else None
            if not isinstance(nid, str) or not nid:
                return self._json(400, {"error": "nid names a note"})
            try:
                gone = chat.clear_note(w.root, nid)
            except OSError as exc:
                return self._json(500, {"error": str(exc)})
            if w.engine is not None:
                w.engine.poke()
            return self._json(200 if gone else 404, {"cleared": gone})

        def _post_mark(self):
            """Point one of system-diff's refs ({"ref": "refs/worktree/system-diff/…"}: the turn, a checkpoint) at
            the working tree as the engine holds it: the hooks' and the MCP server's way to mark, one git call. Token
            only, never from a browser."""
            if not live.token_ok(self.headers.get("X-Token"), w.token) or self.headers.get("Origin") is not None:
                self._drain()
                return self._json(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(max(0, min(n, SEND_BODY_MAX))).decode("utf-8") or "null")
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "the body is JSON: {\"ref\": \"refs/worktree/system-diff/…\"}"})
            ref = body.get("ref") if isinstance(body, dict) else None
            if not isinstance(ref, str) or not ref.startswith("refs/worktree/system-diff/") or ".." in ref:
                return self._json(400, {"error": "ref is one of system-diff's: refs/worktree/system-diff/…"})
            if w.engine is None or not w.engine.ready.is_set():
                return self._json(503, {"error": "the engine is not ready"})
            try:
                sha = w.engine.mark(ref)
            except Exception as exc:  # noqa: BLE001 - the caller snapshots for itself
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return self._json(200, {"sha": sha})

        def _post_hook(self):
            """A hook's work ({"event": "prompt"|"tool"|"stop", …, "wait": seconds}): queued for the engine's
            worker, answered with {"done": true, "message", "context"} when done within `wait` (at most 5 s), else
            {"done": false} (the work goes on; what it finds comes with the agent's next hook). Token only, never from
            a browser."""
            if not live.token_ok(self.headers.get("X-Token"), w.token) or self.headers.get("Origin") is not None:
                self._drain()
                return self._json(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0 or n > SEND_BODY_MAX:
                    return self._json(400, {"error": "the body is too big"})
                body = json.loads(self.rfile.read(n).decode("utf-8") or "null")
                wait = float(body.get("wait") or 0) if isinstance(body, dict) else 0.0
            except (ValueError, UnicodeDecodeError, TypeError, AttributeError):
                return self._json(400, {"error": "the body is JSON: {\"event\": …}"})
            if not isinstance(body, dict) or body.get("event") not in ("prompt", "tool", "stop"):
                return self._json(400, {"error": "event is prompt, tool or stop"})
            got = w.hook_job(body, wait)
            if got is None:
                return self._json(200, {"done": False})
            return self._json(200, {"done": True, "message": got.get("message") or None,
                                    "context": got.get("context") or ""})

        def do_POST(self):
            if urlsplit(self.path).path.startswith("/x/"):
                if not (hub.host_ok(self.headers.get("Host"), self.server.server_address[1]) and self._authorized()):
                    return self._json(403, {"error": "forbidden"})
                n = int(self.headers.get("Content-Length") or 0)
                return self._ext("POST", urlsplit(self.path).path, self.rfile.read(min(n, 1_000_000)) if n else b"")
            if urlsplit(self.path).path == "/hook":
                return self._post_hook()
            if urlsplit(self.path).path == "/mark":
                return self._post_mark()
            if urlsplit(self.path).path == "/ui/state":
                return self._post_ui_state()
            if urlsplit(self.path).path == "/tool":   # Claude's system_diff / system_map / explain, from what this holds
                if self.headers.get("X-Token") != w.token or not hub.host_ok(self.headers.get("Host"),
                                                                             self.server.server_address[1]):
                    self._drain()
                    return self._json(403, {"error": "forbidden"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if 0 < n <= 65536 else {}
                    from . import mcp_server
                    name, a = str(body.get("tool") or ""), body.get("args") if isinstance(body.get("args"), dict) else {}
                    fn = {"diff": mcp_server.tool_diff, "map": mcp_server.tool_map, "explain": mcp_server.tool_explain}[name]
                    return self._json(200, {"result": fn(w.root, **{str(k): v for k, v in a.items()})})
                except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
                    return self._json(400, {"error": f"{type(exc).__name__}: {exc}"})
            if urlsplit(self.path).path == "/agents/plan":   # Claude's plan_agents, from the system this engine holds
                if self.headers.get("X-Token") != w.token or not hub.host_ok(self.headers.get("Host"),
                                                                             self.server.server_address[1]):
                    self._drain()
                    return self._json(403, {"error": "forbidden"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if 0 < n <= 65536 else {}
                    from . import compare as cmp_mod
                    c = cmp_mod.compare(w.root, "checkpoint", ".")
                    if body.get("workflow"):
                        from . import workflows
                        got = workflows.run(w.root, c, str(body["workflow"]), str(body.get("task") or ""),
                                            int(body.get("most") or 8))
                    else:
                        after = body.get("after") if isinstance(body.get("after"), dict) else {}
                        got = agents_mod().plan(w.root, c, str(body.get("task") or ""), int(body.get("most") or 4),
                                                [str(x) for x in body.get("focus") or []],
                                                order=body.get("order") if body.get("order") in ("parallel", "staged")
                                                else "parallel",
                                                after={str(k): [str(x) for x in v] for k, v in after.items()
                                                       if isinstance(v, list)})
                except (ValueError, TypeError, UnicodeDecodeError) as exc:
                    return self._json(400, {"error": str(exc)})
                if w.engine is not None:
                    w.engine._bump()
                return self._json(200, got)
            if urlsplit(self.path).path == "/agents/action":   # pause, resume, stop, retry an extension's step
                if self.headers.get("X-Token") != w.token or not self._same_origin() or not hub.host_ok(
                        self.headers.get("Host"), self.server.server_address[1]):
                    self._drain()
                    return self._json(403, {"error": "forbidden"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if 0 < n <= 4096 else {}
                    got = agents_mod().step_action(w.root, str(body.get("step") or ""), str(body.get("action") or ""))
                except KeyError:
                    return self._json(404, {"error": "no such step"})
                except (ValueError, UnicodeDecodeError) as exc:
                    return self._json(400, {"error": str(exc)})
                if w.engine is not None:
                    w.engine._bump()
                return self._json(200, got)
            if urlsplit(self.path).path == "/projects/mode":   # one project, or split into those found
                if not (hub.host_ok(self.headers.get("Host"), self.server.server_address[1]) and self._authorized()):
                    self._drain()
                    return self._json(403, {"error": "forbidden"})
                self._drain()
                from . import projects
                projects.set_one(w.root, self._query().get("one") == "1")
                if w.engine is not None:
                    w.engine._bump()   # a new version: every page is drawn again
                return self._json(200, projects_mode(w))
            if urlsplit(self.path).path == "/repos/open":
                return self._post_repo()
            if urlsplit(self.path).path == "/send":
                return self._post_send()
            if urlsplit(self.path).path == "/note/clear":
                return self._post_clear_note()
            if urlsplit(self.path).path == "/touch":
                return self._post_touch()
            if urlsplit(self.path).path == "/quit":
                return self._post_quit()
            if self.path != "/seen" or self.headers.get("X-Token") != w.token:
                return self._send(403, b"forbidden", "text/plain")
            w.mark_seen()
            return self._send(200, json.dumps(w.snapshot_state()).encode(), "application/json")

    return Handler


VIEWS = ("now", "review", "agents", "topics", "history")
PAGE = PAGE.replace("__PROJECT_HREF__", hub.HREF_JS)


def page_html(root: Optional[Path] = None) -> str:
    """The page, with the extensions' tabs (and keys) beside system-diff's own, and the view `root` opens on when an
    extension says so (a research repository: research-diff's)."""
    ext = extensions.views(root)
    try:
        home = extensions.home(root) if root is not None and ext else None
    except Exception:  # noqa: BLE001
        home = None
    full = extensions.views_full(root) if ext else []
    sets: Dict[str, Any] = {}
    if home and home.get("system") == "collapsed" and home.get("set"):
        sets[home["set"]] = {"title": home.get("title") or home["set"],
                             "views": [[v["view"], v["label"], v["key"], v["stands"]] for v in full if v["ext"] == home["set"]]}
    keys = {"now": "n", "review": "r", "agents": "a", "topics": "t", "history": "h"}
    shown_key = {v["view"]: (keys[v["stands"]] if v["ext"] in sets and v["stands"] else v["key"]) for v in full}
    known = set(VIEWS) | {v for v, _l, _k in ext}
    opening = extensions.OPEN_VIEW if extensions.OPEN_VIEW in known else None
    import hashlib
    pkey = hashlib.sha1(str(Path(root).resolve()).encode("utf-8", "surrogateescape")).hexdigest()[:10] if root else ""
    tabs = "".join(f'<button role="tab" data-view="{html.escape(v)}">{html.escape(label)}'
                   + (f"<kbd>{html.escape(shown_key.get(v, k))}</kbd>" if shown_key.get(v, k) else "") + "</button>"
                   for v, label, k in ext)
    return (PAGE.replace("__EXT_TABS__", tabs).replace("__EXT_VIEWS__", json.dumps([v for v, _l, _k in ext]))
            .replace("__EXT_KEYS__", json.dumps({k: v for v, _l, k in ext if k}))
            .replace("__HOME__", json.dumps(home).replace("</", "<\\/"))
            .replace("__SETS__", json.dumps(sets).replace("</", "<\\/"))
            .replace("__OPEN__", json.dumps(opening))
            .replace("__PKEY__", pkey))
PANE_LOCK = threading.Lock()


def _build(w: "Watcher", key: tuple) -> Dict[str, Any]:
    """Draw one page: ("pane", view, width, zoom, part) is pane.show's (without the outbox), ("web", view, zoom,
    part) pane.web_view's. In this process compare() takes the working tree from the engine; while its first read is
    under way, from what it has read so far, and the page says so."""
    from . import pane, panel   # here: pane may ask this module where the page is
    e = w.engine
    held = e.held if e is not None else None
    part_read = (held.blueprint.get("partial") if held is not None else None) or None
    if key[0] != "pane" and extensions.owner(key[1]) is not None:
        # an extension's view: drawn outside the pane's lock (it does not use the pane's module state; its project
        # comes as ctx.scope), so a slow one never holds system-diff's own views up
        _k, view, zoom, part, *rest = key
        page = pane.web_view(w.root, view, part, zoom, scope=rest[0] if rest else "")
        if rest:
            page["scope"] = rest[0]
        return page
    with PANE_LOCK:  # the pane's views set its module's zoom: one view at a time
        if key[0] == "pane":
            _k, view, width, zoom, part = key
            page = pane.show(w.root, width, view, part or None, zoom, take=False)
            panel.still_reading(page, part_read)
            return page
        _k, view, zoom, part, *rest = key
        pane.SCOPE = rest[0] if rest else ""   # a project inside the repository (projects.py)
        try:
            page = pane.web_view(w.root, view, part, zoom)
        finally:
            pane.SCOPE = ""
        if rest:
            page["scope"] = rest[0]
        if part_read:
            note = (f"Still reading the code: {int(part_read.get('done') or 0):,} of "
                    f"{int(part_read.get('total') or 0):,} files so far — the diagram fills in as it goes")
            page["sections"] = [{"title": "Reading the system", "aside": "", "kind": "lines",
                                 "items": [{"text": note, "style": "attention"}]}] + list(page.get("sections") or [])
            page["reading"] = dict(part_read)
        return page


def reading_page(w: "Watcher", key: tuple) -> Dict[str, Any]:
    """The page while the engine starts (before its first blueprint): where it is, never a wait. The pane's rows or
    the web page's sections, by the key's kind."""
    e = w.engine
    got = e.progress() if e is not None else {"stage": "Starting", "done": 0, "total": 0, "seconds": 0}
    line = progress_line(got)
    rows = [[["◐ Reading the system… " + line, "attention"]],
            [["  The first look at a repository reads every file once; this page updates by itself.", "muted"]]]
    if key[0] == "pane":
        _k, view, width, zoom, part = key
        return {"protocol": 2, "view": view, "part": part or "", "width": width,
                "wide": max(width, max(sum(len(r[0]) for r in row) for row in rows)), "rows": rows, "blocks": [],
                "lights": {}, "details": {}, "pick": "", "zoom": max(zoom, 0), "web": None, "send": [],
                "reading": {"done": got.get("done") or 0, "total": got.get("total") or 0, "stage": got.get("stage")}}
    _k, view, zoom, part = key
    return {"view": view, "part": part or "", "title": "Reading the system", "headline": "Reading the system… " + line,
            "status": None, "diagram": {}, "sections": [{"title": "Reading the system", "aside": "", "kind": "lines",
                                                         "items": [{"text": r[0][0].strip(), "style": r[0][1]}
                                                                   for r in rows]}],
            "hover": {}, "web": None, "send": [], "reading": got}


def drawing_page(w: "Watcher", key: tuple) -> Dict[str, Any]:
    """The page while a view is drawn for the first time and it takes more than a moment: which view, never a wait
    (the page asks again and gets the view when it is drawn)."""
    view = str(key[1])
    name = next((label for v, label, _k in extensions.views(w.root) if v == view), view.capitalize())
    text = f"Drawing {name}… it shows here as soon as it is ready."
    if key[0] == "pane":
        _k, _view, width, zoom, part = key
        rows = [[["◐ " + text, "attention"]]]
        return {"protocol": 2, "view": view, "part": part or "", "width": width, "wide": max(width, len(text) + 2),
                "rows": rows, "blocks": [], "lights": {}, "details": {}, "pick": "", "zoom": max(zoom, 0),
                "web": None, "send": [], "drawing": True}
    _k, _view, _zoom, part, *rest = key
    return {"view": view, "part": part or "", "title": name, "headline": text, "status": None, "diagram": {},
            "sections": [{"title": "Drawing", "aside": "", "kind": "lines",
                          "items": [{"text": text, "style": "attention"}]}],
            "hover": {}, "web": None, "send": [], "drawing": True, **({"scope": rest[0]} if rest else {})}


def progress_line(got: Dict[str, Any]) -> str:
    return live.progress_text(got)


_CHAT_CACHE: Dict[str, Any] = {}


def chat_index(w: "Watcher", page: Optional[Dict[str, Any]] = None) -> "chat.Index":
    """The system's names → blocks, from the engine's blueprint and the boxes this page's diagram names."""
    held = w.engine.held if w.engine is not None else None
    titles = {}
    for bid, card in ((page or {}).get("hover") or {}).items():
        if bid.startswith("b:") and isinstance(card, dict) and card.get("title"):
            titles[bid] = str(card["title"]).split("  @")[0]
    try:
        code = Code(held.cfg) if held is not None else None
    except Exception:  # noqa: BLE001
        code = None
    if code is None:
        try:
            from .config import lenient
            code = Code(lenient(w.root))
        except Exception:  # noqa: BLE001
            code = None
    return chat.Index(held.blueprint if held is not None else None, code, w.root, titles)


def chat_view(w: "Watcher", page: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The conversation panel's data (chat.view), kept while nothing it is made of changed."""
    try:
        held = w.engine.held if w.engine is not None else None
        titles = sorted(k for k in ((page or {}).get("hover") or {}) if k.startswith("b:"))
        key = json.dumps([chat.stamp(w.root), id(held.blueprint) if held is not None else 0, titles])
        if _CHAT_CACHE.get("key") != key:
            _CHAT_CACHE["value"] = chat.view(w.root, chat_index(w, page))
            _CHAT_CACHE["key"] = key
        out = dict(_CHAT_CACHE["value"])
        from . import narrate
        out["llm"] = narrate.status(w.root)   # your model could not be reached: said here, not in the terminal
        return out
    except Exception as exc:  # noqa: BLE001 - the panel is extra; the page goes on
        return {"stamp": "", "items": [], "pointing": None, "notes": {}, "error": f"{type(exc).__name__}: {exc}"}


_SOON: Dict[tuple, Dict[str, Any]] = {}
_SOON_LOCK = threading.Lock()


def soon(key: tuple, fn, wait: float = 0.5) -> Any:
    """What fn() gives, never holding the page up: worked out in a thread of its own, waited for `wait` seconds at
    most, else the last value it gave (None the first time). The extras of a page (the projects inside a repository,
    its family of repositories) read folders, which on a large repository on a slow disk can take long."""
    with _SOON_LOCK:
        st = _SOON.setdefault(key, {"value": None, "thread": None})
        t = st["thread"]
        if t is None or not t.is_alive():
            def run() -> None:
                try:
                    v = fn()
                except Exception:  # noqa: BLE001 - an extra: the page goes on without it
                    return
                with _SOON_LOCK:
                    st["value"] = v
            t = threading.Thread(target=run, name="system-diff page extra", daemon=True)
            st["thread"] = t
            t.start()
    t.join(wait)
    with _SOON_LOCK:
        return st["value"]


UI_KEEP = 120.0   # a page not heard from for this long is closed
UI_FIELDS = {"view": 40, "part": 200, "picked": 200, "hover": 200, "study": 200, "shown": 40, "project": 400,
             "scope": 300}


def ui_note(w: "Watcher", got: Dict[str, Any], page: str = "") -> None:
    """Keep what one page (or the pane: page "pane") shows now; forget pages not heard from for UI_KEEP."""
    pid = str(page or got.get("page") or "")[:40]
    if not pid:
        return
    rec = {k: str(got.get(k) or "")[:most] for k, most in UI_FIELDS.items()}
    rec.update(page=pid, kind="pane" if pid == "pane" else "web", visible=bool(got.get("visible", True)),
               moved=bool(got.get("moved")), zoom=int(got.get("zoom") or 0) if str(got.get("zoom") or "0").lstrip(
                   "-").isdigit() else 0, at=time.time())
    with UI_LOCK:
        pages = getattr(w, "ui_pages", None)
        if pages is None:
            pages = w.ui_pages = {}
        old = pages.get(pid) or {}
        if not rec["shown"] and old.get("shown"):   # what it last did with a request stays known
            rec["shown"], rec["moved"] = old["shown"], old.get("moved", False)
        pages[pid] = rec
        for k in [k for k, v in pages.items() if time.time() - v["at"] > UI_KEEP]:
            del pages[k]


def ui_pages(w: "Watcher", full: bool = False) -> List[Dict[str, Any]]:
    """The open pages, newest first; with `full`, the picked and hovered blocks' titles and headlines."""
    with UI_LOCK:
        pages = [dict(v) for v in (getattr(w, "ui_pages", None) or {}).values() if time.time() - v["at"] <= UI_KEEP]
    pages.sort(key=lambda v: -v["at"])
    if full:
        for p in pages:
            try:
                key = ("web", p["view"] or "now", p.get("zoom") or 0, p["part"])
                _v, page = _page_of(w, key)
                det = ((page or {}).get("diagram") or {}).get("details") or {}
                p["title"] = (page or {}).get("title", "")
                for k in ("picked", "hover"):
                    bid = p.get(k) or ""
                    d = det.get(bid) or det.get("b:" + bid)
                    if d:
                        p[k + "_about"] = ({x: d[x] for x in ("title", "headline", "what") if d.get(x)}
                                           if isinstance(d, dict) else str(d)[:300])
            except Exception:  # noqa: BLE001 - the bare state is still worth having
                pass
    return pages


UI_LOCK = threading.Lock()


def agents_mod():
    from . import agents
    return agents


def repos_view(w: "Watcher") -> List[Dict[str, Any]]:
    """The page's list of projects in this repository: the repositories of its family (each its own engine) and, under
    the one this engine shows, the folder projects inside it (this engine, scoped: kind "folder", with "scope"). Empty
    for a repository alone with one project."""
    try:
        from . import projects, repos
        fam = repos.family(w.root)
        folders = projects.listed(w.root)
        if not fam and not folders:
            return []
        if not fam:
            root = Path(w.root).resolve()
            fam = [{"path": str(root), "rel": "", "name": root.name, "kind": "outer", "branch": hub.branch(root),
                    "current": True}]
        out = []
        for r in fam:
            out.append(r)
            if r.get("current"):
                out += [{"path": str(Path(w.root) / p["path"]), "rel": r["rel"], "name": p["name"], "kind": "folder",
                         "scope": p["path"], "why": p["why"], "branch": "", "current": True} for p in folders]
        return out
    except Exception:  # noqa: BLE001 - the list is extra
        return []


def projects_mode(w: "Watcher") -> Dict[str, Any]:
    """Whether the person shows this repository as one project, and how many it would be split into."""
    try:
        from . import projects
        return {"one": projects.as_one(w.root), "found": len(projects.detect(w.root))}
    except Exception:  # noqa: BLE001
        return {"one": False, "found": 0}


def file_extras(w: "Watcher", path: str, text: str, prefetch: bool = False) -> Dict[str, Any]:
    """A file's outline (one chunk per class and function, for the viewer's skim column) and its LLM summary (made in
    the background the first time a version of the file is opened or prefetched)."""
    from . import fileview
    data = text.encode("utf-8")
    blob = fileview.blob_id(data)
    if extensions.load():
        try:
            from .config import lenient
            own = not Code(lenient(w.root)).is_code(path) and extensions.file_ok(path)
        except Exception:  # noqa: BLE001
            own = False
        got = extensions.outline(w.root, path, text) if own else None
        if got is not None:   # a file an extension owns (a paper's main.tex): its sections, its summary
            return {"blob": blob, "outline": got.get("outline") or {"chunks": []},
                    "summary": got.get("summary") or {"state": "failed", "why": "no summary for this file"}}
    try:
        index = chat_index(w)
    except Exception:  # noqa: BLE001
        index = None
    out = fileview.outline(path, text, w.root, index, blob)
    return {"blob": blob, "outline": out,
            "summary": fileview.summarize(w.root, path, text, blob, out.get("chunks") or [], prefetch=prefetch)}


def _page_of(w: "Watcher", key: tuple, since: Optional[int] = None) -> tuple:
    """(version, page) for a page's key: from the engine's pages when there is an engine, else drawn now. An asker
    that has `since` already gets no stale copy of it: it waits (a long poll's time) for the drawing under way."""
    if w.pages is not None:
        version, page = w.pages.get(key)
        if page.get("stale") and since is not None and version == since:
            job = w.pages.job(key)
            if job is not None:
                job.wait(live.LONG_POLL)
            version, page = w.pages.get(key)
        return version, page
    return (w.engine.version if w.engine else 0), _build(w, key)
WEB = "web.json"   # where a running watcher listens: {"url", "port", "pid", "id"}, under .git/system-diff
STARTING = "web.starting"   # a watcher being started: {"pid", "port", "at"}, until it has written web.json
ENGINE_OUT = "engine.out"   # the detached engine's stderr: what it printed before it could log (engine.log has the rest)
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
    """"0.9.0.dev2" -> (0, 9, 0, 2); a release counts above all its dev versions: (0, 9, 0, inf)."""
    import re
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?", v or "")
    if not m:
        return ()
    return (int(m[1]), int(m[2]), int(m[3]), int(m[4]) if m[4] is not None else float("inf"))


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
        raw = f.read_text(encoding="utf-8")
        if not raw.strip():   # claimed a moment ago, not written yet: a start under way, not a stale file
            if time.time() - f.stat().st_mtime < STARTING_FOR:
                return {"pid": 0, "port": 0, "at": f.stat().st_mtime}
        info = json.loads(raw)
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
        try:   # what the engine prints if it fails before it can log (an import error): engine.out, kept per start
            err: Any = open(git_dir(root) / ENGINE_OUT, "w", encoding="utf-8")
        except OSError:
            err = subprocess.DEVNULL
        kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": err,
                              "cwd": str(root), "close_fds": True}
        if os.name == "nt":
            # a hidden console, not none: what it runs (git) inherits it, instead of opening a window each time
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        try:
            child = subprocess.Popen([sys.executable, "-m", "system_diff", "--repo", str(root), "watch",
                                      "--port", str(free), "--auto"], **kw)
            os.write(fd, json.dumps({"pid": child.pid, "port": free, "at": time.time()}).encode())
        except Exception:
            os.close(fd)
            marker.unlink()
            raise
        finally:
            if err is not subprocess.DEVNULL:
                err.close()
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
        tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
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


RESTARTS = 5          # an engine thread that fails this many times within RESTART_WINDOW seconds stops the engine
RESTART_WINDOW = 300.0
WATCHDOG_EVERY = 2.0  # seconds between two looks of the watchdog at the engine's threads
FATAL_GRACE = 2.0     # seconds the server still answers (the page learns why) before it stops


class _Server(hub.Exclusive, ThreadingHTTPServer):
    """The HTTP server, logging what a request's handler raised (engine.log) instead of printing it to a stderr no
    one reads. A browser or a client closing the connection early is not an error. Its port is its own, also on
    Windows (hub.Exclusive): a second engine never shares it."""
    daemon_threads = True
    root: Optional[Path] = None

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout, TimeoutError)):
            return
        if self.root is not None:
            live.log_error(self.root, "a request", exc)


def _guarded(root: Path, name: str, fn, fatal) -> Any:
    """`fn` run in a thread for the engine's life: an exception is logged with its traceback and `fn` run again,
    unless it failed RESTARTS times within RESTART_WINDOW, when `fatal(reason)` stops the engine cleanly."""
    def run() -> None:
        fails: List[float] = []
        while True:
            try:
                fn()
                return
            except Exception as exc:  # noqa: BLE001 - logged, then restarted or stopped
                line = live.log_error(root, f"the {name} thread", exc)
                now = time.monotonic()
                fails = [t for t in fails if now - t < RESTART_WINDOW] + [now]
                if len(fails) >= RESTARTS:
                    fatal(f"{line} ({len(fails)} times in {int(RESTART_WINDOW / 60)} min)")
                    return
                live.log(root, f"restarting the {name} thread")
                time.sleep(1.0)
    t = threading.Thread(target=run, name=f"system-diff {name}", daemon=True)
    t.start()
    return t


def _watchdog(w: "Watcher", server: ThreadingHTTPServer, fatal) -> None:
    """Every couple of seconds: an engine thread that ended while the engine runs leaves no half-alive server. The
    engine's reading thread gone: stop (cleanly: web.json removed, so the next look starts a fresh engine); the page
    painter gone: started again."""
    while not w.stop.wait(WATCHDOG_EVERY):
        try:
            e = w.engine
            t = getattr(e, "_thread", None) if e is not None else None
            if t is not None and not t.is_alive() and not e.stopped.is_set():
                fatal("the engine thread stopped" + (f": {e.error}" if e.error else ""))
                return
            p = w.pages
            painter = getattr(p, "_painter", None) if p is not None else None
            if painter is not None and not painter.is_alive() and e is not None and not e.stopped.is_set():
                live.log(w.root, "ERROR in the page painter: it stopped; starting it again")
                p.start()
        except Exception as exc:  # noqa: BLE001
            live.log_error(w.root, "the watchdog", exc)


def serve(root: Path, base: str = "checkpoint", port: int = 8765, config_path: Optional[str] = None,
          interval: float = 2.0, ready=None) -> None:
    """Run until interrupted. `ready(url)` is called once the server listens (tests use port 0). While it runs,
    .git/system-diff/web.json says where (web_url reads it): written as soon as it listens, before the first read of
    the system, which in a large repository takes long (the page shows "Reading the code…" meanwhile).

    Why it stops is kept in .git/system-diff/engine.log (live.log): its start, its stop and the reason, and every
    error its threads meet, with the traceback. A thread that keeps failing, or the engine's reading thread ending,
    stops the whole server cleanly (web.json removed), so the next look starts a fresh one."""
    from . import __version__, narrate
    narrate.QUIET[0] = True   # the LLM's failures go to engine.log and the page, never this terminal
    try:
        w = Watcher(root, base, config_path, interval)
        server = None
        for p in ([port] if port == 0 else range(port, port + 10)):   # taken (another program): the next one
            try:
                server = _Server(("127.0.0.1", p), handler(w))
                break
            except OSError:
                if p == port + 9 or port == 0:
                    raise
    except Exception as exc:
        live.log_error(root, f"starting the engine on port {port}", exc)
        raise
    server.root = root
    bound = server.server_address[1]
    url = f"http://127.0.0.1:{bound}/"
    web = _write_web(root, url, bound, w.ident, w.token)
    live.log(root, f"engine {__version__} started on port {bound} (Python {sys.version.split()[0]}, {sys.executable})")
    entry = None
    why = ["stopped"]

    def fatal(reason: str) -> None:
        """Stop this engine for good: the page is told why for a moment, then the server stops."""
        if w.fatal:
            return
        w.fatal = reason
        why[0] = f"stopping after an error: {reason}"
        live.log(root, f"ERROR in the engine: {reason}; stopping (the next look of the pane starts a fresh engine)")
        if w.engine is not None:
            w.engine.poke()

        def later() -> None:
            time.sleep(FATAL_GRACE)
            server.shutdown()
        threading.Thread(target=later, daemon=True).start()

    try:
        try:
            entry = hub.register(root, url, bound, w.ident, __version__)   # the other projects' pages show it as a tab
        except Exception as exc:  # noqa: BLE001 - the tabs are a convenience
            live.log_error(root, "registering with the other projects", exc)
        if config_path is None:   # the engine reads the repository's own config, as the views do
            try:
                w.engine = live.Engine(root, interval=min(interval, live.INTERVAL)).start()
                w.pages = live.Pages(w.engine, lambda key: _build(w, key),
                                     placeholder=lambda key: reading_page(w, key),
                                     drawing=lambda key: drawing_page(w, key)).start()
            except Exception as exc:  # noqa: BLE001 - no engine: every look reads for itself, as before
                live.log_error(root, "starting the resident engine (each look reads for itself)", exc)
                w.engine, w.pages = None, None
        _guarded(root, "watcher", w.loop, fatal)
        _guarded(root, "hook worker", w.work, fatal)
        w.jobs.put({"body": {"event": "catch-up"}, "done": threading.Event(), "result": None, "late": False,
                    "lock": threading.Lock()})   # a prompt's turn that no engine marked (none ran then)
        threading.Thread(target=_watchdog, args=(w, server, fatal), name="system-diff watchdog", daemon=True).start()
        if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _interrupt)   # stopped by kill: still forget web.json
        if ready:
            ready(url, server, w)
        else:
            print(f"system-diff watch: {url} (Ctrl-C to stop)", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        why[0] = "stopped (interrupted)"
    except BaseException as exc:
        why[0] = "stopped: " + live.log_error(root, "the server", exc)
        raise
    finally:
        w.stop.set()
        if w.engine is not None:
            w.engine.stop()
        server.server_close()
        if entry is not None:
            hub.unregister(entry)
        _forget_web(web, bound)
        live.log(root, f"engine on port {bound} {why[0]}")
