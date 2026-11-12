"""An LLM's reorganisation of the system diagram for a large repository: which parts belong together in named
containers, a short human label for each part, the column each sits in, and the parts that are not the running system
(docs, tools, scripts) to fold away. Only an overlay on what the engine draws: with none, the diagram is as the code
alone gives it.

API (for the integrator):

    regroup.ensure(root: Path, bp: dict, cfg: dict) -> Optional[dict]
        Cheap enough for every draw (a hash of the structure and a small file read; never waits for an LLM, never
        raises). Returns the overlay to draw with, or None: the newest one computed for this structure, else the newest
        one at all (parts that no longer exist are ignored, new parts are drawn where the engine puts them). When none
        matches the structure, starts the background job that asks the LLM, rate-limited and locked.
        `bp` is the blueprint drawn (Comparison.after), `cfg` the comparison's config.

    architecture.system(cmp, ..., overlay=None) / architecture.container(cmp, box, ..., overlay=None)
        apply an overlay given as `overlay`, else the one set on the comparison as `cmp.regroup`. So wiring it is:
            c = compare(...); c.regroup = regroup.ensure(root, c.after, c.cfg)

The overlay (stored as <git dir>/system-diff/regroup/overlay-<key>.json):

    {"version": 1, "key": "<structure hash>", "made": <unix time>, "model": "<who wrote it>",
     "groups": {"<container name>": "<one line on it>"},
     "parts": {"<part id>": {"label": "<short name>", "layer": "ui|interface|logic|data",
                             "group": "<container name>", "fold": true}}}       every field of a part optional

What is sent: part ids, labels, file counts, a few file paths per part, the libraries each uses, endpoint counts, the
connections between parts and the outside systems they reach. Never a file's contents.

Off with SYSTEM_DIFF_REGROUP=0, or `[regroup] enabled = false` in system-diff.toml. Only for repositories with at least
`[regroup] min_parts` parts (default 8). Nothing happens when no LLM is configured (narrate.py's gateways).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import graph, names, narrate
from .sources import git, git_dir

VERSION = 1
MIN_PARTS = 8            # fewer parts than this: the engine's own diagram is clear enough
MIN_GAP = 300            # seconds between two requests for one repository
RETRY = 24 * 3600        # one structure is asked about once; again only after this long (it failed, say)
LOCK_STALE = 900         # a lock older than this is a job that died
KEEP = 5                 # overlays kept
TIMEOUT = 180            # seconds the background job waits for the model
LABEL_MAX = 32
ABOUT_MAX = 100
GROUPS_MAX = 16
PATHS_PER_PART = 4
PARTS_MAX = 250          # parts sent; the largest first
LINKS_MAX = 500          # connections sent; the strongest first

SYSTEM = (
    "You organise the architecture diagram of a software system for a person reading it. You are given only its "
    "structure, read from the code by a tool: its parts (folders of code, each with an id, its file count, a few file "
    "paths, the libraries it uses and how many HTTP endpoints it serves), the connections between parts (imports and "
    "API calls, with their weight) and the outside systems the parts reach. Reply with one JSON object and nothing "
    "else, of this shape:\n"
    '{"groups": [{"name": "<container, 1-4 words>", "about": "<one short line>", "parts": ["<part id>", ...]}],\n'
    ' "parts": {"<part id>": {"label": "<short human name, 1-4 words>", "layer": "<layer>"}},\n'
    ' "fold": ["<part id>", ...]}\n'
    "Rules. Use only the part ids given, spelled exactly. groups: the containers a system designer would draw "
    "(for example \"Backend API\", \"Workflow engine\", \"Web app\", \"Shared libraries\"), each holding two or more "
    "parts of the same layer that are deployed or reasoned about together; a part is in at most one group; leave a "
    "part that stands alone out of every group. parts: for every part, a label of at most 30 characters a newcomer "
    "understands (not the folder path), and its layer, one of the layers given. fold: the parts that are not part of "
    "the running system: documentation, examples, build, release or developer scripts, tooling; never a part that "
    "serves endpoints. Prefer fewer, meaningful groups over many small ones."
)


# ---------------------------------------------------------------------------------------------------- the structure

def _settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    s = cfg.get("regroup")
    return s if isinstance(s, dict) else {}


def enabled(cfg: Dict[str, Any]) -> bool:
    if os.environ.get("SYSTEM_DIFF_REGROUP", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return _settings(cfg).get("enabled", True) is not False


def _parts(bp: dict) -> List[str]:
    return sorted(cid for cid, c in bp["components"].items() if not c.get("quiet"))


def _links(bp: dict) -> List[Tuple[str, str, int]]:
    comps = bp["components"]
    return sorted((s, t, n) for (s, t), (k, n) in graph.connections(bp).items()
                  if k == "call" and s != t and s in comps and t in comps)


def structure_key(bp: dict) -> str:
    """What the overlay was computed from: the parts and who connects to whom (not file counts or weights, so an edit
    that adds a file or an import keeps it)."""
    shape = {"parts": _parts(bp), "links": [[s, t] for s, t, _n in _links(bp)],
             "outside": sorted(bp.get("externals", {}))}
    return hashlib.sha1(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:16]


def layers(cfg: Dict[str, Any]) -> Dict[str, str]:
    """The columns a part can sit in, with their headings."""
    cols = dict((c[0], c[1]) for c in cfg.get("picture", {}).get("columns", []) if len(c) >= 2)
    return {k: cols.get(k, k) for k in graph.PART_KINDS}


def summary(bp: dict, cfg: Dict[str, Any], paths: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """The structure the model is shown: no file's contents, only names, counts and connections."""
    from .design import kinds  # here: design imports the picture modules
    kind = kinds(bp, cfg)
    routes: Dict[str, int] = {}
    for owner in bp["routes"].values():
        routes[owner] = routes.get(owner, 0) + 1
    comps = bp["components"]
    keep = sorted(_parts(bp), key=lambda c: (-int(comps[c].get("files") or 0), c))[:PARTS_MAX]
    parts = []
    for cid in sorted(keep):
        c = comps[cid]
        p: Dict[str, Any] = {"id": cid, "name": c.get("label", cid), "layer": kind.get(cid, "logic"),
                             "files": c.get("files", 0)}
        if routes.get(cid):
            p["endpoints"] = routes[cid]
        uses = sorted((bp.get("stack") or {}).get(cid, {}).items(), key=lambda kv: (-kv[1], kv[0]))[:6]
        if uses:
            p["uses"] = [u for u, _n in uses]
        if c.get("hub"):
            p["used_everywhere"] = True
        if paths and paths.get(cid):
            p["paths"] = paths[cid][:PATHS_PER_PART]
        parts.append(p)
    kept = set(keep)
    links = sorted((l for l in _links(bp) if l[0] in kept and l[1] in kept), key=lambda l: (-l[2], l[0], l[1]))
    outside = [{"id": x, "name": e.get("label", x), "used_by": sorted(u for u in e.get("used_by", []) if u in kept)}
               for x, e in sorted(bp.get("externals", {}).items())]
    return {"layers": layers(cfg), "parts": parts, "connections": [list(l) for l in links[:LINKS_MAX]],
            "outside": outside}


