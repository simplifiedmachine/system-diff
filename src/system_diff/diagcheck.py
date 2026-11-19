"""What could be wrong in a diagram, checked against the code (Claude's ui_check), and a report of what configuration
cannot fix (report_diagram_issue).

The diagrams are computed from the code and system-diff.toml. When one looks wrong, the cause is most often one of a
few: an import that reaches no file (a path alias or a source folder the config does not know), code counted in no
part (outside the code roots, or taken for tooling), a stale config entry, or a read still under way. `check` finds
those from what is already known: the working tree's blueprint (the engine's, or the one kept on disk) and each
file's facts by content (extract's FactStore); a file is read again only when nothing was ever read of its content.
Each finding says what, where (files and lines), why, and the fix: a TOML snippet for ui_config_preview when the
config explains it, else what system-diff does not understand. Nothing here writes, except `report`, which keeps the
issue under .git/system-diff/diagram-issues.jsonl.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ISSUES = "diagram-issues.jsonl"
EXAMPLES = 3          # files (with lines) shown per finding
MOST = 40             # findings at most
ALIAS_LIKE = re.compile(r"^([~#@$][\w-]*/|@/|~/|#/)")   # @/x, ~/x, #/x, $lib/x: a path alias, not a package
TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")


def _toml(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _snippet_aliases(aliases: Dict[str, str]) -> str:
    return "[code]\naliases = { " + ", ".join(f"{_toml(k)} = {_toml(v)}" for k, v in sorted(aliases.items())) + " }"


def _held(root: Path):
    """(blueprint, cfg, files, blobs, source, code) of the working tree: the engine's when it runs in this process,
    else the blueprint of a snapshot (kept on disk by the hooks and the pane, else made from the kept facts)."""
    from . import compare, config
    from .extract import Code
    held = compare.live(root)
    if held is not None and not getattr(config._PREVIEW, "over", None):   # a preview reads with the edit laid on
        src = held.source
        return dict(held.blueprint), held.cfg, list(src.files), dict(getattr(src, "blobs", {}) or {}), src, Code(held.cfg)
    from .sources import GitSource
    sha, _label = compare.resolve(root, ".")
    src = GitSource(sha, root)
    cfg = config.load(src, root)
    bp, _known = compare._read(sha, cfg, src, keep_in=root)
    return bp, cfg, list(src.files), dict(src.blobs), src, Code(cfg)


def _facts(root: Path, src: Any, blobs: Dict[str, str], paths: Iterable[str]) -> Dict[str, dict]:
    """Each code file's facts by content (kept); read only those never read (a store missing or pruned)."""
    from .extract import code_facts, fact_store
    store = fact_store(root)
    out: Dict[str, dict] = {}
    missing = []
    for p in paths:
        got = store.get(blobs[p], p) if store is not None and blobs.get(p) else None
        if got is not None and "code" in got:
            out[p] = got["code"]
        else:
            missing.append(p)
    if missing:
        texts = src.read_many(missing)
        for p in missing:
            out[p] = code_facts(p, texts.get(p, ""))
    return out


