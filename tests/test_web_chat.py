"""The web page's conversation with Claude: Enter sends, the answer comes back to the page (recorded by the Stop hook),
names in it point at the diagram, Claude can point and keep notes (MCP tools), what the person looks at goes with the
question; and the code viewer's outline with each chunk described by the person's own LLM (mocked here)."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from system_diff import agent, chat, fileview, mcp_server, pane, watch
from system_diff.config import lenient
from system_diff.extract import Code
from system_diff.sources import git_dir


@pytest.fixture
def served(repo):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)},
                         daemon=True)
    t.start()
    deadline = time.time() + 15
    while "w" not in up and time.time() < deadline:
        time.sleep(0.05)
    assert up["w"].engine.done.wait(60)
    yield up
    up["server"].shutdown()
    t.join(5)


def _call(url, method="GET", headers=None, body=None, timeout=40):
    req = urllib.request.Request(url, method=method, headers=headers or {},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None


def _page_js() -> str:
    return watch.PAGE.split("<script>")[1].split("</script>")[0]


# ── the page ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_enter_sends_shift_enter_is_a_new_line_and_an_ime_never_sends():
    js = _page_js()
    keys = js[js.index('$("say").addEventListener("keydown"'):]
    keys = keys[:keys.index("});") + 3]
    assert 'e.key !== "Enter"' in keys and "e.isComposing" in keys and "229" in keys
    assert "e.shiftKey" in keys and "send()" in keys and "ctrlKey" in keys
    assert "Enter sends, Shift+Enter for a new line" in watch.PAGE


def test_the_page_script_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    f = Path(__file__).parent / ".page_check.js"
    try:
        f.write_text(_page_js(), encoding="utf-8")
        got = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    finally:
        f.unlink(missing_ok=True)
    assert got.returncode == 0, got.stderr


def test_the_panel_is_pinned_toggled_remembered_and_renders_answers_as_text():
    page, js = watch.PAGE, _page_js()
    assert 'id="chatbtn"' in page and 'aside class="chat" id="chat"' in page
    assert 'localStorage.setItem("sd-chat"' in js and "try {" in js[js.index("function setTalk"):][:400]
    assert "@media (max-width:1000px)" in page and "border-radius:14px 14px 0 0" in page   # a bottom sheet when narrow
    md = js[js.index("function inline("):js.index("function escapeRe")]
    assert "innerHTML" not in md and "createTextNode" in md   # answers are DOM text, never markup
    assert page.count("innerHTML") == 1 and "box.innerHTML = g.svg" in page
    for status in ("queued", "delivered", "answering", "answered"):
        assert status in js[js.index("const STATUS"):js.index("function renderTalk")]


def test_an_answer_with_markup_never_becomes_markup():
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    js = _page_js()
    body = js[js.index("function inline("):js.index("function escapeRe")]
    harness = """
class N { constructor(t, v) { this.tagName = t; this.children = []; this.text = v || ""; }
  append(...k) { for (const x of k) this.children.push(typeof x === "string" ? new N("#text", x) : x); }
  get lastChild() { return this.children[this.children.length - 1]; }
  set textContent(v) { this.text = v; } }
