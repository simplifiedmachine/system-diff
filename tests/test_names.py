"""Readable names without an LLM (names.py), the folded docs and tools, zooming into any box, and the LLM reached
through the person's own Claude Code (narrate's claude CLI gateway; never the real CLI here: run_hidden is mocked)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from system_diff import architecture, design, inside, names, narrate, pane, regroup
from system_diff.compare import compare

from conftest import git_, write

ROUTER = ('from fastapi import APIRouter\nfrom bizflow_backend.dispatch import send\n'
          'from bizflow_backend.repos import orders\n\nrouter = APIRouter(prefix="/api/orders")\n\n\n'
          '@router.get("")\ndef list_orders():\n    return orders.all()\n\n\n@router.post("/{id}")\n'
          'def post(id):\n    return send(id)\n')

BIZFLOW = {
    "bizflow_backend/routers/orders.py": ROUTER,
    "bizflow_backend/dispatch/__init__.py": '"""Job dispatcher. Sends work to the agents."""\nfrom .run import send\n',
    "bizflow_backend/dispatch/run.py": "from libs.llm import client\n\n\ndef send(x):\n    return client.ask(x)\n",
    "bizflow_backend/repos/orders.py": "from libs.db_client import db\n\n\ndef all():\n    return db.q()\n",
    "libs/llm/client.py": "import os\nKEY = os.getenv('LLM_KEY')\n\n\ndef ask(x):\n    return x\n",
    "libs/llm/pyproject.toml": '[project]\nname = "llm"\ndescription = "Wrapper for model providers"\n',
    "libs/db_client/db.py": "def q():\n    return []\n",
    "services/agent/main.py": "from libs.llm import client\n\n\ndef run():\n    return client.ask(1)\n",
    "web/src/components/Button.tsx": 'import { api } from "../pages/api";\nexport const Button = () => api();\n',
    "web/src/pages/api.ts": 'export const api = () => fetch("/api/orders");\n',
    "web/src/pages/Home.tsx": 'import { Button } from "../components/Button";\nexport const Home = () => Button();\n',
    "web/package.json": '{"name": "web", "description": "Bizflow front office", "dependencies": {"react": "18"}}\n',
    "docs/NUMI/build.py": "print('docs')\n",
    "tools/x/helper.py": "print('a tool')\n",
    "tools/y/other.py": "print('another tool')\n",
}


@pytest.fixture
def bizflow(tmp_path: Path) -> Path:
    r = tmp_path / "bizflow"
    r.mkdir()
    git_(r, "init", "-q", "-b", "main")
    write(r, BIZFLOW)
    git_(r, "add", "-A")
    git_(r, "commit", "-q", "-m", "first")
    return r


def _model(root: Path):
    c = pane.compare(root, "HEAD", ".")
    return c, architecture.system(c)


def _titles(model) -> dict:
    out = {}
    for n in model["nodes"]:
        out[n["id"]] = n["title"]
        for i in n.get("items") or []:
            out[i["id"]] = i["title"]
    return out


# ------------------------------------------------------------------------------------------------ names

def test_folders_are_said_as_what_they_are(bizflow):
    c, m = _model(bizflow)
    t = _titles(m)
    assert t["bizflow_backend/routers"] == "API routes"
    assert t["bizflow_backend/repos"] == "Database access"
    assert t["bizflow_backend/dispatch"] == "Job dispatcher"            # its own docstring's first sentence
    assert t["libs/llm"] == "Wrapper for model providers"      # its pyproject's description
    assert t["libs/db_client"] == "Database access (libs)"            # told apart from the backend's repos
    assert t["services/agent"] == "Agent service"
    assert t["web/src/components"] == "UI components"
    assert t["web/src/pages"] == "Pages"
    # the path stays, for hover and detail; the text says the same names
    assert design.name_of("services/agent", c.after) == "Agent service"
    assert design.Reading(c).name("libs/llm") == "Wrapper for model providers"


def test_names_are_unique_and_short():
    bp = {"components": {cid: {"label": cid, "files": 1} for cid in
                         ("a/components", "b/components", "c/x/components", "d/x/components", "libs/thing")},
          "routes": {}}
    got = names.of(bp)
    assert len(set(got.values())) == len(got)
    assert got["a/components"] == "UI components (a)" and got["libs/thing"] == "Shared library: Thing"
    assert all(len(n) <= names.MAX + 12 for n in got.values())
    assert names.cut("a really long description of what this package does in life", 32).endswith("…")


def test_the_repository_top_package_is_dropped_and_a_role_word_added():
    bp = {"components": {"shop/orders": {"label": "shop/orders"}, "shop/models": {"label": "shop/models"},
                         "shop/local_ews_helper": {"label": "shop/local_ews_helper"}},
          "routes": {"GET /orders": "shop/orders"}}
    got = names.of(bp)
    assert got == {"shop/orders": "Orders API", "shop/models": "Data models",
                   "shop/local_ews_helper": "Local EWS Helper"}


def test_docs_and_tools_with_no_connections_are_one_box(bizflow):
    _c, m = _model(bizflow)
    box = next(n for n in m["nodes"] if n["id"] == "g:" + names.FOLD_TITLE)
    assert {i["id"] for i in box["items"]} == {"docs/NUMI", "tools/x", "tools/y"}
    assert not any(n["id"] in ("docs/NUMI", "tools/x", "tools/y") for n in m["nodes"])


def test_the_config_label_wins_then_the_overlay(bizflow):
    write(bizflow, {"system-diff.toml": '[[components]]\nid = "services/agent"\nlabel = "The Agent"\n'
                                        'paths = ["services/agent/**"]\n\n[[components]]\nid = "tools/x"\n'
                                        'paths = ["tools/x/**"]\n'})
    c = pane.compare(bizflow, "HEAD", ".")
    t = _titles(architecture.system(c))
    assert t["services/agent"] == "The Agent"
    assert "tools/x" not in {i["id"] for n in architecture.system(c)["nodes"] if n["id"] == "g:Docs and tools"
                             for i in n["items"]}                                   # configured: not folded
    ov = {"parts": {"services/agent": {"label": "LLM says agent"}, "libs/llm": {"label": "Model gateway"}}}
    t = _titles(architecture.system(c, overlay=ov))
    assert t["services/agent"] == "The Agent" and t["libs/llm"] == "Model gateway"
    c.regroup = ov
    assert design.Reading(c).name("libs/llm") == "Model gateway"


# ------------------------------------------------------------------------------------------------ zoom

MORE = {   # more shared libraries and services: the logic column is crowded
    "libs/auth/jwt.py": "from libs.logging import log\n",
    "libs/logging/log.py": "import logging\n",
    "libs/mcp_client/client.py": "from libs.llm import client\n",
    "services/billing/main.py": "from libs.auth import jwt\nfrom libs.db_client import db\n",
    "services/mail/main.py": "from libs.logging import log\n",
}


@pytest.fixture
def crowded(bizflow: Path) -> Path:
    write(bizflow, MORE)
    git_(bizflow, "add", "-A")
    git_(bizflow, "commit", "-q", "-m", "more")
    return bizflow


def test_a_crowded_column_is_grouped_by_folder(crowded):
    _c, m = _model(crowded)
    groups = {n["title"]: {i["id"] for i in n["items"]} for n in m["nodes"] if n["type"] == "group"}
    assert groups["Shared libraries"] == {"libs/llm", "libs/db_client", "libs/auth", "libs/logging",
                                          "libs/mcp_client"}
    assert groups["Services"] == {"services/agent", "services/billing", "services/mail"}
    assert groups[names.FOLD_TITLE] == {"docs/NUMI", "tools/x", "tools/y"}


def test_zoom_into_a_container_shows_its_parts_and_their_flows(crowded):
    from system_diff import graph
    c, m = _model(crowded)
    t = _titles(m)
    for g in [n for n in m["nodes"] if n["type"] == "group"]:
        z = architecture.container(c, g["id"])
        held = {i["id"] for i in g["items"]}
        ids = {n["id"] for n in z["nodes"]}
        assert held <= ids and z["title"] == g["title"] and z["box"] == g["id"]
        assert all(n["type"] != "group" for n in z["nodes"])
        assert all(n["title"] == t[n["id"]] for n in z["nodes"] if n["id"] in t)
        assert {n["col"] for n in z["nodes"] if n["id"] in held} <= {"in0", "in1", "in2", "in3"}
        # every connection of a part in it, in or out, is drawn, met at the part at the other end
        want = {(s, x) for (s, x), (k, _n) in graph.connections(c.after).items()
                if k == "call" and (s in held or x in held) and s in t and x in t}
        assert want <= {(e["from"], e["to"]) for e in z["edges"]}, want
    libs = architecture.container(c, "g:Shared libraries")
    assert {"libs/auth", "libs/llm"} <= {e["to"] for e in libs["edges"] if e["from"] in ("services/billing",
                                                                                      "services/agent")}
    assert design.and_list([n["title"] for n in libs["nodes"]])
    # zooming into a part held by a container finds the container's view when its code cannot be read
    assert architecture.container(c, "libs/llm")["box"] == "g:Shared libraries"


def test_enter_zooms_into_every_box_and_back(bizflow):
    page = pane.show(bizflow, width=100, view="now")
    zooms = [b["enter"]["zoom"] for b in page["blocks"] if b.get("enter", {}).get("zoom")]
    kinds = {z[:2] if z[:2] in ("g:", "a:", "x:") else "part" for z in zooms}
    assert {"g:", "a:", "part"} <= kinds, zooms
    for z in zooms:
        got = pane.show(bizflow, width=100, view="now", part=z)
        text = "\n".join("".join(r[0] for r in row) for row in got["rows"])
        assert got["part"] == z and ("INSIDE" in text or "ZOOMED IN ON" in text), (z, text[:400])
        assert len([b for b in got["blocks"] if b["id"].startswith("b:")]) >= 1, z
    back = pane.show(bizflow, width=100, view="now", part="")
    assert any("SYSTEM" in "".join(r[0] for r in row) for row in back["rows"])


def test_zoom_into_a_part_uses_the_names(bizflow):
    c = pane.compare(bizflow, "HEAD", ".")
    z = inside.internals(c, bizflow, "bizflow_backend/routers")
    assert z["title"] == "API routes"
    around = {n["id"]: n["title"] for n in z["nodes"] if not n["id"].startswith("f:")}
    assert around.get("bizflow_backend/dispatch") == "Job dispatcher"


# ------------------------------------------------------------------------------------------------ the claude CLI

class _Done:
    def __init__(self, out: str, code: int = 0):
        self.stdout, self.returncode, self.stderr = out, code, ""


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(narrate, "claude_llm", lambda root, extra=(): None)
    monkeypatch.setattr(narrate, "shell_llm", lambda: None)
    monkeypatch.setattr(narrate.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    monkeypatch.setenv("SYSTEM_DIFF_LLM", "cli")
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return _Done(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": '{"ok": 1}',
                                 "modelUsage": {"claude-haiku-5-5": {"inputTokens": 3}}}))
    monkeypatch.setattr(narrate, "run_hidden", run)
    return calls


def test_the_claude_cli_is_asked_when_no_gateway_is_set(cli, tmp_path):
    got = narrate.complete("the question", "the rules", tmp_path, {}, timeout=30)
    assert got == ('{"ok": 1}', "claude-haiku-5-5 (your Claude Code)")
    argv, kw = cli[0]
    assert argv[:2] == ["/usr/bin/claude", "-p"]
    assert argv[argv.index("--output-format") + 1] == "json" and argv[argv.index("--model") + 1] == "haiku"
    assert "--no-session-persistence" in argv and argv[argv.index("--tools") + 1] == ""
    assert "the rules" in kw["input"] and "the question" in kw["input"]          # the prompt on stdin, not argv
    assert not any("the question" in a for a in argv)
    assert kw["timeout"] == 30 and kw["env"]["SYSTEM_DIFF_LLM"] == "off" and kw["env"]["SYSTEM_DIFF_REGROUP"] == "0"
    assert Path(kw["cwd"]) != tmp_path                                         # not in the project: no transcript
    assert narrate.configured(tmp_path)


def test_a_failed_or_error_answer_is_none(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(narrate, "run_hidden", lambda argv, **kw: _Done(json.dumps({"is_error": True,
                                                                                    "result": "Not logged in"})))
    assert narrate.complete("q", "s", tmp_path, {}) is None
    monkeypatch.setattr(narrate, "run_hidden", lambda argv, **kw: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(argv, 1)))
    assert narrate.complete("q", "s", tmp_path, {}) is None


def test_off_means_no_llm_at_all(cli, monkeypatch, tmp_path):
    monkeypatch.setenv("SYSTEM_DIFF_LLM", "off")
    assert narrate.complete("q", "s", tmp_path, {}) is None and not cli
    assert not narrate.configured(tmp_path)
    monkeypatch.setattr(narrate.shutil, "which", lambda name: None)
    monkeypatch.setenv("SYSTEM_DIFF_LLM", "cli")
    assert narrate.complete("q", "s", tmp_path, {}) is None and not narrate.configured(tmp_path)


def test_the_regroup_job_goes_through_the_cli(cli, bizflow, monkeypatch):
    monkeypatch.delenv("SYSTEM_DIFF_REGROUP", raising=False)
    spawned = []
    monkeypatch.setattr(regroup, "_spawn", lambda root, job: spawned.append(job))
    c = compare(bizflow, "HEAD", ".")
    c.cfg["regroup"] = {"min_parts": 0}
    regroup.ensure(bizflow, c.after, c.cfg)
    assert spawned                                                      # the CLI counts as a configured LLM
    answer = {"groups": [{"name": "Backend", "parts": ["bizflow_backend/dispatch", "bizflow_backend/repos"]}],
              "parts": {}, "fold": []}
    monkeypatch.setattr(narrate, "run_hidden", lambda argv, **kw: cli.append((argv, kw)) or _Done(json.dumps(
        {"is_error": False, "result": json.dumps(answer), "modelUsage": {"claude-haiku-5-5": {}}})))
    assert regroup.run(spawned[0]) == 0 and cli
    assert regroup.ensure(bizflow, c.after, c.cfg)["parts"]["bizflow_backend/repos"]["group"] == "Backend"
