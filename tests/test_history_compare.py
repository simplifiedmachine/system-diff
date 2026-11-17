"""The History read as people read it, and any two points of it compared: each entry in one plain sentence of what the
system gained or lost (human.change_sentence; an LLM's better words written in the background, kept, and marked as
such), and the structural diff between two entries (or an entry and now) in the pane, on the web page and on the
command line (history.compare)."""
from __future__ import annotations

import html
import json
import os
import re

import pytest

from system_diff import cli, history, human, pane

from conftest import FIRST, git_, write


@pytest.fixture(autouse=True)
def no_recorder(monkeypatch):
    """No engine starts a recorder of its own, and no LLM is asked unless a test says so."""
    monkeypatch.setenv("SYSTEM_DIFF_HISTORY", "0")
    monkeypatch.delenv("SYSTEM_DIFF_LLM", raising=False)
    monkeypatch.delenv("SYSTEM_DIFF_HISTORY_JOBS", raising=False)


# ── the sentence, from the changes alone ──────────────────────────────────────

def ext(user: str, x: str, xid: str, db: bool = False, impact: int = 605, **more) -> dict:
    verb = "read and write" if db else "talk to"
    return {"id": f"d-{user}-{xid}", "entity": "external", "op": "add", "subject": f"{user.lower()}>{xid}",
            "names": [user, x], "label": f"{user} → {x}", "title": f"Claude made {user} {verb} {x}", "detail": [],
            "impact": impact, **more}


def change(entity: str, op: str, names, label: str = "", detail=(), impact: int = 300, title: str = "") -> dict:
    return {"id": f"d-{entity}-{op}-{'-'.join(names)}-{label}", "entity": entity, "op": op, "subject": label,
            "names": list(names), "label": label, "detail": list(detail), "impact": impact, "title": title}


def test_an_outside_system_is_said_by_what_it_is_for():
    assert human.outside_kind("aws-s3", "Amazon S3", "db") == "storage"
    assert human.outside_kind("minio", "MinIO", "db") == "storage"
    assert human.outside_kind("redis", "Redis", "db") == "db"
    assert human.outside_kind("elasticsearch", "Elasticsearch", "db") == "search"
    assert human.outside_kind("kafka", "Kafka", "") == "queue"
    assert human.outside_kind("mail", "Mail server", "") == "mail"
    assert human.outside_kind("aws-ses", "Amazon SES", "") == "mail"
    assert human.outside_kind("stripe", "Stripe", "") == ""
    assert human.outside_kind("thing", "Thing", "queue") == "queue"           # a shape the config declares
    say = human.change_sentence
    assert say([ext("Control", "Mail server", "mail")]) == "Control now sends email through Mail server"
    assert say([ext("Control", "SQLite", "sqlite", db=True)]) == "Control now keeps data in SQLite"
    assert say([ext("Worker", "Kafka", "kafka")]) == "Worker now sends messages through Kafka"
    assert say([ext("Flows", "Amazon S3", "aws-s3", outside="storage")]) == "Flows now stores files in Amazon S3"
    assert say([ext("Search", "Elasticsearch", "elasticsearch", db=True)]) == "Search now searches with Elasticsearch"
    assert say([ext("Billing", "Stripe", "stripe")]) == "Billing now talks to Stripe"
    gone = {**ext("Billing", "Stripe", "stripe"), "op": "remove"}
    assert say([gone]) == "Billing no longer talks to Stripe"


def test_changes_to_one_part_merge_into_one_clause():
    got = human.change_sentence([ext("Control", "Mail server", "mail", impact=606),
                                 ext("Control", "SQLite", "sqlite", db=True), ext("Control", "Redis", "redis", db=True)])
    assert got == "Control now sends email through Mail server and keeps data in SQLite and Redis"
    many = human.change_sentence([ext("Control", n, n.lower(), db=True) for n in ("SQLite", "Redis", "MongoDB")])
    assert many == "Control now keeps data in 3 stores"