def _lines(root: Path, path: str, needle: str, most: int = 2) -> List[int]:
    """The line numbers of `needle` in a file as it is on disk (only the few files a finding shows)."""
    try:
        text = (Path(root) / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [i for i, line in enumerate(text.split("\n"), 1) if needle in line][:most]


def _where(root: Path, hits: List[Tuple[str, str]]) -> List[Dict[str, Any]]:
    out = []
    for path, needle in hits[:EXAMPLES]:
        out.append({"path": path, "lines": _lines(root, path, needle) if needle else []})
    return out


def _stems(files: Iterable[str]) -> Dict[str, List[str]]:
    """A JavaScript or TypeScript module's import path (no extension, a folder's index as the folder) -> its files."""
    out: Dict[str, List[str]] = defaultdict(list)
    for f in files:
        if f.endswith(TS_EXT):
            stem = f.rsplit(".", 1)[0]
            out[stem].append(f)
            if stem.endswith("/index"):
                out[stem[: -len("/index")]].append(f)
    return out


def _folder_for(rest: str, stems: Dict[str, List[str]], near: str) -> Optional[str]:
    """The folder D such that D + rest is a module of the repository, the one nearest the importing file."""
    hits = [s[: len(s) - len(rest)] for s in stems if s == rest or s.endswith("/" + rest)]
    if not hits:
        return None
    return max(hits, key=lambda d: (len(os.path.commonprefix([d, near])), -len(d)))


def _ts_findings(root: Path, cfg: Dict[str, Any], code: List[str], facts: Dict[str, dict], files: List[str],
                 fileset: set, found_prefixes: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    from .extract import _ts_resolve
    stems = _stems(files)
    aliases_cfg = cfg["code"].get("aliases") or {}

    def aliases_of(f: str) -> Dict[str, str]:
        folder = max((d for d in found_prefixes if not d or f.startswith(d + "/")), key=len, default=None)
        return {**(found_prefixes.get(folder) or {}), **aliases_cfg} if folder is not None else aliases_cfg

    missing_alias: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
    dead_alias: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    dead_relative: List[Tuple[str, str]] = []
    for f in code:
        if f.endswith(".py"):
            continue
        al = aliases_of(f)
        for spec, _names in facts.get(f, {}).get("imports") or []:
            if _ts_resolve(f, spec, fileset, al):
                continue
            hit_alias = next((a for a in al if spec.startswith(a)), None)
            if hit_alias:
                dead_alias[hit_alias].append((f, spec))
                continue
            if spec.startswith("."):
                if not spec.endswith((".css", ".scss", ".svg", ".png", ".json", ".sass", ".less")):
                    dead_relative.append((f, spec))
                continue
            m = ALIAS_LIKE.match(spec)
            if m:
                prefix, rest = m.group(1), spec[len(m.group(1)):]
            elif "/" in spec and not spec.startswith("@"):   # lib/api from a baseUrl: the repository's, if it is there
                at = _folder_for(spec, stems, f)
                if at is None:
                    continue          # a package's own path (lodash/fp): not the repository's
                prefix = spec.split("/", 1)[0] + "/"
                missing_alias[(prefix, at + prefix)].append((f, spec))
                continue
            else:
                continue
            folder = _folder_for(rest, stems, f)
            if folder is not None:
                missing_alias[(prefix, folder)].append((f, spec))
    out = []
    for (prefix, folder), hits in sorted(missing_alias.items(), key=lambda kv: -len(kv[1])):
        out.append({"what": f"{len(hits)} import(s) written with {prefix!s}… reach no part",
                    "kind": "unresolved-import", "where": _where(root, hits),
                    "why": f"`{hits[0][1]}` matches {folder}{hits[0][1][len(prefix):] if hits[0][1].startswith(prefix) else ''} "
                           f"in the repository, but {prefix} is not a known path alias (system-diff.toml's aliases, "
                           "or a tsconfig.json/jsconfig.json paths entry), so it was counted as an outside package and "
                           "the connection is missing",
                    "fix": _snippet_aliases({**(cfg["code"].get("aliases") or {}), prefix: folder}),
                    "files": [h[0] for h in hits]})
    for prefix, hits in sorted(dead_alias.items()):
        target = (cfg["code"].get("aliases") or {}).get(prefix, "")
        out.append({"what": f"{len(hits)} import(s) through the alias {prefix} reach no file",
                    "kind": "unresolved-import", "where": _where(root, hits),
                    "why": f"{prefix} maps to {target or 'a tsconfig folder'!s}, where `{hits[0][1][len(prefix):]}` "
                           "is not a file",
                    "fix": ("change the alias's folder in [code] aliases (preview it with ui_config_preview)"
                            if target else "system-diff does not understand this tsconfig paths entry"),
                    "files": [h[0] for h in hits]})
    if dead_relative:
        out.append({"what": f"{len(dead_relative)} relative import(s) reach no file", "kind": "unresolved-import",
                    "where": _where(root, dead_relative),
                    "why": f"`{dead_relative[0][1]}` from {dead_relative[0][0]} names no file of the repository "
                           "(a file not committed or skipped, or an extension system-diff does not read)",
                    "fix": "system-diff does not understand these imports: check that the files exist and are not "
                           "skipped ([code] skip)", "files": [h[0] for h in dead_relative]})
    return out


def _py_findings(root: Path, cfg: Dict[str, Any], code: List[str], facts: Dict[str, dict], files: List[str],
                 fileset: set) -> List[Dict[str, Any]]:
    from .extract import _py_reached, first_party, import_paths
    python_paths = import_paths(files, cfg["code"]["python_paths"])
    own = first_party(files, cfg["code"]["python_paths"])
    by_folder: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    dead_own: List[Tuple[str, str]] = []
    dead_relative: List[Tuple[str, str]] = []
    py_files = [f for f in files if f.endswith(".py")]
    for f in code:
        if not f.endswith(".py"):
            continue
        for entry in facts.get(f, {}).get("imports") or []:
            if _py_reached(f, [entry], fileset, python_paths):
                continue
            if entry[0] == "from":
                _, dots, mod, _listed = entry
                if dots:
                    dead_relative.append((f, "from " + "." * dots + (mod or "")))
                    continue
                names = [mod] if mod else []
            else:
                names = list(entry[1])
            for dotted in names:
                top = dotted.split(".")[0]
                stem = dotted.replace(".", "/")
                if top in own:
                    dead_own.append((f, dotted))
                    continue
                hit = next((p[: -len(c)] for p in py_files for c in (stem + ".py", stem + "/__init__.py")
                            if p.endswith("/" + c)), None)
                if hit is not None and hit not in python_paths:
                    by_folder[hit].append((f, dotted))
    out = []
    for folder, hits in sorted(by_folder.items(), key=lambda kv: -len(kv[1])):
        paths = list(cfg["code"]["python_paths"]) + [folder]
        out.append({"what": f"{len(hits)} Python import(s) of {hits[0][1].split('.')[0]} reach no part",
                    "kind": "unresolved-import", "where": _where(root, hits),
                    "why": f"`import {hits[0][1]}` matches {folder}{hits[0][1].replace('.', '/')} in the repository, "
                           f"but {folder} is not a place imports are looked up (a folder without __init__.py packages "
                           "is not found by itself), so it was counted as an outside package",
                    "fix": f"[code]\npython_paths = {_toml(paths)}", "files": [h[0] for h in hits]})
    if dead_own:
        out.append({"what": f"{len(dead_own)} import(s) of the repository's own modules reach no file",
                    "kind": "unresolved-import", "where": _where(root, dead_own),
                    "why": f"`{dead_own[0][1]}` starts with a name of this repository but no file matches it "
                           "(a module made at run time, a file not committed, or a layout system-diff does not follow)",
                    "fix": "system-diff does not understand where these modules are: say so with "
                           "report_diagram_issue if the files exist", "files": [h[0] for h in dead_own]})
    if dead_relative:
        out.append({"what": f"{len(dead_relative)} relative Python import(s) reach no file",
                    "kind": "unresolved-import", "where": _where(root, [(f, s.split()[-1]) for f, s in dead_relative]),
                    "why": f"`{dead_relative[0][1]}` in {dead_relative[0][0]} names no file of the repository",
                    "fix": "system-diff does not understand these imports: check the files exist and are not skipped",
                    "files": [h[0] for h in dead_relative]})
    return out


def _unowned(cfg: Dict[str, Any], code_of: Any, files: List[str]) -> List[Dict[str, Any]]:
    """Code files counted in no part: outside the code roots, or taken for tooling."""
    from .extract import glob_rx
    tooling = [glob_rx(p) for p in cfg["code"].get("tooling") or ()]
    outside: Dict[str, List[str]] = defaultdict(list)
    tooled: List[str] = []
    for f in files:
        if not f.endswith(code_of.ext) or code_of.skip.search(f) or code_of.is_test(f) or code_of.is_code(f):
            continue
        if not f.startswith(code_of.roots):
            outside[f.split("/")[0] if "/" in f else f].append(f)
        elif any(r.match(f) for r in tooling):
            tooled.append(f)
    out = []
    for top, fs in sorted(outside.items(), key=lambda kv: -len(kv[1])):
        roots = [r for r in cfg["code"]["roots"]] + [top + "/" if "/" in fs[0] else fs[0]]
        out.append({"what": f"{len(fs)} code file(s) in {top} are in no part", "kind": "unowned-code",
                    "where": [{"path": f, "lines": []} for f in fs[:EXAMPLES]],
                    "why": f"{top} is not under the code roots ({', '.join(cfg['code']['roots']) or 'none'}), so its "
                           "files are not drawn", "fix": f"[code]\nroots = {_toml(roots)}", "files": fs})
    if tooled:
        out.append({"what": f"{len(tooled)} code file(s) are taken for build or release tooling and are in no part",
                    "kind": "unowned-code", "where": [{"path": f, "lines": []} for f in tooled[:EXAMPLES]],
                    "why": "they match [code] tooling (config files, scripts/, migrations, type declarations)",
                    "fix": "if they belong to the system, declare a part that names them:\n[[components]]\n"
                           f"id = \"tools\"\nlabel = \"Tools\"\npaths = {_toml(sorted({t.rsplit('/', 1)[0] + '/**' if '/' in t else t for t in tooled})[:4])}",
                    "files": tooled})
    return out


def _isolated(bp: Dict[str, Any]) -> List[Dict[str, Any]]:
    comps = bp.get("components") or {}
    if len(comps) < 2:
        return []
    linked = set()
    for k in ("flows", "api", "internal"):
        for key in bp.get(k) or {}:
            linked.update(key.split(">"))
    for e in (bp.get("externals") or {}).values():
        linked.update(e.get("used_by") or [])
    linked.update((bp.get("routes") or {}).values())
    out = []
    for cid, c in sorted(comps.items()):
        if cid in linked or c.get("quiet"):
            continue
        out.append({"what": f"the part {c.get('label') or cid} ({cid}) has no connections", "kind": "isolated-part",
                    "where": [], "part": cid,
                    "why": "no import from or to another part, no route, no outside system was found for it: it is "
                           "either on its own, or reached in a way system-diff does not follow (an unresolved import "
                           "above, dynamic imports, a framework's registry)",
                    "fix": "if it is reached in a way system-diff does not follow, report_diagram_issue; "
                           "quiet = true on its [[components]] entry says it is on its own on purpose"})
    return out


def _outside_by_settings(bp: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    declared = {e["id"]: e for e in cfg.get("externals") or []}
    for xid, e in sorted((bp.get("externals") or {}).items()):
        if xid in declared:
            env = declared[xid].get("env") or []
            out.append({"what": f"{e.get('label') or xid} is drawn from setting names only", "kind": "settings-only",
                        "where": [], "part": xid,
                        "why": f"its [[externals]] entry finds it by the settings {', '.join(env)}, read by "
                               f"{', '.join(e.get('used_by') or [])}: a part reading a setting of that name is drawn "
                               "as using it, whatever the setting holds",
                        "fix": "if a part is wrongly connected to it, narrow env on its [[externals]] entry"})
    for xid, e in sorted((bp.get("dormant") or {}).items()):
        out.append({"what": f"{e.get('label') or xid} is not drawn: its settings are read but set nowhere",
                    "kind": "settings-only", "where": [], "part": xid,
                    "why": "a declared outside system is drawn only when its settings are configured (compose, an "
                           "example env file or a config file listed in config_files)",
                    "fix": "config_files = [...] naming the file that sets them, or set them in an example env file"})
    return out


def _stale(cfg: Dict[str, Any], files: List[str], bp: Dict[str, Any]) -> List[Dict[str, Any]]:
    from .config import DEFAULTS
    from .extract import glob_rx, segment_rx
    out = []
    code = cfg["code"]

    def stale(what: str, why: str, fix: str) -> None:
        out.append({"what": what, "kind": "stale-config", "where": [{"path": "system-diff.toml", "lines": []}],
                    "why": why, "fix": fix})
    for r in code.get("roots") or []:
        if r and not any(f.startswith(r) for f in files):
            stale(f"the code root {r!r} holds no file", "no file of the repository is under it",
                  f"[code]\nroots = {_toml([x for x in code['roots'] if x != r] or [''])}")
    for prefix, folder in sorted((code.get("aliases") or {}).items()):
        if folder and not any(f.startswith(folder) for f in files):
            stale(f"the alias {prefix} points at {folder}, which holds no file", "no file is under that folder",
                  _snippet_aliases({k: v for k, v in code["aliases"].items() if k != prefix}) + "  # without it")
    for name in code.get("skip") or []:
        if name in DEFAULTS["code"]["skip"]:
            continue
        if not any(segment_rx([name]).search(f) for f in files):
            stale(f"the skip entry {name!r} matches nothing", "no folder of the repository is named like it",
                  "remove it from [code] skip")
    for c in cfg.get("components") or []:
        rx = [glob_rx(p) for p in c.get("paths") or []]
        if rx and not any(any(r.match(f) for r in rx) for f in files):
            stale(f"the part {c['id']!r} names paths that match no file ({', '.join(c.get('paths') or [])})",
                  "its box is never drawn", f"fix its paths on the [[components]] entry with id = {_toml(c['id'])}")
    read = {v for vs in (bp.get("env") or {}).values() for v in vs}
    from .extract import matches
    for e in cfg.get("externals") or []:
        if e.get("env") and not any(matches(v, e["env"]) for v in read):
            stale(f"the outside system {e['id']!r} is found by settings no code reads ({', '.join(e['env'])})",
                  "it is never drawn", f"fix env on the [[externals]] entry with id = {_toml(e['id'])}")
    ids = set(bp.get("components") or {})
    for a in cfg.get("api") or []:
        if a.get("component") and a["component"] not in ids:
            stale(f"[[api]] {a.get('prefix')} names the part {a['component']!r}, which is not drawn",
                  "calls to that prefix connect to nothing", "fix component on that [[api]] entry")
    return out


def _reading(root: Path, bp: Dict[str, Any]) -> List[Dict[str, Any]]:
    got = bp.get("partial")
    if not got:
        try:
            from .uitools import engine_get
            prog = engine_get(root, "progress", timeout=2) or {}
        except Exception:  # noqa: BLE001
            prog = {}
        if prog and prog.get("ready") is False:
            got = {"done": prog.get("done") or 0, "total": prog.get("total") or 0}
    if not got:
        return []
    return [{"what": f"the code is still being read ({int(got.get('done') or 0):,} of {int(got.get('total') or 0):,} "
                     "files)", "kind": "reading", "where": [],
             "why": "connections of the files not read yet are missing for now", "fix": "wait for the read to finish"}]


def _in_part(f: Dict[str, Any], part: str, owner: Dict[str, str]) -> bool:
    if f.get("part") == part:
        return True
    return any(owner.get(p) == part or p.startswith(part.rstrip("/") + "/") for p in f.get("files") or [])


def check(root: Path, part: str = "", view: str = "now") -> Dict[str, Any]:
    """What could be wrong in the diagram of `root` (one `part`'s, if given), most important first."""
    t0 = time.monotonic()
    root = Path(root)
    bp, cfg, files, blobs, src, code_of = _held(root)
    files = [f for f in files if not code_of.skip.search(f)]
    fileset = set(files)
    code = [f for f in files if code_of.is_code(f) and not code_of.is_test(f)]
    owner = {f: code_of.part_of(f) for f in code}
    facts = _facts(root, src, blobs, code)
    found_prefixes: Dict[str, Dict[str, str]] = {}
    from .extract import TSCONFIG, fact_store, path_prefixes
    configs = [f for f in files if TSCONFIG.search(f)]
    if configs:
        store = fact_store(root)
        missing = []
        for f in configs:
            got = store.get(blobs[f], f) if store is not None and blobs.get(f) else None
            if got is not None and "tsconfig" in got:
                for folder, prefixes in got["tsconfig"].items():
                    for k, w in prefixes.items():
                        found_prefixes.setdefault(folder, {}).setdefault(k, w)
            else:
                missing.append(f)
        if missing:
            texts = src.read_many(missing)
            for folder, prefixes in path_prefixes(missing, lambda p: texts.get(p, "")).items():
                for k, w in prefixes.items():
                    found_prefixes.setdefault(folder, {}).setdefault(k, w)
    findings = (_reading(root, bp) + _ts_findings(root, cfg, code, facts, files, fileset, found_prefixes)
                + _py_findings(root, cfg, code, facts, files, fileset) + _unowned(cfg, code_of, files)
                + _stale(cfg, files, bp) + _isolated(bp) + _outside_by_settings(bp, cfg))
    if part:
        pid = part[2:] if part.startswith("b:") else part
        findings = [f for f in findings if f["kind"] in ("reading", "stale-config") or _in_part(f, pid, owner)]
    for f in findings:
        fs = f.pop("files", None)
        if fs and len(fs) > EXAMPLES:
            f["more_files"] = len(fs) - EXAMPLES
    took = round(time.monotonic() - t0, 2)
    return {"project": str(root), "part": part, "view": view, "findings": findings[:MOST],
            "more": max(0, len(findings) - MOST),
            "checked": {"code_files": len(code), "parts": len(bp.get("components") or {}), "seconds": took},
            "next": ("for a finding with a TOML fix: ui_config_preview(changes=fix) shows the diagram it gives, then "
                     "edit system-diff.toml; for one system-diff does not understand: report_diagram_issue. Nothing "
                     "found: compare the box with its files (ui_source) and the code before saying the diagram is right")}


# ── what configuration cannot fix ───────────────────────────────────────────────────────────────────────────────

EVIDENCE = re.compile(r"([\w./@~+-]+\.[A-Za-z0-9]+)(?::(\d+)(?:-(\d+))?)?")


def _excerpt(root: Path, path: str, first: int, last: int) -> Optional[Dict[str, Any]]:
    """A few lines of a code file (or system-diff.toml), secrets masked; None for anything else."""
    from .config import lenient
    from .extract import Code
    from .fileview import mask_secrets
    if ".." in path.split("/") or path.startswith("/") or ":" in path:
        return None
    code = Code(lenient(root))
    if path != "system-diff.toml" and not (path.endswith(code.ext) and not code.skip.search(path)):
        return None
    p = Path(root) / path
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.split("\n")
    first = max(1, first)
    last = min(len(lines), max(first, last), first + 11)
    return {"path": path, "from": first,
            "text": mask_secrets("\n".join(lines[first - 1:last]))}


def report(root: Path, part: str, wrong: str, expected: str, evidence: str = "") -> Dict[str, Any]:
    """Keep a diagram issue configuration cannot fix, and a report ready to paste to system-diff's maintainers."""
    from . import __version__
    from .fileview import mask_secrets
    from .sources import git_dir
    root = Path(root)
    excerpts = []
    seen = set()
    for m in EVIDENCE.finditer(evidence or ""):
        path, a, b = m.group(1), m.group(2), m.group(3)
        if (path, a) in seen or len(excerpts) >= 5:
            continue
        seen.add((path, a))
        start = int(a) if a else 1
        got = _excerpt(root, path, max(1, start - (1 if a else 0)), int(b) + 1 if b else (start + 1 if a else 6))
        if got is not None:
            excerpts.append(got)
    files: List[str] = []
    if part:
        try:
            from .uitools import source
            files = [f["path"] for f in (source(root, part).get("files") or [])][:10]
        except Exception:  # noqa: BLE001
            files = []
    rec = {"at": round(time.time(), 3), "version": __version__, "part": part,
           "wrong": mask_secrets(str(wrong or ""))[:2000], "expected": mask_secrets(str(expected or ""))[:2000],
           "evidence": mask_secrets(str(evidence or ""))[:2000], "files": files, "excerpts": excerpts}
    where = git_dir(root) / ISSUES
    try:
        where.parent.mkdir(parents=True, exist_ok=True)
        with open(where, "a", encoding="utf-8") as out:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        kept = str(where)
    except OSError as exc:
        kept = f"not kept ({exc})"
    lines = [f"### system-diff diagram issue (system-diff {__version__})", "",
             f"**Part:** {part or '(the whole diagram)'}", f"**Drawn:** {rec['wrong']}",
             f"**Expected:** {rec['expected']}"]
    if files:
        lines += ["", "**Files of the part:** " + ", ".join(files)]
    if rec["evidence"]:
        lines += ["", f"**Evidence:** {rec['evidence']}"]
    for x in excerpts:
        lines += ["", f"`{x['path']}` from line {x['from']}:", "```", x["text"], "```"]
    return {"kept": kept, "report": "\n".join(lines),
            "next": "show the person this report: they can paste it to system-diff's maintainers (only the lines "
                    "above leave the machine, with secrets masked)"}
