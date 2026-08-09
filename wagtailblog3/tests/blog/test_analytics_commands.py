"""分析明细维护命令的安全门禁测试。"""

from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from wagtail.models import Locale, Page, Site

from blog.models import ArticleEngagementSession, FeedClientDaily, PageView


class AnalyticsCleanupCommandTests(TestCase):
    def setUp(self):
        self.page = Page.get_first_root_node()
        self.locale = Locale.get_default()
        self.site = Site.objects.filter(is_default_site=True).first() or Site.objects.create(
            hostname="testserver", port=80, root_page=self.page, is_default_site=True
        )
        old_date = timezone.localdate() - timedelta(days=60)
        now = timezone.now()
        self.old_page_view = PageView.objects.create(
            page=self.page,
            date=old_date,
            visitor_key="old-visitor",
            ip_address="203.0.113.1",
            first_viewed_at=now,
            last_viewed_at=now,
        )
        ArticleEngagementSession.objects.create(
            page=self.page,
            date=old_date,
            visitor_key="old-visitor",
            session_id=uuid4(),
        )
        FeedClientDaily.objects.create(
            site=self.site,
            locale=self.locale,
            date=old_date,
            scope_type="global",
            scope_id=0,
            feed_format="rss",
            client_key="old-client",
        )

    def test_cleanup_defaults_to_preview_and_confirm_deletes_only_details(self):
        call_command("cleanup_pageviews", stdout=StringIO())
        self.assertTrue(PageView.objects.filter(pk=self.old_page_view.pk).exists())

        call_command("cleanup_pageviews", confirm=True, batch_size=1, stdout=StringIO())
        self.assertFalse(PageView.objects.filter(pk=self.old_page_view.pk).exists())
        self.assertFalse(ArticleEngagementSession.objects.exists())
        self.assertFalse(FeedClientDaily.objects.exists())

    def test_backfill_defaults_to_preview(self):
        PageView.objects.filter(pk=self.old_page_view.pk).update(visitor_key=None)
        call_command("backfill_pageview_visitor_keys", stdout=StringIO())
        self.old_page_view.refresh_from_db()
        self.assertIsNone(self.old_page_view.visitor_key)

        call_command("backfill_pageview_visitor_keys", confirm=True, batch_size=1, stdout=StringIO())
        self.old_page_view.refresh_from_db()
        self.assertEqual(self.old_page_view.visitor_key, f"legacy:{self.old_page_view.pk}")
