"""Project-owned Markdown rendering for blog content."""

import re
from collections import defaultdict
from copy import deepcopy

import markdown
import nh3
from django.conf import settings
from django.utils.encoding import smart_str
from django.utils.safestring import mark_safe


class MarkdownRenderer:
    """Render Markdown without changing the stored source string."""

    _default_tags = {
        "a", "abbr", "blockquote", "br", "caption", "code", "colgroup",
        "dd", "del", "details", "div", "dl", "dt", "em", "h1", "h2",
        "h3", "h4", "h5", "h6", "hr", "img", "ins", "li", "mark",
        "ol", "p", "pre", "span", "strong", "sub", "summary", "sup",
        "table", "tbody", "td", "tfoot", "th", "thead", "tr", "tt",
        "u", "ul",
    }
    _default_attributes = {
        "*": {"class", "id", "style"},
        "a": {"href", "title", "target", "rel"},
        "img": {"src", "alt", "title", "width", "height", "loading"},
        "code": {"class", "data-lang"},
        "pre": {"class"},
        "div": {"class", "id"},
        "span": {"class", "id", "style"},
        "table": {"class"},
    }
    _default_styles = {
        "color", "background-color", "font-family", "font-weight",
        "text-align", "width", "height", "margin", "padding",
        "font-size", "border",
    }

    @classmethod
    def _settings(cls):
        return getattr(settings, "BLOG_MARKDOWN", {})

    @classmethod
    def prepare_source(cls, source):
        """Keep the existing list repair behavior without changing storage."""
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
            # The project explicitly allows ``a[rel]``. nh3 rejects using
            # both that attribute and its automatic link_rel injection.
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
        del context  # Kept for Wagtail's block rendering signature.
        rendered = markdown.markdown(
            cls.prepare_source(source),
            **cls.markdown_kwargs(),
        )
        return mark_safe(nh3.clean(rendered, **cls.nh3_kwargs()))
