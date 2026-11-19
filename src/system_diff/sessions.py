"""Claude Code sessions run by the Studio (`system-diff app`) and driven from its page, through the Claude Agent SDK
(`pip install "system-diff[app]"`; it drives the Claude Code CLI with the person's own login and gateways).

A Session wraps one ClaudeSDKClient on the app's asyncio loop (one thread for all sessions). Everything it receives
becomes an event in its log, kept in memory and in .git/system-diff/sessions/<id>.jsonl, so a reload or a second tab
replays it and follows new ones by long poll (`events(since)`). A tool call that needs the person's say waits as a
"permission" event until the page answers (allow, allow always, deny). The diagrams follow the session: its hooks run
the same code the plugin's hooks run (agent.run_hook), so the engine marks the turn and sees each edit, and Claude
gets the same context. The repository's system-diff MCP server is given to every session, its read-only tools allowed.

The SDK is imported only when a session starts; tests give a fake client (`client_factory`).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "dontAsk", "auto", "bypassPermissions")
MODELS = ("opus", "sonnet", "haiku", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2")
KEEP_EVENTS = 5000          # in memory per session; the log file keeps all
LONG_POLL = 25.0
TEXT_MOST = 4000            # a tool's input or result as shown: cut past this
READ_ONLY_TOOLS = ("ui_view", "ui_graph", "ui_source", "ui_config_preview", "explain", "objectives", "team",
                   "system_map", "system_diff")
APPEND = (
    "You run inside system-diff's Studio, a web page: there is no Shift+Tab or /permissions here; the person sets the "
    "permission mode and the model in the Studio's Session options, which apply to this session at once, and allows "
    "or denies each action on a card on the page. "
    "The person sees the system-diff page beside this chat, with the system's "
    "diagram. Read ui_view when they say 'this', 'here' or 'the one I picked'. While explaining, ui_show and point_to "
    "the parts you name. The diagrams are computed from the code and system-diff.toml: change them through their "
    "sources (ui_source finds them, ui_config_preview shows the result first), and never claim a change the page does "
    "not show.")


def plugin_enabled(project: Path, sources: List[str], home: Optional[Path] = None) -> bool:
    """Whether the session will load system-diff's Claude Code plugin (enabled in a settings file it reads): then its
    MCP server is the plugin's, and the app adds none of its own."""
    home = home if home is not None else Path.home() / ".claude"
    files = {"user": [home / "settings.json"], "project": [Path(project) / ".claude" / "settings.json"],
             "local": [Path(project) / ".claude" / "settings.local.json"]}
    for src in sources:
        for f in files.get(src, []):
            try:
                got = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            on = (got.get("enabledPlugins") or {}) if isinstance(got, dict) else {}
            if isinstance(on, dict) and any(str(k).split("@")[0] == "system-diff" and v for k, v in on.items()):
                return True
    return False


def configured_servers(project: Path, sources: List[str], home: Optional[Path] = None) -> set:
    """The MCP servers the session's own settings already bring, by name: `claude mcp add` at user scope and local
    scope (~/.claude.json) and project scope (.mcp.json), as far as the sources it loads reach."""
    home = home if home is not None else Path.home()
    names: set = set()
    try:
        cj = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cj = {}
    if isinstance(cj, dict):
        if "user" in sources:
            names |= set((cj.get("mcpServers") or {}).keys())
        if "local" in sources:
            proj = (cj.get("projects") or {}).get(str(Path(project).resolve())) or {}
            names |= set((proj.get("mcpServers") or {}).keys()) if isinstance(proj, dict) else set()
    if "project" in sources:
        try:
            mj = json.loads((Path(project) / ".mcp.json").read_text(encoding="utf-8"))
            names |= set((mj.get("mcpServers") or {}).keys()) if isinstance(mj, dict) else set()
        except (OSError, ValueError):
            pass
    return {str(n) for n in names}


_READ_ONLY: Dict[str, List[str]] = {}


def extension_read_only(ext: Any) -> List[str]:
    """The tools an extension marks read-only (its MCP annotations), found by registering them on a throwaway server."""
    name = str(getattr(ext, "NAME", "") or "")
    if name in _READ_ONLY:
        return _READ_ONLY[name]
    out: List[str] = []
    add = getattr(ext, "mcp", None)
    if add is not None:
        def probe() -> None:
            try:
                from .mcp_server import Server
                srv = Server("probe")
                add(srv)
                for t in asyncio.run(srv.list_tools()):
                    a = t.model_dump(by_alias=True, exclude_none=True).get("annotations") or {}
                    if a.get("readOnlyHint"):
                        out.append(t.name)
            except Exception:  # noqa: BLE001 - then none is allowed outright: each asks
                pass
        th = threading.Thread(target=probe, daemon=True)   # its own loop: the caller may be inside one
        th.start()
        th.join(10)
    _READ_ONLY[name] = out
    return out


