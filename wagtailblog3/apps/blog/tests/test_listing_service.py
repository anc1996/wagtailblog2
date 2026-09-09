"""ListingService 单元测试.

测试规范化参数、批量装配逻辑、DTO 契约边界、并发互斥锁（Single-Flight）与 Redis 故障降级（Fail-Open）。
"""

from unittest.mock import MagicMock, patch
from django.core.cache import caches
from django.test import SimpleTestCase, override_settings

from blog.services.listing import (
    ListingCard,
    ListingFilters,
    ListingResult,
    ListingService,
    compute_canonical_url,
    normalize_listing_params,
    _batch_prefetch_post_data,
    build_listing,
)


class ListingServiceUnitTests(SimpleTestCase):
    """测试 ListingService 的纯逻辑与 DTO 契约."""

    def test_normalize_listing_params_defaults(self):
        """测试缺省参数下的标准化保底行为."""
        filters = normalize_listing_params({})
        self.assertEqual(filters.page, 1)
        self.assertEqual(filters.search, "")
        self.assertEqual(filters.start_date, "")
        self.assertEqual(filters.end_date, "")
        self.assertEqual(filters.sort_primary, "date_desc")
        self.assertEqual(filters.sort_secondary, "title_asc")

    def test_normalize_listing_params_invalid_values_are_sanitized(self):
        """测试非法页码、异常日期和未知排序字段的过滤与清洗."""
        filters = normalize_listing_params(
            {
                "page": "-5",
                "search": "  公文与材料   ",
                "start_date": "not-a-date",
                "end_date": "2026-12-31",
                "sort_primary": "injected_sql_drop_table",
                "sort_secondary": "invalid_secondary",
            }
        )
        # 页码自动兜底为 1
        self.assertEqual(filters.page, 1)
        self.assertEqual(filters.search, "公文与材料")
        # 非法日期清空
        self.assertEqual(filters.start_date, "")
        self.assertEqual(filters.end_date, "2026-12-31")
        # 未知排序回退为默认
        self.assertEqual(filters.sort_primary, "date_desc")
        self.assertEqual(filters.sort_secondary, "title_asc")

    def test_normalize_listing_params_primary_sort_rebalances_secondary(self):
        """测试主排序为标题时，次排序选项正确切换为日期."""
        filters = normalize_listing_params(
            {
                "sort_primary": "title_asc",
                "sort_secondary": "date_asc",
            }
        )
        self.assertEqual(filters.sort_primary, "title_asc")
        self.assertEqual(filters.sort_secondary, "date_asc")

    def test_compute_canonical_url_omits_defaults(self):
        """规范 URL 仅在非默认参数时拼接 query string."""
        page = MagicMock()
        page.get_url.return_value = "/zh-hans/news/"

        default_filters = normalize_listing_params({})
        url = compute_canonical_url(page, default_filters)
        self.assertEqual(url, "/zh-hans/news/")

        active_filters = normalize_listing_params(
            {"search": "通知", "page": 2, "sort_primary": "title_asc"}
        )
        url_with_params = compute_canonical_url(page, active_filters)
        self.assertIn("search=%E9%80%9A%E7%9F%A5", url_with_params)
        self.assertIn("page=2", url_with_params)
        self.assertIn("sort_primary=title_asc", url_with_params)

    def test_batch_prefetch_post_data_handles_empty_or_mock_lists_safely(self):
        """测试批量装配对空列表或 Mock 对象的零数据库侵入安全性."""
        # 1. 空列表安全返回
        self.assertEqual(_batch_prefetch_post_data([]), [])

        # 2. Mock 列表安全跳过真实数据库查询
        mock_posts = [MagicMock(pk=1), MagicMock(pk=2)]
        result = _batch_prefetch_post_data(mock_posts)
        self.assertEqual(result, mock_posts)

    def test_listing_result_as_context_dict_contains_no_sensitive_or_mongo_body(self):
        """严格核验 DTO 契约：严禁包含 Mongo 正文、Revision 或敏感用户身份信息."""
        filters = normalize_listing_params({})
        result = ListingResult(
            filters=filters,
            blog_pages=[],
            page_obj=MagicMock(),
            total_results=0,
            has_active_filters=False,
            secondary_sort_options=(("title_asc", "标题"),),
            blog_tag_index_page=None,
            canonical_url="/zh-hans/category/",
            cache_status="miss",
            query_count=5,
            assembly_ms=12.5,
        )

        ctx = result.as_context_dict()
        self.assertIn("blog_pages", ctx)
        self.assertIn("search_query", ctx)
        self.assertIn("canonical_url", ctx)
        self.assertIn("query_count", ctx)

        # 严禁暴露的核心数据字段核查
        for forbidden_key in ("body", "mongo_body", "revision", "raw_content", "user_ip", "session_key"):
            self.assertNotIn(forbidden_key, ctx)

        # 验证字典协议支持
        self.assertEqual(result["canonical_url"], "/zh-hans/category/")
        self.assertEqual(result.get("total_results"), 0)
        self.assertTrue("blog_pages" in result)

    def test_listing_card_dto_isolation_and_mongo_protection(self):
        """测试 ListingCard 绝不包含正文及 mongo_content_id 属性."""
        card = ListingCard(
            pk=101,
            title="测试标题",
            url="/test/",
            date="2026-09-08",
            intro="导言简述",
            featured_image=None,
            featured_rendition=None,
            authors=[],
            tags=[],
            view_count={"total": 10, "today": 2, "total_unique": 8, "today_unique": 2},
            reactions=[],
        )
        self.assertEqual(card.id, 101)
        self.assertEqual(card.specific, card)
        self.assertEqual(card.get_view_count()["total"], 10)
        self.assertEqual(card.get_url(), "/test/")

        # 严格检查核心受保护属性不可读
        with self.assertRaises(AttributeError):
            _ = card.body
        with self.assertRaises(AttributeError):
            _ = card.mongo_content_id
        with self.assertRaises(AttributeError):
            _ = card.live_revision


