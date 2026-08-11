"""搜索建议的独立数据通道。"""

from __future__ import annotations

import re

from django.conf import settings
from django.db.models import Count
from wagtail.contrib.search_promotions.models import Query

from blog.models import BlogPage
from search.services.content_query import content_search_read_alias, get_content_search_serving_target
from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import _read_error, _response_body, get_content_search_client


_CONTROL_OR_MARKUP = re.compile(r"[\x00-\x1f\x7f<>]")


def _safe_suggestion_text(value):
    value = str(value or "").strip()
    if len(value) < 2 or len(value) > 100 or _CONTROL_OR_MARKUP.search(value):
        return ""
    return value


def get_popular_query_suggestions(query_string, limit=5):
    """热门搜索词仍来自统计表，但只在新建议 flag 开启时公开。"""

    if not query_string or len(query_string) < 2:
        return []
    try:
        suggestions = (
            Query.objects.filter(query_string__icontains=query_string)
            .annotate(total_hits_count=Count("daily_hits"))
            .filter(total_hits_count__gt=0)
            .order_by("-total_hits_count", "query_string")[:limit]
        )
        return [
            {"query": safe_query, "hits": item.total_hits_count, "source": "popular"}
            for item in suggestions
            if (safe_query := _safe_suggestion_text(item.query_string))
        ]
    except Exception:
        return []


def _title_suggestion_alias():
    alias = getattr(settings, "CONTENT_SEARCH_TITLE_SUGGESTIONS_READ_ALIAS", "")
    if not alias:
        return ""
    try:
        validate_content_index_name(alias, settings.CONTENT_SEARCH_INDEX_PREFIX)
    except ValueError:
        return ""
    return alias


def get_public_title_suggestions(query_string, limit=5, locale=None):
    """从独立标题索引读取候选，再用 live/public 页面集合做最终边界校验。"""

    query_string = _safe_suggestion_text(query_string)
    alias = _title_suggestion_alias()
    if not query_string or not alias:
        return []
    try:
        target = get_content_search_serving_target()
        filters = [{"term": {"searchable": True}}]
        if locale:
            filters.append({"term": {"locale_id": locale}})
        response = get_content_search_client(target).search(
            index=alias,
            query={
                "bool": {
                    "must": [{"match_phrase_prefix": {"title": query_string}}],
                    "filter": filters,
                }
            },
            size=min(max(int(limit), 1), 10),
            sort=[{"_score": "desc"}, {"page_id": "asc"}],
            track_total_hits=False,
            source_includes=("page_id",),
        )
        hits = (_response_body(response).get("hits") or {}).get("hits") or []
        page_ids = []
        for hit in hits:
            try:
                page_ids.append(int((hit.get("_source") or {}).get("page_id", hit.get("_id"))))
            except (TypeError, ValueError):
                continue
        public_pages = {
            page.id: page
            for page in BlogPage.objects.live().public().filter(pk__in=page_ids)
        }
        return [
            {"query": public_pages[page_id].title, "page_id": page_id, "source": "title"}
            for page_id in page_ids
            if page_id in public_pages
        ]
    except Exception as error:
        raise _read_error(error) from error


def get_public_search_suggestions(query_string, limit=5, locale=None):
    """组合两个可独立关闭的建议通道，并按标题优先、查询词去重。"""

    results = []
    if getattr(settings, "SEARCH_POPULAR_SUGGESTIONS_ENABLED", False):
        results.extend(get_popular_query_suggestions(query_string, limit))
    if getattr(settings, "SEARCH_TITLE_SUGGESTIONS_ENABLED", False):
        results.extend(get_public_title_suggestions(query_string, limit, locale))
    unique = []
    seen = set()
    for item in results:
        value = item.get("query")
        if value and value not in seen:
            unique.append(item)
            seen.add(value)
        if len(unique) >= limit:
            break
    return unique
