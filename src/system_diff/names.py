"""Readable names for the parts of a system, without any LLM: a part the config does not label is named by what it
says about itself (its package's description, its module docstring, its README's heading), else by its folder said as
a role ("bizflow_backend/routers" -> "API routes", "libs/llm" -> "LLM client", "services/agent" -> "Agent service",
"web/src" -> "Web app"), with a role word for what it does where that is cheap (it serves endpoints: "API"). The path
stays the part's id, shown on hover and in the detail.

Who wins, for a part's label: the config's `label`; then the LLM's overlay (regroup.py, applied by architecture.py);
then these names. Names are unique: two parts that would read the same get the folder that tells them apart.

    names.label(bp, cid)            the name of one part (the config's label where it declares one)
    names.of(bp)                    {part id: name} for every part of a blueprint
    names.learn(root, bp)           read each part's own description once (cached by the file's content), for `of`
    names.folded(bp, cfg)           the parts under docs/, examples/, scripts/, tools/ that connect to nothing: drawn
                                    together in one "Docs and tools" box, unless the config places them
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

MAX = 32                       # a name longer than this is cut, at a word
FOLD_TITLE = "Docs and tools"
NOT_RUNNING = {"docs", "doc", "documentation", "examples", "example", "samples", "scripts", "script", "tools",
               "tool", "devtools", "bin"}

# A folder's own name said as the role it plays.
ROLE = {
    "routers": "API routes", "router": "API routes", "routes": "API routes", "route": "API routes",
    "api": "API routes", "endpoints": "API routes", "views": "Views", "controllers": "Controllers",
    "handlers": "Handlers",
    "models": "Data models", "model": "Data models", "entities": "Data models", "schemas": "Schemas",
    "schema": "Schemas", "dto": "Schemas", "db": "Database access", "db_client": "Database access",
    "database": "Database access", "repos": "Database access", "repositories": "Database access",
    "repository": "Database access", "dal": "Database access", "migrations": "Migrations", "alembic": "Migrations",
    "llm": "LLM client", "llms": "LLM client", "mcp_client": "MCP client", "mcp": "MCP", "mcp_server": "MCP server",
    "logging": "Logging", "log": "Logging", "logs": "Logging", "auth": "Auth", "authentication": "Auth",
    "components": "UI components", "pages": "Pages", "screens": "Screens", "shell": "App shell",
    "hooks": "UI hooks", "store": "State store", "stores": "State store", "styles": "Styles",
    "services": "Services", "workers": "Workers", "worker": "Worker", "jobs": "Jobs", "tasks": "Tasks",
    "config": "Settings", "settings": "Settings", "core": "Core", "domain": "Domain logic",
    "cli": "Command line", "main": "Main program", "middleware": "Middleware", "types": "Types",
    "utils": "Shared utilities", "util": "Shared utilities", "helpers": "Shared utilities",
    "common": "Shared code", "shared": "Shared code", "lib": "Shared library", "libs": "Shared library",
    "web": "Web app", "frontend": "Web app", "client": "Web app", "ui": "Web app", "webapp": "Web app",
    "backend": "Backend", "server": "Server", "app": "App", "src": "Source",
}
UI_ROLES = {"UI components", "Pages", "Screens", "App shell", "UI hooks", "Web app", "Styles", "State store"}
LIB_FOLDERS = {"lib", "libs", "packages", "pkg", "shared"}
SERVICE_FOLDERS = {"services": "service", "apps": "app", "workers": "worker", "agents": "agent"}
WRAPPERS = {"src", "app", "lib"}          # a folder that only wraps the code: web/src is the web app
ACRONYMS = {"api", "llm", "mcp", "ui", "db", "sql", "http", "ai", "id", "io", "aws", "s3", "ses", "jwt", "sso",
            "ews", "cli", "sdk", "gpt", "rag", "pdf", "csv", "json", "html", "css", "url", "ux", "ml", "nlp", "etl",
            "crm", "erp", "smtp", "oauth"}
DESCRIBED_BY = ("pyproject.toml", "package.json", "__init__.py", "README.md", "README.rst", "README.txt", "README")


def _words(seg: str) -> str:
    """A folder or file name as words, title-cased, acronyms upper: "local_ews_helper" -> "Local EWS Helper"."""
    seg = re.sub(r"\.(py|ts|tsx|js|jsx|mjs|cjs)$", "", seg)
    seg = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", seg)
    out = []
    for w in re.split(r"[_\-.\s]+", seg):
        if not w:
            continue
        out.append(w.upper() if w.lower() in ACRONYMS else (w[:1].upper() + w[1:]) if not w.isupper() else w)
    return " ".join(out) or seg


def cut(text: str, most: int = MAX) -> str:
    text = " ".join(text.split()).strip(" .:;,-")
    if len(text) <= most:
        return text
    head = text[: most - 1]
    space = head.rfind(" ")
    return (head[:space] if space >= most // 2 else head).rstrip(" ,;:-") + "…"


def _segments(cid: str) -> List[str]:
    return [s for s in cid.split("/") if s]


def _common_top(ids: Iterable[str]) -> str:
    """The first folder every part is under ("bizflow_backend"), when there is one and parts go deeper than it."""
    tops = {(_segments(c) or [""])[0] for c in ids}
    many = [c for c in ids if len(_segments(c)) > 1]
    return next(iter(tops)) if len(tops) == 1 and many else ""


def from_path(cid: str, top: str = "") -> str:
    """A part's folder said as what it is."""
    segs = _segments(cid)
    if top and segs[:1] == [top] and len(segs) > 1:
        segs = segs[1:]
    while len(segs) > 1 and segs[-1].lower() in WRAPPERS:      # web/src -> web
        segs = segs[:-1]
    if not segs:
        return cid
    last = re.sub(r"\.(py|ts|tsx|js|jsx)$", "", segs[-1])
    low = last.lower()
    parent = segs[-2].lower() if len(segs) > 1 else ""
    if parent in NOT_RUNNING:
        return f"{'Docs' if parent.startswith('doc') else 'Tool' if parent.startswith('tool') else _words(parent)}: " \
               f"{_words(last)}"
    if parent in SERVICE_FOLDERS and low not in ROLE:
        return f"{_words(last)} {SERVICE_FOLDERS[parent]}"
    if low in ROLE:
        role = ROLE[low]
        if role in ("Shared library", "Shared code") and len(segs) > 1:
            return f"{role}: {_words(segs[-2])}"
        return role
    if parent in LIB_FOLDERS:
        return f"Shared library: {_words(last)}"
    return _words(last)


