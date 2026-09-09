"""
Redis 键名全域针对性识别协议与入口网关定向测试矩阵 (test_redis_protocol.py).

覆盖核心契约：
1. RedisKeyProtocol 工厂方法产物规范性；
2. unified_key_maker 与 unified_reverse_key 的双向可逆性与幂等性；
3. Django Cache (django-redis) 与 delete_pattern 深度集成；
4. Celery Broker 与 ResultBackend 全局键前缀隔离性。
"""
from __future__ import annotations

import os
from unittest.mock import patch

from django.core.cache import caches
from django.test import SimpleTestCase, override_settings

from blog.services.redis_protocol import RedisKeyProtocol
from wagtailblog3.celery_app import app as celery_app
from wagtailblog3.settings.database import (
	get_celery_config,
	unified_key_maker,
	unified_reverse_key,
)


class RedisKeyProtocolTests(SimpleTestCase):
	"""验证业务层 RedisKeyProtocol 工厂键生成规则."""

	def test_listing_keys_format(self):
		"""验证列表页缓存键、锁键与代次键规范."""
		cache_key = RedisKeyProtocol.listing_cache_key(
			site_id=1, locale_id=2, index_id=10, query_hash="abc12345", generation=3
		)
		self.assertEqual(cache_key, "wblog:listing:v2:s_1:loc_2:idx_10:gen_3:q_abc12345")

		lock_key = RedisKeyProtocol.listing_lock_key(index_page_id=10, lock_hash="def67890")
		self.assertEqual(lock_key, "wblog:listing:v2:lock:10:def67890")

		gen_key = RedisKeyProtocol.listing_generation_key(site_id=1, locale_id=2, index_page_id=10)
		self.assertEqual(gen_key, "wblog:list:v2:generation:1:2:10")

	def test_sidebar_keys_format(self):
		"""验证侧边栏归档与作者键规范."""
		agg_key = RedisKeyProtocol.sidebar_archive_aggregate_key(
			site_id=1, locale_id=2, generation=5, policy_version="1"
		)
		self.assertEqual(agg_key, "wblog:sidebar:v2:archive:aggregate:1:2:5:1")

		lock_key = RedisKeyProtocol.sidebar_archive_lock_key(
			site_id=1, locale_id=2, generation=5, policy_version="1"
		)
		self.assertEqual(lock_key, "wblog:sidebar:v2:archive:lock:1:2:5:1")

		gen_key = RedisKeyProtocol.sidebar_archive_generation_key(site_id=1, locale_id=2)
		self.assertEqual(gen_key, "wblog:sidebar:v2:generation:archive:1:2")

		author_cand_key = RedisKeyProtocol.sidebar_author_candidates_key(
			site_id=1, locale_id=2, generation=3
		)
		self.assertEqual(author_cand_key, "wblog:sidebar:v2:author:candidates:1:2:3")

		pick_key = RedisKeyProtocol.sidebar_author_pick_key(
			site_id=1, locale_id=2, generation=3, time_bucket="2026-09-09-14"
		)
		self.assertEqual(pick_key, "wblog:sidebar:v2:author:pick:1:2:3:2026-09-09-14")

		author_gen_key = RedisKeyProtocol.sidebar_author_generation_key(site_id=1, locale_id=2)
		self.assertEqual(author_gen_key, "wblog:sidebar:v2:generation:author:1:2")

	def test_detail_and_rate_keys_format(self):
		"""验证正文多级缓存与评论频率限制键规范."""
		body_key = RedisKeyProtocol.detail_body_key(
			namespace="blog-detail:v1", site_id=1, locale_id=2, page_id=99,
			body_version_id="mongo_v1", schema_version=1
		)
		self.assertEqual(body_key, "blog-detail:v1:body:1:2:99:mongo_v1:1")

		comment_key = RedisKeyProtocol.comment_rate_key(user_identifier="user_ip_192_168_1_1")
		self.assertEqual(comment_key, "comment:user_ip_192_168_1_1")


