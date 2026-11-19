"""The web page's code viewer, made skimmable: a file's outline (one chunk per top-level class and function, methods
nested: its signature, its docstring's first sentence, its routes, the settings it reads, the parts and outside
systems it calls, its lines) from a cheap parse of the file (Python's ast; a light scan of TypeScript/JavaScript),
and a plain-words description of each chunk written by the person's own LLM (narrate.complete), made in the
background when a file is opened and kept by the file's blob id, so each version of a file is summarised once.

The code goes to the LLM the person already uses (their Claude Code gateway, a shell setting, or the `claude` CLI),
with literal secret values masked first; never with SYSTEM_DIFF_LLM=off.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import posixpath
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SUMMARY_DIR = ("explain", "files")
SUMMARY_MAX = 280        # characters of one chunk's description
FILE_SUMMARY_MAX = 400   # characters of the file's
SEND_MAX = 60_000        # characters of code sent to the LLM (the rest is cut, and the prompt says so)
RETRY_AFTER = 600.0      # seconds before a failed summary is tried again
ASK: Optional[Callable[..., Any]] = None   # tests put a stand-in here: (user, system, root, cfg) -> (text, by)

OUTSIDE = {   # imported modules that reach outside the process, by what they reach
    "requests": "HTTP", "httpx": "HTTP", "aiohttp": "HTTP", "urllib": "HTTP", "http": "HTTP", "axios": "HTTP",
    "boto3": "AWS", "aioboto3": "AWS", "botocore": "AWS", "sqlalchemy": "database", "psycopg": "database",
    "psycopg2": "database", "asyncpg": "database", "sqlite3": "database", "pymongo": "MongoDB", "redis": "Redis",
    "smtplib": "mail", "subprocess": "other programs", "socket": "the network", "openai": "OpenAI",
    "anthropic": "Anthropic", "kafka": "Kafka", "pika": "RabbitMQ", "child_process": "other programs",
    "pg": "database", "mysql": "database", "mysql2": "database", "ioredis": "Redis",
}
SECRET_NAME = re.compile(r"(?i)(?:secret|token|passw(?:or)?d|pwd|api[_-]?key|private[_-]?key|access[_-]?key|"
                         r"client[_-]?secret|auth)")


def blob_id(data: bytes) -> str:
    """git's blob id of these bytes: the content address the facts and the summaries are kept by."""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def _first_sentence(doc: Optional[str]) -> str:
    text = " ".join((doc or "").strip().split())
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    return (m.group(1) if m else text)[:200]


def _settings(src: str) -> List[str]:
    from .extract import ENV_PY, ENV_TS
    out = [a or b for a, b in ENV_PY.findall(src)] + ENV_TS.findall(src)
    return list(dict.fromkeys(out))


# ── Python ────────────────────────────────────────────────────────────────────────────────────────────────────

def _py_routes(decorators: List[ast.expr]) -> List[str]:
    out = []
    for d in decorators:
        if not isinstance(d, ast.Call) or not isinstance(d.func, ast.Attribute):
            continue
        verb = d.func.attr.lower()
        arg = d.args[0] if d.args else None
        path = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
        if path is None:
            continue
        if verb in ("get", "post", "put", "patch", "delete", "head", "options", "websocket"):
            out.append(f"{verb.upper()} {path or '/'}")
        elif verb in ("route", "api_route"):
            methods = ["GET"]
            for kw in d.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [e.value.upper() for e in kw.value.elts if isinstance(e, ast.Constant)
                               and isinstance(e.value, str)] or methods
            out += [f"{m} {path}" for m in methods]
    return out


def _py_imports(tree: ast.Module) -> Dict[str, tuple]:
    """{local name: (module, level, name)}: what each imported name of the file is."""
    out: Dict[str, tuple] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out[(a.asname or a.name).split(".")[0]] = (a.name if a.asname else a.name.split(".")[0], 0, "")
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out[a.asname or a.name] = (node.module or "", node.level or 0, a.name)
    return out


def _py_module_files(path: str, module: str, level: int, name: str) -> List[str]:
    """Where an import may be in the repository, nearest first."""
    if level:
        base = posixpath.dirname(path)
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        mods = [posixpath.join(base, *module.split(".")) if module else base]
    else:
        mods = ["/".join(module.split("."))]
        if posixpath.dirname(path).startswith("src/"):
            mods.append("src/" + mods[0])
    out = []
    for m in mods:
        if name:
            out += [f"{m}/{name}.py", f"{m}/{name}/__init__.py"]
        out += [f"{m}.py", f"{m}/__init__.py"]
    return [o.lstrip("/") for o in out]