# -------------------------------------------------------------------------------------------- descriptions

_ABOUT_BY_BLOB: Dict[str, str] = {}          # a description file's content id -> what it says ("" for nothing)
_ABOUT: Dict[str, Dict[str, str]] = {}       # a structure (its part ids) -> {part: description}
_LEARNED: Dict[str, float] = {}             # when each structure's were last looked at
RELEARN = 10.0                               # seconds: a draw within this of the last looks again at no file
_LOCK = threading.Lock()


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text).rstrip(".")


def describe(name: str, text: str) -> str:
    """What a description file says a package is, in a line: pyproject's [project] description, package.json's
    "description", a module docstring's first sentence, a README's first heading or line."""
    base = name.rsplit("/", 1)[-1]
    try:
        if base == "pyproject.toml":
            m = re.search(r"^\[project\][^\[]*?^description\s*=\s*([\"'])(.*?)\1", text, re.M | re.S)
            return _first_sentence(m.group(2)) if m else ""
        if base == "package.json":
            got = json.loads(text).get("description")
            return _first_sentence(got) if isinstance(got, str) else ""
        if base == "__init__.py":
            m = re.match(r'\s*(?:#[^\n]*\n\s*)*[rRuU]?("""|\'\'\')(.*?)\1', text, re.S)
            return _first_sentence(m.group(2).strip().split("\n\n")[0]) if m else ""
        for line in text.splitlines():                                     # a README
            line = line.strip().lstrip("#").strip().strip("=*_` ")
            if line and not line.startswith(("<", "[", "!", "---", "===")):
                return _first_sentence(line)
    except (ValueError, AttributeError):
        return ""
    return ""


def _usable(about: str, cid: str) -> bool:
    """A description that names the part, not boilerplate or the folder name itself."""
    low = about.lower().strip()
    if not low or len(low) < 3 or low in ("todo", "tbd", "none", "description", "add your description here"):
        return False
    return low.replace(" ", "_") != _segments(cid)[-1].lower() and "\n" not in about


def _key(bp: dict) -> str:
    return hashlib.sha1("\0".join(sorted(bp.get("components", {}))).encode()).hexdigest()


