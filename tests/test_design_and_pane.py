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
    assert main(["--repo", str(repo), "panel", "--width", "100", "--protocol", "2", "--zoom", "0", *args]) == 0
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
    assert text[0] == "● Claude is working  ✎ Editing API"
    look = next(i for i, t in enumerate(text) if t.startswith("WORTH A LOOK"))         # a new way in, first
    assert text[look + 1] == " + Claude added the endpoint POST /api/orders/{id}/refund to API"
    assert any(t.startswith("You asked: let people refund an order") for t in text[look:look + 6])
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


def test_review_and_topics(repo, files, capsys):
    files(repo, {"app/api/health.py": HEALTH_ROUTE})
    review, text = page(capsys, repo, "--view", "review")
    assert review["view"] == "review" and {"b:api", "b:x:postgres"} <= {b["id"] for b in review["blocks"]}
    assert any(t.startswith("SYSTEM SINCE") for t in text) and any(t.startswith("EVERY CHANGE SINCE") for t in text)
    assert "+ route GET /health — GET /health   [new-interface]" in text or any(
        t.startswith("+ route GET /health") for t in text)                     # the exact list too
    for old in ("map", "changes"):                          # the two tabs it replaces still open it
        assert page(capsys, repo, "--view", old)[0]["view"] == "review"
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


def test_a_team_plan_splits_work_by_block_and_the_bus_carries_changes_between_agents(repo, files):
    from system_diff import agent, agents
    plan = agents.plan(repo, compare(repo, "HEAD", "."), "make the API and the store faster")
    names = {a["name"]: a for a in plan["agents"]}
    assert set(names) == {"api", "store"} and "launch" in plan["how"].lower()
    assert names["api"]["blocks"] == ["api"] and "store" in names["store"]["blocks"]
    assert "claim" in names["api"]["brief"] and agents.owner(repo, "api") == "api"
    agents.subscribe(repo, "web", ["api"])                    # the front end's agent follows the API
    agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "go"}),
                   emit=lambda _s: None)
    files(repo, {"app/api/health.py": HEALTH_ROUTE.replace("rules.everything()", '"ok"')})
    said: list = []
    agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "PostToolUse", "tool_name": "Write",
                               "agent_id": "a1", "tool_input": {"file_path": str(repo / "app/api/health.py")}}),
                   emit=said.append)
    assert agents.name_of(repo, "a1") == "api"                 # it edited a block claimed for "api": that is it
    new = [m for m in agents.inbox(repo, "web") if m["kind"] == "change"]
    assert [m["text"] for m in new] == ["API now answers GET /health"] and new[0]["from"] == "api"
    assert agents.inbox(repo, "web") == []                     # read once
    agents.publish(repo, "agent:api", "please keep GET /api/orders", sender="web")
    files(repo, {"app/api/health.py": HEALTH_ROUTE.replace("rules.everything()", '"fine"')})
    said.clear()
    agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "PostToolUse", "tool_name": "Edit",
                               "agent_id": "a1", "tool_input": {"file_path": str(repo / "app/api/health.py")}}),
                   emit=said.append)
    context = json.loads(said[-1])["hookSpecificOutput"]["additionalContext"]
    assert "web on agent:api: please keep GET /api/orders" in context   # delivered with its next tool result


def test_the_agents_tab_shows_each_agent_and_warns_when_two_edit_one_block(repo, capsys):
    from system_diff import agents
    agents.plan(repo, compare(repo, "HEAD", "."), "make the API and the store faster")
    agents.tie(repo, "a1", "api")
    agents.tie(repo, "a2", "store")
    activity.record(repo, {"event": "prompt", "text": "go"})
    activity.record(repo, {"event": "edit", "path": "app/api/routes.py", "agent": "a1"})
    activity.record(repo, {"event": "edit", "path": "app/api/routes.py", "agent": "a2"})
    out, text = page(capsys, repo, "--view", "agents")
    assert any(t.startswith("THE TEAM  plan: make the API") for t in text)
    assert any("api and store all edit API" in t for t in text)
    assert "● API  @api @st" in "\n".join(text)               # who works where, on the diagram


