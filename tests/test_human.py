"""What a person should look at first (human.decisions), whether it is what they asked for (human.intent), and how
the pane and the web page lead with them: WORTH A LOOK, the intent line, hover cards."""
from __future__ import annotations

import html
import json
import re

from system_diff import activity, agents, human, pane
from system_diff.extract import Code

from conftest import FIRST, SECOND

REFUND = ('from fastapi import APIRouter\nrouter = APIRouter()\n\n\n@router.post("/api/orders/{id}/refund")\n'
          'def refund(id):\n    return "ok"\n')
MAIL = FIRST["docker-compose.yml"].replace("DATABASE_URL: postgres://db\n",
                                           "DATABASE_URL: postgres://db\n      SMTP_HOST: mail\n")


def _change(repo, files, prompt="let people refund an order"):
    """An outside system (Core now talks to the mail server), an endpoint, and code changed inside Core."""
    activity.record(repo, {"event": "prompt", "text": prompt})
    files(repo, {"app/api/refund.py": REFUND, "docker-compose.yml": MAIL,
                 "app/core/rules.py": SECOND["app/core/rules.py"] + "\n\ndef more():\n    return 1\n"})
    for path in ("app/api/refund.py", "app/core/rules.py"):
        activity.record(repo, {"event": "edit", "path": path})
    return pane.compare(repo, "checkpoint", ".")


def _decisions(repo, c):
    return human.decisions(c, activity.events(repo, 200), Code(c.cfg))


def test_changes_are_ranked_by_consequence(repo, files):
    c = _change(repo, files)
    found = _decisions(repo, c)
    kinds = [d["kind"] for d in found]
    assert kinds.index("outside") < kinds.index("entry") < kinds.index("internals")
    first = found[0]
    assert first["title"] == "Claude made Core talk to Mail server"
    assert "outside dependency" in first["why"] and first["parts"] == ["core", "x:mail"]
    assert first["request"] == "let people refund an order" and first["when"] and first["look"]
    endpoint = next(d for d in found if d["kind"] == "entry")
    assert endpoint["title"] == "Claude added the endpoint POST /api/orders/{id}/refund to API"
    assert endpoint["parts"] == ["api"] and "changes state" in endpoint["why"]
    inside = next(d for d in found if d["kind"] == "internals")
    assert not inside["look"] and inside["impact"] < endpoint["impact"] < first["impact"]
    for d in found:
        assert {"id", "kind", "subject", "title", "why", "impact", "parts", "request", "when"} <= set(d)
        assert "question" not in d and "ask" not in d                          # it shows; it never asks for you


def test_ids_are_stable_while_the_change_stands(repo, files):
    c = _change(repo, files)
    before = {d["title"]: d["id"] for d in _decisions(repo, c)}
    files(repo, {"app/db/repo.py": FIRST["app/db/repo.py"] + "\n# more\n"})   # something else changes too
    activity.record(repo, {"event": "edit", "path": "app/db/repo.py"})
    after = {d["title"]: d["id"] for d in _decisions(repo, pane.compare(repo, "checkpoint", "."))}
    for title in ("Claude made Core talk to Mail server",
                  "Claude added the endpoint POST /api/orders/{id}/refund to API"):
        assert before[title] == after[title]
    assert len(set(before.values())) == len(before)
    assert human.decision_id("route", "add", "POST /x") == human.decision_id("route", "add", "POST /x")
    assert human.decision_id("route", "add", "POST /x") != human.decision_id("route", "remove", "POST /x")


def test_parts_the_request_does_not_name_are_marked(repo, files):
    c = _change(repo, files, prompt="add a refund endpoint to the api")
    it = human.intent(c, activity.events(repo, 200))
    assert it["requests"] == ["add a refund endpoint to the api"] and it["structural"]
    assert "api" in it["changed"] and "core" in it["changed"]
    assert "api" not in it["unnamed"] and "core" in it["unnamed"]          # named by its id; Core is not
    line = "".join(t for t, _s in human.intent_line(it))
    assert line.startswith("You asked: add a refund endpoint to the api")
    assert "changed: API" in line and "not named in your request: Core" in line
    assert not any(w in line.lower() for w in ("wrong", "drift", "should"))   # a fact, never a verdict


