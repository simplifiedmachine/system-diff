"""The LLM's reorganisation of the system diagram: what it is shown, what of its answer is kept, how the diagram takes
it, and when it is asked. No network: the model is mocked."""
from __future__ import annotations

import json

import pytest

from system_diff import architecture, narrate, regroup
from system_diff.compare import compare

from conftest import FIRST, SECOND


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setattr(narrate, "claude_llm", lambda root, extra=(): None)
    monkeypatch.setattr(narrate, "shell_llm", lambda: None)
    monkeypatch.delenv("SYSTEM_DIFF_REGROUP", raising=False)


@pytest.fixture
def an_llm(monkeypatch, no_llm):
    monkeypatch.setattr(narrate, "claude_llm", lambda root, extra=(): {"base_url": "http://x", "token": "t",
                                                                       "model": "m", "source": "test"})
    spawned = []
    monkeypatch.setattr(regroup, "_spawn", lambda root, job: spawned.append(job))
    return spawned


ANSWER = {"groups": [{"name": "Backend", "about": "the order service", "parts": ["core", "store", "nope"]},
                     {"name": "Alone", "parts": ["web"]}],
          "parts": {"core": {"label": "Order rules", "layer": "logic"},
                    "store": {"label": "x" * 80, "layer": "database"},
                    "api": {"label": "Orders API"},
                    "ghost": {"label": "Ghost"}},
          "fold": ["app/jobs", "api"]}


def _undeclare(c, *ids):
    """The config no longer names these parts' label, kind or group, as in a repository with no system-diff.toml."""
    for comp in c.cfg["components"]:
        if comp["id"] in ids:
            for k in ("label", "kind", "group"):
                comp.pop(k, None)


def test_the_model_is_shown_structure_and_never_the_code(repo):
    c = compare(repo, "HEAD", ".")
    s = regroup.summary(c.after, c.cfg, regroup.sample_paths(repo, c.cfg, list(c.after["components"])))
    text = regroup.prompt(s)
    assert "app/core/rules.py" in text and '"endpoints":3' in text and '["app/jobs","core",3]' in text
    for f, body in {**FIRST, **SECOND}.items():
        for line in body.splitlines():
            if len(line.strip()) > 12:
                assert line.strip() not in text, (f, line)
    assert set(s["layers"]) == {"ui", "interface", "logic", "data"}


def test_the_answer_is_validated_hard(repo):
    c = compare(repo, "HEAD", ".")
    s = regroup.summary(c.after, c.cfg)
    ov = regroup.validate("```json\n" + json.dumps(ANSWER) + "\n```", s)
    assert ov["groups"] == {"Backend": "the order service"}               # a group of one is no group
    assert ov["parts"]["core"] == {"label": "Order rules", "layer": "logic", "group": "Backend"}
    assert ov["parts"]["store"] == {"group": "Backend"}                    # a long label, an unknown layer: dropped
    assert "ghost" not in ov["parts"] and "nope" not in json.dumps(ov)     # unknown parts dropped
    assert ov["parts"]["app/jobs"]["fold"] and "fold" not in ov["parts"]["api"]   # never fold what serves endpoints
    for bad in ("not json", "[1, 2]", '{"groups": "x"}', '{"parts": {"ghost": {"label": "G"}}}', ""):
        assert regroup.validate(bad, s) is None
    half = {"fold": ["app/jobs", "core", "store", "web"]}                  # most of the system is not the system?
    assert regroup.validate(half, s) is None


def test_the_diagram_takes_the_overlay_and_the_config_wins(repo):
    c = compare(repo, "HEAD", ".")
    _undeclare(c, "core", "store")
    ov = regroup.validate(ANSWER, regroup.summary(c.after, c.cfg))
    m = architecture.system(c, overlay=ov)
    nodes = {n["id"]: n for n in m["nodes"]}
    box = nodes["g:Backend"]
    assert [(i["id"], i["title"]) for i in box["items"]] == [("core", "Order rules"), ("store", "Store")]
    assert box["about"] == "the order service" and "core" not in nodes
    assert nodes["api"]["title"] == "API"                                  # declared: the label stays
    assert "app/jobs" not in nodes                                         # folded, and unchanged: not drawn
    assert any(e["to"] == "g:Backend" for e in m["edges"])
    c.regroup = ov                                                         # set on the comparison: the same
    assert architecture.system(c) == m
    inner = architecture.container(c, "core")
    assert inner["title"] == "Backend" and {"core", "store"} <= {n["id"] for n in inner["nodes"]}
    c.regroup = None
    assert "app/jobs" in {n["id"] for n in architecture.system(c)["nodes"]}


