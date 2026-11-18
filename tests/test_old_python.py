"""The oldest Python system-diff supports (3.11) rejects two f-string forms newer ones take: a backslash inside an
expression, and the outer quote reused inside one. Caught here on any Python (the tokenizer of 3.12+ shows them)."""
from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="3.11 itself refuses to import such a file")
def test_no_f_string_311_cannot_read():
    bad = []
    for f in sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py")):
        stack = []
        for t in tokenize.generate_tokens(io.StringIO(f.read_text(encoding="utf-8")).readline):
            if t.type == tokenize.FSTRING_START:
                q = t.string.lstrip("rRbBfFuU")
                if stack and stack[-1][1] > 0 and q[0] == stack[-1][0][0]:
                    bad.append(f"{f.name}:{t.start[0]} reuses the outer quote")
                stack.append([q, 0])
            elif t.type == tokenize.FSTRING_END:
                stack.pop()
            elif stack:
                if t.type == tokenize.OP and t.string == "{":
                    stack[-1][1] += 1
                elif t.type == tokenize.OP and t.string == "}":
                    stack[-1][1] -= 1
                elif stack[-1][1] > 0 and t.type == tokenize.STRING:
                    if "\\" in t.string:
                        bad.append(f"{f.name}:{t.start[0]} a backslash in an expression")
                    if t.string.lstrip("rRbBfFuU")[0] == stack[-1][0][0]:
                        bad.append(f"{f.name}:{t.start[0]} reuses the outer quote")
    assert bad == []
