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
.ev.text { justify-self:start; max-width:96%; white-space:pre-wrap; background:var(--glass-strong); border:1px solid var(--hair); border-bottom-left-radius:6px; }
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
.compose { border-top:1px solid var(--line); padding:10px 12px 12px; display:grid; grid-template-columns:minmax(0,1fr); gap:8px; }
.compose .row .st { min-width:0; }
.compose textarea { width:100%; min-height:64px; max-height:40vh; resize:vertical; border-radius:14px; padding:9px 12px; }
.compose .row { display:flex; gap:6px; align-items:center; } .compose .row .st { flex:1; color:var(--muted); font-size:12px; }
.pulse { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--accent); margin-right:6px; animation:p 1.2s ease-in-out infinite; }
@keyframes p { 50% { opacity:.25; transform:scale(.8); } }
#agentsbox { overflow:auto; overflow-x:hidden; padding:12px; display:grid; grid-template-columns:minmax(0,1fr); align-content:start; gap:10px; }
#agentsbox h3 { margin:6px 2px 0; font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); }
.ag { border:1px solid var(--hair); background:var(--glass-strong); border-radius:14px; padding:9px 12px; display:grid; gap:4px; }
.ag .t { font-weight:600; } .ag .s { color:var(--muted); font-size:12px; } .ag .acts { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.notice { margin:10px 12px 0; padding:10px 12px; border-radius:14px; background:color-mix(in srgb, var(--warn) 12%, transparent);
  border:1px solid color-mix(in srgb, var(--warn) 40%, transparent); }
.notice code { font-family:var(--mono); }
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
    <div class="tabs" role="tablist">
      <button role="tab" id="tabsession" aria-selected="true">Session</button>
      <button role="tab" id="tabagents" aria-selected="false">Agents</button>
    </div>
    <div id="sessionhead"><details id="optsbox" open><summary id="optsum"><b>Session options</b><span id="optline"></span></summary>
      <div class="opts" id="opts">
        <label for="sesspick">Session</label>
        <div class="row"><select id="sesspick" aria-label="Session"></select><button type="button" id="newsess" class="primary">New</button></div>
        <label for="settings">Settings</label><select id="settings"></select>
        <label for="model">Model</label><div class="row"><input type="text" id="model" list="models" placeholder="the settings' default"><datalist id="models"></datalist></div>
        <label for="effort">Effort</label><select id="effort"></select>
        <label for="mode">Permissions</label><select id="mode"></select>
        <label for="agentpick">Agent</label><select id="agentpick"><option value="">Claude (no saved agent)</option></select>
        <label for="pastpick">Resume</label><div class="row"><select id="pastpick"><option value="">a past conversation…</option></select><button type="button" id="resume">Resume</button></div>
        <span></span><div class="row"><span class="ev meta" id="choicefrom"></span><button type="button" id="ownoff" hidden>Use the all-projects choice</button></div>
        <span></span><div class="row"><button type="button" id="forall" title="These choices for every project (a project's own change still wins there)">Use for all projects</button><button type="button" id="applyopen" hidden title="The model and the permission mode change now in every open session; settings, effort and agent from the next session">Apply to open sessions</button></div>
        <span></span><div class="row"><button type="button" id="fork" hidden title="Continue this conversation in a new session; this one stays as it is">Fork</button><button type="button" id="apply" hidden title="Start a session with these settings that continues this conversation">Apply to this conversation</button></div>
      </div>
    </details></div>
    <div id="feed" aria-live="polite"></div>
    <div id="agentsbox" hidden></div>
    <div class="compose" id="compose">
      <textarea id="msg" placeholder="Ask Claude… (Enter sends, Shift+Enter a new line)" aria-label="Message to Claude"></textarea>
      <div class="row"><span class="st" id="sstatus"></span><button type="button" id="stop" class="danger" disabled>Interrupt</button><button type="button" id="closesess">Close</button><button type="button" id="send" class="primary">Send</button></div>
    </div>
  </aside>
</div>
<script>
const TOKEN = "__TOKEN__";
const $ = id => document.getElementById(id);
function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined && text !== null && text !== "") e.textContent = text; if (cls) e.className = cls; return e; }
async function api(path, body) {
  const opt = { cache:"no-store", headers:{ "X-Token":TOKEN } };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt); let d = {};
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
    if ((p.kids || []).length) { const sub = el("ul"); for (const k of p.kids) { const l2 = el("li"); l2.append(projBtn(k, 1)); sub.append(l2); } li.append(sub); }
    ul.append(li);
  }
  if (!S.projects.length) ul.append(el("li", "No projects yet: add a repository's folder below, or open Claude Code in one.", "ev meta"));
}
async function loadProjects() {
  try { S.projects = (await api("api/projects")).projects || []; paintProjects(); } catch (e) { $("projnote").textContent = e.message; }
}
function findProject(pred) {
  for (const p of S.projects) { if (pred(p)) return p; for (const k of p.kids || []) if (pred(k)) return k; }
  return null;
}
async function openProject(p) {
  const same = S.project === p.id;
  S.project = p.id; S.scope = p.scope || ""; S.path = p.path; keep.set("project", p.id);
  $("ptitle").textContent = p.name + (p.branch ? " · " + p.branch : "");
  paintProjects();
  const box = $("viewbox"); box.replaceChildren();
  const f = el("iframe"); f.id = "view"; f.title = "The system-diff page of " + p.name;
  f.src = "p/" + p.id + "/?studio=1" + (S.scope ? "#scope=" + encodeURIComponent(S.scope) : ""); box.append(f);
  if (same && p.kind === "folder") return;   // the same repository, another of its projects: its sessions stay
  api("api/projects/open", { project:p.id, wait:0 }).catch(() => {});
  await loadChoices(); await loadSessions(); await loadPast();
  if ($("tabagents").getAttribute("aria-selected") === "true") loadAgents();
}
$("reload").addEventListener("click", () => { const f = $("view"); if (f) f.src = f.src; });
$("addform").addEventListener("submit", async e => {
  e.preventDefault(); const path = $("addpath").value.trim(); if (!path) return;
  try { const got = await api("api/projects", { path }); $("addpath").value = ""; $("projnote").textContent = "Added " + got.added;
        await loadProjects(); const p = findProject(x => x.id === got.id); if (p) openProject(p); }
  catch (err) { $("projnote").textContent = err.message; }
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
  try { S.choices = await api("api/choices?project=" + S.project); } catch (e) { S.choices = null; return; }
  const c = S.choices, d = c.default || {};
  const st = $("settings"); st.replaceChildren();
  for (const p of c.settings) option(st, p.id, p.id === "claude-code" ? p.name : p.name + " · " + p.scope + (p.model ? " · " + p.model : "") + (p.provider ? " · " + p.provider : ""));
  st.value = d.settings_profile || "claude-code";
  const ml = $("models"); ml.replaceChildren(); for (const m of c.models) if (m) option(ml, m, m);
  $("model").value = d.model || "";
  const md = $("mode"); md.replaceChildren(); for (const m of c.permission_modes) option(md, m, m === "bypassPermissions" ? m + " (no questions: dangerous)" : m);
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
function paintMode() { $("mode").classList.toggle("danger", $("mode").value === "bypassPermissions"); paintOptLine(); }
$("mode").addEventListener("change", () => { paintMode(); offerApply(); });
for (const id of ["settings", "model", "agentpick", "effort"]) { $(id).addEventListener("change", offerApply); $(id).addEventListener("change", paintOptLine); }
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
  try { S.sessions = (await api("api/sessions?project=" + S.project)).sessions || []; } catch (e) { S.sessions = []; }
  const sel = $("sesspick"); sel.replaceChildren();
  if (!S.sessions.length) option(sel, "", "none yet: New starts one");
  for (const s of S.sessions) option(sel, s.id, (s.title || "(nothing asked yet)").slice(0, 50) + " · " + s.status);
  const want = S.session && S.sessions.find(s => s.id === S.session.id) ? S.session.id : (S.sessions[0] || {}).id || "";
  sel.value = want; if (want) follow(want); else { S.session = null; $("feed").replaceChildren(); paintStatus(); foldOptions(true); }
}
$("sesspick").addEventListener("change", () => { if ($("sesspick").value) follow($("sesspick").value); });
async function loadPast() {
  if (!S.project) return;
  try { S.past = (await api("api/past?project=" + S.project)).past || []; } catch (e) { S.past = []; }
  const sel = $("pastpick"); sel.replaceChildren(); option(sel, "", S.past.length ? "a past conversation…" : "no past conversations");
  for (const p of S.past) option(sel, p.id, (p.title || p.summary || p.first_prompt || p.id).slice(0, 70) + (p.modified ? " · " + new Date(p.modified).toLocaleString() : ""));
}
async function start(extra) {
  if (!S.project) { $("sstatus").textContent = "Pick a project first."; return null; }
  try { const s = await api("api/sessions", { ...choice(), ...(extra || {}) }); await loadSessions(); $("sesspick").value = s.id; follow(s.id); return s; }
  catch (e) { $("sstatus").textContent = e.message; return null; }
}
$("newsess").addEventListener("click", () => start());
$("resume").addEventListener("click", () => { const id = $("pastpick").value; if (id) start({ resume:id, note:"Resumed a past conversation" }); });
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
  S.session = { id }; S.next = 0; S.streaming = null; S.cards = {}; $("feed").replaceChildren();
  (async () => {
    while (mine === S.polling) {
      try {
        const d = await api("api/sessions/" + id + "/events?since=" + S.next + "&wait=25");
        if (mine !== S.polling) return;
        S.session = d.session; S.next = d.next;
        for (const ev of d.events) paint(ev);
        paintStatus();
      } catch (e) { if (mine !== S.polling) return; $("sstatus").textContent = e.message; await new Promise(r => setTimeout(r, 2000)); }
    }
  })();
}
function paintStatus() {
  const s = S.session, st = $("sstatus"); st.replaceChildren();
  if (!s || !s.status) { st.textContent = "No session: New starts one with the settings above."; $("stop").disabled = true; return; }
  const busy = s.status === "working" || s.status === "waiting";
  if (busy) st.append(el("span", null, "pulse"));
  st.append({ starting:"Starting Claude Code…", idle:"Ready", working:"Claude is working", waiting:"Claude asks you (above)",
              closed:"Closed: New starts another, or Resume this conversation", failed:"Could not start: " + (s.error || "") }[s.status] || s.status);
  if (s.model) st.append(" · " + s.model);
  $("stop").disabled = !busy;
  offerApply(); offerFork();
}
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
function paint(ev) {
  scrolled(() => {
    const f = $("feed");
    if (ev.type === "delta") {
      if (!S.streaming) { S.streaming = el("div", "", "ev text"); f.append(S.streaming); }
      S.streaming.textContent += ev.text; return;
    }
    if (ev.type === "text") { if (S.streaming) { S.streaming.remove(); S.streaming = null; } f.append(el("div", ev.text, "ev text")); return; }
    if (S.streaming && ev.type !== "status") { S.streaming.remove(); S.streaming = null; }
    if (ev.type === "user") f.append(el("div", ev.text, "ev user"));
    else if (ev.type === "thinking") { const d = el("details", null, "ev tool"); d.append(el("summary", "Thinking")); d.append(el("pre", ev.text)); f.append(d); }
    else if (ev.type === "tool_use") { const d = el("details", null, "ev tool"); const s = el("summary"); s.append(el("span", ev.name, "nm"), " " + (ev.view.file || ev.view.command || "").slice(0, 80)); d.append(s, toolView(ev.view)); d.dataset.tid = ev.id; f.append(d); }
    else if (ev.type === "tool_result") { const t = f.querySelector('[data-tid="' + CSS.escape(ev.id) + '"]'); const pre = el("pre", ev.text || "(no output)"); if (ev.error) pre.className = "old"; (t || f).append(pre); }
    else if (ev.type === "permission") f.append(askCard(ev));
    else if (ev.type === "permission_done") { const c = S.cards[ev.request]; if (c) { c.classList.add("done"); const a = c.querySelector(".acts"); if (a) a.replaceChildren(el("span", ev.allow ? (ev.always ? "Allowed always" : "Allowed") : "Denied" + (ev.message ? ": " + ev.message : ""))); } }
    else if (ev.type === "result") {
      const u = ev.usage || {}, toks = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
      f.append(el("div", [ev.model || "", toks ? toks.toLocaleString() + " in" : "", u.output_tokens ? u.output_tokens.toLocaleString() + " out" : "",
                          ev.cost != null ? "$" + Number(ev.cost).toFixed(4) : "", ev.terminal_reason || "", ev.error ? "ended with an error" : ""].filter(Boolean).join(" · "), "ev meta"));
    }
    else if (ev.type === "error") f.append(el("div", ev.text, "ev err"));
    else if (ev.type === "note") f.append(el("div", ev.text, "ev meta"));
    else if (ev.type === "interrupt") f.append(el("div", "Interrupted", "ev meta"));
    else if (ev.type === "system" && ev.subtype === "init") f.append(el("div", "Claude Code started · " + (ev.model || "") + " · " + ev.tools + " tools" + ((ev.mcp || []).length ? " · MCP: " + ev.mcp.join(", ") : ""), "ev meta"));
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
async function send() {
  const text = $("msg").value.trim(); if (!text) return;
  let s = S.session && S.session.status && !["closed", "failed"].includes(S.session.status) ? S.session : await start();
  if (!s) return;
  try { await api("api/sessions/" + s.id + "/send", { text }); $("msg").value = ""; } catch (e) { $("sstatus").textContent = e.message; }
}
$("send").addEventListener("click", send);
$("msg").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("stop").addEventListener("click", () => { if (S.session) api("api/sessions/" + S.session.id + "/interrupt", {}).catch(e => { $("sstatus").textContent = e.message; }); });
$("closesess").addEventListener("click", async () => { if (S.session && S.session.id) { await api("api/sessions/" + S.session.id + "/close", {}).catch(() => {}); loadSessions(); } });

// ── agents ───────────────────────────────────────────────────────────────────────────────────────────────────
function tab(which) {
  $("tabsession").setAttribute("aria-selected", String(which === "session")); $("tabagents").setAttribute("aria-selected", String(which === "agents"));
  for (const id of ["sessionhead", "feed", "compose"]) $(id).hidden = which !== "session";
  $("agentsbox").hidden = which !== "agents"; if (which === "agents") loadAgents();
}
$("tabsession").addEventListener("click", () => tab("session")); $("tabagents").addEventListener("click", () => tab("agents"));
async function loadAgents() {
  const box = $("agentsbox"); box.replaceChildren();
  if (!S.project) { box.append(el("div", "Pick a project first.", "ev meta")); return; }
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
