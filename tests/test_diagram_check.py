"""When the person says the diagram is wrong, Claude checks it against the code (ui_check) and either fixes the config
(previewed with ui_config_preview) or keeps a report of what system-diff misreads (report_diagram_issue)."""
from __future__ import annotations

import json

from conftest import CONFIG, git_

from system_diff import diagcheck, uitools
from system_diff.sources import git_dir

NO_ALIAS = CONFIG.replace('aliases = { "@/" = "web/src/" }\n', "").replace(
    '[[components]]\nid = "web"\nlabel = "Web app"\ncontainer = "web"\npaths = ["web/src/**"]\n\n', "")


def _without_alias(repo, files):
    files(repo, {"system-diff.toml": NO_ALIAS})
    git_(repo, "add", "-A")
    git_(repo, "commit", "-q", "-m", "no alias")


def test_an_import_through_an_unknown_alias_is_found_and_its_fix_adds_the_connection(repo, files):
    _without_alias(repo, files)
    got = uitools.check(repo)
    hit = next(f for f in got["findings"] if f["kind"] == "unresolved-import" and "@/" in f["what"])
    assert hit["where"][0]["path"] == "web/src/App.tsx" and hit["where"][0]["lines"] == [1]
    assert 'aliases = { "@/" = "web/src/" }' in hit["fix"]
    before = uitools.config_preview(repo, "", "now")   # an empty change: the diagram as it is
    edges = lambda d: {(e["from"], e["to"]) for e in d["would_draw"]["edges"]}  # noqa: E731
    after = uitools.config_preview(repo, hit["fix"], "now")
    assert ("web/src", "web/src/lib") in edges(after) - edges(before)


def test_code_in_no_part_and_stale_config_are_found(repo, files):
    files(repo, {"system-diff.toml": CONFIG.replace('roots = ["app/", "web/src/"]', 'roots = ["app/", "web/src/", "gone/"]'),
                 "tools/sync.py": "import os\n"})
    git_(repo, "add", "-A")
    git_(repo, "commit", "-q", "-m", "a tool")
    got = diagcheck.check(repo)
    unowned = next(f for f in got["findings"] if f["kind"] == "unowned-code" and "tools" in f["what"])
    assert unowned["where"][0]["path"] == "tools/sync.py" and '"tools/"' in unowned["fix"]
    assert any(f["kind"] == "stale-config" and "gone/" in f["what"] for f in got["findings"])
    assert got["checked"]["code_files"] > 0
    # one part's check keeps what is about it
    only = diagcheck.check(repo, part="api")
    assert not any(f["kind"] == "unowned-code" for f in only["findings"])


def test_a_diagram_issue_is_kept_and_reported_without_secrets(repo, files):
    files(repo, {"app/core/keys.py": 'API_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\nfrom app.db import repo\n'})
    got = uitools.report_issue(repo, "core", "core is not connected to store", "core -> store",
                               "app/core/keys.py:1-2 and .env:1")
    kept = (git_dir(repo) / diagcheck.ISSUES).read_text(encoding="utf-8").strip().splitlines()
    rec = json.loads(kept[-1])
    assert rec["part"] == "core" and rec["version"] and rec["excerpts"][0]["path"] == "app/core/keys.py"
    assert "ghp_abcdefghijklmnop" not in got["report"] and "ghp_abcdefghijklmnop" not in kept[-1]
    assert "from app.db import repo" in got["report"] and "core -> store" in got["report"]
    assert not any(x["path"] == ".env" for x in rec["excerpts"])   # never a file that is not code


def test_the_check_tools_are_listed():
    import asyncio
    import pytest
    pytest.importorskip("mcp")
    from system_diff import mcp_server
    tools = {t.name: t for t in asyncio.run(mcp_server.build().list_tools())}
    ro = lambda n: (tools[n].model_dump(by_alias=True, exclude_none=True).get("annotations") or {}).get("readOnlyHint")  # noqa: E731
    assert ro("ui_check") and not ro("report_diagram_issue")
    assert "ui_check" in mcp_server.INSTRUCTIONS and "report_diagram_issue" in mcp_server.INSTRUCTIONS
