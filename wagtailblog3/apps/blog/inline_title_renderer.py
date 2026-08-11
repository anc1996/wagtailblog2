"""Safe, single-line Markdown rendering for public blog titles."""

import html
import re
from html.parser import HTMLParser

import markdown
import nh3
from django.core.exceptions import ValidationError
from django.utils.encoding import smart_str
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe


class _RenderedTagCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


class _TitleHighlightParser(HTMLParser):
    """只在已经安全渲染的标题文本节点中插入 mark。"""

    def __init__(self, terms):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.terms = tuple(sorted(set(terms), key=len, reverse=True))
        self.math_depth = 0

    def handle_starttag(self, tag, attrs):
        self.parts.append(self.get_starttag_text())
        if tag == "span" and dict(attrs).get("class") == "arithmatex":
            self.math_depth += 1

    def handle_startendtag(self, tag, attrs):
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")
        if tag == "span" and self.math_depth:
            self.math_depth -= 1

    def handle_data(self, data):
        if self.math_depth or not self.terms:
            self.parts.append(html.escape(data, quote=False))
            return
        pattern = re.compile("|".join(re.escape(term) for term in self.terms), re.IGNORECASE)
        chunks = []
        cursor = 0
        for match in pattern.finditer(data):
            chunks.append(html.escape(data[cursor:match.start()], quote=False))
            chunks.append("<mark>")
            chunks.append(html.escape(match.group(0), quote=False))
            chunks.append("</mark>")
            cursor = match.end()
        chunks.append(html.escape(data[cursor:], quote=False))
        self.parts.append("".join(chunks))

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def render(self):
        return "".join(self.parts)

    def handle_startendtag(self, tag, attrs):
        self.tags.append(tag)


class InlineTitleRenderer:
    """Render the intentionally small Markdown subset allowed in titles."""

    ALLOWED_TAGS = {"code", "strong", "em", "del", "sup", "sub", "span"}
    MARKDOWN_TAGS = ALLOWED_TAGS | {"p"}
    EXTENSIONS = [
        "pymdownx.arithmatex",
        "pymdownx.caret",
        "pymdownx.tilde",
    ]
    EXTENSION_CONFIGS = {
        "pymdownx.arithmatex": {"generic": True},
    }
    _HTML_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
    _FENCE_RE = re.compile(r"(?:`{3,}|~{3,})")
    _DISPLAY_MATH_RE = re.compile(r"\$\$|\\\[|\\\]")
    _MATH_WRAPPER_RE = re.compile(r"\\\((.*?)\\\)")
    _INLINE_CODE_RE = re.compile(r"`+[^`\n]+?`+")

    @classmethod
    def _markdown(cls, source):
        return markdown.markdown(
            smart_str(source or ""),
            extensions=cls.EXTENSIONS,
            extension_configs=cls.EXTENSION_CONFIGS,
            output_format="html5",
        )

    @classmethod
    def validate_source(cls, source):
        source = smart_str(source or "").strip()
        if not source:
            return ""
        if "\n" in source or "\r" in source:
            raise ValidationError("格式化标题只能包含一行。")
        structural_source = cls._INLINE_CODE_RE.sub("", source)
        if cls._HTML_RE.search(structural_source):
            raise ValidationError("格式化标题不允许使用 HTML。")
        if cls._FENCE_RE.search(structural_source):
            raise ValidationError("格式化标题不允许代码块。")
        if cls._DISPLAY_MATH_RE.search(structural_source):
            raise ValidationError("格式化标题只允许 $...$ 行内公式。")

        rendered = cls._markdown(source)
        collector = _RenderedTagCollector()
        collector.feed(rendered)
        forbidden = sorted(set(collector.tags) - cls.MARKDOWN_TAGS)
        if forbidden:
            raise ValidationError(
                "格式化标题包含不允许的结构：%(tags)s。",
                params={"tags": ", ".join(forbidden)},
            )
        return rendered

    @classmethod
    def render(cls, source):
        source = smart_str(source or "").strip()
        if not source:
            return mark_safe("")
        rendered = cls.validate_source(source)
        if rendered.startswith("<p>") and rendered.endswith("</p>"):
            rendered = rendered[3:-4]
        cleaned = nh3.clean(
            rendered,
            tags=cls.ALLOWED_TAGS,
            attributes={"span": {"class"}},
            link_rel=None,
        )
        return mark_safe(cleaned)

    @classmethod
    def render_highlighted(cls, source, query):
        """保留标题原有 Markdown/公式结构，只在普通文本节点中加入 mark。"""

        rendered = str(cls.render(source))
        terms = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", smart_str(query or ""))
        if not terms:
            return mark_safe(rendered)
        parser = _TitleHighlightParser(terms)
        parser.feed(rendered)
        parser.close()
        return mark_safe(parser.render())

    @classmethod
    def plain_text(cls, source):
        rendered = smart_str(cls.render(source))
        return cls._plain_text_from_rendered(rendered)

    @classmethod
    def _plain_text_from_rendered(cls, rendered):
        plain = html.unescape(strip_tags(rendered))
        plain = cls._MATH_WRAPPER_RE.sub(r"\1", plain)
        return " ".join(plain.split())
