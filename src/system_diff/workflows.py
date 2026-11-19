"""Workflows worth keeping: a team of agents (who does what, on which blocks, who waits for whom) and the steps
extensions add, saved to use again for later tasks. A saved workflow is a JSON file: for this project in its
.claude/system-diff/workflows/ (shared through git); for the person, in ~/.claude/system-diff/workflows/, either for
every project ("everywhere") or for the projects of a kind ("research", "software": agentlib.kind_of), like the agent
library. Extensions save theirs the same way (`save(..., by="research")`) and offer ready-made ones
(`WORKFLOW_TEMPLATES`, where their `applies(root)` is true), listed beside the saved ones with who made them.

A file system-diff did not write (no "saved_by") is never overwritten or removed.
"""
from __future__ import annotations

import json
import os
import threading
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCOPES = ("project", "user")   # where the files are; "user" ones carry their kinds ("everywhere", "research", ...)
SAVED_BY = "system-diff"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def folder(root: Path, scope: str) -> Path:
    if scope not in SCOPES:
        raise ValueError("scope is project (this repository, shared through git) or user (yours, every repository)")
    base = Path(root) if scope == "project" else Path.home()
    return base / ".claude" / "system-diff" / "workflows"


def _name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:64]


def _agent_spec(g: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": str(g.get("name") or ""), "area": str(g.get("area") or ""),
            "blocks": [str(b) for b in g.get("blocks") or []], "waits_for": [str(w) for w in g.get("waits_for") or []],
            **({"notes": str(g["notes"])[:4000]} if g.get("notes") else {})}


def _step_spec(s: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": str(s.get("id") or ""), "title": str(s.get("title") or s.get("id") or ""),
            "waits_for": [str(w) for w in s.get("waits_for") or []], "by": str(s.get("by") or "")}


