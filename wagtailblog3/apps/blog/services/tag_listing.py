"""公开标签索引和标签文章列表的只读查询服务。"""

from typing import Any
from django.core.paginator import Paginator
from django.db.models import Count
from django.utils.dateparse import parse_date
from taggit.models import Tag

from blog.models import BlogPage


def normalise_date_filter(value: object) -> tuple[str, Any]:
    """规范化日期过滤条件。

    返回 ISO 日期字符串和解析后的日期对象；空值、格式错误或不可解析日期返回空字符串
    与 ``None``，让调用方统一忽略无效筛选而不把异常暴露给列表页。
    """
    text = (value or "").strip()
    try:
        parsed = parse_date(text) if text else None
    except ValueError:
        parsed = None
    if parsed is None:
        return "", None
    return parsed.isoformat(), parsed


def get_tag_index_context(
    *,
    query_params: Any,
    tag_page_size: int,
    article_page_size: int,
) -> dict[str, Any]:
    """构造完整页面和异步标签列表共用的上下文。

    当存在 tag slug 时查询公开文章并按日期分页；没有 slug 时查询仍被公开文章引用的
    标签并按引用数排序。所有分支都只使用 live/public 页面，避免草稿或受限页面泄露。
    """
    requested_tag_slug = (query_params.get("tag") or "").strip()
    search_query = (query_params.get("q") or "").strip()
    start_date, start_date_value = normalise_date_filter(
        query_params.get("start_date")
    )
    end_date, end_date_value = normalise_date_filter(query_params.get("end_date"))
    page_number = query_params.get("page")

    context = {
        "mode": "tag_list",
        "requested_tag_slug": requested_tag_slug,
        "search_query": search_query,
        "start_date": start_date,
        "end_date": end_date,
        "current_tag": None,
        "paged_items": None,
        "paginator": None,
        "is_paginated": False,
        "total_results": 0,
    }

    if requested_tag_slug:
        current_tag = Tag.objects.filter(slug=requested_tag_slug).first()
        if current_tag is None:
            context["mode"] = "tag_missing"
            return context

        articles = (
            BlogPage.objects.live()
            .public()
            .filter(tags=current_tag)
            .select_related("featured_image")
        )
        if search_query:
            articles = articles.filter(title__icontains=search_query)
        if start_date_value:
            articles = articles.filter(date__gte=start_date_value)
        if end_date_value:
            articles = articles.filter(date__lte=end_date_value)
        articles = articles.order_by("-date", "-pk")

        paginator = Paginator(articles, article_page_size)
        page_obj = paginator.get_page(page_number)
        context.update(
            {
                "mode": "tag_detail",
                "current_tag": current_tag,
                "paged_items": page_obj,
                "paginator": paginator,
                "is_paginated": page_obj.has_other_pages(),
                "total_results": paginator.count,
            }
        )
        return context

    live_public_page_ids = BlogPage.objects.live().public().values("id")
    tags = Tag.objects.filter(
        blog_blogpagetag_items__content_object_id__in=live_public_page_ids
    ).annotate(count=Count("blog_blogpagetag_items"))
    if search_query:
        tags = tags.filter(name__icontains=search_query)
    tags = tags.order_by("-count", "name", "pk")

    paginator = Paginator(tags, tag_page_size)
    page_obj = paginator.get_page(page_number)
    context.update(
        {
            "paged_items": page_obj,
            "paginator": paginator,
            "is_paginated": page_obj.has_other_pages(),
            "total_results": paginator.count,
        }
    )
    return context
