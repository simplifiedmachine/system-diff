"""The Claude Code settings a Studio session can run with, for the page's dropdowns: the person's (~/.claude), the
project's (.claude/settings.json), the local one (.claude/settings.local.json), and any other settings files kept in
those two folders (profiles such as settings.opus.json or settings.bedrock.json). For each: where it is, the model it
sets, the provider it points at and its permission rules in short. No secret reaches the page: a value whose name
says key, token, secret, password or credential is shown as •••.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

SECRET = re.compile(r"key|token|secret|password|passwd|credential|auth", re.I)
MASK = "•••"
OWN = "claude-code"   # the dropdown's first choice: Claude Code's own settings (user + project + local)


def _home() -> Path:
    return Path.home() / ".claude"


def mask(env: Dict[str, Any]) -> Dict[str, str]:
    return {str(k): (MASK if SECRET.search(str(k)) else str(v)[:200]) for k, v in (env or {}).items()}


def _read(f: Path) -> Optional[Dict[str, Any]]:
    try:
        got = json.loads(f.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


def describe(f: Path, scope: str, data: Dict[str, Any]) -> Dict[str, Any]:
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    model = str(data.get("model") or env.get("ANTHROPIC_MODEL") or "")
    on = lambda k: str(env.get(k, "")).strip().lower() not in ("", "0", "false", "no", "off")   # noqa: E731
    provider = ("Bedrock" if on("CLAUDE_CODE_USE_BEDROCK") else "Vertex" if on("CLAUDE_CODE_USE_VERTEX")
                else "Foundry" if on("CLAUDE_CODE_USE_FOUNDRY") else "")
    if not provider and env.get("ANTHROPIC_BASE_URL"):
        provider = "gateway " + str(env["ANTHROPIC_BASE_URL"])[:120]
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    rules = {k: len(perms.get(k) or []) for k in ("allow", "ask", "deny") if perms.get(k)}
    name = f.name.removeprefix("settings.").removesuffix(".json") if f.name != "settings.json" else scope
    return {"id": f"{scope}:{f.name}", "name": name if name != "json" else scope, "scope": scope, "path": str(f),
            "model": model, "provider": provider, "permissions": rules,
            "default_mode": str(perms.get("defaultMode") or ""), "env": mask(env),
            "effort": str(data.get("effortLevel") or data.get("effort") or "")}


def discover(project: Path, home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every settings file the dropdown offers, user ones first."""
    home = home if home is not None else _home()
    out: List[Dict[str, Any]] = []
    for folder, scope in ((home, "user"), (Path(project) / ".claude", "project")):
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.json")):
            if not (f.name.startswith("settings") or f.name.endswith(".settings.json")):
                continue
            data = _read(f)
            if data is None:
                continue
            sc = "local" if f.name == "settings.local.json" else scope
            out.append(describe(f, sc, data))
    return out


def choices(project: Path, home: Optional[Path] = None) -> Dict[str, Any]:
    """What the page's dropdowns offer: settings, models, permission modes."""
    from .sessions import EFFORTS, MODELS, PERMISSION_MODES
    found = discover(project, home)
    models = []
    for p in found:
        if p["model"] and p["model"] not in models:
            models.append(p["model"])
    return {"settings": [{"id": OWN, "name": "Claude Code's own (user + project + local)", "scope": "",
                          "path": "", "model": "", "provider": "", "permissions": {}, "env": {}}] + found,
            "models": [""] + models + [m for m in MODELS if m not in models],
            "permission_modes": list(PERMISSION_MODES), "danger": ["bypassPermissions"],
            "efforts": [""] + list(EFFORTS)}


def options_for(project: Path, choice: Dict[str, Any], home: Optional[Path] = None) -> Dict[str, Any]:
    """The session options a dropdown choice means: Claude Code's own settings, or one file (and the sources ticked)."""
    from .sessions import EFFORTS, PERMISSION_MODES
    profile = str(choice.get("settings_profile") or OWN)
    sources = choice.get("setting_sources")
    sources = [s for s in sources if s in ("user", "project", "local")] if isinstance(sources, list) else None
    out: Dict[str, Any] = {"settings_profile": profile, "model": str(choice.get("model") or "")[:100],
                           "permission_mode": str(choice.get("permission_mode") or "default"),
                           "agent": str(choice.get("agent") or "")[:80],
                           "effort": str(choice.get("effort") or "")}
    if out["effort"] and out["effort"] not in EFFORTS:
        raise ValueError(f"effort is one of {', '.join(EFFORTS)}")
    if out["permission_mode"] not in PERMISSION_MODES:
        raise ValueError(f"permission_mode is one of {', '.join(PERMISSION_MODES)}")
    if profile == OWN:
        out["setting_sources"] = sources if sources is not None else ["user", "project", "local"]
        return out
    hit = next((p for p in discover(project, home) if p["id"] == profile), None)
    if hit is None:
        raise ValueError(f"no settings {profile!r} here")
    out["settings"] = hit["path"]
    out["setting_sources"] = sources if sources is not None else []
    if not out["model"] and hit["model"]:
        out["model"] = hit["model"]
    return out
