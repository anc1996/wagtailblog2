"""Safe, deliberately small Markdown dialect for user comments."""
from __future__ import annotations

import html
import re

import bleach
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML

_ALLOWED_TAGS = {
    "p", "br", "strong", "em", "del", "u", "mark",
    "ul", "ol", "li", "blockquote", "code", "pre", "a", "hr",
    "table", "thead", "tbody", "tr", "th", "td",
}
_ALLOWED_ATTRIBUTES = {
    "a": ["href", "title", "rel", "target"],
    "code": ["class"],
}
_ALLOWED_PROTOCOLS = {"http", "https", "mailto"}
_STRONG_RE = re.compile(r"\*\*(?=\S)(.+?\S)\*\*")
_EM_RE = re.compile(r"(?<!\*)\*(?=\S)(.+?\S)\*(?!\*)")
_MARK_RE = re.compile(r"==(?=\S)(.+?\S)==")
_UNDERLINE_RE = re.compile(r"\+\+(?=\S)(.+?\S)\+\+")


def _render_text(tokens, index, options, env):
    """Render comment-only ==mark== and ++underline++ inside plain text tokens.

    Code spans/fences are separate token types, so their marker characters remain literal.
    """
    value = html.escape(tokens[index].content, quote=False)
    # CommonMark rejects emphasis next to some CJK punctuation. Comments intentionally
    # accept the familiar **中文。**后文 form requested by users.
    value = _STRONG_RE.sub(r"<strong>\1</strong>", value)
    value = _EM_RE.sub(r"<em>\1</em>", value)
    value = _MARK_RE.sub(r"<mark>\1</mark>", value)
    value = _UNDERLINE_RE.sub(r"<u>\1</u>", value)
    return value


def _render_link_open(tokens, index, options, env):
    token = tokens[index]
    token.attrSet("target", "_blank")
    token.attrSet("rel", "nofollow ugc noopener noreferrer")
    return "<a" + RendererHTML.renderAttrs(token) + ">"


def _build_renderer() -> MarkdownIt:
    md = MarkdownIt(
        "commonmark",
        {
            "html": False,
            "linkify": False,
            "typographer": False,
            "breaks": True,
        },
    ).enable(["strikethrough", "table"])
    md.renderer.rules["text"] = _render_text
    md.renderer.rules["link_open"] = _render_link_open
    md.renderer.rules["s_open"] = lambda tokens, index, options, env: "<del>"
    md.renderer.rules["s_close"] = lambda tokens, index, options, env: "</del>"
    return md


_MARKDOWN = _build_renderer()


def render_comment_markdown(value: str | None) -> str:
    """Render Markdown and sanitize the result for untrusted comments."""
    if not value:
        return ""

    rendered = _MARKDOWN.render(str(value).strip())
    return bleach.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )


def render_reply_markdown(value: object, replied_to_username: object) -> str:
    """Render a reply without duplicating its structured leading @mention.

    Older reply rows include ``@username`` in ``content`` because the browser
    used to prefill it. Templates already render the replied-to user separately,
    so remove only an exact mention at the start for display. Stored content is
    deliberately left unchanged for editing and audit purposes.
    """
    source = str(value or "")
    username = str(replied_to_username or "").strip()
    prefix = f"@{username}"

    if username and source.startswith(prefix):
        remainder = source[len(prefix):]
        if not remainder or remainder[0].isspace():
            source = remainder.lstrip()

    return render_comment_markdown(source)
