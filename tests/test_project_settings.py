"""A project's settings beside its secrets: a server's host and user (plain, shown, told to Claude) and its password
(kept by the OS, never shown); changed from the page or from the chat (Claude's tools), never a secret through the
model; seen for every project at once."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from system_diff import app, app_page, mcp_server, vault


@pytest.fixture
def filestore(monkeypatch, tmp_path):
    monkeypatch.setattr(vault, "backend", lambda: "file")
    return tmp_path / "state"


def test_settings_round_trip_with_their_values_in_the_env_and_the_prompt(filestore, repo):
    vault.save("STAGING_HOST", "10.1.2.3", repo, False, "where we deploy", filestore, kind="setting",
               group="staging server")
    vault.save("STAGING_USER", "deploy", repo, False, "", filestore, kind="setting", group="staging server")
    vault.save("STAGING_PASSWORD", "Xyz!23-secret", repo, False, "", filestore, group="staging server")
    env = vault.env_for(repo, filestore)
    assert env == {"STAGING_HOST": "10.1.2.3", "STAGING_USER": "deploy", "STAGING_PASSWORD": "Xyz!23-secret"}
    listed = vault.listing(repo, filestore)
    by = {k["name"]: k for k in listed}
    assert by["STAGING_HOST"]["value"] == "10.1.2.3" and by["STAGING_HOST"]["kind"] == "setting"
    assert "value" not in by["STAGING_PASSWORD"] and by["STAGING_PASSWORD"]["group"] == "staging server"
    note = vault.prompt_note(listed)
    assert "STAGING_HOST (staging server, where we deploy) = 10.1.2.3" in note and "Xyz!23" not in note
    masks = vault.masks_of(env, listed)
    assert [n for _v, n in masks] == ["STAGING_PASSWORD"]            # a setting is shown as it is
    meta = (filestore / vault.META).read_text(encoding="utf-8")
    assert "10.1.2.3" in meta and "Xyz!23" not in meta              # the secret is not beside the settings
    # changed: a setting's value, a secret's description; forgotten
    assert vault.update("STAGING_HOST", repo, folder=filestore, value="10.1.2.4")["value"] == "10.1.2.4"
    assert vault.update("STAGING_PASSWORD", repo, folder=filestore, description="ssh")["description"] == "ssh"
    assert vault.env_for(repo, filestore)["STAGING_PASSWORD"] == "Xyz!23-secret"
    vault.forget("STAGING_USER", repo, folder=filestore)
    assert "STAGING_USER" not in vault.env_for(repo, filestore)


@pytest.mark.parametrize("name,value", [("DB_PASSWORD", "x"), ("API", "ghp_" + "a1B2" * 9), ("X", "aB3!kdjfhg8sdfJKH7sdf98"),
                                        ("ADO", "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrst")])
def test_a_secret_is_never_taken_as_a_setting(filestore, repo, name, value):
    with pytest.raises(ValueError, match="looks like a secret"):
        vault.save(name, value, repo, False, "", filestore, kind="setting")
    assert vault.listing(repo, filestore) == []


def test_every_projects_listing_holds_no_secret(filestore, repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    vault.save("PROD_HOST", "prod.example.com", other, False, "", filestore, kind="setting")
    vault.save("PROD_PASSWORD", "S3cr3t!value", other, False, "", filestore)
    vault.save("GH", "ghp_" + "a1B2" * 9, None, True, "", filestore)
    every = vault.everything(filestore)
    assert {(k["name"], k["project"]) for k in every} == {("PROD_HOST", vault._same(other)),
                                                          ("PROD_PASSWORD", vault._same(other)), ("GH", "")}
    text = json.dumps(every)
    assert "S3cr3t" not in text and "a1B2" not in text and "prod.example.com" in text


def test_claude_stores_settings_and_asks_for_secrets(filestore, repo, monkeypatch):
    monkeypatch.setattr(vault, "_folder", lambda folder: filestore)
    monkeypatch.setattr(mcp_server, "_root", lambda: repo)
    server = mcp_server.build()
    tools = {t.name: t.fn for t in server._tool_manager.list_tools()}
    assert {"project_settings", "save_setting", "forget_setting", "ask_for_secret"} <= set(tools)
    got = tools["save_setting"]("STAGING_HOST", "10.1.2.3", group="staging server")
    assert got["value"] == "10.1.2.3"
    refused = tools["save_setting"]("STAGING_PASSWORD", "Xyz!23")
    assert "looks like a secret" in refused["error"]
    listed = tools["project_settings"]()
    assert [k["name"] for k in listed["entries"]] == ["STAGING_HOST"]
    monkeypatch.setattr(mcp_server, "_ask_studio", lambda body: None)   # no Studio: the command to run
    asked = tools["ask_for_secret"]("STAGING_PASSWORD", "the staging server's ssh password")
    assert asked["asked"] is False and "system-diff secrets set STAGING_PASSWORD" in asked["tell_the_person"]
    seen = {}
    monkeypatch.setattr(mcp_server, "_ask_studio", lambda body: seen.update(body) or {"asked": body["name"], "id": "x"})
    asked = tools["ask_for_secret"]("STAGING_PASSWORD", "ssh", group="staging server")
    assert asked["asked"] == "STAGING_PASSWORD" and "value" not in seen
    assert tools["forget_setting"]("STAGING_HOST")["forgot"] == "STAGING_HOST"


def test_the_studio_keeps_claudes_asks_without_values(tmp_path, repo):
    studio = app.Studio(tmp_path / "state")
    got = studio.ask_secret({"name": "STAGING_PASSWORD", "project": str(repo), "description": "ssh", "value": "no"})
    asks = studio.asks_for(repo)
    assert [a["name"] for a in asks] == ["STAGING_PASSWORD"] and "value" not in asks[0]
    studio.drop_ask(got["id"])
    assert studio.asks_for(repo) == []
    with pytest.raises(ValueError):
        studio.ask_secret({"name": "bad name"})


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_server_message_is_split_into_settings_and_a_secret():
    page = app_page.PAGE
    js = page[page.index("/*keys-detect:start*/"):page.index("/*keys-detect:end*/")]
    cases = ["staging server 10.1.2.3 user deploy password Xyz!23", "server 10.0.0.9, login root, port 2222, pwd: hunter2!",
             "the password is weak, as said"]
    code = js + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(t => [findKeys(t), findSettings(t)])));"
    got = json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30, check=True).stdout)
    keys, sets = got[0]
    assert [(k["value"], k["name"]) for k in keys] == [("Xyz!23", "STAGING_PASSWORD")]
    assert [(s["value"], s["name"]) for s in sets] == [("10.1.2.3", "STAGING_HOST"), ("deploy", "STAGING_USER")]
    keys, sets = got[1]
    assert [(k["value"], k["name"]) for k in keys] == [("hunter2!", "SERVER_PASSWORD")]
    assert {(s["value"], s["name"]) for s in sets} == {("10.0.0.9", "SERVER_HOST"), ("root", "SERVER_USER"),
                                                       ("2222", "SERVER_PORT")}
    assert got[2][0] == []
