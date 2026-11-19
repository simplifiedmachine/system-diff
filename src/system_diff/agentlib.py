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
            e = out.setdefault(name, {"planned": 0, "finished": 0, "last": 0, "area": "", "brief": "", "by": [],
                                      "title": ""})
            e["title"] = e["title"] or str(r.get("title") or "")
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
        _note_global(root, a.get("name") or "", "planned", prompt=a.get("brief") or "")


def finished(root: Path, agent_type: str) -> None:
    if agent_type and agent_type not in ("general-purpose", "Explore", "Plan", "fork"):
        _count(root, agent_type, "finished")
        _note_global(root, agent_type, "finished")


GENERIC = ("general-purpose", "Explore", "Plan", "fork", "claude", "statusline-setup", "claude-code-guide")
_SMALL = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "from", "into", "by", "at", "this",
          "that", "these", "those", "all", "each", "any", "its", "it", "my", "our", "some", "new", "run"}


def role_of(description: str, agent_type: str = "") -> str:
    """The role a subagent plays, the same each time it comes back: its own type when it has one (a saved agent),
    else its Task description's first two words that are not file names, numbers or small words ("Survey the files
    in api" and "Survey files" are both survey-file)."""
    if agent_type and agent_type not in GENERIC:
        return _name(agent_type)
    text = re.sub(r"\S*[./\\:]\S*|`[^`]*`", " ", str(description or "").lower())   # paths, files, code
    words = [w for w in re.findall(r"[a-z][a-z-]*", text) if w not in _SMALL and len(w) > 1]
    words = [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]
    return _name("-".join(words[:2]))


def _print_of(prompt: str) -> str:
    import hashlib
    core = re.sub(r"[^a-z]+", " ", str(prompt or "").lower()).strip()[:300]
    return hashlib.sha1(core.encode("utf-8")).hexdigest()[:10] if len(core) >= 40 else ""


def ran(root: Path, description: str, agent_type: str = "", prompt: str = "") -> str:
    """A subagent finished: its role counted here and across the person's projects (generic subagents too, by what
    they did, so a role Claude keeps launching on its own comes up as worth keeping). The same prompt under another
    description counts as the same role. Returns the role."""
    name = role_of(description, agent_type)
    if not name:
        return ""
    f = _usage_file(root)
    data = _load(f)
    prints = data.setdefault("prints", {})
    fp = _print_of(prompt)
    if fp and prints.get(fp) and prints[fp] != name:
        name = prints[fp]   # the same instructions under another description: one role
    if fp:
        prints[fp] = name
        while len(prints) > 500:
            prints.pop(next(iter(prints)))
    roles = data.setdefault("roles", {})
    r = roles.setdefault(name, {"planned": 0, "finished": 0})
    r["finished"] = int(r.get("finished") or 0) + 1
    r["last"] = round(time.time(), 3)
    title = str(description or "").strip() or name.replace("-", " ")
    title = title[:1].upper() + title[1:80]
    if r.get("title") and r["title"] != title:   # seen under several descriptions: the role's own words
        title = name.replace("-", " ").capitalize()
    r["title"] = title
    if prompt:
        r["brief"] = str(prompt)[:4000]
    if agent_type:
        r["type"] = agent_type
    _save(f, data)
    _note_global(root, name, "finished", prompt=prompt, title=r["title"])
    return name


