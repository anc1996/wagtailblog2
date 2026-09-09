"""Public archive views and their server-rendered fragment APIs."""

import logging

from django.db.models import Count
from django.db.models.functions import TruncMonth, TruncYear
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt

from blog.models import BlogPage

from .services.listing import (
    ARCHIVE_PAGE_SIZE,
    get_archive_canonical_url,
    get_archive_listing_context,
)


logger = logging.getLogger(__name__)


def get_archive_data(site_id: int = 1, locale_id: int = 1):
    """Return the archive tree used by the existing archive APIs and admin UI (delegates to ArchiveSidebarService)."""
    from blog.services.sidebar_cache import ArchiveSidebarService
    return ArchiveSidebarService.get_archive_aggregate(site_id, locale_id)


def archives_api(request):
    """Return the existing archive tree JSON contract."""
    data = []
    for year, year_data in get_archive_data().items():
        data.append(
            {
                "year": year,
                "count": year_data["count"],
                "months": [
                    {
                        "month": month,
                        "name": month_data["name"],
                        "display_name": month_data["display_name"],
                        "count": month_data["count"],
                    }
                    for month, month_data in year_data["months"].items()
                ],
            }
        )
    return JsonResponse({"archives": data})


def _render_archive_page(request, *, template_name, year, month=None):
    context = get_archive_listing_context(
        year=year,
        month=month,
        query_params=request.GET,
    )
    return render(request, template_name, context)


def year_archive(request, year):
    """Render the server-side year archive page."""
    return _render_archive_page(
        request,
        template_name="archive/year_archive.html",
        year=year,
    )


def month_archive(request, year, month):
    """Render the server-side month archive page."""
    return _render_archive_page(
        request,
        template_name="archive/month_archive.html",
        year=year,
        month=month,
    )


def _archive_results_api(request, *, year, month=None):
    if request.method != "GET":
        response = JsonResponse(
            {
                "ok": False,
                "error": {
                    "code": "method_not_allowed",
                    "message": "Only GET requests are supported.",
                },
            },
            status=405,
        )
        response["Allow"] = "GET"
        response["Cache-Control"] = "private, no-store"
        return response

    try:
        context = get_archive_listing_context(
            year=year,
            month=month,
            query_params=request.GET,
        )
    except Http404:
        response = JsonResponse(
            {
                "ok": False,
                "error": {
                    "code": "archive_not_found",
                    "message": "The requested archive does not exist.",
                },
            },
            status=404,
        )
    except Exception:
        logger.exception(
            "archive_results_api_failed year=%s month=%s",
            year,
            month,
        )
        response = JsonResponse(
            {
                "ok": False,
                "error": {
                    "code": "archive_results_unavailable",
                    "message": "归档结果暂时无法加载，请稍后重试。",
                },
            },
            status=500,
        )
    else:
        pages = context["pages"]
        response = JsonResponse(
            {
                "ok": True,
                "data": {
                    "scope": context["archive_scope"],
                    "year": context["year"],
                    "month": context["month"],
                    "search": context["search_query"],
                    "result_count": context["total_count"],
                    "html": render_to_string(
                        "archive/partials/_archive_results.html",
                        context,
                        request=request,
                    ),
                    "pagination": {
                        "page": pages.number,
                        "page_size": ARCHIVE_PAGE_SIZE,
                        "total_pages": pages.paginator.num_pages,
                        "has_previous": pages.has_previous(),
                        "has_next": pages.has_next(),
                    },
                    "canonical_url": get_archive_canonical_url(context=context),
                },
            }
        )

    response["Cache-Control"] = "private, no-store"
    return response


@csrf_exempt
def year_archive_results_api(request, year):
    """Return the year archive's server-rendered results fragment as JSON."""
    return _archive_results_api(request, year=year)


@csrf_exempt
def month_archive_results_api(request, year, month):
    """Return the month archive's server-rendered results fragment as JSON."""
    return _archive_results_api(request, year=year, month=month)
