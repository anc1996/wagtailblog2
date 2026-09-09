"""???????????????? (test_random_author_cache).

???
1. ??????????????
2. ?? ORDER BY RAND()??????????????
3. ??????????????????
4. Redis ??????? Fail-Open?
5. DTO ???????????
6. ??????????????? async decoding?
7. ????????????
"""

import uuid
from unittest.mock import patch

from django.conf import settings
from django.core.cache import caches
from django.template import Context, Template
from django.test import TestCase, override_settings
from wagtail.models import Locale, Page, Site

from blog.models import Author, BlogIndexPage, BlogPage
from blog.services.sidebar_cache import (
    AuthorCardDTO,
    RandomAuthorSidebarService,
)


class RandomAuthorCacheTests(TestCase):
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
            instance=BlogIndexPage(title="???????", slug=f"test-author-blog-{uuid.uuid4().hex[:6]}", locale=cls.locale)
        )

        # ??????????
        cls.active_author = Author.objects.create(
            name="????",
            slug="active-author",
        )
        # ????????????
        cls.idle_author = Author.objects.create(
            name="????",
            slug="idle-author",
        )

        page = BlogPage(
            title="??????",
            slug=f"post-author-{uuid.uuid4().hex[:6]}",
            date="2026-09-08",
            intro="????",
            locale=cls.locale,
        )
        cls.index.add_child(instance=page)
        page.authors.add(cls.active_author)
        page.save()

    def setUp(self):
        self.cache = caches['default']
        self.cache.clear()

    def test_candidate_selection_only_live_authors(self):
        """??????????????????????????."""
        candidates = RandomAuthorSidebarService.get_candidate_ids(site_id=self.site_id, locale_id=self.locale_id)
        self.assertIn(self.active_author.id, candidates)
        self.assertNotIn(self.idle_author.id, candidates)

    def test_cross_site_or_locale_isolation(self):
        """???????????????????."""
        other_site_candidates = RandomAuthorSidebarService.get_candidate_ids(site_id=999999, locale_id=self.locale_id)
        self.assertEqual(other_site_candidates, [])

        other_locale_candidates = RandomAuthorSidebarService.get_candidate_ids(site_id=self.site_id, locale_id=999999)
        self.assertEqual(other_locale_candidates, [])

    def test_no_order_by_rand_execution(self):
        """????????????????? SQL ORDER BY RAND()."""
        chosen_id = RandomAuthorSidebarService.pick_author_id(
            candidate_ids=[self.active_author.id],
            site_id=self.site_id,
            locale_id=self.locale_id,
            gen=1,
        )
        self.assertEqual(chosen_id, self.active_author.id)

    @override_settings(BLOG_SIDEBAR_CACHE_V2=True)
    def test_caching_of_candidates_and_pick(self):
        """??????????????????? Redis."""
        ctx = RandomAuthorSidebarService.get_context(request=None)
        author_dto = ctx['random_author']
        self.assertIsNotNone(author_dto)
        self.assertIsInstance(author_dto, AuthorCardDTO)
        self.assertEqual(author_dto.pk, self.active_author.pk)

        cand_key = RandomAuthorSidebarService.get_candidates_cache_key(self.site_id, self.locale_id, 1)
        self.assertEqual(self.cache.get(cand_key), [self.active_author.id])

    @override_settings(BLOG_SIDEBAR_CACHE_V2=True)
    def test_empty_candidates_graceful_fallback(self):
        """当站点无任何公开文章作者时，侧边栏安全输出空状态."""
        with patch.object(RandomAuthorSidebarService, 'get_candidate_ids', return_value=[]):
            ctx = RandomAuthorSidebarService.get_context(request=None)
            self.assertIsNone(ctx['random_author'])

            template = Template("{% load blog_tags %}{% random_author_sidebar %}")
            rendered = template.render(Context({}))
            self.assertEqual(rendered.strip(), "")

    @override_settings(BLOG_SIDEBAR_CACHE_V2=False)
    def test_kill_switch_fallback_when_disabled(self):
        """当 BLOG_SIDEBAR_CACHE_V2=False 时，回退执行旧版作者查询并成功渲染."""
        template = Template("{% load blog_tags %}{% random_author_sidebar %}")
        rendered = template.render(Context({}))
        self.assertIn("inner-box", rendered)

    @override_settings(BLOG_SIDEBAR_CACHE_V2=True)
    def test_fail_open_on_redis_error(self):
        """?? Redis ????????????????? 500."""
        with patch.object(self.cache, 'get', side_effect=Exception("Redis connection error")):
            ctx = RandomAuthorSidebarService.get_context(request=None)
            self.assertIsNotNone(ctx['random_author'])

    def test_template_rendering_attributes(self):
        """???????? decoding=async ?????."""
        template = Template("{% load blog_tags %}{% random_author_sidebar %}")
        rendered = template.render(Context({}))

        self.assertIn('????', rendered)
        self.assertIn('decoding="async"', rendered)
        self.assertIn('width="300"', rendered)
        self.assertIn('height="200"', rendered)
    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_orig_root_page", None):
            site = Site.objects.filter(is_default_site=True).first()
            if site and cls._orig_root_page:
                site.root_page = cls._orig_root_page
                site.save()
        super().tearDownClass()
