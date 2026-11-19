"""The Studio's page (app.py serves it): projects | the project's page (its engine's, through the app) | the session."""

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>System diff Studio</title>
<style>
/* Glass: frosted, translucent panels floating over a soft colour field; pill controls; quiet motion.
   Solid fallbacks where the browser cannot blur or the person asks for less transparency or motion. */
:root { color-scheme:light;
  --ink:#1d1d1f; --muted:#6e6e73; --line:rgba(60,60,67,.14); --hair:rgba(255,255,255,.65);
  --glass:rgba(255,255,255,.58); --glass-strong:rgba(255,255,255,.78); --glass-soft:rgba(255,255,255,.38);
  --field-a:#dfe9ff; --field-b:#f6e3ff; --field-c:#dff7ef; --base:#f2f3f7;
  --accent:#0a84ff; --accent-2:#5e5ce6; --accent-ink:#fff; --ok:#248a3d; --bad:#d70015; --warn:#b25000;
  --code:rgba(118,118,128,.10); --shadow:0 10px 40px rgba(31,38,64,.12), 0 2px 6px rgba(31,38,64,.06);
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI Variable","Segoe UI",system-ui,Helvetica,Arial,sans-serif;
  --r:18px; --chatw:420px; --projw:260px; --blur:saturate(180%) blur(28px);
  /* the session's column: as wide as dragged, but never leaving the page under ~400px */
  --chatcol:min(var(--chatw), max(280px, calc(100vw - var(--projw) - 440px)));
  --chatcol-noproj:min(var(--chatw), max(280px, calc(100vw - 440px))); }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme:dark;
  --ink:#f5f5f7; --muted:#a1a1a6; --line:rgba(235,235,245,.12); --hair:rgba(255,255,255,.14);
  --glass:rgba(30,32,40,.55); --glass-strong:rgba(38,40,50,.78); --glass-soft:rgba(30,32,40,.32);
  --field-a:#1b2440; --field-b:#2a1a3a; --field-c:#10302a; --base:#0b0c10;
  --accent:#0a84ff; --accent-2:#7d7aff; --ok:#30d158; --bad:#ff453a; --warn:#ff9f0a;
  --code:rgba(118,118,128,.22); --shadow:0 10px 40px rgba(0,0,0,.45), 0 2px 6px rgba(0,0,0,.3); } }
:root[data-theme="dark"] { color-scheme:dark;
  --ink:#f5f5f7; --muted:#a1a1a6; --line:rgba(235,235,245,.12); --hair:rgba(255,255,255,.14);
  --glass:rgba(30,32,40,.55); --glass-strong:rgba(38,40,50,.78); --glass-soft:rgba(30,32,40,.32);
  --field-a:#1b2440; --field-b:#2a1a3a; --field-c:#10302a; --base:#0b0c10;
  --accent:#0a84ff; --accent-2:#7d7aff; --ok:#30d158; --bad:#ff453a; --warn:#ff9f0a;
  --code:rgba(118,118,128,.22); --shadow:0 10px 40px rgba(0,0,0,.45), 0 2px 6px rgba(0,0,0,.3); }
* { box-sizing:border-box; }
[hidden] { display:none !important; }   /* hidden wins over a panel's own display (grid, flex) */
html, body { height:100%; }
body { margin:0; color:var(--ink); font:14px/1.45 var(--sans); -webkit-font-smoothing:antialiased; letter-spacing:-.005em;
  background:var(--base);
  background-image:radial-gradient(60% 50% at 8% 0%, var(--field-a) 0%, transparent 70%),
                   radial-gradient(50% 45% at 100% 10%, var(--field-b) 0%, transparent 70%),
                   radial-gradient(60% 50% at 50% 100%, var(--field-c) 0%, transparent 70%);
  background-attachment:fixed; }
button, select, input, textarea { font:inherit; color:inherit; }
.glass, #projects, .mid, #chat { background:var(--glass); -webkit-backdrop-filter:var(--blur); backdrop-filter:var(--blur);
  border:1px solid var(--hair); border-radius:var(--r); box-shadow:var(--shadow), inset 0 1px 0 var(--hair); }
button { cursor:pointer; border:1px solid var(--line); background:var(--glass-strong); border-radius:999px; padding:5px 13px;
  box-shadow:inset 0 1px 0 var(--hair), 0 1px 2px rgba(0,0,0,.06); transition:transform .16s ease, background .16s ease, box-shadow .16s ease; }
button:hover:not(:disabled) { background:var(--glass); box-shadow:inset 0 1px 0 var(--hair), 0 4px 14px rgba(0,0,0,.10); }
button:active:not(:disabled) { transform:scale(.97); }
button.primary { background:linear-gradient(180deg, color-mix(in srgb, var(--accent) 88%, white), var(--accent));
  color:var(--accent-ink); border-color:transparent; font-weight:600; box-shadow:0 4px 14px color-mix(in srgb, var(--accent) 35%, transparent); }
button.primary:hover:not(:disabled) { background:linear-gradient(180deg, color-mix(in srgb, var(--accent) 80%, white), var(--accent));
  box-shadow:0 6px 18px color-mix(in srgb, var(--accent) 45%, transparent); }
button.danger { color:var(--bad); }
#optsbox > summary { list-style:none; cursor:pointer; padding:8px 14px; font-size:13px; color:var(--muted); display:flex; gap:8px; align-items:center;
  border-bottom:1px solid var(--line); overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
#optsbox > summary::-webkit-details-marker { display:none; }
#optsbox > summary::before { content:"▸"; transition:transform .16s ease; } #optsbox[open] > summary::before { transform:rotate(90deg); }
#optsbox > summary b { color:var(--ink); font-weight:600; }
button:disabled { opacity:.45; cursor:default; }
:focus-visible { outline:3px solid color-mix(in srgb, var(--accent) 55%, transparent); outline-offset:2px; }
select, input[type=text], textarea { background:var(--glass-soft); border:1px solid var(--line); border-radius:12px; padding:6px 10px;
  min-width:0; transition:border-color .16s ease, background .16s ease; }
select:focus, input[type=text]:focus, textarea:focus { background:var(--glass-strong); border-color:color-mix(in srgb, var(--accent) 60%, transparent); }
/* each part in its named area: hiding one (the projects, the session) never moves the others into its column */
.app { display:grid; height:100vh; padding:10px; gap:10px;
  grid-template-columns:var(--projw) minmax(0,1fr) 4px var(--chatcol); grid-template-areas:"proj mid grip chat"; }
.app.noproj { grid-template-columns:minmax(0,1fr) 4px var(--chatcol-noproj); grid-template-areas:"mid grip chat"; }
.app.nochat { grid-template-columns:var(--projw) minmax(0,1fr); grid-template-areas:"proj mid"; }
.app.noproj.nochat { grid-template-columns:minmax(0,1fr); grid-template-areas:"mid"; }
#projects { grid-area:proj; } .mid { grid-area:mid; } #grip { grid-area:grip; } #chat { grid-area:chat; }
.app.noproj #projects, .app.nochat #chat, .app.nochat #grip { display:none; }
#projects { overflow:auto; padding:14px 10px; display:grid; align-content:start; gap:12px; }
#projects h1 { font-size:17px; font-weight:700; letter-spacing:-.02em; margin:2px 6px 4px; }
#projects .tree { list-style:none; margin:0; padding:0; display:grid; gap:3px; }
#projects .tree ul { list-style:none; margin:3px 0 2px 14px; padding:0 0 0 6px; display:grid; gap:3px; border-left:1px solid var(--line); }
.pj { width:100%; text-align:left; display:grid; gap:1px; border:1px solid transparent; background:transparent; box-shadow:none;
  border-radius:12px; padding:7px 10px; }
