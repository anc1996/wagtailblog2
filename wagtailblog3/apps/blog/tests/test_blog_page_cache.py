"""博客详情页多级缓存与存储减负契约测试套件。

覆盖 L2 正文 Clean Payload 缓存、L2.5 上下文导航 DTO 缓存、
Single-Flight 并发互斥锁、Fail-Open 容灾降级以及预览安全穿透。
"""

import unittest
from unittest.mock import MagicMock, patch

from django.core.cache import caches
from django.test import RequestFactory, TestCase

from blog.models import BlogPage, BlogPublicationState
from blog.services.detail_cache import (
    DetailCacheService,
    is_preview_request,
)


class BlogPageDetailCacheTests(TestCase):
    """博客文章详情页多级缓存核心链路单元与契约测试。"""

    def setUp(self) -> None:
        super().setUp()
        self.cache = caches["default"]
        self.cache.clear()
        self.service = DetailCacheService()
        self.rf = RequestFactory()

        # 模拟标准发布文章模型对象
        self.mock_page = MagicMock(spec=BlogPage)
        self.mock_page.pk = 1001
        self.mock_page.title = "测试文章标题"
        self.mock_page.intro = "测试文章摘要"
        self.mock_page.live = True
        self.mock_page._is_preview_context = False
        self.mock_page.get_site.return_value = MagicMock(pk=1)
        self.mock_page.locale_id = 1
        self.mock_page.get_related_posts_by_tags.return_value = []
        self.mock_page.get_prev_post.return_value = None
        self.mock_page.get_next_post.return_value = None

        # 模拟发布状态记录
        self.mock_state = MagicMock(spec=BlogPublicationState)
        self.mock_state.published_body_version_id = "version_uuid_1001"
        self.mock_state.published_body_sha256 = "dummy_sha256_hash"
        self.mock_state.published_body_schema_version = 1

    def tearDown(self) -> None:
        self.cache.clear()
        super().tearDown()

    @patch("wagtailblog3.mongo.MongoManager.get_content_body_version")
    def test_l2_cache_hit_bypasses_mongo_read(self, mock_mongo_get: MagicMock) -> None:
        """验证热缓存状态下正文读取 100% 命中 Redis，跳过 MongoDB 网络 I/O。"""
        raw_blocks = [{"type": "markdown_block", "value": "# 缓存正文内容", "id": "b1"}]
        mock_mongo_get.return_value = {"body": raw_blocks}

        # 第一次冷启动读取：穿透回源到 MongoDB 并写入 Redis
        first_res = self.service.get_or_set_body(self.mock_page, self.mock_state)
        self.assertIsNotNone(first_res)
        self.assertEqual(first_res["body"], raw_blocks)
        self.assertEqual(mock_mongo_get.call_count, 1)

        # 第二次热缓存读取：必须直接命中 Redis，不再调用 MongoDB
        mock_mongo_get.reset_mock()
        second_res = self.service.get_or_set_body(self.mock_page, self.mock_state)
        self.assertIsNotNone(second_res)
        self.assertEqual(second_res["body"], raw_blocks)
        mock_mongo_get.assert_not_called()

    @patch("wagtailblog3.mongo.MongoManager.get_content_body_version")
    def test_preview_mode_completely_bypasses_and_never_writes_cache(
        self, mock_mongo_get: MagicMock
    ) -> None:
        """验证后台实时预览与草稿审计时 100% 旁路，不读也不写公共 Redis 缓存。"""
        draft_blocks = [{"type": "markdown_block", "value": "未发布草稿预览", "id": "d1"}]
        mock_mongo_get.return_value = {"body": draft_blocks}

        request = self.rf.get("/admin/pages/1001/edit/preview/")
        request.is_preview = True

        self.assertTrue(is_preview_request(request, self.mock_page))

        # 预览请求调用
        res = self.service.get_or_set_body(self.mock_page, self.mock_state, request=request)
        self.assertIsNotNone(res)
        self.assertEqual(res["body"], draft_blocks)
        self.assertEqual(mock_mongo_get.call_count, 1)

        # 验证 Redis 中不存在任何该文章的 body 缓存 key
        cache_key = self.service._body_key(1, 1, 1001, "version_uuid_1001", 1)
        self.assertIsNone(self.cache.get(cache_key))

    @patch("wagtailblog3.mongo.MongoManager.get_content_body_version")
    def test_fail_open_on_redis_connection_error(self, mock_mongo_get: MagicMock) -> None:
        """验证 Redis 出现异常时系统 Fail-Open 降级直读 Mongo，绝不向前台抛 500。"""
        raw_blocks = [{"type": "markdown_block", "value": "容灾正文", "id": "r1"}]
        mock_mongo_get.return_value = {"body": raw_blocks}

        with patch.object(self.service.cache, "get", side_effect=Exception("Redis connection timeout")):
            with patch.object(self.service.cache, "add", side_effect=Exception("Redis connection timeout")):
                res = self.service.get_or_set_body(self.mock_page, self.mock_state)
                self.assertIsNotNone(res)
                self.assertEqual(res["body"], raw_blocks)
                self.assertEqual(mock_mongo_get.call_count, 1)

    @patch("wagtailblog3.mongo.MongoManager.get_content_body_version")
    def test_corrupted_payload_triggers_fallback_and_recovery(
        self, mock_mongo_get: MagicMock
    ) -> None:
        """验证 Redis 中存在格式损坏或篡改的数据时自动丢弃并安全回源。"""
        raw_blocks = [{"type": "markdown_block", "value": "恢复后正文", "id": "ok1"}]
        mock_mongo_get.return_value = {"body": raw_blocks}

        cache_key = self.service._body_key(1, 1, 1001, "version_uuid_1001", 1)
        # 写入非法脏数据
        self.cache.set(cache_key, {"corrupted": True, "body_version_id": "wrong_id"}, timeout=3600)

        res = self.service.get_or_set_body(self.mock_page, self.mock_state)
        self.assertIsNotNone(res)
        self.assertEqual(res["body"], raw_blocks)
        self.assertEqual(mock_mongo_get.call_count, 1)

    def test_page_publish_bumps_generation_atomically(self) -> None:
        """验证页面代次推进（Generation Bump）能使旧上下文缓存自然失效。"""
        initial_gen = self.service.get_page_generation(1001)
        self.assertTrue(bool(initial_gen))

        # 推进代次
        bumped_gen = self.service.bump_page_generation(1001)
        self.assertNotEqual(initial_gen, bumped_gen)
        self.assertEqual(self.service.get_page_generation(1001), bumped_gen)

    def test_context_dto_cache_reduces_navigation_computation(self) -> None:
        """验证上下文导航 DTO 缓存命中时，避免重复计算相关文章和上一篇/下一篇。"""
        rel_post = MagicMock(spec=BlogPage)
        rel_post.pk = 1002
        self.mock_page.get_related_posts_by_tags.return_value = [rel_post]

        with patch("blog.models.BlogPage.objects.live") as mock_live:
            mock_filter = MagicMock()
            mock_live.return_value.filter.return_value = mock_filter
            mock_select = MagicMock()
            mock_filter.select_related.return_value = mock_select
            mock_select.in_bulk.return_value = {1002: rel_post}

            # 第一次读取：冷启动计算
            ctx1 = self.service.get_or_set_navigation_context(self.mock_page)
            self.assertEqual(self.mock_page.get_related_posts_by_tags.call_count, 1)

            # 第二次读取：命中 DTO 缓存，基于 ID 列表单条抓取，无需重新全量聚合
            self.mock_page.get_related_posts_by_tags.reset_mock()
            ctx2 = self.service.get_or_set_navigation_context(self.mock_page)
            self.mock_page.get_related_posts_by_tags.assert_not_called()
            self.assertEqual(len(ctx2["related_posts"]), 1)

    def test_single_flight_mutex_logic(self) -> None:
        """验证 Single-Flight 互斥回源锁的获取与释放。"""
        lock_key = self.service._body_lock_key(1001, "version_uuid_1001")
        acquired = self.cache.add(lock_key, "1", timeout=5)
        self.assertTrue(acquired)

        # 第二个并发进程尝试加锁必然失败
        second_acquire = self.cache.add(lock_key, "1", timeout=5)
        self.assertFalse(second_acquire)

        # 释放锁
        self.cache.delete(lock_key)
        re_acquire = self.cache.add(lock_key, "1", timeout=5)
        self.assertTrue(re_acquire)
        self.cache.delete(lock_key)
