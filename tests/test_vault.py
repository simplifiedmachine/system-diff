"""Keys for the agents (vault.py): kept by the OS (a file only you can read where there is no keyring), given to Studio
sessions as environment variables, named in their prompt without values, masked in what the page and the log see,
never said back by a route or a listing; and the page's check for a key pasted into a message."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from system_diff import app, app_page, sessions, vault

PAT = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrst"   # 52 base32 characters: an Azure DevOps PAT's shape
GHP = "ghp_" + "A1b2C3d4E5" * 4


@pytest.fixture
def filestore(monkeypatch, tmp_path):
    monkeypatch.setattr(vault, "backend", lambda: "file")
    return tmp_path / "state"


def test_a_key_round_trips_and_no_listing_holds_it(filestore, repo, tmp_path):
    got = vault.save("AZURE_DEVOPS_EXT_PAT", PAT, repo, False, "PRs on Azure", folder=filestore)
    assert PAT not in json.dumps(got) and got["scope"] == "this project"
    vault.save("SHARED", "everywhere-value", None, True, folder=filestore)
    listed = vault.listing(repo, filestore)
    assert {k["name"] for k in listed} == {"AZURE_DEVOPS_EXT_PAT", "SHARED"} and PAT not in json.dumps(listed)
    assert PAT not in (filestore / vault.META).read_text()
    assert (filestore / vault.VALUES).stat().st_mode & 0o077 == 0 or os.name == "nt"
    assert vault.env_for(repo, filestore) == {"SHARED": "everywhere-value", "AZURE_DEVOPS_EXT_PAT": PAT}
    other = tmp_path / "other"
    other.mkdir()
    assert vault.env_for(other, filestore) == {"SHARED": "everywhere-value"}
    vault.save("SHARED", "the-projects-own", repo, False, folder=filestore)     # the project's own wins
    assert vault.env_for(repo, filestore)["SHARED"] == "the-projects-own"
    vault.forget("SHARED", repo, False, filestore)
    assert vault.env_for(repo, filestore)["SHARED"] == "everywhere-value"
    for bad in ("lower", "PATH", "SYSTEM_DIFF_X", "ANTHROPIC_API_KEY", "1X"):
        with pytest.raises(ValueError):
            vault.save(bad, "value-123", repo, folder=filestore)
    with pytest.raises(ValueError) as no:
        vault.save("EMPTY", "  ", repo, folder=filestore)
    assert "empty" in str(no.value)


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows'")
def test_dpapi_round_trip(monkeypatch, repo, tmp_path):
    folder = tmp_path / "state"
    assert vault.backend() == "dpapi"
    vault.save("TOKEN_X", "s3cr3t-välue", repo, folder=folder)
    raw = (folder / vault.VALUES).read_text()
    assert "s3cr3t" not in raw and '"dpapi"' in raw
    assert vault.env_for(repo, folder) == {"TOKEN_X": "s3cr3t-välue"}


def test_a_session_gets_its_keys_by_name_and_masks_them(filestore, repo):
    pytest.importorskip("claude_agent_sdk")
    vault.save("AZURE_DEVOPS_EXT_PAT", PAT, repo, description="PRs on Azure", folder=filestore)
    m = sessions.Manager(client_factory=lambda s: None)
    m.vault_folder = filestore
    try:
        s = sessions.Session(m, repo, {"setting_sources": []})
        o = s.sdk_options()
        assert o.env["AZURE_DEVOPS_EXT_PAT"] == PAT and o.env["SYSTEM_DIFF_APP_SESSION"] == "1"
        said = o.system_prompt["append"]
        assert "AZURE_DEVOPS_EXT_PAT (PRs on Azure)" in said and PAT not in said and "never print" in said
        ev = s.emit("tool_result", id="t", text=f"token={PAT} ok", view={"command": f"echo {PAT}"})
        assert PAT not in json.dumps(ev) and "••••AZURE_DEVOPS_EXT_PAT" in ev["text"]
        for piece in ("the key is ", PAT[:20], PAT[20:], " done"):   # streamed in pieces: never shown whole or in part
            s._delta(piece)
        streamed = "".join(e["text"] for e in s.events if e["type"] == "delta")
        assert PAT[:20] not in streamed and "the key is ••••AZURE_DEVOPS_EXT_PAT"[:len(streamed)] == streamed
        s.send(f"use {PAT}")
        assert PAT not in s.head()["title"] and PAT not in json.dumps(s.events)
        assert s.head()["keys"] == ["AZURE_DEVOPS_EXT_PAT"]
    finally:
        m.stop()


class _Fake:
    def __init__(self, session):
        self.session = session

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def query(self, text):
        pass

    async def receive_response(self):
        return
        yield


def _until(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _call(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def test_the_routes_need_the_token_and_never_say_a_value(filestore, repo):
    m = sessions.Manager(client_factory=_Fake)
    up = {}
    t = threading.Thread(target=app.serve, kwargs={"port": 0, "folder": filestore, "manager": m,
                                                   "ready": lambda url, server, st: up.update(url=url, server=server,
                                                                                              studio=st)},
                         daemon=True)
    t.start()
    try:
        assert _until(lambda: "url" in up)
        url, tok = up["url"], up["studio"].token
        w = {"X-Token": tok, "Origin": url.rstrip("/")}
        pid = _call(url + "api/projects", {"path": str(repo)}, w)["id"]
        for headers in ({"X-Token": tok}, {"Origin": url.rstrip("/")}):       # the token alone, the page alone
            with pytest.raises(urllib.error.HTTPError) as no:
                _call(url + "api/secrets/save", {"project": pid, "name": "X_KEY", "value": PAT}, headers)
            assert no.value.code == 403
        s = _call(url + "api/sessions", {"project": pid}, w)
        assert _until(lambda: m.get(s["id"]).status == "idle")
        got = _call(url + "api/secrets/save", {"project": pid, "name": "X_KEY", "value": PAT, "scope": "project",
                                               "description": "for PRs"}, w)
        assert PAT not in json.dumps(got) and got["open_sessions"] == [s["id"]]
        with pytest.raises(urllib.error.HTTPError) as bad:
            _call(url + "api/secrets/save", {"project": pid, "name": "bad name", "value": PAT}, w)
        assert bad.value.code == 400 and PAT not in bad.value.read().decode()
        listed = _call(url + f"api/secrets?project={pid}", headers={"Referer": url})
        assert [k["name"] for k in listed["keys"]] == ["X_KEY"] and PAT not in json.dumps(listed) and listed["store"]
        r = _call(url + f"api/sessions/{s['id']}/restart", {}, w)
        assert r["id"] != s["id"] and _until(lambda: m.get(r["id"]).key_env == {"X_KEY": PAT})
        assert m.get(s["id"]).status == "closed" and m.get(r["id"]).head()["keys"] == ["X_KEY"]
        _call(url + "api/secrets/forget", {"project": pid, "name": "X_KEY", "scope": "project"}, w)
        assert _call(url + f"api/secrets?project={pid}", headers={"Referer": url})["keys"] == []
    finally:
        up["server"].shutdown()
        m.stop()
        t.join(5)


def test_the_cli_stores_lists_and_forgets_without_printing(filestore, repo, monkeypatch, capsys):
    from system_diff import cli
    monkeypatch.setenv("SYSTEM_DIFF_APP_STATE", str(filestore))
    monkeypatch.chdir(repo)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(PAT + "\n"))
    assert cli.main(["secrets", "set", "AZURE_DEVOPS_EXT_PAT", "--description", "PRs"]) == 0
    assert cli.main(["secrets", "list", "--project", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "AZURE_DEVOPS_EXT_PAT" in out and PAT not in out
    assert vault.env_for(repo, filestore) == {"AZURE_DEVOPS_EXT_PAT": PAT}
    assert cli.main(["secrets", "forget", "AZURE_DEVOPS_EXT_PAT"]) == 0
    assert vault.env_for(repo, filestore) == {}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_page_offers_to_store_a_key_pasted_into_a_message():
    page = app_page.PAGE
    js = page[page.index("/*keys-detect:start*/"):page.index("/*keys-detect:end*/")]
    cases = [f"azure_pat_key = {PAT}", f"push with {GHP} please", "I would like guard rails for PII detection and "
             "masking like emails, an org tree from the employee db, and a PR at the end", "token = $AZURE_PAT",
             "commit 3f2a1b9c8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a looks fine", "password: changeme",
             f"create the PR on azure, my pat: azure_pat_key = {PAT}", "api_key: hunter22x"]
    code = js + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(t => findKeys(t))));"
    got = json.loads(subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30, check=True).stdout)
    assert [(k["value"], k["name"]) for k in got[0]] == [(PAT, "AZURE_DEVOPS_EXT_PAT")]
    assert [(k["value"], k["name"]) for k in got[1]] == [(GHP, "GITHUB_TOKEN")]
    assert got[2] == [] and got[3] == [] and got[4] == [] and got[5] == []
    assert [(k["value"], k["name"]) for k in got[6]] == [(PAT, "AZURE_DEVOPS_EXT_PAT")]   # "pat: azure_pat_key" is no key
    assert [(k["value"], k["name"]) for k in got[7]] == [("hunter22x", "API_KEY")]
    assert f"azure_pat_key = {PAT}".index(PAT) == got[0][0]["start"]
