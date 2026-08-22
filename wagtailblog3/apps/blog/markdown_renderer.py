"""博客正文的 Markdown 渲染器。"""

import re
from collections import defaultdict
from copy import deepcopy
from html.parser import HTMLParser
from typing import Any

import markdown
import nh3
from django.conf import settings
from django.utils.encoding import smart_str
from django.utils.safestring import mark_safe
from wagtail.rich_text import EmbedRewriter, LinkRewriter
from wagtail.images.formats import get_image_format
from wagtail.images.rich_text import ImageEmbedHandler
from wagtail.rich_text.pages import PageLinkHandler


_WAGTAIL_PAGE_LINK_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_WAGTAIL_IMAGE_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
_DISPLAY_MATH_RE = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.DOTALL)
_INLINE_MATH_RE = re.compile(
    r"(?<![\\$])\$(?!\$)(.+?)(?<![\\$])\$(?!\$)",
    re.DOTALL,
)


class _RawHtmlMathParser(HTMLParser):
    """为原生 HTML 表格中的公式补充 KaTeX 自动渲染标记。"""

    _ignored_tags = {"code", "pre", "script", "style", "textarea"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(self.get_starttag_text())
        if tag.lower() in self._ignored_tags:
            self._ignored_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")
        if tag.lower() in self._ignored_tags and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or "$" not in data:
            self.parts.append(data)
            return

        def display_replacement(match: re.Match[str]) -> str:
            return '<span class="arithmatex">\\[' + match.group(1).strip() + "\\]</span>"

        data = _DISPLAY_MATH_RE.sub(display_replacement, data)
        data = _INLINE_MATH_RE.sub(
            lambda match: '<span class="arithmatex">\\('
            + match.group(1).strip()
            + "\\)</span>",
            data,
        )
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def render(self) -> str:
        return "".join(self.parts)


class _PageLinkRewriter(LinkRewriter):
    """只解析 Vditor 产生的 Wagtail 页面链接，保留其他锚点原样。"""

    def get_tag_type_from_attrs(self, attrs: dict[str, str]) -> str | None:
        return "page" if attrs.get("linktype") == "page" else None


class _ImageEmbedRewriter(EmbedRewriter):
    """只展开格式和 ID 均有效的 Wagtail 图片 embed，其他 embed 留给 nh3 处理。"""

    def get_tag_type_from_attrs(self, attrs: dict[str, str]) -> str | None:
        return "image" if attrs.get("embedtype") == "image" else None

    def get_tag_replacements(self, tag_type: str | None, attrs_list: list[dict[str, str]]) -> list[str]:
        # EmbedRewriter drops unknown embed types by default. Markdown may contain
        # project-specific embeds, so leave those untouched for nh3 to handle.
        if tag_type is None:
            return []

        replacements = [""] * len(attrs_list)
        valid_indexes = []
        valid_attrs = []
        for index, attrs in enumerate(attrs_list):
            image_id = attrs.get("id", "")
            format_name = attrs.get("format", "")
            if not _WAGTAIL_IMAGE_ID_RE.fullmatch(image_id):
                continue
            try:
                get_image_format(format_name)
            except (KeyError, TypeError):
                continue
            valid_indexes.append(index)
            valid_attrs.append(attrs)

        if not valid_attrs:
            return replacements

        expanded = ImageEmbedHandler.expand_db_attributes_many(valid_attrs)
        for index, replacement in zip(valid_indexes, expanded):
            replacements[index] = replacement
        return replacements


class MarkdownRenderer:
    """在不改变原始正文的前提下渲染 Markdown。"""

    _default_tags = {
        "a", "abbr", "blockquote", "br", "caption", "code", "colgroup",
        "dd", "del", "details", "div", "dl", "dt", "em", "h1", "h2",
        "h3", "h4", "h5", "h6", "hr", "img", "ins", "li", "mark",
        "ol", "p", "pre", "span", "strong", "sub", "summary", "sup",
        "table", "tbody", "td", "tfoot", "th", "thead", "tr", "tt",
        "u", "ul",
    }# 元素
    
    _default_attributes = {
        "*": {"class", "id", "style"},
        "a": {"href", "title", "target", "rel"},
        "img": {"src", "alt", "title", "width", "height", "loading"},
        "code": {"class", "data-lang"},
        "pre": {"class"},
        "div": {"class", "id"},
        "span": {"class", "id", "style"},
        # 保留 HTML 表格的合并单元格语义，否则 rowspan/colspan 被清理后会导致列错位。
        "table": {"class", "role", "aria-label"},
        "thead": {"class"},
        "tbody": {"class"},
        "tfoot": {"class"},
        "tr": {"class"},
        "th": {"class", "rowspan", "colspan", "scope", "headers"},
        "td": {"class", "rowspan", "colspan", "headers"},
        "colgroup": {"class", "span"},
        "col": {"class", "span"},
    } # 元素
    
    _default_styles = {
        "color", "background-color", "font-family", "font-weight",
        "text-align", "width", "height", "margin", "padding",
        "font-size", "border",
    } # 属性

    @classmethod
    def _settings(cls) -> dict[str, Any]:
        """读取 Markdown 渲染配置；配置只影响当前渲染，不修改原文。"""
        return getattr(settings, "BLOG_MARKDOWN", {})

    @classmethod
    def prepare_source(cls, source: object) -> str:
        """修复旧内容中的列表缩进，只作用于渲染副本。

        通过正则统一旧内容的二至三空格缩进，并在相邻列表层级缺少空行时补出边界；
        这样 Markdown 解析器能稳定识别嵌套列表，同时不改变数据库中的原始正文。
        """
        # 某些旧正文把引用符、缩进和列表标记组合在同一行；统一成四空格后才能被 Markdown 正确识别。
        source = smart_str(source or "")
        source = re.sub(
            r"^(>[\s]*)?( {2,3})([-\*\+]\s|\d+\.\s)",
            r"\1    \3",
            source,
            flags=re.MULTILINE,
        )
        lines = source.split("\n")
        fixed_lines = []
        list_pattern = re.compile(r"^(>[\s]*)?( {0,4})([-\*\+]\s|\d+\.\s)")
        for index, line in enumerate(lines):
            if index > 0:
                current = list_pattern.match(line)
                previous = list_pattern.match(lines[index - 1])
                previous_is_empty = not lines[index - 1].strip("> \t\r")
                if current and not previous and not previous_is_empty:
                    fixed_lines.append((current.group(1) or "").strip())
            fixed_lines.append(line)
        return "\n".join(fixed_lines)

    @classmethod
    def markdown_kwargs(cls) -> dict[str, Any]:
        config = cls._settings()
        kwargs = {
            "extensions": list(config.get("extensions", [])),
            "extension_configs": deepcopy(config.get("extension_configs", {})),
            "output_format": "html5",
        }
        if "tab_length" in config:
            kwargs["tab_length"] = config["tab_length"]
        return kwargs

    @classmethod
    def nh3_kwargs(cls) -> dict[str, Any]:
        config = cls._settings()
        configured_attributes = config.get("allowed_attributes", {})
        attributes = defaultdict(set)
        for tag, names in cls._default_attributes.items():
            attributes[tag].update(names)
        for tag, names in configured_attributes.items():
            attributes[tag].update(names)
        kwargs = {
            "tags": set(config.get("allowed_tags", cls._default_tags)),
            "attributes": dict(attributes),
            "filter_style_properties": set(
                config.get("allowed_styles", cls._default_styles)
            ),
            # 项目明确允许链接的 rel 属性；关闭 nh3 自动注入，避免同一属性同时配置导致冲突。
            "link_rel": None,
        }
        if config.get("allowed_settings_mode", "extend").lower() == "override":
            kwargs["tags"] = set(config.get("allowed_tags", []))
            kwargs["filter_style_properties"] = set(config.get("allowed_styles", []))
            kwargs["attributes"] = {
                tag: set(names) for tag, names in configured_attributes.items()
            }
        return kwargs

    @classmethod
    def expand_wagtail_page_links(cls, html: str) -> str:
        """在渲染时解析页面 ID，不触碰其他锚点。"""

        def expand_page_links(attrs_list: list[dict[str, str]]) -> list[str]:
            replacements = ["<a>"] * len(attrs_list)
            valid_indexes = []
            valid_attrs = []

            for index, attrs in enumerate(attrs_list):
                page_id = attrs.get("id", "")
                if _WAGTAIL_PAGE_LINK_ID_RE.fullmatch(page_id):
                    valid_indexes.append(index)
                    valid_attrs.append(attrs)

            if not valid_attrs:
                return replacements

            for index, replacement in zip(
                valid_indexes,
                PageLinkHandler.expand_db_attributes_many(valid_attrs),
            ):
                replacements[index] = replacement

            return replacements

        return _PageLinkRewriter(
            bulk_rules={"page": expand_page_links}
        )(html)

    @classmethod
    def expand_wagtail_image_embeds(cls, html: str) -> str:
        """展开经过 ID 和图片格式白名单校验的 Wagtail 图片 embed。"""
        return _ImageEmbedRewriter()(html)

    @classmethod
    def expand_raw_html_math(cls, html: str) -> str:
        """只在原生 HTML 文本节点中补 KaTeX 分隔符，代码节点保持不变。"""
        # Python-Markdown 不会进入原生 HTML 的文本节点，复杂表格中的公式需要在
        # 清理前补充与 arithmatex 相同的 KaTeX 分隔符；代码节点保持原样。
        parser = _RawHtmlMathParser()
        parser.feed(html)
        parser.close()
        return parser.render()

    @classmethod
    def render(cls, source: object, context: object = None) -> Any:
        """依次执行 Markdown 转 HTML、Wagtail embed 展开和 nh3 白名单清理。"""
        # 保留 Wagtail 块渲染签名，但渲染规则只依赖正文和全局配置。
        del context
        # 先由 Markdown 扩展生成 HTML，再由 nh3 做白名单清洗，防止原始正文携带脚本或危险协议。
        rendered = markdown.markdown(
            cls.prepare_source(source),
            **cls.markdown_kwargs(),
        )
        rendered = cls.expand_wagtail_page_links(rendered)
        rendered = cls.expand_wagtail_image_embeds(rendered)
        rendered = cls.expand_raw_html_math(rendered)
        return mark_safe(nh3.clean(rendered, **cls.nh3_kwargs()))