def _signature(node: ast.AST) -> str:
    try:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            return f"class {node.name}" + (f"({bases})" if bases else "")
        args = ast.unparse(node.args)
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        lead = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{lead} {node.name}({args}){ret}"[:240]
    except Exception:  # noqa: BLE001
        return getattr(node, "name", "")


def _py_outline(path: str, text: str, where: Callable[[tuple], Optional[str]]) -> List[Dict[str, Any]]:
    tree = ast.parse(text)
    lines = text.splitlines()
    imports = _py_imports(tree)
    chunks: List[Dict[str, Any]] = []

    def uses(node: ast.AST) -> List[str]:
        names = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                names.add(n.id)
        out = []
        for n in sorted(names):
            if n in imports:
                got = where(imports[n])
                if got and got not in out:
                    out.append(got)
        return out

    def add(node: ast.AST, depth: int, parent: str) -> None:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        src = "\n".join(lines[start - 1:end])
        qual = f"{parent}.{node.name}" if parent else node.name
        kind = "class" if isinstance(node, ast.ClassDef) else ("method" if parent else "function")
        decorators = []
        for d in node.decorator_list:
            try:
                decorators.append("@" + ast.unparse(d)[:80])
            except Exception:  # noqa: BLE001
                pass
        chunks.append({"name": node.name, "qual": qual, "kind": kind, "depth": depth, "line": start, "end": end,
                       "sig": _signature(node), "doc": _first_sentence(ast.get_docstring(node)),
                       "decorators": decorators, "routes": _py_routes(node.decorator_list),
                       "settings": _settings(src), "calls": uses(node), "size": end - start + 1})
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                add(child, depth + 1, qual)
    for node in tree.body:
        add(node, 0, "")
    return chunks


# ── TypeScript / JavaScript: a light scan ────────────────────────────────────────────────────────────────────

TS_IMPORT = re.compile(r"""^\s*import\s+(?:type\s+)?(.+?)\s+from\s+["']([^"']+)["']""", re.M)
TS_FUNC = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*(?:<[^>]*>)?\s*\(")
TS_CLASS = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)")
TS_ARROW = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s+)?"
                      r"(?:function\b|(?:<[^>]*>)?\([^)]*\)\s*(?::\s*[^=]+)?=>|\w+\s*=>)")
TS_METHOD = re.compile(r"^\s+(?:public\s+|private\s+|protected\s+|static\s+|readonly\s+|override\s+)*(?:async\s+)?"
                       r"(?:get\s+|set\s+)?\*?(\w+)\s*(?:<[^>]*>)?\s*\([^;]*$")
TS_ROUTE = re.compile(r"""\b\w+\.(get|post|put|patch|delete|all)\(\s*["'`](/[^"'`]*)["'`]""")
NOT_METHODS = {"if", "for", "while", "switch", "catch", "function", "return", "constructor_", "else", "with"}
TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _strip_ts(line: str) -> str:
    """A line without its strings and // comment, to count braces in."""
    line = re.sub(r"""(["'`])(?:\\.|(?!\1).)*\1""", '""', line)
    return line.split("//", 1)[0]


def _ts_end(lines: List[str], i: int) -> int:
    """The line (0-based) where the block opening at or after line i closes."""
    depth, opened = 0, False
    for j in range(i, len(lines)):
        s = _strip_ts(lines[j])
        for ch in s:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            return j
        if not opened and j > i and s.rstrip().endswith(";"):
            return j
        if not opened and j - i > 40:
            return i
    return len(lines) - 1


def _ts_doc(lines: List[str], i: int) -> str:
    j = i - 1
    while j >= 0 and lines[j].strip().startswith("@"):
        j -= 1
    if j >= 0 and lines[j].strip().endswith("*/"):
        k = j
        while k >= 0 and "/**" not in lines[k] and "/*" not in lines[k]:
            k -= 1
        body = " ".join(re.sub(r"^\s*(/\*\*?|\*/?|\*)\s?", "", ln).replace("*/", "") for ln in lines[k:j + 1])
        return _first_sentence(re.sub(r"@\w+.*", "", body))
    said = []
    while j >= 0 and lines[j].strip().startswith("//"):
        said.insert(0, lines[j].strip()[2:].strip())
        j -= 1
    return _first_sentence(" ".join(said))


