"""公开 GuidePage 搜索流的唯一 QuerySet 构造入口。"""

from __future__ import annotations

from datetime import date, datetime

from django.db.models import QuerySet

from wagtail.models import Page


def build_public_guides_queryset(
    start_date: date | datetime | None = None,
    end_date: date | datetime | None = None,
    order_by: str | None = None,
) -> QuerySet[Page]:
	"""返回已发布、公开且仅包含 GuidePage 的页面集合。"""
	from guides.models import GuidePage

	guide_ids = GuidePage.objects.values("id")
	queryset = Page.objects.live().public().filter(id__in=guide_ids)

	if start_date:
		queryset = queryset.filter(first_published_at__gte=start_date)
	if end_date:
		queryset = queryset.filter(first_published_at__lte=end_date)

	if order_by in ("date", "-date"):
		queryset = queryset.order_by(
			"first_published_at" if order_by == "date" else "-first_published_at"
		)

	return queryset
