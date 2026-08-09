"""博客订阅源的缓存与失效服务。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.core.cache import caches
from django.db import transaction
from wagtail.models import Locale, Site

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlogFeedScope:
    """一个公开订阅源的站点、语言和内容范围边界。"""

    site_id: int
    locale_id: int
    scope_type: str = "global"
    scope_id: int = 0
    scope_slug: str = ""
    scope_label: str = ""


class BlogFeedCache:
    """管理RSS和Atom XML的Redis缓存代次。"""

    def __init__(self) -> None:
        self.cache = caches[getattr(settings, "BLOG_FEED_CACHE_ALIAS", "default")]
        self.namespace = getattr(settings, "BLOG_FEED_CACHE_NAMESPACE", "blog-feed:v1")
        self.timeout = getattr(settings, "BLOG_FEED_CACHE_TTL", 300)

    def _generation_key(self, scope: BlogFeedScope) -> str:
        return f"{self.namespace}:generation:{scope.site_id}:{scope.locale_id}"

    def _payload_key(
        self,
        scope: BlogFeedScope,
        generation: str,
        feed_format: str,
        origin: str,
    ) -> str:
        # 同一站点可能经不同已验证域名访问，XML中的绝对链接必须与请求来源一致。
        origin_digest = sha256(origin.encode("utf-8")).hexdigest()[:16]
        return (
            f"{self.namespace}:payload:{scope.site_id}:{scope.locale_id}:"
            f"{scope.scope_type}:{scope.scope_id}:{generation}:"
            f"{origin_digest}:{feed_format}"
        )

    @staticmethod
    def _new_generation() -> str:
        """生成不可预测的代次，避免代次键丢失后命中历史XML。"""

        return uuid4().hex

    def get_generation(self, scope: BlogFeedScope) -> str:
        """读取当前代次；首次使用时创建不设过期时间的随机标识。"""

        key = self._generation_key(scope)
        try:
            generation = self.cache.get(key)
            if isinstance(generation, str) and generation:
                return generation

            candidate = self._new_generation()
            if generation is None and self.cache.add(key, candidate, timeout=None):
                return candidate

            generation = self.cache.get(key)
            if isinstance(generation, str) and generation:
                return generation

            # 旧版数字代次或异常值不能继续复用，防止旧XML在缓存单键被驱逐后重新生效。
            self.cache.set(key, candidate, timeout=None)
            return candidate
        except Exception:
            logger.warning(
                "feed_generation_read_failed site_id=%s locale_id=%s",
                scope.site_id,
                scope.locale_id,
                exc_info=True,
            )
            return self._new_generation()

    def get_payload(
        self,
        scope: BlogFeedScope,
        feed_format: str,
        origin: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """按当前代次读取Feed响应数据；缓存故障按未命中处理。"""

        generation = self.get_generation(scope)
        try:
            payload = self.cache.get(
                self._payload_key(scope, generation, feed_format, origin)
            )
        except Exception:
            logger.warning(
                "feed_cache_read_failed site_id=%s locale_id=%s format=%s",
                scope.site_id,
                scope.locale_id,
                feed_format,
                exc_info=True,
            )
            payload = None
        if payload is not None and not self._is_valid_payload(payload):
            # 缓存损坏时实时生成Feed，不能把Redis序列化异常扩大为用户可见的500。
            logger.warning(
                "feed_cache_payload_invalid site_id=%s locale_id=%s format=%s",
                scope.site_id,
                scope.locale_id,
                feed_format,
            )
            payload = None
        return generation, payload

    @staticmethod
    def _is_valid_payload(payload: Any) -> bool:
        """只接受本服务写入的最小响应结构，拒绝损坏或过期的缓存值。"""

        return (
            isinstance(payload, dict)
            and isinstance(payload.get("xml"), bytes)
            and isinstance(payload.get("content_type"), str)
            and isinstance(payload.get("etag"), str)
        )

    def set_payload(
        self,
        scope: BlogFeedScope,
        generation: str,
        feed_format: str,
        origin: str,
        payload: dict[str, Any],
    ) -> None:
        """写入当前代次的XML；写缓存失败不影响已生成的响应。"""

        try:
            self.cache.set(
                self._payload_key(scope, generation, feed_format, origin),
                payload,
                timeout=self.timeout,
            )
        except Exception:
            logger.warning(
                "feed_cache_write_failed site_id=%s locale_id=%s format=%s",
                scope.site_id,
                scope.locale_id,
                feed_format,
                exc_info=True,
            )

    def bump_generation(self, scope: BlogFeedScope) -> str | None:
        """切换随机代次，让新请求永远不再读取旧XML。"""

        key = self._generation_key(scope)
        try:
            generation = self._new_generation()
            self.cache.set(key, generation, timeout=None)
        except Exception:
            logger.warning(
                "feed_generation_bump_failed site_id=%s locale_id=%s",
                scope.site_id,
                scope.locale_id,
                exc_info=True,
            )
            return None

        logger.info(
            "feed_cache_generation_bumped site_id=%s locale_id=%s generation=%s",
            scope.site_id,
            scope.locale_id,
            generation,
        )
        return generation


class BlogFeedInvalidationService:
    """将内容变更转换为提交后的Feed缓存失效。"""

    @staticmethod
    def scope_for_page(page) -> BlogFeedScope | None:
        """在页面仍可路由时取得其所属站点和语言。"""

        if not getattr(page, "locale_id", None):
            return None
        try:
            site = page.get_site()
        except Exception:
            logger.warning(
                "feed_scope_resolve_failed page_id=%s",
                getattr(page, "pk", None),
                exc_info=True,
            )
            return None
        if site is None:
            return None
        return BlogFeedScope(site_id=site.pk, locale_id=page.locale_id)

    @classmethod
    def schedule_scope(cls, scope: BlogFeedScope | None) -> None:
        """只在事务提交成功后提升代次，避免回滚时错误清理公开Feed。"""

        if scope is None:
            cls.schedule_all()
            return
        transaction.on_commit(lambda: BlogFeedCache().bump_generation(scope))

    @classmethod
    def schedule_site(cls, site_id: int) -> None:
        """在站点配置变化后刷新该站点的全部语言Feed。"""

        def invalidate_site() -> None:
            cache = BlogFeedCache()
            for locale_id in Locale.objects.values_list("pk", flat=True):
                cache.bump_generation(BlogFeedScope(site_id=site_id, locale_id=locale_id))

        transaction.on_commit(invalidate_site)

    @classmethod
    def schedule_all(cls) -> None:
        """关联Snippet变更时保守刷新全部站点和语言，避免昂贵且脆弱的反查。"""

        def invalidate_all() -> None:
            cache = BlogFeedCache()
            site_ids = Site.objects.values_list("pk", flat=True)
            locale_ids = list(Locale.objects.values_list("pk", flat=True))
            for site_id in site_ids:
                for locale_id in locale_ids:
                    cache.bump_generation(
                        BlogFeedScope(site_id=site_id, locale_id=locale_id)
                    )

        transaction.on_commit(invalidate_all)
