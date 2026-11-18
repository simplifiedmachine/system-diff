"""Several projects at once: a per-user registry of the running engines, so every engine's page can show one tab per
project and one address lists them all (`system-diff hub`, GET /projects/page).

Each repository has its own resident engine (`system-diff watch`), on the first free port from 8765 (watch.
start_detached); it keeps its own token in its repository's .git/system-diff/web.json. On start it also writes one
small file to a folder of this user's (engines_dir: %LOCALAPPDATA%\\system-diff\\engines on Windows, ~/Library/
Application Support/system-diff/engines on macOS, $XDG_STATE_HOME or ~/.local/state/system-diff/engines elsewhere):

    {"root", "name", "branch", "url", "port", "pid", "id", "version", "started"}

never its token. It removes the file when it stops. Whoever reads the folder (projects()) drops an entry whose process
is gone, or whose port answers GET /whoami with another id (another program took the port, or the engine was killed
and another one started there), so a crash leaves nothing behind for long.

The tabs are plain links to each engine's own page: each engine keeps serving its own repository, and no page reads
another engine's data (no CORS). GET /projects (token or same origin, like /web) is the registry cleaned of stale
entries, each with its current branch and whether it changed since its page was last looked at.

Only the standard library is imported here: watch.py imports this module at every engine's start.
"""
from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

ENV = "SYSTEM_DIFF_ENGINES"   # another registry folder (tests; several users sharing one home)
FIRST, LAST = 8765, 8774      # the ports engines take, the first free one each: where the hub is looked for
ENTRY_MAX = 16 * 1024         # an entry is a few hundred bytes: anything bigger is not one


def engines_dir(env=os.environ, platform: str = sys.platform) -> Optional[Path]:
    """This user's registry folder (not made here). None under pytest unless the test names one (ENV), so that the
    test suite's engines never show up as tabs in the person's own pages."""
    if env.get(ENV):
        return Path(env[ENV])
    if env.get("PYTEST_CURRENT_TEST"):
        return None
    if platform.startswith("win"):
        base = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "system-diff" / "engines"


def project_name(root: Path) -> str:
    """What a tab calls the repository: its folder's name, else the name of the repository its origin remote is."""
    name = Path(root).name
    if name:
        return name
    return remote_name(root) or str(root)


def remote_name(root: Path) -> str:
    """The origin remote's repository name ("system-diff" for git@github.com:me/system-diff.git), read from git's
    config file in Python; "" when there is none."""
    try:
        from . import gitfs
        r = gitfs.locate(Path(root))
        if r is None:
            return ""
        text = (r.common / "config").read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    inside = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            inside = s.replace(" ", "").lower() == '[remote"origin"]'
        elif inside and s.lower().startswith("url") and "=" in s:
            url = s.split("=", 1)[1].strip().rstrip("/")
            last = url.replace(":", "/").rsplit("/", 1)[-1]
            return last[:-4] if last.endswith(".git") else last
    return ""


def branch(root: Path) -> str:
    """The branch checked out, read in Python ("" when detached or unknown)."""
    try:
        from . import gitfs
        return gitfs.branch(Path(root)) or ""
    except Exception:  # noqa: BLE001 - reftable, not a repository: no branch shown
        return ""


def register(root: Path, url: str, port: int, ident: str, version: str, folder: Optional[Path] = None) -> Optional[Path]:
    """Write this engine's entry (atomically, readable by this user only). Its path, or None when there is no
    registry (under pytest) or it could not be written: the tabs are a convenience and never stop an engine."""
    folder = folder if folder is not None else engines_dir()
    if folder is None or not ident or not all(c in "0123456789abcdef" for c in ident):
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = {"root": str(root), "name": project_name(root), "branch": branch(root), "url": url, "port": port,
                 "pid": os.getpid(), "id": ident, "version": version, "started": round(time.time(), 3)}
        f = folder / f"{ident}.json"
        tmp = folder / f".{ident}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(entry, ensure_ascii=False))
        os.replace(tmp, f)
        return f
    except OSError:
        return None


