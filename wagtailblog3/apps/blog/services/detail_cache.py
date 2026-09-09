"""博客文章详情页（BlogPage）多级缓存与上下文减负服务。

提供不可变正文版本（L2）缓存、页面上下文导航（L2.5）轻量 DTO 缓存、
Single-Flight 互斥回源锁、页面代次管理以及 Fail-Open 容灾降级机制。
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.core.cache import caches
from django.db import transaction
from wagtailblog3.mongo import MongoRevisionReadError

logger = logging.getLogger(__name__)

DEFAULT_BODY_CACHE_TTL = 86400  # 不可变正文版本缓存 24 小时
DEFAULT_NAV_CACHE_TTL = 14400   # 上下文导航缓存 4 小时
DEFAULT_LOCK_TTL = 5            # 回源互斥锁最长 5 秒租约


def is_preview_request(request: Any = None, page: Any = None) -> bool:
    """确定当前请求是否属于后台实时预览、历史 Revision 审计或未发布草稿。"""
    if request is not None:
        if getattr(request, "in_preview_panel", False) or getattr(request, "is_preview", False):
            return True
        resolver_match = getattr(request, "resolver_match", None)
        if resolver_match and getattr(resolver_match, "url_name", "") in {"preview", "preview_revision"}:
            return True
    if page is not None:
        if (
            not getattr(page, "live", True)
            or getattr(page, "_is_preview_context", False)
            or getattr(page, "is_preview", False)
            or getattr(page, "_is_preview", False)
        ):
            return True
    return False


class DetailCacheService:
    """博客详情页高频访问缓存调度器。"""

    @staticmethod
    def is_preview(request: Any = None, page: Any = None) -> bool:
        """判断请求是否属于后台实时预览或未发布草稿。"""
        return is_preview_request(request, page)

    def __init__(self, cache_alias: str | None = None) -> None:
        alias = cache_alias or getattr(settings, "BLOG_DETAIL_CACHE_ALIAS", "default")
        self.cache = caches[alias]
        self.namespace = getattr(settings, "BLOG_DETAIL_CACHE_NAMESPACE", "blog-detail:v1")
        self.body_ttl = getattr(settings, "BLOG_DETAIL_BODY_CACHE_TTL", DEFAULT_BODY_CACHE_TTL)
        self.nav_ttl = getattr(settings, "BLOG_DETAIL_NAV_CACHE_TTL", DEFAULT_NAV_CACHE_TTL)
        self.lock_ttl = getattr(settings, "BLOG_DETAIL_LOCK_TTL", DEFAULT_LOCK_TTL)

    @staticmethod
    def _get_mongo_manager() -> Any:
        """获取 MongoDB 管理器实例，优先支持测试中的 blog.models.MongoManager 打桩。"""
        try:
            from blog.models import MongoManager
            return MongoManager()
        except (ImportError, AttributeError):
            from wagtailblog3.mongo import MongoManager
            return MongoManager()

    def _body_key(
        self, site_id: int, locale_id: int, page_id: int, body_version_id: str, schema_version: int
    ) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.detail_body_key(
            self.namespace, site_id, locale_id, page_id, body_version_id, schema_version
        )

    def _body_lock_key(self, page_id: int, body_version_id: str) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.detail_body_lock_key(self.namespace, page_id, body_version_id)

    def _generation_key(self, page_id: int) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.detail_generation_key(self.namespace, page_id)

    def _nav_key(self, site_id: int, locale_id: int, page_id: int, generation: str) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.detail_nav_key(self.namespace, site_id, locale_id, page_id, generation)

    @staticmethod
    def _new_generation() -> str:
        return uuid4().hex

    def get_page_generation(self, page_id: int) -> str:
        """读取页面上下文代次；若缓存缺失则从状态表读取或生成初始代次并写入缓存。"""
        key = self._generation_key(page_id)
        try:
            gen = self.cache.get(key)
            if isinstance(gen, str) and gen:
                return gen
            from blog.models import BlogPublicationState
            state = BlogPublicationState.objects.filter(page_id=page_id).first()
            if state and getattr(state, "publication_generation", None) is not None:
                candidate = str(state.publication_generation)
            else:
                candidate = self._new_generation()
            self.cache.set(key, candidate, timeout=None)
            return candidate
        except Exception:
            logger.warning("detail_generation_read_failed page_id=%s", page_id, exc_info=True)
            return self._new_generation()

    def bump_page_generation(self, page_id: int) -> str:
        """推进页面代次，使依赖该代次的所有导航与片段缓存自然淘汰。"""
        key = self._generation_key(page_id)
        new_gen = self._new_generation()
        try:
            self.cache.set(key, new_gen, timeout=None)
        except Exception:
            logger.warning("detail_generation_bump_failed page_id=%s", page_id, exc_info=True)
        return new_gen

    def get_or_set_body(
        self,
        page: Any,
        state: Any,
        request: Any = None,
    ) -> dict[str, Any] | None:
        """服务层正文读取门禁：仅对非预览正式请求启用 L2 Redis 缓存与 Single-Flight 回源。"""
        page_id = page.pk
        published_version_id = getattr(state, "published_body_version_id", None)
        if not published_version_id:
            return None

        sha256_hash = getattr(state, "published_body_sha256", "") or ""
        schema_version = getattr(state, "published_body_schema_version", 0) or 0

        # 预览或草稿请求直接穿透到 MongoDB，绝对不读取也不写入公共 Redis 缓存
        if is_preview_request(request, page):
            try:
                version = self._get_mongo_manager().get_content_body_version(
                    "blog_page", page_id, published_version_id, sha256_hash, schema_version
                )
                if not isinstance(version.get("body"), list):
                    return None
                return {
                    "_id": published_version_id,
                    "page_id": page_id,
                    "title": page.title,
                    "intro": page.intro,
                    "body": version["body"],
                }
            except Exception:
                return None

        site_id = 0
        try:
            site = page.get_site()
            if site:
                site_id = site.pk
        except Exception:
            pass
        locale_id = getattr(page, "locale_id", 0) or 0

        cache_key = self._body_key(site_id, locale_id, page_id, published_version_id, schema_version)

        # 1. 尝试从 Redis 读取 L2 缓存
        try:
            cached_payload = self.cache.get(cache_key)
            if self._is_valid_body_payload(cached_payload, published_version_id, sha256_hash):
                return {
                    "_id": published_version_id,
                    "page_id": page_id,
                    "title": page.title,
                    "intro": page.intro,
                    "body": cached_payload["body"],
                }
        except Exception:
            logger.warning("detail_l2_cache_get_failed page_id=%s", page_id, exc_info=True)

        # 2. 未命中：Single-Flight 分布式互斥锁回源
        lock_key = self._body_lock_key(page_id, published_version_id)
        acquired = False
        try:
            acquired = bool(self.cache.add(lock_key, "1", timeout=self.lock_ttl))
        except Exception:
            acquired = True  # Redis 异常时降级允许回源

        if acquired:
            try:
                version = self._get_mongo_manager().get_content_body_version(
                    "blog_page", page_id, published_version_id, sha256_hash, schema_version
                )
                if not isinstance(version.get("body"), list):
                    return None

                payload = {
                    "body": version["body"],
                    "body_version_id": published_version_id,
                    "body_sha256": sha256_hash,
                    "body_schema_version": schema_version,
                }
                try:
                    self.cache.set(cache_key, payload, timeout=self.body_ttl)
                except Exception:
                    logger.warning("detail_l2_cache_set_failed page_id=%s", page_id, exc_info=True)

                return {
                    "_id": published_version_id,
                    "page_id": page_id,
                    "title": page.title,
                    "intro": page.intro,
                    "body": version["body"],
                }
            except MongoRevisionReadError as exc:
                logger.warning("detail_mongo_read_unavailable page_id=%s error=%s", page_id, type(exc).__name__)
                return None
            except Exception:
                logger.warning("detail_mongo_fetch_failed page_id=%s", page_id, exc_info=True)
                return None
            finally:
                try:
                    self.cache.delete(lock_key)
                except Exception:
                    pass
        else:
            # 3. 未抢到互斥锁：等待 50ms 后重新读取 Redis
            time.sleep(0.05)
            try:
                cached_payload = self.cache.get(cache_key)
                if self._is_valid_body_payload(cached_payload, published_version_id, sha256_hash):
                    return {
                        "_id": published_version_id,
                        "page_id": page_id,
                        "title": page.title,
                        "intro": page.intro,
                        "body": cached_payload["body"],
                    }
            except Exception:
                pass

            # 若自旋重试仍未命中，Fail-Open 直读 Mongo 保证前台绝不 500
            try:
                version = self._get_mongo_manager().get_content_body_version(
                    "blog_page", page_id, published_version_id, sha256_hash, schema_version
                )
                if not isinstance(version.get("body"), list):
                    return None
                return {
                    "_id": published_version_id,
                    "page_id": page_id,
                    "title": page.title,
                    "intro": page.intro,
                    "body": version["body"],
                }
            except Exception:
                return None

    @staticmethod
    def _is_valid_body_payload(payload: Any, expected_version_id: str, expected_sha256: str) -> bool:
        """校验从缓存反序列化的正文结构完整性与版本防篡改哈希。"""
        if not isinstance(payload, dict):
            return False
        if payload.get("body_version_id") != expected_version_id:
            return False
        if expected_sha256 and payload.get("body_sha256") != expected_sha256:
            return False
        if not isinstance(payload.get("body"), list):
            return False
        return True

    def get_or_set_navigation_context(self, page: Any, request: Any = None) -> dict[str, Any]:
        """获取相关推荐与上下篇导航；在热缓存下基于 ID 列表单条查询，压降 8~10 次复杂 SQL。"""
        if not page.pk or is_preview_request(request, page):
            return {
                "related_posts": list(page.get_related_posts_by_tags()),
                "prev_post": page.get_prev_post(),
                "next_post": page.get_next_post(),
            }

        site_id = 0
        try:
            site = page.get_site()
            if site:
                site_id = site.pk
        except Exception:
            pass
        locale_id = getattr(page, "locale_id", 0) or 0
        generation = self.get_page_generation(page.pk)
        nav_key = self._nav_key(site_id, locale_id, page.pk, generation)

        try:
            cached_nav = self.cache.get(nav_key)
            if isinstance(cached_nav, dict) and "related_ids" in cached_nav:
                related_ids = cached_nav.get("related_ids", [])
                prev_id = cached_nav.get("prev_id")
                next_id = cached_nav.get("next_id")

                from blog.models import BlogPage
                needed_ids: set[int] = set()
                for item in related_ids:
                    if isinstance(item, int):
                        needed_ids.add(item)
                if isinstance(prev_id, int):
                    needed_ids.add(prev_id)
                if isinstance(next_id, int):
                    needed_ids.add(next_id)

                if needed_ids:
                    posts_by_id = (
                        BlogPage.objects.live()
                        .filter(id__in=needed_ids)
                        .select_related("featured_image")
                        .in_bulk()
                    )
                else:
                    posts_by_id = {}

                related_posts = [posts_by_id[pid] for pid in related_ids if pid in posts_by_id]
                prev_post = posts_by_id.get(prev_id) if prev_id else None
                next_post = posts_by_id.get(next_id) if next_id else None

                return {
                    "related_posts": related_posts,
                    "prev_post": prev_post,
                    "next_post": next_post,
                }
        except Exception:
            logger.warning("detail_nav_cache_read_failed page_id=%s", page.pk, exc_info=True)

        # 缓存未命中：执行原生计算并沉淀 ID 列表
        try:
            raw_related = list(page.get_related_posts_by_tags())
            raw_prev = page.get_prev_post()
            raw_next = page.get_next_post()

            nav_data = {
                "related_ids": [p.pk for p in raw_related if getattr(p, "pk", None)],
                "prev_id": raw_prev.pk if raw_prev and getattr(raw_prev, "pk", None) else None,
                "next_id": raw_next.pk if raw_next and getattr(raw_next, "pk", None) else None,
            }
            try:
                self.cache.set(nav_key, nav_data, timeout=self.nav_ttl)
            except Exception:
                logger.warning("detail_nav_cache_write_failed page_id=%s", page.pk, exc_info=True)

            return {
                "related_posts": raw_related,
                "prev_post": raw_prev,
                "next_post": raw_next,
            }
        except Exception:
            logger.warning("detail_nav_compute_failed page_id=%s", page.pk, exc_info=True)
            return {
                "related_posts": [],
                "prev_post": None,
                "next_post": None,
            }

    @classmethod
    def schedule_invalidation_for_page(cls, page: Any) -> None:
        """在事务成功提交后推进当前文章及相邻文章代次。"""
        if not getattr(page, "pk", None):
            return

        page_id = page.pk
        adjacent_ids: list[int] = []
        try:
            prev_p = page.get_prev_post()
            if prev_p and getattr(prev_p, "pk", None):
                adjacent_ids.append(prev_p.pk)
            next_p = page.get_next_post()
            if next_p and getattr(next_p, "pk", None):
                adjacent_ids.append(next_p.pk)
        except Exception:
            pass

        def _do_bump() -> None:
            service = cls()
            service.bump_page_generation(page_id)
            for adj_id in adjacent_ids:
                service.bump_page_generation(adj_id)

        try:
            transaction.on_commit(_do_bump)
        except Exception:
            # 兼容非事务上下文与 SimpleTestCase 沙箱环境
            pass