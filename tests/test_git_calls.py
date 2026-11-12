"""Git calls are the cost on a slow machine (starting any program takes seconds where endpoint security scans each
launch): the engine makes a handful when it starts and none per look, per tick or per edit; what Python can read of
git's own files it reads itself (gitfs); and nothing reads the system twice. Every git process started here is
counted, and made to cost a little, as it would there."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest

from system_diff import compare, gitfs, live, panel, sources, watch

from conftest import git_, write

COST = 0.3   # seconds each git process costs here


@pytest.fixture
def spawns(monkeypatch):
    """Every git process started (subprocess.run goes through Popen too, and so does the cat-file process): counted,
    each made to cost COST seconds."""
    seen = []
    real = subprocess.Popen

    class Counted(real):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **k):
            if isinstance(args, (list, tuple)) and args and args[0] == "git":
                seen.append(" ".join(str(x) for x in args[1:4]))
                time.sleep(COST)
            super().__init__(args, *a, **k)
    monkeypatch.setattr(subprocess, "Popen", Counted)
    return seen


@pytest.fixture
def packed(repo):
    """The made-up repository, its objects packed (as a real one's mostly are): read through one cat-file."""
    git_(repo, "gc", "-q")
    return repo


def test_gitfs_reads_what_git_says(packed):
    repo = packed
    git_(repo, "update-ref", "refs/worktree/system-diff/checkpoints/main", "HEAD~1")
    assert gitfs.head(repo) == git_(repo, "rev-parse", "HEAD").strip()
    assert gitfs.branch(repo) == "main"
    for expr in ("HEAD~1", "HEAD^", "main", "HEAD^{commit}", "refs/worktree/system-diff/checkpoints/main"):
        assert gitfs.rev(repo, expr) == git_(repo, "rev-parse", expr).strip(), expr
    assert gitfs.rev(repo, "HEAD~5") is None
    listed = [f"{m} {k} {s}\t{p}" for m, k, s, p in gitfs.ls_tree(repo, gitfs.head(repo))]
    assert listed == git_(repo, "ls-tree", "-r", "HEAD").splitlines()
    for version in ("2", "3", "4"):
        git_(repo, "update-index", "--index-version", version)
        index, _ = gitfs.read_index(gitfs.git_dir(repo) / "index")
        assert sorted(f"{e.mode} {e.sha} 0\t{p}" for p, e in index.items()) == \
            sorted(git_(repo, "ls-files", "-s").splitlines())
    info = gitfs.commit(repo, gitfs.head(repo))
    assert info["parents"] == [git_(repo, "rev-parse", "HEAD~1").strip()] and info["message"].startswith("second")


def test_an_engine_starts_with_a_handful_of_git_calls_and_then_makes_none(packed, spawns):
    repo = packed
    e = live.Engine(repo, interval=3600).start()   # ticks only when the test says so
    try:
        assert e.done.wait(60)
        started = list(spawns)
        # git config -l, git ls-files -c -o --exclude-standard --eol, and the one cat-file process: that is all
        assert len(started) <= 4, started
        assert sorted(c.split()[0] if not c.startswith("--") else c.split()[1] for c in started) == \
            ["cat-file", "config", "ls-files"], started
        spawns.clear()
        assert compare.compare(repo, "checkpoint", ".").diff is not None   # a look: refs, numstat, the base's files
        compare.compare(repo, "checkpoint", ".")
        first_look = list(spawns)
        spawns.clear()
        assert not e.tick() and spawns == []                             # a tick with nothing new: no git
        compare.compare(repo, "checkpoint", ".")
        assert spawns == [], spawns                                      # nor a look again
        spawns.clear()
        assert first_look == [], first_look                              # HEAD is the base: no commit log to ask
        # an edit, found by the stat scan (no hook, no /touch): no git
        write(repo, {"app/core/rules.py": "import os\nX = os.getenv('PAYMENT_KEY')\n"})
        os.utime(repo / "app/core/rules.py", (time.time() - 10, time.time() - 10))
        assert e.tick() and spawns == [], spawns
        c = compare.compare(repo, "checkpoint", ".")
        assert "PAYMENT_KEY" in c.diff["plan"] and spawns == [], spawns
        assert any("lines" in " ".join(x["detail"]) for x in c.diff["changes"] if x["id"] == "core")
        # a new file: git says once whether it would add it
        write(repo, {"app/jobs/new.py": "import os\nos.getenv('QUEUE_URL')\n"})
        os.utime(repo / "app/jobs", None)
        assert e.tick() and "app/jobs/new.py" in e.entries
        assert len(spawns) == 1 and "ls-files" in spawns[0], spawns
        spawns.clear()
        # an ignored one stays out, and is asked about once
        (repo / ".gitignore").write_text("*.log\n")
        e.tick()
        spawns.clear()
        write(repo, {"app/debug.log": "x"})
        os.utime(repo / "app", (time.time() + 1, time.time() + 1))
        e.tick()
        assert "app/debug.log" not in e.entries and len(spawns) <= 1
        spawns.clear()
        os.utime(repo / "app", (time.time() + 2, time.time() + 2))
        e.tick()
        assert spawns == []                                              # known ignored: not asked again
        # a commit of the working tree, when one must be kept (a checkpoint): written in Python, the same tree
        sha = e.materialize()
        assert spawns == []
        assert git_(repo, "rev-parse", f"{sha}^{{tree}}").strip() == compare.worktree_tree(repo)
        spawns.clear()
        marked = e.mark("refs/worktree/system-diff/turn")
        assert [c.split()[0] for c in spawns] == ["update-ref"], spawns   # one call: the ref
        assert marked == git_(repo, "rev-parse", "refs/worktree/system-diff/turn").strip()
    finally:
        e.stop()


def test_the_engine_finds_editor_edits_deletions_and_commits_by_itself(packed, spawns):
    repo = packed
    e = live.Engine(repo, interval=3600).start()
    try:
        assert e.done.wait(60)
        spawns.clear()
        (repo / "app/db/repo.py").unlink()
        assert e.tick() and "app/db/repo.py" not in e.entries and spawns == []
        git_(repo, "add", "-A")
        git_(repo, "commit", "-q", "-m", "third")
        e.tick()
        assert e.head == git_(repo, "rev-parse", "HEAD").strip()
        assert compare.compare(repo, "HEAD", ".").diff["changes"] == []
        tracked = {line.split("\t")[1]: line.split()[1] for line in git_(repo, "ls-files", "-s").splitlines()}
        assert e.blobs() == tracked
    finally:
        e.stop()


CRLF_FILES = {
    "lf.py": b"a = 1\nb = 2\n",
    "crlf.py": b"a = 1\r\nb = 2\r\n",
    "mixed.py": b"a = 1\r\nb = 2\n",
    "lonecr.txt": b"a\rb\r\n",
    "bin.dat": b"\x00\x01\r\n\x02",
    "forced.txt": b"x\r\ny\r\n",
    "kept.txt": b"x\r\ny\r\n",
    "eol.md": b"q\r\n",
}


@pytest.mark.parametrize("autocrlf", ["true", "input", "false"])
def test_blob_ids_match_git_add_with_line_ending_conversion(tmp_path, autocrlf):
    """On Windows core.autocrlf is usually true: the engine hashes in Python as git add would store the file."""
    r = tmp_path / "eol"
    r.mkdir()
    git_(r, "init", "-q", "-b", "main")
    git_(r, "config", "core.autocrlf", autocrlf)
    (r / ".gitattributes").write_bytes(b"forced.txt text\nkept.txt -text\neol.md eol=lf\n")
    (r / "committed.txt").write_bytes(b"has\r\ncr\r\n")
    git_(r, "-c", "core.autocrlf=false", "add", "committed.txt", ".gitattributes")
    git_(r, "commit", "-q", "-m", "a file committed with CRs")
    (r / "committed.txt").write_bytes(b"has\r\ncr\r\nmore\r\n")   # "safer autocrlf": git keeps its CRs
    for name, data in CRLF_FILES.items():
        (r / name).write_bytes(data)
    e = live.Engine(r, interval=3600).start()
    try:
        assert e.done.wait(30)
        theirs = compare.worktree_tree(r)
        assert git_(r, "rev-parse", f"{e.materialize()}^{{tree}}").strip() == theirs
    finally:
        e.stop()


def test_a_page_never_waits_for_the_first_read_and_progress_answers_at_once(repo, monkeypatch):
    gate = threading.Event()
    real = live.Engine._relist

    def slow(self):   # the start's listing takes long (a slow machine): pages answer meanwhile
        gate.wait(20)
        return real(self)
    monkeypatch.setattr(live.Engine, "_relist", slow)
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)}, daemon=True)
    t.start()
    try:
        for _ in range(300):
            if "w" in up:
                break
            time.sleep(0.02)
        began = time.monotonic()
        got = live.client_page(repo, "now", 90, timeout=5)
        assert got is not None and time.monotonic() - began < 2
        assert "Reading the system" in json.dumps(got["page"]) and got["page"]["reading"]["stage"]
        prog = live.client_progress(repo)
        assert prog is not None and prog["ready"] is False and prog["stage"]
        # the pane's client shows that, and never reads the system itself beside the engine
        monkeypatch.setattr(live, "client_page", lambda *a, **k: None)   # as if the page timed out
        monkeypatch.setattr(panel, "build", lambda *a, **k: pytest.fail("read in-process while an engine answers"))
        page = panel.waiting(repo, 90, "now", None)
        assert page is not None and "version" not in page and "Reading the system" in json.dumps(page)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert panel.main(repo, 90, "now", None, 2, start=True) == 0
        assert "Reading the system" in out.getvalue()
        from system_diff import progress
        assert progress.line(repo)                                       # the pane's progress line, from the engine
        gate.set()
        assert up["w"].engine.done.wait(30)
    finally:
        gate.set()
        if "server" in up:
            up["server"].shutdown()
        t.join(5)