def _ts_outline(path: str, text: str, where: Callable[[tuple], Optional[str]]) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    imports: Dict[str, tuple] = {}
    for m in TS_IMPORT.finditer(text):
        for name in re.findall(r"\b(\w+)\b", re.sub(r"\bas\b\s+", "", m.group(1)).replace("type ", "")):
            imports[name] = (m.group(2), -1, name)
    chunks: List[Dict[str, Any]] = []

    def chunk(name: str, kind: str, depth: int, parent: str, i: int, end: int) -> Dict[str, Any]:
        src = "\n".join(lines[i:end + 1])
        used = set(re.findall(r"\b(\w+)\b", src))
        calls = []
        for n in sorted(used & set(imports)):
            got = where(imports[n])
            if got and got not in calls:
                calls.append(got)
        return {"name": name, "qual": f"{parent}.{name}" if parent else name, "kind": kind, "depth": depth,
                "line": i + 1, "end": end + 1, "sig": lines[i].strip().rstrip("{").strip()[:240],
                "doc": _ts_doc(lines, i), "decorators": [],
                "routes": [f"{v.upper() if v != 'all' else 'ANY'} {p}" for v, p in TS_ROUTE.findall(src)],
                "settings": _settings(src), "calls": calls, "size": end - i + 1}
    i = 0
    while i < len(lines):
        line = lines[i]
        m = TS_CLASS.match(line)
        if m:
            end = _ts_end(lines, i)
            chunks.append(chunk(m.group(1), "class", 0, "", i, end))
            j = i + 1
            while j < end:
                mm = TS_METHOD.match(lines[j])
                if mm and mm.group(1) not in NOT_METHODS and lines[j].startswith((" ", "\t")):
                    e = _ts_end(lines, j)
                    if e > j or "{" in lines[j]:
                        chunks.append(chunk(mm.group(1), "method", 1, m.group(1), j, max(e, j)))
                        j = max(e, j) + 1
                        continue
                j += 1
            i = end + 1
            continue
        m = TS_FUNC.match(line) or TS_ARROW.match(line)
        if m:
            end = _ts_end(lines, i)
            chunks.append(chunk(m.group(1), "function", 0, "", i, end))
            i = end + 1
            continue
        i += 1
    return chunks


# ── the outline, for the viewer ───────────────────────────────────────────────────────────────────────────────

_CACHE: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _where(root: Optional[Path], path: str, index: Any) -> Callable[[tuple], Optional[str]]:
    """What an import reaches: a part of the system (by its name), an outside system, or None (the file's own part,
    the standard library, a package of no note)."""
    code = getattr(index, "code", None)
    labels = {v: k for k, v in (getattr(index, "labels", {}) or {}).items()}
    own = None
    if code is not None:
        try:
            from .activity import part_of
            own = part_of(path, code)
        except Exception:  # noqa: BLE001
            own = None

    def part_name(f: str) -> Optional[str]:
        if root is None or code is None:
            return None
        try:
            if not (Path(root) / f).is_file():
                return None
            from .activity import part_of
            p = part_of(f, code)
        except Exception:  # noqa: BLE001
            return None
        if not p or p == own:
            return None
        return labels.get(p) or p

    def where(imp: tuple) -> Optional[str]:
        module, level, name = imp
        if level == -1:   # TypeScript: a path
            if module.startswith("."):
                base = posixpath.normpath(posixpath.join(posixpath.dirname(path), module))
                for cand in [base] + [base + e for e in TS_EXTS] + [base + "/index" + e for e in TS_EXTS]:
                    got = part_name(cand)
                    if got:
                        return got
                return None
            top = module.split("/")[0]
            return OUTSIDE.get(top)
        for cand in _py_module_files(path, module, level, name):
            got = part_name(cand)
            if got:
                return got
        return OUTSIDE.get((module or "").split(".")[0]) if not level else None
    return where


def outline(path: str, text: str, root: Optional[Path] = None, index: Any = None, blob: str = "") -> Dict[str, Any]:
    """{"chunks": [...], "lang"} for one file; kept by blob id (a few hundred). A file that does not parse: no
    chunks, and "error" says why."""
    key = f"{blob or blob_id(text.encode('utf-8'))} {path}"
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    where = _where(root, path, index)
    out: Dict[str, Any] = {"chunks": [], "lang": ""}
    try:
        if path.endswith((".py", ".pyi")):
            out = {"chunks": _py_outline(path, text, where), "lang": "python"}
        elif path.endswith(TS_EXTS):
            out = {"chunks": _ts_outline(path, text, where), "lang": "typescript"}
    except SyntaxError as exc:
        out = {"chunks": [], "lang": "python", "error": f"does not parse: line {exc.lineno}"}
    except Exception as exc:  # noqa: BLE001 - an outline is a convenience
        out = {"chunks": [], "lang": "", "error": type(exc).__name__}
    with _CACHE_LOCK:
        _CACHE[key] = out
        while len(_CACHE) > 300:
            _CACHE.popitem(last=False)
    return out


