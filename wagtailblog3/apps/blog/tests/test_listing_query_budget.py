"""分类列表查询预算测试 (Listing Query Budget Tests).

严格核验：
1. 20 篇真实文章在冷路径下的业务 SQL 预算 (<= 10 次)；
2. 循环遍历 post 属性（访问量、反应、作者、标签）时 0 额外 SQL 产生（彻底消除 N+1）；
3. stats_block 模板渲染与 _blog_index_results.html 完整渲染 0 重入/预算达标；
4. API JSON 输出契约与业务 SQL <= 10；
5. L2 Redis 热缓存命中 0 SQL 查询；
6. 多态兼容开关 (BLOG_INDEX_COMPAT_POLYMORPHIC_QUERY) 行为与 SQL 预算。
"""

import json
from django.conf import settings
from django.core.cache import caches
from django.db import connection
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from wagtail.models import Locale, Page, Site

from blog.models import Author, BlogIndexPage, BlogPage, BlogTagIndexPage, PageViewCount, Reaction, ReactionType
from blog.services.listing import (
    ListingService,
    _batch_prefetch_post_data,
    build_listing,
)
from blog.views import blog_index_results_api


class ListingQueryBudgetTests(TestCase):
    """验证列表批量装配引擎的严格 SQL 预算契约 (20 篇基准)."""

    @classmethod
    def setUpTestData(cls):
        locale, _ = Locale.objects.get_or_create(language_code=settings.LANGUAGE_CODE)
        cls.root = Page.get_first_root_node()
        if cls.root is None:
            cls.root = Page(title="根节点", slug="test-root", locale=locale)
            Page.add_root(instance=cls.root)

        site = Site.objects.filter(is_default_site=True).first()
        if site:
            cls._orig_root_page = site.root_page
            site.root_page = cls.root
            site.save()
        else:
            cls._orig_root_page = None
            Site.objects.create(hostname="localhost", port=80, is_default_site=True, root_page=cls.root)

        cls.index_page = cls.root.add_child(
            instance=BlogIndexPage(title="测试分类索引", slug="test-listing-index", locale=locale)
        )

        cls.tag_index_page = cls.root.add_child(
            instance=BlogTagIndexPage(title="测试标签索引", slug="test-tags-index", locale=locale)
        )

        cls.author1 = Author.objects.create(name="测试作者1", slug="author-1")
        cls.author2 = Author.objects.create(name="测试作者2", slug="author-2")
        cls.rx_type1 = ReactionType.objects.create(name="点赞", icon="fa-thumbs-up")
        cls.rx_type2 = ReactionType.objects.create(name="喜欢", icon="fa-heart")

        cls.posts = []
        today = timezone.localdate()

        # 严格创建 20 篇真实文章作为基线规模
        for i in range(1, 21):
            post = cls.index_page.add_child(
                instance=BlogPage(
                    title=f"公文测试文章-{i:02d}",
                    slug=f"test-post-{i:02d}",
                    intro=f"这是第 {i:02d} 篇公文材料与政务公开题材测试文章导言",
                    date=today,
                    locale=locale,
                )
            )
            post.authors.add(cls.author1 if i % 2 == 0 else cls.author2)
            post.tags.add("公文", "政策")
            post.save()

            PageViewCount.objects.create(
                page_id=post.pk,
                date=today,
                view_count_v2=10 + i,
                unique_visitor_count_v2=5 + i,
            )
            Reaction.objects.create(
                page=post,
                reaction_type=cls.rx_type1,
                ip_address="127.0.0.1",
            )
            cls.posts.append(post)

    def setUp(self):
        self.factory = RequestFactory()
        caches["default"].clear()
        # 预热 Wagtail 框架级站点路由树与标签索引缓存，隔离框架一次性元数据加载与业务查询
        Site.get_site_root_paths()
        from blog.services.listing import (
            _get_cached_reaction_types,
            _get_cached_tag_index_page,
            _get_cached_tag_index_url,
            _get_default_site,
        )
        _get_default_site()
        _get_cached_reaction_types()
        _get_cached_tag_index_page()
        _get_cached_tag_index_url()

    def test_batch_prefetch_20_posts_stays_within_sql_budget(self):
        """测试 20 篇文章的批量装配业务 SQL 严格在 10 次以内."""
        request = self.factory.get(self.index_page.url or "/")
        raw_pages = list(self.index_page.get_children().live().public())
        self.assertEqual(len(raw_pages), 20) # 20 posts under index_page

        blog_pages = [p for p in raw_pages if isinstance(p.specific, BlogPage)]
        self.assertEqual(len(blog_pages), 20)

        with CaptureQueriesContext(connection) as ctx:
            prefetched = _batch_prefetch_post_data(blog_pages, request=request)

        query_count = len(ctx.captured_queries)
        self.assertLessEqual(
            query_count,
            10,
            f"批量装配 SQL 次数超出预算: 实际 {query_count} 次，预算 <= 10 次",
        )
        self.assertEqual(len(prefetched), 20)

    def test_looping_20_prefetched_posts_emits_zero_sql_queries(self):
        """核心验证：循环访问 20 篇文章的访问量、反应、作者和标签时，0 额外查询."""
        request = self.factory.get(self.index_page.url or "/")
        blog_pages = list(BlogPage.objects.live().public().child_of(self.index_page))
        prefetched = _batch_prefetch_post_data(blog_pages, request=request)

        with CaptureQueriesContext(connection) as ctx:
            for post in prefetched:
                vc = post.get_view_count()
                self.assertGreater(vc["total"], 0)
                self.assertGreater(vc["today"], 0)

                rx = post.get_reactions()
                self.assertTrue(any(item["count"] > 0 for item in rx))

                authors = getattr(post, "listing_authors", [])
                self.assertEqual(len(authors), 1)

                tags = getattr(post, "listing_tags", [])
                self.assertEqual(len(tags), 2)

        zero_queries = len(ctx.captured_queries)
        self.assertEqual(
            zero_queries,
            0,
            f"属性访问时仍有 {zero_queries} 次意外 SQL 发生，未达成 0-query 消除目标！",
        )

    def test_stats_block_template_rendering_emits_zero_sql(self):
        """验证 stats_block.html 模板在预装配后渲染 20 篇文章 0 SQL 产生."""
        request = self.factory.get(self.index_page.url or "/")
        blog_pages = list(BlogPage.objects.live().public().child_of(self.index_page))
        prefetched = _batch_prefetch_post_data(blog_pages, request=request)

        with CaptureQueriesContext(connection) as ctx:
            for post in prefetched:
                html = render_to_string("blog/stats_block.html", {"page": post})
                self.assertIn("view-stats", html)
                self.assertIn("点赞", html)

        self.assertEqual(
            len(ctx.captured_queries),
            0,
            "stats_block 模板渲染时触发了意外的 SQL 查询！",
        )

    def test_full_listing_context_and_template_rendering_budget(self):
        """验证 20 篇文章从 build_listing 到完整 _blog_index_results.html 渲染，业务 SQL <= 10."""
        request = self.factory.get(self.index_page.url or "/zh-hans/category/")

        with CaptureQueriesContext(connection) as ctx:
            result = build_listing(request, self.index_page, {}, output_format="context")
            context = result.as_context_dict()
            context["page"] = self.index_page
            rendered_html = render_to_string(
                "blog/partials/_blog_index_results.html",
                context,
                request=request,
            )

        query_count = len(ctx.captured_queries)
        print("\n--- QUERIES IN FULL LISTING RENDER ---")
        for i, q in enumerate(ctx.captured_queries):
            print(f"[{i+1}] {q['sql']}")
        print("--- END QUERIES ---\n")
        self.assertLessEqual(
            query_count,
            10,
            f"完整列表模板渲染业务 SQL 超出预算: 实际 {query_count} 次，预算 <= 10 次",
        )
        print("\n--- QUERIES IN FULL LISTING RENDER ---")
        for i, q in enumerate(ctx.captured_queries):
            print(f"[{i+1}] {q['sql']}")
        print("--- END QUERIES ---\n")
        self.assertEqual(result.total_results, 20)
        self.assertIn("公文测试文章-01", rendered_html)
        self.assertIn("公文测试文章-20", rendered_html)

    def test_api_json_output_budget_and_contract(self):
        """验证 build_listing(output_format='json') 契约完整且业务 SQL <= 10."""
        request = self.factory.get("/api/v1/blog/index/1/results/")

        with CaptureQueriesContext(connection) as ctx:
            result = build_listing(request, self.index_page, {}, output_format="json")

        query_count = len(ctx.captured_queries)

        self.assertLessEqual(
            query_count,
            10,
            f"API JSON 装配业务 SQL 超出预算: 实际 {query_count} 次，预算 <= 10 次",
        )
        self.assertIsNotNone(result.payload)
        payload = result.payload
        self.assertEqual(payload["result_count"], 20)
        self.assertEqual(payload["pagination"]["page"], 1)
        self.assertEqual(payload["pagination"]["page_size"], 20)
        self.assertEqual(payload["pagination"]["total_pages"], 1)
        self.assertFalse(payload["pagination"]["has_next"])
        self.assertIn("公文测试文章-01", payload["html"])
        self.assertIn("filters", payload)

    @override_settings(BLOG_INDEX_LISTING_ENGINE_V2=True)
    def test_blog_index_results_api_view_delegates_to_listing_service(self):
        """验证视图层 blog_index_results_api 启用 V2 开关后顺利委托并满足 SQL <= 10."""
        request = self.factory.get(f"/api/v1/blog/index/{self.index_page.pk}/results/")

        with CaptureQueriesContext(connection) as ctx:
            response = blog_index_results_api(request, self.index_page.pk)

        query_count = len(ctx.captured_queries)

        self.assertLessEqual(
            query_count,
            10,
            f"API 视图业务 SQL 超出预算: 实际 {query_count} 次，预算 <= 10 次",
        )
        self.assertEqual(response.status_code, 200)
        content = json.loads(response.content.decode("utf-8"))
        self.assertTrue(content["ok"])
        self.assertEqual(content["data"]["result_count"], 20)
        self.assertIn("公文测试文章-20", content["data"]["html"])

    @override_settings(BLOG_INDEX_LISTING_ENGINE_V2=True)
    def test_blog_index_page_get_listing_context_delegation(self):
        """验证 BlogIndexPage.get_listing_context 在启用 V2 时委托至 ListingService."""
        request = self.factory.get(self.index_page.url or "/")
        with CaptureQueriesContext(connection) as ctx:
            ctx_data = self.index_page.get_listing_context({}, request=request)

        query_count = len(ctx.captured_queries)
        self.assertLessEqual(
            query_count,
            10,
            f"BlogIndexPage.get_listing_context 业务 SQL 超出预算: 实际 {query_count} 次，预算 <= 10 次",
        )
        self.assertEqual(len(ctx_data["blog_pages"]), 20)
        self.assertEqual(ctx_data["total_results"], 20)

    @override_settings(BLOG_INDEX_LISTING_ENGINE_V2=True, BLOG_INDEX_CACHE_V2=True)
    def test_cache_hit_zero_sql_queries(self):
        """验证 L2 Redis 列表缓存命中时，0 业务 SQL 查询."""
        request = self.factory.get(self.index_page.url or "/zh-hans/category/")

        # 1. 首次查询：冷缓存穿透填充
        first_result = build_listing(request, self.index_page, {}, output_format="json")
        self.assertEqual(first_result.cache_status, "miss")

        # 2. 二次查询：热缓存命中
        with CaptureQueriesContext(connection) as ctx:
            second_result = build_listing(request, self.index_page, {}, output_format="json")

        hit_queries = len(ctx.captured_queries)
        self.assertEqual(second_result.cache_status, "hit")
        self.assertEqual(
            hit_queries,
            0,
            f"热缓存命中时产生意外 SQL 查询: {hit_queries} 次，必须为 0 次！",
        )
        self.assertEqual(second_result.total_results, 20)
        self.assertIn("公文测试文章-01", second_result.html)

    def test_polymorphic_query_switch_preserves_query_budget(self):
        """验证 BLOG_INDEX_COMPAT_POLYMORPHIC_QUERY 开关在 True/False 下均符合预算."""
        request = self.factory.get(self.index_page.url or "/zh-hans/category/")

        with override_settings(BLOG_INDEX_COMPAT_POLYMORPHIC_QUERY=True):
            with CaptureQueriesContext(connection) as ctx_poly:
                res_poly = build_listing(request, self.index_page, {})
            self.assertLessEqual(len(ctx_poly.captured_queries), 10)
            self.assertEqual(res_poly.total_results, 20)

        with override_settings(BLOG_INDEX_COMPAT_POLYMORPHIC_QUERY=False):
            with CaptureQueriesContext(connection) as ctx_blog:
                res_blog = build_listing(request, self.index_page, {})
            self.assertLessEqual(len(ctx_blog.captured_queries), 10)
            self.assertEqual(res_blog.total_results, 20)
    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_orig_root_page", None):
            site = Site.objects.filter(is_default_site=True).first()
            if site and cls._orig_root_page:
                site.root_page = cls._orig_root_page
                site.save()
        super().tearDownClass()