def sample_paths(root: Path, cfg: Dict[str, Any], parts: List[str]) -> Dict[str, List[str]]:
    """A few file paths of each part (names only), shortest first: what tells a reader what a folder is."""
    from .extract import Code
    code, want, out = Code(cfg), set(parts), {}  # type: ignore[var-annotated]
    try:
        files = git("ls-files", "-z", cwd=root).split("\0")
    except (OSError, subprocess.CalledProcessError):
        return {}
    for f in files:
        if f and code.is_code(f) and not code.is_test(f):
            cid = code.part_of(f)
            if cid in want:
                out.setdefault(cid, []).append(f)
    return {cid: sorted(fs, key=lambda f: (f.count("/"), len(f), f))[:PATHS_PER_PART] for cid, fs in out.items()}


def prompt(s: Dict[str, Any]) -> str:
    lay = "\n".join(f"- {k}: {v}" for k, v in s["layers"].items())
    return (f"Layers (use these ids):\n{lay}\n\nThe system's structure (JSON):\n"
            + json.dumps({k: s[k] for k in ("parts", "connections", "outside")}, separators=(",", ":"))
            + "\n\nReply with the JSON object only.")


# ---------------------------------------------------------------------------------------------------- the answer

def _short(v: Any, most: int) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = " ".join(v.split())
    return v if 0 < len(v) <= most else None


def _json(text: str) -> Any:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    return json.loads(text[start:end + 1])


