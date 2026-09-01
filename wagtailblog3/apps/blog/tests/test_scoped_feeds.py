"""标签和作者范围订阅源的路由、查询与缓存隔离测试。"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

from django.core.cache import cache
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse
from django.utils.translation import override
from taggit.models import Tag

from blog.feeds import (
    AuthorBlogAtomFeed,
    AuthorBlogRssFeed,
    TagBlogAtomFeed,
    TagBlogRssFeed,
)
from blog.services.feed_cache import BlogFeedCache, BlogFeedScope
from blog.services.feed_query import BlogFeedContext, BlogFeedEntry, BlogFeedQueryService


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "blog-scoped-feed-tests",
    }
}


@override_settings(CACHES=TEST_CACHES, BLOG_FEED_ANALYTICS_ENABLED=False)
class ScopedBlogFeedTests(SimpleTestCase):
    """范围订阅源不能与全站或其他标签、作者复用同一XML缓存。"""

    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.site = SimpleNamespace(pk=7, site_name="测试博客")
        self.locale = SimpleNamespace(pk=3, language_code="zh-hans")
        published_at = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
        self.entry = BlogFeedEntry(
            title="Feed范围测试",
            summary="范围订阅源的安全摘要。",
            url="https://blog.example.test/zh-hans/python/scoped-feed/",
            guid="urn:wagtailblog:blogpage:scoped:3",
            published_at=published_at,
            updated_at=published_at,
            authors=("测试作者",),
            categories=("Django",),
        )

    def _context(self, *, scope_type, scope_id, slug, label, path):
        return BlogFeedContext(
            request=self.factory.get(path),
            site=self.site,
            locale=self.locale,
            origin="https://blog.example.test",
            feed_url=f"https://blog.example.test{path}",
            scope_type=scope_type,
            scope_id=scope_id,
            scope_slug=slug,
            scope_label=label,
        )

    def _url(self, name, **kwargs):
        with override("zh-hans"):
            return reverse(name, kwargs=kwargs)

    def test_scoped_routes_keep_unicode_tag_slugs_and_author_slugs(self):
        tag_url = self._url("blog_feed:tag_rss", tag_slug="Django缓存")
        author_url = self._url("blog_feed:author_atom", author_slug="测试作者")

        self.assertEqual(tag_url, "/zh-hans/feed/tag/Django%E7%BC%93%E5%AD%98/rss/")
        self.assertEqual(author_url, "/zh-hans/feed/author/%E6%B5%8B%E8%AF%95%E4%BD%9C%E8%80%85/atom/")
        self.assertIsInstance(resolve(tag_url).func, TagBlogRssFeed)
        self.assertIsInstance(resolve(author_url).func, AuthorBlogAtomFeed)

    def test_tag_and_author_feed_render_scope_specific_metadata(self):
        tag_path = "/zh-hans/feed/tag/django/rss/"
        author_path = "/zh-hans/feed/author/adrian/atom/"
        tag_context = self._context(
            scope_type="tag",
            scope_id=11,
            slug="django",
            label="Django",
            path=tag_path,
        )
        author_context = self._context(
            scope_type="author",
            scope_id=12,
            slug="adrian",
            label="Adrian",
            path=author_path,
        )

        with (
            patch.object(BlogFeedQueryService, "build_tag_context", return_value=tag_context),
            patch.object(BlogFeedQueryService, "list_entries", return_value=[self.entry]),
        ):
            tag_response = TagBlogRssFeed()(self.factory.get(tag_path), tag_slug="django")
        with (
            patch.object(
                BlogFeedQueryService,
                "build_author_context",
                return_value=author_context,
            ),
            patch.object(BlogFeedQueryService, "list_entries", return_value=[self.entry]),
        ):
            author_response = AuthorBlogAtomFeed()(
                self.factory.get(author_path), author_slug="adrian"
            )

        self.assertEqual(tag_response.status_code, 200)
        self.assertEqual(author_response.status_code, 200)
        ElementTree.fromstring(tag_response.content)
        ElementTree.fromstring(author_response.content)
        self.assertIn("标签：Django", tag_response.content.decode())
        self.assertIn("作者：Adrian", author_response.content.decode())
        # RSS频道规范没有根级guid元素；范围身份由自链接和内部稳定标识共同表达。
        self.assertEqual(
            TagBlogRssFeed().feed_guid(tag_context),
            "urn:wagtailblog:feed:7:3:tag:11:rss",
        )
        self.assertEqual(
            AuthorBlogAtomFeed().feed_guid(author_context),
            "urn:wagtailblog:feed:7:3:author:12:atom",
        )

    def test_payload_cache_key_is_isolated_by_scope_type_and_object(self):
        cache_service = BlogFeedCache()
        origin = "https://blog.example.test"
        global_scope = BlogFeedScope(site_id=7, locale_id=3)
        django_scope = BlogFeedScope(
            site_id=7,
            locale_id=3,
            scope_type="tag",
            scope_id=11,
        )
        python_scope = BlogFeedScope(
            site_id=7,
            locale_id=3,
            scope_type="tag",
            scope_id=12,
        )

        self.assertNotEqual(
            cache_service._payload_key(global_scope, "same", "rss", origin),
            cache_service._payload_key(django_scope, "same", "rss", origin),
        )
        self.assertNotEqual(
            cache_service._payload_key(django_scope, "same", "rss", origin),
            cache_service._payload_key(python_scope, "same", "rss", origin),
        )

    def test_query_service_applies_the_requested_scope_filter(self):
        tag_context = self._context(
            scope_type="tag",
            scope_id=11,
            slug="django",
            label="Django",
            path="/zh-hans/feed/tag/django/rss/",
        )
        manager = MagicMock()
        queryset = manager.live.return_value.public.return_value.in_site.return_value
        queryset.filter.return_value.defer.return_value.prefetch_related.return_value.order_by.return_value.__getitem__.return_value = []

        with patch("blog.services.feed_query.BlogPage.objects", manager):
            self.assertEqual(BlogFeedQueryService.list_entries(tag_context), [])

        queryset.filter.assert_called_once_with(
            locale=self.locale,
            first_published_at__isnull=False,
            tags__pk=11,
        )

    def test_scoped_feed_rejects_tracking_query_parameters_before_lookup(self):
        response = TagBlogRssFeed()(
            self.factory.get("/zh-hans/feed/tag/django/rss/?utm_source=reader"),
            tag_slug="django",
        )

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/zh-hans/feed/tag/django/rss/")

    def test_missing_scope_object_returns_404_instead_of_a_global_feed(self):
        with patch(
            "blog.services.feed_query.Tag.objects.get",
            side_effect=Tag.DoesNotExist,
        ):
            with self.assertRaises(Http404):
                BlogFeedQueryService.build_tag_context(self.factory.get("/"), "missing")