def test_new_parts_endpoints_settings_and_dependencies():
    say = human.change_sentence
    agents = change("component", "add", ["Agents"], "Agents", impact=205)
    assert say([agents]) == "A new part, Agents"
    control = change("component", "add", ["Control"], "Control", impact=205)
    compose = change("component", "add", ["Gen Compose"], "Gen Compose", impact=205)
    sqlite = ext("Control", "SQLite", "sqlite", db=True)
    assert say([control, sqlite]) == "A new part, Control, that keeps data in SQLite"
    assert say([sqlite, control, compose]) == "New parts Control and Gen Compose; Control keeps data in SQLite"
    routes = [change("route", "add", ["API"], f"GET /api/orders/{p}", impact=405) for p in ("a", "b", "c")]
    assert say(routes) == "API now serves 3 new endpoints under /api/orders"
    assert say(routes[:1]) == "API now serves GET /api/orders/a"
    setting = change("settings", "change", ["Control"], "Control", ["DEVFLEET_BOARD"], impact=107)
    assert say([setting]) == "Control now reads DEVFLEET_BOARD (a new setting)"
    uses = [change("flow", "add", ["Jobs", n], f"jobs>{n}", impact=302, title=f"Claude made Jobs depend on {n}")
            for n in ("Core", "Store", "Mail", "Queue", "Cache")]
    assert say(uses[:1]) == "Jobs now uses Core"
    assert say(uses[:2]) == "Jobs now uses Core and Store"
    assert say(uses) == "Jobs now uses 5 other parts"
    cycle = change("flow", "add", ["Core", "Store"], "core>store", impact=308,
                   title="Claude made Core and Store depend on each other")
    assert say([cycle]) == "Core and Store now depend on each other"
    table = change("table", "change", ["PostgreSQL"], "orders", ["+ column note"], impact=505)
    assert say([table]) == "The table orders gains the column note"
    assert say([]) == "no change to the system's structure"


def test_what_does_not_fit_is_counted_as_smaller_changes():
    big = [ext("Control", "Mail server", "mail", impact=606), ext("Control", "SQLite", "sqlite", db=True),
           ext("Control", "Redis", "redis", db=True)]
    small = [change("settings", "change", [f"Part{i}"], f"Part{i}", [f"SETTING_{i}"], impact=100 + i)
             for i in range(6)]
    got = human.change_sentence(big + small, 110)
    assert got == "Control now sends email through Mail server and keeps data in SQLite and Redis; 6 smaller changes"
    short = human.change_sentence(big + small, 70)                  # less room: the first thing it gained, the rest counted
    assert short == "Control now sends email through Mail server; 8 more changes", short
    inside = change("component", "change", ["Core"], "Core", ["2 files"], impact=0)
    assert human.change_sentence([inside]) == "Code changed inside Core"


def test_the_commits_read_back_are_told_in_one_sentence(repo):
    history.backfill(repo)
    second, first = history.load(repo)
    said, by = history.sentence(second)
    assert by == "" and said.startswith("A new part, Jobs, that uses Core") and len(said) <= human.SENTENCE_MAX
    assert "Store keeps data in PostgreSQL" in history.sentence(first)[0]
    assert history.plain(second).endswith("→ " + said)
    text = history.text(repo, width=100)
    assert "it got" not in text and f"{second['id']} · " in text and "--compare" in text


# ── better words from an LLM, in the background, kept ─────────────────────────

def test_an_llm_writes_better_sentences_in_one_batch_and_they_are_kept(repo, monkeypatch):
    history.backfill(repo)
    asked = []

    def ask(user):
        asked.append(user)
        ids = [e["id"] for e in json.loads(user.split("\n", 1)[1])]
        return json.dumps({i: f"The system grew ({i[:4]})" for i in ids}), "claude-haiku (your Claude Code)"

    assert history.words(repo, ask=ask) == 2
    assert len(asked) == 1                                         # one question for the batch
    sent = asked[0]
    assert "Claude made Jobs depend on Core" in sent and "draft" in sent
    assert "def " not in sent and "import " not in sent and "APIRouter" not in sent   # structure, never code
    second, first = history.load(repo)
    assert history.sentence(second) == (f"The system grew ({second['id'][:4]})", "claude-haiku (your Claude Code)")
    assert history.words(repo, ask=ask) == 0 and len(asked) == 1   # kept: not asked again
    # shown, and marked as an LLM's, in the pane, its hover card and the web page
    page = pane.show(repo, 100, "history")
    text = "\n".join("".join(r[0] for r in row) for row in page["rows"])
    assert f"→ The system grew ({second['id'][:4]}) ✦" in text and "✦ a summary an LLM wrote" in text
    web = pane.web_view(repo, "history")
    item = web["sections"][0]["items"][0]
    assert item["said"].startswith("The system grew") and item["by"].startswith("claude-haiku")
    assert any("Summary written by claude-haiku" in x for x in web["hover"][item["id"]]["lines"])
    # an entry whose changes grew (a burst) needs new words: until then, its own sentence
    grown = {**second, "changes": second["changes"][:-1]}
    history._write(repo, [grown])
    again = history.find(repo, second["id"])
    assert history.sentence(again)[1] == "" and history.sentence(again)[0] == human.change_sentence(grown["changes"])