def _note_global(root: Path, role: str, field: str, prompt: str = "", title: str = "") -> None:
    """The across-projects count only (the repository's is counted by the caller)."""
    f = _global_file()
    if f is None or not _name(role):
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        data = _load(f)
        r = data.setdefault("roles", {}).setdefault(_name(role), {"kinds": {}})
        if prompt:
            r["prompt"] = str(prompt)[:4000]
        if title and not r.get("description"):
            r["description"] = f"Use to {title[:1].lower() + title[1:200]}."
            r["title"] = title[:120]
        r["last"] = round(time.time(), 3)
        for kind in sorted(kind_of(root)):
            c = r["kinds"].setdefault(kind, {"planned": 0, "finished": 0, "projects": []})
            c[field] = int(c.get(field) or 0) + 1
            if str(Path(root).resolve()) not in c["projects"]:
                c["projects"] = (c["projects"] + [str(Path(root).resolve())])[-50:]
        _save(f, data)
    except (OSError, ValueError):
        pass


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
            title = c.get("title") or ""
            why = ("a standing role" if standing and c["planned"] < 2 and c["finished"] < 2
                   else f"{title} ran {c['finished']} times: keep it as an agent?" if title and not c["planned"]
                   else f"planned {c['planned']}× and finished {c['finished']}×")
            out.append({"name": name, "planned": c["planned"], "finished": c["finished"], "area": c["area"],
                        "why": why, "title": title, "brief": c["brief"][:4000], "by": c["by"],
                        **({"description": f"Use to {title[:1].lower() + title[1:200]}."} if title else {})})
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
    return {"saved": saved(root), "worth_keeping": suggest(root), "kinds": sorted(kind_of(root)),
            "for_project": [{k: a[k] for k in ("name", "description", "source", "scope")} for a in for_project(root)],
            "in_library": [{k: a[k] for k in ("name", "description", "kinds", "ours")} for a in _library()],
            "how": "save_agent writes a subagent Claude Code starts by name (Task tool, subagent_type); save_skill a "
                   "procedure. Save a role when it recurs or the person asks, and say what you saved."}


# ── the person's library, by kind of project (shared with research-diff) ────────────────────────────────────────
#
# Claude Code has no scope for "research projects" or "software projects": an agent is a repository's
# (.claude/agents/) or the person's everywhere (~/.claude/agents/). So the library lives beside them, where Claude Code
# does not look (~/.claude/agent-library/<name>.md, the same file format with a `kinds:` line), and each project gets
# the agents of its kinds: a Studio session through the Agent SDK (no file written), a terminal session as files in
# the repository once the person says so (install). Roles are counted across all the person's projects, by kind, so a
# role used once in each of several research repositories is still found worth keeping.
#
# The API research-diff calls: kind_of, note_use, suggest, save, for_project.

LIBRARY_SCOPES = ("project", "everywhere")   # and any kind: "research", "software", ...
GLOBAL_USAGE = "agent-usage.json"


def library_dir() -> Path:
    return Path.home() / ".claude" / "agent-library"


def _global_file() -> Optional[Path]:
    try:
        from .app import state_dir
        folder = state_dir()
    except Exception:  # noqa: BLE001
        folder = None
    return folder / GLOBAL_USAGE if folder is not None else None


_KINDS: Dict[str, tuple] = {}


def kind_of(root: Path) -> set:
    """The project's kinds: what system-diff.toml says (`kind = "research"` or a list), else "research" when an
    extension of that kind applies to it (research-diff), else "software". Kept while the index and the config are
    unchanged: an extension may ask git (a program start: seconds on some machines), so it is asked once."""
    try:
        from .projects import _stamp
        stamp = _stamp(Path(root))
    except Exception:  # noqa: BLE001
        stamp = None
    key = str(Path(root).resolve())
    got = _KINDS.get(key)
    if stamp is not None and got is not None and got[0] == stamp:
        return set(got[1])
    kinds = _kind_of(root)
    if stamp is not None:
        _KINDS[key] = (stamp, frozenset(kinds))
    return kinds


def _kind_of(root: Path) -> set:
    try:
        from .config import lenient
        said = lenient(Path(root)).get("kind")
    except Exception:  # noqa: BLE001
        said = None
    if isinstance(said, str) and said.strip():
        return {said.strip().lower()}
    if isinstance(said, list) and said:
        return {str(k).strip().lower() for k in said if str(k).strip()}
    kinds = set()
    try:
        from . import extensions
        for ext in extensions.load():
            kind = str(getattr(ext, "KIND", "") or ("research" if getattr(ext, "NAME", "") == "research" else ""))
            if kind and getattr(ext, "applies", None) is not None and extensions.applies(ext, Path(root)):
                kinds.add(kind)
    except Exception:  # noqa: BLE001
        pass
    return kinds or {"software"}