def test_messages_reach_an_agent_with_its_next_tool_result_of_any_kind_once(repo):
    from system_diff import agent, agents
    from system_diff.sources import git_dir

    def tool(name, **given):
        said: list = []
        agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "PostToolUse", "tool_name": name,
                                   "agent_id": "a1", "tool_input": given}), emit=said.append)
        return [json.loads(s) for s in said]

    bus = git_dir(repo) / agents.BUS
    assert not bus.exists() and tool("Bash", command="ls") == []      # no bus: nothing said, nothing written
    assert not bus.exists()
    agents.tie(repo, "a1", "api")
    agents.publish(repo, "agent:api", "please keep GET /api/orders", sender="web")
    out = tool("Bash", command="ls")                                  # not a file tool: it still gets it
    assert "web on agent:api: please keep GET /api/orders" in out[0]["hookSpecificOutput"]["additionalContext"]
    assert out[0]["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert tool("Bash", command="ls") == []                           # once only
    agents.publish(repo, "agent:api", "store moved to app/db", sender="store")
    out = tool("Read", file_path=str(repo / "app/core/rules.py"))
    assert "store on agent:api: store moved to app/db" in out[0]["hookSpecificOutput"]["additionalContext"]
    assert tool("Read", file_path=str(repo / "app/core/rules.py")) == []


def test_one_agent_editing_two_areas_in_a_turn_is_told_once_to_plan_a_team(repo, files):
    from system_diff import agent

    def prompt():
        agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "go"}),
                       emit=lambda _s: None)

    def edit(path, text, **who):
        files(repo, {path: text})
        said: list = []
        agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "PostToolUse", "tool_name": "Edit", **who,
                                   "tool_input": {"file_path": str(repo / path)}}), emit=said.append)
        outs = [json.loads(s) for s in said]
        warns = " ".join(o.get("systemMessage", "") for o in outs)
        context = " ".join(o.get("hookSpecificOutput", {}).get("additionalContext", "") for o in outs)
        return warns, context

    routes, rules = "app/api/routes.py", "app/core/rules.py"
    prompt()
    warns, context = edit(routes, (repo / routes).read_text() + "\n# one\n")
    assert "areas this turn" not in warns + context              # one area so far
    warns, context = edit(rules, (repo / rules).read_text() + "\n# one\n")
    assert "main has edited blocks in 2 areas this turn" in warns and "plan_agents" in warns
    assert "main has edited blocks in 2 areas this turn" in context and "plan_agents" in context
    warns, context = edit("app/db/repo.py", (repo / "app/db/repo.py").read_text() + "\n# one\n")
    assert "areas this turn" not in warns + context              # once a turn
    prompt()
    edit(routes, (repo / routes).read_text() + "# two\n")
    warns, context = edit(rules, (repo / rules).read_text() + "# two\n")
    assert "areas this turn" in warns and "areas this turn" in context    # a new turn may warn again
    prompt()
    for text in ("# a\n", "# b\n"):                            # a subagent staying in its one area
        warns, context = edit(routes, (repo / routes).read_text() + text, agent_id="a1")
        assert "areas this turn" not in warns and "a1 has edited" not in context  # main's warning may reach it by bus


def test_a_narrow_pane_gets_compact_diagrams_unless_asked_otherwise(repo, capsys):
    narrow = json.loads(_run(capsys, repo, "--width", "90"))
    wide = json.loads(_run(capsys, repo, "--width", "140"))
    assert narrow["zoom"] == 1 and wide["zoom"] == 0
    assert json.loads(_run(capsys, repo, "--width", "90", "--zoom", "2"))["zoom"] == 2


def _run(capsys, repo, *args):
    assert main(["--repo", str(repo), "panel", "--protocol", "2", *args]) == 0
    return capsys.readouterr().out


def test_what_an_agent_read_before_it_had_a_name_stays_read(repo):
    from system_diff import agents
    agents.publish(repo, "all", "hello", sender="main")
    assert len(agents.inbox(repo, "agent-a1b2c3")) == 1
    agents.tie(repo, "a1b2c3d4", "api")
    assert agents.inbox(repo, "api") == []


