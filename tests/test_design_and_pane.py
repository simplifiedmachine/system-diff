"""The pane as a system designer reads it: the system diagram (actors, entry points, containers, stores, flows), the
design notes on each change, the page the mod paints, and what a person marks new to them."""
from __future__ import annotations

import json
from types import SimpleNamespace

from system_diff import activity, architecture, canvas, design, profile
from system_diff.cli import main
from system_diff.compare import compare
from system_diff.config import DEFAULTS, merge
from system_diff.diff import diff

HEALTH_ROUTE = ('from fastapi import APIRouter\nrouter = APIRouter()\n\n\n@router.get("/health")\n'
                'def health():\n    return rules.everything()\n')
REFUND = ('from fastapi import APIRouter\nrouter = APIRouter()\n\n\n@router.post("/api/orders/{id}/refund")\n'
          'def refund(id):\n    return "ok"\n')


def page(capsys, repo, *args):
    assert main(["--repo", str(repo), "panel", "--width", "100", "--protocol", "2", *args]) == 0
    out = json.loads(capsys.readouterr().out)
    return out, ["".join(run[0] for run in row) for row in out["rows"]]


def test_now_draws_the_system_as_a_designer_would_with_claude_lit(repo, files, capsys):
    activity.record(repo, {"event": "prompt", "text": "let people refund an order"})
    activity.record(repo, {"event": "edit", "path": "app/api/routes.py"})            # changed no structure
    files(repo, {"app/api/refund.py": REFUND})
    activity.record(repo, {"event": "edit", "path": "app/api/refund.py"})
    activity.record(repo, {"event": "change", "path": "app/api/refund.py", "source": "hook", "items": [
        {"text": "API now answers POST /api/orders/{id}/refund", "why": "it creates or acts", "sign": "+",
         "status": "added"}]})
    out, text = page(capsys, repo)
    assert out["protocol"] == 2 and out["view"] == "now" and out["part"] == ""
    assert text[0] == "● Claude is working  ✎ Editing API" and "  You asked  let people refund an order" in text
    urgent = text.index("▲ NEEDS YOUR ATTENTION  worth a look before Claude goes on")   # a way to change state
    assert text[urgent + 1] == " + API now answers POST /api/orders/{id}/refund"
    assert any(t.startswith("   Ask Claude  Who may call POST") for t in text[urgent:urgent + 8])
    assert any(t.startswith("SYSTEM") for t in text)
    diagram = "\n".join(text)
    for heading in ("WHO USES IT", "WHAT PEOPLE USE", "ENTRY POINTS", "INSIDE THE SYSTEM", "DATA AND OUTSIDE"):
        assert heading in diagram
    assert "▸" in diagram and "browser" in diagram and "SQL" in diagram               # named flows, with arrows
    ids = {b["id"] for b in out["blocks"]}
    assert {"b:a:people", "b:web", "b:api", "b:core", "b:store", "b:x:postgres"} <= ids
    assert out["pick"] == "b:api"                                                       # Claude's, picked
    assert {"b:core", "e:api>core"} <= set(out["lights"]["b:api"])                     # its flows and their ends
    api = next(b for b in out["blocks"] if b["id"] == "b:api")
    assert api["enter"] == {"zoom": "api"} and api["slot"][1] == 2
    assert "uses Core" in "".join(r[0] for row in out["details"]["b:api"] for r in row)
    for heading in ("THIS TURN", "LEARN · HOW API IS DESIGNED", "WHAT JUST HAPPENED"):
        assert any(t.startswith(heading) for t in text), heading
    happened = text[next(i for i, t in enumerate(text) if t.startswith("WHAT JUST HAPPENED")) + 1:]
    assert happened[0].endswith("+ API now answers POST /api/orders/{id}/refund")   # the system diff, step by step
    assert happened[1].strip() == "it creates or acts (refund.py)"                   # and why, in plain words
    assert happened[2].endswith("✎ Claude edited API (routes.py): no change to the structure")
    assert happened[3].endswith("› You asked: let people refund an order")
    assert not any(t.startswith("ABOUT") for t in text)
    assert all(len(run) in (2, 3) for row in out["rows"] for run in row)


def test_enter_on_a_component_opens_its_insides(repo, files, capsys):
    files(repo, {"app/api/health.py": 'from app.core import rules\n' + HEALTH_ROUTE})
    activity.record(repo, {"event": "prompt", "text": "add a health check"})
    activity.record(repo, {"event": "edit", "path": "app/api/health.py"})
    out, text = page(capsys, repo, "--part", "api")
    assert out["part"] == "api" and any(t.startswith("INSIDE API  2 modules") for t in text)
    blocks = {b["id"]: b for b in out["blocks"]}
    assert {"b:f:app/api/routes.py", "b:f:app/api/health.py", "b:web", "b:core"} <= set(blocks)
    assert "enter" not in blocks["b:f:app/api/health.py"] and blocks["b:core"]["enter"] == {"zoom": "core"}
    diagram = "\n".join(text)
    assert "● + health.py" in diagram and "GET /health" in diagram              # Claude's module, new, what it serves
    assert "HTTP" in diagram                                                     # the front end's calls, to a router
    assert "serves GET /health" in "".join(r[0] for row in out["details"]["b:f:app/api/health.py"] for r in row)
    assert "e:web>f:app/api/routes.py" in out["lights"]["b:web"]