def test_without_an_llm_the_sentence_is_the_deterministic_one(repo, monkeypatch):
    history.backfill(repo)
    calls = []
    monkeypatch.setenv("SYSTEM_DIFF_LLM", "off")
    assert history.words(repo, ask=lambda u: calls.append(u) or ("{}", "x")) == 0 and calls == []
    assert history.start_words(repo) is False
    monkeypatch.delenv("SYSTEM_DIFF_LLM")
    assert history.words(repo, ask=lambda u: None) == 0             # no answer
    e = history.load(repo)[0]
    assert history.sentence(e) == (human.change_sentence(e["changes"]), "")
    monkeypatch.setattr(history, "WORDS_RETRY", 0)
    assert history.words(repo, ask=lambda u: ("not JSON at all", "m")) == 0   # an answer that is no use
    assert history.sentence(history.load(repo)[0])[1] == ""


def test_the_background_job_starts_only_when_asked_and_not_twice(repo, monkeypatch):
    history.backfill(repo)
    assert history.start_words(repo) is False                       # under a test: never on its own
    monkeypatch.setenv("SYSTEM_DIFF_HISTORY_JOBS", "1")
    monkeypatch.setenv("SYSTEM_DIFF_HISTORY", "1")
    from system_diff import narrate
    monkeypatch.setattr(narrate, "configured", lambda root, extra=(): True)
    spawned = []
    monkeypatch.setattr(history, "_spawn", lambda root, *args: spawned.append(args))
    assert history.start_words(repo) is True and spawned == [("words",)]
    assert history.start_words(repo) is False and len(spawned) == 1   # a job runs: not another
    monkeypatch.setattr(history, "words", lambda root, **kw: 0)
    history.words_now(repo)                                          # the job frees its lock
    assert not (history.git_dir(repo) / "history-words.lock").exists()


# ── two points compared ───────────────────────────────────────────────────────

THIRD = {   # the mail server is deployed beside the API now: Core, which reads SMTP_HOST, talks to it
    "docker-compose.yml": FIRST["docker-compose.yml"].replace("DATABASE_URL: postgres://db\n",
                                                              "DATABASE_URL: postgres://db\n      SMTP_HOST: mail\n"),
}


@pytest.fixture
def three(repo):
    """The fixture's two commits and a third: the worker gone, Core sending mail, a health endpoint."""
    write(repo, THIRD)
    write(repo, {"app/api/routes.py": (repo / "app/api/routes.py").read_text(encoding="utf-8")
                 + '\n\n@router.get("/health")\ndef health():\n    return 1\n'})
    (repo / "app/jobs/worker.py").unlink()
    git_(repo, "add", "-A")
    git_(repo, "commit", "-q", "-m", "third: mail, health, no worker")
    assert history.backfill(repo) == 3
    third, second, first = history.load(repo)
    return repo, first, second, third


def _subjects(changes, op=None):
    return {c["subject"] for c in changes if op is None or c["op"] == op}


def test_compare_parts():
    assert history.compare_part("compare:a1..b2") == ("", "a1", "b2")
    assert history.compare_part("compare:a1") == ("", "a1", "")
    assert history.compare_part("api#compare:a1..now") == ("api", "a1", "now")
    assert history.compare_part("api#t1") is None and history.compare_part("") is None
    assert history.compare_key("", "a", "b") == "compare:a..b" and history.compare_key("api", "a") == "api#compare:a"