def note_use(root: Path, role: str, description: str = "", prompt: str = "", tools: str = "", model: str = "",
             event: str = "planned") -> None:
    """A role was used (planned, or finished as a subagent): counted for the repository and, across the person's
    projects, under the project's kinds."""
    name = _name(role)
    if not name:
        return
    field = "finished" if event == "finished" else "planned"
    _count(root, name, field, brief=prompt or None)
    f = _global_file()
    if f is None:
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        data = _load(f)
        r = data.setdefault("roles", {}).setdefault(name, {"kinds": {}})
        for k, v in (("description", description), ("prompt", prompt), ("tools", tools), ("model", model)):
            if v:
                r[k] = str(v)[:4000]
        r["last"] = round(time.time(), 3)
        for kind in sorted(kind_of(root)):
            c = r["kinds"].setdefault(kind, {"planned": 0, "finished": 0, "projects": []})
            c[field] = int(c.get(field) or 0) + 1
            if str(Path(root).resolve()) not in c["projects"]:
                c["projects"] = (c["projects"] + [str(Path(root).resolve())])[-50:]
        _save(f, data)
    except (OSError, ValueError):
        pass


def _library() -> List[Dict[str, Any]]:
    out = []
    folder = library_dir()
    for f in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _front(text)
        body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S).replace(MARKER, "").strip()
        kinds = {k.strip().lower() for k in fm.get("kinds", "everywhere").split(",") if k.strip()}
        out.append({"name": fm.get("name") or f.stem, "description": fm.get("description", ""), "prompt": body,
                    "tools": fm.get("tools", ""), "model": fm.get("model", ""), "kinds": sorted(kinds),
                    "path": str(f), "ours": MARKER in text, "scope": ",".join(sorted(kinds))})
    return out


def _files_of(folder: Path, scope: str) -> List[Dict[str, Any]]:
    out = []
    for f in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _front(text)
        body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S).replace(MARKER, "").strip()
        out.append({"name": fm.get("name") or f.stem, "description": fm.get("description", ""), "prompt": body,
                    "tools": fm.get("tools", ""), "model": fm.get("model", ""), "path": str(f), "scope": scope,
                    "ours": MARKER in text})
    return out


def for_project(root: Path) -> List[Dict[str, Any]]:
    """The agents a session in this project gets: the library's of its kinds, the person's (~/.claude/agents), and the
    repository's own, which win on a name clash. Each with "source": project, user or library."""
    kinds = kind_of(root)
    got: Dict[str, Dict[str, Any]] = {}
    for a in _library():
        if "everywhere" in a["kinds"] or set(a["kinds"]) & kinds:
            got[a["name"]] = {**a, "source": "library"}
    for a in _files_of(Path.home() / ".claude" / "agents", "user"):
        got[a["name"]] = {**a, "source": "user"}
    for a in _files_of(Path(root) / ".claude" / "agents", "project"):
        got[a["name"]] = {**a, "source": "project"}
    return sorted(got.values(), key=lambda a: a["name"])


def suggest(root: Path) -> List[Dict[str, Any]]:
    """The roles worth keeping, with the scope to offer: "project" (only this repository uses it), a kind ("research":
    used in several projects of that kind), or "everywhere" (used across kinds)."""
    have = {a["name"] for a in for_project(root)} | {a["name"] for a in _library()}
    out: Dict[str, Dict[str, Any]] = {}
    for r in worth_keeping(root):
        if r["name"] not in have:
            out[r["name"]] = {**r, "scope": "project"}
    f = _global_file()
    roles = (_load(f).get("roles") or {}) if f is not None else {}
    mine = kind_of(root)
    for name, r in roles.items() if isinstance(roles, dict) else []:
        if name in have or not isinstance(r, dict):
            continue
        kinds = r.get("kinds") or {}
        wide = {k: c for k, c in kinds.items() if len(c.get("projects") or []) >= 2}
        if not wide or not (set(wide) & mine or len(wide) > 1):
            continue
        scope = "everywhere" if len(wide) > 1 else next(iter(wide))
        n = sum(len(c.get("projects") or []) for c in wide.values())
        out[name] = {"name": name, "scope": scope, "why": f"used in {n} of your projects ({', '.join(sorted(wide))})",
                     "planned": sum(int(c.get("planned") or 0) for c in kinds.values()),
                     "finished": sum(int(c.get("finished") or 0) for c in kinds.values()),
                     "area": "", "brief": str(r.get("prompt") or "")[:600], "description": r.get("description", ""),
                     "by": ["system-diff"]}
    return sorted(out.values(), key=lambda r: -(r["planned"] + r["finished"]))


