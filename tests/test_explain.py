"""Every block explained as a good engineer explains a system to a newcomer: what it is for, how it works and fits,
what is worth knowing, the facts summarised; rule-based first, an LLM's words (mocked here) in the background, cached
by the block's structure; never a secret, never code sent."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from system_diff import architecture, explain, narrate, pane, watch
from system_diff.extract import fact_store
from system_diff.sources import GitSource, git_dir

from conftest import git_, write

PG_PASSWORD = "very-secret-pg-pw"
OPENAI_KEY = "sk-live-abcdefghijklmnop123456"

MODELS = ["GPT4", "GPT35", "CLAUDE", "MISTRAL", "LLAMA", "EMBED", "RERANK", "SUMMARY", "TRANSLATE", "CLASSIFY",
          "VISION", "SPEECH"]
SETTINGS = ([f"{m}_APIM_URL" for m in MODELS] + [f"{m}_API_KEY" for m in MODELS[:8]]
            + ["LLM_TIMEOUT", "ES_TIMEOUT", "DB_TIMEOUT", "HTTP_TIMEOUT", "MAX_RETRIES", "MAX_TOKENS"]
            + ["APP_UPDATE_DIR", "LOG_LEVEL", "ENVIRONMENT", "ELASTICSEARCH_URL", "ES_INDEX", "OPENAI_API_KEY",
               "DATABASE_URL", "DB_PASSWORD", "FEATURE_X", "REGION"])

def settings_py():
    body = "\n".join(f'{s} = os.getenv("{s}")' for s in SETTINGS)
    return '"""Settings for the orchestrator, read from the environment."""\nimport os\n\n' + body + "\n"

FILES = {
    "orchestrator/__init__.py": '"""DS LLM Orchestrator: routes chat requests to language models."""\n',
    "orchestrator/settings.py": settings_py(),
    "orchestrator/main.py": ('"""The web application: mounts the chat and health routers."""\nfrom fastapi import FastAPI\n'
                             'from orchestrator.service_layer.chat import router as chat_router\n'
                             'from orchestrator.service_layer.health import router as health_router\n\n'
                             'app = FastAPI()\napp.include_router(chat_router)\napp.include_router(health_router)\n'),
    "orchestrator/service_layer/__init__.py": "",
    "orchestrator/service_layer/chat.py": ('"""Chat endpoints: take a question, answer it with a model."""\n'
                                           'from fastapi import APIRouter\n'
                                           'from orchestrator.domain.conversation import Conversation\n'
                                           'from orchestrator.llms.client import complete\n'
                                           'from orchestrator.db.history import save_turn\n\n'
                                           'router = APIRouter(prefix="/chat")\n\n\n'
                                           '@router.post("")\nasync def ask(q: dict):\n'
                                           '    c = Conversation(q)\n    a = await complete(c)\n    save_turn(c, a)\n'
                                           '    return a\n\n\n@router.get("/history")\nasync def history():\n'
                                           '    return []\n\n\n@router.get("/{id}")\nasync def one(id: str):\n'
                                           '    return {}\n'),
    "orchestrator/service_layer/health.py": ('from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n'
                                             '@router.get("/health")\ndef health():\n    return {"ok": True}\n'),
    "orchestrator/domain/__init__.py": "",
    "orchestrator/domain/conversation.py": ('"""A conversation and its turns."""\nimport re\nfrom pydantic import BaseModel\n'
                                            'from orchestrator.search.index import related\n\n\n'
                                            'class Conversation(BaseModel):\n    text: str = ""\n\n\n'
                                            'class Turn(BaseModel):\n    q: str = ""\n\n\n'
                                            'def clean(text):\n    return re.sub(r"\\s+", " ", text)\n'),
    "orchestrator/llms/__init__.py": "",
    "orchestrator/llms/client.py": ('"""Calls the language models."""\nimport os\nimport httpx\nimport openai\n'
                                    'from orchestrator import settings\n\n'
                                    'KEY = os.getenv("OPENAI_API_KEY")\n\n\nasync def complete(c):\n'
                                    '    return await openai.AsyncOpenAI(api_key=KEY).chat.completions.create()\n'),
    "orchestrator/db/__init__.py": "",
    "orchestrator/db/history.py": ('"""Keeps the chat history in PostgreSQL."""\nimport os\nimport psycopg\n'
                                   'from sqlalchemy import Column, Integer\nfrom sqlalchemy.orm import declarative_base\n'
                                   'from orchestrator import settings\n\nBase = declarative_base()\n\n\n'
                                   'class ChatHist(Base):\n    __tablename__ = "chat_hist"\n'
                                   '    id = Column(Integer, primary_key=True)\n\n\n'
                                   'class ChatReq(Base):\n    __tablename__ = "chat_req"\n'
                                   '    id = Column(Integer, primary_key=True)\n\n\n'
                                   'def save_turn(c, a):\n    return None\n'),
    "orchestrator/search/__init__.py": "",
    "orchestrator/search/index.py": ('"""Finds related documents in Elasticsearch."""\nimport os\n'
                                     'from elasticsearch import Elasticsearch\n\n'
                                     'es = Elasticsearch(os.getenv("ELASTICSEARCH_URL"))\n\n\n'
                                     'def related(q):\n    return es.search(index="docs", q=q)\n'),
    "docker-compose.yml": ("services:\n  db:\n    image: postgres:16\n    environment:\n      POSTGRES_DB: chat\n"
                           f"      POSTGRES_PASSWORD: {PG_PASSWORD}\n"
                           "  es:\n    image: docker.elastic.co/elasticsearch/elasticsearch:8.13.0\n"
                           "  api:\n    build: .\n    ports:\n      - \"8000:8000\"\n    environment:\n"
                           f"      - DATABASE_URL=postgresql://app:{PG_PASSWORD}@db:5432/chat\n"
                           f"      - OPENAI_API_KEY={OPENAI_KEY}\n"),
    "tests/test_chat.py": "from orchestrator.service_layer import chat\n\ndef test_x():\n    assert chat\n",
}
FILES["setup/bootstrap.py"] = ('"""Prepares a new environment: creates the tables and the search index."""\n'
                               'from orchestrator.db.history import Base\n\n\ndef main():\n    return Base\n')

SECRET_NAMES = [s for s in SETTINGS if s.endswith("_API_KEY") or s in ("DB_PASSWORD", "OPENAI_API_KEY")]


@pytest.fixture
def orch(tmp_path: Path) -> Path:
    r = tmp_path / "orch"
    r.mkdir()
    git_(r, "init", "-q", "-b", "main")
    write(r, FILES)
    git_(r, "add", "-A")
    git_(r, "commit", "-q", "-m", "orchestrator")
    return r


@pytest.fixture(autouse=True)
def no_llm_by_default(monkeypatch):
    monkeypatch.setattr(narrate, "claude_llm", lambda root, extra=(): None)
    monkeypatch.setattr(narrate, "shell_llm", lambda: None)
    explain._MEMO.clear()
    explain._STARTED.clear()


def _system(root: Path):
    c = pane.compare(root, "checkpoint", ".")
    model = architecture.system(c)
    return c, model, explain.blocks(c, model)


def _group(model) -> str:
    return next(n["id"] for n in model["nodes"] if n.get("items")
                and any(i["id"] == "orchestrator/db" for i in n["items"]))


def _main_text(ex) -> str:
    return " ".join([ex["headline"]] + ex["how"] + ex["insights"])


def test_a_container_is_explained_in_plain_words_with_settings_summarised(orch):
    c, model, ex = _system(orch)
    g = ex["b:" + _group(model)]
    assert g["kind"] == "container"
    text = _main_text(g)
    assert not re.search(r"\b[gxaf]:\w", text), text                       # no ids
    assert "orchestrator/" not in text                                      # no folders
    assert not any(s in g["headline"] + " ".join(g["how"]) for s in SETTINGS)   # no setting names in the main text
    assert g["headline"].startswith("Groups the") and "PostgreSQL" in g["headline"]
    assert any(i.startswith("Configuration-heavy: it reads 36 settings") and "(…_APIM_URL)" in i
               for i in g["insights"])
    assert any("have no value in the repository" in i for i in g["insights"])
    facts = {f["title"]: f for f in g["facts"]}
    settings = facts["Settings"]
    assert settings["summary"].startswith("36 settings: 14 addresses of other services (…_APIM_URL)")
    assert "secrets (API keys, tokens, passwords: not shown)" in settings["summary"]
    assert "6 timeouts and limits" in settings["summary"]
    listed = " ".join(settings["items"])
    assert "GPT4_APIM_URL" in listed and "LLM_TIMEOUT" in listed
    assert not any(name in listed for name in SECRET_NAMES)                # secrets counted, never named
    assert "names and values not shown" in listed
    parts = facts["Parts"]
    assert any(p.startswith("Database access: Reads and writes") for p in parts["items"])   # names with roles
    assert g["inside"] == f"Click to look inside {g['title']}"


def test_a_part_an_outside_system_and_who_uses_it(orch):
    c, model, ex = _system(orch)
    api = ex["b:orchestrator/service_layer"]
    assert "answers chat and health requests" in api["headline"]
    assert "API clients reach it from outside over HTTPS" in api["how"][0]
    assert any(i.startswith("The single front door") for i in api["insights"])
    assert {f["title"]: f for f in api["facts"]}["Endpoints"]["summary"].startswith("4 endpoints: 3 under /chat")
    db = ex["b:orchestrator/db"]
    assert "chat_hist and chat_req in PostgreSQL" in db["headline"]
    assert any("migration" in i for i in db["insights"])
    pg = ex["b:x:postgres"]
    assert pg["headline"].startswith("A relational database")
    assert any("db:5432" in h for h in pg["how"])
    oa = ex["b:x:openai"]
    assert oa["headline"].startswith("A hosted language-model service")
    assert any("not in the repository" in i for i in oa["insights"])
    assert ex["b:a:clients"]["headline"].startswith("Programs outside the system")
    for one in ex.values():
        assert not re.search(r"\b[gxaf]:\w", _main_text(one)), one["title"]
        assert 0 < len(one["how"]) <= 3 and len(one["insights"]) <= 3


def test_a_file_box_says_what_it_is_for_from_its_docstring_and_definitions(orch):
    web = pane.web_view(orch, "now", "orchestrator/service_layer", 0)
    chat = web["hover"]["b:f:orchestrator/service_layer/chat.py"]
    assert chat["ex"]["headline"] == "Chat endpoints: take a question, answer it with a model."
    assert chat["lines"] != ["A module: orchestrator/service_layer/chat.py"]
    health = web["diagram"]["details"]["b:f:orchestrator/service_layer/health.py"]
    assert health["headline"] == "Defines the health endpoint: GET /health."
    facts = {f["title"]: f for f in health["facts"]}
    assert facts["Defines"]["summary"] == "health" and "Main program" in facts["Imported by"]["summary"]
    chat_facts = {f["title"]: f for f in web["diagram"]["details"]["b:f:orchestrator/service_layer/chat.py"]["facts"]}
    assert "LLM client" in chat_facts["Imports from the system"]["summary"]
    # what a file says about itself is kept beside its other facts, by blob id: worked out once per content
    src = GitSource(pane.compare(orch, "checkpoint", ".").head_sha, orch)
    path = "orchestrator/service_layer/chat.py"
    got = fact_store(orch).get(src.blobs[path], path)
    assert got["about"]["doc"].startswith("Chat endpoints") and "ask" in got["about"]["functions"]
    shown = pane.show(orch, 100, "now", "orchestrator/service_layer")
    rows = " ".join("".join(r[0] for r in row) for row in shown["details"]["b:f:orchestrator/service_layer/chat.py"])
    assert "Chat endpoints: take a question" in rows


def _fake_llm(calls, invent=""):
    def complete(user, system, root, cfg, timeout=120, limit=1500):
        calls.append(user)
        items = json.loads(user.split("Blocks:\n", 1)[1])
        out = {}
        for i, b in enumerate(items):
            name = b["structure"]["name"]
            out[b["id"]] = {"headline": f"The {name} is where the system does its {name.lower()} work.",
                            "how": f"It is explained by a teacher. {name} fits in the middle.",
                            "insights": [f"Worth knowing about {name}."]}
            if invent and i == 0:
                out[b["id"]]["insights"] = [f"It also uses {invent} for caching."]
        return json.dumps(out), "claude-haiku (test)"
    return complete


def test_the_llm_words_come_in_the_background_cached_by_structure_and_marked(orch, monkeypatch):
    calls = []
    monkeypatch.setattr(narrate, "complete", _fake_llm(calls))
    c, model, ex = _system(orch)
    assert all(not e["by"] for e in ex.values())                            # the rules' words first
    kept = explain.now(orch, c, model)
    assert kept == len({e["key"] for e in ex.values()}) and 1 <= len(calls) <= 2   # ten blocks a question
    sent = "\n".join(calls)
    assert PG_PASSWORD not in sent and OPENAI_KEY not in sent                # never a secret value
    assert not any(name in sent for name in SECRET_NAMES)                    # nor a secret's name
    assert "def save_turn" not in sent and "return" not in sent              # never code
    assert (git_dir(orch, "explain") / "cache.json").is_file()
    explain._MEMO.clear()
    c2, model2, again = _system(orch)
    g = again["b:" + _group(model2)]
    assert g["by"] == "claude-haiku (test)" and g["headline"].startswith("The ")
    web = pane.web_view(orch, "review", "", 0)
    card = web["hover"]["b:" + _group(model2)]
    assert card["ex"]["by"] and card["lines"][0].endswith(explain.MARK)       # marked as an LLM's
    assert web["diagram"]["details"]["b:x:postgres"]["by"]
    n = len(calls)
    assert explain.now(orch, c2, model2) == 0 and len(calls) == n             # asked once per structure


def test_an_answer_naming_what_the_block_does_not_have_is_not_believed(orch, monkeypatch):
    calls = []
    monkeypatch.setattr(narrate, "complete", _fake_llm(calls, invent="Redis"))
    c, model, ex = _system(orch)
    explain.now(orch, c, model)
    first = json.loads(calls[0].split("Blocks:\n", 1)[1])[0]["id"]
    cache = json.loads((git_dir(orch, "explain") / "cache.json").read_text())
    assert first not in cache and len(cache) >= 2
    bad = [{"id": "k", "key": "k", "ask": {"name": "Search"}, "draft": {}}]
    assert explain.validate({"k": {"headline": "Uses g:Logic.", "how": "x."}}, bad) == {}           # an id
    assert explain.validate({"k": {"headline": "Search.", "how": "Reads main.py."}}, bad) == {}     # a file it lacks
    assert explain.validate({"k": {"headline": "x" * 400, "how": "y."}}, bad) == {}                 # too long
    assert explain.validate("not json", bad) == {}


def test_off_means_the_rules_words_and_no_job(orch, monkeypatch):
    monkeypatch.setenv("SYSTEM_DIFF_LLM", "off")
    c, model, ex = _system(orch)
    assert explain.ensure(orch, c.cfg, ex) is False
    assert explain.now(orch, c, model) == 0
    assert all(not e["by"] for e in explain.blocks(c, model).values())
    monkeypatch.delenv("SYSTEM_DIFF_LLM")
    monkeypatch.setenv("SYSTEM_DIFF_EXPLAIN_JOBS", "1")                     # a job, when asked for, is detached
    monkeypatch.setattr(narrate, "configured", lambda root, extra=(): True)
    started = []
    monkeypatch.setattr(explain, "_spawn", lambda root, job: started.append(json.loads(job.read_text())))
    assert explain.ensure(orch, c.cfg, ex) is True
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.02)
    assert started and len(started[0]["blocks"]) == len({e["key"] for e in ex.values()})
    assert PG_PASSWORD not in json.dumps(started) and OPENAI_KEY not in json.dumps(started)
    assert explain.ensure(orch, c.cfg, ex) is False                          # not again so soon
    explain._unlock(git_dir(orch, "explain"))


def test_the_web_page_shows_sections_and_collapses_long_lists_and_never_a_secret(orch):
    web = pane.web_view(orch, "review", "", 0)
    model_group = next(k for k, v in web["diagram"]["details"].items()
                       if isinstance(v, dict) and v.get("kind") == "container")
    d = web["diagram"]["details"][model_group]
    assert d["headline"] and d["how"] and d["insights"] and d["facts"]
    assert any(len(f["items"]) > 3 for f in d["facts"])                     # a long list, for "show all (N)"
    assert d["inside"].startswith("Click to look inside ") and ":" not in d["inside"]
    for heading in ("What it is", "How it works", "Worth knowing", "Facts", "show all (", 'el("details")',
                    "Click to look inside "):
        assert heading in watch.PAGE, heading
    assert "Click to zoom into " not in watch.PAGE
    shown = json.dumps([web, pane.web_view(orch, "now", "orchestrator/service_layer", 0),
                        pane.show(orch, 100, "review")], ensure_ascii=False)
    assert PG_PASSWORD not in shown and OPENAI_KEY not in shown
    assert "A container of" not in shown and "Reads settings" not in shown   # the old inventory is gone


def test_a_file_box_opens_its_code_and_nothing_else(orch):
    web = pane.web_view(orch, "now", "orchestrator/service_layer", 0)
    files = web["diagram"]["files"]
    assert files["b:f:orchestrator/service_layer/chat.py"] == "orchestrator/service_layer/chat.py"
    assert web["diagram"]["details"]["b:f:orchestrator/service_layer/chat.py"]["inside"] == "Click to read its code"
    got = watch.read_code_file(orch, "orchestrator/service_layer/chat.py")
    assert got["text"].startswith('"""Chat endpoints') and not got["cut"]
    for bad in ("docker-compose.yml", "../x.py", "/etc/passwd", "orchestrator/../../x.py", ".env", ""):
        assert watch.read_code_file(orch, bad)["text"] is None, bad
    assert "showCode(" in watch.PAGE and 'fetch("file?path="' in watch.PAGE