const document = { createElement: t => new N(t.toUpperCase()), createTextNode: v => new N("#text", v) };
function el(tag, text, cls) { const e = document.createElement(tag); if (text) e.textContent = text; return e; }
""" + body + """
const out = markdown("Hi <script>alert(1)</script>\\n\\n- `core` **bold** <b>x</b>\\n\\n```\\n<img src=x>\\n```");
const tags = []; const walk = n => { tags.push(n.tagName); (n.children || []).forEach(walk); };
out.forEach(walk);
console.log(JSON.stringify({ tags, texts: JSON.stringify(out) }));
"""
    f = Path(__file__).parent / ".md_check.js"
    try:
        f.write_text(harness, encoding="utf-8")
        got = subprocess.run([node, str(f)], capture_output=True, text=True)
    finally:
        f.unlink(missing_ok=True)
    assert got.returncode == 0, got.stderr
    res = json.loads(got.stdout)
    assert set(res["tags"]) <= {"P", "UL", "LI", "CODE", "STRONG", "PRE", "#text"}
    assert "<script>alert(1)</script>" in res["texts"]   # kept as text, as it was written


# ── a question's way: queued → delivered → answering → answered ──────────────────────────────────────────────

def test_the_stop_hook_keeps_the_answer_from_the_payload_matched_to_the_web_question(repo, monkeypatch):
    monkeypatch.setenv("SYSTEM_DIFF_WEB_START", "0")
    line = watch.outbox_put(repo, chat.with_about("API › app/api/routes.py", "What does approve do?"))
    v = chat.view(repo)
    assert v["items"][-1]["status"] == "queued" and v["items"][-1]["about"] == "API › app/api/routes.py"
    taken = pane.take_outbox(repo)                       # the pane takes it, to submit it
    assert taken and chat.view(repo)["items"][-1]["status"] == "delivered"
    said = agent.on_prompt(repo, {"prompt": taken[0]["text"], "session_id": "s1"}, env={})
    assert chat.WEB_HINT in said["context"]               # Claude is told it came from the web page
    v = chat.view(repo)
    assert v["items"][-1]["status"] == "answering" and v["items"][-1]["key"] == line["id"]
    agent.on_stop(repo, {"session_id": "s1", "last_assistant_message": "It approves an order in **API**."}, env={})
    v = chat.view(repo)
    item = v["items"][-1]
    assert item["status"] == "answered" and item["key"] == line["id"] and item["question"] == "What does approve do?"
    assert item["answer"].startswith("It approves") and item["from"] == "web"
    rec = json.loads((git_dir(repo) / chat.ANSWERS).read_text(encoding="utf-8").splitlines()[-1])
    assert rec["id"] == line["id"]


def test_a_prompt_typed_in_claude_code_gets_no_web_hint_but_its_answer_shows(repo):
    said = agent.on_prompt(repo, {"prompt": "refactor the store", "session_id": "s2"}, env={}) or {}
    assert chat.WEB_HINT not in (said.get("context") or "")
    agent.on_stop(repo, {"session_id": "s2", "last_assistant_message": "Done."}, env={})
    item = chat.view(repo)["items"][-1]
    assert item["from"] == "chat" and item["question"] == "refactor the store" and item["answer"] == "Done."


def test_the_answer_comes_from_the_transcript_when_the_payload_has_none(repo, tmp_path):
    t = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "old question"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]}},
        {"type": "user", "message": {"role": "user", "content": "how does the API reach the Store?"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Let me look."}, {"type": "tool_use", "id": "1", "name": "Read", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "subagent"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Through Core."}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "See `core`."}]}},
        {"type": "system", "content": "x"},
    ]
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    assert chat.transcript_answer(str(t)) == "Through Core.\n\nSee `core`."
    chat.asked(repo, "how does the API reach the Store?", "s3")
    rec = chat.answered(repo, {"session_id": "s3", "transcript_path": str(t)})
    assert rec["text"] == "Through Core.\n\nSee `core`." and rec["prompt"] == "how does the API reach the Store?"


def test_answers_are_capped(repo, monkeypatch):
    monkeypatch.setattr(chat, "ANSWERS_KEEP", 5)
    for i in range(9):
        chat.asked(repo, f"q{i}", "s", at=1000 + i)
        chat.answered(repo, {"session_id": "s", "last_assistant_message": "a" * (chat.ANSWER_MAX + 50)})
    kept = chat.answers(repo)
    assert len(kept) == 5 and kept[-1]["prompt"] == "q8" and all(len(a["text"]) == chat.ANSWER_MAX for a in kept)


# ── names in an answer → blocks ──────────────────────────────────────────────────────────────────────────────

def _index(repo):
    from system_diff import compare
    c = compare.compare(repo, "checkpoint", ".")
    return chat.Index(c.after, Code(lenient(repo)), repo)


def test_names_in_an_answer_link_to_their_blocks(repo):
    ix = _index(repo)
    text = ("The **API** calls Core, which reads `store` and PostgreSQL; see app/api/routes.py:12 and "
            "`POST /api/orders/{id}/approve`; the `orders` table. A core idea (lower case) is no link.")
    got = {(l["text"], l["id"], l["file"], l["code"]) for l in ix.links(text)}
    assert ("API", "api", "", False) in got and ("Core", "core", "", False) in got
    assert ("store", "store", "", True) in got and ("PostgreSQL", "x:postgres", "", False) in got
    assert ("app/api/routes.py:12", "api", "app/api/routes.py", False) in got
    assert ("POST /api/orders/{id}/approve", "api", "", False) in got
    assert ("orders", "x:postgres", "", True) in got
    assert not any(t == "core" and not code for t, _i, _f, code in got)
    assert ix.resolve("Web app")["id"] == "web" and ix.resolve("b:x:postgres")["id"] == "x:postgres"
    assert ix.resolve("web/src/lib/api.ts") == {"text": "web/src/lib/api.ts", "id": "web", "file": "web/src/lib/api.ts"}
    assert ix.resolve("nothing like it")["id"] == ""


def test_the_page_links_names_and_lights_them():
    js = _page_js()
    assert "function linkify(" in js and "function refLink(" in js and "dataset.hid = \"b:\" + id" in js
    point = js[js.index("function pointAt("):js.index("// the answer's text as DOM nodes")]
    assert "ui.pick = \"b:\" + id" in point and "applyPick()" in point and "scrollIntoView" in point
    assert "for (const x of talk.point)" in js           # what Claude points at is lit


# ── point_to and note ────────────────────────────────────────────────────────────────────────────────────────

def test_point_to_records_and_the_page_gets_it_resolved(repo):
    got = mcp_server.tool_point_to(repo, ["API", "app/db/repo.py", "PostgreSQL", "  "], "the request path")
    assert got["pointing_at"] == ["API", "app/db/repo.py", "PostgreSQL"]
    v = chat.view(repo, _index(repo))
    assert [p["id"] for p in v["pointing"]["parts"]] == ["api", "store", "x:postgres"]
    assert v["pointing"]["note"] == "the request path"
    assert "error" in mcp_server.tool_point_to(repo, [])


def test_notes_are_redacted_capped_shown_and_cleared(repo):
    chat.asked(repo, "why is the store slow?", "s")
    got = mcp_server.tool_note(repo, "Store", "connects with postgres://admin:hunter2@db/x and token=abc123def "
                                              "and key sk-abcdefghijklmnop1234 " + "x" * 400)
    text = got["noted"]
    assert "hunter2" not in text and "abc123def" not in text and "sk-abcdefghijklmnop1234" not in text
    assert len(text) <= chat.NOTE_MAX
    v = chat.view(repo, _index(repo))
    notes = v["notes"]["b:store"]
    assert notes[0]["text"] == text and notes[0]["question"] == "why is the store slow?"
    assert chat.notes(repo)[0]["id"] == "store"          # resolved once, kept for the pane
    page = pane.show(repo, 100, "now", None, -1, take=False)
    assert "b:store" in page["details"]                  # the pane's own detail wins: the note is not forced in
    assert chat.clear_note(repo, notes[0]["nid"]) and not chat.view(repo)["notes"]
    assert "error" in mcp_server.tool_note(repo, "", "x")


def test_a_note_fills_the_pane_detail_slot_only_when_it_is_empty(repo):
    chat.add_note(repo, "store", "Every call opens a new connection.")
    page = {"blocks": [{"id": "b:store"}, {"id": "b:api"}], "details": {"b:api": [[["API", "bold"]]]}}
    pane._found_notes(repo, page, 80)
    assert "Every call opens" in json.dumps(page["details"]["b:store"])
    assert page["details"]["b:api"] == [[["API", "bold"]]]


def test_the_hover_card_and_details_show_found_notes_with_a_clear_button():
    js = _page_js()
    card = js[js.index("function showTipSoon"):js.index("function tipTarget")]
    assert "foundBox(hoverId(n), false)" in card
    assert "foundBox(picked, true)" in js and 'fetch("note/clear"' in js and "Found while answering" in js


# ── what the person looks at goes with the question ──────────────────────────────────────────────────────────

def test_the_context_prefix_goes_with_the_question(repo):
    assert chat.with_about("Service Layer › a/b.py", "why?") == "[About: Service Layer › a/b.py]\nwhy?"
    assert chat.split_about("[About: X › y.py]\nwhy?") == ("X › y.py", "why?")
    assert chat.with_about("", "why?") == "why?"
    js = _page_js()
    assert "about:about && talk.aboutOff !== about ? about : \"\"" in js and 'id="about"' in watch.PAGE


def test_the_server_serves_chat_with_auth_and_wakes_the_long_poll(served, repo):
    url, w = served["url"], served["w"]
    own = {"Origin": url.rstrip("/"), "X-Token": w.token, "Content-Type": "application/json"}
    assert _call(url + "send", "POST", {"X-Token": w.token, "Content-Type": "application/json"}, {"text": "hi"})[0] == 403
    s, got = _call(url + "send", "POST", own, {"text": "what is Core?", "about": "Core › app/core/rules.py"})
    assert s == 200
    line = (git_dir(repo) / "outbox.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    assert json.loads(line)["text"] == "[About: Core › app/core/rules.py]\nwhat is Core?"
    assert _call(url + "web?view=now")[0] == 200   # same-origin rules as before: the token, or the page itself
    s, d = _call(url + "web?view=now", headers={"X-Token": w.token})
    assert s == 200 and d["chat"]["items"][-1]["status"] == "queued" and d["chat"]["items"][-1]["question"] == "what is Core?"
    # a long poll at this version and stamp wakes when the panel changes (an answer arrives), not after 25 s
    t0 = time.time()
    threading.Timer(0.5, lambda: chat.point(repo, ["Core"], "")).start()
    s, d2 = _call(url + f"web?view=now&since={d['version']}&chat={d['chat']['stamp']}", headers={"X-Token": w.token})
    assert s == 200 and time.time() - t0 < 10 and d2["chat"]["pointing"]["parts"][0]["id"] == "core"
    assert _call(url + "note/clear", "POST", {"X-Token": w.token}, {"nid": "x"})[0] == 403


# ── the code viewer: an outline, and the LLM's description of each chunk ─────────────────────────────────────

PY = '''import os
from app.core import rules


class Orders:
    """Orders as the API sees them. More words."""

    def list(self):
        return rules.everything()

    async def approve(self, id: int) -> bool:
        """Approve one."""
        return os.getenv("APPROVE_URL") is not None


@router.post("/{id}/approve")
def approve(id):
    return rules.approve(id)
'''

TS = '''import { orders } from "./lib/api";

/** The app's root. It lists orders. */
export function App() {
  return orders();
}

export class Store {
  private items: string[] = [];
  load(id: string): void {
    fetch(process.env.API_URL + "/x");
  }
}

export const total = (a: number) => {
  return a + 1;
};
'''


def test_the_outline_of_a_python_file(repo):
    ix = _index(repo)
    o = fileview.outline("app/api/orders.py", PY, repo, ix)
    by = {c["qual"]: c for c in o["chunks"]}
    assert list(by) == ["Orders", "Orders.list", "Orders.approve", "approve"]
    assert by["Orders"]["kind"] == "class" and by["Orders"]["doc"] == "Orders as the API sees them."
    assert (by["Orders"]["line"], by["Orders"]["end"]) == (5, 13)
    assert by["Orders.approve"]["kind"] == "method" and by["Orders.approve"]["depth"] == 1
    assert by["Orders.approve"]["settings"] == ["APPROVE_URL"] and by["Orders.approve"]["sig"].startswith("async def")
    assert by["approve"]["routes"] == ["POST /{id}/approve"] and by["approve"]["line"] == 16   # from its decorator
    assert by["approve"]["calls"] == ["Core"] and by["Orders.list"]["calls"] == ["Core"]


def test_the_outline_of_a_typescript_file(repo):
    o = fileview.outline("web/src/Main.tsx", TS, repo, _index(repo))
    by = {c["qual"]: c for c in o["chunks"]}
    assert list(by) == ["App", "Store", "Store.load", "total"]
    assert by["App"]["doc"] == "The app's root." and (by["App"]["line"], by["App"]["end"]) == (4, 6)
    assert (by["Store.load"]["line"], by["Store.load"]["end"]) == (10, 12) and by["Store.load"]["settings"] == ["API_URL"]
    assert (by["total"]["line"], by["total"]["end"]) == (15, 17)


def test_the_outline_chunks_link_to_their_lines_in_the_page():
    js = _page_js()
    assert "li.dataset.line = c.line; li.dataset.end = c.end" in js and "function jumpTo(" in js
    assert "function showCodeCard(" in js and "Claude is reading this file…" in js and 'addEventListener("selectionchange"' in js


def test_secrets_are_masked_before_any_code_is_sent():
    code = ('API_KEY = "sk-live-abcdefgh12345678"\nDB = "postgres://u:hunter22@db/x"\nname = "hello world"\n'
            'x = os.getenv("SECRET_TOKEN")\nclient_secret: str = "abcabcabcabc"\n')
    out = fileview.mask_secrets(code)
    assert "sk-live" not in out and "hunter22" not in out and "abcabcabcabc" not in out
    assert '"hello world"' in out and '"SECRET_TOKEN"' in out


def test_opening_a_file_summarises_it_once_in_the_background(served, repo, monkeypatch):
    url, w = served["url"], served["w"]
    (repo / "app" / "api" / "orders.py").write_text(PY + 'TOKEN = "sk-abcdefghijklmnopqrstu"\n', encoding="utf-8")
    sent = []

    def fake(user, system, root, cfg):
        sent.append(user)
        return (json.dumps({"file": "Orders for the API.", "chunks": {
            "Orders.approve": {"what": "Approves an order.", "how": "Checks a setting.", "watch": "Reads the env."},
            "approve": "Approves by id.", "nope": {"what": "made up"}}}), "test-llm")
    monkeypatch.setattr(fileview, "ASK", fake)
    h = {"X-Token": w.token}
    assert _call(url + "summary?path=app/api/orders.py")[0] == 403
    s, d = _call(url + "file?path=app/api/orders.py", headers=h)
    assert s == 200 and d["summary"]["state"] in ("summarizing", "done") and d["outline"]["chunks"]
    fileview.wait_jobs()
    s, d = _call(url + "summary?path=app/api/orders.py", headers=h)
    got = d["summary"]
    assert got["state"] == "done" and got["file"] == "Orders for the API." and got["by"] == "test-llm"
    assert got["chunks"]["Orders.approve"]["watch"] == "Reads the env."
    assert got["chunks"]["approve"]["what"] == "Approves by id." and "nope" not in got["chunks"]
    assert len(sent) == 1 and "sk-abcdefghijklmnopqrstu" not in sent[0] and "Orders.approve" in sent[0]
    _call(url + "file?path=app/api/orders.py", headers=h)       # kept by blob id: not asked again
    fileview.wait_jobs()
    assert len(sent) == 1
    assert list((git_dir(repo) / "explain" / "files").glob("*.json"))


def test_nothing_is_sent_with_the_llm_off(served, repo, monkeypatch):
    url, w = served["url"], served["w"]
    monkeypatch.setattr(fileview, "ASK", lambda *a: pytest.fail("asked"))
    monkeypatch.setenv("SYSTEM_DIFF_LLM", "off")
    s, d = _call(url + "file?path=app/core/rules.py", headers={"X-Token": w.token})
    assert s == 200 and d["summary"]["state"] == "off" and "No LLM available" in d["summary"]["why"]
