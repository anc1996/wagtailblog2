"""RSS和Atom订阅源的路由、缓存与生命周期测试。"""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse
from django.utils.translation import override

from blog.feeds import BlogAtomFeed, BlogRssFeed
from blog.models import BlogPage
from blog.services.feed_cache import (
    BlogFeedCache,
    BlogFeedInvalidationService,
    BlogFeedScope,
)
from blog.services.feed_query import BlogFeedContext, BlogFeedEntry, BlogFeedQueryService
from blog.signals import (
    invalidate_feed_on_page_deleted,
    invalidate_feed_on_page_published,
    invalidate_feed_on_page_unpublished,
    invalidate_feed_on_related_content_changed,
)


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "blog-feed-tests",
    }
}


@override_settings(CACHES=TEST_CACHES, BLOG_FEED_ANALYTICS_ENABLED=False)
class BlogFeedTests(SimpleTestCase):
    """验证Feed在不写测试内容库时的公开接口和缓存边界。"""

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.site = SimpleNamespace(
            pk=7,
            site_name="测试博客",
            root_url="https://blog.example.test",
        )
        self.locale = SimpleNamespace(pk=3, language_code="zh-hans")
        self.context = BlogFeedContext(
            request=self.factory.get("/zh-hans/feed/rss/"),
            site=self.site,
            locale=self.locale,
            origin="https://blog.example.test",
            feed_url="https://blog.example.test/zh-hans/feed/rss/",
        )
        published_at = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
        self.entry = BlogFeedEntry(
            title="check_highlight：x2 公式",
            summary="一个安全的摘要。",
            url="https://blog.example.test/zh-hans/python/feed-test/",
            guid="urn:wagtailblog:blogpage:test:3",
            published_at=published_at,
            updated_at=published_at,
            authors=("测试作者",),
            categories=("Python", "Feed"),
        )

    def _url(self, name):
        with override("zh-hans"):
            return reverse(name)

    def _render(self, feed_class, request):
        with (
            patch.object(BlogFeedQueryService, "build_context", return_value=self.context),
            patch.object(BlogFeedQueryService, "list_entries", return_value=[self.entry]),
        ):
            return feed_class()(request)

    def test_root_level_routes_resolve_for_each_format(self):
        rss_url = self._url("blog_feed:rss")
        atom_url = self._url("blog_feed:atom")

        self.assertEqual(rss_url, "/zh-hans/feed/rss/")
        self.assertEqual(atom_url, "/zh-hans/feed/atom/")
        self.assertIsInstance(resolve(rss_url).func, BlogRssFeed)
        self.assertIsInstance(resolve(atom_url).func, BlogAtomFeed)

    def test_rss_and_atom_return_valid_public_xml(self):
        for feed_class, path, content_type in (
            (BlogRssFeed, "/zh-hans/feed/rss/", "application/rss+xml"),
            (BlogAtomFeed, "/zh-hans/feed/atom/", "application/atom+xml"),
        ):
            with self.subTest(feed=feed_class.__name__):
                response = self._render(feed_class, self.factory.get(path))

                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response["Content-Type"])
                self.assertEqual(response["Cache-Control"], "public, max-age=60")
                ElementTree.fromstring(response.content)
                content = response.content.decode()
                self.assertIn("check_highlight", content)
                self.assertIn("测试作者", content)
                self.assertIn("Python", content)
                self.assertIn("urn:wagtailblog:blogpage:test:3", content)
                self.assertIn(self.context.feed_url, content)
                self.assertIn(self.entry.url, content)

    def test_feed_cache_hits_after_the_first_request(self):
        request = self.factory.get("/zh-hans/feed/rss/")
        with (
            patch.object(BlogFeedQueryService, "build_context", return_value=self.context),
            patch.object(
                BlogFeedQueryService,
                "list_entries",
                return_value=[self.entry],
            ) as list_entries,
        ):
            BlogRssFeed()(request)
            BlogRssFeed()(self.factory.get("/zh-hans/feed/rss/"))

        self.assertEqual(list_entries.call_count, 1)

    def test_feed_supports_conditional_requests(self):
        first_response = self._render(
            BlogRssFeed,
            self.factory.get("/zh-hans/feed/rss/"),
        )
        conditional_request = self.factory.get(
            "/zh-hans/feed/rss/",
            HTTP_IF_NONE_MATCH=first_response["ETag"],
        )
        second_response = self._render(BlogRssFeed, conditional_request)

        self.assertEqual(first_response.status_code, 200)
        self.assertIn("Last-Modified", first_response)
        self.assertEqual(second_response.status_code, 304)

    def test_query_parameters_redirect_and_non_get_methods_are_rejected(self):
        response = BlogRssFeed()(
            self.factory.get("/zh-hans/feed/rss/?utm_source=reader&ref=example")
        )

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/zh-hans/feed/rss/")
        self.assertEqual(
            BlogRssFeed()(self.factory.post("/zh-hans/feed/rss/")).status_code,
            405,
        )

    def test_cache_generation_changes_without_reusing_old_payload_keys(self):
        scope = BlogFeedScope(site_id=7, locale_id=3)
        cache_service = BlogFeedCache()

        initial_generation = cache_service.get_generation(scope)
        next_generation = cache_service.bump_generation(scope)

        self.assertIsInstance(initial_generation, str)
        self.assertTrue(initial_generation)
        self.assertNotEqual(initial_generation, next_generation)
        self.assertEqual(cache_service.get_generation(scope), next_generation)

    def test_invalid_cached_payload_is_treated_as_a_cache_miss(self):
        scope = BlogFeedScope(site_id=7, locale_id=3)
        cache_service = BlogFeedCache()
        generation = cache_service.get_generation(scope)
        cache_service.cache.set(
            cache_service._payload_key(
                scope,
                generation,
                "rss",
                self.context.origin,
            ),
            {"xml": "not-bytes"},
            timeout=300,
        )

        _, payload = cache_service.get_payload(scope, "rss", self.context.origin)

        self.assertIsNone(payload)

    def test_query_service_uses_public_site_locale_limited_queryset(self):
        manager = MagicMock()
        queryset = manager.live.return_value.public.return_value.in_site.return_value
        queryset.filter.return_value.defer.return_value.prefetch_related.return_value.order_by.return_value.__getitem__.return_value = []

        with patch("blog.services.feed_query.BlogPage.objects", manager):
            self.assertEqual(BlogFeedQueryService.list_entries(self.context), [])

        manager.live.assert_called_once_with()
        manager.live.return_value.public.assert_called_once_with()
        manager.live.return_value.public.return_value.in_site.assert_called_once_with(self.site)
        queryset.filter.assert_called_once_with(
            locale=self.locale,
            first_published_at__isnull=False,
        )

    def test_title_and_summary_are_plain_text_and_never_require_body(self):
        self.assertEqual(
            BlogFeedQueryService._title_text("`code`：$x^2$"),
            "code：x^2",
        )
        self.assertEqual(
            BlogFeedQueryService._summary_text("<p>一个 <strong>摘要</strong></p>"),
            "一个 摘要",
        )
        self.assertEqual(BlogFeedQueryService._summary_text(""), "点击阅读全文")

    def test_page_lifecycle_receivers_schedule_the_resolved_scope(self):
        page = SimpleNamespace(pk=12, locale_id=3)
        scope = BlogFeedScope(site_id=7, locale_id=3)

        with patch.object(
            BlogFeedInvalidationService,
            "scope_for_page",
            return_value=scope,
        ), patch.object(BlogFeedInvalidationService, "schedule_scope") as schedule_scope:
            invalidate_feed_on_page_published(BlogPage, page)
            invalidate_feed_on_page_unpublished(BlogPage, page)
            invalidate_feed_on_page_deleted(BlogPage, page)

        self.assertEqual(schedule_scope.call_count, 3)
        schedule_scope.assert_called_with(scope)

    def test_related_content_receiver_uses_global_scope_without_article_scan(self):
        with patch.object(BlogFeedInvalidationService, "schedule_all") as schedule_all:
            invalidate_feed_on_related_content_changed(BlogPage, MagicMock())

        schedule_all.assert_called_once_with()

    def test_public_templates_advertise_both_feed_formats_and_footer_has_no_form(self):
        project_root = Path(__file__).resolve().parents[2]
        base_template = (project_root / "templates/base.html").read_text(encoding="utf-8")
        footer_template = (project_root / "templates/includes/footer.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("blog_feed:rss", base_template)
        self.assertIn("blog_feed:atom", base_template)
        self.assertIn("blog_feed:rss", footer_template)
        self.assertIn("blog_feed:atom", footer_template)
        self.assertNotIn("contact.html", footer_template)
        self.assertNotIn("<form", footer_template)
