"""system-diff Studio (`system-diff app`): one local web app, on one port, that is the place to work from. Every
project (repository) on the left, its submodules and nested repositories under it; the project's page in the middle
(system-diff's tabs and every extension's: today's page, served through the app); a Claude Code session beside it,
run by the app through the Claude Agent SDK and driven from the page (sessions.py), with its settings, model and
permission mode picked from dropdowns (profiles.py), and the agents worth keeping (agentlib.py).

One app per user: app.json in the state folder (readable by this user only) says where it listens, with its token;
`system-diff app` on a running one opens its page. The app uses each repository's own engine (the detached
`system-diff watch` the terminal's sessions use too, started if none runs) and forwards /p/<project>/... to it: one
engine per repository, whoever started it. Every route checks the Host (no DNS rebinding: the sessions run code);
reads need the token or the app's own page; changes need both, as /send does.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

from . import hub

FIRST = 8765
APP = "app.json"
PROJECTS = "projects.json"
DEFAULTS = "defaults.json"
BODY_MAX = 512 * 1024
PROXY_TIMEOUT = 60.0


def state_dir(env=os.environ) -> Optional[Path]:
    """This user's state folder (beside the engines' registry); None under pytest unless a test names one."""
    if env.get("SYSTEM_DIFF_APP_STATE"):
        return Path(env["SYSTEM_DIFF_APP_STATE"])
    eng = hub.engines_dir(env)
    return eng.parent if eng is not None else None


def _write_private(f: Path, data: Any) -> None:
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, f)


def _read(f: Optional[Path]) -> Any:
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f is not None else None
    except (OSError, ValueError):
        return None


def running(folder: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The running app's {"url", "port", "pid", "token"}, if one runs (its pid lives and its port answers as the app)."""
    folder = folder if folder is not None else state_dir()
    got = _read(folder / APP) if folder is not None else None
    if not isinstance(got, dict) or not got.get("port"):
        return None
    if not hub._alive(int(got.get("pid") or 0)):
        return None
    return got if _is_app(int(got["port"])) else None


def _is_app(port: int, timeout: float = 0.8) -> bool:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/whoami")
        r = conn.getresponse()
        info = json.loads(r.read(4096).decode("utf-8")) if r.status == 200 else {}
        return isinstance(info, dict) and info.get("app") == "system-diff-studio"
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def pid_of(path: str) -> str:
    return hashlib.sha1(str(Path(path).resolve()).encode("utf-8", "surrogateescape")).hexdigest()[:12]


