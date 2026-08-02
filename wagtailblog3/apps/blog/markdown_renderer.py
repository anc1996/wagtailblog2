"""博客正文的 Markdown 渲染器。"""

import re
from collections import defaultdict
from copy import deepcopy

import markdown
import nh3
from django.conf import settings
from django.utils.encoding import smart_str
from django.utils.safestring import mark_safe


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
        "table": {"class"},
    } # 元素
    
    _default_styles = {
        "color", "background-color", "font-family", "font-weight",
        "text-align", "width", "height", "margin", "padding",
        "font-size", "border",
    } # 属性

    @classmethod
    def _settings(cls):
        return getattr(settings, "BLOG_MARKDOWN", {})

    @classmethod
    def prepare_source(cls, source):
        """修复旧内容中的列表缩进，只作用于渲染副本。"""
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
    def markdown_kwargs(cls):
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
    def nh3_kwargs(cls):
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
    def render(cls, source, context=None):
        # 保留 Wagtail 块渲染签名，但渲染规则只依赖正文和全局配置。
        del context
        # 先由 Markdown 扩展生成 HTML，再由 nh3 做白名单清洗，防止原始正文携带脚本或危险协议。
        rendered = markdown.markdown(
            cls.prepare_source(source),
            **cls.markdown_kwargs(),
        )
        return mark_safe(nh3.clean(rendered, **cls.nh3_kwargs()))
