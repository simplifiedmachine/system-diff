"""Each branch keeps its own checkpoint (what the person has seen there), as branches can hold different designs."""
from __future__ import annotations

import subprocess

from system_diff import compare


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, path, text, msg):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


def test_each_branch_keeps_its_own_checkpoint(repo):
    main = git(repo, "symbolic-ref", "--short", "HEAD")
    seen_main = compare.checkpoint(repo)
    assert compare.checkpoint_point(repo) == (seen_main, "checkpoint")
    assert compare.checkpoint_ref(repo).endswith("/checkpoints/" + main)

    git(repo, "checkout", "-qb", "feature")          # a new branch: what was seen on main still holds here
    sha, label = compare.checkpoint_point(repo)
    assert sha == git(repo, "rev-parse", f"{seen_main}^") and label == f"what you saw on {main}"

    commit(repo, "app/extra.py", "x = 1\n", "feature work")
    seen_feature = compare.checkpoint(repo)
    assert compare.checkpoint_point(repo) == (seen_feature, "checkpoint")

    git(repo, "checkout", "-q", main)                # back on main: its own checkpoint, not the feature's
    assert compare.checkpoint_point(repo) == (seen_main, "checkpoint")


def test_a_branch_that_parted_starts_where_it_left(repo):
    main = git(repo, "symbolic-ref", "--short", "HEAD")
    fork = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "other")
    commit(repo, "app/other.py", "y = 2\n", "other work")
    compare.checkpoint(repo)                         # seen on `other` only
    git(repo, "checkout", "-q", main)
    git(repo, "checkout", "-qb", "fresh")            # from main, which never had other's commit
    commit(repo, "app/fresh.py", "z = 3\n", "fresh work")
    sha, label = compare.checkpoint_point(repo)
    assert sha == fork and label == "where this branch left other"


def test_no_checkpoint_anywhere_means_head(repo):
    assert compare.checkpoint_point(repo) == (None, "")
    assert compare.resolve(repo, "checkpoint")[1] == "HEAD (no checkpoint yet)"
