"""Keys the person stores for their agents (an Azure DevOps PAT to open a pull request, a GitHub token): kept in the
operating system's own store, given to every later Studio session of a project as environment variables, never put
in a chat, a prompt, a log or a listing.

A key has a name (an environment variable's), a scope ("*": every project; else one project's root) and a
description. What is not secret (name, scope, description, when, where it is kept) is in vault.json in the Studio's
state folder (readable by this user only). The value is kept by the OS:

- Windows: DPAPI (CryptProtectData, for this Windows account only), the encrypted blob in vault-values.json;
- macOS: the Keychain (`security`, service "system-diff");
- Linux: the Secret Service (`secret-tool`) when it is there and a desktop session runs;
- otherwise: vault-values.json itself, a file only this user can read ("no keyring here", said where keys are listed).

Values are read when a session starts (env_for), not per request.

Beside secrets, a project's **settings**: plain values a session needs that are not secret (a server's host or IP,
a user name, a port, a URL). Kept in vault.json itself, shown on the page and named with their value in Claude's
prompt; never masked. Every entry can carry a group ("staging server": STAGING_HOST, STAGING_USER,
STAGING_PASSWORD). A value that looks secret is never taken as a setting (looks_secret).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

META = "vault.json"
VALUES = "vault-values.json"
SERVICE = "system-diff"
NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
VALUE_MOST = 16 * 1024
# names that steer Claude Code, Python or the OS themselves: a key under one would change how the session runs
RESERVED = {"PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
            "TEMP", "TMP", "TMPDIR", "SHELL", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "LD_PRELOAD",
            "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "CLAUDECODE", "CLAUDE_CONFIG_DIR"}
RESERVED_PREFIXES = ("SYSTEM_DIFF_", "CLAUDE_CODE_", "ANTHROPIC_")
WHERE = {"dpapi": "Windows' protected storage (DPAPI), for your Windows account only",
         "keychain": "your macOS Keychain",
         "secret-service": "your keyring (Secret Service)",
         "file": "a file only you can read (no keyring here)"}
_LOCK = threading.Lock()


# ── where ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _folder(folder: Optional[Path]) -> Optional[Path]:
    if folder is not None:
        return Path(folder)
    from .app import state_dir
    return state_dir()


def _same(p: Any) -> str:
    try:
        return os.path.normcase(str(Path(p).resolve()))
    except (OSError, ValueError, RuntimeError):
        return os.path.normcase(str(p))


def scope_of(project: Optional[Path], everywhere: bool) -> str:
    if everywhere:
        return "*"
    if project is None:
        raise ValueError("a key for one project needs the project")
    return _same(project)


def backend() -> str:
    """Where a new value goes on this machine."""
    if os.name == "nt":
        return "dpapi"
    if sys.platform == "darwin" and shutil.which("security"):
        return "keychain"
    if shutil.which("secret-tool") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return "secret-service"
    return "file"


def store_name() -> str:
    return WHERE[backend()]


def check_name(name: str) -> str:
    name = str(name or "").strip()
    if not NAME.match(name):
        raise ValueError("a key's name is an environment variable's: capital letters, digits and _ (AZURE_DEVOPS_EXT_PAT)")
    if name in RESERVED or name.startswith(RESERVED_PREFIXES):
        raise ValueError(f"{name} steers Claude Code or the system itself: pick another name")
    return name


# ── the files ──────────────────────────────────────────────────────────────────────────────────────────────────
def _read(f: Path) -> Dict[str, Any]:
    try:
        got = json.loads(f.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(f: Path, data: Any) -> None:
    from .app import _write_private
    _write_private(f, data)


def _meta(folder: Path) -> List[Dict[str, Any]]:
    keys = _read(folder / META).get("keys")
    return [k for k in keys if isinstance(k, dict) and k.get("name") and k.get("scope")] if isinstance(keys, list) else []


# ── the OS's stores ────────────────────────────────────────────────────────────────────────────────────────────
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.POINTER(BLOB), ctypes.c_void_p, ctypes.c_void_p,
                   wintypes.DWORD, ctypes.POINTER(BLOB)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(data, len(data))
    extra = ctypes.create_string_buffer(b"system-diff vault", 17)
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    entropy = BLOB(17, ctypes.cast(extra, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not fn(ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None, 0x01, ctypes.byref(out)):  # UI_FORBIDDEN
        raise OSError(f"DPAPI failed (error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _run(argv: List[str], given: Optional[str] = None) -> str:
    done = subprocess.run(argv, input=given, capture_output=True, text=True, timeout=15)
    if done.returncode:
        raise OSError(f"{Path(argv[0]).name} {argv[1]} failed (exit {done.returncode})")   # never its output
    return done.stdout


def _put(folder: Path, kind: str, account: str, value: str) -> None:
    if kind == "keychain":
        _run(["security", "add-generic-password", "-U", "-s", SERVICE, "-a", account, "-w", value])
        return
    if kind == "secret-service":
        _run(["secret-tool", "store", f"--label=system-diff {account}", "service", SERVICE, "account", account],
             given=value)
        return
    vals = _read(folder / VALUES)
    if kind == "dpapi":
        vals[account] = {"kind": "dpapi", "data": base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode("ascii")}
    else:
        vals[account] = {"kind": "file", "data": value}
    _write(folder / VALUES, vals)


def _get(folder: Path, kind: str, account: str) -> str:
    if kind == "keychain":
        return _run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"]).rstrip("\n")
    if kind == "secret-service":
        return _run(["secret-tool", "lookup", "service", SERVICE, "account", account])
    got = _read(folder / VALUES).get(account)
    if not isinstance(got, dict):
        raise OSError("its value is missing")
    if got.get("kind") == "dpapi":
        return _dpapi(base64.b64decode(str(got.get("data") or "")), False).decode("utf-8")
    return str(got.get("data") or "")


def _drop_os(kind: str, account: str) -> None:
    try:
        if kind == "keychain":
            _run(["security", "delete-generic-password", "-s", SERVICE, "-a", account])
        elif kind == "secret-service":
            _run(["secret-tool", "clear", "service", SERVICE, "account", account])
    except (OSError, subprocess.SubprocessError):
        pass


def _drop(folder: Path, kind: str, account: str) -> None:
    _drop_os(kind, account)
    vals = _read(folder / VALUES)
    if account in vals:
        del vals[account]
        _write(folder / VALUES, vals)


# ── what the Studio, the sessions and the command line use ─────────────────────────────────────────────────────
KINDS = ("secret", "setting")
SECRET_NAME = re.compile(r"PASS|PWD|SECRET|TOKEN|(^|_)KEY($|_)|APIKEY|(^|_)PAT($|_)|CREDENTIAL|PRIVATE|CERT|AUTH")
SECRET_VALUE = re.compile(r"^(gh[pousr]_|github_pat_|AKIA|ASIA|xox[abposr]-|sk-|eyJ[\w-]+\.|-----BEGIN)")


def looks_secret(name: str, value: str) -> bool:
    """Whether a name or a value says it is secret: a setting must not be (its value is shown and told to Claude)."""
    n, v = str(name or "").upper(), str(value or "").strip()
    if SECRET_NAME.search(n) or SECRET_VALUE.search(v):
        return True
    if re.fullmatch(r"[a-z2-7]{52}", v):   # an Azure DevOps PAT
        return True
    if len(v) >= 20 and " " not in v and not re.match(r"^[a-z][a-z0-9+.-]*://", v):
        classes = sum(bool(re.search(c, v)) for c in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w.-]"))
        if classes >= 3:
            return True
    return False


def kind_of(k: Dict[str, Any]) -> str:
    return "setting" if k.get("kind") == "setting" else "secret"


def _public(k: Dict[str, Any], project: Optional[Path] = None) -> Dict[str, Any]:
    setting = kind_of(k) == "setting"
    return {"name": k["name"], "scope": "all projects" if k["scope"] == "*" else
            ("this project" if project is not None and k["scope"] == _same(project) else k["scope"]),
            "kind": kind_of(k), "group": str(k.get("group") or ""),
            "description": str(k.get("description") or ""), "saved": k.get("saved"),
            "kept_in": "system-diff's settings (not secret)" if setting else
            WHERE.get(str(k.get("kept_in")), str(k.get("kept_in") or "")),
            **({"value": str(k.get("value") or "")} if setting else {})}


def everything(folder: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every entry of every project: scope, project, kind, group, description; a setting's value, never a secret's."""
    where = _folder(folder)
    if where is None:
        return []
    out = []
    for k in _meta(where):
        got = _public(k)
        got["project"] = "" if k["scope"] == "*" else k["scope"]
        out.append(got)
    return sorted(out, key=lambda x: (x["project"], x["group"], x["name"]))