class ListingServiceConcurrencyAndFailOpenTests(SimpleTestCase):
    """测试 Single-Flight 互斥锁与 Redis 异常 Fail-Open 降级."""

    @override_settings(BLOG_INDEX_CACHE_V2=True)
    def test_single_flight_mutex_acquires_and_releases_with_token(self):
        """测试并发构建时正确获取互斥锁并安全释放 (Token Compare-and-Delete)."""
        captured_tokens = []
        mock_cache = MagicMock()

        def fake_add(key, token, timeout=5):
            captured_tokens.append(token)
            return True

        def fake_get(key):
            if "lock" in key and captured_tokens:
                return captured_tokens[-1]
            return None

        mock_cache.add.side_effect = fake_add
        mock_cache.get.side_effect = fake_get

        service = ListingService()
        service._get_cache = MagicMock(return_value=mock_cache)

        mock_index_page = MagicMock()
        mock_index_page.pk = 99
        mock_index_page.locale_id = 1
        mock_index_page.site.pk = 1
        mock_index_page.listing_cache_generation = "1"
        mock_index_page.get_url.return_value = "/zh-hans/category/"
        mock_index_page.get_children.return_value.live.return_value.public.return_value.annotate.return_value.order_by.return_value = []

        with patch("blog.services.listing._batch_prefetch_post_data", return_value=[]):
            with patch("blog.models.BlogTagIndexPage.objects.live"):
                result = service.build_listing(None, mock_index_page, {})

        # 验证互斥锁加锁调用
        self.assertTrue(mock_cache.add.called)
        lock_call_args = mock_cache.add.call_args
        self.assertIn("wblog:listing:v2:lock:99:", lock_call_args[0][0])
        self.assertEqual(lock_call_args[1].get("timeout") or lock_call_args[0][2], 5)

        # 验证锁释放时成功调用 delete
        self.assertTrue(mock_cache.delete.called)

    @override_settings(BLOG_INDEX_CACHE_V2=True)
    def test_cache_failure_fails_open_gracefully(self):
        """测试 Redis 发生连接中断或超时异常时，系统平稳降级 (Fail-Open) 而不抛出 500."""
        broken_cache = MagicMock()
        broken_cache.get.side_effect = ConnectionError("Redis connection refused")
        broken_cache.add.side_effect = ConnectionError("Redis connection refused")
        broken_cache.set.side_effect = ConnectionError("Redis connection refused")

        service = ListingService()
        service._get_cache = MagicMock(return_value=broken_cache)

        mock_index_page = MagicMock()
        mock_index_page.pk = 99
        mock_index_page.locale_id = 1
        mock_index_page.site.pk = 1
        mock_index_page.listing_cache_generation = "1"
        mock_index_page.get_url.return_value = "/zh-hans/category/"
        mock_index_page.get_children.return_value.live.return_value.public.return_value.annotate.return_value.order_by.return_value = []

        with patch("blog.services.listing._batch_prefetch_post_data", return_value=[]):
            with patch("blog.models.BlogTagIndexPage.objects.live"):
                # 即使 Redis 彻底不可用，依然返回安全结果，cache_status 为 miss
                result = service.build_listing(None, mock_index_page, {})
                self.assertEqual(result.cache_status, "miss")
                self.assertEqual(result.total_results, 0)

    @override_settings(BLOG_INDEX_CACHE_V2=True)
    def test_ssr_cache_then_json_api_reconstructs_payload(self):
        """测试 SSR 首次访问写入缓存后，异步 JSON API 命中缓存能够正确补全 payload 与 html."""
        mock_cache = {}

        def fake_get(key, default=None):
            return mock_cache.get(key, default)

        def fake_set(key, val, timeout=None):
            mock_cache[key] = val

        service = ListingService()
        cache_mock = MagicMock()
        cache_mock.get.side_effect = fake_get
        cache_mock.set.side_effect = fake_set
        cache_mock.add.return_value = True
        cache_mock.delete.return_value = True
        service._get_cache = MagicMock(return_value=cache_mock)

        mock_index_page = MagicMock()
        mock_index_page.pk = 99
        mock_index_page.locale_id = 1
        mock_index_page.site.pk = 1
        mock_index_page.listing_cache_generation = "1"
        mock_index_page.get_url.return_value = "/zh-hans/category/"
        mock_index_page.get_children.return_value.live.return_value.public.return_value.annotate.return_value.order_by.return_value = []

        with patch("blog.services.listing._batch_prefetch_post_data", return_value=[]):
            with patch("blog.models.BlogTagIndexPage.objects.live"):
                with patch("blog.services.listing.render_to_string", return_value="<section>rendered-html</section>"):
                    # 1. 首次为 SSR 访问 (output_format='context')
                    ssr_result = service.build_listing(None, mock_index_page, {}, output_format="context")
                    self.assertEqual(ssr_result.cache_status, "miss")
                    self.assertIsNone(ssr_result.payload)

                    # 2. 紧接着异步 JSON API 请求相同页面与筛选参数 (output_format='json')
                    api_result = service.build_listing(None, mock_index_page, {}, output_format="json")
                    self.assertEqual(api_result.cache_status, "hit")
                    self.assertIsNotNone(api_result.payload)
                    self.assertEqual(api_result.payload["html"], "<section>rendered-html</section>")
                    self.assertEqual(api_result.payload["result_count"], 0)