def validate(answer: Any, s: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The model's answer as an overlay, or None. Unknown part ids, long labels, layers not offered, groups of fewer
    than two parts and folding a part with endpoints are dropped; a reply that is not the asked shape is no overlay."""
    if isinstance(answer, str):
        try:
            answer = _json(answer)
        except ValueError:
            return None
    if not isinstance(answer, dict):
        return None
    groups_in, parts_in, fold_in = answer.get("groups", []), answer.get("parts", {}), answer.get("fold", [])
    if not isinstance(groups_in, list) or not isinstance(parts_in, dict) or not isinstance(fold_in, list):
        return None
    known = {p["id"]: p for p in s["parts"]}
    allowed = set(s["layers"])
    parts: Dict[str, Dict[str, Any]] = {}
    for cid, p in parts_in.items():
        if cid not in known or not isinstance(p, dict):
            continue
        got: Dict[str, Any] = {}
        label = _short(p.get("label"), LABEL_MAX)
        if label:
            got["label"] = label
        if p.get("layer") in allowed:
            got["layer"] = p["layer"]
        if got:
            parts[cid] = got
    folded = [c for c in fold_in if isinstance(c, str) and c in known and not known[c].get("endpoints")]
    if len(set(folded)) * 2 > len(known):
        folded = []                                  # more than half the system is not the system: not believed
    for cid in folded:
        parts.setdefault(cid, {})["fold"] = True
    groups: Dict[str, str] = {}
    for g in groups_in:
        if not isinstance(g, dict) or len(groups) >= GROUPS_MAX:
            continue
        name = _short(g.get("name"), LABEL_MAX)
        members = [c for c in g.get("parts", []) if isinstance(c, str) and c in known
                   and not parts.get(c, {}).get("fold") and "group" not in parts.get(c, {})]
        members = list(dict.fromkeys(members))
        if not name or name in groups or len(members) < 2:
            continue
        groups[name] = _short(g.get("about"), ABOUT_MAX) or ""
        for c in members:
            parts.setdefault(c, {})["group"] = name
    if not parts:
        return None
    return {"version": VERSION, "groups": groups, "parts": parts}


# ---------------------------------------------------------------------------------------------------- storage

_READ: Dict[str, Tuple[float, Optional[dict]]] = {}


_DIRS: Dict[str, Path] = {}


def _dir(root: Path) -> Path:
    """<git dir>/system-diff/regroup, found once per process (it is asked on every draw)."""
    d = _DIRS.get(str(root))
    if d is None or not d.is_dir():
        d = _DIRS[str(root)] = git_dir(root, "regroup")
    return d


def _load(f: Path) -> Optional[dict]:
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return None
    hit = _READ.get(str(f))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) and isinstance(data.get("parts"), dict) else None
    except (OSError, ValueError):
        data = None
    _READ[str(f)] = (mtime, data)
    return data


def current(root: Path, key: str) -> Optional[dict]:
    """The overlay for this structure, else the newest one at all; None when there is none."""
    d = _dir(root)
    exact = _load(d / f"overlay-{key}.json")
    if exact:
        return exact
    for f in sorted(d.glob("overlay-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        got = _load(f)
        if got:
            return got
    return None


def save(root: Path, key: str, overlay: Dict[str, Any], model: str = "") -> Path:
    d = _dir(root)
    f = d / f"overlay-{key}.json"
    tmp = d / f".overlay-{key}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({**overlay, "key": key, "made": time.time(), "model": model}), encoding="utf-8")
    os.replace(tmp, f)
    for old in sorted(d.glob("overlay-*.json"), key=lambda p: p.stat().st_mtime)[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return f


def _state(d: Path) -> Dict[str, Any]:
    try:
        s = json.loads((d / "state.json").read_text(encoding="utf-8"))
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _lock(d: Path) -> bool:
    f = d / "lock"
    for _ in range(2):
        try:
            fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - f.stat().st_mtime < LOCK_STALE:
                    return False
                f.unlink()                       # a job that died holding it
            except OSError:
                return False
        except OSError:
            return False
    return False


def _unlock(d: Path) -> None:
    try:
        (d / "lock").unlink()
    except OSError:
        pass


def _gateway(root: Path, cfg: Dict[str, Any]) -> bool:
    """An LLM to ask: Claude Code's gateway, one set in the shell, or the `claude` CLI itself (Bedrock, Vertex or a
    subscription: whatever the person's Claude Code uses). Cheap: no LLM is asked here."""
    return narrate.configured(root, cfg.get("narrate", {}).get("settings", []))


# ---------------------------------------------------------------------------------------------------- the job

def ensure(root: Path, bp: dict, cfg: Dict[str, Any]) -> Optional[dict]:
    """The overlay to draw with (see the top of this module); starts the background job when it is missing or stale.
    Never waits for the LLM and never raises. Also reads what each part says about itself (names.learn), for the
    names drawn without any LLM."""
    try:
        names.learn(Path(root), bp)
    except Exception:  # noqa: BLE001 - only names
        pass
    try:
        if not enabled(cfg):
            return None
        root = Path(root)
        key = structure_key(bp)
        got = current(root, key)
        if got and got.get("key") == key:
            return got
        if len(_parts(bp)) >= int(_settings(cfg).get("min_parts", MIN_PARTS)):
            start(root, bp, cfg, key)
        return got
    except Exception:  # noqa: BLE001 - a drawing aid: never stop the draw
        return None


def start(root: Path, bp: dict, cfg: Dict[str, Any], key: str) -> bool:
    """Start the background job for `key` if no job runs, this structure was not asked about lately and none was
    asked about in the last few minutes, and an LLM is configured. True if it was started."""
    d = _dir(root)
    now = time.time()
    state = _state(d)
    asked = {k: t for k, t in (state.get("asked") or {}).items() if now - float(t) < RETRY}
    if key in asked or now - float(state.get("last", 0)) < MIN_GAP:
        return False
    if not _gateway(root, cfg):
        return False
    if not _lock(d):
        return False
    try:
        state = _state(d)                             # again, under the lock: another process may just have asked
        asked = {k: t for k, t in (state.get("asked") or {}).items() if now - float(t) < RETRY}
        if key in asked or now - float(state.get("last", 0)) < MIN_GAP:
            _unlock(d)
            return False
        asked[key] = now
        (d / "state.json").write_text(json.dumps({"asked": asked, "last": now}), encoding="utf-8")
        job = d / f"job-{key}.json"
        job.write_text(json.dumps({"root": str(root), "key": key, "summary": summary(bp, cfg),
                                   "settings": cfg.get("narrate", {}).get("settings", []),
                                   "code_cfg": {k: cfg.get(k) for k in ("code", "tests", "components")}},
                                  default=str), encoding="utf-8")
        _spawn(root, job)
        return True
    except Exception:  # noqa: BLE001
        _unlock(d)
        return False


def _spawn(root: Path, job: Path) -> None:
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                          "cwd": str(root), "close_fds": True}
    if os.name == "nt":
        # a hidden console, not none: what it runs (git) inherits it, instead of opening a window each time
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "system_diff.regroup", str(job)], **kw)