def test_map_changes_and_topics(repo, capsys):
    the_map, _ = page(capsys, repo, "--view", "map")
    assert the_map["view"] == "map" and {"b:api", "b:x:postgres"} <= {b["id"] for b in the_map["blocks"]}
    changes, text = page(capsys, repo, "--view", "changes")
    assert any(t.startswith("SINCE") for t in text)
    topics, text = page(capsys, repo, "--view", "topics")
    chip = next(b for b in topics["blocks"] if b["id"] == "t:fastapi")
    assert chip["space"] == {"learn": "fastapi"} and chip["slot"][1] == 4
    assert "b:api" in topics["lights"]["t:fastapi"]                                    # where it is used, lit
    assert "used by API" in "".join(r[0] for row in topics["details"]["t:fastapi"] for r in row)
    assert any(t.startswith("THE STACK") for t in text)


def test_the_system_level_names_who_uses_it_and_what_each_flow_carries(repo):
    c = compare(repo, "HEAD", ".")
    m = architecture.system(c)
    nodes = {n["id"]: n for n in m["nodes"]}
    assert nodes["a:people"]["col"] == "actors" and nodes["web"]["col"] == "ui"
    assert "a:clients" not in nodes                       # the API's caller is its front end, drawn as a flow
    flows = {(e["from"], e["to"]): e for e in m["edges"]}
    assert flows[("a:people", "web")]["label"] == "browser"
    assert flows[("web", "api")]["label"] == "HTTP"
    assert flows[("store", "x:postgres")]["label"] == "SQL"
    assert "x:mail" not in nodes                          # read, but set by no deploy configuration: not drawn
    inner = architecture.container(c, "core")
    assert inner["title"] == "Core" and {n["id"] for n in inner["nodes"]} >= {"core", "api", "store"}


def test_declared_containers_hold_their_components_and_flows_meet_them_on_their_rows(repo):
    c = compare(repo, "HEAD", ".")
    c.cfg = merge(c.cfg, {"actors": [{"id": "ops", "label": "Operators", "uses": ["api:HTTPS"]}]})
    for comp in c.cfg["components"]:
        if comp["id"] in ("core", "store"):
            comp["group"] = "Backend"
    m = architecture.system(c, agent="store")
    nodes = {n["id"]: n for n in m["nodes"]}
    box = nodes["g:Backend"]
    assert [i["id"] for i in box["items"]] == ["core", "store"] and box["agent"]
    assert nodes["a:ops"]["title"] == "Operators"
    flow = next(e for e in m["edges"] if e["from"] == "api" and e["to"] == "g:Backend")
    assert flow["to_item"] == "core"                       # API uses Core alone: the line meets Core's row
    g = canvas.layered(m["nodes"], m["edges"], m["columns"], width=140)
    assert "b:core" in {b["id"] for b in g.blocks} and "e:api>g:Backend" in g.lights["b:core"]


def _bp(**over):
    base = {"ref": "x", "components": {}, "flows": {}, "api": {}, "routes": {}, "externals": {}, "dormant": {},
            "internal": {}, "env": {}, "containers": {}, "container_labels": {}, "tables": {}, "store": "postgres",
            "migrations": [], "tests": {}, "uses": {}}
    return {**base, **over}


def _comp(label, kind):
    return {"label": label, "kind": kind, "layer": "", "order": 0, "container": "", "declared": True, "files": 1,
            "lines": 10, "hub": False, "quiet": False}


def _reading(a, b):
    cfg = merge(DEFAULTS, {})
    return design.Reading(SimpleNamespace(before=a, after=b, cfg=cfg, diff=diff(a, b)))


def test_design_notes_read_a_change_as_a_decision_with_its_trade_off():
    comps = {"web": _comp("Web app", "ui"), "api": _comp("API", "interface"), "core": _comp("Core", "logic"),
             "db": _comp("Store", "data"), "util": _comp("Utils", "logic")}
    a = _bp(components=comps, flows={"api>core": 1, "core>db": 1}, api={"web>api": 2},
            routes={"GET /api/orders": "api", "DELETE /api/orders/{id}": "api"},
            uses={"web>api": ["/api/orders/{id}"]})
    b = _bp(components=comps, flows={"api>core": 1, "core>db": 1, "db>core": 1, "db>util": 1}, api={"web>api": 2},
            routes={"GET /api/orders": "api"}, uses={"web>api": ["/api/orders/{id}"]})
    notes = _reading(a, b).notes()
    top = notes[0]
    assert top["text"] == "API no longer answers DELETE /api/orders/{id}" and top["weight"] == 10
    assert "Web app still calls it" in top["why"] and top["ask"]
    texts = {n["text"]: n for n in notes}
    cycle = next(n for t, n in texts.items() if "depend on each other" in t)
    assert "a cycle" in cycle["why"] and cycle["weight"] >= 8
    upward = next(n for t, n in texts.items() if t.startswith("Store (data access) now depends on Utils (logic)"))
    assert "points up the layers" in upward["why"]