.pj:hover:not(:disabled) { background:var(--glass-soft); box-shadow:none; }
.pj[aria-current="true"] { background:var(--glass-strong); border-color:var(--hair); box-shadow:0 2px 10px rgba(0,0,0,.08); }
.pj[aria-current="true"] .t { color:var(--accent); }
.pj .t { font-weight:600; overflow-wrap:anywhere; } .pj .s { font-size:12px; color:var(--muted); }
.badge { display:inline-block; font-size:11px; padding:1px 7px; border-radius:999px; background:var(--code); margin-left:6px; font-weight:500; vertical-align:1px; }
.badge.ask { background:var(--warn); color:#fff; }
form.add { display:flex; gap:6px; padding:0 4px; min-width:0; } form.add input { flex:1 1 0; min-width:0; width:0; } form.add button { flex:0 0 auto; }
.mid { display:grid; grid-template-rows:auto 1fr; min-width:0; overflow:hidden; }
.bar { display:flex; gap:8px; align-items:center; padding:8px 10px; border-bottom:1px solid var(--line); }
.bar .title { font-weight:600; font-size:15px; letter-spacing:-.01em; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#viewbox { min-height:0; }
iframe#view { width:100%; height:100%; border:0; background:transparent; border-radius:0 0 var(--r) var(--r); }
.empty { padding:48px 28px; color:var(--muted); max-width:60ch; font-size:15px; }
#grip { cursor:col-resize; border-radius:999px; background:transparent; margin:30vh 0; transition:background .16s ease; }
#grip:hover, #grip.drag, #grip:focus-visible { background:color-mix(in srgb, var(--accent) 60%, transparent); }
#chat { display:grid; grid-template-rows:auto auto 1fr auto; grid-template-columns:minmax(0,1fr); min-width:0; overflow:hidden; }
.chathead { display:flex; align-items:center; gap:6px; margin:10px 10px 4px; }
.chathead .tabs { flex:1; margin:0; }
#sessionsbtn { white-space:nowrap; padding:5px 12px; border-radius:999px; }
.pitem .pl { color:var(--muted); font-size:12px; margin-top:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.tabs { display:flex; gap:2px; margin:10px 10px 4px; padding:3px; background:var(--code); border-radius:999px; }
.tabs button { flex:1; border:0; box-shadow:none; background:transparent; border-radius:999px; padding:5px 10px; color:var(--muted); font-weight:500; }
.tabs button[aria-selected="true"] { background:var(--glass-strong); color:var(--ink); box-shadow:0 1px 4px rgba(0,0,0,.12), inset 0 1px 0 var(--hair); }
.opts { display:grid; grid-template-columns:auto minmax(0,1fr); gap:7px 10px; align-items:center; padding:8px 12px 10px; border-bottom:1px solid var(--line); font-size:13px; }
.opts label { color:var(--muted); } .opts .row { display:flex; gap:6px; flex-wrap:wrap; align-items:center; min-width:0; }
.opts .row > * { flex:1 1 0; min-width:0; } .opts .row > button { flex:0 0 auto; }
.opts select, .opts input[type=text] { width:100%; min-width:0; }
.opts select.danger { border-color:var(--bad); color:var(--bad); }
#feed { overflow:auto; overflow-x:hidden; padding:12px; display:grid; grid-template-columns:minmax(0,1fr); align-content:start; gap:10px; }
.ev { border-radius:16px; padding:8px 12px; overflow-wrap:anywhere; animation:in .2s ease both; }
@keyframes in { from { opacity:0; transform:translateY(4px); } }
.ev.user { justify-self:end; max-width:88%; white-space:pre-wrap; color:#fff; border-bottom-right-radius:6px;
  background:linear-gradient(180deg, color-mix(in srgb, var(--accent) 88%, white), var(--accent)); box-shadow:0 4px 14px color-mix(in srgb, var(--accent) 25%, transparent); }
.ev.user .qstate { font-size:11px; opacity:.85; margin-top:4px; }
.ev.user.waitq { opacity:.8; border:1px dashed color-mix(in srgb, white 60%, transparent); }
.ev.user.waitq .qstate::before { content:"◷ "; }
.ev.user.withdrawn .qtext { text-decoration:line-through; opacity:.7; }
.ev.user .qacts, .ev.user .qedit { display:flex; gap:6px; margin-top:6px; flex-wrap:wrap; }
.ev.user .qacts button, .ev.user .qedit button { font-size:11px; padding:2px 10px; }
.ev.user .qedit textarea { flex:1 1 100%; font:inherit; color:var(--ink); }
.ev.text .aboutq { display:block; font-size:11px; color:var(--muted); background:none; border:0; padding:0 0 4px; cursor:pointer; }
.ev.text { justify-self:start; max-width:96%; white-space:pre-wrap; background:var(--glass-strong); border:1px solid var(--hair); border-bottom-left-radius:6px; }
.ev.text.md { white-space:normal; }
.md > :first-child { margin-top:0; } .md > :last-child { margin-bottom:0; }
.md p { margin:0 0 .6em; } .md h1, .md h2, .md h3, .md h4, .md h5, .md h6 { margin:.7em 0 .35em; line-height:1.25; letter-spacing:-.01em; }
.md h1 { font-size:1.3em; } .md h2 { font-size:1.18em; } .md h3 { font-size:1.06em; } .md h4, .md h5, .md h6 { font-size:1em; }
.md ul, .md ol { margin:0 0 .6em; padding-left:1.4em; } .md li { margin:.15em 0; } .md li > p { margin:0 0 .3em; }
.md li > ul, .md li > ol { margin:.15em 0 .2em; } .md li.task { list-style:none; margin-left:-1.2em; } .md li.task input { margin:0 .45em 0 0; vertical-align:-1px; }
.md code { font:.92em var(--mono); background:var(--code); border-radius:6px; padding:.08em .35em; }
.md .mdcode { margin:.5em 0 .7em; border:1px solid var(--line); border-radius:10px; overflow:hidden; background:var(--code); }
.md .mdbar { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:3px 6px 3px 10px; font-size:11px; color:var(--muted); border-bottom:1px solid var(--line); }
.md .mdbar button { padding:1px 9px; font-size:11px; box-shadow:none; }
.ev .md .mdcode pre, .md .mdcode pre { margin:0; border-radius:0; background:transparent; white-space:pre; overflow:auto; overflow-wrap:normal; word-break:normal; max-height:420px; }
.md .mdcode pre code { background:none; padding:0; font:12px/1.5 var(--mono); }
.md blockquote { margin:0 0 .6em; padding:.1em 0 .1em .9em; border-left:3px solid color-mix(in srgb, var(--accent) 45%, transparent); color:var(--muted); }
.md hr { border:0; border-top:1px solid var(--line); margin:.8em 0; }
.md a { color:var(--accent); text-decoration:none; } .md a:hover { text-decoration:underline; }
.md .mdtable { overflow-x:auto; margin:0 0 .7em; }
.md table { border-collapse:collapse; font-size:13px; } .md th, .md td { border:1px solid var(--line); padding:4px 9px; vertical-align:top; overflow-wrap:normal; }
.md th { background:var(--code); font-weight:600; }
.ev.meta { color:var(--muted); font-size:12px; padding:0 4px; animation:none; }
.ev.err { color:var(--bad); font-size:13px; background:color-mix(in srgb, var(--bad) 10%, transparent); }
.ev.tool { border:1px solid var(--line); background:var(--glass-soft); font-size:13px; padding:7px 12px; }
.ev.tool summary { cursor:pointer; list-style:none; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .ev.tool summary::before { content:"▸ "; color:var(--muted); }
.ev.tool[open] summary::before { content:"▾ "; } .ev.tool .nm { font-weight:600; }
.ev pre, .card pre { overflow-wrap:anywhere; word-break:break-word; font:12px/1.5 var(--mono); background:var(--code); border-radius:10px; padding:7px 10px; margin:6px 0 0; white-space:pre-wrap; max-height:260px; overflow:auto; }
pre.old { border-left:3px solid var(--bad); } pre.new { border-left:3px solid var(--ok); }
.card { border-radius:16px; padding:10px 12px; display:grid; gap:7px; background:var(--glass-strong);
  border:1px solid color-mix(in srgb, var(--warn) 55%, transparent); box-shadow:0 6px 24px color-mix(in srgb, var(--warn) 18%, transparent); animation:in .2s ease both; }
.card .q { font-weight:600; } .card .acts { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.card .acts input { flex:1 1 140px; }
.card.done { border-color:var(--line); box-shadow:none; opacity:.7; }
.past { display:grid; gap:8px; } .past h3 { margin:6px 2px 0; font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); }
.past .pitem { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:4px 10px; align-items:center; padding:9px 12px; border-radius:14px; background:var(--glass-soft); border:1px solid var(--line); }
.past .pitem.left { background:var(--glass-strong); border-color:color-mix(in srgb, var(--accent) 45%, var(--line)); }
.past .pt { font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .past .pm { grid-column:1; color:var(--muted); font-size:12px; }
.past .pitem button { grid-row:1 / span 2; grid-column:2; } .past input[type=search] { width:100%; }
.hist { display:grid; grid-template-columns:minmax(0,1fr); gap:10px; opacity:.92; }
.histsep { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.07em; text-align:center; border-top:1px solid var(--line); padding-top:6px; }
.spin { display:inline-block; margin-left:8px; color:var(--muted); font-size:12px; } .spin.run::before { content:""; display:inline-block; width:9px; height:9px; margin-right:5px; border:2px solid var(--accent); border-right-color:transparent; border-radius:50%; vertical-align:-1px; animation:spin .8s linear infinite; }
.spin.ok { color:var(--ok); } .spin.bad { color:var(--bad); }
@keyframes spin { to { transform:rotate(360deg); } }
.st .doing { font-weight:600; } .st .quiet { color:var(--warn); font-weight:600; } .st .agent { color:var(--accent-2); }
.compose { border-top:1px solid var(--line); padding:10px 12px 12px; display:grid; grid-template-columns:minmax(0,1fr); gap:8px; }
.compose .row .st { min-width:0; }
.compose textarea { width:100%; min-height:64px; max-height:40vh; resize:vertical; border-radius:14px; padding:9px 12px; }
.compose .row { display:flex; gap:6px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }
.compose .row .st { flex-basis:100% !important; }   /* what Claude is doing: a line of its own, above the buttons */
.aboutchip { justify-self:start; display:flex; gap:6px; align-items:center; max-width:100%; font-size:12px; padding:3px 4px 3px 10px;
  border-radius:999px; background:color-mix(in srgb, var(--accent) 12%, transparent); color:var(--ink); }
.aboutchip span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.aboutchip button { border:0; box-shadow:none; background:transparent; padding:0 6px; font-size:14px; line-height:1; } .compose .row .st { flex:1; color:var(--muted); font-size:12px; }
.pulse { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--accent); margin-right:6px; animation:p 1.2s ease-in-out infinite; }
@keyframes p { 50% { opacity:.25; transform:scale(.8); } }
#agentsbox { overflow:auto; overflow-x:hidden; padding:12px; display:grid; grid-template-columns:minmax(0,1fr); align-content:start; gap:10px; }
#agentsbox h3 { margin:6px 2px 0; font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); }
.ag { border:1px solid var(--hair); background:var(--glass-strong); border-radius:14px; padding:9px 12px; display:grid; gap:4px; }
.ag .t { font-weight:600; } .ag .s { color:var(--muted); font-size:12px; } .ag .acts { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.notice { margin:10px 12px 0; padding:10px 12px; border-radius:14px; background:color-mix(in srgb, var(--warn) 12%, transparent);
  border:1px solid color-mix(in srgb, var(--warn) 40%, transparent); }
.notice code { font-family:var(--mono); }
/* keys: names only, the value never comes back to the page */
.keys { display:grid; gap:5px; min-width:0; }
.keys .k { display:flex; gap:6px; align-items:center; min-width:0; font-size:12px; }
.keys .k code { font:12px var(--mono); font-weight:600; } .keys .k .s { color:var(--muted); flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.keys .k button { padding:1px 9px; font-size:12px; }
.keys form, .card form { display:grid; gap:6px; } .keys form input, .keys form select, .card input, .card select { width:100%; min-width:0; }
input[type=password] { background:var(--glass-soft); border:1px solid var(--line); border-radius:12px; padding:6px 10px; }
::-webkit-scrollbar { width:10px; height:10px; } ::-webkit-scrollbar-thumb { background:var(--code); border-radius:999px; border:3px solid transparent; background-clip:padding-box; }
@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
  :root { --glass:var(--glass-strong); } }
@media (prefers-reduced-transparency: reduce) {
  .glass, #projects, .mid, #chat { -webkit-backdrop-filter:none; backdrop-filter:none; background:var(--base); }
  :root { --glass-soft:var(--code); } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation:none !important; transition:none !important; } }
@media (max-width: 900px) {
  .app, .app.noproj, .app.nochat, .app.noproj.nochat { grid-template-columns:minmax(0,1fr); height:auto; padding:8px; }
  .app { grid-template-rows:auto minmax(60vh,1fr) auto; grid-template-areas:"proj" "mid" "chat"; }
  .app.noproj { grid-template-rows:minmax(60vh,1fr) auto; grid-template-areas:"mid" "chat"; }
  .app.nochat { grid-template-rows:auto minmax(60vh,1fr); grid-template-areas:"proj" "mid"; }
  .app.noproj.nochat { grid-template-rows:minmax(60vh,1fr); grid-template-areas:"mid"; }
  #grip { display:none; } #projects { max-height:32vh; }
  #chat { min-height:70vh; }
}
</style></head><body>
<div class="app" id="app">
  <nav id="projects" aria-label="Projects">
    <h1>System diff Studio</h1>
    <ul class="tree" id="tree"></ul>
    <form class="add" id="addform"><input type="text" id="addpath" placeholder="Add a repository: its folder" aria-label="Folder of a repository to add"><button type="submit">Add</button></form>
    <div class="ev meta" id="projnote"></div>
  </nav>
  <div class="mid">
    <div class="bar">
      <button type="button" id="toggleproj" title="Show or hide the projects" aria-pressed="true">☰</button>
      <span class="title" id="ptitle">No project open</span>
      <button type="button" id="reload" title="Reload the project's page">↻</button>
      <button type="button" id="togglechat" title="Show or hide the session" aria-pressed="true">Session</button>
      <button type="button" id="theme" title="Light, dark, or as your system is">◐</button>
    </div>
    <div id="viewbox"><div class="empty" id="viewempty">Pick a project on the left, or add a repository by its folder. Its system-diff page opens here, and a Claude Code session beside it.</div></div>
  </div>
  <div id="grip" role="separator" aria-orientation="vertical" aria-label="Resize the session" tabindex="0"></div>
  <aside id="chat" aria-label="Claude Code session">
    <div class="chathead"><div class="tabs" role="tablist">
      <button role="tab" id="tabsession" aria-selected="true">Session</button>
      <button role="tab" id="tabagents" aria-selected="false">Agents</button>
    </div><button type="button" id="sessionsbtn" title="This project's conversations: new, open, past (like /resume)">☰ Sessions</button></div>
    <div id="sessionhead"><details id="optsbox" open><summary id="optsum"><b>Session options</b><span id="optline"></span></summary>
      <div class="opts" id="opts">
        <label for="sesspick">Session</label>
        <div class="row"><select id="sesspick" aria-label="Session"></select><button type="button" id="newsess" class="primary">New</button></div>
        <label for="settings">Settings</label><select id="settings"></select>
        <label for="model">Model</label><div class="row"><input type="text" id="model" list="models" placeholder="the settings' default"><datalist id="models"></datalist></div>
        <label for="effort">Effort</label><select id="effort"></select>
        <label for="mode">Permissions</label><select id="mode"></select>
        <label for="agentpick">Agent</label><select id="agentpick"><option value="">Claude (no saved agent)</option></select>
        <label for="pastbtn">Resume</label><div class="row"><button type="button" id="pastbtn" title="Claude Code's past conversations of this project, like /resume">Past conversations…</button></div>
        <label style="align-self:start;padding-top:8px">Keys</label><div class="keys" id="keys"><span class="ev meta">…</span></div>
        <span></span><div class="row"><span class="ev meta" id="choicefrom"></span><button type="button" id="ownoff" hidden>Use the all-projects choice</button></div>
        <span></span><div class="row"><button type="button" id="forall" title="These choices for every project (a project's own change still wins there)">Use for all projects</button><button type="button" id="applyopen" hidden title="The model and the permission mode change now in every open session; settings, effort and agent from the next session">Apply to open sessions</button></div>
        <span></span><div class="row"><button type="button" id="fork" hidden title="Continue this conversation in a new session; this one stays as it is">Fork</button><button type="button" id="apply" hidden title="Start a session with these settings that continues this conversation">Apply to this conversation</button></div>
      </div>
    </details></div>
    <div id="feed" aria-live="polite"></div>
    <div id="agentsbox" hidden></div>
    <div class="compose" id="compose">
      <div class="aboutchip" id="aboutchip" hidden title="Sent with your message, so Claude knows what you are looking at on the page"><span id="abouttext"></span><button type="button" id="aboutx" aria-label="Do not send this">×</button></div>
      <div id="keyask" hidden></div>
      <textarea id="msg" placeholder="Ask Claude… (Enter sends, Shift+Enter a new line, ↑ ↓ past messages)" aria-label="Message to Claude"></textarea>
      <div class="row"><span class="st" id="sstatus"></span><button type="button" id="stop" class="danger" disabled>Interrupt</button><button type="button" id="closesess">Close</button><button type="button" id="isend" hidden title="Stop what Claude is doing and send this as the next request">Stop &amp; send</button><button type="button" id="send" class="primary">Send</button></div>
    </div>
  </aside>
</div>
<script>
const TOKEN = "__TOKEN__";
const $ = id => document.getElementById(id);
function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined && text !== null && text !== "") e.textContent = text; if (cls) e.className = cls; return e; }
async function api(path, body, ms) {   // every call gives up after `ms` (a long poll asks for more): never a silent wait
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), ms || 30000);
  const opt = { cache:"no-store", headers:{ "X-Token":TOKEN }, signal:ctl.signal };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(path, opt); }
  catch (e) { throw new Error(e.name === "AbortError" ? "the Studio did not answer " + path.split("?")[0] + " in " + Math.round((ms || 30000) / 1000) + " s" : e.message); }
  finally { clearTimeout(timer); }
  let d = {};
  try { d = await r.json(); } catch (e) { d = { error:"no answer" }; }
  if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}