def test_a_client_marks_the_turn_through_the_engine(repo, spawns, monkeypatch):
    up = {}
    t = threading.Thread(target=watch.serve, kwargs={"root": repo, "port": 0, "interval": 0.2,
                                                     "ready": lambda url, server, w: up.update(url=url, server=server,
                                                                                               w=w)}, daemon=True)
    t.start()
    try:
        for _ in range(300):
            if "w" in up:
                break
            time.sleep(0.02)
        assert up["w"].engine.done.wait(60)
        write(repo, {"app/core/rules.py": "import os\nX = os.getenv('PAYMENT_KEY')\n"})
        # as a hook process would: the engine is in this test's process, so stand outside it
        key = compare.live_key(repo)
        made = compare.COMMIT.pop(key)
        try:
            spawns.clear()
            sha = compare.mark_turn(repo)
        finally:
            compare.COMMIT[key] = made
        assert [c.split()[0] for c in spawns] == ["update-ref"], spawns   # the engine's one call; none here
        assert git_(repo, "rev-parse", "refs/worktree/system-diff/turn").strip() == sha
        assert git_(repo, "rev-parse", f"{sha}^{{tree}}").strip() == compare.worktree_tree(repo)
    finally:
        if "server" in up:
            up["server"].shutdown()
        t.join(5)


def test_no_git_for_the_git_dir_and_progress(repo, spawns):
    spawns.clear()
    assert sources.git_dir(repo).name == "system-diff"
    from system_diff import progress
    assert progress.line(repo) == ""
    assert live.engine_info(repo) is None
    assert spawns == []
