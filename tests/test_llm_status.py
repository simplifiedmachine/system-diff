"""When the LLM cannot be reached: said once, on the page (not the engine's terminal), and not asked again for a while."""
from __future__ import annotations

from system_diff import narrate, watch


def test_a_failure_is_kept_paused_and_cleared(monkeypatch, repo, capsys):
    calls = []

    def failing(*a, **k):
        calls.append(1)
        narrate._failed(repo, "system-diff: claude -p --model haiku: ValueError: AWS authentication failed")
        return None
    monkeypatch.setattr(narrate, "off", lambda: False)
    monkeypatch.setattr(narrate, "_complete", failing)
    monkeypatch.setattr(narrate, "QUIET", [False])
    monkeypatch.setattr(narrate, "_PRINTED", set())
    narrate._PAUSED.clear()
    assert narrate.complete("u", "s", repo, {}) is None and calls == [1]
    assert narrate.complete("u", "s", repo, {}) is None and calls == [1]          # paused: not asked again
    st = narrate.status(repo)
    assert "AWS authentication failed" in st["error"] and st["until"] > st["at"]
    assert capsys.readouterr().err.count("AWS authentication failed") == 1        # said once
    narrate._PAUSED.clear()
    monkeypatch.setattr(narrate, "_complete", lambda *a, **k: ("an answer", "model"))
    assert narrate.complete("u", "s", repo, {})[0] == "an answer" and narrate.status(repo) is None


def test_in_the_engine_a_failure_goes_to_the_log_and_the_page(monkeypatch, repo, capsys):
    monkeypatch.setattr(narrate, "QUIET", [True])
    narrate._failed(repo, "system-diff: claude -p --model haiku: ValueError: AWS authentication failed")
    narrate._PAUSED.clear()
    assert capsys.readouterr().err == ""
    from system_diff.sources import git_dir
    assert "AWS authentication failed" in (git_dir(repo) / "engine.log").read_text(encoding="utf-8")
    w = type("W", (), {"root": repo, "engine": None})()
    assert "AWS authentication failed" in watch.chat_view(w)["llm"]["error"]
    assert "Your model could not be reached" in watch.page_html()


def test_the_llm_features_use_the_model_and_settings_chosen(monkeypatch, repo, tmp_path):
    """Not Claude Code's defaults (an expired SSO sign-in, say): the gateway and model the person chose in the Studio."""
    import json
    from pathlib import Path
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    profile = home / ".claude" / "settings.dsa2.json"
    profile.write_text(json.dumps({"model": "DSA-Flash2", "env": {"ANTHROPIC_BASE_URL": "http://gateway.example:30090",
                                                                  "ANTHROPIC_AUTH_TOKEN": "tok"}}), encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "defaults.json").write_text(json.dumps({"*": {"settings_profile": "user:settings.dsa2.json",
                                                           "model": "DSA-Flash2"}}), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SYSTEM_DIFF_APP_STATE", str(state))
    pick = narrate.chosen(repo, {})
    assert pick == {"settings": str(profile), "model": "DSA-Flash2"}
    gw = narrate.claude_llm(repo, [pick["settings"]])
    assert gw["base_url"] == "http://gateway.example:30090" and gw["token"] == "tok"
    cmd = narrate.cli_command({"path": "claude", "model": pick["model"], "settings": pick["settings"]})
    assert cmd[cmd.index("--model") + 1] == "DSA-Flash2" and cmd[cmd.index("--settings") + 1] == str(profile)
    assert narrate.chosen(repo, {"narrate": {"model": "from-the-toml"}})["model"] == "from-the-toml"
    asked = []
    monkeypatch.setattr(narrate, "off", lambda: False)
    monkeypatch.setattr(narrate, "ask", lambda s, *a, **k: asked.append(s) or "an answer")
    narrate._PAUSED.clear()
    assert narrate.complete("u", "s", repo, {})[0] == "an answer"
    assert asked[0]["model"] == "DSA-Flash2" and asked[0]["base_url"] == "http://gateway.example:30090"
