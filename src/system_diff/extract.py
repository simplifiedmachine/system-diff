"""The system's structure as one JSON-able dict (a "blueprint"), read from the code of one commit or directory.

Observed, not declared: the imports between parts, FastAPI-style routes, the API paths the front end calls, the settings
each part reads (which name the outside systems it talks to), the deployed containers (a compose file) and the tables the
migrations create. Declared in the config: what the parts are called, which column each belongs in, and which settings
mean which outside system. An outside system is drawn only when some deploy configuration sets one of its settings;
read by the code but configured nowhere, it is DORMANT: listed, not drawn.
"""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional

from .config import segment_rx


def glob_rx(pattern: str) -> re.Pattern:
    """* stays within one directory, ** crosses them, ? is one character."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def matches(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_rx(p).match(path) for p in patterns)


PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+\(?([^)#\n]+)", re.M)
PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]*,[ \t]*[\w.]+)*)", re.M)
TS_IMPORT = re.compile(r"""(?:from\s+|import\s*\(\s*|require\(\s*)["']([^"']+)["']""")
ROUTE = re.compile(r"""^[ \t]*@(?:router|app)\.(get|post|put|patch|delete)\(\s*[rfb]?["']([^"']*)["']""", re.M)
PREFIX = re.compile(r"""APIRouter\([^)]*prefix\s*=\s*["']([^"']+)["']""")
ENV_PY = re.compile(r"""(?:getenv|environ\.get|environ\.setdefault)\(\s*["']([A-Z][A-Z0-9_]+)["']|environ\[\s*["']([A-Z][A-Z0-9_]+)["']\s*\]""")
ENV_TS = re.compile(r"(?:import\.meta\.env|process\.env)\.([A-Z][A-Z0-9_]+)")
API_STR = re.compile(r"""["'`](/api/[^"'`\s]*)""")
CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)", re.I)
ADD_COLUMN = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([\w.\"]+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?(\w+)", re.I)
SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
CONFIG_KEY = re.compile(r"^\s*(?:-\s*)?\"?(?:export\s+)?([A-Z][A-Z0-9_]+)\s*[=:]", re.M)
TS_EXT = (".ts", ".tsx", ".js", ".jsx")


def py_targets(path: str, text: str, files: set, python_paths: Iterable[str] = ("",)) -> List[str]:
    """Files a Python file imports, among `files`; imports of anything else (the standard library,
    third-party packages) resolve to nothing and are ignored."""
    package = path.split("/")[:-1]

    def module_file(dotted: str) -> Optional[str]:
        stem = dotted.replace(".", "/")
        return next((c for base in python_paths for c in (base + stem + ".py", base + stem + "/__init__.py")
                     if c in files), None)

    def relative_file(dotted: str) -> Optional[str]:
        stem = dotted.replace(".", "/")
        return next((c for c in (stem + ".py", stem + "/__init__.py") if c in files), None)

    def nearest(dotted: str, find=module_file) -> Optional[str]:  # `import a.b.c` where only a/b.py exists
        parts = dotted.split(".")
        while parts:
            hit = find(".".join(parts))
            if hit:
                return hit
            parts.pop()
        return None

    found: List[Optional[str]] = []
    for dots, mod, names in PY_FROM.findall(text):
        base = package[: len(package) - (len(dots) - 1)] if dots else []
        stem = ".".join(base + ([mod] if mod else []))
        find = relative_file if dots else module_file
        modules = [find(f"{stem}.{n}") for n in
                   (x.strip().split(" as ")[0].strip() for x in names.split(",")) if n and n != "*"]
        modules = [m for m in modules if m]
        found += modules or [nearest(stem, find)]  # `from x import name`: x itself, unless the names are modules
    for mods in PY_IMPORT.findall(text):
        found += [nearest(m.strip()) for m in mods.split(",")]
    return [f for f in found if f]


def ts_targets(path: str, text: str, files: set, aliases: Optional[Dict[str, str]] = None) -> List[str]:
    base = path.rsplit("/", 1)[0]
    out = []
    for spec in TS_IMPORT.findall(text):
        alias = next((a for a in (aliases or {}) if spec.startswith(a)), None)
        if alias:
            stem = aliases[alias] + spec[len(alias):]
        elif spec.startswith("."):
            stem = os.path.normpath(os.path.join(base, spec)).replace(os.sep, "/")
        else:
            continue
        for cand in (stem, *(stem + e for e in TS_EXT), *(stem + "/index" + e for e in TS_EXT)):
            if cand in files:
                out.append(cand)
                break
    return out


def compose_services(text: str) -> Dict[str, dict]:
    """The services of a compose file: just enough YAML to see what changed in the deployment."""
    services: Dict[str, dict] = {}
    current, in_services, key = None, False, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^\S", line):
            in_services, current = line.startswith("services:"), None
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m:
            current = services.setdefault(m.group(1), {"image": "", "command": "", "ports": [], "replicas": ""})
            continue
        if current is None:
            continue
        m = re.match(r"^\s{4}(image|command|ports):\s*(.*)$", line)
        if m:
            key = m.group(1)
            if key != "ports" and m.group(2):
                current[key] = m.group(2).strip().strip("\"'")
            continue
        m = re.match(r"^\s+replicas:\s*(\S+)", line)
        if m:
            current["replicas"] = m.group(1)
            continue
        m = re.match(r"^\s{4,}-\s*\"?([^\"#]+?)\"?\s*$", line)
        if m and key == "ports":
            current["ports"].append(m.group(1))
        elif re.match(r"^\s{4}\w", line):
            key = None
    return services


class Code:
    """Which files are code, which are tests, and which part owns each: one answer for the map and the diff."""

    def __init__(self, cfg: Dict[str, Any]):
        c = cfg["code"]
        self.skip = segment_rx(c["skip"])
        self.roots = tuple(c["roots"])
        self.ext = tuple(c["extensions"])
        self.group = c["group"]
        self.strip = tuple(c["strip"] if c.get("strip") is not None else c["extensions"])
        self.tests = [glob_rx(p) for p in cfg.get("tests", [])]
        self.declared = [(d, [glob_rx(p) for p in d.get("paths", [])]) for d in cfg.get("components", [])]

    def is_code(self, path: str) -> bool:
        return path.endswith(self.ext) and path.startswith(self.roots) and not self.skip.search(path)

    def is_test(self, path: str) -> bool:
        return any(r.match(path) for r in self.tests)

    def part_of(self, path: str) -> str:
        """The declared part claiming `path`; else one named by its directory."""
        hit = next((d["id"] for d, rs in self.declared if any(r.match(path) for r in rs)), None)
        if hit is not None:
            return hit
        parts = path.split("/")
        name = "/".join(parts[: int(self.group.get(parts[0], self.group.get("default", 2)))])
        return next((name[: -len(s)] for s in self.strip if name.endswith(s)), name)


def extract(source, cfg: Dict[str, Any]) -> dict:
    """The blueprint of one source."""
    code_of = Code(cfg)
    comps = cfg.get("components", [])
    files = [f for f in source.files if not code_of.skip.search(f)]
    fileset = set(files)
    code = [f for f in files if code_of.is_code(f) and not code_of.is_test(f)]
    aliases, python_paths = cfg["code"]["aliases"], cfg["code"]["python_paths"]

    owner: Dict[str, str] = {}
    components: Dict[str, dict] = {}
    for f in code:
        cid = code_of.part_of(f)
        owner[f] = cid
        meta = next((c for c in comps if c["id"] == cid), None)
        entry = components.setdefault(cid, {
            "label": (meta or {}).get("label", cid), "layer": meta.get("layer", "") if meta else "undeclared",
            "kind": (meta or {}).get("kind", ""), "order": comps.index(meta) if meta else len(comps),
            "container": (meta or {}).get("container", ""), "declared": meta is not None, "files": 0, "lines": 0,
            "hub": bool((meta or {}).get("hub")), "quiet": bool((meta or {}).get("quiet"))})
        entry["files"] += 1

    texts = source.read_many(code)
    flows, api, env_by_comp, routes = Counter(), Counter(), defaultdict(Counter), {}
    for f in code:
        text, cid = texts[f], owner[f]
        components[cid]["lines"] += text.count("\n")
        python = f.endswith(".py")
        targets = py_targets(f, text, fileset, python_paths) if python else ts_targets(f, text, fileset, aliases)
        for t in targets:
            if t in owner and owner[t] != cid:
                flows[f"{cid}>{owner[t]}"] += 1
        if python:
            prefix = (PREFIX.search(text) or [None, ""])[1]
            for method, path in ROUTE.findall(text):
                routes[f"{method.upper()} {prefix}{path}"] = cid
            env_by_comp[cid].update(a or b for a, b in ENV_PY.findall(text))
        else:
            env_by_comp[cid].update(ENV_TS.findall(text))
            for s in API_STR.findall(text):
                target = next((a["component"] for a in cfg.get("api", []) if s.startswith(a["prefix"])), None)
                if target and target != cid:
                    api[f"{cid}>{target}"] += 1

    config_rx = [glob_rx(p) for p in cfg.get("config_files", [])]
    configured = set()
    for f in files:
        if any(r.match(f) for r in config_rx):
            configured |= set(CONFIG_KEY.findall(source.read(f)))
    externals, dormant = {}, {}
    for ext in cfg.get("externals", []):
        reads = {c: sum(n for v, n in vs.items() if matches(v, ext["env"])) for c, vs in env_by_comp.items()}
        users = sorted((c for c, n in reads.items() if n), key=lambda c: (-reads[c], c))  # heaviest reader first
        if not users:
            continue
        live = any(matches(v, ext["env"]) for v in configured)
        (externals if live else dormant)[ext["id"]] = {"label": ext["label"], "shape": ext.get("shape", ""), "used_by": users}
    internal = Counter()
    for var, container in cfg.get("internal_env", {}).items():
        for c, vs in env_by_comp.items():
            if var in vs:
                internal[f"{c}>{container}"] += 1

    compose = cfg.get("compose", "")
    containers = compose_services(source.read(compose)) if compose in fileset else {}

    tables, migrations = defaultdict(set), []
    mig = cfg.get("migrations", {})
    mig_files = sorted(f for f in files if mig and glob_rx(mig.get("glob", "")).match(f))
    mig_texts = source.read_many(mig_files)
    for f in mig_files:
        name = f.rsplit("/", 1)[1]
        migrations.append(name)
        num = int(re.match(r"\d+", name).group(0)) if re.match(r"\d+", name) else 0
        schema = next((s for lo, hi, s in mig.get("schemas", []) if lo <= num <= hi), "public")
        text = SQL_COMMENT.sub("", mig_texts[f])  # "CREATE TABLE IF NOT EXISTS only." in a comment is not a table
        for table in CREATE_TABLE.findall(text):
            tables[qualify(table, schema)] |= set()
        for table, col in ADD_COLUMN.findall(text):
            tables[qualify(table, schema)].add(col)

    test_files = Counter(("/".join(f.split("/")[:2]) if f.startswith("tests/") else f.split("/")[0])
                         for f in files if code_of.is_test(f) and f.endswith(code_of.ext))
    return {
        "ref": source.label,
        "components": components,
        "flows": dict(sorted(flows.items())),
        "api": dict(sorted(api.items())),
        "routes": dict(sorted(routes.items())),
        "externals": externals,
        "dormant": dormant,
        "internal": dict(sorted(internal.items())),
        "env": {c: sorted(v) for c, v in sorted(env_by_comp.items()) if v},
        "containers": containers,
        "container_labels": cfg.get("containers", {}),
        "tables": {t: sorted(c) for t, c in sorted(tables.items())},
        "store": mig.get("store", "postgres"),   # the outside system the migrations' tables live in
        "migrations": migrations,
        "tests": dict(sorted(test_files.items())),
    }


def qualify(name: str, schema: str) -> str:
    name = name.replace('"', "")
    return name if "." in name else f"{schema}.{name}"