def save(name: str, description: str, prompt: str, tools: str = "", model: str = "", scope: str = "project",
         root: Optional[Path] = None) -> Dict[str, Any]:
    """Save an agent: "project" (the repository's .claude/agents, needs root), "everywhere" (~/.claude/agents: every
    session loads it), or a kind ("research", "software": the library, given to projects of that kind)."""
    scope = str(scope or "project").strip().lower()
    if scope == "user":
        scope = "everywhere"
    if scope == "project":
        if root is None:
            raise ValueError("a project agent needs the project")
        return save_agent(root, name, description, prompt, tools, model, "project")
    if scope == "everywhere":
        return save_agent(Path.home(), name, description, prompt, tools, model, "user")
    if not re.match(r"^[a-z][a-z0-9-]{0,30}$", scope):
        raise ValueError("scope: project, everywhere, or a kind of project such as research or software")
    name = _name(name)
    if not NAME.match(name or ""):
        raise ValueError("name: lowercase letters, digits and dashes")
    if not str(description).strip() or not str(prompt).strip():
        raise ValueError("an agent needs a description (when to use it) and its standing instructions")
    f = library_dir() / f"{name}.md"
    kinds = {scope}
    if f.exists():
        old = next((a for a in _library() if a["path"] == str(f)), None)
        if old is not None and old["ours"]:
            kinds |= set(old["kinds"]) - {"everywhere"}
    head = ["---", f"name: {name}", f"description: {_yaml(description.strip()[:1000])}", f"kinds: {', '.join(sorted(kinds))}"]
    if tools.strip():
        head.append(f"tools: {tools.strip()[:400]}")
    if model.strip():
        head.append(f"model: {model.strip()[:60]}")
    _write(f, "\n".join(head + ["---", MARKER, "", prompt.strip(), ""]))
    return {"saved": name, "scope": ", ".join(sorted(kinds)), "path": str(f),
            "use": f"projects of kind {', '.join(sorted(kinds))} get it (the Studio at once; a terminal session after "
                   "install)"}


def forget_library(name: str) -> Dict[str, Any]:
    f = library_dir() / f"{_name(name)}.md"
    if not f.exists():
        raise ValueError(f"no library agent {name!r}")
    if MARKER not in f.read_text(encoding="utf-8"):
        raise ValueError(f"{f} was written by someone else: system-diff leaves it as it is")
    f.unlink()
    return {"forgot": _name(name), "scope": "library"}


def install(root: Path, names: List[str]) -> Dict[str, Any]:
    """Copy library agents into the repository's .claude/agents/, so terminal sessions there get them too (only when
    the person says so: it writes files in their repository). Never over a file system-diff did not write."""
    lib = {a["name"]: a for a in _library()}
    done, skipped = [], []
    for n in names:
        a = lib.get(_name(n))
        if a is None:
            skipped.append({"name": n, "why": "not in the library"})
            continue
        try:
            save_agent(root, a["name"], a["description"] or a["name"], a["prompt"], a["tools"], a["model"], "project")
            done.append(a["name"])
        except ValueError as exc:
            skipped.append({"name": a["name"], "why": str(exc)})
    return {"installed": done, "skipped": skipped}


def sdk_agents(root: Path) -> Dict[str, Any]:
    """The library's agents of this project's kinds, for a Studio session (the Agent SDK's `agents`): the ones Claude
    Code would not load from files already (not the repository's own or the person's)."""
    from claude_agent_sdk import AgentDefinition
    out = {}
    for a in for_project(root):
        if a["source"] != "library":
            continue
        tools = [t.strip() for t in a["tools"].split(",") if t.strip()] or None
        model = a["model"] if a["model"] in ("sonnet", "opus", "haiku", "inherit") else None
        out[a["name"]] = AgentDefinition(description=a["description"] or a["name"], prompt=a["prompt"], tools=tools,
                                         model=model)
    return out


