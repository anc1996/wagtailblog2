"""Wagtail 内容分析后台的只读查询服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from taggit.models import Tag
from wagtail.models import Locale, Site

from blog.models import (
    Author,
    BlogPage,
    FeedRequestDaily,
    PageTrafficSourceDaily,
    PageViewCount,
)


def _positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


@dataclass(frozen=True)
class ContentAnalyticsFilters:
    """经过白名单解析的后台筛选条件。"""

    start_date: object
    end_date: object
    author_id: int | None = None
    tag_id: int | None = None
    locale_id: int | None = None
    feed_scope: str = ""
    feed_format: str = ""

    @classmethod
    def from_request(cls, request):
        today = timezone.localdate()
        start_date = parse_date(request.GET.get("start_date", "")) or today - timedelta(days=29)
        end_date = parse_date(request.GET.get("end_date", "")) or today
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        feed_scope = request.GET.get("feed_scope", "")
        if feed_scope not in {"", "global", "tag", "author"}:
            feed_scope = ""
        feed_format = request.GET.get("feed_format", "")
        if feed_format not in {"", "rss", "atom"}:
            feed_format = ""
        return cls(
            start_date=start_date,
            end_date=end_date,
            author_id=_positive_int(request.GET.get("author")),
            tag_id=_positive_int(request.GET.get("tag")),
            locale_id=_positive_int(request.GET.get("locale")),
            feed_scope=feed_scope,
            feed_format=feed_format,
        )

    def as_query_dict(self):
        values = {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
        }
        for key, value in (
            ("author", self.author_id),
            ("tag", self.tag_id),
            ("locale", self.locale_id),
            ("feed_scope", self.feed_scope),
            ("feed_format", self.feed_format),
        ):
            if value:
                values[key] = value
        return values


class ContentAnalyticsQueryService:
    """集中生成后台统计，所有指标只读取 V2 字段。"""

    def __init__(self, filters: ContentAnalyticsFilters, site: Site | None = None):
        self.filters = filters
        self.site = site

    def _pages(self):
        pages = BlogPage.objects.all()
        if self.site is not None:
            pages = pages.in_site(self.site)
        if self.filters.author_id:
            pages = pages.filter(authors__pk=self.filters.author_id)
        if self.filters.tag_id:
            pages = pages.filter(tags__pk=self.filters.tag_id)
        if self.filters.locale_id:
            pages = pages.filter(locale_id=self.filters.locale_id)
        return pages.distinct()

    def _counts(self):
        return PageViewCount.objects.filter(
            page_id__in=self._pages().values("pk"),
            date__range=(self.filters.start_date, self.filters.end_date),
        )

    def overview(self):
        values = self._counts().aggregate(
            views=Coalesce(Sum("view_count_v2"), 0),
            visitors=Coalesce(Sum("unique_visitor_count_v2"), 0),
            engaged=Coalesce(Sum("engaged_visitor_count"), 0),
            reached_90=Coalesce(Sum("scroll_90_visitor_count"), 0),
            active_seconds=Coalesce(Sum("active_reading_seconds"), 0),
        )
        visitors = values["visitors"]
        values["engagement_rate"] = round(values["engaged"] * 100 / visitors, 1) if visitors else 0
        values["reach_90_rate"] = round(values["reached_90"] * 100 / visitors, 1) if visitors else 0
        values["average_active_seconds"] = round(values["active_seconds"] / visitors, 1) if visitors else 0
        return values

    def trends(self):
        return list(
            self._counts()
            .values("date")
            .annotate(
                views=Coalesce(Sum("view_count_v2"), 0),
                visitors=Coalesce(Sum("unique_visitor_count_v2"), 0),
                engaged=Coalesce(Sum("engaged_visitor_count"), 0),
            )
            .order_by("date")
        )

    def article_performance(self):
        """返回可继续分页的文章表现查询，避免主报表一次加载全部文章。"""

        date_filter = Q(view_counts__date__range=(self.filters.start_date, self.filters.end_date))
        return self._pages().prefetch_related("authors", "tags").annotate(
            analytics_views=Coalesce(Sum("view_counts__view_count_v2", filter=date_filter), 0),
            analytics_visitors=Coalesce(Sum("view_counts__unique_visitor_count_v2", filter=date_filter), 0),
            analytics_engaged=Coalesce(Sum("view_counts__engaged_visitor_count", filter=date_filter), 0),
            analytics_reached_90=Coalesce(Sum("view_counts__scroll_90_visitor_count", filter=date_filter), 0),
            analytics_active_seconds=Coalesce(Sum("view_counts__active_reading_seconds", filter=date_filter), 0),
        ).filter(analytics_views__gt=0).order_by("-analytics_views", "pk")

    def top_pages(self, limit=100):
        queryset = self.article_performance()
        return queryset[:limit] if limit is not None else queryset

    def sources(self):
        return list(
            PageTrafficSourceDaily.objects.filter(
                page_id__in=self._pages().values("pk"),
                date__range=(self.filters.start_date, self.filters.end_date),
            )
            .values("source_category")
            .annotate(
                views=Coalesce(Sum("view_count"), 0),
                visitors=Coalesce(Sum("unique_visitor_count"), 0),
            )
            .order_by("-views", "source_category")
        )

    def feeds(self, limit=100):
        queryset = FeedRequestDaily.objects.filter(
            date__range=(self.filters.start_date, self.filters.end_date)
        )
        if self.site is not None:
            queryset = queryset.filter(site=self.site)
        if self.filters.locale_id:
            queryset = queryset.filter(locale_id=self.filters.locale_id)
        if self.filters.feed_scope:
            queryset = queryset.filter(scope_type=self.filters.feed_scope)
        if self.filters.feed_format:
            queryset = queryset.filter(feed_format=self.filters.feed_format)
        if self.filters.author_id:
            queryset = queryset.filter(scope_type="author", scope_id=self.filters.author_id)
        if self.filters.tag_id:
            queryset = queryset.filter(scope_type="tag", scope_id=self.filters.tag_id)
        return list(
            queryset.values("scope_type", "scope_id", "scope_label", "feed_format")
            .annotate(
                responses_200=Coalesce(Sum("response_200_count"), 0),
                responses_304=Coalesce(Sum("response_304_count"), 0),
                estimated_clients=Coalesce(Sum("estimated_client_count"), 0),
            )
            .order_by("-responses_200", "-responses_304", "scope_type")[:limit]
        )

    @staticmethod
    def filter_options():
        return {
            "authors": Author.objects.order_by("name"),
            "tags": Tag.objects.order_by("name"),
            "locales": Locale.objects.order_by("language_code"),
        }
