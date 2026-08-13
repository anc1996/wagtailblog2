"""公开非 BlogPage 搜索流的唯一 QuerySet 构造入口。"""

from __future__ import annotations

from blog.models import BlogPage
from wagtail.models import Page


def build_public_pages_queryset(start_date=None, end_date=None, order_by=None):
	"""返回已发布、公开且排除 BlogPage 的普通页面集合。"""
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
