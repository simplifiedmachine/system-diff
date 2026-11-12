"""How the system evolved (history.py): the commits read back as plain-language entries, each structural change the
engine sees recorded under the person's latest request, bursts folded into one entry, a block's own history, and the
History view in the pane and on the web page."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from system_diff import activity, cli, history, live, pane

from conftest import git_, write

HEALTH = ('\n\n@router.get("/health")\ndef health():\n    return 1\n')
READY = ('\n\n@router.get("/ready")\ndef ready():\n    return 1\n')


@pytest.fixture(autouse=True)
def no_recorder(monkeypatch):
    """No engine starts a recorder of its own: the tests drive one by hand."""
    monkeypatch.setenv("SYSTEM_DIFF_HISTORY", "0")


@pytest.fixture
def engine(repo):
    e = live.Engine(repo, interval=3600).start()   # ticks only when a test says so
    assert e.done.wait(30)
    yield e
    e.stop()


def test_the_commits_are_read_back_as_entries_in_plain_words(repo):
    assert history.backfill(repo) == 2
    second, first = history.load(repo)
    head = git_(repo, "rev-parse", "HEAD").strip()
    assert second["source"] == "commit"
    assert second["commit"] == head and second["head"] == head and second["base"] == git_(repo, "rev-parse",
                                                                                          "HEAD~1").strip()
    assert second["message"].startswith("second") and second["summary"] == second["message"]
    titles = [c["title"] for c in second["changes"]]
    assert "Claude removed" not in " ".join(titles)
    assert any(t.startswith("Claude added the endpoint DELETE /api/orders/{id}") for t in titles), titles
    assert any("note to the table" in t for t in titles), titles
    assert all(c["why"] for c in second["changes"])
    assert "the endpoint DELETE /api/orders/{id}" in second["got"]
    assert second["files"] == 4 and "api" in second["parts"]
    assert first["base"] == "empty" and first["source"] == "commit" and first["changes"]
    assert history.backfill(repo) == 0                             # read once: never again
    text = history.text(repo, width=100)
    assert "→ it got:" in text and "commit " in text


def test_a_change_the_engine_sees_is_recorded_under_the_latest_request_and_bursts_fold(repo, engine):
    rec = history.Recorder(engine)
    activity.record(repo, {"event": activity.PROMPT, "text": "add a health check to the orders API"})
    assert rec.step() is None                                      # where the history starts (and the commits read)
    assert {e["source"] for e in history.load(repo)} == {"commit"}
    t0 = time.time()
    write(repo, {"app/api/routes.py": (repo / "app/api/routes.py").read_text() + HEALTH})
    engine.sync()
    got = rec.step(now=t0)
    assert got is not None and got["source"] == "turn" and got["request"] == "add a health check to the orders API"
    assert [c["subject"] for c in got["changes"]] == ["GET /api/orders/health"]
    assert got["got"] == "the endpoint GET /api/orders/health" and got["files"] == 1
    assert rec.step(now=t0 + 1) is None                            # nothing new: nothing written

    write(repo, {"app/api/routes.py": (repo / "app/api/routes.py").read_text() + READY})   # the same burst
    engine.sync()
    again = rec.step(now=t0 + 30)
    assert again["id"] == got["id"] and again["since"] == got["since"]
    assert sorted(c["subject"] for c in again["changes"]) == ["GET /api/orders/health", "GET /api/orders/ready"]
    assert again["got"] == "2 endpoints under /api/orders"
    turns = [e for e in history.load(repo) if e["source"] == "turn"]
    assert len(turns) == 1 and len(turns[0]["changes"]) == 2      # one entry for the burst

    activity.record(repo, {"event": activity.PROMPT, "text": "the worker should send mail"})   # a new request
    write(repo, {"app/jobs/worker.py": "from app.core import rules\nfrom app.db import repo\n"})
    engine.sync()
    third = rec.step(now=t0 + 40)
    assert third["id"] != got["id"] and third["request"] == "the worker should send mail"
    assert [c["subject"] for c in third["changes"]] == ["app/jobs>store"], third["changes"]

    write(repo, {"app/jobs/worker.py": "from app.core import rules\n"})   # the burst undoes itself: no entry
    engine.sync()
    rec.step(now=t0 + 50)
    assert third["id"] not in {e["id"] for e in history.load(repo)}

    write(repo, {"app/api/routes.py": (repo / "app/api/routes.py").read_text() + '\n\n@router.get("/x")\ndef x():\n'
                                                                                   '    return 1\n'})
    engine.sync()
    rec.quiet = 0                                                  # as if quiet for long enough: a new entry
    later = rec.step(now=t0 + 60)
    rec.quiet = history.QUIET
    assert later["id"] not in (got["id"], third["id"])

    # committed: the commit's changes were recorded as they happened, so those entries get its id, nothing more
    git_(repo, "add", "-A")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m",
                    "health, ready and x"], cwd=repo, check=True, capture_output=True,
                   env={**os.environ, "GIT_COMMITTER_DATE": f"@{int(t0) + 70} +0000"})   # after what it holds
    engine.sync()
    rec.step(now=t0 + 80)
    head = git_(repo, "rev-parse", "HEAD").strip()
    assert not [e for e in history.load(repo) if e["source"] == "commit" and e["commit"] == head]
    assert {e["commit"] for e in history.load(repo) if e["source"] == "turn"} == {head}


def test_a_block_has_its_own_history(repo):
    history.backfill(repo)
    entries = history.load(repo)
    api = history.for_part(entries, {"api"})
    assert api and all(all("api" in c["parts"] for c in e["changes"]) for e in api)
    assert len(api[0]["changes"]) < len(entries[0]["changes"])    # only its own changes
    assert history.for_part(entries, {"nothing-here"}) == []
    out = history.text(repo, "api", 100)
    assert "DELETE /api/orders/{id}" in out and "to the table" not in out


def _text(page) -> str:
    return "\n".join("".join(r[0] for r in row) for row in page["rows"])


def test_the_history_page_lists_entries_as_blocks_with_their_changes(repo):
    history.backfill(repo)
    page = pane.show(repo, 100, "history")
    text = _text(page)
    assert "HISTORY" in text and "→ it got: " in text and "commit " in text
    ids = [b["id"] for b in page["blocks"]]
    assert ids and all(i.startswith("hy:") for i in ids) and page["pick"] == ids[0]
    first = page["blocks"][0]
    assert first["h"] == 2 and first["enter"] == {"entry": ids[0][3:]} and first["slot"][1] == 5
    detail = "\n".join("".join(r[0] for r in row) for row in page["details"][ids[0]])
    assert "Claude added" in detail and "↵ shows it on the diagram" in detail
    narrow = pane.show(repo, 60, "history")
    assert max(len("".join(r[0] for r in row)) for row in narrow["rows"]) <= 60

    one = pane.show(repo, 100, "history", "#" + ids[0][3:])       # ↵ on an entry: that change on the diagram
    text = _text(one)
    assert "THE SYSTEM AFTER IT" in text and "WHAT IT CHANGED" in text
    enters = [b.get("enter") for b in one["blocks"] if b["id"].startswith("b:")]
    assert {"history": "api"} in enters                            # a box there: its own history
    assert any(b["id"].startswith("w:") for b in one["blocks"]) and one["pick"].startswith("w:")
    assert any(one["lights"].get(b["id"]) for b in one["blocks"] if b["id"].startswith("w:"))


def test_h_on_a_block_shows_only_what_changed_it(repo):
    history.backfill(repo)
    whole = pane.show(repo, 100, "history")
    api = pane.show(repo, 100, "history", "api")
    assert "HISTORY OF API" in _text(api) and "b back" in _text(api)
    assert 0 < len(api["blocks"]) <= len(whole["blocks"])
    detail = "\n".join("".join(r[0] for r in row) for b in api["blocks"] for row in api["details"][b["id"]])
    assert "to the table" not in detail                            # the table's column is not the API's
    one = pane.show(repo, 100, "history", "api#" + api["blocks"][0]["id"][3:])
    assert "CHANGE TO API" in _text(one)


def test_the_web_page_has_the_history_with_hover_cards(repo):
    history.backfill(repo)
    out = pane.web_view(repo, "history")
    assert out["view"] == "history" and out["headline"] == "How the system evolved"
    [sec] = out["sections"]
    assert sec["kind"] == "history" and sec["items"]
    for item in sec["items"]:
        assert item["id"].startswith("hy:") and item["id"] in out["hover"] and item["got"] and item["asked"]
        assert item["id"] in out["diagram"]["lights"]
        card = out["hover"][item["id"]]
        assert card["title"] and any(line.startswith("It got: ") for line in card["lines"])
        assert any("files changed" in line for line in card["lines"])
        assert any(line.startswith("Commit ") for line in card["lines"])
    assert out["diagram"]["svg"]
    one = pane.web_view(repo, "history", "#" + sec["items"][0]["id"][3:])
    assert [s["title"] for s in one["sections"]] == ["This change", "What it changed"]
    assert all(i["id"] in one["hover"] for i in one["sections"][1]["items"])
    api = pane.web_view(repo, "history", "api")
    assert api["headline"] == "How API evolved" and len(api["sections"][0]["items"]) <= len(sec["items"])


def test_the_history_command(repo, capsys):
    assert cli.main(["--repo", str(repo), "history", "--width", "100"]) == 0   # empty: read from the commits now
    out = capsys.readouterr().out
    assert "→ it got:" in out and "commit " in out
    assert cli.main(["--repo", str(repo), "history", "--json", "--part", "api"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert entries and all("api" in c["parts"] for e in entries for c in e["changes"])


def test_the_engine_records_on_its_own_in_the_background(repo, monkeypatch):
    monkeypatch.setenv("SYSTEM_DIFF_HISTORY", "1")
    activity.record(repo, {"event": activity.PROMPT, "text": "add a health check"})
    e = live.Engine(repo, interval=3600).start()
    try:
        assert e.done.wait(30) and e.recorder is not None
        deadline = time.time() + 20
        while time.time() < deadline and len(history.load(repo)) < 2:   # the commits, read in the background
            time.sleep(0.1)
        assert [x["source"] for x in history.load(repo)] == ["commit", "commit"]
        write(repo, {"app/api/routes.py": (repo / "app/api/routes.py").read_text() + HEALTH})
        e.touch(["app/api/routes.py"])                                # what the hooks tell it
        while time.time() < deadline and len(history.load(repo)) < 3:
            time.sleep(0.1)
        newest = history.load(repo)[0]
        assert newest["source"] == "turn" and newest["request"] == "add a health check"
        assert newest["got"] == "the endpoint GET /api/orders/health"
    finally:
        e.stop()


def test_a_page_kept_for_another_part_is_never_served_for_this_one(repo):
    """While the engine reads again, the page last kept stands in only for the same view and the same part: a zoom
    (or a History filter or entry) must never open on another part's page, as if it did nothing."""
    page = lambda title: {"protocol": 2, "rows": [[[title, "heading"]]], "blocks": [], "wide": 100}  # noqa: E731
    live._saved_at.clear()
    live.save_page(repo, ("pane", "now", 100, -1, ""), page("SYSTEM"))
    live.save_page(repo, ("pane", "history", 100, -1, "api"), page("HISTORY OF API"))
    assert live.saved_page(repo, ("pane", "now", 100, -1, "core")) is None            # zoomed in: not the System
    assert live.saved_page(repo, ("pane", "now", 140, -1, ""))["rows"][3][0][0] == "SYSTEM"   # another width: yes
    assert live.saved_page(repo, ("pane", "history", 80, -1, "api"))["rows"][3][0][0] == "HISTORY OF API"
    assert live.saved_page(repo, ("pane", "history", 100, -1, "")) is None            # the whole system's: not API's
    assert live.saved_page(repo, ("pane", "history", 100, -1, "api#t1")) is None      # one entry: not the list
