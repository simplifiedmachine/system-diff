"""The person's objectives where they look: OBJECTIVES on the Now page (a block each: x clears it, lit to its parts),
changes that touch one marked in WORTH A LOOK, the agents' checks on the Agents view, the web page's items and hover
cards, and every brief of a team plan."""
from __future__ import annotations

import html
import json
import re

from system_diff import activity, agents, objectives, pane

from conftest import FIRST, SECOND

MAIL = FIRST["docker-compose.yml"].replace("DATABASE_URL: postgres://db\n",
                                           "DATABASE_URL: postgres://db\n      SMTP_HOST: mail\n")
OBJS = [
    {"id": "mail-optional", "text": "Keep the mail server optional", "evidence": ["mail must stay optional"],
     "count": 4, "last": "2026-11-12T10:00:00Z", "parts": ["core"], "check": "Core cannot start without mail",
     "since": "2026-11-10T10:00:00Z"},
    {"id": "windows", "text": "Must work on Windows", "evidence": ["it has to run on windows too"], "count": 3,
     "last": "2026-11-12T09:00:00Z", "parts": [], "check": "a path or a shell only Unix has"},
]


def _keep(repo, objs=OBJS):
    data = objectives.load(repo)
    data["objectives"] = [dict(o) for o in objs]
    objectives.save(repo, data)


def _change(repo, files):
    activity.record(repo, {"event": "prompt", "text": "send a mail when an order is refunded"})
    files(repo, {"docker-compose.yml": MAIL,
                 "app/core/rules.py": SECOND["app/core/rules.py"] + "\n\ndef more():\n    return 1\n"})
    activity.record(repo, {"event": "edit", "path": "app/core/rules.py"})


def _page(repo, width=100, view="now"):
    out = pane.show(repo, width, view, zoom=0, take=False)
    return out, ["".join(run[0] for run in row) for row in out["rows"]]


def test_now_shows_the_objectives_after_worth_a_look(repo, files):
    _keep(repo)
    objectives.record_check(repo, "windows", "tests", "broken", "a path with a slash")
    out, text = _page(repo)
    look = next(i for i, t in enumerate(text) if "WORTH A LOOK" in t)
    head = next(i for i, t in enumerate(text) if t.startswith("OBJECTIVES"))
    assert look < head < next(i for i, t in enumerate(text) if t.startswith("SYSTEM"))
    mail = next(t for t in text if "Keep the mail server optional" in t)
    assert "asked 4×, last" in mail
    win = next(t for t in text if "Must work on Windows" in t)
    assert "✗ broken · tests" in win
    blocks = {b["id"]: b for b in out["blocks"] if b["id"].startswith("o:")}
    assert set(blocks) == {"o:mail-optional", "o:windows"}
    assert blocks["o:mail-optional"]["x"] == {"clear_objective": "mail-optional"}
    assert "b:core" in out["lights"]["o:mail-optional"]
    said = "".join(r[0] for row in out["details"]["o:mail-optional"] for r in row)
    assert "breaks if: Core cannot start without mail" in said and '"mail must stay optional"' in said
    row = out["rows"][text.index(win)]
    assert any(r[0].startswith("✗") and r[1] == "removed" for r in row)


def test_no_objectives_no_section(repo, files):
    out, text = _page(repo)
    assert not any(t.startswith("OBJECTIVES") for t in text)
    assert not any(b["id"].startswith("o:") for b in out["blocks"])


def test_a_narrow_pane_fits(repo, files):
    _keep(repo)
    objectives.record_check(repo, "mail-optional", "engine", "holds")
    _change(repo, files)
    out, text = _page(repo, 60)
    head = next(i for i, t in enumerate(text) if t.startswith("OBJECTIVES"))
    assert all(len(t) <= 60 for t in text[:head + 4])
    assert any("✓ holds" in t for t in text[head:head + 3])


def test_a_change_touching_an_objective_says_so(repo, files):
    _keep(repo)
    _change(repo, files)
    out, text = _page(repo)
    picks = [b["id"] for b in out["blocks"] if b["id"].startswith("w:")]
    said = {bid: "".join(r[0] for row in out["details"][bid] for r in row) for bid in picks}
    touched = [bid for bid, s in said.items() if "touches objective: Keep the mail server optional" in s]
    assert touched
    web = pane.web_view(repo, "now")
    look = next(s for s in web["sections"] if s["title"] == "Worth a look")
    item = next(i for i in look["items"] if i["id"] in touched)
    assert item["objectives"] == ["o:mail-optional"] and "touches objective" in item["why"]
    assert any(x.startswith("Touches objective: Keep the mail server optional")
               for x in web["hover"][item["id"]]["lines"])


REFUND = ('from fastapi import APIRouter\nrouter = APIRouter()\n\n\n@router.post("/api/orders/{id}/refund")\n'
          'def refund(id):\n    return "ok"\n')


