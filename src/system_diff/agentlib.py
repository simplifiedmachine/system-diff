"""Agents worth keeping. A plan's agents (plan_agents: one per area) are made for one task, but some roles keep coming
back as a system grows: a tester, a reviewer, an API agent, a migrations agent. Those are saved as Claude Code
subagents (.claude/agents/<name>.md for the project, shared through git; ~/.claude/agents/<name>.md for the person),
so every later session, plan and team uses them by name. A procedure an agent keeps repeating (how this repository
runs its tests, how it releases) is saved as a skill (.claude/skills/<name>/SKILL.md) the same way.

Each role is counted: every plan that names it (planned) and every SubagentStop of an agent of that type (finished),
in .git/system-diff/agent-usage.json; research-diff counts its own in .git/research-diff/agent-usage.json, and both
are read, so an agent saved by either tool is reused by both. A file system-diff writes carries MARKER, and a file
without it (one the person wrote) is never overwritten or removed.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

USAGE = "agent-usage.json"
MARKER = "<!-- saved by system-diff: edit freely; system-diff overwrites or forgets this file only if this line stays -->"
STANDING = ("review", "reviewer", "test", "tester", "verify", "verifier", "runner", "audit", "auditor", "qa", "lint")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SCOPES = ("project", "user")


def _name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:64]


def _usage_file(root: Path) -> Path:
    from .sources import git_dir
    return git_dir(root) / USAGE


def _load(f: Path) -> Dict[str, Any]:
    try:
        got = json.loads(f.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(f: Path, data: Dict[str, Any]) -> None:
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, f)


def usage(root: Path) -> Dict[str, Dict[str, Any]]:
    """Every role counted, by name: {"planned", "finished", "last", "area", "brief", "by"}, research-diff's merged."""
    out: Dict[str, Dict[str, Any]] = {}
    sources = [(_usage_file(root), "system-diff")]
    try:
        from .sources import git_dir
        sources.append((git_dir(root).parent / "research-diff" / USAGE, "research-diff"))
    except Exception:  # noqa: BLE001
        pass
    for f, by in sources:
        roles = _load(f).get("roles") or {}
        for name, r in roles.items() if isinstance(roles, dict) else []:
            if not isinstance(r, dict):
                continue
            e = out.setdefault(name, {"planned": 0, "finished": 0, "last": 0, "area": "", "brief": "", "by": []})
            e["planned"] += int(r.get("planned") or 0)
            e["finished"] += int(r.get("finished") or 0)
            e["last"] = max(e["last"], float(r.get("last") or 0))
            e["area"] = e["area"] or str(r.get("area") or "")
            e["brief"] = e["brief"] or str(r.get("brief") or "")
            e["by"].append(by)
    return out


def _count(root: Path, name: str, field: str, **extra: Any) -> None:
    name = _name(name)
    if not name:
        return
    f = _usage_file(root)
    data = _load(f)
    roles = data.setdefault("roles", {})
    r = roles.setdefault(name, {"planned": 0, "finished": 0})
    r[field] = int(r.get(field) or 0) + 1
    r["last"] = round(time.time(), 3)
    for k, v in extra.items():
        if v:
            r[k] = str(v)[:4000]
    _save(f, data)


def planned(root: Path, agents: List[Dict[str, Any]]) -> None:
    for a in agents:
        _count(root, a.get("name") or "", "planned", area=a.get("area"), brief=a.get("brief"))


def finished(root: Path, agent_type: str) -> None:
    if agent_type and agent_type not in ("general-purpose", "Explore", "Plan", "fork"):
        _count(root, agent_type, "finished")


