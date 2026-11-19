"""The Studio's chat: Claude's replies as Markdown (safe: everything escaped, no script links), and Up/Down walking
the messages sent in the project, as in Claude Code."""
import json
import shutil
import subprocess

import pytest

from system_diff import app_page

NODE = shutil.which("node")


def _renderer() -> str:
    page = app_page.PAGE
    a, b = page.index("/* md:start */"), page.index("/* md:end */")
    return page[a:b]


def render(*texts):
    """md(text) for each text, run under node."""
    code = _renderer() + "\nconst texts = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n" \
                         "process.stdout.write(JSON.stringify(texts.map(md)));\n"
    done = subprocess.run([NODE, "-e", code], input=json.dumps(list(texts)), capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_bold_and_code_in_list_items():
    [html] = render("- **New flow definitions**: acct0022 monthly travel plan\n- **Agent**: `newhire_form.py` and *more*")
    assert html.startswith("<ul><li><strong>New flow definitions</strong>: acct0022 monthly travel plan</li>")
    assert "<code>newhire_form.py</code>" in html and "<em>more</em>" in html and "**" not in html


@needs_node
def test_blocks():
    fence, table, nested, link, head = render(
        "```python\nif a < b and c & d:\n    print('**not bold**')\n```",
        "| Name | Size |\n|:-----|-----:|\n| `a|b` | 3 |",
        "1. one\n   - inner **b**\n   - two\n2. three\n- [x] done\n- [ ] todo",
        "See [the docs](https://example.com/a?b=1&c=2) or https://x.org/p.",
        "## Title\n\n> quoted\n\n---\nline one\nline two ~~gone~~ snake_case_name")
    assert '<span>python</span>' in fence and "mdcopy" in fence
    assert "if a &lt; b and c &amp; d:\n    print(&#39;**not bold**&#39;)" in fence and "<strong>" not in fence
    assert '<th style="text-align:left">Name</th><th style="text-align:right">Size</th>' in table
    assert "<td style=\"text-align:left\"><code>a|b</code></td>" in table
    assert nested.startswith("<ol><li>one<ul><li>inner <strong>b</strong></li><li>two</li></ul></li><li>three</li></ol>")
    assert 'type="checkbox" disabled checked>done' in nested and 'type="checkbox" disabled>todo' in nested
    assert '<a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">the docs</a>' in link
    assert '<a href="https://x.org/p" target="_blank" rel="noopener noreferrer">https://x.org/p</a>.' in link
    assert "<h2>Title</h2>" in head and "<blockquote><p>quoted</p></blockquote>" in head and "<hr>" in head
    assert "line one<br>line two <del>gone</del> snake_case_name" in head


@needs_node
def test_nothing_runs():
    for html in render("<script>alert(1)</script>", '<img src=x onerror="alert(1)">', "[x](javascript:alert(1))",
                       "[y](data:text/html,<b>)", "<b>raw</b> **<i>x</i>**", "`<script>`",
                       "| <img src=x onerror=alert(1)> |\n|---|\n| <svg onload=alert(1)> |"):
        assert "<script" not in html and "<img" not in html and "<svg" not in html and "<b>" not in html \
            and "<i>" not in html and "href=\"javascript" not in html and "href=\"data" not in html, html
    js, = render("[x](javascript:alert(1))")
    assert "<a" not in js and "javascript:alert(1)" in js


@needs_node
def test_a_key_like_message_stays_out_of_the_stored_history():
    page = app_page.PAGE
    line = next(x for x in page.splitlines() if x.startswith("const KEYLIKE = "))
    code = line + "\nprocess.stdout.write(JSON.stringify(JSON.parse(require('fs').readFileSync(0, 'utf8')).map(t => KEYLIKE.test(t))));"
    texts = ["azure_pat_key = abcdefghijklmnop1234", "my token: ghp_" + "a" * 36, "AKIAABCDEFGHIJKLMNOP",
             "switch to main and pull", "the key idea is simple", "monkey = 3"]
    done = subprocess.run([NODE, "-e", code], input=json.dumps(texts), capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert json.loads(done.stdout) == [True, True, True, False, False, False], done.stderr


def test_the_history_is_there():
    page = app_page.PAGE
    assert "function histStep(dir)" in page and "histPush(text);" in page and 'e.key === "ArrowUp"' in page
    assert "if (e.isComposing) return;" in page and "KEYLIKE" in page   # a key-like message stays out of storage
    assert 'mdPaint(d, ev.text)' in page and "mdPaint(box, box.raw)" in page   # the final text and the stream
