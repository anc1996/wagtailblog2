"""Read-only query service for the public tag index page."""

from django.core.paginator import Paginator
from django.db.models import Count
from django.utils.dateparse import parse_date
from taggit.models import Tag

from blog.models import BlogPage


def normalise_date_filter(value):
    """Return a canonical ISO date string and parsed date, or an empty filter."""
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
    query_params,
    tag_page_size,
    article_page_size,
):
    """Build the shared context for full-page and asynchronous tag listings."""
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