const keep = { get(k, d) { try { const v = localStorage.getItem("sd-studio-" + k); return v === null ? d : v; } catch (e) { return d; } },
               set(k, v) { try { localStorage.setItem("sd-studio-" + k, v); } catch (e) {} } };
const S = { projects:[], project:"", path:"", choices:null, session:null, next:0, polling:0, past:[], sessions:[], streaming:null, cards:{} };

// ── theme, columns ───────────────────────────────────────────────────────────────────────────────────────────
(function () {
  const order = ["auto", "light", "dark"]; let t = keep.get("theme", "auto");
  const paint = () => { if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.dataset.theme = t; $("theme").textContent = "◐ " + t; };
  $("theme").addEventListener("click", () => { t = order[(order.indexOf(t) + 1) % 3]; keep.set("theme", t); paint(); }); paint();
  const app = $("app");
  const cols = () => { app.classList.toggle("noproj", keep.get("proj", "1") === "0"); app.classList.toggle("nochat", keep.get("chat", "1") === "0");
    $("toggleproj").setAttribute("aria-pressed", String(keep.get("proj", "1") !== "0")); $("togglechat").setAttribute("aria-pressed", String(keep.get("chat", "1") !== "0")); };
  $("toggleproj").addEventListener("click", () => { keep.set("proj", keep.get("proj", "1") === "0" ? "1" : "0"); cols(); });
  $("togglechat").addEventListener("click", () => { keep.set("chat", keep.get("chat", "1") === "0" ? "1" : "0"); cols(); });
  cols();
  const grip = $("grip"); let drag = false;
  // the session's width: as dragged, but never more than the window leaves (a width dragged on a big screen, then
  // opened on a small one, would leave the page no room)
  const fit = px => Math.max(280, Math.min(innerWidth - 440 - ($("app").classList.contains("noproj") ? 0 : 260), px));
  const setW = px => { const v = fit(px); document.documentElement.style.setProperty("--chatw", v + "px"); keep.set("chatw", String(Math.round(v))); };
  const wantW = () => +keep.get("chatw", "420") || 420;
  const applyW = () => document.documentElement.style.setProperty("--chatw", fit(wantW()) + "px");   // not saved: the wish stays
  applyW(); addEventListener("resize", applyW);
  grip.addEventListener("pointerdown", e => { drag = true; grip.classList.add("drag"); grip.setPointerCapture(e.pointerId); $("view") && ($("view").style.pointerEvents = "none"); });
  grip.addEventListener("pointermove", e => { if (drag) setW(innerWidth - e.clientX); });
  grip.addEventListener("pointerup", () => { drag = false; grip.classList.remove("drag"); $("view") && ($("view").style.pointerEvents = ""); });
  grip.addEventListener("keydown", e => { const cur = $("chat").offsetWidth; if (e.key === "ArrowLeft") setW(cur + 24); if (e.key === "ArrowRight") setW(cur - 24); });
})();

// ── projects ─────────────────────────────────────────────────────────────────────────────────────────────────
function projBtn(p, depth) {
  const b = el("button", null, "pj"); b.type = "button";
  if (p.id === S.project && (p.scope || "") === (S.scope || "")) b.setAttribute("aria-current", "true");
  const t = el("span", p.name + (p.kind === "submodule" ? " (submodule)" : p.kind === "nested" && depth ? " (nested)" : ""), "t");
  if (p.sessions) t.append(el("span", p.sessions + " session" + (p.sessions > 1 ? "s" : ""), "badge"));
  if (p.waiting) t.append(el("span", "asks you", "badge ask"));
  b.append(t, el("span", p.kind === "folder" ? (p.why || "a project in this repository") : [p.branch, p.port ? "" : "engine not running"].filter(Boolean).join(" · "), "s"));
  b.title = p.path;
  b.addEventListener("click", () => openProject(p));
  return b;
}
function paintProjects() {
  const ul = $("tree"); ul.replaceChildren();
  for (const p of S.projects) {
    const li = el("li"); li.append(projBtn(p, 0));
    if ((p.kids || []).length) { const sub = el("ul"); for (const k of p.kids) { if (k.within) continue; const l2 = el("li"); l2.append(projBtn(k, 1)); sub.append(l2); } li.append(sub); }
    ul.append(li);
  }
  if (!S.projects.length) ul.append(el("li", "No projects yet: add a repository's folder below, or open Claude Code in one.", "ev meta"));
}
async function loadProjects() {
  try { S.projects = (await api("api/projects", undefined, 60000)).projects || []; paintProjects(); $("projnote").textContent = ""; }
  catch (e) { $("projnote").textContent = "Could not list the projects: " + e.message; }
}
function findProject(pred) {
  for (const p of S.projects) { if (pred(p)) return p; for (const k of p.kids || []) if (pred(k)) return k; }
  return null;
}
async function openProject(p) {
  const same = S.project === p.id;
  if (!same) { S.about = ""; S.aboutOff = ""; paintAboutChip(); }
  S.project = p.id; S.scope = p.scope || ""; S.path = p.path; keep.set("project", p.id);
  $("ptitle").textContent = p.name + (p.branch ? " · " + p.branch : "");
  paintProjects();
  const box = $("viewbox"); box.replaceChildren();
  const f = el("iframe"); f.id = "view"; f.title = "The system-diff page of " + p.name;
  f.src = "p/" + p.id + "/?studio=1" + (S.scope ? "#scope=" + encodeURIComponent(S.scope) : ""); box.append(f);
  if (same && p.kind === "folder") return;   // the same repository, another of its projects: its sessions stay
  api("api/projects/open", { project:p.id, wait:0 }).catch(() => {});
  await Promise.allSettled([loadChoices(), loadSessions(), loadPast(), loadKeys()]);   // side by side: one slow list never holds the others
  if ($("tabagents").getAttribute("aria-selected") === "true") loadAgents();
}
$("reload").addEventListener("click", () => { const f = $("view"); if (f) f.src = f.src; });
$("addform").addEventListener("submit", async e => {
  e.preventDefault(); const path = $("addpath").value.trim(); if (!path) return;
  try { const got = await api("api/projects", { path }); $("addpath").value = ""; $("projnote").textContent = "Added " + got.added;
        await loadProjects(); const p = findProject(x => x.id === got.id); if (p) openProject(p); }
  catch (err) { $("projnote").textContent = err.message; }
});
// what you look at on the project's page (a part, a file): sent with your message, as the page itself would
S.about = ""; S.aboutOff = "";
function paintAboutChip() {
  const on = !!S.about && S.aboutOff !== S.about;
  $("aboutchip").hidden = !on; $("abouttext").textContent = on ? "About: " + S.about : "";
}
$("aboutx").addEventListener("click", () => { S.aboutOff = S.about; paintAboutChip(); });
addEventListener("message", e => {
  if (e.origin !== location.origin || !e.data || e.data.studio !== "about") return;
  S.about = String(e.data.about || ""); paintAboutChip();
});
function withAbout(text) {
  const about = (S.about && S.aboutOff !== S.about) ? S.about.replace(/\]/g, ")").split(/\s+/).join(" ").slice(0, 200) : "";
  return about ? "[About: " + about + "]\n" + text : text;
}
addEventListener("message", e => {   // the project's page: open a session it names (a workflow step's)
  if (e.origin !== location.origin || !e.data || e.data.studio !== "session" || !e.data.id) return;
  tab("session"); if (keep.get("chat", "1") === "0") $("togglechat").click();
  follow(String(e.data.id));
});
addEventListener("message", async e => {   // the project's page asks to open another repository of its family
  if (e.origin !== location.origin || !e.data || e.data.studio !== "open") return;
  await loadProjects();
  let p = findProject(x => x.path === e.data.path);
  if (!p) { try { await api("api/projects", { path:e.data.path }); await loadProjects(); p = findProject(x => x.path === e.data.path); } catch (err) { $("projnote").textContent = err.message; } }
  if (p) openProject(p);
});

// ── settings, model, permissions ─────────────────────────────────────────────────────────────────────────────
function option(sel, value, label) { const o = el("option", label); o.value = value; sel.append(o); return o; }
async function loadChoices() {
  if (!S.project) return;
  try { S.choices = await api("api/choices?project=" + S.project); }
  catch (e) { S.choices = null; $("sstatus").textContent = "Could not load the session choices: " + e.message; return; }
  const c = S.choices, d = c.default || {};
  const st = $("settings"); st.replaceChildren();
  for (const p of c.settings) option(st, p.id, p.id === "claude-code" ? p.name : p.name + " · " + p.scope + (p.model ? " · " + p.model : "") + (p.provider ? " · " + p.provider : ""));
  st.value = d.settings_profile || "claude-code";
  const ml = $("models"); ml.replaceChildren(); for (const m of c.models) if (m) option(ml, m, m);
  $("model").value = d.model || "";
  const say = { default:"default: asks you before each action", acceptEdits:"acceptEdits: edits files without asking, asks for the rest",
                plan:"plan: plans only, changes nothing", dontAsk:"dontAsk: does only what is allowed, never asks",
                auto:"auto: a safety check decides" + (c.gateway ? " (not available through your gateway)" : ""),
                bypassPermissions:"bypassPermissions: never asks (dangerous)" };
  const md = $("mode"); md.replaceChildren(); for (const m of c.permission_modes) option(md, m, say[m] || m);
  md.value = d.permission_mode || "default"; paintMode();
  const ef = $("effort"); ef.replaceChildren(); for (const x of c.efforts || [""]) option(ef, x, x || "the settings' default");
  ef.value = d.effort || "";
  $("choicefrom").textContent = c.default_from === "project" ? "These choices: this project only" : c.default_from === "all" ? "These choices: all projects" : "";
  $("ownoff").hidden = c.default_from !== "project";
  const ag = $("agentpick"); ag.replaceChildren(); option(ag, "", "Claude (no saved agent)"); for (const a of c.agents || []) option(ag, a, a);
  paintSdk();
}
function paintOptLine() {
  const st = $("settings"), name = st.selectedOptions[0] ? st.selectedOptions[0].textContent.split(" · ")[0] : "";
  $("optline").textContent = " · " + [$("model").value || "default model", $("mode").value, $("effort").value ? "effort " + $("effort").value : "", name].filter(Boolean).join(" · ");
}
function paintMode() {
  $("mode").classList.toggle("danger", $("mode").value === "bypassPermissions"); paintOptLine();
  const old = $("autowarn"); if (old) old.remove();
  if ($("mode").value === "auto" && S.choices && S.choices.gateway) {   // its safety check is Anthropic's: not behind a gateway
    const n = el("div", "Auto mode needs Anthropic's safety check, which " + S.choices.gateway + " does not pass on: Claude would be refused every action, without asking you. Use default: it asks you here.", "notice");
    n.id = "autowarn"; $("opts").after(n);
  }
}
$("mode").addEventListener("change", () => paintMode());
for (const id of ["settings", "agentpick", "effort"]) { $(id).addEventListener("change", offerApply); $(id).addEventListener("change", paintOptLine); }
// the permission mode and the model change the open session at once (the others need a new one: Apply)
async function live(what) {
  const s = S.session;
  if (!s || !s.id || ["closed", "failed"].includes(s.status)) return;
  try { const h = await api("api/sessions/" + s.id + "/" + what, what === "mode" ? { mode:$("mode").value } : { model:$("model").value.trim() });
        S.session = { ...S.session, ...h }; paintStatus(); }
  catch (e) { $("sstatus").textContent = e.message; }
}
$("mode").addEventListener("change", () => live("mode"));
$("model").addEventListener("change", () => { paintOptLine(); live("model"); });
function choice() { return { project:S.project, settings_profile:$("settings").value, model:$("model").value.trim(),
                             permission_mode:$("mode").value, agent:$("agentpick").value, effort:$("effort").value }; }