def ask(root: Path, s: Dict[str, Any], settings: List[str] = ()) -> Optional[Tuple[Dict[str, Any], str]]:
    """Ask the model; the validated overlay and who wrote it, or None."""
    got = narrate.complete(prompt(s), SYSTEM, root, {"narrate": {"settings": list(settings)}}, TIMEOUT, limit=200000)
    if not got:
        return None
    overlay = validate(got[0], s)
    return (overlay, got[1]) if overlay else None


def run(job: Path) -> int:
    """The background job: read the job, add sample paths, ask, keep a valid answer; always free the lock."""
    d = job.parent
    try:
        spec = json.loads(job.read_text(encoding="utf-8"))
        root = Path(spec["root"])
        s = spec["summary"]
        try:
            from .config import DEFAULTS, merge
            paths = sample_paths(root, merge(DEFAULTS, {k: v for k, v in spec.get("code_cfg", {}).items() if v}),
                                 [p["id"] for p in s["parts"]])
        except Exception:  # noqa: BLE001 - the paths only help
            paths = {}
        for p in s["parts"]:
            if paths.get(p["id"]):
                p["paths"] = paths[p["id"]]
        got = ask(root, s, spec.get("settings", []))
        if got:
            save(root, spec["key"], got[0], got[1])
        return 0 if got else 1
    finally:
        try:
            job.unlink()
        except OSError:
            pass
        _unlock(d)


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1])) if len(sys.argv) > 1 else 2)