def learn(root: Path, bp: dict, read: Optional[Any] = None) -> Dict[str, str]:
    """Read what each undeclared part says about itself from the files in its own folder (the working tree's, or
    `read(path) -> text | None`), each file once per content; kept for `of` and `label` on this structure. A part
    ending in a folder that only wraps the code (web/src) is described by the folder above it too. Never raises."""
    out: Dict[str, str] = {}
    key = _key(bp)
    with _LOCK:
        if read is None and key in _ABOUT and time.monotonic() - _LEARNED.get(key, -1e9) < RELEARN:
            return dict(_ABOUT[key])
    try:
        root = Path(root)
        for cid, c in bp.get("components", {}).items():
            if c.get("declared") and c.get("label") not in (None, "", cid):
                continue
            segs = _segments(cid)
            folders = ["/".join(segs)]
            if len(segs) > 1 and segs[-1].lower() in WRAPPERS:
                folders.append("/".join(segs[:-1]))
            for folder in folders:
                said = ""
                for name in DESCRIBED_BY:
                    path = f"{folder}/{name}" if folder else name
                    said = _read_about(root, path, read)
                    if said and _usable(said, cid):
                        break
                    said = ""
                if said:
                    out[cid] = said
                    break
        with _LOCK:
            _ABOUT[key] = out
            _LEARNED[key] = time.monotonic()
            while len(_ABOUT) > 16:
                _ABOUT.pop(next(iter(_ABOUT)))
    except Exception:  # noqa: BLE001 - names only help
        pass
    return out


def _read_about(root: Path, path: str, read: Optional[Any]) -> str:
    if read is not None:
        text = read(path)
        if not text:
            return ""
        bid = hashlib.sha1(text[:20000].encode("utf-8", "replace")).hexdigest()
    else:
        f = root / path
        try:
            st = f.stat()
        except OSError:
            return ""
        if st.st_size > 200_000 or not f.is_file():
            return ""
        bid = f"{f}\0{st.st_size}\0{st.st_mtime_ns}"
        if bid in _ABOUT_BY_BLOB:
            return _ABOUT_BY_BLOB[bid]
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        bid = hashlib.sha1(text[:20000].encode("utf-8", "replace")).hexdigest()
        _ABOUT_BY_BLOB[f"{f}\0{st.st_size}\0{st.st_mtime_ns}"] = _ABOUT_BY_BLOB.get(bid) or describe(path, text)
    if bid not in _ABOUT_BY_BLOB:
        _ABOUT_BY_BLOB[bid] = describe(path, text)
    if len(_ABOUT_BY_BLOB) > 4000:
        _ABOUT_BY_BLOB.clear()
    return _ABOUT_BY_BLOB[bid]


def about(bp: dict) -> Dict[str, str]:
    """The descriptions learned for this structure ({} before `learn`)."""
    with _LOCK:
        return dict(_ABOUT.get(_key(bp), {}))


# -------------------------------------------------------------------------------------------- the names

def _declared(c: dict, cid: str) -> Optional[str]:
    lab = c.get("label")
    return lab if c.get("declared") and lab and lab != cid else None


def _role_word(name: str, cid: str, bp: dict, kind: str) -> str:
    low = name.lower()
    serves = any(o == cid for o in (bp.get("routes") or {}).values())
    if name in UI_ROLES:
        return name
    if serves and not any(w in low for w in ("api", "route", "endpoint", "server", "service")):
        return f"{name} API"
    if kind == "data" and not any(w in low for w in ("data", "database", "store", "model", "schema", "repo")):
        return f"{name} data"
    return name


_CACHE: Dict[Tuple[str, str], Dict[str, str]] = {}


def of(bp: dict) -> Dict[str, str]:
    """{part id: name} for every part of `bp`: the config's label, else its own description, else its folder said
    as a role; a role word where it serves endpoints (or keeps tables); unique."""
    comps = bp.get("components") or {}
    said = about(bp)
    serves = sorted(set((bp.get("routes") or {}).values()))
    ck = (_key(bp), hashlib.sha1(json.dumps([said, serves, sorted((c, _declared(v, c) or "", v.get("kind") or "")
                                                    for c, v in comps.items())],
                                            default=str).encode()).hexdigest())
    with _LOCK:
        hit = _CACHE.get(ck)
    if hit is not None:
        return dict(hit)
    top = _common_top([c for c in comps if not _declared(comps[c], c)])
    out: Dict[str, str] = {}
    fixed: Set[str] = set()
    for cid, c in comps.items():
        dec = _declared(c, cid)
        if dec:
            out[cid] = dec
            fixed.add(cid)
        elif said.get(cid):
            out[cid] = cut(said[cid])
        else:
            out[cid] = cut(_role_word(from_path(cid, top), cid, bp, c.get("kind") or ""))
    owners = [(_segments(o) or [""])[0] for o in (bp.get("routes") or {}).values()]
    main = max(set(owners), key=owners.count) if owners else ""
    out = unique(out, fixed, top, main)
    with _LOCK:
        _CACHE[ck] = out
        while len(_CACHE) > 32:
            _CACHE.pop(next(iter(_CACHE)))
    return dict(out)