def extension_servers(project: Path, configured: set, sd_ours: bool) -> Dict[str, Any]:
    """The extensions' own MCP servers for a session: {"servers": {name: config}, "allowed": [tool names], "off":
    [extension names whose tools system-diff's own server leaves out, since their own server brings them]}. One is
    added when it applies to this repository and the settings do not bring a server of that name already; one whose
    tools system-diff's server carries and cannot leave out (the plugin's, or one the person added) is not added."""
    import importlib.util
    out: Dict[str, Any] = {"servers": {}, "allowed": [], "off": []}
    try:
        from . import extensions
        exts = extensions.load()
    except Exception:  # noqa: BLE001
        return out
    for ext in exts:
        spec = getattr(ext, "MCP_SERVER", None)
        name = str(getattr(ext, "NAME", "") or "")
        if not isinstance(spec, dict) or not name or not spec.get("command"):
            continue
        applies = getattr(ext, "applies", None)
        try:
            if applies is not None and not applies(Path(project)):
                continue
        except Exception:  # noqa: BLE001
            continue
        command = str(spec["command"])
        known = {name, str(spec.get("name") or ""), Path(command).stem} - {""}
        if known & configured:
            continue   # the person's settings bring it already
        has_tools = getattr(ext, "mcp", None) is not None
        if has_tools and not sd_ours:
            continue   # system-diff's server (the plugin's, or the person's) carries its tools already
        args = [str(a) for a in spec.get("args") or []]
        top = getattr(ext, "__name__", "").split(".")[0]
        try:
            runnable = bool(top) and importlib.util.find_spec(top + ".__main__") is not None
        except (ImportError, ValueError):
            runnable = False
        argv = [sys.executable, "-m", top, *args] if runnable else [command, *args]
        out["servers"][name] = {"type": "stdio", "command": argv[0], "args": argv[1:]}
        if has_tools:
            out["off"].append(name)
        out["allowed"] += [f"mcp__{name}__{t}" for t in extension_read_only(ext)]
    return out


def sdk_available() -> Optional[str]:
    """The installed Agent SDK's version, or None."""
    try:
        import claude_agent_sdk
        return str(getattr(claude_agent_sdk, "__version__", "") or "installed")
    except Exception:  # noqa: BLE001
        return None


def _cut(text: Any, most: int = TEXT_MOST) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return s if len(s) <= most else s[:most] + f"… ({len(s) - most} more characters)"


def _redact(text: str) -> str:
    try:
        from .extract import redact
        return redact(text)
    except Exception:  # noqa: BLE001
        return text