def save(root: Path, name: str, description: str = "", scope: str = "project", by: str = SAVED_BY,
         spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Save a workflow: `spec` ({"agents": [...], "steps": [...], "order"}), or the team's current plan. scope:
    "project", "everywhere", or a kind of project ("research", "software")."""
    scope = str(scope or "project").strip().lower()
    if scope == "user":
        scope = "everywhere"
    kinds: List[str] = []
    if scope != "project":
        if not re.match(r"^[a-z][a-z0-9-]{0,30}$", scope):
            raise ValueError("scope: project, everywhere, or a kind of project such as research or software")
        kinds = [scope]
        scope = "user"
    name = _name(name)
    if not NAME.match(name or ""):
        raise ValueError("name: lowercase letters, digits and dashes")
    if spec is None:
        from . import agents
        pl = agents.state(root).get("plan") or {}
        if not pl.get("agents"):
            raise ValueError("there is no team plan to save: plan one first (plan_agents)")
        spec = {"agents": pl["agents"], "steps": pl.get("steps") or [], "order": pl.get("order") or "parallel",
                "task": pl.get("task") or ""}
    out = {"name": name, "description": str(description or "")[:2000], "by": str(by or SAVED_BY),
           **({"kinds": kinds} if kinds else {}),
           "saved_by": SAVED_BY, "saved": round(time.time(), 3), "order": str(spec.get("order") or "parallel"),
           "example_task": str(spec.get("task") or "")[:500],
           "agents": [_agent_spec(g) for g in spec.get("agents") or [] if isinstance(g, dict) and g.get("name")],
           "steps": [_step_spec(s) for s in spec.get("steps") or [] if isinstance(s, dict) and s.get("id")]}
    if not out["agents"] and not out["steps"]:
        raise ValueError("a workflow needs at least one agent or step")
    f = folder(root, scope) / f"{name}.json"
    if f.exists():
        try:
            old = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        if not isinstance(old, dict) or old.get("saved_by") != SAVED_BY:
            raise ValueError(f"{f} was written by someone else: system-diff leaves it as it is")
        if kinds and "everywhere" not in kinds:   # saved again for another kind: for both
            out["kinds"] = sorted(set(kinds) | {k for k in old.get("kinds") or [] if k != "everywhere"})
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, f)
    return {"saved": name, "scope": "project" if scope == "project" else ", ".join(out.get("kinds") or []),
            "path": str(f), "agents": [g["name"] for g in out["agents"]],
            "steps": [s["id"] for s in out["steps"]],
            "use": f"run_workflow(name={name!r}, task=...) plans the same team for a new task"}


def _from_extensions(root: Path) -> List[Dict[str, Any]]:
    out = []
    try:
        from . import extensions
        exts = extensions.load()
    except Exception:  # noqa: BLE001
        return out
    for ext in exts:
        by = str(getattr(ext, "NAME", "") or "extension")
        items = getattr(ext, "WORKFLOW_TEMPLATES", None)
        applies = getattr(ext, "applies", None)
        try:
            if callable(items):
                items = items(extensions.context(root, "", ""))
            if applies is not None and not applies(Path(root)):
                continue   # offered only where the extension has something to do
        except Exception as exc:  # noqa: BLE001 - one extension's failure hides only its own
            extensions.failed(by + " workflow templates", exc)
            continue
        for w in items or []:
            if isinstance(w, dict) and w.get("name"):
                out.append({**_with_waits(w), "name": _name(w["name"]), "by": by, "scope": "extension", "path": ""})
    return out


def _with_waits(w: Dict[str, Any]) -> Dict[str, Any]:
    """A workflow's waits ({"from", "to"}: "to" waits for "from") folded into its agents' and steps' waits_for."""
    w = dict(w)
    agents_ = [dict(g) for g in w.get("agents") or [] if isinstance(g, dict)]
    steps = [dict(x) for x in w.get("steps") or [] if isinstance(x, dict)]
    by_name = {str(g.get("name")): g for g in agents_}
    by_name.update({str(x.get("id")): x for x in steps})
    for wait in w.get("waits") or []:
        if isinstance(wait, dict) and str(wait.get("to")) in by_name and wait.get("from"):
            got = by_name[str(wait["to"])]
            got["waits_for"] = list(dict.fromkeys(list(got.get("waits_for") or []) + [str(wait["from"])]))
    w["agents"], w["steps"] = agents_, steps
    return w


def listing(root: Path) -> List[Dict[str, Any]]:
    """Every workflow there is for this project: the project's, the person's for every project or for this
    project's kind, then those extensions offer here."""
    out: List[Dict[str, Any]] = []
    try:
        from .agentlib import kind_of
        mine = kind_of(Path(root))
    except Exception:  # noqa: BLE001
        mine = {"software"}
    for scope in SCOPES:
        d = folder(root, scope)
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                w = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(w, dict) or not w.get("name"):
                continue
            kinds = set(w.get("kinds") or ["everywhere"])
            if scope == "user" and "everywhere" not in kinds and not kinds & mine:
                continue   # a research workflow, in a software project
            out.append({**_with_waits(w), "scope": scope if scope == "project" else ", ".join(sorted(kinds)),
                        "path": str(f)})
    seen = {w["name"] for w in out}
    out += [w for w in _from_extensions(root) if w["name"] not in seen]
    return out


def summary(w: Dict[str, Any]) -> Dict[str, Any]:
    from .agents import stages_of
    names = [str(g.get("name")) for g in w.get("agents") or [] if isinstance(g, dict)]
    steps = [str(s.get("id")) for s in w.get("steps") or [] if isinstance(s, dict)]
    waits = {str(g.get("name")): set(g.get("waits_for") or []) for g in w.get("agents") or [] if isinstance(g, dict)}
    waits.update({str(s.get("id")): set(s.get("waits_for") or []) for s in w.get("steps") or [] if isinstance(s, dict)})
    st = stages_of({k: {x for x in v if x in waits} for k, v in waits.items()}) if waits else {}
    return {"name": w.get("name"), "description": w.get("description", ""), "by": w.get("by") or SAVED_BY,
            "scope": w.get("scope", ""), "agents": names, "steps": steps, "stages": max(st.values() or [1]),
            "example_task": w.get("example_task", "")}


def find(root: Path, name: str) -> Dict[str, Any]:
    want = _name(name)
    hit = next((w for w in listing(root) if w["name"] == want), None)
    if hit is None:
        raise ValueError(f"no workflow {name!r} here: workflows lists them")
    return hit


def forget(root: Path, name: str, scope: str = "project") -> Dict[str, Any]:
    f = folder(root, "project" if scope == "project" else "user") / f"{_name(name)}.json"
    if not f.exists():
        raise ValueError(f"no saved workflow {name!r} in {scope}")
    try:
        w = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        w = {}
    if not isinstance(w, dict) or w.get("saved_by") != SAVED_BY:
        raise ValueError(f"{f} was written by someone else: system-diff leaves it as it is")
    f.unlink()
    return {"forgot": _name(name), "scope": scope}


def run(root: Path, c: Any, name: str, task: str, most: int = 8) -> Dict[str, Any]:
    """Plan a saved workflow's team for a new task: the same agents, areas and order, on the blocks of theirs the
    system still has; its steps come with the plan."""
    from . import agents
    w = find(root, name)
    preset = [_agent_spec(g) for g in w.get("agents") or [] if isinstance(g, dict) and g.get("name")]
    if not preset and not w.get("steps"):
        raise ValueError(f"the workflow {name!r} has no agents or steps")
    got = agents.plan(root, c, task, max(most, len(preset) or 1), preset=preset or None,
                      order=str(w.get("order") or "parallel")) if preset else {"task": task, "agents": [], "how": ""}
    steps = [_step_spec(s) for s in w.get("steps") or [] if isinstance(s, dict) and s.get("id")]
    if steps:
        agents.set_steps(root, steps)
    got["workflow"] = w["name"]
    got["steps"] = steps
    return got
