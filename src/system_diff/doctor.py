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
from typing import List, Tuple

from .sources import run_hidden


def _git(root: Path, *args: str) -> str:
    try:
        return run_hidden(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


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
    doing("taking a snapshot of the working tree, as every look does")
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
