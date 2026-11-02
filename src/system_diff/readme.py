"""The README's generated system-map section: the picture, how it is made, and a warning when the picture is stale."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable

from .pictures import write_if_changed


def readme_section(dormant: Dict[str, dict], cfg: Dict[str, Any], stale: Iterable[str] = ()) -> str:
    """The generated block. `stale`: pictures that could not be redrawn this time, named, so no one mistakes an old
    picture for the current system; the SVGs beside them are always current."""
    r = cfg["readme"]
    stale = list(stale)
    tag = ""
    if stale:
        tag = r["stale"].format(pictures=", ".join(f"`{x}`" for x in stale), it="it" if len(stale) == 1 else "them")
    note = ""
    if dormant:
        names = ", ".join(f"{v['label']} ({', '.join(v['used_by'])})" for v in dormant.values())
        note = r["dormant"].format(names=names)
    return (
        f"{r['begin']}\n"
        f"{tag}"
        f"![System map, generated from the code]({r['image']})\n\n"
        "<details><summary>How this map is made</summary>\n\n"
        f"{r['how'].format(dormant=note)}\n\n</details>\n"
        f"{r['end']}"
    )


def update_readme(readme: Path, dormant: Dict[str, dict], cfg: Dict[str, Any], stale: Iterable[str] = ()) -> bool:
    """Rewrite the section between the README's markers. True when the file changed."""
    r = cfg["readme"]
    text = readme.read_text(encoding="utf-8")
    if r["begin"] not in text or r["end"] not in text:
        raise SystemExit(f"{readme} has no {r['begin']} … {r['end']} section: add the two lines where the map goes")
    start, end = text.index(r["begin"]), text.index(r["end"]) + len(r["end"])
    return write_if_changed(readme, text[:start] + readme_section(dormant, cfg, stale) + text[end:])