def test_two_entries_compared_from_just_after_the_first_to_just_after_the_second(three):
    repo, first, second, third = three
    got = history.compare(repo, second["id"], third["id"])
    assert got["a"]["id"] == second["id"] and got["b"]["id"] == third["id"] and got["comparison"] is not None
    assert [e["id"] for e in got["between"]] == [third["id"]]
    assert _subjects(got["changes"]) == _subjects(third["changes"])    # one step: that entry's own changes
    removed = [c for c in got["changes"] if c["op"] == "remove"]
    assert removed and any(c["entity"] == "component" for c in removed) and all(c["status"] == "removed"
                                                                                 for c in removed)
    assert any(c["entity"] == "external" and c["op"] == "add" and c.get("outside") == "mail" for c in got["changes"])

    wide = history.compare(repo, third["id"], first["id"])             # either order: the older first
    assert wide["a"]["id"] == first["id"] and wide["b"]["id"] == third["id"]
    assert [e["id"] for e in wide["between"]] == [second["id"], third["id"]]   # oldest first
    subjects = _subjects(wide["changes"])
    assert "GET /api/orders/health" in subjects and "DELETE /api/orders/{id}" in subjects
    assert not any("jobs" in s for s in subjects)                    # added then removed: no change at all
    assert any(c["entity"] == "table" and c["status"] == "changed" for c in wide["changes"])
    assert "sends email through Mail server" in human.change_sentence(wide["changes"])
    same = history.compare(repo, second["id"], second["id"])
    assert same["changes"] == [] and same["between"] == []
    assert history.compare(repo, "nothere", third["id"])["missing"] == ["nothere"]


def test_an_entry_compared_with_now(three, files):
    repo, first, second, third = three
    files(repo, {"app/api/routes.py": (repo / "app/api/routes.py").read_text(encoding="utf-8")
                 + '\n\n@router.get("/ready")\ndef ready():\n    return 1\n'})
    now = pane._history_now(repo)[0].after
    got = history.compare(repo, third["id"], history.NOW, now=now)
    assert got["b"] is None and got["between"] == []
    assert _subjects(got["changes"]) == {"GET /api/orders/ready"}
    back = history.compare(repo, history.NOW, second["id"], now=now)  # now given first: still the end
    assert back["a"]["id"] == second["id"] and [e["id"] for e in back["between"]] == [third["id"]]
    assert {"GET /api/orders/ready", "GET /api/orders/health"} <= _subjects(back["changes"])


def test_a_state_not_kept_is_read_in_the_background_and_the_views_say_so(three, monkeypatch):
    repo, first, second, third = three
    for f in history._states(repo).glob("*.json"):
        f.unlink()
    history._BPS.clear()
    history._TREES.clear()
    started = []
    monkeypatch.setattr(history, "read_later", lambda root, sha: started.append(sha) or True)
    got = history.compare(repo, first["id"], third["id"])
    assert got["comparison"] is None and got["reading"] and started   # nothing read on the look's path
    page = pane.show(repo, 100, "history", f"compare:{first['id']}..{third['id']}")
    text = "\n".join("".join(r[0] for r in row) for row in page["rows"])
    assert "reading the system at that point" in text and "ASKED IN BETWEEN" in text
    web = pane.web_view(repo, "history", f"compare:{first['id']}..{third['id']}")
    assert web["sections"][0]["items"][0]["reading"] is True
    log = history._log(repo)
    before = log.stat().st_mtime_ns
    os.utime(log, ns=(before - 10 ** 9, before - 10 ** 9))
    for sha in dict.fromkeys(started):
        assert history.read_now(repo, sha)                            # the job: read, kept, the views told
    assert log.stat().st_mtime_ns > before - 10 ** 9
    assert history.compare(repo, first["id"], third["id"])["comparison"] is not None


def _text(page) -> str:
    return "\n".join("".join(r[0] for r in row) for row in page["rows"])