class UnifiedKeyGatewayTests(SimpleTestCase):
	"""验证 Django Cache unified_key_maker 与 unified_reverse_key 双向可逆网关."""

	@patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"})
	def test_key_maker_and_reversibility_in_test_env(self):
		"""测试环境下键名包装与逆向解析 100% 双向可逆."""
		test_cases = [
			("wagtail_site_root_paths", "cache", 1),
			("template.cache.sidebar_block.a1b2c3d4", "cache", 1),
			("wblog:listing:v2:s_1:loc_2:idx_3:gen_1:q_hash", "cache", 1),
			("wblog:listing:v2:lock:10:a1b2c3d4", "cache", 1),
			("wblog:sidebar:v2:archive:aggregate:1:2:1:1", "cache", 1),
			("comment:user_88", "rate", 1),
		]

		for raw_key, prefix, version in test_cases:
			physical_key = unified_key_maker(raw_key, prefix, version)
			self.assertTrue(physical_key.startswith("wblog:test:"), f"Key not prefixed: {physical_key}")
			reversed_key = unified_reverse_key(physical_key)
			self.assertEqual(reversed_key, raw_key, f"Mismatch on reverse: {reversed_key} != {raw_key}")

	@patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"})
	def test_key_maker_in_production_env(self):
		"""生产环境下键名包装为 wblog:prod:* 标识."""
		raw_key = "wagtail_site_root_paths"
		physical_key = unified_key_maker(raw_key, "cache", 1)
		self.assertEqual(physical_key, "wblog:prod:cache:wagtail_site_root_paths")
		reversed_key = unified_reverse_key(physical_key)
		self.assertEqual(reversed_key, raw_key)

	@patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"})
	def test_idempotence_guard(self):
		"""幂等防护：若已经携带全域前缀，不可重复包装."""
		already_prefixed = "wblog:test:cache:wagtail_site_root_paths"
		self.assertEqual(unified_key_maker(already_prefixed, "cache", 1), already_prefixed)

	@patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"})
	def test_delete_pattern_version_compatibility(self):
		"""django-redis delete_pattern 会传入字符串版本号 '1'，核验兼容性."""
		# 传入字符串 '1' 时不得在尾部追加 :v1
		pattern_key = unified_key_maker("*sample_query*", "cache", "1")
		self.assertFalse(pattern_key.endswith(":v1"))
		self.assertEqual(pattern_key, "wblog:test:cache:*sample_query*")

		# 传入非默认版本 2 时，正常追加 :v2
		versioned_key = unified_key_maker("sample_query", "cache", 2)
		self.assertTrue(versioned_key.endswith(":v2"))
		self.assertEqual(versioned_key, "wblog:test:cache:sample_query:v2")


class CeleryGatewayTests(SimpleTestCase):
	"""验证 Celery 全局前缀与物理分库回退."""

	@patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"})
	def test_celery_test_env_defaults(self):
		"""测试环境 Celery 默认使用 DB 7 / DB 8 并带 wblog:test:* 前缀."""
		config = get_celery_config("Asia/Shanghai", "127.0.0.1", 6379, "")
		self.assertIn("/7", config["CELERY_BROKER_URL"])
		self.assertIn("/8", config["CELERY_RESULT_BACKEND"])
		self.assertEqual(
			config["CELERY_BROKER_TRANSPORT_OPTIONS"]["global_keyprefix"],
			"wblog:test:broker:",
		)
		self.assertEqual(
			config["CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS"]["global_keyprefix"],
			"wblog:test:result:",
		)

	@patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}, clear=False)
	def test_celery_prod_env_defaults(self):
		"""生产环境 Celery 默认避让商城，使用 DB 14 / DB 15 并带 wblog:prod:* 前缀."""
		# 临时移除测试环境写在 .env.test 里的固定分库覆盖
		with patch.dict(os.environ, {"CELERY_BROKER_DB": "", "CELERY_RESULT_DB": ""}):
			os.environ.pop("CELERY_BROKER_DB", None)
			os.environ.pop("CELERY_RESULT_DB", None)
			config = get_celery_config("Asia/Shanghai", "127.0.0.1", 6379, "")
			self.assertIn("/14", config["CELERY_BROKER_URL"])
			self.assertIn("/15", config["CELERY_RESULT_BACKEND"])
			self.assertEqual(
				config["CELERY_BROKER_TRANSPORT_OPTIONS"]["global_keyprefix"],
				"wblog:prod:broker:",
			)
			self.assertEqual(
				config["CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS"]["global_keyprefix"],
				"wblog:prod:result:",
			)

	@patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production", "CELERY_BROKER_DB": "9", "CELERY_RESULT_DB": "10"})
	def test_celery_env_explicit_override(self):
		"""若环境变量显式指定 Celery DB，则优先尊重配置值."""
		config = get_celery_config("Asia/Shanghai", "127.0.0.1", 6379, "")
		self.assertIn("/9", config["CELERY_BROKER_URL"])
		self.assertIn("/10", config["CELERY_RESULT_BACKEND"])