def listing(project: Optional[Path] = None, folder: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The keys that apply to `project` (every key with no project): name, scope, description, where kept. No value."""
    where = _folder(folder)
    if where is None:
        return []
    mine = _same(project) if project is not None else None
    return [_public(k, project) for k in _meta(where) if mine is None or k["scope"] in ("*", mine)]


def save(name: str, value: str, project: Optional[Path] = None, everywhere: bool = False, description: str = "",
         folder: Optional[Path] = None, kind: str = "secret", group: str = "") -> Dict[str, Any]:
    """Store a secret (kept by the OS) or a setting (`kind="setting"`: a plain value, refused when it looks secret)."""
    name = check_name(name)
    if kind not in KINDS:
        raise ValueError("kind: secret or setting")
    value = str(value if value is not None else "")
    if not value.strip():
        raise ValueError("the key's value is empty")
    if len(value) > VALUE_MOST:
        raise ValueError("the key's value is too long")
    where = _folder(folder)
    if where is None:
        raise ValueError("there is no state folder to keep keys in here")
    scope = scope_of(project, everywhere)
    account = f"{scope}:{name}"
    if kind == "setting":
        if looks_secret(name, value):
            raise ValueError(f"{name} looks like a secret (a password, a token, a key): store it as a secret, in the "
                             f"Studio's Keys or with `system-diff secrets set {name}`, never as a setting")
        with _LOCK:
            keys = _meta(where)
            old = next((k for k in keys if k["name"] == name and k["scope"] == scope), None)
            if old is not None and kind_of(old) == "secret":
                _drop(where, str(old.get("kept_in")), account)
            entry = {"name": name, "scope": scope, "kind": "setting", "group": str(group or "")[:80],
                     "description": str(description or "")[:300], "saved": round(time.time(), 3),
                     "kept_in": "plain", "value": value}
            keys = [k for k in keys if not (k["name"] == name and k["scope"] == scope)] + [entry]
            _write(where / META, {"keys": keys})
        return _public(entry, project)
    with _LOCK:
        keys = _meta(where)
        old = next((k for k in keys if k["name"] == name and k["scope"] == scope), None)
        kind = backend()
        try:
            _put(where, kind, account, value)
        except (OSError, subprocess.SubprocessError, ValueError):
            if kind != "secret-service":
                raise
            kind = "file"   # the keyring would not take it (locked, no session): a file only you can read
            _put(where, kind, account, value)
        if old is not None and old.get("kept_in") != kind:   # moved stores: the old copy goes
            if old.get("kept_in") in ("keychain", "secret-service"):
                _drop_os(str(old.get("kept_in")), account)
            elif kind in ("keychain", "secret-service"):
                _drop(where, "file", account)
        entry = {"name": name, "scope": scope, "kind": "secret", "group": str(group or "")[:80],
                 "description": str(description or "")[:300], "saved": round(time.time(), 3), "kept_in": kind}
        keys = [k for k in keys if not (k["name"] == name and k["scope"] == scope)] + [entry]
        _write(where / META, {"keys": keys})
    return _public(entry, project)


def update(name: str, project: Optional[Path] = None, everywhere: bool = False, folder: Optional[Path] = None,
           value: Optional[str] = None, description: Optional[str] = None, group: Optional[str] = None) -> Dict[str, Any]:
    """Change an entry: a setting's value, a secret's value (replaced, never read back), its description or group."""
    where = _folder(folder)
    if where is None:
        raise ValueError("there is no state folder here")
    scope = scope_of(project, everywhere)
    with _LOCK:
        hit = next((k for k in _meta(where) if k["name"] == str(name or "").strip() and k["scope"] == scope), None)
    if hit is None:
        raise ValueError(f"no entry {name} stored for {'all projects' if scope == '*' else 'this project'}")
    if value is not None and str(value) != "":
        return save(hit["name"], str(value), project, everywhere,
                    hit.get("description", "") if description is None else description, folder, kind_of(hit),
                    hit.get("group", "") if group is None else group)
    with _LOCK:
        keys = _meta(where)
        for k in keys:
            if k["name"] == hit["name"] and k["scope"] == scope:
                if description is not None:
                    k["description"] = str(description)[:300]
                if group is not None:
                    k["group"] = str(group)[:80]
                k["saved"] = round(time.time(), 3)
                hit = k
        _write(where / META, {"keys": keys})
    return _public(hit, project)


def forget(name: str, project: Optional[Path] = None, everywhere: bool = False,
           folder: Optional[Path] = None) -> Dict[str, Any]:
    name = str(name or "").strip()
    where = _folder(folder)
    if where is None:
        raise ValueError("there is no state folder here")
    scope = scope_of(project, everywhere)
    with _LOCK:
        keys = _meta(where)
        hit = next((k for k in keys if k["name"] == name and k["scope"] == scope), None)
        if hit is None:
            raise ValueError(f"no key {name} stored for {'all projects' if scope == '*' else 'this project'}")
        if kind_of(hit) == "secret":
            _drop(where, str(hit.get("kept_in")), f"{scope}:{name}")
        _write(where / META, {"keys": [k for k in keys if k is not hit]})
    return {"forgot": name, "scope": "all projects" if scope == "*" else "this project"}


def env_for(project: Path, folder: Optional[Path] = None, problems: Optional[List[str]] = None) -> Dict[str, str]:
    """{name: value} for a session of `project`: the all-projects keys, then the project's own over them. A key whose
    value cannot be read is left out (its name, never its value, goes to `problems`)."""
    where = _folder(folder)
    if where is None:
        return {}
    mine = _same(project)
    keys = [k for k in _meta(where) if k["scope"] == "*"] + [k for k in _meta(where) if k["scope"] == mine]
    out: Dict[str, str] = {}
    for k in keys:
        if kind_of(k) == "setting":
            out[k["name"]] = str(k.get("value") or "")
            continue
        try:
            out[k["name"]] = _get(where, str(k.get("kept_in")), f"{k['scope']}:{k['name']}")
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError) as exc:
            if problems is not None:
                problems.append(f"{k['name']}: {exc}")
    return out


def reveal(name: str, project: Optional[Path] = None, everywhere: bool = False, folder: Optional[Path] = None) -> str:
    """One stored value, for the person who asks on the Studio's own page (Show): never logged, never kept."""
    where = _folder(folder)
    if where is None:
        raise ValueError("there is no key store here")
    name = check_name(name)
    scope = "*" if everywhere or project is None else _same(project)
    k = next((x for x in _meta(where) if x["name"] == name and x["scope"] == scope), None)
    if k is None:
        raise ValueError(f"no key {name} {'for all projects' if scope == '*' else 'for this project'}")
    if kind_of(k) == "setting":
        return str(k.get("value") or "")
    return _get(where, str(k.get("kept_in")), f"{k['scope']}:{k['name']}")


def prompt_note(keys: List[Dict[str, Any]]) -> str:
    """What the session's prompt says of its keys: names and descriptions, never a secret's value; a setting's value
    (not secret: a host, a user) is said, with the tools to change them."""
    if not keys:
        return ""

    def label(k: Dict[str, Any]) -> str:
        about = ", ".join(x for x in (k.get("group"), k.get("description")) if x)
        return k["name"] + (f" ({about})" if about else "")
    sets = [k for k in keys if k.get("kind") == "setting"]
    keys = [k for k in keys if k.get("kind") != "setting"]
    said = ""
    if sets:
        said = (" Project settings the person stored for you (in your environment too): "
                + "; ".join(f"{label(k)} = {k.get('value', '')}" for k in sets) + ". Change them with save_setting "
                "(project_settings lists them); a secret's value is never yours to take: ask_for_secret asks the "
                "person for it in a password field.")
    if not keys:
        return said
    names = ", ".join(label(k) for k in keys)
    hint = (" az devops and az repos read AZURE_DEVOPS_EXT_PAT themselves."
            if any(k["name"] == "AZURE_DEVOPS_EXT_PAT" for k in keys) else "")
    return (f" Keys the person stored for you (system-diff's key store) are in your environment: {names}. Use them by "
            "name ($NAME in bash, $env:NAME in PowerShell); never print, echo or write their values into files, "
            "commits, logs or messages; when a command needs one (a header, a URL), build it inside the command from "
            "the variable." + hint + said)


def mask(obj: Any, masks: List[tuple]) -> Any:
    """`obj` with every stored value in its text replaced by ••••NAME. masks: [(value, name)], longest first."""
    if not masks:
        return obj
    if isinstance(obj, str):
        for v, n in masks:
            if v in obj:
                obj = obj.replace(v, "••••" + n)
        return obj
    if isinstance(obj, dict):
        return {k: mask(v, masks) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask(v, masks) for v in obj]
    return obj


def masks_of(env: Dict[str, str], keys: Optional[List[Dict[str, Any]]] = None) -> List[tuple]:
    """[(value, name)] to mask: the secrets' (a setting is shown as it is), longest first."""
    plain = {k["name"] for k in keys or [] if k.get("kind") == "setting"}
    return sorted(((v, n) for n, v in env.items() if len(v) >= 6 and n not in plain), key=lambda x: -len(x[0]))