def test_the_pane_marks_where_to_compare_from_and_shows_the_comparison(three):
    repo, first, second, third = three
    page = pane.show(repo, 100, "history")
    entries = [b for b in page["blocks"] if b["id"].startswith("hy:")]
    assert entries and all(b["on"] == {"c": {"compare": b["id"][3:]}} for b in entries)
    assert "c on two compares them" in _text(page)

    marked = pane.show(repo, 100, "history", f"compare:{first['id']}")
    text = _text(marked)
    assert "◆ from" in text and " ● now" in text and marked["pick"] == "hy:" + first["id"]
    now = next(b for b in marked["blocks"] if b["id"] == "hy:now")
    assert now["on"] == {"c": {"compare": "now"}}
    assert max(len("".join(r[0] for r in row)) for row in pane.show(repo, 60, "history",
                                                                      f"compare:{first['id']}")["rows"]) <= 60

    cmp = pane.show(repo, 100, "history", f"compare:{first['id']}..{third['id']}")
    text = _text(cmp)
    assert "COMPARE" in text and "from  just after" in text and "to    just after" in text
    assert "THE SYSTEM AFTER IT" in text and "WHAT CHANGED" in text and "ASKED IN BETWEEN" in text
    assert "→ " in text and "sends email through Mail server" in text
    ws = [b for b in cmp["blocks"] if b["id"].startswith("w:")]
    assert ws and cmp["pick"] == ws[0]["id"] and any(cmp["lights"].get(b["id"]) for b in ws)
    assert {"history": "api"} in [b.get("enter") for b in cmp["blocks"] if b["id"].startswith("b:")]
    asked = [b for b in cmp["blocks"] if b["id"].startswith("hy:")]
    assert [b["enter"] for b in asked] == [{"entry": second["id"]}, {"entry": third["id"]}]
    with_now = _text(pane.show(repo, 100, "history", f"compare:{third['id']}..now"))
    assert "to    now, the system as it is" in with_now and "no change to the system's structure" in with_now
    gone = _text(pane.show(repo, 100, "history", "compare:nothere..now"))
    assert "no longer in the history" in gone


def test_the_web_page_shows_a_comparison_with_hover_cards(three):
    repo, first, second, third = three
    lst = pane.web_view(repo, "history")
    sec = lst["sections"][0]
    assert sec["pick"] is True and all(i["eid"] == i["id"][3:] and i["said"] for i in sec["items"])
    web = pane.web_view(repo, "history", f"compare:{third['id']}..{first['id']}")
    json.dumps(web)
    assert web["headline"].startswith("From ") and " to " in web["headline"]
    assert [s["title"] for s in web["sections"]] == ["Compare", "What changed", "Asked in between"]
    head = web["sections"][0]["items"][0]
    assert head["from"]["eid"] == first["id"] and head["to"]["eid"] == third["id"] and head["sentence"]
    changed = web["sections"][1]["items"]
    assert changed and all(i["id"] in web["hover"] and i["id"] in web["diagram"]["lights"] for i in changed)
    assert [i["eid"] for i in web["sections"][2]["items"]] == [second["id"], third["id"]]
    svg = web["diagram"]["svg"]
    boxes = {html.unescape(i) for i in re.findall(r'data-id="([^"]+)"', svg)}
    items = {i["id"] for s in web["sections"] for i in s["items"] if i.get("id")}
    for hid, card in web["hover"].items():                           # only what the page renders, each with a card
        assert hid in boxes or hid in items, hid
        assert card["title"] and card["lines"] and all(isinstance(x, str) for x in card["lines"])
    assert {b for b in boxes if b.startswith("b:")} <= set(web["hover"])
    now = pane.web_view(repo, "history", f"compare:{second['id']}..now")
    assert now["sections"][0]["items"][0]["to"]["now"] is True
    marked = pane.web_view(repo, "history", f"compare:{first['id']}")
    assert [i["marked"] for i in marked["sections"][0]["items"]] == [False, False, True]


def test_the_history_command_compares(three, capsys):
    repo, first, second, third = three
    assert cli.main(["--repo", str(repo), "history", "--compare", third["id"], first["id"], "--width", "100"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("From just after: ") and "What changed (" in out and "What was asked in between (2" in out
    assert "Claude added the endpoint GET /api/orders/health" in out
    assert cli.main(["--repo", str(repo), "history", "--compare", third["id"], "now"]) == 0
    assert "No change to the system's structure between them." in capsys.readouterr().out
    assert cli.main(["--repo", str(repo), "history", "--compare", first["id"], second["id"], "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["from"] == first["id"] and got["to"] == second["id"] and got["between"] == [second["id"]]
    assert cli.main(["--repo", str(repo), "history", "--compare", "nothere", "now"]) == 0
    assert "Not in the history: nothere" in capsys.readouterr().out