def unregister(f: Optional[Path]) -> None:
    """Remove this engine's entry (when it stops)."""
    if f is None:
        return
    try:
        f.unlink()
    except OSError:
        pass


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":   # os.kill(pid, 0) would end the process there: /whoami says enough
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:   # another user's process: not this user's engine
        return False
    except OSError:
        return False
    return True


def whoami(port: int, timeout: float = 0.6) -> Any:
    """GET /whoami on this machine: its JSON (a dict); False when nothing answers there or it is not system-diff's;
    None when it did not answer in time (an engine busy reading: still there)."""
    try:   # nothing listens there: gone, at once (Windows retries a refused connection for seconds, which a short
        from .watch import _free   # timeout would take for a busy engine, keeping a dead one's tab for ever)
        if _free(port):
            return False
    except Exception:  # noqa: BLE001
        pass
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)   # no proxy: this machine only
    try:
        conn.request("GET", "/whoami", headers={"Accept": "application/json"})
        r = conn.getresponse()
        body = r.read(4096)
        if r.status != 200:
            return False
        info = json.loads(body.decode("utf-8"))
        return info if isinstance(info, dict) and info.get("app") == "system-diff" else False
    except (socket.timeout, TimeoutError):
        return None
    except (OSError, ValueError, UnicodeDecodeError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _moved(e: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """{"seq", "seen"} of another engine (GET /moved), with the token its own repository's web.json keeps (read
    here, by the same user, and sent to that engine only); {} when it cannot be told."""
    try:
        from .sources import git_dir
        info = json.loads((git_dir(Path(e["root"])) / "web.json").read_text(encoding="utf-8"))
        if info.get("id") != e["id"] or int(info.get("port") or 0) != e["port"]:
            return {}
        conn = http.client.HTTPConnection("127.0.0.1", e["port"], timeout=timeout)
        try:
            conn.request("GET", "/moved", headers={"X-Token": str(info.get("token") or "")})
            r = conn.getresponse()
            got = json.loads(r.read(4096).decode("utf-8")) if r.status == 200 else {}
        finally:
            conn.close()
        return got if isinstance(got, dict) and got.get("id") == e["id"] else {}
    except Exception:  # noqa: BLE001 - no dot, that is all
        return {}


def _read(f: Path) -> Optional[Dict[str, Any]]:
    try:
        if os.name != "nt" and f.stat().st_uid != os.getuid():   # only this user's own entries
            return None
        raw = f.read_bytes()
        if len(raw) > ENTRY_MAX:
            return None
        e = json.loads(raw.decode("utf-8"))
        port, pid = int(e["port"]), int(e["pid"])
        if not isinstance(e.get("id"), str) or not 0 < port < 65536:
            return None
        return {"root": str(e.get("root") or ""), "name": str(e.get("name") or ""), "branch": str(e.get("branch") or ""),
                "url": str(e.get("url") or f"http://127.0.0.1:{port}/"), "port": port, "pid": pid, "id": e["id"],
                "version": str(e.get("version") or ""), "started": float(e.get("started") or 0)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        return None


def projects(self_id: str = "", self_info: Optional[Dict[str, Any]] = None, folder: Optional[Path] = None,
             timeout: float = 0.6) -> List[Dict[str, Any]]:
    """The running engines, by name: each entry of the registry whose process is there and whose port answers
    /whoami with its id, with its current branch, "current" (it is `self_id`, the engine asking) and "changed" (its
    model moved since its page was last looked at). Stale entries are removed. `self_info` is what the asking engine
    would answer at /moved (it does not ask itself)."""
    folder = folder if folder is not None else engines_dir()
    if folder is None or not folder.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for f in sorted(folder.glob("*.json")):
        e = _read(f)
        if e is None:
            if not f.name.startswith("."):
                _drop(f, None)
            continue
        if f.stem != e["id"]:   # not a file an engine wrote (each writes <id>.json)
            continue
        mine = bool(self_id) and e["id"] == self_id
        info = (self_info or {}) if mine else (whoami(e["port"], timeout) if _alive(e["pid"]) else False)
        if info is False or (not mine and isinstance(info, dict) and (
                info.get("id") != e["id"] or int(info.get("pid") or e["pid"]) != e["pid"])):
            _drop(f, e)
            continue
        busy = info is None
        moved = (self_info or {}) if mine else (_moved(e, timeout) if not busy else {})
        seq, seen = moved.get("seq"), moved.get("seen")
        e.update(branch=branch(Path(e["root"])) or e["branch"] if e["root"] else e["branch"], current=mine,
                 changed=bool(not mine and isinstance(seq, int) and isinstance(seen, int) and seq > seen),
                 busy=busy)
        e.pop("pid", None)
        out.append(e)
    out.sort(key=lambda e: (e["name"].lower(), e["root"], e["port"]))
    return out


def _drop(f: Path, e: Optional[Dict[str, Any]]) -> None:
    """Remove a stale entry, unless it was rewritten meanwhile (an engine that just started on that id)."""
    try:
        if e is None or _read(f) == e:
            f.unlink()
    except OSError:
        pass


def find_hub(timeout: float = 0.6) -> Optional[int]:
    """The port of the first engine (or hub) from 8765 that serves the project list, if one runs."""
    for port in range(FIRST, LAST + 1):
        if _free(port):
            continue
        if isinstance(whoami(port, timeout), dict) and _serves_list(port, timeout):
            return port
    return None


def _serves_list(port: int, timeout: float) -> bool:
    """Whether the system-diff server on `port` serves the project list (one from before it did answers 404)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/projects/page", headers={"Host": f"localhost:{port}"})
        r = conn.getresponse()
        r.read()
        return r.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


class Exclusive:
    """A server socket that no other program can share. Python's HTTP servers ask for SO_REUSEADDR, which on Linux
    and macOS only lets a server reopen a port whose last connections are still closing, but on Windows lets a second
    program open a port that is in use: two engines (or an engine and the Studio) on one port, each request answered
    by either. On Windows the port is taken with SO_EXCLUSIVEADDRUSE instead; elsewhere nothing changes."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:   # type: ignore[override]
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()   # type: ignore[misc]


def hub_url(port: int) -> str:
    return f"http://localhost:{port}/projects/page"


def host_ok(host: Optional[str], port: int, env=os.environ) -> bool:
    """The request's Host is this machine's (or the Codespace's forwarded name for this port): a page on another site
    that points its own name at 127.0.0.1 (DNS rebinding) is refused."""
    if not host:
        return False
    h = host.strip().lower()
    if h in (f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"):
        return True
    name, domain = env.get("CODESPACE_NAME", ""), env.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
    return bool(name and domain) and h.split(":")[0] == f"{name}-{port}.{domain}".lower()


# ── the project list page (GET /projects/page): the same at every engine, and at `system-diff hub` ─────────────────

HUB_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>System diff projects</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: light; --ink:#161c24; --ink2:#3b4655; --muted:#5f6b7c; --line:#e1e5eb; --bg:#f8f9fb; --card:#fff;
  --work:#1f5fbf; --work-bg:#e8f0fc; --warn:#b4540a; --shadow:0 1px 2px rgba(22,28,36,.05);
  --sans:'Segoe UI', FreeSans, 'Helvetica Neue', Arial, sans-serif; --mono:Consolas, 'DejaVu Sans Mono', monospace; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark; --ink:#e4e9f0; --ink2:#c2cad6;
  --muted:#97a2b3; --line:#2a3240; --bg:#0f1319; --card:#161b23; --work:#6ea8ff; --work-bg:#16243a; --warn:#ffa95e; --shadow:none; } }
:root[data-theme="dark"] { color-scheme: dark; --ink:#e4e9f0; --ink2:#c2cad6; --muted:#97a2b3; --line:#2a3240; --bg:#0f1319;
  --card:#161b23; --work:#6ea8ff; --work-bg:#16243a; --warn:#ffa95e; --shadow:none; }
* { box-sizing:border-box; }
html, body { margin:0; background:var(--bg); color:var(--ink); }
body { font:14px/1.5 var(--sans); }
main { max-width:900px; margin:0 auto; padding:24px 16px 40px; display:grid; gap:14px; }
h1 { font-size:18px; margin:0; }
.sub { color:var(--muted); font-size:13px; margin:0; }
ul { list-style:none; margin:0; padding:0; display:grid; gap:10px; }
li.p { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 16px; box-shadow:var(--shadow);
       display:grid; gap:6px; }
.top { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }
.name { font-size:16px; font-weight:650; color:var(--ink); text-decoration:none; overflow-wrap:anywhere; }
.name:hover { color:var(--work); }
.branch { font:12.5px var(--mono); color:var(--muted); }
.dot { width:8px; height:8px; border-radius:50%; background:var(--warn); display:inline-block; }
.where { color:var(--muted); font:12px var(--mono); overflow-wrap:anywhere; }
.views { display:flex; gap:6px; flex-wrap:wrap; }
.views a { font-size:13px; padding:3px 11px; border-radius:999px; border:1px solid var(--line); color:var(--ink2);
           text-decoration:none; background:var(--bg); }
.views a:hover, .views a:focus-visible { border-color:var(--work); color:var(--work); }
a:focus-visible { outline:2px solid var(--work); outline-offset:2px; }
.empty { color:var(--muted); padding:20px; text-align:center; border:1px dashed var(--line); border-radius:12px; }
.error { color:#c4321c; } .error:empty { display:none; }
</style></head><body><main>
<h1>System diff: your projects</h1>
<p class="sub">Every repository with a running engine on this machine. Each has its own page; this list follows them as
they start and stop. Bookmark this page.</p>
<div class="error" id="error"></div>
<ul id="list"></ul>
</main><script>
const TOKEN = "__TOKEN__";
const VIEWS = [["now", "Now"], ["review", "Review"], ["agents", "Agents"], ["topics", "Topics"], ["history", "History"]];
__HREF__
function el(tag, text, cls) { const e = document.createElement(tag); if (text) e.textContent = text; if (cls) e.className = cls; return e; }
let last = "";
async function load() {
  try {
    const r = await fetch("/projects", { cache:"no-store", headers:{ "X-Token":TOKEN } });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    document.getElementById("error").textContent = "";
    const key = JSON.stringify(d.projects); if (key === last) return; last = key;
    const list = document.getElementById("list"), items = [];
    for (const p of d.projects) {
      const li = el("li", null, "p"), top = el("div", null, "top"), a = el("a", p.name || "(unnamed)", "name");
      a.href = projectHref(p.port, "now");
      top.append(a);
      if (p.branch) top.append(el("span", p.branch, "branch"));
      if (p.changed) { const dot = el("span", null, "dot"); dot.title = "changed since you last looked"; top.append(dot); }
      const views = el("div", null, "views");
      for (const [v, label] of VIEWS) { const l = el("a", label); l.href = projectHref(p.port, v); views.append(l); }
      li.append(top, el("div", p.root + " · port " + p.port, "where"), views);
      items.push(li);
    }
    if (!items.length) items.push(el("li", "No project is running. Open Claude Code in a repository with system-diff, " +
                                     "or run system-diff watch there.", "empty"));
    list.replaceChildren(...items);
  } catch (e) { document.getElementById("error").textContent = "Could not list the projects: " + (e.message || e); }
}
load(); setInterval(() => { if (!document.hidden) load(); }, 3000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
</script></body></html>
"""

# How a page links to another engine on this machine: the same host on that port, or, opened through a Codespace's
# forwarded address (https://<name>-<port>.<domain>/), that address for the other port.
HREF_JS = r"""function projectHref(port, view) {
  const here = String(location.port || ""), m = location.hostname.match(/^(.*-)(\d+)(\..+)$/);
  const tail = "/#view=" + encodeURIComponent(view || "now");
  if (!here && m) return location.protocol + "//" + m[1] + port + m[3] + tail;
  return location.protocol + "//" + location.hostname + ":" + port + tail;
}"""


def hub_page(token: str) -> bytes:
    return HUB_PAGE.replace("__HREF__", HREF_JS).replace("__TOKEN__", token).encode()


# ── `system-diff hub` ───────────────────────────────────────────────────────────────────────────────────────────────

class _HubServer(Exclusive, ThreadingHTTPServer):
    daemon_threads = True


def serve_hub(port: int = FIRST, ready: Optional[Callable[..., None]] = None) -> None:
    """Serve the project list page by itself (no engine runs to serve it), until interrupted: on the first free port
    from `port`. `ready(url, server)` once it listens (tests use port 0)."""
    token, ident = secrets.token_urlsafe(16), secrets.token_hex(16)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path, bound = urlsplit(self.path).path, self.server.server_address[1]
            if path == "/whoami":
                return self._send(200, json.dumps({"app": "system-diff", "id": ident, "pid": os.getpid()}).encode(),
                                  "application/json")
            if not host_ok(self.headers.get("Host"), bound):
                return self._send(403, b"forbidden", "text/plain")
            if path in ("/", "/projects/page"):
                return self._send(200, hub_page(token), "text/html; charset=utf-8")
            if path == "/projects":
                if not allowed(self.headers, token, bound):
                    return self._send(403, b'{"error": "forbidden"}', "application/json")
                return self._send(200, json.dumps({"projects": projects()}).encode(), "application/json")
            return self._send(404, b"not found", "text/plain")

    server = None
    for p in ([port] if port == 0 else range(port, port + 10)):
        try:
            server = _HubServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise OSError(f"no free port from {port}")
    url = hub_url(server.server_address[1])
    try:
        if ready:
            ready(url, server)
        else:
            print(f"system-diff hub: {url} (Ctrl-C to stop)", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def allowed(headers: Any, token: str, port: int, env=os.environ) -> bool:
    """The token (X-Token), or a request from a page of this very server (Origin, else Referer)."""
    import hmac
    given = headers.get("X-Token")
    if given and token and hmac.compare_digest(str(given), token):
        return True
    own = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    name, domain = env.get("CODESPACE_NAME", ""), env.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
    if name and domain:
        own.add(f"https://{name}-{port}.{domain}")
    origin = headers.get("Origin")
    if origin is not None:
        return origin.rstrip("/") in own
    ref = headers.get("Referer")
    if not ref:
        return False
    r = urlsplit(ref)
    return f"{r.scheme}://{r.netloc}" in own


def main(open_it: bool = False, serve: bool = True) -> int:
    """`system-diff hub`: print (and with `open_it`, open) the project list's address: the page of the first engine
    from port 8765, so the address stays the same while any engine runs there; when none runs, serve it here."""
    port = find_hub()
    if port is None:   # engines on other ports (watch --port 9000): the first of them
        port = min((e["port"] for e in projects()), default=None)
    if port is None:
        if not serve:
            print("system-diff hub: no engine is running (start one: system-diff watch, or open Claude Code)",
                  file=sys.stderr)
            return 1
        def ready(url: str, server: Any) -> None:
            print(f"system-diff hub: {url} (no engine runs: served from here until Ctrl-C)", flush=True)
            if open_it:
                threading.Thread(target=_open, args=(url,), daemon=True).start()
        serve_hub(FIRST, ready)
        return 0
    url = hub_url(port)
    print(url)
    name, domain = os.environ.get("CODESPACE_NAME", ""), os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "")
    if name and domain:
        print(f"https://{name}-{port}.{domain}/projects/page  (from your own browser)")
    if open_it:
        _open(url)
    return 0


def _open(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
