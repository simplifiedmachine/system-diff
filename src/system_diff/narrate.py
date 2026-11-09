"""The optional plain-words summary on top of the exact diff, written by an LLM; the pictures never depend on it.

Which LLM: the one your Claude Code uses (its settings files' `env` blocks, layered as Claude Code layers them, with
ANTHROPIC_* in the environment over all of them); else an OpenAI-compatible gateway exported in your own shell
(SYSTEM_DIFF_LLM_BASE_URL / _API_KEY / _MODEL, or LLM_*). Only the headline, the exact plan and the commit messages are
sent: never the code. Nothing answers, no summary.
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

DEFAULT_MODEL = "claude-sonnet-5-5"

SYSTEM = (
    "You explain to the reviewers of a pull request what it does to a system's architecture. You are given an "
    "exact, machine-extracted structural diff and the commit messages. Write two parts, plain words, no headings: "
    "1) two to four sentences on what changes in the system and why; 2) up to four bullet points a reviewer should "
    "check, each grounded in a line of the diff. Never mention a component, endpoint, table or system that is not "
    "in the diff. If the diff shows no structural change, say so in one sentence."
)


def claude_settings_files(root: Path, extra: List[str] = ()) -> List[Tuple[Path, str]]:
    """Claude Code settings that can carry the gateway, lowest precedence first: user, user-local, project,
    project-local, then any the config adds."""
    home, repo = Path.home() / ".claude", root / ".claude"
    files = [(home / "settings.json", "~/.claude/settings.json"),
             (home / "settings.local.json", "~/.claude/settings.local.json"),
             (repo / "settings.json", ".claude/settings.json"),
             (repo / "settings.local.json", ".claude/settings.local.json")]
    return files + [(Path(os.path.expanduser(p)), p) for p in extra]


def claude_llm(root: Path, extra: List[str] = ()) -> Optional[Dict[str, Any]]:
    """The gateway your Claude Code uses; whatever model it names is the model used."""
    env: Dict[str, str] = {}
    source = ""
    for path, label in claude_settings_files(root, extra):
        try:
            block = json.loads(path.read_text(encoding="utf-8")).get("env", {}) or {}
        except (OSError, ValueError):
            continue
        env.update(block)
        if block.get("ANTHROPIC_BASE_URL") or block.get("ANTHROPIC_API_KEY"):
            source = label
    for k, v in os.environ.items():
        if k.startswith("ANTHROPIC_") and v:
            env[k] = v
            if k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"):
                source = "ANTHROPIC_* in the environment"
    token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY", "")
    if not token:
        return None
    base = (env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    return {"base_url": base, "token": token, "model": env.get("ANTHROPIC_MODEL", DEFAULT_MODEL), "source": source}


def shell_llm() -> Optional[Dict[str, Any]]:
    """An OpenAI-compatible LLM someone exported in their own shell on purpose — never a committed file."""
    env = os.environ
    base = env.get("SYSTEM_DIFF_LLM_BASE_URL") or env.get("LLM_BASE_URL", "")
    key = env.get("SYSTEM_DIFF_LLM_API_KEY") or env.get("LLM_API_KEY", "")
    model = env.get("SYSTEM_DIFF_LLM_MODEL") or env.get("LLM_MODEL", "")
    if base and key and model:
        return {"kind": "openai", "base_url": base.rstrip("/"), "token": key, "model": model,
                "verify": env.get("LLM_SSL_VERIFY", "true").lower() == "true"}
    return None


def ask(s: Dict[str, Any], user: str, max_tokens: int, timeout: int, system: str = SYSTEM) -> str:
    """One tool-less completion, OpenAI-compatible or Anthropic Messages."""
    if s.get("kind") == "openai":
        url = s["base_url"] + "/chat/completions"
        body: Dict[str, Any] = {"model": s["model"], "max_tokens": max_tokens,
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"content-type": "application/json", "authorization": "Bearer " + s["token"]}
    else:
        url = s["base_url"] + "/v1/messages"
        body = {"model": s["model"], "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        if s.get("thinking"):
            # A reasoning model given no thinking budget can think until max_tokens and return no text at all.
            body["thinking"] = {"type": "enabled", "budget_tokens": s["thinking"]}
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
                   "authorization": "Bearer " + s["token"], "x-api-key": s["token"]}
    # Fail in seconds, not after the read timeout, when this machine cannot reach the gateway at all.
    u = urlparse(url)
    socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=5).close()
    context = None if s.get("verify", True) else ssl._create_unverified_context()
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
        data = json.loads(r.read())
    if s.get("kind") == "openai":
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    else:
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def narrate(d: dict, commits: str, root: Path, cfg: Dict[str, Any], timeout: int = 120) -> Optional[str]:
    """The summary, naming the model that wrote it; None when no LLM answers from here."""
    user = f"Headline: {d['headline']}\n\nStructural diff (exact):\n{d['plan']}\n\nCommit messages:\n{commits[:4000]}"
    got = complete(user, SYSTEM, root, cfg, timeout)
    return f"{got[0]}\n\n_Summary written by {got[1]} from the exact diff below._" if got else None


def complete(user: str, system: str, root: Path, cfg: Dict[str, Any], timeout: int = 120,
             limit: int = 1500) -> Optional[Tuple[str, str]]:
    """The first answer an LLM gives from here, and who gave it: (text, "model (where it is set)"); else None."""
    claude = claude_llm(root, cfg.get("narrate", {}).get("settings", []))
    if claude:
        # With a thinking budget first (some gateways' reasoning models need one); without it if refused.
        for attempt in ({**claude, "thinking": 4000}, claude):
            try:
                text = ask(attempt, user, 12000, timeout, system)
            except urllib.error.HTTPError as exc:
                if exc.code == 400 and attempt.get("thinking"):
                    continue  # this model or gateway takes no thinking parameter
                print(f"system-diff: {claude['model']} via {claude['source']}: HTTP {exc.code}", file=sys.stderr)
                break
            except (OSError, ValueError) as exc:  # not reachable from here, or not JSON
                print(f"system-diff: {claude['model']} via {claude['source']}: {type(exc).__name__}: {str(exc)[:120]}",
                      file=sys.stderr)
                break
            if text:
                return text[:limit], f"{claude['model']} ({claude['source']})"
    shell = shell_llm()
    if shell:
        for tokens in (2500, 12000):
            try:
                text = ask(shell, user, tokens, timeout, system)
            except (OSError, ValueError) as exc:
                print(f"system-diff: {shell['model']} via {shell['base_url']}: {type(exc).__name__}: {str(exc)[:120]}",
                      file=sys.stderr)
                break
            if text:
                return text[:limit], f"{shell['model']} (an LLM set in this shell)"
    return None