def managed_settings_files() -> List[Path]:
    """Where an organization's managed Claude Code settings live (they win over everything the person sets)."""
    if os.name == "nt":
        return [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ClaudeCode" / "managed-settings.json",
                Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ClaudeCode" / "managed-settings.json"]
    if sys.platform == "darwin":
        return [Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
    return [Path("/etc/claude-code/managed-settings.json")]


def asked_why(project: Path, tool: str, options: Dict[str, Any]) -> str:
    """Why Claude Code asked although the mode chosen should not have: the `ask` rules that name this tool in the
    settings this session reads, an organization's policy turning bypass off, else the remaining reasons."""
    files: List[Tuple[str, Path]] = [("your organization's managed settings", f) for f in managed_settings_files()]
    sources = options.get("setting_sources")
    sources = ["user", "project", "local"] if sources is None else list(sources)
    home = Path.home() / ".claude"
    if "user" in sources:
        files.append(("~/.claude/settings.json", home / "settings.json"))
    if "project" in sources:
        files.append((".claude/settings.json", Path(project) / ".claude" / "settings.json"))
    if "local" in sources:
        files.append((".claude/settings.local.json", Path(project) / ".claude" / "settings.local.json"))
    if options.get("settings"):
        files.append((Path(str(options["settings"])).name, Path(str(options["settings"]))))
    found: List[str] = []
    for label, f in files:
        try:
            perms = (json.loads(f.read_text(encoding="utf-8")) or {}).get("permissions") or {}
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(perms, dict):
            continue
        rules = [str(r) for r in perms.get("ask") or [] if str(r).split("(")[0].strip() in (tool, "*")
                 or str(r).startswith("mcp__") and tool.startswith(str(r).split("(")[0])]
        if rules:
            found.append(f"an ask rule in {label}: {', '.join(rules[:4])}")
        if str(perms.get("disableBypassPermissionsMode") or "") == "disable":
            found.append(f"bypass is turned off by {label} (disableBypassPermissionsMode)")
    if found:
        return "Claude Code asks because of " + "; ".join(found) + "."
    return ("Claude Code asked without saying why, though this session should not ask: an ask rule or a hook "
            "outside the files system-diff reads, or a check Claude Code keeps in every mode.")


def tool_input_view(name: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """What the page shows of a tool call: its file and, for an edit, what changes; a command; else the input, cut."""
    data = data if isinstance(data, dict) else {"input": data}
    out: Dict[str, Any] = {}
    if data.get("file_path"):
        out["file"] = str(data["file_path"])
    if name in ("Edit", "MultiEdit"):
        edits = data.get("edits") or [{"old_string": data.get("old_string", ""), "new_string": data.get("new_string", "")}]
        out["diff"] = [{"old": _cut(e.get("old_string", ""), 1500), "new": _cut(e.get("new_string", ""), 1500)}
                       for e in edits[:8] if isinstance(e, dict)]
    elif name == "Write":
        out["diff"] = [{"old": "", "new": _cut(data.get("content", ""), 3000)}]
    elif name == "Bash":
        out["command"] = _cut(data.get("command", ""), 2000)
        if data.get("description"):
            out["description"] = str(data["description"])[:300]
    if not out:
        out["input"] = _cut(data, 2000)
    return out


class Session:
    """One Claude Code conversation from the page."""

    def __init__(self, manager: "Manager", project: Path, options: Dict[str, Any], sid: str = "",
                 resume: str = "", fork: bool = False):
        self.manager, self.project = manager, Path(project)
        self.id = sid or uuid.uuid4().hex[:12]
        self.options = dict(options)
        self.resume, self.fork = resume, fork
        self.events: List[Dict[str, Any]] = []
        self.first = 0                    # the number of the first event kept in memory
        self.cond = threading.Condition()
        self.status = "starting"
        self.claude_id = resume or ""
        self.model = ""
        self.error = ""
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.queue: Optional[asyncio.Queue] = None
        self.client: Any = None
        self.created = time.time()
        self.title = ""
        self.key_env: Optional[Dict[str, str]] = None   # the keys stored for this project (vault.py), read at start
        self.key_list: List[Dict[str, Any]] = []
        self.masks: List[tuple] = []
        self._held = ""                                  # streamed text held back while it may end in part of a key
        self.turn_at = 0.0                # when the turn under way began (0: none)
        self.last_at = time.time()        # when Claude Code last said anything
        self.doing: Dict[str, Any] = {}   # what it does now: {"what", "tool", "detail", "agent"}
        self.tools: Dict[str, Dict[str, Any]] = {}   # this turn's tool calls, by id: name, label, when begun
        self.forget = False               # closed by the person: not offered to continue after a restart
        self._log = self._log_path()

    # ── the log ───────────────────────────────────────────────────────────────────────────────────────────────
    def _log_path(self) -> Optional[Path]:
        try:
            from .sources import git_dir
            return git_dir(self.project, "sessions") / f"{self.id}.jsonl"
        except Exception:  # noqa: BLE001 - kept in memory only
            return None

    def load_keys(self) -> Dict[str, str]:
        """The keys stored for this project, read once as the session starts: its environment, and what its events
        mask (a stored value never reaches the page or the log)."""
        if self.key_env is not None:
            return self.key_env
        self.key_env, problems = {}, []
        try:
            from . import vault
            folder = getattr(self.manager, "vault_folder", None)
            env = vault.env_for(self.project, folder, problems)
            listed = [k for k in vault.listing(self.project, folder) if k["name"] in env]
            self.key_list = list({k["name"]: k for k in listed}.values())
            self.masks = vault.masks_of(env, listed)
            self.key_env = env   # last: whoever sees the keys sees their names and masks too
        except Exception as exc:  # noqa: BLE001 - the session starts without them
            problems.append(type(exc).__name__)
        if problems:
            self.emit("note", text="Keys that could not be read, so this session goes without them: "
                                   + "; ".join(problems)[:400])
        return self.key_env

    def _mask(self, obj: Any) -> Any:
        if not self.masks:
            return obj
        from .vault import mask
        return mask(obj, self.masks)

    def _delta(self, text: str) -> None:
        """Streamed text, masked: with keys, the tail that may be the start of one waits for the next piece."""
        if not self.masks:
            self.emit("delta", keep=False, text=text)
            return
        self._held = self._mask(self._held + text)
        back = min(256, max(len(v) for v, _ in self.masks) - 1)
        out, self._held = (self._held[:-back], self._held[-back:]) if len(self._held) > back else ("", self._held)
        if out:
            self.emit("delta", keep=False, text=out)

    def emit(self, kind: str, keep: bool = True, **data: Any) -> Dict[str, Any]:
        data = self._mask(data)
        with self.cond:
            ev = {"n": self.first + len(self.events), "at": round(time.time(), 3), "type": kind, **data}
            self.events.append(ev)
            if len(self.events) > KEEP_EVENTS:
                drop = len(self.events) - KEEP_EVENTS
                del self.events[:drop]
                self.first += drop
            self.cond.notify_all()
        if keep and self._log is not None:
            try:
                with open(self._log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass
        return ev

    def set_status(self, status: str, **extra: Any) -> None:
        self.status = status
        self.emit("status", status=status, **extra)
        if status == "working":
            self.turn_at = self.last_at = time.time()
            self.tools.clear()
            self.state("thinking")
        elif status in ("idle", "closed", "failed"):
            self.turn_at = 0.0
            self.doing = {}
        if self.claude_id and not self.forget:
            note_open(self)

    # -- what it is doing now (the column's status line) --
    def state(self, what: str, tool: str = "", detail: str = "", agent: str = "") -> None:
        """What Claude Code does now: thinking, writing (a reply), tool (running one). Said to the page when it
        changes (not kept in the log)."""
        now = {"what": what, "tool": tool, "detail": detail[:160], "agent": agent[:120]}
        if now != self.doing:
            self.doing = now
            self.emit("state", keep=False, now=self.now())

    def now(self) -> Dict[str, Any]:
        """{"what", "tool", "detail", "agent", "turn_for", "quiet_for"} while a turn is under way, else {}. "what" is
        "waiting" while a question waits for the person."""
        if not self.turn_at:
            return {}
        t = time.time()
        what = "waiting" if self.pending else (self.doing.get("what") or "thinking")
        return self._mask({**self.doing, "what": what, "turn_for": round(t - self.turn_at, 1),
                           "quiet_for": round(t - self.last_at, 1)})   # a command's text may hold a stored value

    def _agent_of(self, msg: Any) -> str:
        parent = getattr(msg, "parent_tool_use_id", None)
        if not parent:
            return ""
        t = self.tools.get(parent) or {}
        return t.get("label") or "a subagent"

    def head(self) -> Dict[str, Any]:
        return {"id": self.id, "project": str(self.project), "status": self.status, "claude_session": self.claude_id,
                "mode": getattr(self, "mode", "") or self.options.get("permission_mode") or "",
                "model": self.model or self.options.get("model") or "", "options": self.public_options(),
                "title": self.title, "created": self.created, "error": self.error, "now": self.now(),
                "keys": [k["name"] for k in self.key_list],
                "pending": [{"request": k, **{x: self._mask(v[x]) for x in ("tool", "view")}}
                            for k, v in self.pending.items()]}

    def public_options(self) -> Dict[str, Any]:
        o = self.options
        return {k: o.get(k) for k in ("model", "effort", "permission_mode", "settings_profile", "setting_sources",
                                      "agent")}

    def events_since(self, since: int, wait: float = 0.0) -> Dict[str, Any]:
        deadline = time.time() + max(0.0, min(wait, LONG_POLL))
        with self.cond:
            while self.first + len(self.events) <= since and time.time() < deadline:
                self.cond.wait(max(0.05, deadline - time.time()))
            start = max(0, since - self.first)
            got = self.events[start:]
            gap = since < self.first
        return {"session": self.head(), "events": got, "next": self.first + len(self.events), "gap": gap}

    # ── the conversation ──────────────────────────────────────────────────────────────────────────────────────
    def send(self, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            raise ValueError("nothing to send")
        if self.status in ("closed", "failed"):
            raise ValueError(f"the session is {self.status}: start a new one (or resume it)")
        if not self.title:
            self.title = self._mask(text)[:80]
        self.emit("user", text=text)
        self.manager.call(self._put(text))

    async def _put(self, text: str) -> None:
        if self.queue is None:
            self.queue = asyncio.Queue()
        await self.queue.put(text)

    def interrupt(self) -> None:
        if self.client is not None and self.status in ("working", "waiting"):
            for req in list(self.pending):   # a question waiting for the person ends with the turn
                self.answer(req, False, message="interrupted")
            self.manager.call(self.client.interrupt())
            self.emit("interrupt")

    def answer(self, request: str, allow: bool, always: bool = False, message: str = "") -> None:
        p = self.pending.pop(request, None)
        if p is None:
            raise ValueError("no such question waiting (answered already, or the turn ended)")
        fut: asyncio.Future = p["future"]
        self.emit("permission_done", request=request, allow=bool(allow), always=bool(always and allow),
                  message=str(message or "")[:500])
        decision = {"allow": bool(allow), "always": bool(always), "message": str(message or ""),
                    "suggestions": p.get("suggestions")}

        def settle() -> None:
            if not fut.done():
                fut.set_result(decision)
        self.manager.loop.call_soon_threadsafe(settle)
        if not self.pending and self.status == "waiting":
            self.set_status("working")

    def apply_live(self, model: str, mode: str) -> None:
        """Change the model and the permission mode of this open session now (the SDK can; settings, effort and
        agent wait for the next session)."""
        if self.client is None or self.status in ("closed", "failed"):
            return
        said = []
        if model and model != self.options.get("model") and hasattr(self.client, "set_model"):
            self.manager.call(self.client.set_model(model))
            self.options["model"] = model
            said.append(f"model {model}")
        if mode in PERMISSION_MODES and mode != self.options.get("permission_mode") and hasattr(self.client,
                                                                                               "set_permission_mode"):
            self.manager.call(self.client.set_permission_mode(mode))
            self.options["permission_mode"] = mode
            said.append(f"permissions {mode}")
        if said:
            self.emit("note", text="Now: " + ", ".join(said) + " (this session, from here on)")

    def close(self, forget: bool = True) -> None:
        """End the session. `forget`: the person closed it, so it is not offered to continue after a restart; the
        Studio stopping (forget=False) keeps it offered."""
        if forget:
            self.forget = True
            forget_open(self)
        if self.status == "closed":
            return
        for req in list(self.pending):
            self.answer(req, False, message="the session was closed")
        self.manager.call(self._stop())

    async def _stop(self) -> None:
        if self.queue is None:
            self.queue = asyncio.Queue()
        await self.queue.put(None)

    # ── what the SDK calls ────────────────────────────────────────────────────────────────────────────────────
    async def can_use_tool(self, tool_name: str, tool_input: Dict[str, Any], context: Any) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        decision = await self.ask(tool_name, tool_input, context)
        if decision["allow"]:
            updates = decision.get("suggestions") if decision.get("always") else None
            return PermissionResultAllow(updated_input=tool_input, updated_permissions=updates or None)
        return PermissionResultDeny(message=decision.get("message") or "The person said no on the system-diff page.",
                                    interrupt=False)

    async def ask(self, tool_name: str, tool_input: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        """Put the question on the page and wait for the person's answer."""
        req = uuid.uuid4().hex[:10]
        fut = self.manager.loop.create_future()
        suggestions = list(getattr(context, "suggestions", None) or [])
        view = tool_input_view(tool_name, tool_input)
        self.pending[req] = {"future": fut, "tool": tool_name, "view": view, "suggestions": suggestions}
        why = str(getattr(context, "decision_reason", "") or getattr(context, "description", "") or "")
        if getattr(context, "agent_id", None):
            why = ("A subagent asks. " + why).strip()
        if not getattr(context, "decision_reason", None) and (getattr(self, "mode", "") or self.options.get(
                "permission_mode")) in ("bypassPermissions", "dontAsk", "acceptEdits"):
            why = (why + " " + asked_why(self.project, tool_name, self.options)).strip()
        self.emit("permission", request=req, tool=tool_name, view=view, why=why[:400],
                  can_always=bool(suggestions))
        if self.status != "waiting":
            self.set_status("waiting")
        return await fut

    def hooks(self) -> Dict[str, Any]:
        """The SDK's hooks: the plugin's own hook code, run for this session (the engine marks the turn, sees edits,
        and Claude gets the same context)."""
        from claude_agent_sdk import HookMatcher

        async def run(hook_input: Dict[str, Any], tool_use_id: Optional[str], context: Any) -> Dict[str, Any]:
            said: List[str] = []
            env = {k: v for k, v in os.environ.items() if k != "SYSTEM_DIFF_APP_SESSION"}
            env["CLAUDE_PROJECT_DIR"] = str(self.project)
            payload = json.dumps({**dict(hook_input), "cwd": str(self.project)}, default=str)
            try:
                from . import agent
                await asyncio.get_running_loop().run_in_executor(None, lambda: agent.run_hook(payload, said.append, env))
            except Exception as exc:  # noqa: BLE001 - a hook never stops the session
                self.emit("system", text=f"system-diff's hook failed: {type(exc).__name__}: {exc}", keep=False)
                return {}
            for line in said:
                try:
                    got = json.loads(line)
                except ValueError:
                    continue
                if isinstance(got, dict):
                    return got
            return {}
        return {ev: [HookMatcher(hooks=[run])] for ev in ("UserPromptSubmit", "PostToolUse", "Stop", "SubagentStart",
                                                          "SubagentStop")}

    def sdk_options(self) -> Any:
        import warnings
        from claude_agent_sdk import ClaudeAgentOptions
        # the read-only tools are allowed outright, on purpose: the SDK's note that they skip the callback is not news
        warnings.filterwarnings("ignore", message="can_use_tool will not be invoked")
        o = self.options
        mode = o.get("permission_mode") or "default"
        sources = list(o.get("setting_sources") if o.get("setting_sources") is not None
                       else ["user", "project", "local"])
        kw: Dict[str, Any] = {
            "cwd": str(self.project), "permission_mode": mode, "can_use_tool": self.can_use_tool,
            "include_partial_messages": True, "hooks": self.hooks(), "setting_sources": sources,
            "system_prompt": {"type": "preset", "preset": "claude_code", "append": APPEND},
            "env": {**self.load_keys(), "SYSTEM_DIFF_APP_SESSION": "1"},   # the keys stored for it, by name
        }
        try:
            from .vault import prompt_note
            kw["system_prompt"]["append"] = APPEND + prompt_note(self.key_list)
        except Exception:  # noqa: BLE001
            pass
        configured = configured_servers(self.project, sources)
        plugin = plugin_enabled(self.project, sources)
        sd_ours = not plugin and "system-diff" not in configured
        ext = extension_servers(self.project, configured, sd_ours)
        servers: Dict[str, Any] = dict(ext["servers"])
        if plugin:   # the plugin brings the same server: one, not two
            allowed = [f"mcp__plugin_system-diff_system-diff__{t}" for t in READ_ONLY_TOOLS]
        else:
            if sd_ours:
                servers["system-diff"] = {"type": "stdio", "command": sys.executable,
                                          "args": ["-m", "system_diff", "mcp"],
                                          **({"env": {"SYSTEM_DIFF_EXT_TOOLS_OFF": ",".join(ext["off"])}}
                                             if ext["off"] else {})}
            allowed = [f"mcp__system-diff__{t}" for t in READ_ONLY_TOOLS]
        kw["allowed_tools"] = allowed + ext["allowed"]
        if servers:
            kw["mcp_servers"] = servers
        try:   # the person's library agents of this project's kinds (research, software), with no file written
            from . import agentlib
            lib = agentlib.sdk_agents(self.project)
            if lib:
                kw["agents"] = lib
        except Exception:  # noqa: BLE001 - the session starts without them
            pass
        if o.get("model"):
            kw["model"] = str(o["model"])
        if o.get("settings"):
            kw["settings"] = str(o["settings"])
        if o.get("effort") in EFFORTS:
            kw["effort"] = o["effort"]
        if self.resume:
            kw["resume"] = self.resume
            if self.fork:
                kw["fork_session"] = True
        return ClaudeAgentOptions(**kw)

    def record(self, msg: Any) -> None:
        """One SDK message as events."""
        kind = type(msg).__name__
        self.last_at = time.time()
        agent = self._agent_of(msg)
        if kind == "StreamEvent":
            ev = getattr(msg, "event", None) or {}
            ev = ev if isinstance(ev, dict) else {}
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                if not agent:
                    self._delta(delta["text"])
                self.state("writing", agent=agent)
            elif ev.get("type") == "content_block_start":
                block = ev.get("content_block") or {}
                bt = block.get("type") if isinstance(block, dict) else ""
                if bt in ("thinking", "redacted_thinking"):
                    self.state("thinking", agent=agent)
                elif bt == "text":
                    self.state("writing", agent=agent)
                elif bt in ("tool_use", "server_tool_use", "mcp_tool_use"):
                    self.state("tool", tool=str(block.get("name") or ""), detail="preparing the call", agent=agent)
            elif ev.get("type") == "message_start":
                self.state("thinking", agent=agent)
            return
        self._held = ""   # the whole text follows (masked): what was held back is not needed
        if kind == "AssistantMessage":
            self.model = getattr(msg, "model", "") or self.model
            for b in getattr(msg, "content", None) or []:
                bk = type(b).__name__
                if bk == "TextBlock":
                    self.emit("text", text=b.text, model=self.model, sub=bool(getattr(msg, "parent_tool_use_id", None)))
                elif bk == "ThinkingBlock":
                    if (b.thinking or "").strip():   # often withheld: nothing to show then
                        self.emit("thinking", text=_cut(b.thinking, 6000))
                elif bk == "ToolUseBlock":
                    label = tool_label(b.name, b.input if isinstance(b.input, dict) else {})
                    self.tools[b.id] = {"name": b.name, "label": label, "at": time.time()}
                    self.emit("tool_use", id=b.id, name=b.name, view=tool_input_view(b.name, b.input), label=label,
                              agent=agent, sub=bool(getattr(msg, "parent_tool_use_id", None)))
                    self.state("tool", tool=b.name, detail=label, agent=agent)
            return
        if kind == "UserMessage":
            content = getattr(msg, "content", None)
            for b in content if isinstance(content, list) else []:
                if type(b).__name__ == "ToolResultBlock":
                    c = b.content
                    text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                    began = (self.tools.get(b.tool_use_id) or {}).get("at")
                    self.emit("tool_result", id=b.tool_use_id, error=bool(b.is_error), text=_cut(_redact(text or ""), 3000),
                              ms=int((time.time() - began) * 1000) if began else None)
                    if b.tool_use_id in self.tools:
                        self.tools[b.tool_use_id]["done"] = True   # kept: a subagent's messages name it by this id
                    running = [t for t in self.tools.values() if not t.get("done")]
                    if running:
                        self.state("tool", tool=running[-1]["name"], detail=running[-1]["label"], agent=agent)
                    else:
                        self.state("thinking", agent=agent)
            return
        if kind == "SystemMessage":
            data = getattr(msg, "data", None) or {}
            if getattr(msg, "subtype", "") == "init" and isinstance(data, dict):
                self.claude_id = str(data.get("session_id") or self.claude_id)
                self.model = str(data.get("model") or self.model)
                self.mode = str(data.get("permissionMode") or self.options.get("permission_mode") or "")
                wanted = str(self.options.get("permission_mode") or "")
                if wanted and self.mode and self.mode != wanted and self.client is not None \
                        and hasattr(self.client, "set_permission_mode"):
                    # a settings file chose another mode (its defaultMode): the one chosen here is the one it runs in
                    self.manager.call(self.client.set_permission_mode(wanted))
                    self.emit("note", text=f"Claude Code started in {self.mode} (from your settings); now {wanted}, "
                                           "as chosen here")
                    self.mode = wanted
                if not self.forget:
                    note_open(self)
                self.emit("system", subtype="init", model=self.model, claude_session=self.claude_id,
                          tools=len(data.get("tools") or []), permission_mode=self.mode,
                          mcp=[s.get("name") for s in data.get("mcp_servers") or [] if isinstance(s, dict)])
            return
        if kind == "ResultMessage":
            self.claude_id = str(getattr(msg, "session_id", "") or self.claude_id)
            if not self.forget:
                note_open(self)
            usage = getattr(msg, "usage", None) or {}
            self.emit("result", cost=getattr(msg, "total_cost_usd", None), usage=usage if isinstance(usage, dict) else {},
                      model=self.model, terminal_reason=str(getattr(msg, "terminal_reason", "") or ""),
                      error=bool(getattr(msg, "is_error", False)), turns=getattr(msg, "num_turns", None),
                      ms=getattr(msg, "duration_ms", None), claude_session=self.claude_id)

    async def run(self) -> None:
        self.queue = self.queue or asyncio.Queue()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self.load_keys)   # a keyring may take a moment
        except Exception:  # noqa: BLE001
            pass
        try:
            self.client = self.manager.client_factory(self)
            await self.client.connect()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            self.set_status("failed", error=self.error)
            return
        self.set_status("idle")
        try:
            while True:
                text = await self.queue.get()
                if text is None:
                    break
                self.set_status("working")
                try:
                    await self.client.query(text)
                    async for msg in self.client.receive_response():
                        self.record(msg)
                except Exception as exc:  # noqa: BLE001 - this turn failed; the session goes on
                    self.emit("error", text=f"{type(exc).__name__}: {exc}")
                self.set_status("idle")
        finally:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            if self.status != "failed":
                self.set_status("closed")


def _sdk_client(session: Session) -> Any:
    from claude_agent_sdk import ClaudeSDKClient
    return ClaudeSDKClient(options=session.sdk_options())


class Manager:
    """The app's sessions, on one asyncio loop in a thread of its own."""

    def __init__(self, client_factory: Callable[[Session], Any] = _sdk_client):
        self.client_factory = client_factory
        self.vault_folder: Optional[Path] = None   # where the keys are (the Studio's state folder): vault.py
        self.sessions: Dict[str, Session] = {}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="system-diff sessions", daemon=True)
        self.thread.start()
        self.lock = threading.Lock()

    def call(self, coro) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def start(self, project: Path, options: Dict[str, Any], resume: str = "", fork: bool = False) -> Session:
        mode = options.get("permission_mode") or "default"
        if mode not in PERMISSION_MODES:
            raise ValueError(f"permission_mode is one of {', '.join(PERMISSION_MODES)}")
        if self.client_factory is _sdk_client and sdk_available() is None:
            raise ValueError('the Claude Agent SDK is not installed: pip install "system-diff[app]"')
        s = Session(self, project, options, resume=resume, fork=fork)
        with self.lock:
            self.sessions[s.id] = s
        s.emit("start", options=s.public_options(), resume=resume, fork=fork)
        if resume:   # what was said before, so the conversation reads on from there
            try:
                got = history(project, resume)
                if got["entries"]:
                    s.emit("history", keep=False, claude_session=resume, **got)
            except Exception:  # noqa: BLE001 - the conversation goes on without it
                pass
            if not fork:
                drop_open(resume)   # continued here: this session is the one offered from now on
        self.call(s.run())
        return s

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if s is None:
            raise KeyError(sid)
        return s

    def of(self, project: Path) -> List[Session]:
        p = str(Path(project).resolve())
        return sorted((s for s in self.sessions.values() if str(s.project.resolve()) == p), key=lambda s: -s.created)

    def active(self, project: Path) -> Optional[Session]:
        """The session the page's Send to Claude goes to: the newest open one of the project."""
        return next((s for s in self.of(project) if s.status not in ("closed", "failed")), None)

    def deliver_outbox(self, projects: List[Path]) -> int:
        """What the page (and extensions: outbox_put) asked to send to Claude goes to the project's active session."""
        n = 0
        for root in projects:
            s = self.active(root)
            if s is None:
                continue
            try:
                from . import chat, pane
                items = pane.take_outbox(root)
            except Exception:  # noqa: BLE001
                continue
            for it in items:
                try:
                    s.send(it.get("text") or "")
                    n += 1
                except ValueError:
                    continue
            if items:
                try:
                    chat.delivered(root, items)
                except Exception:  # noqa: BLE001
                    pass
        return n

    def stop(self, wait: float = 5.0) -> None:
        """Close every session (each disconnects its Claude Code), then stop the loop. Once."""
        if getattr(self, "_stopped", False) or not self.loop.is_running():
            return
        self._stopped = True
        for s in list(self.sessions.values()):
            s.close(forget=False)   # the Studio stops: each is offered to continue after its restart
        deadline = time.time() + wait
        while time.time() < deadline and any(s.status not in ("closed", "failed") for s in self.sessions.values()):
            time.sleep(0.05)

        async def end() -> None:
            me = asyncio.current_task()
            rest = [t for t in asyncio.all_tasks() if t is not me]
            for t in rest:
                t.cancel()
            await asyncio.gather(*rest, return_exceptions=True)
            self.loop.stop()
        try:
            asyncio.run_coroutine_threadsafe(end(), self.loop)
        except RuntimeError:
            return
        self.thread.join(wait)


# -- past conversations: Claude Code's transcripts of this project, like /resume --

HISTORY_MOST = 200       # the entries of a past conversation shown at once ("show earlier" for more)
KEEP_PARSED = 4000       # the entries of one transcript kept parsed (the newest)
FULL_SCAN = 8 << 20      # transcripts bigger than this are listed from a quick count, not parsed
OPEN_FILE = "open-sessions.json"   # the Studio's sessions not closed by the person: offered after a restart
_SCANS: Dict[str, Any] = {}        # transcript path -> ((size, mtime), parsed)
_OPEN_LOCK = threading.Lock()


def tool_label(name: str, data: Dict[str, Any]) -> str:
    """A tool call in a few words: its command, its file, its pattern, what it asks; an MCP tool by its server."""
    data = data if isinstance(data, dict) else {}
    for k in ("description", "command", "file_path", "notebook_path", "pattern", "url", "query", "prompt", "path"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            if k == "file_path" or k == "notebook_path":
                v = v.replace("\\", "/").rsplit("/", 2)
                v = "/".join(v[-2:])
            return v[:140]
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"{parts[1] if len(parts) > 1 else ''}: {parts[-1]}"
    return ""


def transcripts_dir(project: Path) -> Path:
    """Where Claude Code keeps this project's transcripts: ~/.claude/projects/<its path, every other character a ->"""
    import re
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(project).resolve()))
    base = Path.home() / ".claude" / "projects"
    d = base / slug
    if not d.is_dir() and len(slug) > 200 and base.is_dir():   # a long path: cut, with a hash after it
        hit = sorted(base.glob(slug[:200] + "*"))
        if hit:
            return hit[0]
    return d


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _shown_prompt(text: str) -> str:
    """A prompt as the person wrote it: Claude Code's own wrappers (commands, caveats, reminders) are not theirs."""
    t = text.strip()
    if not t or t.startswith(("<local-command", "<command-", "Caveat:", "<system-reminder>", "[Request interrupted")):
        return ""
    return t


def _parse(f: Path) -> Dict[str, Any]:
    """One transcript read: its entries for the feed (the newest KEEP_PARSED), title, first prompt, counts."""
    entries: List[Dict[str, Any]] = []
    total = prompts = replies = 0
    first = title = summary = branch = ""
    last = ""
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            kind = d.get("type")
            if kind == "summary":
                summary = str(d.get("summary") or summary)
                continue
            if kind in ("custom-title", "title") or d.get("customTitle"):
                title = str(d.get("customTitle") or d.get("title") or title)
                continue
            if kind not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
                continue
            branch = str(d.get("gitBranch") or branch)
            last = str(d.get("timestamp") or last)
            m = d.get("message") or {}
            content = m.get("content")
            got: List[Dict[str, Any]] = []
            if kind == "user":
                said = _shown_prompt(_text_of(content))
                if said:
                    got.append({"type": "user", "text": _cut(said, 6000)})
                    prompts += 1
                    first = first or said[:200]
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        text = c if isinstance(c, str) else _text_of(c)
                        got.append({"type": "tool_result", "id": str(b.get("tool_use_id") or ""),
                                    "error": bool(b.get("is_error")), "text": _cut(_redact(text or ""), 600)})
            else:
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and str(b.get("text") or "").strip():
                        got.append({"type": "text", "text": _cut(b["text"], 20000)})
                        replies += 1
                    elif b.get("type") == "tool_use":
                        data = b.get("input") if isinstance(b.get("input"), dict) else {}
                        got.append({"type": "tool_use", "id": str(b.get("id") or ""), "name": str(b.get("name") or ""),
                                    "view": tool_input_view(str(b.get("name") or ""), data),
                                    "label": tool_label(str(b.get("name") or ""), data)})
            for e in got:
                e["i"] = total
                e["past"] = True
                total += 1
                entries.append(e)
            if len(entries) > KEEP_PARSED * 2:
                del entries[:len(entries) - KEEP_PARSED]
    return {"entries": entries[-KEEP_PARSED:], "total": total, "prompts": prompts, "replies": replies,
            "first": first, "title": title, "summary": summary, "branch": branch, "last": last}


def _quick(f: Path) -> Dict[str, Any]:
    """A big transcript listed without parsing it: its first prompt, and its messages counted roughly."""
    first, n = "", 0
    with open(f, "rb") as fh:
        for raw in fh:
            if b'"type":"assistant"' in raw:
                n += 1
            elif not first and b'"type":"user"' in raw:
                try:
                    d = json.loads(raw)
                    first = _shown_prompt(_text_of((d.get("message") or {}).get("content")))[:200]
                except ValueError:
                    pass
    return {"entries": None, "total": 0, "prompts": 0, "replies": n, "first": first, "title": "", "summary": "",
            "branch": "", "last": "", "rough": True}


def scan(f: Path, full: bool = False) -> Dict[str, Any]:
    """A transcript, read once while its size and time stay the same."""
    st = f.stat()
    stamp = (st.st_size, st.st_mtime_ns)
    got = _SCANS.get(str(f))
    if got is not None and got[0] == stamp and (got[1]["entries"] is not None or not full):
        return got[1]
    parsed = _parse(f) if full or st.st_size <= FULL_SCAN else _quick(f)
    _SCANS[str(f)] = (stamp, parsed)
    while len(_SCANS) > 80:
        del _SCANS[next(iter(_SCANS))]
    return parsed


def history(project: Path, claude_id: str, before: Optional[int] = None, most: int = HISTORY_MOST) -> Dict[str, Any]:
    """A past conversation's entries for the feed: the newest `most` (before entry number `before`), and how many
    earlier ones there are. {"entries", "earlier", "total"}."""
    import re
    if not re.match(r"^[A-Za-z0-9-]{8,80}$", str(claude_id or "")):
        raise ValueError("not a conversation id")
    f = transcripts_dir(project) / f"{claude_id}.jsonl"
    if not f.is_file():
        return {"entries": [], "earlier": 0, "total": 0}
    got = scan(f, full=True)
    rows = got["entries"] or []
    if before is not None:
        rows = [e for e in rows if e["i"] < int(before)]
    shown = rows[-max(1, min(int(most), 1000)):]
    earlier = shown[0]["i"] if shown else 0
    return {"entries": shown, "earlier": earlier, "kept_from": rows[0]["i"] if rows else 0, "total": got["total"]}


def _open_path() -> Optional[Path]:
    try:
        from .app import state_dir
        d = state_dir()
    except Exception:  # noqa: BLE001
        return None
    return d / OPEN_FILE if d is not None else None


def _open_read() -> Dict[str, Any]:
    f = _open_path()
    try:
        got = json.loads(f.read_text(encoding="utf-8")) if f is not None else {}
    except (OSError, ValueError):
        got = {}
    return got if isinstance(got, dict) else {}


def _open_write(got: Dict[str, Any]) -> None:
    f = _open_path()
    if f is None:
        return
    try:
        from .app import _write_private
        _write_private(f, got)
    except OSError:
        pass


def note_open(s: "Session") -> None:
    """This session is open (not closed by the person): after a restart of the Studio, it is offered to continue.
    Keyed by its conversation, so a continued one replaces the entry it came from."""
    if not s.claude_id:
        return
    with _OPEN_LOCK:
        got = _open_read()
        was = got.get(s.claude_id) or {}
        now = {"project": str(s.project), "title": s.title or was.get("title") or "", "studio": s.id,
               "status": s.status, "at": round(time.time(), 3), "options": s.public_options()}
        if {k: v for k, v in was.items() if k != "at"} == {k: v for k, v in now.items() if k != "at"} \
                and time.time() - float(was.get("at") or 0) < 30:
            return
        got[s.claude_id] = now
        _open_write(got)


def forget_open(s: "Session") -> None:
    if s.claude_id:
        drop_open(s.claude_id)


def drop_open(claude_id: str) -> None:
    with _OPEN_LOCK:
        got = _open_read()
        if got.pop(claude_id, None) is not None:
            _open_write(got)


def left_off(project: Path, live: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """The conversations of this project the Studio had open when it (or its machine) stopped, by id: not the ones
    open now (`live`: their conversation ids)."""
    here = os.path.normcase(str(Path(project).resolve()))
    out = {}
    for cid, e in _open_read().items():
        if not isinstance(e, dict) or cid in (live or []):
            continue
        try:
            if os.path.normcase(str(Path(e.get("project") or "").resolve())) != here:
                continue
        except (OSError, ValueError):
            continue
        out[cid] = e
    return out


def past(project: Path, live: Optional[List[str]] = None, most: int = 50) -> List[Dict[str, Any]]:
    """Claude Code's past conversations of this project (its transcripts), newest first, to resume one, each
    {"id", "first_prompt", "title", "summary", "branch", "modified" (ms), "size", "messages", "left_off" (the
    Studio had it open when it stopped)}. The ones left off first. Read once while a transcript is unchanged."""
    folder = transcripts_dir(project)
    stopped = left_off(project, live)
    files = sorted(folder.glob("*.jsonl"), key=lambda f: -f.stat().st_mtime) if folder.is_dir() else []
    out = []
    for f in files[:most]:
        try:
            got = scan(f)
            st = f.stat()
        except OSError:
            continue
        if not got["first"] and not got["replies"] and f.stem not in stopped:
            continue   # nothing said in it (a session opened and left)
        out.append({"id": f.stem, "first_prompt": got["first"], "summary": got["summary"][:200],
                    "title": got["title"] or (stopped.get(f.stem) or {}).get("title", ""), "branch": got["branch"],
                    "modified": int(st.st_mtime * 1000), "size": st.st_size,
                    "messages": got["prompts"] + got["replies"], "rough": bool(got.get("rough")),
                    "left_off": f.stem in stopped, "options": (stopped.get(f.stem) or {}).get("options") or {}})
    if not out and not files:
        try:   # no folder where it was looked for: the SDK knows where Claude Code keeps them
            from claude_agent_sdk import list_sessions
            for x in list_sessions(str(project), limit=most):
                out.append({"id": x.session_id, "first_prompt": (x.first_prompt or "")[:200],
                            "summary": (x.summary or "")[:200], "title": x.custom_title or "",
                            "branch": x.git_branch or "", "modified": x.last_modified, "size": x.file_size,
                            "messages": 0, "rough": True, "left_off": x.session_id in stopped, "options": {}})
        except Exception:  # noqa: BLE001
            pass
    out.sort(key=lambda x: (not x["left_off"], -int(x["modified"] or 0)))
    return out