def _front(text: str) -> Dict[str, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    out: Dict[str, str] = {}
    for line in (m.group(1).splitlines() if m else []):
        k, sep, v = line.partition(":")
        if sep:
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _folders(root: Path) -> List[tuple]:
    return [(Path(root) / ".claude" / "agents", "project"), (Path.home() / ".claude" / "agents", "user")]


def saved(root: Path) -> List[Dict[str, Any]]:
    """The subagents saved (the project's, then the person's), each with how often its role was planned and finished."""
    counts = usage(root)
    out = []
    for folder, scope in _folders(root):
        for f in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _front(text)
            name = fm.get("name") or f.stem
            c = counts.get(name) or {}
            out.append({"name": name, "scope": scope, "path": str(f), "description": fm.get("description", ""),
                        "tools": fm.get("tools", ""), "model": fm.get("model", ""), "ours": MARKER in text,
                        "planned": c.get("planned", 0), "finished": c.get("finished", 0)})
    return out


def worth_keeping(root: Path) -> List[Dict[str, Any]]:
    """Roles planned or finished twice, or a standing role (a reviewer, a tester) finished once, not saved yet."""
    have = {a["name"] for a in saved(root)}
    out = []
    for name, c in usage(root).items():
        if name in have:
            continue
        standing = any(w in name.split("-") for w in STANDING)
        if c["planned"] >= 2 or c["finished"] >= 2 or (standing and c["finished"] >= 1):
            out.append({"name": name, "planned": c["planned"], "finished": c["finished"], "area": c["area"],
                        "why": ("a standing role" if standing and c["planned"] < 2 and c["finished"] < 2
                                else f"planned {c['planned']}× and finished {c['finished']}×"),
                        "brief": c["brief"][:600], "by": c["by"]})
    out.sort(key=lambda r: -(r["planned"] + r["finished"]))
    return out


def _target(root: Path, kind: str, name: str, scope: str) -> Path:
    if scope not in SCOPES:
        raise ValueError("scope is project (shared through git) or user (yours, every repository)")
    base = Path(root) if scope == "project" else Path.home()
    if kind == "agent":
        return base / ".claude" / "agents" / f"{name}.md"
    return base / ".claude" / "skills" / name / "SKILL.md"


def _write(f: Path, text: str) -> None:
    if f.exists():
        try:
            if MARKER not in f.read_text(encoding="utf-8"):
                raise ValueError(f"{f} was written by someone else: system-diff leaves it as it is")
        except OSError as exc:
            raise ValueError(f"cannot read {f}: {exc}") from exc
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, f)


def _yaml(v: str) -> str:
    return json.dumps(str(v))   # a JSON string is a YAML string


def save_agent(root: Path, name: str, description: str, prompt: str, tools: str = "", model: str = "",
               scope: str = "project") -> Dict[str, Any]:
    name = _name(name)
    if not NAME.match(name or ""):
        raise ValueError("name: lowercase letters, digits and dashes")
    if not str(description).strip() or not str(prompt).strip():
        raise ValueError("an agent needs a description (when to use it) and its standing instructions")
    f = _target(root, "agent", name, scope)
    head = ["---", f"name: {name}", f"description: {_yaml(description.strip()[:1000])}"]
    if tools.strip():
        head.append(f"tools: {tools.strip()[:400]}")
    if model.strip():
        head.append(f"model: {model.strip()[:60]}")
    _write(f, "\n".join(head + ["---", MARKER, "", prompt.strip(), ""]))
    return {"saved": name, "scope": scope, "path": str(f),
            "use": f"start it as the Task tool's subagent type {name!r}"}


def save_skill(root: Path, name: str, description: str, body: str, scope: str = "project") -> Dict[str, Any]:
    name = _name(name)
    if not NAME.match(name or ""):
        raise ValueError("name: lowercase letters, digits and dashes")
    if not str(description).strip() or not str(body).strip():
        raise ValueError("a skill needs a description (when to use it) and its steps")
    f = _target(root, "skill", name, scope)
    _write(f, "\n".join(["---", f"name: {name}", f"description: {_yaml(description.strip()[:1000])}", "---", MARKER,
                         "", body.strip(), ""]))
    return {"saved": name, "scope": scope, "path": str(f)}


def forget(root: Path, name: str, scope: str = "project") -> Dict[str, Any]:
    f = _target(root, "agent", _name(name), scope)
    if not f.exists():
        raise ValueError(f"no saved agent {name!r} in {scope}")
    if MARKER not in f.read_text(encoding="utf-8"):
        raise ValueError(f"{f} was written by someone else: system-diff leaves it as it is")
    f.unlink()
    return {"forgot": _name(name), "scope": scope}


def match(root: Path, name: str, area: str = "") -> Optional[Dict[str, Any]]:
    """A saved agent for a plan's role: the same name, or one whose name is the area's."""
    want = {_name(name), _name(area)} - {""}
    return next((a for a in saved(root) if a["name"] in want), None)


def library(root: Path) -> Dict[str, Any]:
    return {"saved": saved(root), "worth_keeping": worth_keeping(root),
            "how": "save_agent writes a subagent Claude Code starts by name (Task tool, subagent_type); save_skill a "
                   "procedure. Save a role when it recurs or the person asks, and say what you saved."}