class Studio:
    """The app's state: its projects, its sessions, where each project's engine listens."""

    def __init__(self, folder: Optional[Path], token: str = "", manager: Any = None):
        self.folder = folder
        self.token = token or secrets.token_hex(24)
        self.manager = manager
        self.lock = threading.Lock()
        self.added: List[str] = []
        got = _read(folder / PROJECTS) if folder is not None else None
        if isinstance(got, dict) and isinstance(got.get("projects"), list):
            self.added = [str(p) for p in got["projects"] if isinstance(p, str)]
        self.defaults: Dict[str, Any] = {}
        got = _read(folder / DEFAULTS) if folder is not None else None
        if isinstance(got, dict):
            self.defaults = got

    def sessions(self):
        if self.manager is None:
            from . import sessions
            self.manager = sessions.Manager()
        return self.manager

    # ── projects ──────────────────────────────────────────────────────────────────────────────────────────────
    def add(self, path: str) -> Dict[str, Any]:
        p = Path(path).expanduser()
        if not p.is_dir():
            raise ValueError(f"no folder {path}")
        from .agent import find_root
        root = find_root(p.resolve())
        if root is None:
            raise ValueError(f"{p} is not in a git repository")
        with self.lock:
            if str(root) not in self.added:
                self.added.append(str(root))
                self._save_projects()
        return {"added": str(root), "id": pid_of(str(root))}

    def remove(self, path: str) -> None:
        with self.lock:
            self.added = [p for p in self.added if p != path and pid_of(p) != path]
            self._save_projects()

    def _save_projects(self) -> None:
        if self.folder is not None:
            _write_private(self.folder / PROJECTS, {"projects": self.added})

    def projects(self) -> List[Dict[str, Any]]:
        """The projects: the ones added, then every running engine's; each with its repositories inside."""
        from . import repos
        seen: Dict[str, Dict[str, Any]] = {}
        order: List[str] = list(self.added)
        try:
            for e in hub.projects():
                if e.get("root") and e["root"] not in order:
                    order.append(e["root"])
        except Exception:  # noqa: BLE001
            pass
        for root in order:
            outer = repos.parent(Path(root))
            if outer is not None and str(outer) in order:
                continue   # shown under its outer repository
            p = Path(root)
            if not p.is_dir():
                continue
            entry = self._entry(p, "outer" if root in self.added or outer is None else "nested")
            try:
                entry["kids"] = [self._entry((p / k["path"]).resolve(), k["kind"], k["path"]) for k in repos.inside(p)]
            except Exception:  # noqa: BLE001
                entry["kids"] = []
            seen[entry["id"]] = entry
        return list(seen.values())

    def _entry(self, p: Path, kind: str, rel: str = "") -> Dict[str, Any]:
        from . import repos
        eng = repos._engine(p)
        live = []
        if self.manager is not None:
            live = [s.head() for s in self.manager.of(p) if s.status not in ("closed", "failed")]
        return {"id": pid_of(str(p)), "path": str(p), "name": rel or p.name, "kind": kind, "branch": hub.branch(p),
                "port": eng.get("port") or 0, "added": str(p) in self.added, "sessions": len(live),
                "waiting": sum(1 for s in live if s["pending"])}

    def root_of(self, pid: str) -> Optional[Path]:
        for e in self.projects():
            if e["id"] == pid:
                return Path(e["path"])
            for k in e.get("kids") or []:
                if k["id"] == pid:
                    return Path(k["path"])
        for s in (self.manager.sessions.values() if self.manager is not None else []):
            if pid_of(str(s.project)) == pid:
                return s.project
        return None

    def engine(self, root: Path, wait: float = 0.0) -> Dict[str, Any]:
        """This repository's engine {"port", "token"}: the running one, else one started (waited for `wait` s)."""
        from . import repos, watch
        deadline = time.time() + wait
        started = False
        while True:
            info = _read(_web_json(root)) or {}
            if repos._engine(root).get("port") and info.get("token"):
                return {"port": int(info["port"]), "token": str(info["token"])}
            if not started:
                try:
                    watch.start_detached(root, wait=0)
                except Exception:  # noqa: BLE001
                    pass
                started = True
            if time.time() >= deadline:
                return {}
            time.sleep(0.3)

    # ── defaults per project ──────────────────────────────────────────────────────────────────────────────────
    def default_for(self, root: Path) -> Dict[str, Any]:
        got = self.defaults.get(str(root))
        return dict(got) if isinstance(got, dict) else {}

    def remember(self, root: Path, choice: Dict[str, Any]) -> None:
        with self.lock:
            self.defaults[str(root)] = {k: choice.get(k) for k in ("settings_profile", "model", "effort",
                                                                   "permission_mode", "setting_sources") if k in choice}
            if self.folder is not None:
                _write_private(self.folder / DEFAULTS, self.defaults)

    def deliver(self) -> None:
        """What a project's page sent to Claude goes to its open app session (when it has one)."""
        if self.manager is None:
            return
        roots = {s.project for s in self.manager.sessions.values() if s.status not in ("closed", "failed")}
        self.manager.deliver_outbox(sorted(roots))


def _web_json(root: Path) -> Path:
    from .sources import git_dir
    return git_dir(root) / "web.json"


def status(studio: Studio) -> Dict[str, Any]:
    from . import __version__, sessions
    import shutil
    m = studio.manager
    return {"app": "system-diff-studio", "version": __version__, "pid": os.getpid(),
            "sdk": sessions.sdk_available(), "claude_cli": shutil.which("claude") or "",
            "sessions": [s.head() for s in m.sessions.values()] if m is not None else []}


