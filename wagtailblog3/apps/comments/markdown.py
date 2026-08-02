"""为用户评论提供安全且有意保持精简的 Markdown 方言。"""
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
_DISPLAY_MATH_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)


def _render_text(tokens, index, options, env):
    """在普通文本令牌中渲染评论专用的标记和下划线语法。

    代码行内块和围栏代码属于独立令牌，因此其中的标记字符会保持原样。
    """
    value = html.escape(tokens[index].content, quote=False)
    # 先转义原始文本，再替换受控标签，避免用户输入直接形成 HTML。
    # CommonMark 对中文标点相邻的强调语法限制较多，评论区因此接受用户熟悉的写法。
    value = _STRONG_RE.sub(r"<strong>\1</strong>", value)
    value = _EM_RE.sub(r"<em>\1</em>", value)
    value = _MARK_RE.sub(r"<mark>\1</mark>", value)
    value = _UNDERLINE_RE.sub(r"<u>\1</u>", value)
    return value


def _render_link_open(tokens, index, options, env):
    # 外链统一新窗口打开，并加入防止反向标签和搜索引擎传递权重的属性。
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


def _normalise_display_math(value: str) -> str:
    """将 ``$$...$$`` 保持在同一个 Markdown 文本节点中，交给 KaTeX 自动渲染。"""
    def collapse(match: re.Match[str]) -> str:
        body = re.sub(r"\s+", " ", match.group(1)).strip()
        return f"$${body}$$"

    return _DISPLAY_MATH_RE.sub(collapse, value)


def render_comment_markdown(value: str | None) -> str:
    """渲染 Markdown，并按白名单清理不可信评论产生的 HTML。"""
    if not value:
        return ""

    # Markdown 渲染器关闭原始 HTML；Bleach 再做一次标签、属性和协议级白名单校验。
    rendered = _MARKDOWN.render(_normalise_display_math(str(value).strip()))
    return bleach.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
    )


def render_reply_markdown(value: object, replied_to_username: object) -> str:
    """渲染回复，避免重复显示结构化的开头 @提及。

    旧回复记录可能因为浏览器预填充而把 ``@username`` 写入正文。模板已经单独
    展示被回复用户，所以这里只移除开头完全匹配的提及；数据库中的原文保持不变。
    """
    source = str(value or "")
    username = str(replied_to_username or "").strip()
    prefix = f"@{username}"

    if username and source.startswith(prefix):
        remainder = source[len(prefix):]
        if not remainder or remainder[0].isspace():
            source = remainder.lstrip()

    return render_comment_markdown(source)