def test_a_request_that_changed_nothing_structural_says_so(repo, files):
    activity.record(repo, {"event": "prompt", "text": "fix a typo"})
    activity.record(repo, {"event": "stop"})
    activity.record(repo, {"event": "prompt", "text": "tidy the rules"})            # a new turn: only this one
    files(repo, {"app/core/rules.py": SECOND["app/core/rules.py"] + "\n# tidy\n"})
    c = pane.compare(repo, "checkpoint", ".")
    it = human.intent(c, activity.events(repo, 200))
    assert it["requests"] == ["tidy the rules"] and not it["structural"]
    line = "".join(t for t, _s in human.intent_line(it))
    assert "changed nothing in the system's structure" in line
    assert "not yet" not in line and "structure yet" in "".join(t for t, _s in human.intent_line(it, working=True))


def _page(repo, width=100):
    out = pane.show(repo, width, "now", zoom=0, take=False)
    return out, ["".join(run[0] for run in row) for row in out["rows"]]


def test_now_leads_with_worth_a_look(repo, files):
    _change(repo, files)
    out, text = _page(repo)
    headings = [t for row, t in zip(out["rows"], text) if row and row[0][1] in ("heading", "attention")]
    assert headings[0].lstrip("▲ ").startswith("WORTH A LOOK")
    look = next(i for i, t in enumerate(text) if "WORTH A LOOK" in t)
    assert text[look + 1].endswith("Claude made Core talk to Mail server")
    assert "Claude added the endpoint POST /api/orders/{id}/refund to API" in text[look + 2]
    assert not any("inside" in t and "Claude changed code" in t for t in text[look:look + 6])   # internals: Review
    picks = [b for b in out["blocks"] if b["id"].startswith("w:")]
    assert picks and all(b["slot"][1] == 2 for b in picks)
    mail = picks[0]["id"]
    said = "".join(r[0] for row in out["details"][mail] for r in row)
    assert "outside dependency" in said and "touches Core and Mail server" in said
    assert {"b:core", "b:x:mail"} <= set(out["lights"][mail])                     # lit to its parts in the diagram
    asked = next(t for t in text if t.startswith("You asked: let people refund an order"))
    assert "not named in your request" in asked
    assert text.index(asked) < next(i for i, t in enumerate(text) if t.startswith("SYSTEM"))
    assert not any("Ask Claude" in t for t in text[look:look + 8])


def test_now_fits_a_narrow_pane_and_says_when_there_is_nothing(repo, files):
    _change(repo, files)
    out, text = _page(repo, 60)
    look = next(i for i, t in enumerate(text) if "WORTH A LOOK" in t)
    assert all(len(t) <= 60 for t in text[:look + 8])
    plain, text = _page_fresh(repo)
    assert any(t.startswith("WORTH A LOOK  nothing") for t in text)


def _page_fresh(repo):
    from conftest import git_
    git_(repo, "add", "-A")
    git_(repo, "commit", "-q", "-m", "done")
    return _page(repo)


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


def test_the_web_page_gets_a_hover_card_for_what_it_renders(repo, files):
    agents.plan(repo, pane.compare(repo, "HEAD", "."), "refunds")
    agents.tie(repo, "a1", "api")
    _change(repo, files)
    activity.record(repo, {"event": "edit", "path": "app/api/refund.py", "agent": "a1"})
    agents.publish(repo, "all", "refund endpoint added", sender="api")
    for view in ("now", "review", "agents"):
        web = pane.web_view(repo, view)
        json.dumps(web)
        boxes, items = _ids_of(web)
        assert web["hover"], view
        for hid, card in web["hover"].items():
            assert hid in boxes or hid in items, (view, hid)
            assert card["title"] and card["lines"] and all(isinstance(x, str) for x in card["lines"])
        assert {b for b in boxes if b.startswith("b:")} <= set(web["hover"]), view   # every box has its card
    now = pane.web_view(repo, "now")
    look = next(s for s in now["sections"] if s["title"] == "Worth a look")
    first = look["items"][0]
    assert first["text"] == "Claude made Core talk to Mail server" and first["ask"] == ""
    card = now["hover"][first["id"]]
    assert any(x.startswith("Why it matters") for x in card["lines"])
    assert any("Touches Core and Mail server" in x for x in card["lines"])
    assert any("let people refund an order" in x for x in card["lines"])
    core = now["hover"]["b:core"]["lines"]
    assert any("Claude made Core talk to Mail server" in x for x in core) and any("Edited by" in x for x in core)
    assert set(now["diagram"]["lights"][first["id"]]) >= {"b:core", "b:x:mail"}
    review = pane.web_view(repo, "review")
    ranked = review["sections"][0]["items"]
    assert ranked[0]["text"] == "Claude made Core talk to Mail server" and len(ranked) >= 3
    team = pane.web_view(repo, "agents")["hover"]
    api = team["ag:api"]["lines"]
    assert any(x.startswith("Edited API") for x in api) and any("refund endpoint added" in x for x in api)

