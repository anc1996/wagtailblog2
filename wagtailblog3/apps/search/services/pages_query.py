"""普通页面与全站非博客搜索流的公开 QuerySet 构造入口。"""

from __future__ import annotations

from datetime import date, datetime

from django.db.models import QuerySet

from blog.models import BlogPage
from wagtail.models import Page


def build_public_non_blog_queryset(
    start_date: date | datetime | None = None,
    end_date: date | datetime | None = None,
    order_by: str | None = None,
) -> QuerySet[Page]:
    """全站非博客流保留指南，避免普通页面类型隔离导致全站遗漏指南。"""
    blog_ids = BlogPage.objects.values("id")
    queryset = Page.objects.live().public().exclude(id__in=blog_ids)
    if start_date:
        queryset = queryset.filter(first_published_at__gte=start_date)
    if end_date:
        queryset = queryset.filter(first_published_at__lte=end_date)
    if order_by in ("date", "-date"):
        queryset = queryset.order_by(
            "first_published_at" if order_by == "date" else "-first_published_at"
        )
    return queryset


def build_public_pages_queryset(
    start_date: date | datetime | None = None,
    end_date: date | datetime | None = None,
    order_by: str | None = None,
) -> QuerySet[Page]:
	"""返回已发布、公开且排除 BlogPage 和 GuidePage 的普通页面集合。"""
	blog_ids = BlogPage.objects.values("id")
	from guides.models import GuidePage

	guide_ids = GuidePage.objects.values("id")
	queryset = Page.objects.live().public().exclude(id__in=blog_ids).exclude(id__in=guide_ids)
	if start_date:
		queryset = queryset.filter(first_published_at__gte=start_date)
	if end_date:
		queryset = queryset.filter(first_published_at__lte=end_date)
	if order_by in ("date", "-date"):
		queryset = queryset.order_by(
			"first_published_at" if order_by == "date" else "-first_published_at"
		)
	return queryset