function offerApply() { $("apply").hidden = !(S.session && S.session.claude_session && S.session.status !== "closed"); }
function offerFork() { $("fork").hidden = !(S.session && S.session.claude_session); }
function paintSdk() {
  const old = $("sdknote"); if (old) old.remove();
  if (S.choices && !S.choices.sdk) {
    const n = el("div", null, "notice"); n.id = "sdknote";
    n.append("Sessions run Claude Code through the Claude Agent SDK, which is not installed here. In a terminal: ");
    n.append(el("code", 'pip install -U "system-diff[app]"'), ", then restart system-diff app.");
    $("feed").before(n);
  }
}

// ── sessions ─────────────────────────────────────────────────────────────────────────────────────────────────
async function loadSessions() {
  if (!S.project) return;
  try { S.sessions = (await api("api/sessions?project=" + S.project)).sessions || []; }
  catch (e) { S.sessions = []; $("sstatus").textContent = "Could not load the sessions: " + e.message; }
  const sel = $("sesspick"); sel.replaceChildren();
  if (!S.sessions.length) option(sel, "", "none yet: New starts one");
  for (const s of S.sessions) option(sel, s.id, (s.title || "(nothing asked yet)").slice(0, 50) + " · " + s.status);
  const want = S.session && S.sessions.find(s => s.id === S.session.id) ? S.session.id : (S.sessions[0] || {}).id || "";
  sel.value = want; if (want) follow(want); else { S.session = null; S.polling++; paintPast(); paintStatus(); foldOptions(true); }
}
$("sesspick").addEventListener("change", () => { if ($("sesspick").value) follow($("sesspick").value); });
async function loadPast() {
  if (!S.project) return;
  try { S.past = (await api("api/past?project=" + S.project, undefined, 60000)).past || []; S.pastErr = ""; }
  catch (e) { S.past = []; S.pastErr = e.message; }
  if (!S.session || S.showingPast) paintPast();
}
function agoText(ms) {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 129600 ? Math.round(s / 3600) + " h ago"
       : new Date(ms).toLocaleDateString();
}
// the past conversations, like /resume: the ones the Studio had open when it stopped first, then the rest, searchable
function paintPast() {
  const f = $("feed"); f.replaceChildren(); S.cards = {}; S.streaming = null;
  const box = el("div", null, "past"), list = S.past || [];
  const top = el("div", null, "row"), nb = el("button", "New session", "primary"); nb.type = "button";
  nb.addEventListener("click", () => start());
  top.append(nb);
  if (S.showingPast && S.session && S.session.id) {
    const bk = el("button", "Back to the open session"); bk.type = "button";
    bk.addEventListener("click", () => { S.showingPast = false; follow(S.session.id); }); top.append(bk);
  }
  box.append(top);
  const item = (p, left) => {
    const row = el("div", null, "pitem" + (left ? " left" : ""));
    const label = (p.label || p.title || p.summary || p.first_prompt || "(no prompt)").replace(/\s+/g, " ");
    row.append(el("div", label.slice(0, 120), "pt"));
    if (p.last_prompt && p.last_prompt.replace(/\s+/g, " ") !== label)
      row.append(el("div", "Last: " + p.last_prompt.replace(/\s+/g, " ").slice(0, 140), "pl"));
    const size = p.size ? (p.size > 1048576 ? (p.size / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(p.size / 1024)) + " KB") : "";
    const meta = [p.modified ? agoText(p.modified) : "", p.messages ? (p.rough ? "~" : "") + p.messages + " messages" : "",
                  size, p.branch || "", String(p.id || "").slice(0, 8), left ? "open when the Studio stopped" : ""].filter(Boolean).join(" · ");
    row.append(el("div", meta, "pm"));
    const b = el("button", left ? "Continue" : "Resume", left ? "primary" : ""); b.type = "button";
    b.title = (p.first_prompt || "") + "\n" + p.id;
    b.addEventListener("click", () => { S.showingPast = false;
      start({ resume:p.id, note:left ? "Continued where you left off" : "Resumed a past conversation" }); });
    row.append(b); return row;
  };
  const left = list.filter(p => p.left_off), rest = list.filter(p => !p.left_off);
  if (left.length) { box.append(el("h3", "Continue where you left off")); for (const p of left) box.append(item(p, true)); }
  box.append(el("h3", "Past conversations"));
  if (S.pastErr) box.append(el("div", "Could not list them: " + S.pastErr, "ev err"));
  if (!rest.length && !S.pastErr) box.append(el("div", S.past ? "None yet in this project." : "Looking…", "ev meta"));
  if (rest.length) {
    const q = el("input"); q.type = "search"; q.placeholder = "Search " + rest.length + " conversations"; q.setAttribute("aria-label", "Search past conversations");
    const ul = el("div", null, "past");
    const fill = () => { const t = q.value.trim().toLowerCase(); ul.replaceChildren();
      for (const p of rest) if (!t || [p.label, p.title, p.summary, p.first_prompt, p.last_prompt, p.branch, p.id].join(" ").toLowerCase().includes(t)) ul.append(item(p, false)); };
    q.addEventListener("input", fill); fill(); box.append(q, ul);
  }
  f.append(box);
}
$("pastbtn").addEventListener("click", () => { S.showingPast = true; S.polling++; paintPast(); loadPast(); });
$("sessionsbtn").addEventListener("click", () => { tab("session"); $("optsbox").open = false; $("pastbtn").click(); });   // one click from anywhere
async function start(extra) {
  if (!S.project) { $("sstatus").textContent = "Pick a project first."; return null; }
  try { const s = await api("api/sessions", { ...choice(), ...(extra || {}) }); await loadSessions(); $("sesspick").value = s.id; follow(s.id); return s; }
  catch (e) { $("sstatus").textContent = e.message; return null; }
}
$("newsess").addEventListener("click", () => start());
$("apply").addEventListener("click", async () => {
  if (!S.session) return;
  try { const s = await api("api/sessions/" + S.session.id + "/switch", choice()); await loadSessions(); $("sesspick").value = s.id; follow(s.id); $("apply").hidden = true; }
  catch (e) { $("sstatus").textContent = e.message; }
});
$("forall").addEventListener("click", async () => {
  try { const d = await api("api/defaults/all", choice()); $("applyopen").hidden = !d.open_sessions; await loadChoices();
        $("sstatus").textContent = "Every project now starts with these choices." + (d.open_sessions ? " Open sessions: Apply to open sessions." : ""); }
  catch (e) { $("sstatus").textContent = e.message; }
});
$("applyopen").addEventListener("click", async () => {
  try { const d = await api("api/defaults/apply_open", {}); $("applyopen").hidden = true; $("sstatus").textContent = d.note; }
  catch (e) { $("sstatus").textContent = e.message; }
});
$("ownoff").addEventListener("click", async () => {
  try { await api("api/defaults/drop", { project:S.project }); await loadChoices(); } catch (e) { $("sstatus").textContent = e.message; }
});
$("fork").addEventListener("click", async () => {
  if (!S.session) return;
  try { const s = await api("api/sessions/" + S.session.id + "/fork", {}); await loadSessions(); $("sesspick").value = s.id; follow(s.id); }
  catch (e) { $("sstatus").textContent = e.message; }
});
function foldOptions(open) { $("optsbox").open = open; paintOptLine(); }
function follow(id) {
  foldOptions(false);   // a conversation: the options fold to one line above it
  S.polling++; const mine = S.polling;
  S.showingPast = false;
  S.session = { id }; S.next = 0; S.streaming = null; S.cards = {}; $("feed").replaceChildren();
  (async () => {
    while (mine === S.polling) {
      try {
        const d = await api("api/sessions/" + id + "/events?since=" + S.next + "&wait=25", undefined, 45000);
        if (mine !== S.polling) return;
        S.session = d.session; S.next = d.next; S.nowAt = Date.now();
        for (const ev of d.events) paint(ev);
        paintStatus();
      } catch (e) { if (mine !== S.polling) return; $("sstatus").textContent = e.message; await new Promise(r => setTimeout(r, 2000)); }
    }
  })();
}
function paintStatus() {
  const s = S.session, st = $("sstatus"); st.replaceChildren();
  if (!S.project) { st.textContent = "No project open: pick one on the left, or add a repository by its folder."; $("stop").disabled = true; return; }
  if (!s || !s.status) { st.textContent = "No session: New starts one with the settings above."; $("stop").disabled = true; return; }
  const busy = s.status === "working" || s.status === "waiting";
  if (busy) st.append(el("span", null, "pulse"));
  const now = s.now || {};
  if (busy && now.what) {
    const gone = Math.max(0, (Date.now() - (S.nowAt || Date.now())) / 1000);
    const turn = Math.round((now.turn_for || 0) + gone), quiet = Math.round((now.quiet_for || 0) + gone);
    if (now.agent) st.append(el("span", "Subagent " + now.agent.slice(0, 50) + " · ", "agent"));
    st.append(el("span", doingText(now, s.status), "doing"));
    st.append(" · " + clock(turn));
    if (quiet >= 60 && now.what !== "waiting") st.append(" · ", el("span", "no word from Claude Code for " + clock(quiet) + ": it may be waiting on the model or a slow tool (Interrupt stops the turn)", "quiet"));
  } else {
    st.append({ starting:"Starting Claude Code…", idle:"Ready", working:"Claude is working", waiting:"Claude asks you (above)",
                closed:"Closed: New starts another, or Resume this conversation", failed:"Could not start: " + (s.error || "") }[s.status] || s.status);
  }
  if (s.model) st.append(" · " + s.model);
  $("stop").disabled = !busy;
  // while Claude works, Enter adds the message to this turn (read at its next step); Interrupt & send starts anew
  $("send").textContent = busy ? "Add to turn" : "Send";
  $("send").title = busy ? "Claude reads it at its next step, without stopping (Enter)" : "";
  $("isend").hidden = !busy;
  offerApply(); offerFork();
}
function clock(sec) { return sec < 60 ? sec + " s" : Math.floor(sec / 60) + " min " + (sec % 60) + " s"; }
function toolName(n) { const p = String(n || "").split("__"); return n && n.startsWith("mcp__") && p.length > 2 ? p[1] + " › " + p.slice(2).join("__") : n; }
function doingText(now, status) {
  if (status === "waiting" || now.what === "waiting") return "Waiting for your answer (above)";
  if (now.what === "tool") return "Running " + toolName(now.tool) + (now.detail ? ": " + now.detail : "");
  if (now.what === "writing") return "Writing a reply";
  return "Thinking";
}
setInterval(() => { const s = S.session; if (s && (s.status === "working" || s.status === "waiting")) paintStatus(); }, 1000);
function scrolled(fn) { const f = $("feed"), end = f.scrollHeight - f.scrollTop - f.clientHeight < 60; fn(); if (end) f.scrollTop = f.scrollHeight; }
function toolView(v) {
  const box = el("div");
  if (v.file) box.append(el("div", v.file, "ev meta"));
  if (v.description) box.append(el("div", v.description));
  if (v.command) box.append(el("pre", v.command));
  for (const d of v.diff || []) { if (d.old) box.append(el("pre", d.old, "old")); if (d.new) box.append(el("pre", d.new, "new")); }
  if (v.input) box.append(el("pre", v.input));
  return box;
}
// ── Claude's replies, as Markdown: everything escaped first, only these tags made (no raw HTML, no script links) ──
/* md:start */
function mdEsc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;"); }
function mdLink(u, text) { return '<a href="' + mdEsc(u) + '" target="_blank" rel="noopener noreferrer">' + text + "</a>"; }
function mdInline(src) {
  const held = [], hold = html => "\u0000" + (held.push(html) - 1) + "\u0000";
  let s = String(src).replace(/\u0000/g, "");
  s = s.replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, (m, t, c) =>
    hold("<code>" + mdEsc(c.length > 2 && c[0] === " " && c[c.length - 1] === " " ? c.slice(1, -1) : c) + "</code>"));
  s = s.replace(/\[([^\]\n]+)\]\(\s*<?([^\s)>]+)>?(?:\s+"[^"\n]*")?\s*\)/g, (m, t, u) =>
    hold(/^(https?:\/\/|mailto:)/i.test(u) ? mdLink(u, mdInline(t)) : mdEsc(m)));   // javascript:, data:: shown as text
  s = s.replace(/<((?:https?:\/\/|mailto:)[^\s>\u0000]+)>/gi, (m, u) => hold(mdLink(u, mdEsc(u))));
  s = s.replace(/\bhttps?:\/\/[^\s<>"'\u0000]*[^\s<>"'\u0000.,;:!?)\]*_~]/gi, u => hold(mdLink(u, mdEsc(u))));
  s = mdEsc(s)
    .replace(/\*\*\*(?=\S)([\s\S]*?\S)\*\*\*/g, "<strong><em>$1</em></strong>")
    .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, "$1<strong>$2</strong>")
    .replace(/\*([^\s*](?:[^*]*?[^\s*])?)\*/g, "<em>$1</em>")
    .replace(/(^|[^\w])_([^\s_](?:[^_]*?[^\s_])?)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/~~(?=\S)([\s\S]*?\S)~~/g, "<del>$1</del>")
    .replace(/\n/g, "<br>");
  return s.replace(/\u0000(\d+)\u0000/g, (m, i) => held[+i]);
}
const MD = {
  fence: l => /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(l),
  hr: l => /^ {0,3}([-*_])(?:[ ]*\1){2,}[ ]*$/.test(l),
  head: l => /^ {0,3}(#{1,6})(?:[ ]+(.*?))?[ ]*#*[ ]*$/.exec(l),
  item: l => /^([ ]*)([-*+]|\d{1,9}[.)])[ ]+(.*)$/.exec(l),
  quote: l => /^ {0,3}>/.test(l),
  delim: l => l.includes("-") && /^[ ]*\|?[ ]*:?-+:?[ ]*(\|[ ]*:?-+:?[ ]*)*\|?[ ]*$/.test(l),
  lead: l => l.length - l.replace(/^ +/, "").length,
};
MD.table = (l, next) => l.includes("|") && next !== undefined && next.includes("|") && MD.delim(next);
MD.starts = (l, next) => !!(MD.fence(l) || MD.hr(l) || MD.head(l) || MD.item(l) || MD.quote(l) || MD.table(l, next));
function mdCells(l) {
  const out = []; let cur = "", code = false; const t = l.trim().replace(/^\|/, "").replace(/\|$/, "");
  for (let i = 0; i < t.length; i++) {
    const c = t[i];
    if (c === "\\" && t[i + 1] === "|") { cur += "|"; i++; continue; }
    if (c === "`") code = !code;
    if (c === "|" && !code) { out.push(cur.trim()); cur = ""; continue; }
    cur += c;
  }
  out.push(cur.trim()); return out;
}
function mdList(lines, i) {
  const first = MD.item(lines[i]), ind = first[1].length, ordered = /\d/.test(first[2]);
  const items = []; let loose = false;
  while (i < lines.length) {
    if (!lines[i].trim()) {   // a blank line between two items of this list: it goes on, loosely
      let j = i; while (j < lines.length && !lines[j].trim()) j++;
      const n = j < lines.length && MD.item(lines[j]);
      if (n && n[1].length === ind && /\d/.test(n[2]) === ordered) { loose = true; i = j; } else break;
    }
    const m = MD.item(lines[i]);
    if (!m || m[1].length !== ind || /\d/.test(m[2]) !== ordered) break;
    const inner = m[0].length - m[3].length, body = [m[3]]; i++;
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) {
        let j = i; while (j < lines.length && !lines[j].trim()) j++;
        if (j < lines.length && MD.lead(lines[j]) > ind) { for (; i < j; i++) body.push(""); loose = true; continue; }
        break;
      }
      const li = MD.lead(l);
      if (li > ind) { body.push(l.slice(Math.min(li, inner))); i++; continue; }
      if (!MD.starts(l, lines[i + 1]) && body[body.length - 1].trim()) { body.push(l.trim()); i++; continue; }   // lazy continuation
      break;
    }
    items.push({ body, num:m[2] });
  }
  const start = ordered ? parseInt(items[0].num, 10) : 1, tag = ordered ? "ol" : "ul";
  let html = "<" + tag + (ordered && start !== 1 ? ' start="' + start + '"' : "") + ">";
  for (const it of items) {
    const task = /^\[([ xX])\][ ]+/.exec(it.body[0]);
    if (task) it.body[0] = it.body[0].slice(task[0].length);
    let inner = mdBlocks(it.body);
    if (!loose) inner = inner.replace(/^<p>([\s\S]*?)<\/p>/, "$1");
    html += task ? '<li class="task"><input type="checkbox" disabled' + (task[1] === " " ? "" : " checked") + ">" + inner + "</li>" : "<li>" + inner + "</li>";
  }
  return { html:html + "</" + tag + ">", i };
}
function mdBlocks(lines) {
  let out = "", i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (!l.trim()) { i++; continue; }
    let m;
    if ((m = MD.fence(l))) {
      const mark = m[1], pad = MD.lead(l), lang = (m[2].trim().split(/\s+/)[0] || "").replace(/[^\w+#.-]/g, "").slice(0, 24), code = [];
      const close = new RegExp("^ {0,3}" + (mark[0] === "`" ? "`" : "~") + "{" + mark.length + ",}[ ]*$");
      i++;
      while (i < lines.length && !close.test(lines[i])) { code.push(lines[i].slice(Math.min(pad, MD.lead(lines[i])))); i++; }
      i++;   // the closing fence (none yet, while it streams: the block runs to the end)
      out += '<div class="mdcode"><div class="mdbar"><span>' + mdEsc(lang || "text") + '</span><button type="button" class="mdcopy">Copy</button></div><pre><code>' + mdEsc(code.join("\n")) + "</code></pre></div>";
      continue;
    }
    if ((m = MD.head(l))) { const n = m[1].length; out += "<h" + n + ">" + mdInline(m[2] || "") + "</h" + n + ">"; i++; continue; }
    if (MD.hr(l)) { out += "<hr>"; i++; continue; }
    if (MD.quote(l)) {
      const q = []; while (i < lines.length && MD.quote(lines[i])) { q.push(lines[i].replace(/^ {0,3}> ?/, "")); i++; }
      out += "<blockquote>" + mdBlocks(q) + "</blockquote>"; continue;
    }
    if (MD.table(l, lines[i + 1])) {
      const head = mdCells(l), align = mdCells(lines[i + 1]).map(c => /^:-+:$/.test(c) ? "center" : /-:$/.test(c) ? "right" : /^:-/.test(c) ? "left" : "");
      const cell = (tag, c, k) => "<" + tag + (align[k] ? ' style="text-align:' + align[k] + '"' : "") + ">" + mdInline(c) + "</" + tag + ">";
      let t = '<div class="mdtable"><table><thead><tr>' + head.map((c, k) => cell("th", c, k)).join("") + "</tr></thead><tbody>";
      i += 2;
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        const row = mdCells(lines[i]); t += "<tr>" + head.map((h, k) => cell("td", row[k] || "", k)).join("") + "</tr>"; i++;
      }
      out += t + "</tbody></table></div>"; continue;
    }
    if (MD.item(l)) { const got = mdList(lines, i); out += got.html; i = got.i; continue; }
    const para = [l.trim()]; i++;
    while (i < lines.length && lines[i].trim() && !MD.starts(lines[i], lines[i + 1])) { para.push(lines[i].trim()); i++; }
    out += "<p>" + mdInline(para.join("\n")) + "</p>";
  }
  return out;
}
function md(text) { return mdBlocks(String(text || "").replace(/\r\n?/g, "\n").replace(/\t/g, "    ").split("\n")); }
/* md:end */
function mdPaint(box, text) { box.innerHTML = md(text); }   // md() escapes everything it is given
$("feed").addEventListener("click", e => {   // a code block's Copy
  const b = e.target.closest && e.target.closest(".mdcopy"); if (!b) return;
  const pre = b.closest(".mdcode").querySelector("pre"), text = pre.textContent;
  const done = () => { b.textContent = "Copied"; setTimeout(() => { b.textContent = "Copy"; }, 1400); };
  const pick = () => { const r = document.createRange(); r.selectNodeContents(pre); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); b.textContent = "Selected: Ctrl+C"; };
  try { navigator.clipboard.writeText(text).then(done, pick); } catch (err) { pick(); }
});

