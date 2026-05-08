"""Tests for ``benchclaw.channels.telegrm.markdown_to_telegram_html``.

A pure-function utility powering Telegram delivery: model output
(markdown) → Telegram HTML.

Rendering bugs here are user-visible — broken bold, leaked HTML
metacharacters, links that don't click. Cheap and high-value to lock
down with unit tests.

NOTE: Phase 2 splits ``channels/telegrm.py`` into a package with a
public ``markdown_html`` module and adds ``split_long``. Tests for
``split_long`` land with that phase.
"""

from __future__ import annotations

from benchclaw.channels.telegrm import _markdown_to_telegram_html as markdown_to_telegram_html


def test_empty_input_returns_empty_string():
    assert markdown_to_telegram_html("") == ""


def test_plain_text_passes_through():
    assert markdown_to_telegram_html("hello world") == "hello world"


def test_bold_with_double_asterisks():
    assert markdown_to_telegram_html("**hi**") == "<b>hi</b>"


def test_bold_with_double_underscores():
    assert markdown_to_telegram_html("__hi__") == "<b>hi</b>"


def test_italic_with_single_underscore():
    assert markdown_to_telegram_html("_hi_") == "<i>hi</i>"


def test_italic_does_not_match_inside_words():
    """``foo_bar_baz`` must not turn ``_bar_`` into italics — that's a snake_case
    identifier, not emphasis."""
    assert markdown_to_telegram_html("foo_bar_baz") == "foo_bar_baz"


def test_strikethrough():
    assert markdown_to_telegram_html("~~gone~~") == "<s>gone</s>"


def test_link_renders_as_anchor():
    assert (
        markdown_to_telegram_html("see [docs](https://example.com)")
        == 'see <a href="https://example.com">docs</a>'
    )


def test_heading_strips_hash_marks():
    assert markdown_to_telegram_html("# Title\nbody") == "Title\nbody"
    assert markdown_to_telegram_html("### Sub") == "Sub"


def test_blockquote_strips_marker():
    assert markdown_to_telegram_html("> quoted line") == "quoted line"


def test_unordered_list_uses_bullet_char():
    rendered = markdown_to_telegram_html("- one\n* two")
    assert rendered == "• one\n• two"


def test_html_metacharacters_in_prose_are_escaped():
    assert markdown_to_telegram_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_inline_code_escapes_html_inside():
    """HTML inside `inline code` must be entity-escaped — Telegram parses
    the rendered output as HTML, so an unescaped < would break the message."""
    rendered = markdown_to_telegram_html("`<script>alert(1)</script>`")
    assert rendered == "<code>&lt;script&gt;alert(1)&lt;/script&gt;</code>"


def test_fenced_code_block_preserves_content():
    src = "```\nx = 1\ny = 2\n```"
    assert markdown_to_telegram_html(src) == "<pre><code>x = 1\ny = 2\n</code></pre>"


def test_fenced_code_block_with_language_tag():
    src = "```python\ndef f(): pass\n```"
    assert markdown_to_telegram_html(src) == "<pre><code>def f(): pass\n</code></pre>"


def test_fenced_code_block_escapes_html():
    src = "```\n<div>&\n```"
    assert markdown_to_telegram_html(src) == "<pre><code>&lt;div&gt;&amp;\n</code></pre>"


def test_inline_code_protects_markdown_inside():
    """Markdown special chars inside `code` must not be re-interpreted as
    markdown — that's the whole point of code spans."""
    rendered = markdown_to_telegram_html("use `**not bold**` here")
    assert rendered == "use <code>**not bold**</code> here"


def test_bold_inside_link_text():
    rendered = markdown_to_telegram_html("[**important**](https://x)")
    assert rendered == '<a href="https://x"><b>important</b></a>'