def ran_agents(root: Path) -> List[Dict[str, Any]]:
    """The subagents that ran in this project, newest first: {"id", "title", "type", "role", "prompt", "at"}, from the
    runs the hooks recorded (the Task call's description and prompt) and what Claude Code keeps beside each one's
    transcript."""
    from . import agents
    by: Dict[str, Dict[str, Any]] = {}
    for r in agents.runs(Path(root)):
        aid = str(r.get("agent_id") or "")
        if not aid:
            continue
        e = by.setdefault(aid, {"id": aid, "title": "", "type": "", "prompt": "", "at": 0.0, "transcript": ""})
        e["at"] = max(e["at"], float(r.get("at") or 0))
        e["seq"] = len(by) if "seq" not in e else e["seq"]   # the order they were recorded: ties of time
        e["type"] = e["type"] or str(r.get("agent_type") or "")
        e["title"] = e["title"] or str(r.get("description") or "")
        e["prompt"] = e["prompt"] or str(r.get("prompt") or "")
        e["transcript"] = e["transcript"] or str(r.get("transcript") or "")
    out = []
    for e in by.values():
        if (not e["title"] or not e["prompt"]) and e["transcript"]:
            info = agents.subagent_info(e["transcript"])
            e["title"] = e["title"] or str(info.get("description") or "")
            e["type"] = e["type"] or str(info.get("type") or "")
            e["prompt"] = e["prompt"] or str(info.get("prompt") or "")
        e["role"] = role_of(e["title"], e["type"]) if (e["title"] or e["type"]) else ""
        e.pop("transcript", None)
        out.append(e)
    out.sort(key=lambda e: (-e["at"], -e["seq"]))
    for e in out:
        e.pop("seq", None)
    return out


def keep_ran(root: Path, which: str, name: str = "", scope: str = "project", description: str = "") -> Dict[str, Any]:
    """Keep a subagent that ran here as an agent: `which` is its id ("sub:<id>" too), its role, or words of its
    description. Several different ones match: they are listed to choose from, never guessed."""
    want = str(which or "").strip()
    if want.startswith("sub:"):
        want = want[4:]
    if not want:
        raise ValueError("which: the subagent's id, its role, or words of what it did")
    every = [e for e in ran_agents(root) if e["prompt"]]
    words = re.findall(r"[a-z0-9]+", want.lower())
    hits = [e for e in every if e["id"] == want or (len(want) >= 6 and e["id"].startswith(want))]
    hits = hits or [e for e in every if e["role"] and e["role"] == _name(want)]
    hits = hits or [e for e in every if words and all(w in (e["title"] + " " + e["type"]).lower() for w in words)]
    listing = [{"which": "sub:" + e["id"], "title": e["title"] or e["type"], "role": e["role"]}
               for e in (hits or every)[:10]]
    if not hits:
        return {"kept": False, "error": f"no subagent that ran here matches {which!r}", "ran": listing}
    if len({e["role"] or e["id"] for e in hits}) > 1:
        return {"kept": False, "choose": listing,
                "say": "several subagents match: ask the person which one, then call keep_agent with its which"}
    hit = hits[0]   # the newest of that role: its latest prompt
    kind = str(scope or "project").strip().lower()
    if kind == "kind":
        kinds = sorted(kind_of(Path(root)))
        kind = kinds[0] if kinds else "software"
    title = hit["title"] or hit["type"] or hit["role"]
    if not description:
        description = ("Use to " + title[:1].lower() + title[1:]) if title else "A subagent kept from a run"
    got = save(name or hit["role"] or _name(title), description, hit["prompt"], "", "", kind, root=Path(root))
    return {"kept": True, "from": "sub:" + hit["id"], "title": title, "runs_of_this_role": len(hits), **got}
