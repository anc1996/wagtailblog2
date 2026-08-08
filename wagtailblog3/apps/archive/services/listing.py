"""Shared query and pagination helpers for public archive listings."""

from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import Http404
from django.urls import reverse

from blog.models import BlogPage, BlogTagIndexPage


ARCHIVE_PAGE_SIZE = 10


def _validate_scope(*, year: int, month: int | None = None) -> None:
    """Reject invalid archive path parameters before they reach date filters."""
    if not 1 <= year <= 9999:
        raise Http404("Invalid archive year")

    if month is not None and not 1 <= month <= 12:
        raise Http404("Invalid archive month")


def _get_month_date_range(*, year: int, month: int) -> tuple[date, date]:
    start_date = date(year, month, 1)
    if month == 12:
        return start_date, date(year + 1, 1, 1)
    return start_date, date(year, month + 1, 1)


def get_archive_listing_context(*, year: int, month: int | None, query_params):
    """Build the normalized, paginated context for an archive scope."""
    _validate_scope(year=year, month=month)

    search_query = (query_params.get("search") or "").strip()
    page_number = query_params.get("page")

    pages_queryset = (
        BlogPage.objects.live()
        .public()
        .select_related("featured_image")
        .prefetch_related("tags")
    )
    if month is None:
        pages_queryset = pages_queryset.filter(date__year=year)
    elif year == date.max.year and month == 12:
        pages_queryset = pages_queryset.filter(date__year=year, date__month=month)
    else:
        start_date, end_date = _get_month_date_range(year=year, month=month)
        pages_queryset = pages_queryset.filter(date__gte=start_date, date__lt=end_date)

    if search_query:
        pages_queryset = pages_queryset.filter(title__icontains=search_query)

    paginator = Paginator(pages_queryset.order_by("-date", "-pk"), ARCHIVE_PAGE_SIZE)
    try:
        pages = paginator.page(page_number)
    except PageNotAnInteger:
        pages = paginator.page(1)
    except EmptyPage:
        pages = paginator.page(paginator.num_pages)

    archive_url_name = "archive:month_archive" if month is not None else "archive:year_archive"
    archive_url_args = (year, month) if month is not None else (year,)
    results_api_name = (
        "archive:month_archive_results_api"
        if month is not None
        else "archive:year_archive_results_api"
    )

    return {
        "archive_scope": "month" if month is not None else "year",
        "year": year,
        "month": month,
        "month_name": date(year, month, 1).strftime("%B") if month is not None else "",
        "archive_url": reverse(archive_url_name, args=archive_url_args),
        "results_api_url": reverse(results_api_name, args=archive_url_args),
        "pages": pages,
        "pagination_range": list(
            paginator.get_elided_page_range(
                number=pages.number,
                on_each_side=2,
                on_ends=1,
            )
        ),
        "search_query": search_query,
        "total_count": paginator.count,
        "blog_tag_index_page": BlogTagIndexPage.objects.live().public().first(),
    }


def get_archive_canonical_url(*, context) -> str:
    """Return the normalized public URL for the current archive result state."""
    params = {}
    if context["search_query"]:
        params["search"] = context["search_query"]
    if context["pages"].number > 1:
        params["page"] = context["pages"].number

    query_string = urlencode(params)
    return f'{context["archive_url"]}?{query_string}' if query_string else context["archive_url"]
