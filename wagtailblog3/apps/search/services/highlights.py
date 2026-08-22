"""搜索结果高亮的共享安全协议。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from django.utils.html import conditional_escape, strip_tags
from django.utils.safestring import mark_safe


HIGHLIGHT_START_TAG = "__WAGTAIL_SEARCH_MARK_START__"
HIGHLIGHT_END_TAG = "__WAGTAIL_SEARCH_MARK_END__"
MAX_HIGHLIGHT_FRAGMENTS = 3
HIGHLIGHT_FRAGMENT_SIZE = 160
BODY_HIGHLIGHT_MAX_ANALYZER_OFFSET = 100_000


def safe_highlight_fragment(fragment: object) -> str:
    """只保留转义文本和服务端生成的 mark 标签，拒绝原始 HTML。"""
    """只保留转义文本和服务端生成的 mark，拒绝索引中的原始 HTML。"""

    text = strip_tags(str(fragment))
    text = conditional_escape(text)
    text = text.replace(HIGHLIGHT_START_TAG, "<mark>")
    text = text.replace(HIGHLIGHT_END_TAG, "</mark>")
    return mark_safe(text)


def build_highlight_fields(field_labels: Sequence[tuple[str, str]]) -> dict[str, dict[str, int]]:
    """根据字段标签构造 ES 高亮参数，并限制正文分析偏移。"""
    """统一旧索引和独立索引的片段数量、长度及正文分析上限。"""

    return {
        field_name: {
            "number_of_fragments": 0 if label == "title" else MAX_HIGHLIGHT_FRAGMENTS,
            "fragment_size": HIGHLIGHT_FRAGMENT_SIZE,
            "no_match_size": 0,
            **(
                {"max_analyzed_offset": BODY_HIGHLIGHT_MAX_ANALYZER_OFFSET}
                if label == "body_text"
                else {}
            ),
        }
        for field_name, label in field_labels
    }


def extract_safe_highlights(
    hit: Mapping[str, object], field_labels: Sequence[tuple[str, str]]
) -> tuple[str, tuple[str, ...], str]:
    """从单个 ES hit 提取已转义且去重的展示片段。"""
    """从单个 ES hit 提取经过转义和数量限制的展示片段。"""

    highlight = hit.get("highlight") or {}
    fragments = []
    matched_field = ""
    title_fragment = ""
    for field_name, label in field_labels:
        raw_fragments = highlight.get(field_name, [])
        if isinstance(raw_fragments, str):
            raw_fragments = [raw_fragments]
        for raw_fragment in raw_fragments[:MAX_HIGHLIGHT_FRAGMENTS]:
            fragment = safe_highlight_fragment(raw_fragment)
            if not fragment or fragment in fragments:
                continue
            matched_field = matched_field or label
            if label == "title" and not title_fragment:
                title_fragment = fragment
            elif label != "title":
                fragments.append(fragment)
    return matched_field, tuple(fragments), title_fragment