def test_a_box_an_agent_owns_draws_in_that_agents_colour():
    nodes = [{"id": "a", "col": "one", "title": "Alpha", "sub": "", "status": "same", "type": "component", "owner": 0},
             {"id": "b", "col": "two", "title": "Beta", "sub": "", "status": "changed", "type": "component", "owner": 1},
             {"id": "c", "col": "two", "title": "Gamma", "sub": "", "status": "same", "type": "component"}]
    g = canvas.layered(nodes, [], [("one", "One"), ("two", "Two")], width=80)
    runs = [r for row in g.rows() for r in row if len(r) == 3]
    border = {bid: {r[1] for r in runs if r[2] == bid and set(r[0]) <= set("╭╮╰╯┌┐└┘─│ ")} for bid in ("b:a", "b:b", "b:c")}
    assert border["b:a"] == {"agent0"} and border["b:b"] == {"agent1"} and border["b:c"] == {"muted"}
    titles = {r[1] for r in runs if r[0].strip() in ("Alpha", "~ Beta", "~Beta")}
    assert "title agent0" in titles and any(t.endswith("agent1") for t in titles)


def test_each_agent_is_a_lane_that_lights_its_blocks_drawn_in_its_colour(repo, capsys):
    from system_diff import agents
    agents.plan(repo, compare(repo, "HEAD", "."), "make the API and the store faster")
    agents.tie(repo, "a1", "api")
    agents.tie(repo, "a2", "store")
    activity.record(repo, {"event": "prompt", "text": "go"})
    activity.record(repo, {"event": "edit", "path": "app/api/routes.py", "agent": "a1"})
    out, text = page(capsys, repo, "--view", "agents")
    ids = {b["id"] for b in out["blocks"]}
    assert {"ag:api", "ag:store"} <= ids
    assert "b:api" in out["lights"]["ag:api"]
    detail = "\n".join("".join(r[0] for r in row) for row in out["details"]["ag:api"])
    assert detail.startswith("owns ") and "edited API" in detail
    styles = {r[1] for row in out["rows"] for r in row}
    assert any(s.split()[-1] in ("agent0", "agent1", "agent2") for s in styles if s)   # legend and boxes coloured
    runs = [r for row in out["rows"] for r in row if len(r) == 3 and r[2] == "b:api"]
    assert any("agent" in r[1] for r in runs)                  # the API box drawn in its editor's colour


def test_a_part_picked_on_the_topics_tab_lights_and_names_the_technology_it_uses(repo, files, capsys):
    files(repo, {"app/api/health.py": HEALTH_ROUTE})
    topics, _text = page(capsys, repo, "--view", "topics")
    assert "t:fastapi" in topics["lights"]["b:api"]
    assert "t:fastapi" in topics["lights"] and "b:api" in topics["lights"]["t:fastapi"]   # chips still light parts
    detail = "".join(r[0] for row in topics["details"]["b:api"] for r in row)
    assert "uses " in detail and "FastAPI" in detail


def test_a_subagent_naming_itself_to_a_team_tool_gets_its_messages_by_that_name(repo):
    from system_diff import agent, agents
    agents.publish(repo, "agent:panes", "canvas draws owners now", sender="pictures")
    said: list = []
    agent.run_hook(json.dumps({"cwd": str(repo), "hook_event_name": "PostToolUse", "agent_id": "x9",
                               "tool_name": "mcp__system-diff__claim", "tool_input": {"agent": "panes", "blocks": []}}),
                   emit=said.append)
    assert agents.name_of(repo, "x9") == "panes"
    assert "pictures on agent:panes: canvas draws owners now" in json.loads(said[-1])["hookSpecificOutput"]["additionalContext"]


def test_the_agents_tab_shows_each_agent_live_in_its_lane(repo, capsys):
    from system_diff import agents
    agents.plan(repo, compare(repo, "HEAD", "."), "make the API and the store faster")
    agents.tie(repo, "a1", "api")
    activity.record(repo, {"event": "prompt", "text": "go"})
    activity.record(repo, {"event": "read", "path": "app/api/routes.py", "agent": "a1"})
    activity.record(repo, {"event": "edit", "path": "app/api/routes.py", "agent": "a1"})
    agents.publish(repo, "api", "API now answers GET /health", sender="api", kind="change")
    out, text = page(capsys, repo, "--view", "agents")
    live = text[next(i for i, t in enumerate(text) if t.startswith("LIVE")) + 1:]
    lane = next(t for t in live if t.startswith("api"))
    assert "▲" in lane and "● ✎ editing routes.py" in lane and "1/1 blocks · 1 changes" in lane
    assert any(t.startswith("store") and "○ idle" in t for t in live)


