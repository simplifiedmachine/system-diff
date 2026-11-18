"""`system-diff update`: the whole update in one step (pip cannot run anything after it installs)."""
from __future__ import annotations

import json
import sys

from system_diff import update


def test_the_channel_stays_the_same():
    assert update.channel_of("0.11.0.dev6") == "nightly" and update.channel_of("0.10.0") == "stable"
    assert update.spec_for("nightly").endswith("@main") and update.spec_for("stable") == "system-diff[mcp,app]"


def test_what_runs_system_diff_exe_is_found():
    text = ('"system-diff.exe","4120","Console","1","21,340 K"\n"python.exe","4200","Console","1","1 K"\n'
            '"system-diff.exe","5532","Console","1","20,112 K"\n')
    assert update.holders_from_tasklist(text) == [4120, 5532]
    assert update.holders_from_tasklist("INFO: No tasks are running which match the specified criteria.") == []


def test_one_mcp_server_started_the_right_way():
    py = {"command": r"C:\Python312\python.exe", "args": ["-m", "system_diff", "mcp"]}
    exe = {"command": "system-diff", "args": ["mcp"]}
    rm = ["mcp", "remove", "--scope", "user", "system-diff"]
    add = ["mcp", "add", "--scope", "user", "system-diff", "--", py["command"], "-m", "system_diff", "mcp"]
    assert update.mcp_steps(exe, plugin=False, want=py) == [rm, add]       # the old way: system-diff.exe held
    assert update.mcp_steps(None, plugin=False, want=py) == [add]
    assert update.mcp_steps(dict(py), plugin=False, want=py) == []         # already right
    assert update.mcp_steps(py, plugin=True, want=py) == [rm]              # the plugin brings it: not twice
    assert update.mcp_steps(None, plugin=True, want=py) == []
    assert update.same_server({"command": "/usr/local/bin/system-diff", "args": ["mcp"]}, exe)


def test_a_windows_update_stops_what_holds_the_exe_and_asks_first(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"enabledPlugins": {"system-diff@system-diff": True}}),
                                                    encoding="utf-8")
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"system-diff": {"command": "python",
                                                                                "args": ["-m", "system_diff", "mcp"]}}}),
                                       encoding="utf-8")
    monkeypatch.setattr(update.Path, "home", lambda: home)
    monkeypatch.setattr(update, "windows", lambda: True)
    monkeypatch.setattr(update, "holders", lambda: [4120])
    monkeypatch.setattr(update, "leftover_pids", lambda: [])
    monkeypatch.setattr(update.shutil, "which", lambda name: {"claude": r"C:\npm\claude.CMD"}.get(name))
    said = []
    assert update.run(dry=True, say=said.append, ask=lambda q: "n") == 1          # asked (dry: no), not updated
    said.clear()
    assert update.run(yes=True, dry=True, say=said.append, ask=lambda q: "") == 0
    text = "\n".join(said)
    assert "taskkill /PID 4120 /F" in text and "pip install -U" in text and sys.executable in text
    assert r"C:\npm\claude.CMD mcp remove --scope user system-diff" in text      # the plugin brings it: one
    assert "plugin update system-diff@system-diff --scope user" in text and "marketplace update system-diff" in text
    said.clear()
    monkeypatch.setattr(update, "holders", lambda: [4120])
    assert update.run(dry=False, say=said.append, ask=lambda q: "n", mcp=False, plugin=False) == 1
    assert any("Not updated" in s for s in said)


def test_on_windows_the_launcher_hands_over_to_python(monkeypatch):
    monkeypatch.setattr(update, "windows", lambda: True)
    assert update.from_launcher(r"C:\Python312\Scripts\system-diff.exe")
    assert not update.from_launcher(r"C:\Python312\python.exe")
    monkeypatch.setattr(update, "windows", lambda: False)
    assert not update.from_launcher("/usr/local/bin/system-diff")


def test_stop_ends_engines_left_running(monkeypatch):
    monkeypatch.setattr(update, "leftover_pids", lambda: [4321])
    said = []
    assert update.stop_everything(say=said.append, dry=True) == 0
    assert any("pid 4321" in x for x in said)
    assert update._ps_pids("1234\r\n 5678\r\n") == [1234, 5678]


def test_ports_are_taken_exclusively_on_windows():
    from system_diff import app, hub, watch
    for server in (watch._Server, app._Server, hub._HubServer):
        assert issubclass(server, hub.Exclusive)
    assert hub.Exclusive.allow_reuse_address is (__import__("os").name != "nt")
