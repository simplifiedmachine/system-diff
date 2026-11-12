"""`system-diff doctor` reports where a read's time goes, and what usually explains a slow or quiet setup."""
from __future__ import annotations

from system_diff import doctor


def test_doctor_times_a_look_and_checks_the_setup(repo, capsys):
    assert doctor.main(repo) == 0
    out = capsys.readouterr().out
    for said in ("system-diff ", "files: ", "snapshot of the working tree: ", "pane look 1: ", "pane look 2: ",
                 "hooks: ", "web page: ", "caches: "):
        assert said in out, said


def test_on_windows_no_command_opens_a_console_window(monkeypatch):
    """A process without a visible console (the web server, a background job, the pane) opened a window for every git
    it ran on Windows: a flood of windows taking the focus. Each command now runs with CREATE_NO_WINDOW there."""
    import subprocess
    from system_diff import sources
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.append(k))
    monkeypatch.setattr(sources.os, "name", "nt")
    sources.run_hidden(["git", "status"], cwd=".")
    assert seen[-1]["creationflags"] & 0x08000000
    monkeypatch.setattr(sources.os, "name", "posix")
    sources.run_hidden(["git", "status"], cwd=".")
    assert "creationflags" not in seen[-1]
