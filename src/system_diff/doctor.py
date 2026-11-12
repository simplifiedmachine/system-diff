"""`system-diff doctor`: why system-diff is slow, or quiet, on this machine. It times one read of the system the way
the pane does, stage by stage, with every git call it makes, and checks what usually explains a slow or quiet setup:
untracked folders git must hash on every look (a virtualenv or node_modules not in .gitignore), the shell the
plugin's hooks run through, whether the hooks have recorded anything, and the web page's server. It changes nothing
but system-diff's own caches. Paste its output into an issue, or to Claude."""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import List, Optional, Tuple

from .sources import run_hidden


def _git(root: Path, *args: str) -> str:
    try:
        return run_hidden(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


SLOW_START = 0.5   # seconds to start one program: above this, every git call is felt


def start_cost(argv: List[str], times: int) -> Optional[float]:
    """The median time to start `argv` and see it end, or None when it cannot be started."""
    took = []
    for _ in range(times):
        began = time.perf_counter()
        try:
            run_hidden(argv, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        took.append(time.perf_counter() - began)
    took.sort()
    return took[len(took) // 2]


def _secs(t: Optional[float]) -> str:
    return "could not start it" if t is None else f"{t:.2f}s"


def engine_report(root: Path, say, doing) -> None:
    """Whether the repository's engine answers, its version and where it is, and the git calls one look costs
    against it: in the client (none) and in the engine (none once it has read the repository)."""
    try:
        from . import live, sources
        doing("asking the resident engine (GET /progress)")
        info = live.engine_info(root) or {}
        got = live.client_progress(root, timeout=5.0)
        if got is None:
            say("engine: none answers" + (" (one is being started)" if live.starting(root) else "")
                + " · the pane starts one; with none, every look reads the system itself")
            return
        say(f"engine: {info.get('version') or '?'} on port {info.get('port')} · {live.progress_text(got)}"
            + (" · has read everything" if got.get("complete") else " · still reading")
            + (f" · error: {got['error']}" if got.get("error") else ""))
        calls = got.get("engine_git_calls") or []
        if calls:
            say(f"  git calls the engine made itself: {len(calls)} ({', '.join(sorted(set(calls)))})")
        before = int(got.get("git_spawns") or 0)
        mine = sources.SPAWNS["git"]
        began = time.time()
        width = 80 + int(time.time()) % 37   # a width not drawn yet: the engine draws the page for this look
        page = live.client_page(root, "now", width, timeout=60.0)
        took = time.time() - began
        after = (live.client_progress(root, timeout=5.0) or {}).get("git_spawns")
        engine_calls = "?" if after is None else str(max(0, int(after) - before - 0))
        say(f"a look against the engine: {took:.2f}s · git calls: {sources.SPAWNS['git'] - mine} here, "
            f"{engine_calls} in the engine" + ("" if page else " · it gave no page"))
        if after is not None and int(after) - before > 0 and got.get("complete"):
            say("  ! the engine ran git for a look: a comparison it had not made before (a new checkpoint or "
                "commit) runs `git log` once; more than that on every look is a bug worth reporting")
    except Exception as exc:  # noqa: BLE001 - a report, never a failure
        say(f"(could not ask the engine: {exc})")


def main(root: Path) -> int:
    from . import __version__
    terminal = sys.stdout   # the person's, even while a look's page is captured

    def say(line: str) -> None:   # each line as soon as it is known: a slow machine shows where it is
        print(line, file=terminal, flush=True)

    def doing(what: str) -> None:
        print(f"  … {what}", file=terminal, flush=True)
    say(f"system-diff {__version__} · Python {platform.python_version()} · {platform.system()} {platform.release()}")
    say(f"git: {_git(root, '--version').strip() or 'not found'} · sh: {shutil.which('sh') or 'not found'}"
        f" · bash: {shutil.which('bash') or 'not found'}")
    say(f"repository: {root}")

    doing("timing how long starting a program takes here (git --version ×3, python ×2)")
    git_start = start_cost(["git", "--version"], 3)
    py_start = start_cost([sys.executable, "-c", "pass"], 2)
    say(f"starting git: {_secs(git_start)} (median of 3) · starting Python: {_secs(py_start)}")
    slow = max(git_start or 0.0, py_start or 0.0)
    if slow >= SLOW_START:
        say(f"  ! starting a program takes {slow:.1f} s here — usually endpoint security scanning each launch, or a "
            "home folder on a network drive; system-diff keeps git calls to a minimum (its engine makes a handful "
            "when it starts and none per look), but the first read still pays a few")

    doing("counting files (git ls-files)")
    tracked = _git(root, "ls-files", "-z").count("\0")
    untracked = [f for f in _git(root, "ls-files", "-z", "-o", "--exclude-standard").split("\0") if f]
    say(f"files: {tracked:,} tracked, {len(untracked):,} untracked and not ignored")
    if untracked:
        tops = Counter(f.split("/")[0] for f in untracked).most_common(5)
        say("  untracked, by folder: " + ", ".join(f"{d} ({n:,})" for d, n in tops))
        if len(untracked) > 500:
            say("  ! git hashes these on every look: add folders like these to .gitignore if they are not your code")

    from . import compare, extract, panel, progress
    doing("taking a snapshot of the working tree, as a look without the engine does")
    t = time.time()
    compare.worktree_tree(root)
    say(f"snapshot of the working tree: {time.time() - t:.2f}s")

    calls: List[Tuple[str, float]] = []
    run = subprocess.run

    def timed(*a, **k):
        began = time.time()
        try:
            return run(*a, **k)
        finally:
            argv = a[0] if a else k.get("args")
            calls.append((" ".join(argv[:3]) if isinstance(argv, list) else str(argv)[:40], time.time() - began))
    stages: List[Tuple[float, str]] = []
    last = [0.0]
    stage = progress.stage

    def noted(text, done=None, total=None):
        if not stages or stages[-1][1] != text:
            stages.append((time.time(), text))
            print(f"      {text} …", file=terminal, flush=True)
            last[0] = time.time()
        elif total and time.time() - last[0] > 2:   # a long read: how far it is, every couple of seconds
            print(f"        {done or 0:,} of {total:,} files ({time.time() - stages[-1][0]:.0f}s)", file=terminal, flush=True)
            last[0] = time.time()
        stage(text, done, total)

    subprocess.run, progress.stage = timed, noted
    try:
        for n in (1, 2):
            calls.clear()
            stages.clear()
            doing(f"timing pane look {n} (the first in a large repository can take a while; stages print below)")
            began = time.time()
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                panel.main(root, 100, "now", None, 2)
            took = time.time() - began
            reading = (json.loads(buf.getvalue() or "{}").get("reading") or {})
            say(f"pane look {n}: {took:.2f}s"
                + (f" · read {reading.get('done')} of {reading.get('total')} files" if reading else " · complete")
                + f" · {len(calls)} git calls, {sum(c[1] for c in calls):.2f}s in git")
            marks = stages + [(time.time(), "drawing and the rest")]
            first = marks[0][0] if marks else began
            if marks and first - began > 0.05:
                say(f"    {first - began:6.2f}s  before the first stage (starting, config)")
            for (at, text), (nxt, _) in zip(marks, marks[1:]):
                say(f"    {nxt - at:6.2f}s  {text}")
            for argv, secs in sorted(calls, key=lambda c: -c[1])[:3]:
                if secs > 0.2:
                    say(f"    slowest git: {secs:.2f}s  {argv}")
    finally:
        subprocess.run, progress.stage = run, stage
    say(f"cold-start budget per look: {extract.COLD_BUDGET:.0f}s")

    engine_report(root, say, doing)

    try:
        from . import activity, watch
        from .sources import git_dir
        doing("checking the hooks, the web page and the caches")
        events = activity.events(root, 5)
        say(f"hooks: {len(events)} recent events recorded" + ("" if events else
            " · none: the plugin's hooks may not run here (they run through sh)"))
        say(f"web page: {watch.web_url(root) or 'no server running'}")
        cache = git_dir(root)
        files = sum(1 for _ in (cache / "files").rglob("*.json")) if (cache / "files").exists() else 0
        say(f"caches: {cache} · {files} facts shards")
    except Exception as exc:  # noqa: BLE001 - a report, never a failure
        say(f"(could not check hooks/web/caches: {exc})")
    return 0
