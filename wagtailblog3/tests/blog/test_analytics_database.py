"""内容分析的数据库聚合、幂等和阈值测试。"""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from wagtail.models import Locale, Page, Site

from blog.analytics_views import record_engagement
from blog.models import FeedClientDaily, FeedRequestDaily, PageView, PageViewCount
from blog.page_view_counter import PageViewCounter, visitor_key_for_request
from blog.services.feed_analytics import FeedRequestRecorder
from blog.services.feed_cache import BlogFeedScope
from blog.services.content_analytics import ContentAnalyticsFilters, ContentAnalyticsQueryService


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "analytics-db-tests"}})
class AnalyticsDatabaseTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.locale, _ = Locale.objects.get_or_create(language_code=settings.LANGUAGE_CODE)
        self.page = Page.get_first_root_node()
        if self.page is None:
            self.page = Page.add_root(title="Analytics test root", slug="analytics-test-root")
        self.site = Site.objects.filter(is_default_site=True).first()
        if self.site is None:
            self.site = Site.objects.create(
                hostname="testserver", port=80, root_page=self.page, is_default_site=True
            )

    def request(self, *, user_agent="reader-a", referrer=""):
        request = self.factory.get(
            "/zh-hans/article/",
            HTTP_HOST="testserver",
            HTTP_USER_AGENT=user_agent,
            HTTP_REFERER=referrer,
            REMOTE_ADDR="203.0.113.10",
        )
        request.user = AnonymousUser()
        return request

    def test_page_view_counter_separates_views_and_unique_visitors(self):
        counter = PageViewCounter(self.page.pk)
        self.assertTrue(counter.record(self.request()))
        self.assertFalse(counter.record(self.request()))
        self.assertTrue(counter.record(self.request(user_agent="reader-b")))

        aggregate = PageViewCount.objects.get(page=self.page, date=timezone.localdate())
        self.assertEqual(aggregate.view_count_v2, 3)
        self.assertEqual(aggregate.unique_visitor_count_v2, 2)
        self.assertEqual(PageView.objects.filter(page=self.page).count(), 2)
        self.assertEqual(sum(PageView.objects.values_list("view_count", flat=True)), 3)
        self.assertEqual(aggregate.count, 0)
        self.assertEqual(aggregate.unique_count, 0)

    def test_engagement_thresholds_and_sequences_are_idempotent(self):
        request = self.request()
        PageViewCounter(self.page.pk).record(request)
        session_id = str(uuid4())

        def send(sequence, seconds, scroll):
            event_request = self.factory.post(
                "/blog/api/analytics/engagement/",
                data=json.dumps({
                    "page_id": self.page.pk,
                    "session_id": session_id,
                    "sequence": sequence,
                    "engaged": True,
                    "max_scroll_percent": scroll,
                    "active_reading_seconds": seconds,
                }),
                content_type="application/json",
                HTTP_HOST="testserver",
                HTTP_USER_AGENT="reader-a",
                REMOTE_ADDR="203.0.113.10",
            )
            event_request.user = AnonymousUser()
            event_request._dont_enforce_csrf_checks = True
            return record_engagement(event_request)

        manager = SimpleNamespace()
        with (
            patch("blog.analytics_views.BlogPage.objects") as page_manager,
            patch("blog.analytics_views.Site.find_for_request", return_value=self.site),
            patch.object(Page, "get_site", return_value=self.site),
        ):
            page_manager.live.return_value.public.return_value.filter.return_value.first.return_value = self.page
            self.assertEqual(send(1, 5, 90).status_code, 200)
            aggregate = PageViewCount.objects.get(page=self.page, date=timezone.localdate())
            self.assertEqual(aggregate.engaged_visitor_count, 0)
            self.assertEqual(aggregate.scroll_50_visitor_count, 0)
            self.assertEqual(aggregate.scroll_90_visitor_count, 0)

            self.assertEqual(send(2, 15, 90).status_code, 200)
            aggregate.refresh_from_db()
            self.assertEqual(aggregate.engaged_visitor_count, 1)
            self.assertEqual(aggregate.scroll_50_visitor_count, 1)
            self.assertEqual(aggregate.scroll_90_visitor_count, 0)

            self.assertEqual(send(3, 30, 90).status_code, 200)
            self.assertEqual(send(3, 30, 90).status_code, 200)
            self.assertEqual(send(2, 20, 90).status_code, 200)

        aggregate.refresh_from_db()
        self.assertEqual(aggregate.engaged_visitor_count, 1)
        self.assertEqual(aggregate.scroll_50_visitor_count, 1)
        self.assertEqual(aggregate.scroll_90_visitor_count, 1)
        self.assertEqual(aggregate.active_reading_seconds, 30)

    def test_feed_recorder_counts_responses_and_estimated_clients(self):
        scope = BlogFeedScope(site_id=self.site.pk, locale_id=self.locale.pk)
        request = self.request()
        FeedRequestRecorder.record(request, HttpResponse(status=200), scope, "rss")
        FeedRequestRecorder.record(request, HttpResponse(status=200), scope, "rss")
        FeedRequestRecorder.record(request, HttpResponse(status=304), scope, "rss")

        daily = FeedRequestDaily.objects.get(site=self.site, locale=self.locale)
        self.assertEqual(daily.response_200_count, 2)
        self.assertEqual(daily.response_304_count, 1)
        self.assertEqual(daily.estimated_client_count, 1)
        self.assertEqual(FeedClientDaily.objects.count(), 1)

    def test_content_analytics_queries_compile_with_empty_blog_scope(self):
        filters = ContentAnalyticsFilters(
            start_date=timezone.localdate() - timedelta(days=29),
            end_date=timezone.localdate(),
        )
        service = ContentAnalyticsQueryService(filters)
        self.assertEqual(service.overview()["views"], 0)
        self.assertEqual(service.trends(), [])
        self.assertEqual(list(service.top_pages()), [])
        self.assertEqual(service.sources(), [])
        self.assertEqual(service.feeds(), [])