def test_a_new_outside_system_in_a_request_path_says_what_its_failures_do():
    comps = {"api": _comp("API", "interface"), "pay": _comp("Payments", "logic")}
    a = _bp(components=comps, flows={"api>pay": 1})
    b = _bp(components=comps, flows={"api>pay": 1},
            externals={"stripe": {"label": "Stripe", "shape": "", "used_by": ["pay"]}})
    r = _reading(a, b)
    note = next(n for n in r.notes() if n["text"] == "Payments now talks to Stripe")
    assert "inside requests to API" in note["why"] and "slow or down" in note["ask"] and note["weight"] == 7
    shape = r.shape("pay")
    assert shape["entries"] == ["api"] and shape["outside"] == ["x:stripe"]


def test_lines_join_boxes_and_a_picked_box_lights_its_ins_and_outs():
    nodes = [{"id": "a", "col": "one", "title": "Alpha", "sub": "", "status": "same", "type": "component"},
             {"id": "b", "col": "two", "title": "Beta", "sub": "", "status": "added", "type": "component"},
             {"id": "c", "col": "two", "title": "Gamma", "sub": "", "status": "same", "type": "component"}]
    edges = [{"from": "a", "to": "b", "kind": "call", "status": "added", "label": "HTTP"},
             {"from": "a", "to": "c", "kind": "call", "status": "same"},
             {"from": "b", "to": "c", "kind": "call", "status": "same"}]
    g = canvas.layered(nodes, edges, [("one", "One"), ("two", "Two")], width=80)
    rows = ["".join(r[0] for r in row) for row in g.rows()]
    text = "\n".join(rows)
    assert text.count("▸") == 2 and "◂" in text and "├" in text and "HTTP" in text
    assert set(g.lights["b:a"]) == {"e:a>b", "e:a>c", "b:b", "b:c"}
    assert set(g.lights["b:c"]) == {"e:a>c", "e:b>c", "b:a", "b:b"}
    for blk in g.blocks:                                   # nothing drawn through a box
        inner = [rows[y][blk["x"] + 1: blk["x"] + blk["w"] - 1] for y in range(blk["y"] + 1, blk["y"] + blk["h"] - 1)]
        assert not any(ch in line for line in inner for ch in "│─┼┬┴▸◂")
    added = [r for row in g.rows() for r in row if len(r) == 3 and "e:a>b" in r[2].split()]
    assert added and all(r[1] == "added" for r in added)


def test_a_technology_marked_new_to_you_stays_marked_across_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert profile.learning() == set()
    assert profile.toggle("fastapi") is True and profile.learning() == {"fastapi"}
    assert profile.toggle("fastapi") is False and profile.learning() == set()
    assert (tmp_path / "system-diff" / "profile.json").is_file()


def test_a_notice_claude_code_sends_as_a_prompt_starts_no_turn(repo):
    from system_diff import agent
    from system_diff.compare import turn_sha
    agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "UserPromptSubmit",
                               "prompt": "<task-notification> <task-id>a1</task-id> done"}), emit=lambda _s: None)
    assert not turn_sha(repo) and not any(e["event"] == "prompt" for e in activity.events(repo))
    agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "add refunds"}),
                   emit=lambda _s: None)
    assert turn_sha(repo) and [e["text"] for e in activity.events(repo)] == ["add refunds"]


def test_learn_names_the_principle_a_part_s_place_calls_for(repo):
    r = design.Reading(compare(repo, "HEAD", "."))
    assert r.principle("api")[0] == "an edge of the system"
    assert r.principle("store")[0] == "an adapter" and "PostgreSQL" in r.principle("store")[1]
    assert r.principle("x:postgres")[0] == "outside the system"


def test_many_changes_are_told_a_part_at_a_time():
    comps = {"api": _comp("API", "interface"), "core": _comp("Core", "logic"), "db": _comp("Store", "data")}
    a = _bp(components=comps)
    b = _bp(components=comps, flows={"api>core": 1, "api>db": 1}, routes={"GET /health": "api", "POST /refund": "api"})
    told = design.summarize(_reading(a, b).notes())
    assert len(told) == 1 and told[0]["count"] == 4
    assert told[0]["text"] == "API now uses Store and Core; now answers POST /refund and GET /health"
    assert told[0]["why"].startswith("it creates or acts")          # the weightiest change's reason
    new = design.summarize(_reading(_bp(components={"api": comps["api"]}), b).notes())
    assert any(t["text"].startswith("New part Core") for t in new)
