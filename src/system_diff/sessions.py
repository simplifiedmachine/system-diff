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
from typing import Any, Callable, Dict, List, Optional

EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "dontAsk", "auto", "bypassPermissions")
MODELS = ("opus", "sonnet", "haiku", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2")
KEEP_EVENTS = 5000          # in memory per session; the log file keeps all
LONG_POLL = 25.0
TEXT_MOST = 4000            # a tool's input or result as shown: cut past this
READ_ONLY_TOOLS = ("ui_view", "ui_graph", "ui_source", "ui_config_preview", "explain", "objectives", "team",
                   "system_map", "system_diff")
APPEND = (
    "You run inside system-diff's Studio: the person sees the system-diff page beside this chat, with the system's "
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
        self._log = self._log_path()

    # ── the log ───────────────────────────────────────────────────────────────────────────────────────────────
    def _log_path(self) -> Optional[Path]:
        try:
            from .sources import git_dir
            return git_dir(self.project, "sessions") / f"{self.id}.jsonl"
        except Exception:  # noqa: BLE001 - kept in memory only
            return None

    def emit(self, kind: str, keep: bool = True, **data: Any) -> Dict[str, Any]:
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

    def head(self) -> Dict[str, Any]:
        return {"id": self.id, "project": str(self.project), "status": self.status, "claude_session": self.claude_id,
                "model": self.model or self.options.get("model") or "", "options": self.public_options(),
                "title": self.title, "created": self.created, "error": self.error,
                "pending": [{"request": k, **{x: v[x] for x in ("tool", "view")}} for k, v in self.pending.items()]}

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
            self.title = text[:80]
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

    def close(self) -> None:
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
        return {ev: [HookMatcher(hooks=[run])] for ev in ("UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop")}

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
            "env": {"SYSTEM_DIFF_APP_SESSION": "1"},
        }
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
        if kind == "StreamEvent":
            ev = getattr(msg, "event", None) or {}
            delta = (ev.get("delta") or {}) if isinstance(ev, dict) else {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                self.emit("delta", keep=False, text=delta["text"])
            return
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
                    self.emit("tool_use", id=b.id, name=b.name, view=tool_input_view(b.name, b.input),
                              sub=bool(getattr(msg, "parent_tool_use_id", None)))
            return
        if kind == "UserMessage":
            content = getattr(msg, "content", None)
            for b in content if isinstance(content, list) else []:
                if type(b).__name__ == "ToolResultBlock":
                    c = b.content
                    text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                    self.emit("tool_result", id=b.tool_use_id, error=bool(b.is_error), text=_cut(_redact(text or ""), 3000))
            return
        if kind == "SystemMessage":
            data = getattr(msg, "data", None) or {}
            if getattr(msg, "subtype", "") == "init" and isinstance(data, dict):
                self.claude_id = str(data.get("session_id") or self.claude_id)
                self.model = str(data.get("model") or self.model)
                self.emit("system", subtype="init", model=self.model, claude_session=self.claude_id,
                          tools=len(data.get("tools") or []), mcp=[s.get("name") for s in data.get("mcp_servers") or []
                                                                   if isinstance(s, dict)])
            return
        if kind == "ResultMessage":
            self.claude_id = str(getattr(msg, "session_id", "") or self.claude_id)
            usage = getattr(msg, "usage", None) or {}
            self.emit("result", cost=getattr(msg, "total_cost_usd", None), usage=usage if isinstance(usage, dict) else {},
                      model=self.model, terminal_reason=str(getattr(msg, "terminal_reason", "") or ""),
                      error=bool(getattr(msg, "is_error", False)), turns=getattr(msg, "num_turns", None),
                      ms=getattr(msg, "duration_ms", None), claude_session=self.claude_id)

    async def run(self) -> None:
        self.queue = self.queue or asyncio.Queue()
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
            s.close()
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


def past(project: Path) -> List[Dict[str, Any]]:
    """Claude Code's own past sessions of this repository (its transcripts), newest first, to resume one."""
    try:
        from claude_agent_sdk import list_sessions
        got = list_sessions(str(project), limit=50)
        return [{"id": x.session_id, "first_prompt": (x.first_prompt or "")[:200], "summary": (x.summary or "")[:200],
                 "title": x.custom_title or "", "branch": x.git_branch or "", "modified": x.last_modified,
                 "size": x.file_size} for x in got]
    except Exception:  # noqa: BLE001 - without the SDK: read the transcripts' folder
        pass
    import re
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(project).resolve()))
    folder = Path.home() / ".claude" / "projects" / slug
    out = []
    for f in sorted(folder.glob("*.jsonl"), key=lambda f: -f.stat().st_mtime)[:50] if folder.is_dir() else []:
        first = ""
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    m = d.get("message") or {}
                    if d.get("type") == "user" and isinstance(m.get("content"), str):
                        first = m["content"][:200]
                        break
        except OSError:
            continue
        st = f.stat()
        out.append({"id": f.stem, "first_prompt": first, "summary": "", "title": "", "branch": "",
                    "modified": int(st.st_mtime * 1000), "size": st.st_size})
    return out