// ── the message box's history: Up and Down walk the messages sent in this project, as in Claude Code ──
const HIST = { mem:{}, pos:-1, draft:"", key:"" };
// a message that looks like it holds a key is remembered for this page only, never written to the browser's storage
const KEYLIKE = /\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|AKIA[0-9A-Z]{16}|xox[abprs]-[\w-]{10,}|sk-[\w-]{20,}|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,})|(?<![a-z])(pat|token|secret|password|passwd|api[_-]?key|key)(?![a-z])[^\n]{0,24}?[:=]\s*\S{12,}/i;
function histKey() { return "hist:" + (S.project || ""); }
function histList() {
  const k = histKey();
  if (!HIST.mem[k]) { let got = []; try { got = JSON.parse(keep.get(k, "[]")); } catch (e) { got = []; }
                      HIST.mem[k] = Array.isArray(got) ? got.filter(x => typeof x === "string" && x) : []; }
  return HIST.mem[k];
}
function histPush(text) {
  const list = histList();
  if (text && list[list.length - 1] !== text) list.push(text);
  while (list.length > 200) list.shift();
  keep.set(histKey(), JSON.stringify(list.filter(x => !KEYLIKE.test(x) && !(typeof findKeys === "function" && findKeys(x).length))));
  HIST.pos = -1; HIST.draft = "";
}
function histStep(dir) {   // true when it moved (the key is then not the caret's)
  const ta = $("msg"), v = ta.value, at = ta.selectionStart;
  if (at !== ta.selectionEnd) return false;
  if (dir < 0 ? at > 0 && v.lastIndexOf("\n", at - 1) !== -1 : v.indexOf("\n", at) !== -1) return false;   // not on the first (last) line
  if (HIST.key !== histKey()) { HIST.key = histKey(); HIST.pos = -1; HIST.draft = ""; }
  const list = histList();
  if (!list.length) {   // nothing sent from here yet: this session's own messages (a resumed one)
    for (const u of document.querySelectorAll("#feed .ev.user")) {
      const t = (u.querySelector(".qtext") || u).textContent.replace(/^\[About: [^\]\n]*\]\n/, "");
      if (t && list[list.length - 1] !== t) list.push(t);
    }
  }
  if (!list.length) return false;
  if (HIST.pos === -1) { if (dir > 0) return false; HIST.draft = v; HIST.pos = list.length; }
  const to = HIST.pos + dir;
  if (to < 0) return true;
  if (to >= list.length) { HIST.pos = -1; ta.value = HIST.draft; } else { HIST.pos = to; ta.value = list[to]; }
  ta.setSelectionRange(ta.value.length, ta.value.length);
  return true;
}
function paintHistory(ev, before) {   // a resumed conversation's past entries, above what it says next
  const f = $("feed"), box = el("div", null, "hist");
  for (const e of ev.entries || []) paint(e, box);
  const cid = ev.claude_session || (S.session && S.session.claude_session) || "";
  if (ev.earlier > 0) {
    const more = el("button", "Show earlier (" + ev.earlier + ")"); more.type = "button";
    more.addEventListener("click", async () => {
      more.disabled = true;
      try { const d = await api("api/past/" + encodeURIComponent(cid) + "/history?project=" + S.project + "&before=" + ((ev.entries[0] || {}).i || 0));
            paintHistory({ ...d, claude_session:cid }, box); more.remove(); }
      catch (e) { more.disabled = false; more.textContent = "Could not load: " + e.message; }
    });
    box.prepend(more);
  }
  if (before) before.before(box);
  else { box.append(el("div", "resumed here", "histsep")); f.append(box); f.scrollTop = f.scrollHeight; }
}
function settleTools(f, why) {   // the turn ended: a call still marked running did not say how it went
  for (const sp of f.querySelectorAll(".spin.run")) { sp.className = "spin"; sp.textContent = why; }
}
function paint(ev, into) {
  if (ev.type === "state") { if (S.session) { S.session.now = ev.now; S.nowAt = Date.now(); } return; }
  if (ev.type === "history" && !into) { paintHistory(ev); return; }
  scrolled(() => {
    const f = into || $("feed");
    if (ev.type === "delta") {
      if (!S.streaming) { S.streaming = el("div", "", "ev text md"); S.streaming.raw = ""; f.append(S.streaming); }
      const box = S.streaming; box.raw += ev.text;
      if (!box.queued) { box.queued = true; requestAnimationFrame(() => { box.queued = false; if (box.isConnected) scrolled(() => mdPaint(box, box.raw)); }); }
      return;
    }
    if (ev.type === "text") { if (S.streaming) { S.streaming.remove(); S.streaming = null; } const d = el("div", null, "ev text md"); mdPaint(d, ev.text); f.append(d); return; }
    if (S.streaming && ev.type !== "status") { S.streaming.remove(); S.streaming = null; }
    if (ev.type === "user") f.append(ev.queued ? queuedBubble(ev) : el("div", ev.text, "ev user"));
    else if (ev.type === "delivered") qState(f, ev.id, ev.next_turn ? "read: it starts Claude's next turn" : "read by Claude", true);
    else if (ev.type === "queued_edit") { const b = qBubble(f, ev.id); if (b) b.querySelector(".qtext").textContent = ev.text; }
    else if (ev.type === "queued_withdrawn") { const b = qBubble(f, ev.id); if (b) b.classList.add("withdrawn"); qState(f, ev.id, "withdrawn", true); }
    else if (ev.type === "handed") for (const id of ev.ids || []) qState(f, id, "→ handed to " + ev.to, true);
    else if (ev.type === "replied") {
      const texts = f.querySelectorAll(".ev.text"), last = texts[texts.length - 1];
      for (const id of ev.ids || []) {
        const b = qBubble(f, id); if (!b) continue;
        const was = b.querySelector(".qstate").textContent;
        qState(f, id, (was.startsWith("→") ? was + " · " : "") + "answered below ↓", true);
        if (last) { const a = el("button", "↩ about: “" + b.querySelector(".qtext").textContent.slice(0, 50) + "”", "aboutq"); a.type = "button";
                    a.addEventListener("click", () => b.scrollIntoView({ block:"center", behavior:"smooth" })); last.prepend(a); }
      }
    }
    else if (ev.type === "thinking") { const d = el("details", null, "ev tool"); d.append(el("summary", "Thinking")); d.append(el("pre", ev.text)); f.append(d); }
    else if (ev.type === "tool_use") {
      const d = el("details", null, "ev tool"); const s = el("summary");
      s.append(el("span", toolName(ev.name), "nm"), " " + (ev.label || ev.view.file || ev.view.command || "").slice(0, 90));
      if (ev.agent) s.title = "by the subagent " + ev.agent;
      if (!ev.past) s.append(el("span", "running", "spin run"));
      d.append(s, toolView(ev.view)); d.dataset.tid = ev.id; f.append(d);
    }
    else if (ev.type === "tool_result") {
      const t = f.querySelector('[data-tid="' + CSS.escape(ev.id) + '"]'); const pre = el("pre", ev.text || "(no output)"); if (ev.error) pre.className = "old"; (t || f).append(pre);
      const sp = t && t.querySelector(".spin");
      if (sp) { sp.className = "spin " + (ev.error ? "bad" : "ok"); sp.textContent = (ev.error ? "✗ failed" : "✓") + (ev.ms != null ? " " + (ev.ms < 1000 ? ev.ms + " ms" : (ev.ms / 1000).toFixed(1) + " s") : ""); }
    }
    else if (ev.type === "permission") f.append(askCard(ev));
    else if (ev.type === "permission_done") { const c = S.cards[ev.request]; if (c) { c.classList.add("done"); const a = c.querySelector(".acts"); if (a) a.replaceChildren(el("span", ev.allow ? (ev.always ? "Allowed always" : "Allowed") : "Denied" + (ev.message ? ": " + ev.message : ""))); } }
    else if (ev.type === "result") {
      const u = ev.usage || {}, toks = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
      f.append(el("div", [ev.model || "", toks ? toks.toLocaleString() + " in" : "", u.output_tokens ? u.output_tokens.toLocaleString() + " out" : "",
                          ev.cost != null ? "$" + Number(ev.cost).toFixed(4) : "", ev.terminal_reason || "", ev.error ? "ended with an error" : ""].filter(Boolean).join(" · "), "ev meta"));
    }
    else if (ev.type === "error") f.append(el("div", ev.text, "ev err"));
    else if (ev.type === "note") f.append(el("div", ev.text, "ev meta"));
    else if (ev.type === "interrupt") { settleTools(f, "interrupted"); f.append(el("div", "Interrupted", "ev meta")); }
    else if (ev.type === "status" && (ev.status === "idle" || ev.status === "closed")) settleTools(f, "no result");
    else if (ev.type === "system" && ev.subtype === "init") {
      f.append(el("div", "Claude Code started · " + (ev.model || "") + " · permissions: " + (ev.permission_mode || "default") + " · " + ev.tools + " tools" + ((ev.mcp || []).length ? " · MCP: " + ev.mcp.join(", ") : ""), "ev meta"));
      if (ev.permission_mode === "auto" && S.choices && S.choices.gateway) {   // every action would be refused: say so, offer the way out
        const n = el("div", null, "card"), b = el("button", "Ask me instead", "primary"); b.type = "button";
        n.append(el("div", "This session runs in auto mode, whose safety check " + S.choices.gateway + " does not pass on: Claude will be refused every action.", "q"));
        b.addEventListener("click", async () => { try { await api("api/sessions/" + S.session.id + "/mode", { mode:"default" }); n.replaceChildren(el("div", "Now asking you: each action Claude wants comes here as a card.", "q")); } catch (e) { alert(e.message); } });
        n.append(b); f.append(n);
      }
    }
    else if (ev.type === "system" && ev.text) f.append(el("div", ev.text, "ev meta"));
    else if (ev.type === "start" && ev.resume) f.append(el("div", "Resuming conversation " + ev.resume, "ev meta"));
    else if (ev.type === "status" && ev.status === "failed") f.append(el("div", "Could not start Claude Code: " + (ev.error || ""), "ev err"));
  });
}
function askCard(ev) {
  const c = el("div", null, "card"); S.cards[ev.request] = c;
  c.append(el("div", "Claude wants to use " + ev.tool, "q"));
  if (ev.why) c.append(el("div", ev.why, "ev meta"));
  c.append(toolView(ev.view));
  const acts = el("div", null, "acts"), msg = el("input"); msg.type = "text"; msg.placeholder = "why not (optional)"; msg.setAttribute("aria-label", "Why not");
  const answer = (allow, always) => api("api/sessions/" + S.session.id + "/permission", { request:ev.request, allow, always, message:msg.value }).catch(e => { $("sstatus").textContent = e.message; });
  const yes = el("button", "Allow", "primary"); yes.type = "button"; yes.addEventListener("click", () => answer(true, false));
  acts.append(yes);
  if (ev.can_always) { const al = el("button", "Allow always"); al.type = "button"; al.title = "Allow this kind of call for the rest of the session"; al.addEventListener("click", () => answer(true, true)); acts.append(al); }
  const no = el("button", "Deny", "danger"); no.type = "button"; no.addEventListener("click", () => answer(false, false));
  acts.append(no, msg); c.append(acts);
  return c;
}
// A message written while Claude works: held, then read at Claude's next step (a tool's result); until then it can be
// changed or withdrawn. Its line says where it is: queued, read, handed to a subagent, answered.
function qBubble(f, id) { return (f || $("feed")).querySelector('.ev.user[data-q="' + CSS.escape(id) + '"]'); }
function qState(f, id, text, settled) {
  const b = qBubble(f, id); if (!b) return;
  b.querySelector(".qstate").textContent = text;
  if (settled) { b.classList.remove("waitq"); const a = b.querySelector(".qacts"); if (a) a.remove(); }
}
function queuedBubble(ev) {
  const b = el("div", null, "ev user waitq"); b.dataset.q = ev.queued;
  b.append(el("div", ev.text, "qtext"), el("div", "queued: Claude reads it at its next step", "qstate"));
  if (ev.past) return b;
  const acts = el("div", null, "qacts"), ed = el("button", "Edit"), wd = el("button", "Withdraw");
  ed.type = wd.type = "button";
  const sid = S.session && S.session.id;
  ed.addEventListener("click", () => {
    const box = el("textarea"); box.value = b.querySelector(".qtext").textContent; box.rows = 3;
    const ok = el("button", "Save"), no = el("button", "Cancel"); ok.type = no.type = "button";
    const form = el("div", null, "qedit"); form.append(box, ok, no); acts.replaceWith(form); box.focus();
    const done = () => form.replaceWith(acts);
    no.addEventListener("click", done);
    ok.addEventListener("click", async () => {
      try { await api("api/sessions/" + sid + "/queued", { id:ev.queued, text:box.value }); done(); }
      catch (e) { form.replaceWith(el("div", e.message, "qstate")); }
    });
  });
  wd.addEventListener("click", async () => {
    try { await api("api/sessions/" + sid + "/queued", { id:ev.queued, withdraw:true }); }
    catch (e) { b.querySelector(".qstate").textContent = e.message; }
  });
  acts.append(ed, wd); b.append(acts);
  return b;
}
async function send(interrupt) {
  const text = $("msg").value.trim(); if (!text) return;
  if (keysFirst(text)) return;   // a key in the message: offered to the key store first (keys, below)
  let s = S.session && S.session.status && !["closed", "failed"].includes(S.session.status) ? S.session : await start();
  if (!s) return;
  try { await api("api/sessions/" + s.id + "/send", { text:withAbout(text), interrupt:interrupt === true }); $("msg").value = ""; } catch (e) { $("sstatus").textContent = e.message; }
  histPush(text);   // Up brings it back
}
$("send").addEventListener("click", () => send(false));
$("isend").addEventListener("click", () => send(true));
$("msg").addEventListener("keydown", e => {
  if (e.isComposing) return;
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); return; }
  if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey && histStep(e.key === "ArrowUp" ? -1 : 1)) e.preventDefault();
});
$("stop").addEventListener("click", () => { if (S.session) api("api/sessions/" + S.session.id + "/interrupt", {}).catch(e => { $("sstatus").textContent = e.message; }); });
$("closesess").addEventListener("click", async () => { if (S.session && S.session.id) { await api("api/sessions/" + S.session.id + "/close", {}).catch(() => {}); loadSessions(); } });