def test_review_without_a_checkpoint_starts_before_the_last_commit(repo, capsys):
    out, text = page(capsys, repo, "--view", "review")
    assert any(t.startswith("SYSTEM SINCE BEFORE THE LAST COMMIT") for t in text)
    assert any("no longer answers DELETE" in t or "DELETE /api/orders/{id}" in t for t in text)   # the last commit's


def test_a_new_plan_starts_a_clean_team_and_old_agents_have_nothing_pending(repo):
    from system_diff import agents
    agents.publish(repo, "all", "before any plan", sender="main")
    agents.inbox(repo, "main")
    agents.plan(repo, compare(repo, "HEAD", "."), "make the API and the store faster")
    agents.tie(repo, "a1", "api")
    agents.publish(repo, "all", "first team at work", sender="main")
    agents.publish(repo, "agent:main", "a note for main", sender="api")
    assert agents.pending(repo, "api") == 1
    agents.plan(repo, compare(repo, "HEAD", "."), "make the store faster")
    data = agents.state(repo)
    assert "main" in data["agents"] and data["agents"]["main"]["read"] >= 1
    assert data["ids"] == {} and data["warned"] == {}
    old = {"api", "store"} - {g["name"] for g in data["plan"]["agents"]}
    assert old == {"api"}
    new = [g["name"] for g in data["plan"]["agents"]][0]
    assert all(n not in data["agents"] for n in old)
    assert set(data["agents"]) == {"main"} | {g["name"] for g in data["plan"]["agents"]}
    assert set(data["claims"].values()) <= {g["name"] for g in data["plan"]["agents"]}
    for n in old:
        assert agents.pending(repo, n) == 0
    assert agents.pending(repo, "someone-else") == 0
    assert agents.pending(repo, new) == 0                       # nothing from before the plan
    agents.publish(repo, "all", "second team at work", sender="main")
    assert agents.pending(repo, new) == 1
    assert agents.pending(repo, new) == 1                       # nothing marked read
    texts = [m["text"] for m in agents.inbox(repo, new)]
    assert texts == ["second team at work"]
    agents.tie(repo, "b1", new)
    assert agents.name_of(repo, "b1") == new and agents.name_of(repo, "a1") != "api"