def test_a_touched_change_ranks_first_in_its_tier(repo, files):
    from system_diff import human
    from system_diff.extract import Code
    files(repo, {"app/api/refund.py": REFUND + '\n\n@router.get("/api/orders/{id}/receipt")\ndef receipt(id):\n'
                                               '    return "ok"\n'})
    c = pane.compare(repo, "checkpoint", ".")
    decs = human.decisions(c, [], Code(c.cfg))
    entry = [d["title"] for d in decs if d["kind"] == "entry"]
    assert len(entry) == 2
    word = "refund" if "refund" in entry[1] else "receipt"            # the one ranked second, unmarked
    marked = human.mark_objectives([dict(d) for d in decs], c, [{"id": "o", "text": f"the {word} stays safe",
                                                                 "parts": []}])
    entry = [d for d in marked if d["kind"] == "entry"]
    assert word in entry[0]["title"] and entry[0]["objectives"] == ["o"] and entry[0]["touches"][0]["reason"]
    assert entry[1]["objectives"] == []


def test_agents_view_lists_the_checks_broken_first(repo, files):
    _keep(repo)
    agents.plan(repo, pane.compare(repo, "HEAD", "."), "refunds")
    objectives.record_check(repo, "mail-optional", "engine", "holds", "mail is lazy")
    objectives.record_check(repo, "windows", "tests", "broken", "a path with a slash")
    objectives.record_check(repo, "windows", "engine", "holds")
    out, text = _page(repo, 100, "agents")
    head = next(i for i, t in enumerate(text) if t.startswith("OBJECTIVES"))
    after = text[head + 1:]
    assert after[0].endswith("Must work on Windows")                 # broken first
    assert "tests broken" in after[1] and "a path with a slash" in after[1]
    assert "engine holds" in after[2]
    assert any("Keep the mail server optional" in t for t in after[3:5])
    web = pane.web_view(repo, "agents")
    sec = next(s for s in web["sections"] if s["title"] == "Objectives")
    assert [i["id"] for i in sec["items"]] == ["o:windows", "o:mail-optional"]
    assert sec["items"][0]["checks"][0]["agent"] == "tests" and sec["items"][0]["status"] == "bad"
    assert "ag:engine" not in web["hover"] or any("Checked" in x for x in web["hover"]["ag:engine"]["lines"])


def _ids_of(web):
    svg = web["diagram"]["svg"]
    boxes = {html.unescape(i) for i in re.findall(r'data-id="([^"]+)"', svg)}
    items = set()
    for s in web["sections"]:
        for item in s["items"]:
            if item.get("id"):
                items.add(item["id"])
            if s["kind"] == "lanes":
                items.add("ag:" + item["name"])
    return boxes, items


def test_web_items_and_hover_cards(repo, files):
    _keep(repo)
    agents.plan(repo, pane.compare(repo, "HEAD", "."), "refunds")
    agents.tie(repo, "a1", "core")
    _change(repo, files)
    activity.record(repo, {"event": "prompt", "text": "mail must stay optional, please"})
    activity.record(repo, {"event": "edit", "path": "app/core/rules.py", "agent": "a1"})
    objectives.record_check(repo, "mail-optional", "core", "broken", "imports smtplib at start")
    for view in ("now", "agents"):
        web = pane.web_view(repo, view)
        json.dumps(web)
        boxes, items = _ids_of(web)
        assert {"o:mail-optional", "o:windows"} <= items, view
        for hid in web["hover"]:
            assert hid in boxes or hid in items, (view, hid)
        card = web["hover"]["o:mail-optional"]
        assert card["title"] == "Keep the mail server optional"
        lines = card["lines"]
        assert any(x.startswith('You said: "mail must stay optional" (') for x in lines)
        assert "Breaks if: Core cannot start without mail" in lines
        assert any(x.startswith("core: ✗ broken") and "imports smtplib" in x for x in lines)
        assert "b:core" in web["diagram"]["lights"]["o:mail-optional"]
    team = pane.web_view(repo, "agents")
    assert any('Checked "Keep the mail server optional": ✗ broken' in x for x in team["hover"]["ag:core"]["lines"])
    lane = next(i for s in team["sections"] if s["kind"] == "lanes" for i in s["items"] if i["name"] == "core")
    assert lane["checks"][0]["id"] == "o:mail-optional"


def test_plan_briefs_carry_the_objectives(repo, files):
    c = pane.compare(repo, "HEAD", ".")
    plain = agents.plan(repo, c, "refunds")
    assert all("check_objective" not in g["brief"] for g in plain["agents"])
    _keep(repo)
    planned = agents.plan(repo, c, "refunds")
    assert planned["agents"]
    for g in planned["agents"]:
        assert "check_objective" in g["brief"] and "mail-optional: Keep the mail server optional" in g["brief"]
