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