# ── the LLM's description of each chunk: automatic, in the background, once per blob ─────────────────────────

LITERAL = re.compile(r"""(?P<q>["'`])(?P<body>(?:\\.|(?!(?P=q)).){6,}?)(?P=q)""")


def mask_secrets(code: str) -> str:
    """The code with literal secret values masked: a string assigned to a name that says it is a secret, and any
    string extract.redact would mask (a key-like token, a URL's password, password=… pairs)."""
    from .extract import redact
    out = []
    for line in code.split("\n"):
        named = SECRET_NAME.search(line.split("=", 1)[0]) if ("=" in line or ":" in line) else None
        if named is None and ":" in line:
            named = SECRET_NAME.search(line.split(":", 1)[0])

        def fix(m: re.Match) -> str:
            body = m.group("body")
            if named is not None and not re.fullmatch(r"[A-Z0-9_]+", body):   # SECRET_KEY = "…" (not an env name)
                return m.group("q") + "***" + m.group("q")
            masked = redact(body)
            return m.group("q") + masked + m.group("q") if masked != body.strip() else m.group(0)
        out.append(LITERAL.sub(fix, line))
    return "\n".join(out)


SYSTEM = ("You explain source code to a busy engineer who skims it to understand a system. Be short, concrete "
          "and plain: name the real things the code does, never restate the signature. Answer with JSON only.")


def prompt(path: str, code: str, chunks: List[Dict[str, Any]]) -> str:
    names = [c["qual"] for c in chunks]
    cut = len(code) > SEND_MAX
    return (f"File: {path}\n"
            "Return JSON: {\"file\": \"2-3 sentences: what this file does and how it fits in the system\", "
            "\"chunks\": {\"<name>\": {\"what\": \"what it does, in plain words (one sentence)\", \"how\": "
            "\"how: its key steps (one or two short sentences)\", \"watch\": \"what to watch: side effects, I/O, "
            "errors, concurrency or a surprising choice; empty if nothing\"}}}. "
            f"Use exactly these names as keys, skipping only trivial ones: {json.dumps(names)}\n"
            + ("(The file is cut; describe what you see.)\n" if cut else "")
            + "```\n" + code[:SEND_MAX] + "\n```")


def _clean(v: Any, most: int) -> str:
    if not isinstance(v, str):
        return ""
    v = " ".join(v.split())
    return v if len(v) <= most else v[:most - 1].rstrip() + "…"