def test_without_an_overlay_the_diagram_is_as_before(repo):
    c = compare(repo, "HEAD", ".")
    plain = architecture.system(c)
    assert architecture.system(c, overlay=None) == plain
    c.regroup = {"version": 1, "groups": {}, "parts": {}}
    assert architecture.system(c) == plain
    c.regroup = "garbage"
    assert architecture.system(c) == plain


def test_ensure_asks_once_in_the_background_and_then_draws_with_the_answer(repo, an_llm, monkeypatch):
    c = compare(repo, "HEAD", ".")
    c.cfg["regroup"] = {"min_parts": 0}
    assert regroup.ensure(repo, c.after, c.cfg) is None and len(an_llm) == 1   # started, not waited for
    assert regroup.ensure(repo, c.after, c.cfg) is None and len(an_llm) == 1   # asked already: not again
    job = an_llm[0]
    assert (job.parent / "lock").exists()
    sent = []
    monkeypatch.setattr(narrate, "complete", lambda user, system, root, cfg, timeout=120, limit=1500:
                        sent.append(user) or (json.dumps(ANSWER), "m (test)"))
    assert regroup.run(job) == 0
    assert not (job.parent / "lock").exists() and not job.exists()
    assert "app/api/routes.py" in sent[0] and "def list_orders" not in sent[0]
    ov = regroup.ensure(repo, c.after, c.cfg)
    assert ov["key"] == regroup.structure_key(c.after) and ov["model"] == "m (test)"
    assert ov["parts"]["core"]["group"] == "Backend"
    # the structure changes: the newest overlay still draws, its missing parts ignored; asked again only later
    bp = {**c.after, "components": {k: v for k, v in c.after["components"].items() if k != "app/jobs"}}
    assert regroup.ensure(repo, bp, c.cfg) == ov and len(an_llm) == 1     # within a few minutes of the last ask


def test_a_bad_answer_keeps_no_overlay_and_frees_the_lock(repo, an_llm, monkeypatch):
    c = compare(repo, "HEAD", ".")
    c.cfg["regroup"] = {"min_parts": 0}
    regroup.ensure(repo, c.after, c.cfg)
    monkeypatch.setattr(narrate, "complete", lambda *a, **k: ("I think the backend is nice.", "m"))
    assert regroup.run(an_llm[0]) == 1
    assert regroup.ensure(repo, c.after, c.cfg) is None
    assert not (an_llm[0].parent / "lock").exists()


def test_nothing_is_asked_without_an_llm_when_off_or_for_a_small_repository(repo, no_llm, an_llm, monkeypatch):
    c = compare(repo, "HEAD", ".")
    assert regroup.ensure(repo, c.after, c.cfg) is None and not an_llm      # 5 parts: too few to need it
    c.cfg["regroup"] = {"min_parts": 0}
    monkeypatch.setenv("SYSTEM_DIFF_REGROUP", "0")
    assert regroup.ensure(repo, c.after, c.cfg) is None and not an_llm
    monkeypatch.delenv("SYSTEM_DIFF_REGROUP")
    c.cfg["regroup"]["enabled"] = False
    assert regroup.ensure(repo, c.after, c.cfg) is None and not an_llm
    c.cfg["regroup"]["enabled"] = True
    monkeypatch.setattr(narrate, "claude_llm", lambda root, extra=(): None)
    assert regroup.ensure(repo, c.after, c.cfg) is None and not an_llm      # no gateway: nothing, no error


def test_the_structure_key_ignores_file_counts(repo):
    c = compare(repo, "HEAD", ".")
    bp = json.loads(json.dumps(c.after))
    bp["components"]["core"]["files"] = 40
    assert regroup.structure_key(bp) == regroup.structure_key(c.after)
    bp["components"]["extra"] = dict(bp["components"]["core"])
    assert regroup.structure_key(bp) != regroup.structure_key(c.after)
