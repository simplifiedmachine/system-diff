"""The Studio's page (app.py serves it): projects | the project's page (its engine's, through the app) | the session."""

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>System diff Studio</title>
<style>
:root { color-scheme:light;
  --ink:#161c24; --muted:#5f6b7c; --line:#e1e5eb; --bg:#f3f5f8; --card:#ffffff; --accent:#1f5fbf; --accent-ink:#fff;
  --ok:#1b7f4b; --bad:#b42318; --warn:#9a6700; --code:#f0f2f6; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --sans:-apple-system,"Segoe UI",Helvetica,Arial,sans-serif; --chatw:420px; --projw:250px; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme:dark;
  --ink:#e4e9f0; --muted:#97a2b3; --line:#2a3240; --bg:#0f1319; --card:#161b23; --accent:#6ea8ff; --accent-ink:#0f1319;
  --ok:#5cc28d; --bad:#ff7b72; --warn:#e3b341; --code:#0d1117; } }
:root[data-theme="dark"] { color-scheme:dark; --ink:#e4e9f0; --muted:#97a2b3; --line:#2a3240; --bg:#0f1319; --card:#161b23;
  --accent:#6ea8ff; --accent-ink:#0f1319; --ok:#5cc28d; --bad:#ff7b72; --warn:#e3b341; --code:#0d1117; }
* { box-sizing:border-box; }
html, body { height:100%; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 var(--sans); }
button, select, input, textarea { font:inherit; color:inherit; }
button { cursor:pointer; border:1px solid var(--line); background:var(--card); border-radius:7px; padding:4px 10px; }
button.primary { background:var(--accent); color:var(--accent-ink); border-color:var(--accent); font-weight:600; }
button.danger { color:var(--bad); }
button:disabled { opacity:.5; cursor:default; }
button:focus-visible, select:focus-visible, input:focus-visible, textarea:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
select, input[type=text], textarea { background:var(--card); border:1px solid var(--line); border-radius:7px; padding:4px 8px; min-width:0; }
.app { display:grid; height:100vh; grid-template-columns:var(--projw) minmax(0,1fr) 6px var(--chatw); }
.app.noproj { grid-template-columns:0 minmax(0,1fr) 6px var(--chatw); }
.app.nochat { grid-template-columns:var(--projw) minmax(0,1fr) 0 0; }
.app.noproj.nochat { grid-template-columns:0 minmax(0,1fr) 0 0; }
.app.noproj #projects, .app.nochat #chat, .app.nochat #grip { display:none; }
#projects { overflow:auto; border-right:1px solid var(--line); padding:12px 10px; display:grid; align-content:start; gap:10px; }
#projects h1 { font-size:15px; margin:0 0 2px; display:flex; align-items:center; gap:6px; }
#projects .tree { list-style:none; margin:0; padding:0; display:grid; gap:2px; }
#projects .tree ul { list-style:none; margin:2px 0 0 12px; padding:0; display:grid; gap:2px; border-left:1px solid var(--line); }
.pj { width:100%; text-align:left; display:grid; gap:0; border:1px solid transparent; background:transparent; padding:5px 8px; }
.pj:hover { background:var(--card); border-color:var(--line); }
.pj[aria-current="true"] { background:var(--card); border-color:var(--accent); }
.pj .t { font-weight:600; overflow-wrap:anywhere; } .pj .s { font-size:12px; color:var(--muted); }
.badge { display:inline-block; font-size:11px; padding:0 6px; border-radius:9px; background:var(--line); margin-left:6px; font-weight:500; }
.badge.ask { background:var(--warn); color:#000; }
form.add { display:flex; gap:6px; } form.add input { flex:1; }
.mid { display:grid; grid-template-rows:auto 1fr; min-width:0; }
.bar { display:flex; gap:6px; align-items:center; padding:6px 10px; border-bottom:1px solid var(--line); background:var(--card); }
.bar .title { font-weight:600; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
iframe#view { width:100%; height:100%; border:0; background:var(--bg); }
.empty { padding:40px 24px; color:var(--muted); max-width:60ch; }
#grip { cursor:col-resize; background:var(--line); }
#grip:hover, #grip.drag { background:var(--accent); }
#chat { display:grid; grid-template-rows:auto auto 1fr auto; min-width:0; background:var(--card); border-left:1px solid var(--line); }
.tabs { display:flex; gap:4px; padding:6px 8px 0; border-bottom:1px solid var(--line); }
.tabs button { border:0; border-bottom:2px solid transparent; border-radius:0; background:transparent; padding:6px 10px; }
.tabs button[aria-selected="true"] { border-bottom-color:var(--accent); font-weight:600; }
.opts { display:grid; grid-template-columns:auto 1fr; gap:6px 8px; align-items:center; padding:8px 10px; border-bottom:1px solid var(--line); font-size:13px; }
.opts label { color:var(--muted); } .opts .row { display:flex; gap:6px; flex-wrap:wrap; } .opts .row > * { flex:1 1 auto; }
.opts select.danger { border-color:var(--bad); color:var(--bad); }
#feed { overflow:auto; padding:10px; display:grid; align-content:start; gap:8px; }
.ev { border-radius:9px; padding:7px 10px; overflow-wrap:anywhere; }
.ev.user { background:var(--code); border:1px solid var(--line); white-space:pre-wrap; }
.ev.text { white-space:pre-wrap; }
.ev.meta { color:var(--muted); font-size:12px; padding:0 4px; }
.ev.err { color:var(--bad); font-size:13px; }
.ev.tool { border:1px solid var(--line); font-size:13px; }
.ev.tool summary { cursor:pointer; } .ev.tool .nm { font-weight:600; }
.ev pre, .card pre { font:12px/1.45 var(--mono); background:var(--code); border-radius:6px; padding:6px 8px; margin:6px 0 0; white-space:pre-wrap; max-height:260px; overflow:auto; }
pre.old { border-left:3px solid var(--bad); } pre.new { border-left:3px solid var(--ok); }
.card { border:2px solid var(--warn); border-radius:10px; padding:8px 10px; display:grid; gap:6px; }
.card .q { font-weight:600; } .card .acts { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.card .acts input { flex:1 1 140px; }
.card.done { border-color:var(--line); opacity:.75; }
.compose { border-top:1px solid var(--line); padding:8px 10px; display:grid; gap:6px; }
.compose textarea { width:100%; min-height:62px; max-height:40vh; resize:vertical; }
.compose .row { display:flex; gap:6px; align-items:center; } .compose .row .st { flex:1; color:var(--muted); font-size:12px; }
.pulse { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--accent); margin-right:6px; animation:p 1s infinite; }
@keyframes p { 50% { opacity:.3; } }
@media (prefers-reduced-motion: reduce) { .pulse { animation:none; } }
#agentsbox { overflow:auto; padding:10px; display:grid; align-content:start; gap:10px; }
#agentsbox h3 { margin:4px 0 0; font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }
.ag { border:1px solid var(--line); border-radius:9px; padding:7px 10px; display:grid; gap:3px; }
.ag .t { font-weight:600; } .ag .s { color:var(--muted); font-size:12px; } .ag .acts { display:flex; gap:6px; }
.notice { margin:10px; padding:10px; border-radius:9px; border:1px solid var(--warn); }
.notice code { font-family:var(--mono); }
@media (max-width: 900px) {
  .app, .app.noproj, .app.nochat { grid-template-columns:1fr; grid-template-rows:auto minmax(60vh,1fr) auto; height:auto; }
  #grip { display:none; } #projects { border-right:0; border-bottom:1px solid var(--line); max-height:30vh; }
  #chat { border-left:0; border-top:1px solid var(--line); min-height:70vh; }
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
    <div id="sessionhead">
      <div class="opts" id="opts">
        <label for="sesspick">Session</label>
        <div class="row"><select id="sesspick" aria-label="Session"></select><button type="button" id="newsess" class="primary">New</button></div>
        <label for="settings">Settings</label><select id="settings"></select>
        <label for="model">Model</label><div class="row"><input type="text" id="model" list="models" placeholder="the settings' default"><datalist id="models"></datalist></div>
        <label for="mode">Permissions</label><select id="mode"></select>
        <label for="agentpick">Agent</label><select id="agentpick"><option value="">Claude (no saved agent)</option></select>
        <label for="pastpick">Resume</label><div class="row"><select id="pastpick"><option value="">a past conversation…</option></select><button type="button" id="resume">Resume</button></div>
        <span></span><div class="row"><button type="button" id="apply" hidden title="Start a session with these settings that continues this conversation">Apply to this conversation</button></div>
      </div>
    </div>
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
  const w = +keep.get("chatw", "420"); if (w >= 280) document.documentElement.style.setProperty("--chatw", w + "px");
  const grip = $("grip"); let drag = false;
  const setW = px => { const v = Math.max(280, Math.min(innerWidth - 360, px)); document.documentElement.style.setProperty("--chatw", v + "px"); keep.set("chatw", String(Math.round(v))); };
  grip.addEventListener("pointerdown", e => { drag = true; grip.classList.add("drag"); grip.setPointerCapture(e.pointerId); $("view") && ($("view").style.pointerEvents = "none"); });
  grip.addEventListener("pointermove", e => { if (drag) setW(innerWidth - e.clientX); });
  grip.addEventListener("pointerup", () => { drag = false; grip.classList.remove("drag"); $("view") && ($("view").style.pointerEvents = ""); });
  grip.addEventListener("keydown", e => { const cur = $("chat").offsetWidth; if (e.key === "ArrowLeft") setW(cur + 24); if (e.key === "ArrowRight") setW(cur - 24); });
})();

// ── projects ─────────────────────────────────────────────────────────────────────────────────────────────────
function projBtn(p, depth) {
  const b = el("button", null, "pj"); b.type = "button";
  if (p.id === S.project) b.setAttribute("aria-current", "true");
  const t = el("span", p.name + (p.kind === "submodule" ? " (submodule)" : p.kind === "nested" && depth ? " (nested)" : ""), "t");
  if (p.sessions) t.append(el("span", p.sessions + " session" + (p.sessions > 1 ? "s" : ""), "badge"));
  if (p.waiting) t.append(el("span", "asks you", "badge ask"));
  b.append(t, el("span", [p.branch, p.port ? "" : "engine not running"].filter(Boolean).join(" · "), "s"));
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
  S.project = p.id; S.path = p.path; keep.set("project", p.id);
  $("ptitle").textContent = p.name + (p.branch ? " · " + p.branch : "");
  paintProjects();
  const box = $("viewbox"); box.replaceChildren();
  const f = el("iframe"); f.id = "view"; f.title = "The system-diff page of " + p.name;
  f.src = "p/" + p.id + "/?studio=1"; box.append(f);
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
  const ag = $("agentpick"); ag.replaceChildren(); option(ag, "", "Claude (no saved agent)"); for (const a of c.agents || []) option(ag, a, a);
  paintSdk();
}
function paintMode() { $("mode").classList.toggle("danger", $("mode").value === "bypassPermissions"); }
$("mode").addEventListener("change", () => { paintMode(); offerApply(); });
for (const id of ["settings", "model", "agentpick"]) $(id).addEventListener("change", offerApply);
function choice() { return { project:S.project, settings_profile:$("settings").value, model:$("model").value.trim(),
                             permission_mode:$("mode").value, agent:$("agentpick").value }; }
function offerApply() { $("apply").hidden = !(S.session && S.session.claude_session && S.session.status !== "closed"); }
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
  sel.value = want; if (want) follow(want); else { S.session = null; $("feed").replaceChildren(); paintStatus(); }
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
function follow(id) {
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
  offerApply();
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
  box.append(el("h3", "Worth keeping"));
  if (!lib.worth_keeping.length) box.append(el("div", "No role has come back yet: roles that plans or subagents use again show here.", "ev meta"));
  for (const r of lib.worth_keeping) {
    const a = el("div", null, "ag"); a.append(el("span", r.name, "t"), el("span", r.why + (r.area ? " · " + r.area : ""), "s"));
    const acts = el("div", null, "acts"), k = el("button", "Keep", "primary"); k.type = "button";
    k.addEventListener("click", async () => {
      const description = prompt("When should Claude use " + r.name + "? (its description)", "Use for work on " + (r.area || r.name) + ".");
      if (!description) return;
      try { await api("api/agents/save", { project:S.project, name:r.name, description, prompt:r.brief || ("You are the " + r.name + " agent."), scope:"project" }); loadAgents(); loadChoices(); }
      catch (e) { alert(e.message); }
    });
    acts.append(k); a.append(acts); box.append(a);
  }
  box.append(el("h3", "Saved"));
  if (!lib.saved.length) box.append(el("div", "None saved: Claude saves one with save_agent, or press Keep above.", "ev meta"));
  for (const s of lib.saved) {
    const a = el("div", null, "ag"); a.append(el("span", s.name + " (" + s.scope + ")", "t"));
    if (s.description) a.append(el("span", s.description, "s"));
    a.append(el("span", "planned " + s.planned + "×, finished " + s.finished + "×" + (s.model ? " · " + s.model : ""), "s"));
    const acts = el("div", null, "acts"), use = el("button", "Start with it"); use.type = "button";
    use.addEventListener("click", () => { tab("session"); $("agentpick").value = s.name; start(); });
    acts.append(use);
    if (s.ours) { const f = el("button", "Forget", "danger"); f.type = "button"; f.addEventListener("click", async () => { if (!confirm("Forget " + s.name + "? Its file is deleted.")) return; try { await api("api/agents/forget", { project:S.project, name:s.name, scope:s.scope }); loadAgents(); loadChoices(); } catch (e) { alert(e.message); } }); acts.append(f); }
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
