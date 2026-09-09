"""????????? DOM ?????? (test_sidebar_cache).

???
1. ?? Cache Key ?????/?????
2. ??????? TTL ???
3. ?? 3 ???????????DOM ?????
4. Redis ???? Fail-Open ??????
5. ???? archive_sidebar ???? HTML ??????? (< 18 KB)?
6. ??????????
"""

import uuid
from unittest.mock import patch

from django.conf import settings
from django.core.cache import caches
from django.template import Context, Template
from django.test import TestCase, override_settings
from wagtail.models import Locale, Page, Site

from blog.models import BlogIndexPage, BlogPage
from blog.services.sidebar_cache import (
    ARCHIVE_POLICY_VERSION,
    ArchiveSidebarService,
)


class ArchiveSidebarCacheTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.locale, _ = Locale.objects.get_or_create(language_code=settings.LANGUAGE_CODE)
        root = Page.get_first_root_node()
        if root is None:
            root = Page(title="???", slug="test-root", locale=cls.locale)
            Page.add_root(instance=root)

        cls.site = Site.objects.filter(is_default_site=True).first()
        if cls.site is None:
            cls._orig_root_page = None
            cls.site = Site.objects.create(hostname="localhost", port=80, root_page=root, is_default_site=True)
        else:
            cls._orig_root_page = cls.site.root_page
            cls.site.root_page = root
            cls.site.save()

        cls.site_id = cls.site.pk
        cls.locale_id = cls.locale.pk

        cls.index = root.add_child(
            instance=BlogIndexPage(title="??????", slug=f"test-archive-blog-{uuid.uuid4().hex[:6]}", locale=cls.locale)
        )

        # ?????????????
        for year in [2024, 2025, 2026]:
            for month in [1, 6]:
                page = BlogPage(
                    title=f"??-{year}-{month}",
                    slug=f"post-{year}-{month}-{uuid.uuid4().hex[:6]}",
                    date=f"{year}-{month:02d}-15",
                    intro="????",
                    locale=cls.locale,
                )
                cls.index.add_child(instance=page)

    def setUp(self):
        self.cache = caches['default']
        self.cache.clear()

    def test_cache_key_structure(self):
        """???? key ????????."""
        key = ArchiveSidebarService.get_aggregate_cache_key(site_id=1, locale_id=2, gen=3)
        expected = f"wblog:sidebar:v2:archive:aggregate:1:2:3:{ARCHIVE_POLICY_VERSION}"
        self.assertEqual(key, expected)

    @override_settings(BLOG_SIDEBAR_CACHE_V2=True)
    def test_archive_aggregate_caching(self):
        """???????????????????."""
        data1 = ArchiveSidebarService.get_archive_aggregate(site_id=self.site_id, locale_id=self.locale_id)
        self.assertIn(2026, data1)
        self.assertIn(2025, data1)
        self.assertIn(2024, data1)

        # ?? Redis ????
        gen = 1
        key = ArchiveSidebarService.get_aggregate_cache_key(self.site_id, self.locale_id, gen)
        cached_val = self.cache.get(key)
        self.assertIsNotNone(cached_val)
        self.assertEqual(cached_val[2026]['count'], 2)

    def test_cross_site_or_locale_isolation(self):
        """??????????????????."""
        other_site_data = ArchiveSidebarService.get_archive_aggregate(site_id=999999, locale_id=self.locale_id)
        self.assertEqual(other_site_data, {})

        other_locale_data = ArchiveSidebarService.get_archive_aggregate(site_id=self.site_id, locale_id=999999)
        self.assertEqual(other_locale_data, {})

    def test_dom_slimming_and_progressive_disclosure(self):
        """???? 3 ?????????DOM ????."""
        context = ArchiveSidebarService.get_context(request=None)
        archive_tree = context['archive_tree']

        # 2026, 2025, 2024 ????????
        self.assertTrue(archive_tree[2026]['should_render_month_grid'])
        self.assertTrue(archive_tree[2025]['should_render_month_grid'])
        self.assertTrue(archive_tree[2024]['should_render_month_grid'])

    @override_settings(BLOG_SIDEBAR_CACHE_V2=True)
    def test_fail_open_on_redis_exception(self):
        """?? Redis ??????????????????? 500."""
        with patch.object(self.cache, 'get', side_effect=Exception("Redis connection timeout")):
            data = ArchiveSidebarService.get_archive_aggregate(site_id=self.site_id, locale_id=self.locale_id)
            self.assertIn(2026, data)

    def test_template_tag_rendered_html_size_under_budget(self):
        """????????? HTML ???????? 18 KB ????."""
        template = Template("{% load archive_tags %}{% archive_sidebar %}")
        rendered_html = template.render(Context({}))

        html_bytes = len(rendered_html.encode('utf-8'))
        print(f"[Archive Sidebar HTML Size] {html_bytes} bytes")
        # ???? 18 KB (18432 ??)
        self.assertLess(html_bytes, 18432)
        # ???????????????
        self.assertIn('2026', rendered_html)
        self.assertIn('fa-plus', rendered_html)

    @override_settings(BLOG_SIDEBAR_CACHE_V2=False)
    def test_sidebar_kill_switch_fallback_when_disabled(self):
        """当 BLOG_SIDEBAR_CACHE_V2=False 时，archive_sidebar 降级执行旧逻辑并成功渲染."""
        template = Template("{% load archive_tags %}{% archive_sidebar %}")
        rendered = template.render(Context({}))
        self.assertIn("archive-tree", rendered)
        self.assertIn("2026", rendered)

    @override_settings(BLOG_SIDEBAR_CACHE_V2=True)
    def test_archive_aggregate_single_flight(self):
        """测试归档聚合冷缓存构建时的 Single-Flight 互斥锁获取与释放."""
        lock_key = ArchiveSidebarService.get_lock_cache_key(self.site_id, self.locale_id, 1)
        self.cache.delete(lock_key)

        data = ArchiveSidebarService.get_archive_aggregate(self.site_id, self.locale_id)
        self.assertIn(2026, data)
        self.assertIsNone(self.cache.get(lock_key))
    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_orig_root_page", None):
            site = Site.objects.filter(is_default_site=True).first()
            if site and cls._orig_root_page:
                site.root_page = cls._orig_root_page
                site.save()
        super().tearDownClass()