// ── keys: kept by the operating system, given to sessions as environment variables, never sent in a message ──────
/*keys-detect:start*/
const KEY_NAME = /^(?=.*(key|token|pat|secret|passw|pwd|api[_-]?key|credential))/i;
const PLACEHOLDER = /^(x+|\*+|\.+|<.*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|your[_-].*|changeme|example.*|placeholder|redacted|null|none|true|false|undefined)$/i;
const KNOWN = [
  [/\b(gh[pousr]_[A-Za-z0-9]{36,255})\b/g, "GITHUB_TOKEN", "a GitHub token"],
  [/\b(github_pat_[A-Za-z0-9_]{22,255})\b/g, "GITHUB_TOKEN", "a GitHub token"],
  [/\b((?:AKIA|ASIA)[0-9A-Z]{16})\b/g, "AWS_ACCESS_KEY_ID", "an AWS access key"],
  [/\b(xox[abposr]-[A-Za-z0-9-]{10,})\b/g, "SLACK_TOKEN", "a Slack token"],
  [/\b(sk-ant-[A-Za-z0-9_-]{20,})\b/g, "CLAUDE_API_KEY", "an Anthropic API key"],
  [/\b(sk-(?!ant-)[A-Za-z0-9_-]{20,})\b/g, "OPENAI_API_KEY", "an OpenAI API key"],
  [/\b(AIza[0-9A-Za-z_-]{35})\b/g, "GOOGLE_API_KEY", "a Google API key"],
  [/\b(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b/g, "JWT", "a JSON web token"],
  [/\b([a-z2-7]{52})\b/g, "AZURE_DEVOPS_EXT_PAT", "an Azure DevOps personal access token"],
  [/\b([A-Za-z0-9]{76}AZDO[A-Za-z0-9]{4})\b/g, "AZURE_DEVOPS_EXT_PAT", "an Azure DevOps personal access token"],
  [/(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)/g, "PRIVATE_KEY", "a private key"],
];
function keyEntropy(s) { const n = {}; for (const c of s) n[c] = (n[c] || 0) + 1; let h = 0; for (const c in n) { const p = n[c] / s.length; h -= p * Math.log2(p); } return h; }
function looksRandom(v) {
  if (v.length < 32 || /^[0-9a-f]+$/i.test(v) && v.length <= 64) return false;          // a commit or a checksum
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v)) return false;   // an id
  const kinds = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter(r => r.test(v)).length;
  return kinds >= 3 && keyEntropy(v) >= 3.5;
}
function keyName(text) { return String(text || "").toUpperCase().replace(/[^A-Z0-9_]+/g, "_").replace(/^([0-9])/, "_$1").replace(/_+$/, "").slice(0, 64) || "SECRET"; }
function findKeys(text) {   // [{value, start, name, what}]: what in this text looks like a key, longest first, no overlaps
  const out = [], taken = [];
  const add = (start, value, name, what) => {
    const end = start + value.length;
    if (taken.some(([a, b]) => start < b && a < end) || PLACEHOLDER.test(value)) return;
    taken.push([start, end]); out.push({ value, start, name, what });
  };
  for (const [re, name, what] of KNOWN) for (const m of text.matchAll(re)) add(m.index + m[0].indexOf(m[1]), m[1], name, what);
  const assign = /(^|[\s,;{(])["']?([A-Za-z_][A-Za-z0-9_.-]{0,63})["']?\s*[:=]\s*["']?([^\s"',;]{8,})["']?/g;
  for (const m of text.matchAll(assign)) {
    if (!KEY_NAME.test(m[2]) || /^\$|^https?:/i.test(m[3])) continue;
    const after = text.slice(m.index + m[0].length);
    if (/^\s*[:=]/.test(after) || (m[3].length < 20 && !/[0-9]|[^A-Za-z0-9_]/.test(m[3]))) continue;   // a name, or a word
    const at = m.index + m[0].lastIndexOf(m[3]);
    const known = out.find(k => k.start === at);
    if (known) { if (known.name !== "AZURE_DEVOPS_EXT_PAT") known.name = keyName(m[2]); continue; }   // its own name
    const ado = /azure|ado|devops/i.test(m[2]) && /pat|token/i.test(m[2]);
    const pw = /^(passwords?|passwd|pwd|pass(?:phrase)?)$/i.test(m[2]);   // a bare "password": the server's
    add(at, m[3], ado ? "AZURE_DEVOPS_EXT_PAT" : pw ? serverPrefix(text) + "_PASSWORD" : keyName(m[2]),
        ado ? "an Azure DevOps personal access token" : pw ? "a password" : "a key");
  }
  for (const m of text.matchAll(/\b(passwords?|passwd|pwd|pass(?:phrase)?)(?:\s+is)?\s*[:=]?\s+(\S{4,})/gi)) {   // "password Xyz!23"
    const v = m[2].replace(/[,.;:)\]]+$/, "");
    if (/^\$/.test(v) || v.length < 4 || (!/[:=]/.test(m[0]) && /^[A-Za-z]+$/.test(v))) continue;   // a word, not a password
    add(m.index + m[0].lastIndexOf(m[2]), v, serverPrefix(text) + "_PASSWORD", "a password");
  }
  for (const m of text.matchAll(/[A-Za-z0-9+\/=_-]{32,}/g)) if (looksRandom(m[0])) add(m.index, m[0], "SECRET", "a long random string");
  return out.sort((a, b) => a.start - b.start);
}
function serverPrefix(text) {   // "staging server 10.1.2.3" → STAGING; else SERVER
  const m = /\b([A-Za-z][A-Za-z0-9-]{1,20})\s+(?:server|host|box|machine|vm)\b/i.exec(text);
  const w = m && !/^(the|a|an|my|our|this|that|to|on|of|in|for|with|and|test)$/i.test(m[1]) ? m[1] : "server";
  return keyName(w);
}
function findSettings(text) {   // [{value, start, name, what}]: the host, user and port beside a password (not secret)
  const out = [], pre = serverPrefix(text), seen = new Set();
  const pats = [[/\b(?:server|host|ip|address)(?:\s+is)?\s*[:=]?\s*((?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)/gi, "_HOST", "its host"],
                [/\b(?:user(?:name)?|login)(?:\s+is)?\s*[:=]?\s*([A-Za-z0-9._-]{2,64})/gi, "_USER", "its user"],
                [/\bport(?:\s+is)?\s*[:=]?\s*(\d{2,5})\b/gi, "_PORT", "its port"]];
  for (const [re, suffix, what] of pats) for (const m of text.matchAll(re)) {
    if (seen.has(suffix) || /^(is|the|name)$/i.test(m[1])) continue;
    seen.add(suffix); out.push({ value:m[1], start:m.index + m[0].lastIndexOf(m[1]), name:pre + suffix, what, setting:true });
  }
  if (!out.some(x => x.name.endsWith("_HOST"))) {   // a bare IP address
    const ip = /\b((?:\d{1,3}\.){3}\d{1,3})\b/.exec(text); if (ip) out.push({ value:ip[1], start:ip.index, name:pre + "_HOST", what:"its host", setting:true });
  }
  return out.sort((a, b) => a.start - b.start);
}
/*keys-detect:end*/
S.keysSent = "";   // the text the person chose to send as it is
S.keysAll = false;   // the Keys list: this project's, or every project's
async function loadKeys() {
  const box = $("keys"); if (!box) return;
  if (!S.project && !S.keysAll) { box.replaceChildren(el("span", "Pick a project first.", "ev meta")); return; }
  let d; try { d = await api(S.keysAll ? "api/secrets?all=1" : "api/secrets?project=" + S.project); } catch (e) { box.replaceChildren(el("span", e.message, "ev meta")); return; }
  box.replaceChildren();
  const sw = el("button", S.keysAll ? "This project only" : "All projects", "switch"); sw.type = "button"; sw.style.justifySelf = "end";
  sw.title = S.keysAll ? "Back to this project's settings and secrets" : "Every project's settings and secrets (a secret's value is never shown)";
  sw.addEventListener("click", () => { S.keysAll = !S.keysAll; loadKeys(); }); box.append(sw);
  let lastGroup = null;
  const keys = (d.keys || []).slice().sort((a, b) => ((a.project || "") + a.group + a.name).localeCompare((b.project || "") + b.group + b.name));
  for (const k of keys) {
    const head = (S.keysAll ? (k.project ? k.project.split(/[\\/]/).pop() : "all projects") + " · " : "") + (k.group || "");
    if (head !== lastGroup) { if (head) box.append(el("div", head, "ev meta")); lastGroup = head; }
    const r = el("div", null, "k");
    r.append(el("code", k.name), el("span", (k.kind === "setting" ? "= " + k.value + " · " : "secret · ") + [S.keysAll ? "" : k.scope, k.description].filter(Boolean).join(" · "), "s"));
    const scopeOf = k.scope === "all projects" ? "all" : "project", proj = S.keysAll ? (k.project || S.project || "") : S.project;
    if (!S.keysAll || k.project === "" || proj) {
      const ed = el("button", "Edit"); ed.type = "button"; ed.title = k.kind === "setting" ? "Change its value or what it is for" : "Replace its value (the old one is never shown) or change what it is for";
      ed.addEventListener("click", () => r.replaceWith(editForm(k, proj, scopeOf)));
      const f = el("button", "Forget", "danger"); f.type = "button"; f.title = "Remove it" + (k.kind === "setting" ? "" : " from " + k.kept_in);
      f.addEventListener("click", async () => {
        if (!confirm("Forget " + k.name + " (" + (k.scope || "") + ")? Sessions started from now on go without it.")) return;
        try { await api("api/secrets/forget", { project:proj, name:k.name, scope:scopeOf }); loadKeys(); } catch (e) { alert(e.message); }
      });
      r.append(ed, f);
    }
    box.append(r);
  }
  if (!keys.length) box.append(el("span", "None: a token your agents need (an Azure DevOps PAT), a server's password, or a setting such as its host is kept here, not in the chat.", "ev meta"));
  paintAsks(d.asks || []);
  if (S.keysAll) { box.append(el("span", "Secrets kept in " + d.store + ". A secret's value is never shown.", "ev meta")); return; }
  const add = el("button", "Add…"); add.type = "button"; add.style.justifySelf = "start"; add.title = "A secret (kept by your system) or a setting (a host, a user: shown)";
  add.addEventListener("click", () => { add.replaceWith(keyForm({}, async got => { await loadKeys(); offerRestart(got); })); });
  box.append(add, el("span", "Kept in " + d.store + ". Sessions get them as environment variables; Claude sees a secret's name only, a setting's value too.", "ev meta"));
}
function editForm(k, proj, scopeOf) {   // change a setting's value, replace a secret's, or what it is for
  const f = el("form", null, "k"), value = el("input"), desc = el("input"), group = el("input");
  value.type = k.kind === "setting" ? "text" : "password"; value.value = k.kind === "setting" ? k.value : "";
  value.placeholder = k.kind === "setting" ? "its value" : "a new value (empty: keep the old one)"; value.autocomplete = "off"; value.setAttribute("aria-label", "Value of " + k.name);
  desc.type = "text"; desc.value = k.description || ""; desc.placeholder = "what it is for"; desc.setAttribute("aria-label", "Description");
  group.type = "text"; group.value = k.group || ""; group.placeholder = "group (staging server)"; group.setAttribute("aria-label", "Group");
  const ok = el("button", "Save", "primary"), no = el("button", "Cancel"); no.type = "button";
  f.append(el("code", k.name), value, group, desc, ok, no);
  no.addEventListener("click", loadKeys);
  f.addEventListener("submit", async e => {
    e.preventDefault();
    try { const got = await api("api/secrets/update", { project:proj, name:k.name, scope:scopeOf, value:value.value, description:desc.value.trim(), group:group.value.trim() });
          value.value = ""; await loadKeys(); if (got.open_sessions) offerRestart(got); }
    catch (err) { alert(err.message); }
  });
  return f;
}
function paintAsks(asks) {   // Claude asked for a secret's value (ask_for_secret): a password field above the message box
  const box = $("keyask"); if (!box || (box.childNodes.length && !box.dataset.asks)) return;   // a paste card is open
  box.replaceChildren(); box.dataset.asks = asks.length ? "1" : ""; box.hidden = !asks.length;
  for (const a of asks) {
    const c = el("div", null, "card");
    c.append(el("div", "Claude asks for " + a.name + (a.description ? ": " + a.description : "") + (a.group ? " (" + a.group + ")" : ""), "q"),
             el("div", "Type it here, not in the chat: it is kept by your system and given to sessions as $" + a.name + "; Claude never sees it.", "ev meta"));
    c.append(keyForm({ name:a.name, what:a.description, group:a.group, scope:a.scope, ask:a.id }, async got => { box.dataset.asks = ""; await loadKeys(); offerRestart(got); },
                     async () => { await api("api/secrets/dismiss", { ask:a.id }).catch(() => {}); box.dataset.asks = ""; loadKeys(); }));
    box.append(c);
  }
}
setInterval(() => { if (S.project && !document.hidden && $("keyask") && !($("keyask").childNodes.length && !$("keyask").dataset.asks))
  api("api/secrets?project=" + S.project).then(d => paintAsks(d.asks || [])).catch(() => {}); }, 5000);
function keyForm(k, done, cancel) {   // name, kind, value (hidden for a secret), group, for this project or all, what for
  const f = el("form"), name = el("input"), value = el("input"), scope = el("select"), desc = el("input"), kind = el("select"), group = el("input");
  name.type = "text"; name.placeholder = "its name, such as AZURE_DEVOPS_EXT_PAT"; name.value = k.name || ""; name.setAttribute("aria-label", "Key name");
  name.addEventListener("input", () => { name.value = keyName(name.value) === "SECRET" && !name.value ? "" : name.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"); });
  value.type = "password"; value.placeholder = "its value (never shown again)"; value.autocomplete = "off"; value.setAttribute("aria-label", "Key value");
  if (k.value) { value.value = k.value; value.hidden = true; }
  option(kind, "secret", "a secret (a password, a token: never shown)"); option(kind, "setting", "a setting (a host, a user, a port: shown)"); kind.setAttribute("aria-label", "Kind");
  if (k.ask) kind.hidden = true;
  kind.addEventListener("change", () => { value.type = kind.value === "setting" ? "text" : "password"; });
  group.type = "text"; group.placeholder = "group, such as staging server (optional)"; group.value = k.group || ""; group.setAttribute("aria-label", "Group");
  option(scope, "project", "for this project"); option(scope, "all", "for all projects"); scope.setAttribute("aria-label", "For which projects");
  if (k.scope === "all") scope.value = "all";
  desc.type = "text"; desc.placeholder = "what it is for (the agents read this)"; desc.value = k.what || ""; desc.setAttribute("aria-label", "Description");
  const acts = el("div", null, "acts"), ok = el("button", "Store", "primary"), no = el("button", "Cancel"); no.type = "button";
  acts.append(ok, no); f.append(name, kind, value, group, scope, desc, acts);
  no.addEventListener("click", () => cancel ? cancel() : loadKeys());
  f.addEventListener("submit", async e => {
    e.preventDefault();
    try { const got = await api("api/secrets/save", { project:S.project, name:name.value.trim(), value:value.value, scope:scope.value, description:desc.value.trim(), kind:kind.value, group:group.value.trim(), ask:k.ask || "" });
          value.value = ""; done(got); }
    catch (err) { alert(err.message); }
  });
  return f;
}
function sessionOpen() { const s = S.session; return s && s.id && s.status && !["closed", "failed"].includes(s.status) ? s : null; }
async function restartWithKeys() {   // the same conversation, in a session that has the keys
  const s = sessionOpen(); if (!s) return null;
  const t = await api("api/sessions/" + s.id + "/restart", {}); await loadSessions(); $("sesspick").value = t.id; follow(t.id); return t;
}
function offerRestart(got) {
  if (!(got.open_sessions || []).length || !sessionOpen() || !got.open_sessions.includes(S.session.id)) { $("sstatus").textContent = got.name + " is stored: sessions started from now on get it."; return; }
  const n = el("div", null, "card"), b = el("button", "Restart this session with it", "primary"); b.type = "button";
  n.append(el("div", got.name + " is stored. This session started before it: restart it to give it the key (the conversation goes on).", "q"));
  b.addEventListener("click", async () => { try { await restartWithKeys(); n.remove(); } catch (e) { $("sstatus").textContent = e.message; } });
  const x = el("button", "Later"); x.type = "button"; x.addEventListener("click", () => n.remove());
  const acts = el("div", null, "acts"); acts.append(b, x); n.append(acts); $("feed").append(n); $("feed").scrollTop = $("feed").scrollHeight;
}
function keysFirst(text) {   // true: the message waits for the person's answer on the card above the box
  if (S.keysSent && S.keysSent === text) { S.keysSent = ""; return false; }
  const keys = findKeys(text); if (!keys.length) return false;
  const sets = keys.some(k => /_PASSWORD$/.test(k.name)) ? findSettings(text).filter(x => !keys.some(k => x.start < k.start + k.value.length && k.start < x.start + x.value.length)) : [];
  const found = keys.concat(sets).sort((a, b) => a.start - b.start);
  const group = sets.length ? serverPrefix(text).toLowerCase().replace(/_/g, " ") + (/server/i.test(serverPrefix(text)) ? "" : " server") : "";
  const box = $("keyask"), c = el("div", null, "card"); box.replaceChildren(c); box.hidden = false; box.dataset.asks = "";
  c.append(el("div", "This looks like a key. Store it safely instead of sending it?", "q"),
           el("div", "Stored, it is kept by your system and given to this project's sessions as an environment variable: Claude uses it by name and never sees it.", "ev meta"));
  const rows = found.map(k => {
    const name = el("input"); name.type = "text"; name.value = k.name; name.setAttribute("aria-label", "Name for " + k.what);
    const r = el("div", null, "acts"), use = el("input"); use.type = "checkbox"; use.checked = true; use.setAttribute("aria-label", "Store " + k.what);
    r.append(use, el("span", k.setting ? k.what + " " + k.value + " (a setting: shown, not secret), named" : k.what + " (a secret), named", "s"), name);
    c.append(r); return { k, name, use };
  });
  if (group) c.append(el("div", "Kept together as \u201c" + group + "\u201d.", "ev meta"));
  const scope = el("select"); option(scope, "project", "for this project"); option(scope, "all", "for all projects"); scope.setAttribute("aria-label", "For which projects");
  const acts = el("div", null, "acts"), store = el("button", "Store and send without it", "primary"), as = el("button", "Send as it is"), no = el("button", "Cancel");
  for (const b of [store, as, no]) b.type = "button";
  acts.append(scope, store, as, no); c.append(acts);
  const close = () => { box.replaceChildren(); box.hidden = true; };
  no.addEventListener("click", close);
  as.addEventListener("click", () => { close(); S.keysSent = text; send(); });
  store.addEventListener("click", async () => {
    let out = text, names = [], restart = false;
    try {
      for (const { k, name, use } of rows.slice().reverse()) {   // from the end: the earlier positions stay right
        if (!use.checked) continue;
        const got = await api("api/secrets/save", { project:S.project, name:name.value.trim(), value:k.value, scope:scope.value, description:k.what + " (from your message)", kind:k.setting ? "setting" : "secret", group });
        out = out.slice(0, k.start) + "$" + got.name + out.slice(k.start + k.value.length); names.unshift(got.name);
        if ((got.open_sessions || []).length) restart = true;
      }
    } catch (e) { alert(e.message); return; }
    out += "\n\n[" + names.join(", ") + (names.length > 1 ? " are" : " is") + " stored in system-diff's key store and in your environment: use " + (names.length > 1 ? "them" : "it") + " by name, never print " + (names.length > 1 ? "them" : "it") + ".]";
    close(); loadKeys();
    const s = sessionOpen();
    if (restart && s) {
      if (["working", "waiting"].includes(s.status)) $("sstatus").textContent = "Claude is working: this session gets " + names.join(", ") + " when you restart it (Session options, Keys).";
      else { try { await restartWithKeys(); } catch (e) { $("sstatus").textContent = e.message; } }
    }
    $("msg").value = out; S.keysSent = out.trim(); send();
  });
  return true;
}

// ── agents ───────────────────────────────────────────────────────────────────────────────────────────────────
function tab(which) {
  $("tabsession").setAttribute("aria-selected", String(which === "session")); $("tabagents").setAttribute("aria-selected", String(which === "agents"));
  for (const id of ["sessionhead", "feed", "compose"]) $(id).hidden = which !== "session";
  $("agentsbox").hidden = which !== "agents"; if (which === "agents") loadAgents();
}
$("tabsession").addEventListener("click", () => tab("session")); $("tabagents").addEventListener("click", () => tab("agents"));
async function ask(text) {   // a request to Claude, in the open session (one starts when none is open)
  tab("session");
  let s = S.session && S.session.status && !["closed", "failed"].includes(S.session.status) ? S.session : await start();
  if (!s) return;
  try { await api("api/sessions/" + s.id + "/send", { text }); } catch (e) { $("sstatus").textContent = e.message; }
}
async function loadWorkflows(box) {
  let wf; try { wf = await api("api/workflows?project=" + S.project, undefined, 60000); } catch (e) { box.append(el("div", e.message, "ev err")); return; }
  box.append(el("h3", "Workflows"));
  const top = el("div", null, "acts");
  const add = el("button", "Add an agent…"); add.type = "button"; add.title = "Ask Claude to add an agent to the team that is working";
  add.addEventListener("click", () => {
    const kinds = (wf.kinds || []).map(k => k.kind).slice(0, 12);
    const what = prompt("What should the new agent do, and after whom?" + (kinds.length ? "\nKinds you can name: " + kinds.join(", ") : ""), "");
    if (what) ask("Add an agent to the current team (system-diff add_agent): " + what);
  });
  top.append(add);
  if (wf.current && (wf.current.agents || wf.current.steps)) {
    const save = el("button", "Save this team as a workflow…"); save.type = "button";
    save.addEventListener("click", async () => {
      const name = prompt("A name for this workflow (letters, digits, dashes):", ""); if (!name) return;
      const scope = prompt("Keep it for: project, everywhere, or a kind of project (research, software)?", "project") || "project";
      try { await api("api/workflows/save", { project:S.project, name, scope, description:wf.current.task || "" }); loadAgents(); } catch (e) { alert(e.message); }
    });
    top.append(save);
  }
  box.append(top);
  if (!(wf.workflows || []).length) box.append(el("div", "None saved yet: save the team that is working, or ask Claude to (save_workflow).", "ev meta"));
  for (const w of wf.workflows || []) {
    const a = el("div", null, "ag");
    a.append(el("span", w.name, "t"), el("span", [w.agents.length ? w.agents.length + " agents" : "", w.steps.length ? w.steps.length + " steps" : "", w.stages > 1 ? w.stages + " stages" : "in parallel", w.by !== "system-diff" ? "from " + w.by : "", w.scope].filter(Boolean).join(" · "), "s"));
    if (w.description) a.append(el("span", w.description, "s"));
    const acts = el("div", null, "acts"), use = el("button", "Use…", "primary"); use.type = "button";
    use.addEventListener("click", () => { const task = prompt("Run the " + w.name + " workflow for which task?", w.example_task || ""); if (task) ask("Run the saved system-diff workflow `" + w.name + "` (run_workflow) for: " + task); });
    acts.append(use);
    if (w.scope === "project" || w.by === "system-diff" && w.scope !== "extension") {
      const f = el("button", "Forget", "danger"); f.type = "button";
      f.addEventListener("click", async () => { if (!confirm("Forget the workflow " + w.name + "?")) return; try { await api("api/workflows/forget", { project:S.project, name:w.name, scope:w.scope === "project" ? "project" : "everywhere" }); loadAgents(); } catch (e) { alert(e.message); } });
      acts.append(f);
    }
    a.append(acts); box.append(a);
  }
}
async function loadAgents() {
  const box = $("agentsbox"); box.replaceChildren();
  if (!S.project) { box.append(el("div", "Pick a project first.", "ev meta")); return; }
  await loadWorkflows(box);
  let lib; try { lib = await api("api/agents?project=" + S.project); } catch (e) { box.append(el("div", e.message, "ev err")); return; }
  const kinds = lib.kinds || ["software"];
  box.append(el("div", "This project is " + kinds.join(" and ") + ": it gets your library's " + kinds.join(" and ") + " agents.", "ev meta"));
  box.append(el("h3", "Worth keeping"));
  if (!lib.worth_keeping.length) box.append(el("div", "No role has come back yet: roles your plans or subagents use again (here or across your projects) show here.", "ev meta"));
  for (const r of lib.worth_keeping) {
    const a = el("div", null, "ag"); a.append(el("span", r.name, "t"), el("span", r.why + (r.area ? " · " + r.area : ""), "s"));
    const acts = el("div", null, "acts"), sc = el("select"); sc.setAttribute("aria-label", "Keep it for");
    for (const [v, label] of [["project", "this project"], ...kinds.map(k => [k, "all " + k + " projects"]), ["everywhere", "everywhere"]]) option(sc, v, label);
    sc.value = [...sc.options].some(o => o.value === r.scope) ? r.scope : "project";
    const k = el("button", "Keep", "primary"); k.type = "button";
    k.addEventListener("click", async () => {
      const description = prompt("When should Claude use " + r.name + "? (its description)", r.description || ("Use for work on " + (r.area || r.name) + "."));
      if (!description) return;
      try { await api("api/agents/save", { project:S.project, name:r.name, description, prompt:r.brief || ("You are the " + r.name + " agent."), scope:sc.value }); loadAgents(); loadChoices(); }
      catch (e) { alert(e.message); }
    });
    acts.append(sc, k); a.append(acts); box.append(a);
  }
  box.append(el("h3", "This project's agents"));
  const mine = lib.for_project || [];
  if (!mine.length) box.append(el("div", "None yet: Keep one above, or Claude saves one with save_agent.", "ev meta"));
  const counts = Object.fromEntries((lib.saved || []).map(x => [x.name, x]));
  for (const s of mine) {
    const a = el("div", null, "ag");
    a.append(el("span", s.name, "t"), el("span", { project:"this repository", user:"everywhere (yours)", library:"your library: " + (s.scope || "") }[s.source] || s.source, "badge"));
    if (s.description) a.append(el("span", s.description, "s"));
    const c = counts[s.name]; if (c) a.append(el("span", "planned " + c.planned + "×, finished " + c.finished + "×", "s"));
    const acts = el("div", null, "acts"), use = el("button", "Start with it"); use.type = "button";
    use.addEventListener("click", () => { tab("session"); $("agentpick").value = s.name; start(); });
    acts.append(use);
    if (s.source === "library") {
      const ins = el("button", "Add to this repository"); ins.type = "button"; ins.title = "Write it into .claude/agents/ here, so terminal sessions get it too";
      ins.addEventListener("click", async () => { try { const d = await api("api/agents/install", { project:S.project, names:[s.name] }); if (d.skipped.length) alert(d.skipped[0].why); loadAgents(); } catch (e) { alert(e.message); } });
      acts.append(ins);
    }
    const lib_ = (lib.in_library || []).find(x => x.name === s.name);
    const ours = s.source === "library" ? !!(lib_ && lib_.ours) : !!((lib.saved || []).find(x => x.name === s.name && x.ours));
    if (ours) {
      const f = el("button", "Forget", "danger"); f.type = "button";
      f.addEventListener("click", async () => {
        if (!confirm("Forget " + s.name + "? Its file is deleted" + (s.source === "library" ? " from your library (every project of its kind)." : "."))) return;
        try { await api("api/agents/forget", { project:S.project, name:s.name, scope:s.source === "library" ? "library" : s.source }); loadAgents(); loadChoices(); }
        catch (e) { alert(e.message); }
      });
      acts.append(f);
    }
    a.append(acts); box.append(a);
  }
}

// ── start ────────────────────────────────────────────────────────────────────────────────────────────────────
(async () => {
  await loadProjects();
  const want = keep.get("project", ""), p = findProject(x => x.id === want) || S.projects[0];
  if (p) openProject(p); else paintStatus();
  setInterval(loadProjects, 5000);
})();
</script></body></html>
"""