def test_the_page_carries_the_web_address_and_the_session_starts_the_web_page_unless_told_not_to(repo, capsys,
                                                                                                  monkeypatch):
    from system_diff import agent, watch
    out, _ = page(capsys, repo)
    assert "web" in out and isinstance(out["web"], str)
    started = []
    monkeypatch.setattr(watch, "start_detached", lambda root, port=8765: started.append(root) or None, raising=False)
    hook = {"CLAUDE_PROJECT_DIR": str(repo), "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}   # no terminal view to open
    start = {"cwd": str(repo), "hook_event_name": "SessionStart", "source": "startup"}
    assert agent.on_start(repo, start, env=hook) is None and started == [repo]
    (repo / "system-diff.toml").write_text((repo / "system-diff.toml").read_text() + "\n[web]\nstart = false\n"
                                           if (repo / "system-diff.toml").exists() else "[web]\nstart = false\n")
    started.clear()
    assert agent.on_start(repo, start, env=hook) is None and started == []


def test_what_the_web_page_sends_comes_back_once_in_the_page_and_the_outbox_is_emptied(repo):
    import json
    from system_diff import pane
    from system_diff.sources import git_dir
    assert pane.show(repo)["send"] == []                        # no outbox: nothing to send
    box = git_dir(repo) / "outbox.jsonl"
    box.write_text(json.dumps({"id": "1", "at": "2026-11-10T10:00:00", "text": "hi"}) + "\nnot json\n"
                   + json.dumps({"id": "2", "at": "2026-11-10T10:00:01", "text": "ünïcode ok"}, ensure_ascii=False)
                   + "\n", encoding="utf-8")
    assert pane.show(repo, view="review")["send"] == [{"id": "1", "text": "hi"}, {"id": "2", "text": "ünïcode ok"}]
    assert not box.exists() and not list(git_dir(repo).glob("outbox.jsonl*"))
    assert pane.show(repo, view="agents")["send"] == []         # taken once
    assert pane.take_outbox(repo) == []


def test_web_view_gives_each_view_as_structured_json_agreeing_with_the_pane(repo, files):
    import json
    from system_diff import agents, pane, pictures
    agents.plan(repo, compare(repo, "HEAD", "."), "make the API faster")
    agents.tie(repo, "a1", "api")
    activity.record(repo, {"event": "prompt", "text": "let people refund an order"})
    files(repo, {"app/api/refund.py": REFUND, "app/api/health.py": HEALTH_ROUTE})
    activity.record(repo, {"event": "edit", "path": "app/api/refund.py", "agent": "a1"})
    activity.record(repo, {"event": "change", "path": "app/api/refund.py", "source": "hook", "items": [
        {"text": "API now answers POST /api/orders/{id}/refund", "why": "it creates or acts", "sign": "+",
         "status": "added"}]})
    box = __import__("system_diff.sources", fromlist=["git_dir"]).git_dir(repo) / "outbox.jsonl"
    box.write_text(json.dumps({"id": "1", "text": "hi"}) + "\n", encoding="utf-8")
    kinds = {"notes": {"sign", "status", "text", "why", "ask"}, "steps": {"at", "kind", "sign", "text", "why", "file"},
             "lanes": {"name", "colour", "area", "live", "doing", "progress", "unread", "marks", "blocks"},
             "chips": {"key", "title", "learning", "stdlib", "layer", "parts", "role", "what", "link"},
             "lines": {"text", "style"}}
    for view in ("now", "review", "agents", "topics"):
        web = pane.web_view(repo, view)
        json.dumps(web)                                                         # all of it JSON
        assert {"view", "part", "title", "headline", "diagram", "sections", "web", "send"} <= set(web)
        assert web["view"] == view and web["send"] == []                         # never takes the outbox
        assert ("status" in web) == (view == "now")
        d = web["diagram"]
        assert {"svg", "title", "aside", "lights", "details", "enter", "pick"} <= set(d)
        if getattr(pictures, "architecture_svg", None):
            assert d["svg"].startswith("<svg")
        for s in web["sections"]:
            assert {"title", "aside", "kind", "items"} <= set(s) and s["kind"] in kinds
            for item in s["items"]:
                assert kinds[s["kind"]] <= set(item), (view, s["title"], item)
        shown = pane.show(repo, 100, view, zoom=0, take=False)
        for bid, lit in d["lights"].items():                                    # lights as the pane's
            assert sorted(lit) == sorted(shown["lights"][bid]), (view, bid)
        entered = {b["id"]: b["enter"]["zoom"] for b in shown["blocks"] if b.get("enter")}
        assert d["enter"] == entered and d["pick"] == shown["pick"]
    assert box.exists()                                                         # still there for the pane
    now = pane.web_view(repo, "now")
    assert now["status"]["prompt"] == "let people refund an order" and now["status"]["part"] == "api"
    happened = next(s for s in now["sections"] if s["title"] == "What just happened")
    assert happened["kind"] == "steps"
    assert happened["items"][0]["text"] == "API now answers POST /api/orders/{id}/refund"
    assert happened["items"][0]["file"] == "refund.py" and len(happened["items"][0]["at"]) == 5
    team = next(s for s in pane.web_view(repo, "agents")["sections"] if s["kind"] == "lanes")
    api = next(i for i in team["items"] if i["name"] == "api")
    assert "b:api" in api["blocks"] and any(m["kind"] == "edit" and 0 <= m["pos"] <= 1 for m in api["marks"])
    topics = pane.web_view(repo, "topics")
    fastapi = next(i for i in topics["sections"][0]["items"] if i["key"] == "fastapi")
    assert "b:api" in fastapi["parts"] and "t:fastapi" in topics["diagram"]["lights"]["b:api"]
    zoomed = pane.web_view(repo, "now", part="api")
    assert zoomed["part"] == "api" and zoomed["diagram"]["title"].startswith(("Inside", "Zoomed"))


def test_messages_in_the_same_millisecond_as_the_plan_still_count(repo, monkeypatch):
    """What is pending since a plan is counted by message number: on a fast machine the plan and the next message
    can share a millisecond (this made CI fail now and then)."""
    from system_diff import agents
    monkeypatch.setattr(agents.time, "time", lambda: 1_800_000_000.0)
    agents.plan(repo, compare(repo, "HEAD", "."), "make the store faster")
    new = [g["name"] for g in agents.state(repo)["plan"]["agents"]][0]
    assert agents.pending(repo, new) == 0
    agents.publish(repo, "all", "at work", sender="main")
    assert agents.pending(repo, new) == 1
