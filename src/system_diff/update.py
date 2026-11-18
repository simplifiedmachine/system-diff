"""`system-diff update`: the whole update in one command, so there is nothing to keep track of.

pip cannot run anything after it installs (packages have no install hooks, by design), so this does the steps an
update needs, in order:

1. Stops what would stop pip: the Studio, and, on Windows, any process still running system-diff.exe (Claude Code's
   MCP server when it was added as `system-diff mcp`, or the plugin's): Windows cannot replace a program that runs.
   It asks first (--yes does not), and Claude Code reconnects its tools with /mcp, or on a restart.
2. Installs the new version from the same channel as now: nightly (main) when this one is a .dev version, else the
   stable release from PyPI (--nightly or --stable to switch). pipx installs are updated with pipx.
3. Registers system-diff's MCP server with Claude Code the right way for this machine, once: on Windows through Python
   itself (nothing then holds system-diff.exe at the next update), elsewhere as `system-diff mcp`; with the plugin on
   (it brings the same server), a second one is removed so no tool is there twice.
4. Updates the Claude Code plugin, when it is installed.

On Windows `system-diff update` cannot replace the very system-diff.exe it runs from: it starts itself again, as
`python -m system_diff update`, in a window of its own, and returns.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = "https://github.com/simplifiedmachine/system-diff"
EXTRAS = "mcp,app"
PLUGIN = "system-diff"


def channel_of(version: str) -> str:
    return "nightly" if ".dev" in version else "stable"


def spec_for(channel: str) -> str:
    return (f"system-diff[{EXTRAS}] @ git+{REPO}@main" if channel == "nightly" else f"system-diff[{EXTRAS}]")


def windows() -> bool:
    return os.name == "nt"


def from_launcher(argv0: str = "") -> bool:
    """Whether this runs from system-diff.exe (pip's launcher, on Windows), which pip would have to replace."""
    import re
    name = re.split(r"[\\/]", argv0 or sys.argv[0])[-1].lower()   # a Windows path, wherever this runs
    return windows() and name in ("system-diff.exe", "system-diff")


def holders_from_tasklist(text: str) -> List[int]:
    """The process ids `tasklist /FO CSV /NH` lists for system-diff.exe."""
    out = []
    for line in text.splitlines():
        cells = [c.strip().strip('"') for c in line.split('","')]
        if cells and cells[0].strip('"').lower() == "system-diff.exe" and len(cells) > 1 and cells[1].isdigit():
            out.append(int(cells[1]))
    return out


def holders() -> List[int]:
    """The processes running system-diff.exe now (Windows only)."""
    if not windows():
        return []
    try:
        got = subprocess.run(["tasklist", "/FI", "IMAGENAME eq system-diff.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=30,
                             creationflags=0x08000000)   # CREATE_NO_WINDOW
        return holders_from_tasklist(got.stdout)
    except (OSError, subprocess.SubprocessError):
        return []


def _ps_pids(text: str) -> List[int]:
    return [int(x) for x in text.split() if x.strip().isdigit()]


def leftover_pids() -> List[int]:
    """The processes still running system-diff's engine or Studio (by their command line), this one excepted: what
    `system-diff stop` ends when asking them to stop did not (an engine that lost its web.json, one from a version
    from before /quit)."""
    me = os.getpid()
    pids: List[int] = []
    try:
        if windows():
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
                  "'system_diff.*\\s(watch|app)\\b|system-diff(\\.exe)?\"?\\s+(--repo\\s+\\S+\\s+)?(watch|app)\\b' } "
                  "| Select-Object -ExpandProperty ProcessId")
            got = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                 timeout=60, creationflags=0x08000000)
            pids = _ps_pids(got.stdout)
        elif Path("/proc").is_dir():
            for d in Path("/proc").iterdir():
                if not d.name.isdigit():
                    continue
                try:
                    cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
                except OSError:
                    continue
                if ("system_diff" in cmd or "system-diff" in cmd) and (" watch" in cmd or " app" in cmd):
                    pids.append(int(d.name))
        else:
            got = subprocess.run(["ps", "-ax", "-o", "pid=,command="], capture_output=True, text=True, timeout=30)
            for line in got.stdout.splitlines():
                pid, _, cmd = line.strip().partition(" ")
                if pid.isdigit() and ("system_diff" in cmd or "system-diff" in cmd) and (" watch" in cmd or " app" in cmd):
                    pids.append(int(pid))
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return [p for p in pids if p != me and p != os.getppid()]