def handler(studio: Studio):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # ── plumbing ──────────────────────────────────────────────────────────────────────────────────────────
        def _send(self, code: int, body: bytes, kind: str, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json")

        def _query(self) -> Dict[str, str]:
            return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

        def _port(self) -> int:
            return self.server.server_address[1]

        def _host(self) -> bool:
            return hub.host_ok(self.headers.get("Host"), self._port())

        def _reader(self) -> bool:
            """The token, or this app's own page."""
            return hub.allowed(self.headers, studio.token, self._port())

        def _writer(self) -> bool:
            """The token and this app's own page (as the engine's /send): a change starts or steers code on this machine."""
            from .live import token_ok
            return token_ok(self.headers.get("X-Token"), studio.token) and self._same_origin()

        def _same_origin(self) -> bool:
            own = {f"http://localhost:{self._port()}", f"http://127.0.0.1:{self._port()}"}
            name, domain = os.environ.get("CODESPACE_NAME", ""), os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
            if name and domain:
                own.add(f"https://{name}-{self._port()}.{domain}")
            origin = self.headers.get("Origin")
            if origin is not None:
                return origin.rstrip("/") in own
            ref = self.headers.get("Referer") or ""
            r = urlsplit(ref)
            return bool(ref) and f"{r.scheme}://{r.netloc}" in own

        def _body(self) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > BODY_MAX:
                raise ValueError("the body is too big")
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode("utf-8")) if raw else {}

        def _drain(self) -> None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if 0 < n <= BODY_MAX:
                    self.rfile.read(n)
            except (ValueError, OSError):
                pass

        # ── GET ───────────────────────────────────────────────────────────────────────────────────────────────
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/whoami":
                return self._json(200, {"app": "system-diff-studio", "pid": os.getpid()})
            if not self._host():
                return self._send(403, b"forbidden", "text/plain")
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.replace("__TOKEN__", studio.token).encode("utf-8"), "text/html; charset=utf-8")
            if path == "/projects/page":   # the hub's list, as engines link to it
                return self._send(200, hub.hub_page(studio.token), "text/html; charset=utf-8")
            if path.startswith("/p/"):
                return self._proxy("GET", path)
            if not self._reader():
                return self._json(403, {"error": "forbidden"})
            q = self._query()
            try:
                if path == "/projects":
                    return self._json(200, {"projects": hub.projects()})
                if path == "/api/status":
                    return self._json(200, status(studio))
                if path == "/api/projects":
                    return self._json(200, {"projects": studio.projects()})
                if path == "/api/choices":
                    from . import agentlib, profiles
                    root = self._root(q)
                    got = profiles.choices(root)
                    got["default"] = studio.default_for(root)
                    got["agents"] = [a["name"] for a in agentlib.saved(root)]
                    from . import sessions
                    got["sdk"] = sessions.sdk_available()
                    return self._json(200, got)
                if path == "/api/agents":
                    from . import agentlib
                    return self._json(200, agentlib.library(self._root(q)))
                if path == "/api/sessions":
                    root = self._root(q)
                    m = studio.manager
                    return self._json(200, {"sessions": [s.head() for s in m.of(root)] if m is not None else []})
                if path == "/api/past":
                    from . import sessions
                    return self._json(200, {"past": sessions.past(self._root(q))})
                if path.startswith("/api/sessions/") and path.endswith("/events"):
                    s = studio.sessions().get(path.split("/")[3])
                    since = int(q.get("since") or 0)
                    wait = float(q.get("wait") or 0)
                    return self._json(200, s.events_since(since, wait))
            except KeyError:
                return self._json(404, {"error": "no such session"})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            return self._send(404, b"not found", "text/plain")

        def _root(self, q: Dict[str, str]) -> Path:
            pid = q.get("project", "")
            root = studio.root_of(pid) if pid else None
            if root is None:
                raise ValueError("no such project: add it on the left first")
            return root

        # ── POST ──────────────────────────────────────────────────────────────────────────────────────────────
        def do_POST(self):
            path = urlsplit(self.path).path
            if not self._host():
                self._drain()
                return self._send(403, b"forbidden", "text/plain")
            if path.startswith("/p/"):
                return self._proxy("POST", path)
            if not self._writer():
                self._drain()
                return self._json(403, {"error": "forbidden"})
            try:
                body = self._body()
                if not isinstance(body, dict):
                    raise ValueError("the body is a JSON object")
                return self._post(path, body)
            except KeyError:
                return self._json(404, {"error": "no such session"})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})

        def _post(self, path: str, b: Dict[str, Any]):
            from . import agentlib, profiles
            if path == "/api/projects":
                return self._json(200, studio.add(str(b.get("path") or "")))
            if path == "/api/projects/remove":
                studio.remove(str(b.get("path") or b.get("id") or ""))
                return self._json(200, {"ok": True})
            if path == "/api/projects/open":
                root = self._root({"project": str(b.get("project") or "")})
                got = studio.engine(root, wait=float(b.get("wait") or 0))
                return self._json(200, {"port": got.get("port") or 0, "starting": not got})
            if path == "/api/sessions":
                root = self._root({"project": str(b.get("project") or "")})
                opts = profiles.options_for(root, b)
                s = studio.sessions().start(root, opts, resume=str(b.get("resume") or ""), fork=bool(b.get("fork")))
                studio.remember(root, opts)
                if b.get("note"):
                    s.emit("note", text=str(b["note"])[:300])
                return self._json(200, s.head())
            if path.startswith("/api/sessions/"):
                parts = path.split("/")
                s = studio.sessions().get(parts[3])
                act = parts[4] if len(parts) > 4 else ""
                if act == "send":
                    text = str(b.get("text") or "")
                    agent = str(s.options.get("agent") or "")
                    if agent and not s.title:   # a saved agent picked: the first message goes to it
                        text = f"Use the `{agent}` subagent (Task tool, subagent_type {agent!r}) for this:\n\n{text}"
                    s.send(text)
                    return self._json(200, {"ok": True})
                if act == "interrupt":
                    s.interrupt()
                    return self._json(200, {"ok": True})
                if act == "permission":
                    s.answer(str(b.get("request") or ""), bool(b.get("allow")), bool(b.get("always")),
                             str(b.get("message") or ""))
                    return self._json(200, {"ok": True})
                if act == "close":
                    s.close()
                    return self._json(200, {"ok": True})
                if act == "fork":   # the conversation so far, continued apart: this one goes on as it was
                    if not s.claude_id:
                        raise ValueError("this session has no conversation yet to fork")
                    opts = profiles.options_for(s.project, b) if b.get("settings_profile") else dict(s.options)
                    t = studio.sessions().start(s.project, opts, resume=s.claude_id, fork=True)
                    t.emit("note", text=f"A fork of session {s.id}: the same conversation so far, going its own way")
                    return self._json(200, t.head())
                if act == "switch":   # new options, the same conversation: a new session resuming it
                    if not s.claude_id:
                        raise ValueError("this session has no conversation yet: just start a new one")
                    opts = profiles.options_for(s.project, b)
                    s.close()
                    t = studio.sessions().start(s.project, opts, resume=s.claude_id)
                    t.emit("note", text=f"Continues session {s.id} with new settings: "
                                        f"{opts.get('model') or 'the default model'}, {opts['permission_mode']}, "
                                        f"{opts['settings_profile']}")
                    studio.remember(s.project, opts)
                    return self._json(200, t.head())
            if path == "/api/agents/save":
                root = self._root({"project": str(b.get("project") or "")})
                return self._json(200, agentlib.save_agent(root, str(b.get("name") or ""),
                                                           str(b.get("description") or ""), str(b.get("prompt") or ""),
                                                           str(b.get("tools") or ""), str(b.get("model") or ""),
                                                           str(b.get("scope") or "project")))
            if path == "/api/agents/forget":
                root = self._root({"project": str(b.get("project") or "")})
                return self._json(200, agentlib.forget(root, str(b.get("name") or ""), str(b.get("scope") or "project")))
            return self._json(404, {"error": "not found"})

        # ── the project's engine, through the app ─────────────────────────────────────────────────────────────
        def _proxy(self, method: str, path: str):
            """/p/<project>/<rest> → that repository's engine, with its token. Only this app's own page (or the
            token) gets through, as the engine itself would ask; the engine's page is served to any local load."""
            pid, _, rest = path[3:].partition("/")
            rest = "/" + rest
            q = urlsplit(self.path).query
            root = studio.root_of(pid)
            if root is None:
                self._drain()
                return self._json(404, {"error": "no such project"})
            page = method == "GET" and rest in ("/", "/index.html")
            if not page and not (self._reader() if method == "GET" else (self._reader() and (
                    self._same_origin() or self.headers.get("X-Token")))):
                self._drain()
                return self._json(403, {"error": "forbidden"})
            body = b""
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n < 0 or n > BODY_MAX:
                    return self._json(400, {"error": "the body is too big"})
                body = self.rfile.read(n) if n else b""
            eng = studio.engine(root, wait=20.0 if page else 5.0)
            if not eng:
                if page:
                    return self._send(200, STARTING.encode("utf-8"), "text/html; charset=utf-8")
                return self._json(503, {"error": "the project's engine is starting"})
            headers = {"X-Token": eng["token"], "Host": f"127.0.0.1:{eng['port']}"}
            if method == "POST":
                headers["Content-Type"] = self.headers.get("Content-Type") or "application/json"
                headers["Origin"] = f"http://127.0.0.1:{eng['port']}"    # the engine's /send asks for its own page
                headers["Referer"] = f"http://127.0.0.1:{eng['port']}/"
            conn = http.client.HTTPConnection("127.0.0.1", eng["port"], timeout=PROXY_TIMEOUT)
            try:
                conn.request(method, rest + (("?" + q) if q else ""), body=body or None, headers=headers)
                r = conn.getresponse()
                data = r.read()
                kind = r.getheader("Content-Type") or "application/octet-stream"
                if page and kind.startswith("text/html"):
                    data = data.replace(b"<head>", b'<head><base href="/p/' + pid.encode() + b'/">', 1)
                return self._send(r.status, data, kind)
            except (OSError, http.client.HTTPException) as exc:
                return self._json(502, {"error": f"the project's engine did not answer: {type(exc).__name__}"})
            finally:
                conn.close()

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"