def unique(names: Dict[str, str], fixed: Iterable[str] = (), top: str = "", main: str = "") -> Dict[str, str]:
    """The same names, two that read the same told apart by the folder that differs ("Database access (libs)"); the
    one in the repository's main package (`main`: the folder serving its endpoints, else the one holding the most
    parts) keeps the plain name; a configured label is never changed."""
    fixed = set(fixed)
    out = dict(names)
    home: Dict[str, int] = {}
    for cid in names:
        top_ = (_segments(cid) or [""])[0]
        home[top_] = home.get(top_, 0) + 1
    for _round in range(3):
        by: Dict[str, List[str]] = {}
        for cid, n in out.items():
            by.setdefault(n.casefold(), []).append(cid)
        clash = [ids for ids in by.values() if len(ids) > 1]
        if not clash:
            break
        for ids in clash:
            free = [c for c in ids if c not in fixed]
            if not free:
                continue
            if _round == 0 and len(free) == len(ids):
                # the one in the folder holding the most parts (the repository's own package) keeps the plain name
                size = {c: (10 ** 6 if main and (_segments(c) or [""])[0] == main else 0)
                        + home.get((_segments(c) or [""])[0], 0) for c in free}
                best = max(size.values())
                if list(size.values()).count(best) == 1:
                    free = [c for c in free if size[c] != best]
            for cid in free:
                out[cid] = cut(f"{names[cid]} ({_qualifier(cid, ids, top, _round)})", MAX + 12)
    return out


def _qualifier(cid: str, ids: List[str], top: str, depth: int) -> str:
    """The folder of `cid` that tells it apart from the others in `ids`."""
    segs = [s for s in _segments(cid) if s.lower() not in WRAPPERS or s == _segments(cid)[0]]
    if top and segs[:1] == [top] and len(segs) > 1:
        segs = segs[1:]
    others = [[s for s in _segments(o)] for o in ids if o != cid]
    if depth >= 2:
        return cid
    for s in segs[:-1] if len(segs) > 1 else segs:
        if all(s not in o for o in others):
            return _words(s).lower() if s.islower() else _words(s)
    return cid if depth else _words(segs[0]).lower()


GROUP = {"libs": "Shared libraries", "lib": "Shared libraries", "packages": "Packages", "pkg": "Packages",
         "services": "Services", "apps": "Apps", "web": "Web app", "frontend": "Web app", "backend": "Backend",
         "server": "Server", "workers": "Workers", "agents": "Agents", "plugins": "Plugins", "modules": "Modules"}


def group_title(folder: str) -> str:
    return GROUP.get(folder.lower()) or _words(folder)


def by_folder(parts: Iterable[str]) -> Dict[str, List[str]]:
    """Parts by the folder they are in, below the one every part shares: {container title: parts}, only folders
    holding two or more parts ("libs/llm", "libs/db_client" -> {"Shared libraries": [...]})."""
    parts = sorted(parts)
    top = _common_top(parts)
    found: Dict[str, List[str]] = {}
    for cid in parts:
        segs = _segments(cid)
        if top and segs[:1] == [top]:
            segs = segs[1:]
        if len(segs) >= 2:
            found.setdefault(segs[0], []).append(cid)
    out: Dict[str, List[str]] = {}
    for f, held in found.items():
        if len(held) >= 2:
            out.setdefault(group_title(f), []).extend(held)
    return out


def label(bp: dict, cid: str) -> str:
    """The name of one part of `bp` (its config label where it declares one)."""
    c = (bp.get("components") or {}).get(cid)
    if c is None:
        return cid
    return _declared(c, cid) or of(bp).get(cid, cid)


def folded(bp: dict, cfg: Dict[str, Any]) -> Set[str]:
    """The parts that are not the running system, by where they are (docs/, examples/, scripts/, tools/) and by what
    they do (nothing connects to or from them, they serve no endpoint), unless the config declares them."""
    from . import graph
    declared = {c.get("id") for c in cfg.get("components", []) if isinstance(c, dict)}
    linked: Set[str] = set()
    for (s, t), _k in graph.connections(bp).items():
        linked.update((s, t))
    served = set((bp.get("routes") or {}).values())
    return {cid for cid in bp.get("components", {})
            if cid not in declared and cid not in linked and cid not in served
            and any(s.lower() in NOT_RUNNING for s in _segments(cid)[:-1] or _segments(cid)[:1])}