def stop_everything(say=print, dry: bool = False) -> int:
    """`system-diff stop`: the Studio and every engine of this user, asked to stop (POST /quit, each with its own
    token); what is left of them (by command line) ended. Claude Code's MCP servers are not touched. The number
    stopped."""
    import http.client
    from . import app, hub
    n = 0
    try:
        if app.running() is not None:
            say("Stopping the Studio")
            if not dry:
                app.stop(say=say) if "say" in app.stop.__code__.co_varnames else app.stop()
            n += 1
    except Exception as exc:  # noqa: BLE001
        say(f"  the Studio: {type(exc).__name__}: {exc}")
    folder = hub.engines_dir()
    for f in sorted(folder.glob("*.json")) if folder is not None and folder.is_dir() else []:
        e = hub._read(f)
        if e is None or not e.get("root"):
            continue
        try:
            from .sources import git_dir
            info = json.loads((git_dir(Path(e["root"])) / "web.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            info = {}
        token = str(info.get("token") or "") if info.get("port") == e["port"] else ""
        say(f"Stopping the engine of {e['name'] or e['root']} (port {e['port']})")
        n += 1
        if dry or not token:
            continue
        try:
            conn = http.client.HTTPConnection("127.0.0.1", e["port"], timeout=5)
            conn.request("POST", "/quit", body=b"{}", headers={"X-Token": token, "Content-Type": "application/json"})
            conn.getresponse().read()
            conn.close()
        except (OSError, http.client.HTTPException):
            pass
    if not dry:
        time.sleep(1.0)
    for pid in leftover_pids():
        say(f"Ending a system-diff process left running (pid {pid})")
        n += 1
        if dry:
            continue
        try:
            if windows():
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=30,
                               creationflags=0x08000000)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            pass
    say("Nothing of system-diff's was running" if not n else "Stopped: the next look of the pane (or system-diff watch, "
        "or system-diff app) starts what is needed again")
    return 0


def in_pipx() -> bool:
    return "pipx" in Path(sys.prefix).as_posix().lower().split("/")


def pip_argv(channel: str) -> List[str]:
    spec = spec_for(channel)
    pipx = shutil.which("pipx")
    if in_pipx() and pipx:
        return [pipx, "install", "--force", spec]
    return [sys.executable, "-m", "pip", "install", "-U", spec]


def desired_server() -> Dict[str, Any]:
    """How Claude Code should start system-diff's MCP server here: on Windows through this Python (its absolute
    path: no PATH question, and system-diff.exe stays free for the next update), elsewhere the system-diff command."""
    if windows() or not shutil.which("system-diff"):
        return {"command": sys.executable, "args": ["-m", "system_diff", "mcp"]}
    return {"command": "system-diff", "args": ["mcp"]}


def claude_json(home: Optional[Path] = None) -> Dict[str, Any]:
    try:
        got = json.loads(((home or Path.home()) / ".claude.json").read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def user_server(home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """system-diff's MCP server as registered at user scope (`claude mcp add --scope user`), if it is."""
    got = (claude_json(home).get("mcpServers") or {}).get("system-diff")
    return got if isinstance(got, dict) else None


def plugin_key(home: Optional[Path] = None) -> str:
    """The plugin's key in the person's Claude Code settings ("system-diff@<marketplace>") when it is enabled."""
    try:
        got = json.loads(((home or Path.home()) / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    on = got.get("enabledPlugins") if isinstance(got, dict) else None
    for k, v in (on or {}).items() if isinstance(on, dict) else []:
        if str(k).split("@")[0] == PLUGIN and v:
            return str(k)
    return ""


def same_server(current: Optional[Dict[str, Any]], want: Dict[str, Any]) -> bool:
    if not current:
        return False
    cmd = str(current.get("command") or "")
    return (os.path.normcase(cmd) == os.path.normcase(want["command"]) or Path(cmd).name == want["command"]) \
        and list(current.get("args") or []) == want["args"]


def mcp_steps(current: Optional[Dict[str, Any]], plugin: bool, want: Dict[str, Any]) -> List[List[str]]:
    """The `claude mcp ...` calls (without the `claude`) that leave exactly one system-diff MCP server, started the
    right way: none of the person's own with the plugin on (it brings the same one), else theirs as `want`."""
    remove = ["mcp", "remove", "--scope", "user", "system-diff"]
    if plugin:
        return [remove] if current else []
    if same_server(current, want):
        return []
    add = ["mcp", "add", "--scope", "user", "system-diff", "--", want["command"], *want["args"]]
    return ([remove] if current else []) + [add]


def _run(argv: List[str], dry: bool, say) -> int:
    say("  $ " + " ".join(f'"{a}"' if " " in a else a for a in argv))
    if dry:
        return 0
    try:
        return subprocess.run(argv).returncode
    except OSError as exc:
        say(f"  could not run it: {exc}")
        return 1


def run(channel: str = "", yes: bool = False, dry: bool = False, mcp: bool = True, plugin: bool = True,
        relaunched: bool = False, say=print, ask=input) -> int:
    from . import __version__
    if from_launcher() and not relaunched and not dry:
        # this process runs from system-diff.exe, which pip must replace, and a program cannot replace itself
        flags = " ".join((["--" + channel] if channel else []) + (["--yes"] if yes else []) +
                         ([] if mcp else ["--no-mcp"]) + ([] if plugin else ["--no-plugin"]))
        say("On Windows, run it through Python (system-diff.exe cannot replace itself while it runs):\n"
            f"  python -m system_diff update{(' ' + flags) if flags else ''}")
        return 1
    if relaunched:
        time.sleep(1.5)   # the launcher that started this one ends first
    channel = channel or channel_of(__version__)
    say(f"system-diff {__version__} → the latest {channel} ({spec_for(channel)})")

    # 1. what would stop pip, and what would keep running the old version: the Studio and the engines
    stop_everything(say, dry)
    held = holders()
    if held:
        say(f"{len(held)} process{'es' if len(held) > 1 else ''} still run system-diff.exe (pid {', '.join(map(str, held))}):"
            " usually Claude Code's system-diff MCP server. Windows cannot update it while it runs.")
        go = yes or (not dry and ask("Stop them now? Claude Code reconnects its tools with /mcp (or on a restart) [Y/n] ")
                     .strip().lower() in ("", "y", "yes"))
        if not go:
            say("Not updated. Close Claude Code (or stop them), then run this again.")
            return 1
        for pid in held:
            _run(["taskkill", "/PID", str(pid), "/F"], dry, say)
        time.sleep(0 if dry else 1.0)

    # 2. the new version
    say("Installing")
    if _run(pip_argv(channel), dry, say) != 0:
        say("The install failed (above). On Windows, WinError 32 means something still runs system-diff.exe: "
            "Get-Process system-diff shows what.")
        return 1

    # 3. one MCP server, started the right way
    claude = shutil.which("claude")
    key = plugin_key()
    if mcp:
        steps = mcp_steps(user_server(), bool(key), desired_server())
        if not claude:
            if steps:
                say("Claude Code's `claude` is not on PATH: register the MCP server yourself:")
                for s in steps:
                    say("  claude " + " ".join(s))
        elif steps:
            say("Registering system-diff's MCP server with Claude Code" + (" (the plugin brings it: one, not two)"
                                                                        if key else ""))
            for s in steps:
                _run([claude, *s], dry, say)
        else:
            say("Claude Code's MCP server: already right")

    # 4. the plugin
    if plugin and key and claude:
        say("Updating the Claude Code plugin")
        _run([claude, "plugin", "marketplace", "update", key.split("@", 1)[-1]], dry, say)
        _run([claude, "plugin", "update", key, "--scope", "user"], dry, say)

    if not dry:
        try:
            now = subprocess.run([sys.executable, "-m", "system_diff", "--version"], capture_output=True, text=True,
                                 timeout=60).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            now = ""
        say(f"Done: {now or 'updated'}. Restart Claude Code (or /mcp to reconnect), and `system-diff app` for the Studio.")
    if relaunched:
        try:
            ask("Press Enter to close this window ")
        except EOFError:
            pass
    return 0