def serve(port: int = FIRST, open_it: bool = False, folder: Optional[Path] = None, ready=None,
          manager: Any = None) -> int:
    """Run the Studio until interrupted (one per user: a running one is opened instead)."""
    folder = folder if folder is not None else state_dir()
    have = running(folder)
    if have is not None:
        url = f"http://localhost:{have['port']}/"
        print(f"system-diff app: already running at {url}", flush=True)
        if open_it:
            hub._open(url)
        return 0
    studio = Studio(folder, manager=manager)
    server = None
    for p in ([port] if port == 0 else range(port, port + 10)):
        try:
            server = _Server(("127.0.0.1", p), handler(studio))
            break
        except OSError:
            continue
    if server is None:
        print(f"system-diff app: no free port from {port}", file=sys.stderr)
        return 1
    bound = server.server_address[1]
    url = f"http://localhost:{bound}/"
    if folder is not None:
        _write_private(folder / APP, {"url": url, "port": bound, "pid": os.getpid(), "token": studio.token,
                                      "started": round(time.time(), 3)})
    stop = threading.Event()

    def deliver_loop() -> None:
        while not stop.wait(1.0):
            try:
                studio.deliver()
            except Exception:  # noqa: BLE001 - the next second tries again
                pass
    threading.Thread(target=deliver_loop, name="system-diff app outbox", daemon=True).start()
    try:
        if ready:
            ready(url, server, studio)
        else:
            print(f"system-diff app: {url} (Ctrl-C to stop)", flush=True)
            name, domain = os.environ.get("CODESPACE_NAME", ""), os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
            if name and domain:
                print(f"  from your own browser: https://{name}-{bound}.{domain}/", flush=True)
        if open_it:
            threading.Thread(target=hub._open, args=(url,), daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if studio.manager is not None:
            studio.manager.stop()
        server.server_close()
        if folder is not None:
            got = _read(folder / APP)
            if isinstance(got, dict) and got.get("pid") == os.getpid():
                try:
                    (folder / APP).unlink()
                except OSError:
                    pass
    return 0


STARTING = """<!doctype html><meta charset="utf-8"><title>Starting</title>
<body style="font:15px system-ui;padding:24px;color:#555">The project's engine is starting (a first read of a large
repository takes a while)… <script>setTimeout(() => location.reload(), 2000)</script>"""

from .app_page import PAGE  # noqa: E402  (the page is long: its own module)
