"""`system-diff watch`: a live page of what changed in the system since the checkpoint, for a person to keep beside the
editor while they, or an AI coding agent, change the code. It redraws when a file changes; "Mark as seen" moves the
checkpoint, so the page then shows only what is new to them.

Local only: the server listens on 127.0.0.1, and "Mark as seen" needs the page's own token, so no other web page open in
the same browser can press it.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from . import graph
from .compare import checkpoint, checkpoint_sha, compare, worktree_tree

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>System diff</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { --ink:#161c24; --muted:#667385; --line:#e1e5eb; --bg:#f8f9fb; --work:#1f5fbf; }
* { box-sizing: border-box; }
body { margin:0; font:14px/1.45 'Segoe UI', FreeSans, 'Helvetica Neue', Arial, sans-serif; color:var(--ink); background:var(--bg); }
header { position:sticky; top:0; display:flex; flex-wrap:wrap; gap:12px; align-items:center; padding:12px 20px;
         background:#fff; border-bottom:1px solid var(--line); }
h1 { font-size:15px; margin:0; } .since { color:var(--muted); font-size:12.5px; }
button { margin-left:auto; border:1px solid var(--work); background:var(--work); color:#fff; border-radius:8px;
         padding:6px 14px; font:inherit; font-weight:600; cursor:pointer; } button:disabled { opacity:.5; cursor:default; }
main { padding:16px 20px; display:grid; gap:14px; }
.headline { font-size:16px; font-weight:600; } .error { color:#c4321c; white-space:pre-wrap; }
.picture { overflow:auto; background:#fff; border:1px solid var(--line); border-radius:10px; padding:8px; }
.picture svg { display:block; max-width:none; }
pre { margin:0; padding:12px 14px; background:#fff; border:1px solid var(--line); border-radius:10px; overflow:auto;
      font:12.5px/1.5 Consolas, Inconsolata, 'DejaVu Sans Mono', monospace; }
.quiet { color:var(--muted); }
</style></head><body>
<header><h1>System diff</h1><span class="since" id="since"></span>
<button id="seen" type="button" title="Move the checkpoint here: the page then shows only what is new">Mark as seen</button></header>
<main><div class="headline" id="headline">Reading the code…</div><div class="error" id="error"></div>
<div class="picture" id="picture" hidden></div><pre id="plan" hidden></pre></main>
<script>
const TOKEN = "__TOKEN__";
let version = -1;
function show(s) {
  version = s.version;
  document.getElementById("since").textContent = "since " + s.base + " · updated " + s.at;
  document.getElementById("headline").textContent = s.headline;
  document.getElementById("error").textContent = s.error || "";
  const pic = document.getElementById("picture"), plan = document.getElementById("plan");
  pic.hidden = !s.svg; pic.innerHTML = s.svg || "";
  plan.hidden = !s.changes; plan.textContent = s.plan;
  document.getElementById("seen").disabled = !s.changes;
}
async function poll() {
  try {
    const r = await fetch("state?since=" + version, { cache: "no-store" });
    if (r.status === 200) show(await r.json());
  } catch (e) { document.getElementById("error").textContent = "The watcher stopped: start system-diff watch again."; }
  setTimeout(poll, 1500);
}
document.getElementById("seen").onclick = async () => {
  const r = await fetch("seen", { method: "POST", headers: { "X-Token": TOKEN } });
  if (r.status === 200) show(await r.json());
};
poll();
</script></body></html>
"""


class Watcher:
    """The current diff, recomputed in the background whenever the working tree or the checkpoint moves."""

    def __init__(self, root: Path, base: str = "checkpoint", config_path: Optional[str] = None, interval: float = 2.0):
        self.root, self.base, self.config_path, self.interval = root, base, config_path, interval
        self.token = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.state: Dict[str, Any] = {"version": 0, "headline": "Reading the code…", "plan": "", "svg": "",
                                      "changes": 0, "base": base, "at": "", "error": ""}
        self.seen_key: Optional[tuple] = None
        self.stop = threading.Event()
        # One look at the files at a time: otherwise a slow look that started before "Mark as seen" could finish
        # after it and put the old diff back on the page.
        self.busy = threading.Lock()

    def key(self) -> tuple:
        return worktree_tree(self.root), checkpoint_sha(self.root)

    def refresh(self) -> None:
        try:
            c = compare(self.root, self.base, ".", self.config_path)
            svg = graph.diff_svg(c.diff, c.before, c.after, c.cfg) or ""
            new = {"headline": c.diff["headline"], "plan": c.diff["plan"], "svg": svg,
                   "changes": len(c.diff["changes"]), "base": c.base, "error": ""}
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
            new = {"error": f"{type(exc).__name__}: {exc}"}
        with self.lock:
            self.state.update(new, version=self.state["version"] + 1, at=time.strftime("%H:%M:%S"))

    def tick(self, force: bool = False) -> None:
        with self.busy:
            try:
                key = self.key()
            except (OSError, subprocess.CalledProcessError) as exc:
                with self.lock:
                    self.state.update(error=f"git: {exc}", version=self.state["version"] + 1)
                return
            if force or key != self.seen_key:
                self.seen_key = key
                self.refresh()

    def loop(self) -> None:
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(self.interval)

    def mark_seen(self) -> None:
        with self.busy:
            checkpoint(self.root)
        self.tick(force=True)  # the page's answer is the diff from the new checkpoint, never an older look

    def snapshot_state(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.state)


def handler(w: Watcher):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet: the terminal shows the URL, not every poll
            pass

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE.replace("__TOKEN__", w.token).encode(), "text/html; charset=utf-8")
            if self.path.startswith("/state"):
                return self._send(200, json.dumps(w.snapshot_state()).encode(), "application/json")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/seen" or self.headers.get("X-Token") != w.token:
                return self._send(403, b"forbidden", "text/plain")
            w.mark_seen()
            return self._send(200, json.dumps(w.snapshot_state()).encode(), "application/json")

    return Handler


def serve(root: Path, base: str = "checkpoint", port: int = 8765, config_path: Optional[str] = None,
          interval: float = 2.0, ready=None) -> None:
    """Run until interrupted. `ready(url)` is called once the server listens (tests use port 0)."""
    w = Watcher(root, base, config_path, interval)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(w))
    thread = threading.Thread(target=w.loop, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    if ready:
        ready(url, server, w)
    else:
        print(f"system-diff watch: {url} (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        w.stop.set()
        server.server_close()