def parse_answer(text: str, chunks: List[Dict[str, Any]], by: str = "") -> Optional[Dict[str, Any]]:
    """The LLM's JSON, kept only for names the outline has: {"file", "chunks": {qual: {"what", "how", "watch"}},
    "by", "at"}."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(got, dict):
        return None
    names = {c["qual"] for c in chunks}
    short = {c["name"]: c["qual"] for c in chunks}
    out: Dict[str, Dict[str, str]] = {}
    for k, v in (got.get("chunks") or {}).items() if isinstance(got.get("chunks"), dict) else []:
        q = k if k in names else short.get(k)
        if not q:
            continue
        said = ({f: _clean(v.get(f), SUMMARY_MAX) for f in ("what", "how", "watch")} if isinstance(v, dict)
                else {"what": _clean(v, SUMMARY_MAX), "how": "", "watch": ""})
        if said["what"] or said["how"]:
            out[q] = said
    return {"file": _clean(got.get("file"), FILE_SUMMARY_MAX), "chunks": out, "by": by, "at": round(time.time(), 3)}


def _summary_file(root: Path, blob: str) -> Path:
    from .sources import git_dir
    return git_dir(root, *SUMMARY_DIR) / f"{blob}.json"


_JOBS: Dict[str, float] = {}       # blob → when its job started (running) or failed (negative)
_JOBS_LOCK = threading.Lock()
_OPENED: set = set()                # blobs of files the person opened, waiting or being summarised: they go first
_WHY: Dict[str, str] = {}           # blob → why its last try failed


def summary(root: Path, blob: str) -> Optional[Dict[str, Any]]:
    try:
        got = json.loads(_summary_file(root, blob).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else None
    except (OSError, ValueError):
        return None


_CONFIGURED: Dict[str, tuple] = {}


def enabled(root: Optional[Path] = None) -> bool:
    """Whether an LLM may be asked: not with SYSTEM_DIFF_LLM=off, not in tests (unless one stands in), and only when
    one is set up (Claude Code's gateway, a shell setting, the `claude` CLI; looked up once a minute)."""
    from . import narrate
    if narrate.off():
        return False
    if ASK is not None:
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    key = str(root or "")
    got = _CONFIGURED.get(key)
    if got is None or time.time() - got[0] > 60:
        try:
            ok = narrate.configured(Path(root)) if root else True
        except Exception:  # noqa: BLE001
            ok = False
        got = _CONFIGURED[key] = (time.time(), ok)
    return got[1]


NO_LLM = "No LLM available: system-diff uses your Claude Code's, or set SYSTEM_DIFF_LLM_*"
RUNNING = 2        # summaries made at once
PREFETCH_MOST = 6  # summaries waiting at most before a prefetch is skipped
_RUN = threading.BoundedSemaphore(RUNNING)


def summarize(root: Path, path: str, text: str, blob: str, chunks: List[Dict[str, Any]],
              prefetch: bool = False) -> Dict[str, Any]:
    """The file's summary: {"state": "done", ...} when kept; else starts making it in the background (once per blob,
    a few at a time) and says {"state": "summarizing"}; {"state": "off", "why"} when no LLM may be asked;
    {"state": "failed"} after a failed try (tried again later); {"state": "later"} for a prefetch while many wait."""
    got = summary(root, blob)
    if got is not None:
        return {"state": "done", **got}
    if not enabled(root):
        return {"state": "off", "why": NO_LLM}
    if not text.strip():
        return {"state": "off", "why": "an empty file"}
    now = time.time()
    with _JOBS_LOCK:
        was = _JOBS.get(blob)
        if was is not None and was > 0:
            return {"state": "summarizing"}
        if was is not None and now + was < RETRY_AFTER:   # failed lately
            return {"state": "failed", "why": (_WHY.get(blob) or "the LLM gave no usable answer") +
                    "; tried again in a few minutes"}
        if prefetch and sum(1 for v in _JOBS.values() if v > 0) >= PREFETCH_MOST:
            return {"state": "later"}
        _JOBS[blob] = now
    threading.Thread(target=_job, args=(Path(root), path, text, blob, list(chunks), prefetch), daemon=True,
                     name="system-diff-summary").start()
    return {"state": "summarizing"}


def _job(root: Path, path: str, text: str, blob: str, chunks: List[Dict[str, Any]], prefetch: bool = False) -> None:
    ok = False
    # a file the person opened goes before files read ahead: prefetches wait while an opened one waits or runs
    if prefetch:
        while True:
            with _JOBS_LOCK:
                if not _OPENED:
                    break
            time.sleep(0.2)
    else:
        with _JOBS_LOCK:
            _OPENED.add(blob)
    _RUN.acquire()
    try:
        from . import config, narrate
        user = prompt(path, mask_secrets(text), chunks)
        ask = ASK or narrate.complete
        got = ask(user, SYSTEM, root, config.lenient(root), timeout=180, limit=12000) if ASK is None \
            else ask(user, SYSTEM, root, {})
        if got:
            answer, by = got if isinstance(got, tuple) else (got, "")
            parsed = parse_answer(answer, chunks, by)
            if parsed is not None:
                f = _summary_file(root, blob)
                tmp = f.with_name(f".{f.name}.{os.getpid()}.{threading.get_ident()}")
                tmp.write_text(json.dumps({"path": path, **parsed}, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, f)
                ok = True
            else:
                _WHY[blob] = "the LLM's answer did not fit this file's outline"
        else:
            _WHY[blob] = getattr(narrate, "LAST_ERROR", [""])[0].removeprefix("system-diff: ") or "no LLM answered"
    except Exception as exc:  # noqa: BLE001 - a summary is a convenience: say why it failed, try again later
        ok = False
        _WHY[blob] = f"{type(exc).__name__}: {str(exc)[:160]}"
    finally:
        _RUN.release()
        with _JOBS_LOCK:
            _OPENED.discard(blob)
            if ok:
                _JOBS.pop(blob, None)
            else:
                _JOBS[blob] = -time.time()


def wait_jobs(timeout: float = 5.0) -> None:
    """For tests: wait until no summary is being made."""
    end = time.time() + timeout
    while time.time() < end:
        with _JOBS_LOCK:
            if not any(v > 0 for v in _JOBS.values()):
                return
        time.sleep(0.02)
