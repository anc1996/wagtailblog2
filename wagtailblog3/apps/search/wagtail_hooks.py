"""搜索应用的 Wagtail 后台扩展。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import path, reverse
from wagtail import hooks
from wagtail.admin.menu import MenuItem

from .analytics import AnalyticsValidationError, SearchAnalytics


@hooks.register("register_admin_menu_item")
def register_search_analytics_menu() -> MenuItem:
    """注册仅 staff 可访问的搜索统计入口。"""

    return MenuItem("搜索分析", reverse("search_analytics"), icon_name="search", order=800)


def _parse_positive_int(value: str | None, default: int) -> int:
    """将分页和 Top N 参数收敛为安全的正整数。"""

    try:
        return max(int(value or default), 1)
    except (TypeError, ValueError):
        return default


def _add_record_urls(payload: dict[str, Any]) -> dict[str, Any]:
    """仅在展示层构造公开搜索 URL，避免聚合服务依赖 URL 配置。"""

    for record in payload["records"]:
        record["url"] = f"{reverse('search:search')}?{urlencode({'query': record['query'], 'type': 'all'})}"
    return payload


@hooks.register("register_admin_urls")
def register_search_admin_urls():
    """注册后台页和单一 dashboard JSON 接口。"""

    def _json_method_error() -> JsonResponse:
        response = JsonResponse(
            {"error": {"code": "method_not_allowed", "message": "仅支持 GET 请求"}},
            status=405,
        )
        response["Cache-Control"] = "private, no-store"
        return response

    def _private_json(response: HttpResponse) -> HttpResponse:
        """为分析 JSON 禁止共享缓存，避免 staff 数据被代理缓存。"""

        if isinstance(response, JsonResponse):
            response["Cache-Control"] = "private, no-store"
        return response

    def _search_analytics_response(
        request: HttpRequest, forced_view: str | None = None
    ) -> HttpResponse:
        """根据接口用途生成页面或局部 JSON；调用前已完成 staff 权限检查。"""

        view = forced_view or request.GET.get("view")
        if forced_view and request.method != "GET":
            return _json_method_error()
        if view == "today":
            # 旧接口保留模糊词筛选契约；新 dashboard 的热词下钻使用精确规范化词。
            page_size = min(
                max(_parse_positive_int(request.GET.get("page_size"), 20), 10), 100
            )
            page = Paginator(
                SearchAnalytics.get_today_searches((request.GET.get("q") or "")[:100]),
                page_size,
            ).get_page(_parse_positive_int(request.GET.get("page"), 1))
            payload = {
                "today_searches": [
                    {
                        "query": row.query.query_string,
                        "hits": row.hits,
                        "url": f"{reverse('search:search')}?{urlencode({'query': row.query.query_string, 'type': 'all'})}",
                    }
                    for row in page.object_list
                ],
                "query": request.GET.get("q", ""),
                "pagination": {
                    "page": page.number,
                    "total_pages": page.paginator.num_pages,
                    "total_count": page.paginator.count,
                    "page_size": page_size,
                    "has_previous": page.has_previous(),
                    "has_next": page.has_next(),
                },
            }
            return JsonResponse(payload)

        if view == "dashboard":
            try:
                payload = _add_record_urls(
                    SearchAnalytics.build_dashboard(
                        range_key=request.GET.get("range", "month"),
                        start_date=request.GET.get("from"),
                        end_date=request.GET.get("to"),
                        query=(request.GET.get("q") or "")[:100],
                        page_number=_parse_positive_int(request.GET.get("page"), 1),
                        page_size=_parse_positive_int(request.GET.get("page_size"), 20),
                        top_n=_parse_positive_int(request.GET.get("top_n"), 10),
                        granularity=request.GET.get("granularity"),
                        analysis_query=(request.GET.get("analysis_q") or "")[:100],
                        records_query=(request.GET.get("records_q") or request.GET.get("q") or "")[:100],
                    )
                )
            except AnalyticsValidationError as error:
                return JsonResponse(
                    {"error": {"code": "invalid_analytics_range", "message": str(error)}},
                    status=400,
                )
            return JsonResponse(payload)

        if view == "records":
            try:
                records_range = request.GET.get("records_range") or request.GET.get("range", "last30")
                _, start_date, end_date = SearchAnalytics.resolve_date_range(
                    records_range,
                    request.GET.get("records_from") or request.GET.get("from"),
                    request.GET.get("records_to") or request.GET.get("to"),
                )
                page_size = min(max(_parse_positive_int(request.GET.get("page_size"), 20), 10), 100)
                page = Paginator(
                    SearchAnalytics.get_records(
                        start_date,
                        end_date,
                        (request.GET.get("records_q") or request.GET.get("q") or "")[:100],
                        exact_query=False,
                    ),
                    page_size,
                ).get_page(_parse_positive_int(request.GET.get("page"), 1))
                payload = {
                    "range": {"from": start_date.isoformat(), "to": end_date.isoformat()},
                    "records": [
                        {"date": row.date.isoformat(), "query": row.query.query_string, "hits": row.hits}
                        for row in page.object_list
                    ],
                    "pagination": {
                        "page": page.number,
                        "total_pages": page.paginator.num_pages,
                        "total_count": page.paginator.count,
                        "page_size": page_size,
                        "has_previous": page.has_previous(),
                        "has_next": page.has_next(),
                    },
                }
                return JsonResponse(_add_record_urls(payload))
            except AnalyticsValidationError as error:
                return JsonResponse(
                    {"error": {"code": "invalid_analytics_range", "message": str(error)}},
                    status=400,
                )

        return render(request, "search/admin/analytics.html")

    @staff_member_required
    def search_analytics_view(request: HttpRequest) -> HttpResponse:
        return _private_json(_search_analytics_response(request))

    @staff_member_required
    def search_analytics_dashboard_view(request: HttpRequest) -> HttpResponse:
        return _private_json(_search_analytics_response(request, "dashboard"))

    @staff_member_required
    def search_analytics_records_view(request: HttpRequest) -> HttpResponse:
        return _private_json(_search_analytics_response(request, "records"))

    return [
        path("search-analytics/", search_analytics_view, name="search_analytics"),
        path(
            "search-analytics/dashboard/",
            search_analytics_dashboard_view,
            name="search_analytics_dashboard",
        ),
        path(
            "search-analytics/records/",
            search_analytics_records_view,
            name="search_analytics_records",
        ),
    ]
