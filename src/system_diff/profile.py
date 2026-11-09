"""What the person wants explained: the technologies they marked as new to them (★ in the pane's Topics), so the
pane teaches the basics of those and, for everything else, only how the system is designed. Theirs across projects:
kept in their own config folder, never in a repository.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List


def path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "system-diff" / "profile.json"


def load() -> Dict[str, List[str]]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        return {"learning": sorted({str(k) for k in data.get("learning", [])})}
    except (OSError, ValueError, AttributeError):
        return {"learning": []}


def learning() -> set:
    return set(load()["learning"])


def toggle(key: str) -> bool:
    """Marks a technology as new to the person, or unmarks it; True when it is now marked."""
    data = load()
    keys = set(data["learning"])
    on = key not in keys
    keys = keys | {key} if on else keys - {key}
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"learning": sorted(keys)}, indent=1) + "\n", encoding="utf-8")
    tmp.replace(p)
    return on
