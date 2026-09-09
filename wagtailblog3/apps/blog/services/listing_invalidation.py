"""博客分类与列表页精准失效与代次服务 (ListingInvalidationService).

本服务负责全站列表页、侧栏归档与作者侧栏的代次（Generation）管理与生命周期失效契约：
1. 严禁全局 cache.clear() 或 KEYS/SCAN 遍历删除；
2. 缓存 Payload 均带代次标识，发布/下线/删除时仅通过 Redis 原子 INCR 推进代次；
3. 代次变更必须严格绑定 Django transaction.on_commit()，防止事务回滚造成缓存脏写或穿透；
4. 线程级事务去重，防止批量发布或 Hook + Signal 双重触发造成代次无意义膨胀；
5. Redis 故障全链路 Fail-Open 降级，不阻塞前台发布与只读访问。
"""

from __future__ import annotations
import logging
import threading
from dataclasses import dataclass
from typing import Any

from django.core.cache import caches
from django.db import transaction
from wagtail.models import Page, Site

logger = logging.getLogger("blog.services.listing_invalidation")

# 线程局部存储，用于单事务内失效 scope 去重
_local_tx_state = threading.local()


def _get_tx_pending_scopes() -> set["ListingScope"]:
    if not hasattr(_local_tx_state, "pending_listing_scopes"):
        _local_tx_state.pending_listing_scopes = set()
    return _local_tx_state.pending_listing_scopes


def _get_tx_pending_archive_scopes() -> set[tuple[int, int]]:
    if not hasattr(_local_tx_state, "pending_archive_scopes"):
        _local_tx_state.pending_archive_scopes = set()
    return _local_tx_state.pending_archive_scopes


def _get_tx_pending_author_scopes() -> set[tuple[int, int]]:
    if not hasattr(_local_tx_state, "pending_author_scopes"):
        _local_tx_state.pending_author_scopes = set()
    return _local_tx_state.pending_author_scopes


def clear_tx_pending_state() -> None:
    """清理当前线程的待推进事务缓存，防止事务回滚或异常时残留脏状态."""
    _get_tx_pending_scopes().clear()
    _get_tx_pending_archive_scopes().clear()
    _get_tx_pending_author_scopes().clear()


def _clear_tx_pending_state_signal_receiver(*args: Any, **kwargs: Any) -> None:
    clear_tx_pending_state()


from django.core.signals import request_finished
request_finished.connect(_clear_tx_pending_state_signal_receiver, weak=False, dispatch_uid="blog.listing_invalidation.clear_tx_pending_state")

try:
    from celery.signals import task_postrun
    task_postrun.connect(_clear_tx_pending_state_signal_receiver, weak=False, dispatch_uid="blog.listing_invalidation.celery_task_postrun")
except Exception:
    pass


class _InvalidationCommitHandler:
    """事务提交时执行代次自增的回调载体，绑定在 Django 事务 savepoint 生命周期上."""

    def __init__(self, service_cls: type[Any], reason: str = "") -> None:
        self.service_cls = service_cls
        self.reason = reason
        self.listing_scopes: set[ListingScope] = set()
        self.archive_scopes: set[tuple[int, int]] = set()
        self.author_scopes: set[tuple[int, int]] = set()

    def update(
        self,
        listing_scopes: set[ListingScope],
        archive_scopes: set[tuple[int, int]],
        author_scopes: set[tuple[int, int]],
    ) -> None:
        self.listing_scopes.update(listing_scopes)
        self.archive_scopes.update(archive_scopes)
        self.author_scopes.update(author_scopes)

    def __call__(self) -> None:
        clear_tx_pending_state()
        for sc in self.listing_scopes:
            self.service_cls.bump_generation(sc, reason=self.reason)
        for sid, lid in self.archive_scopes:
            self.service_cls.bump_archive_generation(sid, lid, reason=self.reason)
        for sid, lid in self.author_scopes:
            self.service_cls.bump_author_generation(sid, lid, reason=self.reason)


@dataclass(frozen=True, slots=True)
class ListingScope:
    """列表页缓存作用域维度."""
    site_id: int
    locale_id: int
    index_page_id: int


@dataclass(frozen=True, slots=True)
class DeleteScope:
    """删除操作前捕获的完整失效上下文快照."""
    scopes: tuple[ListingScope, ...]
    archive_scopes: tuple[tuple[int, int], ...]
    author_scopes: tuple[tuple[int, int], ...]


class ListingInvalidationService:
    """列表与侧栏代次管理核心服务."""

    @classmethod
    def get_listing_generation_key(cls, site_id: int, locale_id: int, index_page_id: int) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.listing_generation_key(site_id, locale_id, index_page_id)

    @classmethod
    def get_archive_generation_key(cls, site_id: int, locale_id: int) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.sidebar_archive_generation_key(site_id, locale_id)

    @classmethod
    def get_author_generation_key(cls, site_id: int, locale_id: int) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.sidebar_author_generation_key(site_id, locale_id)

    # ---------------------------------------------------------
    # 代次读取接口 (Fail-Open)
    # ---------------------------------------------------------

    @classmethod
    def get_generation(cls, site_id: int, locale_id: int, index_page_id: int) -> int:
        """读取指定列表页的当前代次，若 Redis 异常或不存在返回 1."""
        key = cls.get_listing_generation_key(site_id, locale_id, index_page_id)
        try:
            val = caches["default"].get(key)
            if val is not None:
                return int(val)
        except Exception as e:
            logger.warning("listing_get_generation_failed", extra={"key": key, "error": str(e)})
        return 1

    @classmethod
    def get_archive_generation(cls, site_id: int, locale_id: int) -> int:
        """读取指定站点与语言的归档侧栏当前代次."""
        key = cls.get_archive_generation_key(site_id, locale_id)
        try:
            val = caches["default"].get(key)
            if val is not None:
                return int(val)
        except Exception as e:
            logger.warning("archive_get_generation_failed", extra={"key": key, "error": str(e)})
        return 1

    @classmethod
    def get_author_generation(cls, site_id: int, locale_id: int) -> int:
        """读取指定站点与语言的随机作者侧栏当前代次."""
        key = cls.get_author_generation_key(site_id, locale_id)
        try:
            val = caches["default"].get(key)
            if val is not None:
                return int(val)
        except Exception as e:
            logger.warning("author_get_generation_failed", extra={"key": key, "error": str(e)})
        return 1

    # ---------------------------------------------------------
    # 原子推进接口 (INCR)
    # ---------------------------------------------------------

    @classmethod
    def bump_generation(cls, scope: ListingScope, reason: str = "") -> int:
        """通过 Redis 原子 INCR 推进列表代次，绝不使用覆盖写."""
        key = cls.get_listing_generation_key(scope.site_id, scope.locale_id, scope.index_page_id)
        cache = caches["default"]
        try:
            cache.add(key, 1, timeout=None)
            new_gen = cache.incr(key)
            logger.info("listing_generation_bumped", extra={
                "scope": f"{scope.site_id}:{scope.locale_id}:{scope.index_page_id}",
                "new_gen": new_gen,
                "reason": reason,
            })
            return new_gen
        except Exception as e:
            logger.warning("listing_bump_generation_failed", extra={"key": key, "error": str(e)})
            return 1

    @classmethod
    def bump_archive_generation(cls, site_id: int, locale_id: int, reason: str = "") -> int:
        """原子推进指定站点/语言的侧栏归档代次."""
        key = cls.get_archive_generation_key(site_id, locale_id)
        cache = caches["default"]
        try:
            cache.add(key, 1, timeout=None)
            new_gen = cache.incr(key)
            logger.info("archive_generation_bumped", extra={
                "site_id": site_id,
                "locale_id": locale_id,
                "new_gen": new_gen,
                "reason": reason,
            })
            return new_gen
        except Exception as e:
            logger.warning("archive_bump_generation_failed", extra={"key": key, "error": str(e)})
            return 1

    @classmethod
    def bump_author_generation(cls, site_id: int, locale_id: int, reason: str = "") -> int:
        """原子推进指定站点/语言的作者侧栏候选代次."""
        key = cls.get_author_generation_key(site_id, locale_id)
        cache = caches["default"]
        try:
            cache.add(key, 1, timeout=None)
            new_gen = cache.incr(key)
            logger.info("author_generation_bumped", extra={
                "site_id": site_id,
                "locale_id": locale_id,
                "new_gen": new_gen,
                "reason": reason,
            })
            return new_gen
        except Exception as e:
            logger.warning("author_bump_generation_failed", extra={"key": key, "error": str(e)})
            return 1

    # ---------------------------------------------------------
    # 作用域解析与生命周期契约
    # ---------------------------------------------------------

    @classmethod
    def capture_page_scopes(cls, page: Page) -> tuple[set[ListingScope], tuple[int, int]]:
        """解析单篇文章所属的父级索引列表作用域与侧边栏作用域."""
        site_id = 0
        try:
            site = page.get_site()
            if site and site.pk:
                site_id = site.pk
        except Exception:
            pass

        locale_id = getattr(page, "locale_id", 0) or 0
        listing_scopes: set[ListingScope] = set()

        try:
            from blog.models import BlogIndexPage
            # 1. 查找直接父级与祖先 BlogIndexPage
            ancestor_indexes = BlogIndexPage.objects.ancestor_of(page).values_list("id", flat=True)
            for idx_id in ancestor_indexes:
                listing_scopes.add(ListingScope(site_id=site_id, locale_id=locale_id, index_page_id=idx_id))

            # 2. 如果直接父级也是 BlogIndexPage 且未命中，补偿加入
            parent = page.get_parent()
            if parent and isinstance(parent.specific_deferred, BlogIndexPage):
                listing_scopes.add(ListingScope(site_id=site_id, locale_id=locale_id, index_page_id=parent.pk))
        except Exception as e:
            logger.warning("capture_page_scopes_failed", extra={"page_id": page.pk, "error": str(e)})

        return listing_scopes, (site_id, locale_id)

    @classmethod
    def capture_delete_scope(cls, page: Page) -> DeleteScope:
        """在页面被从数据库物理删除/下线前，提前捕获其关联的所有 scope 快照."""
        listing_scopes, (site_id, locale_id) = cls.capture_page_scopes(page)
        return DeleteScope(
            scopes=tuple(listing_scopes),
            archive_scopes=((site_id, locale_id),),
            author_scopes=((site_id, locale_id),),
        )

    @classmethod
    def schedule_page_publication(cls, page: Page, event: str) -> None:
        """将页面发布/下线的失效操作注册到当前事务提交后 (transaction.on_commit)."""
        listing_scopes, (site_id, locale_id) = cls.capture_page_scopes(page)
        cls.schedule_scopes_bump(listing_scopes, {(site_id, locale_id)}, {(site_id, locale_id)}, reason=f"{event}:page_{page.pk}")

    @classmethod
    def schedule_delete_invalidation(cls, delete_scope: DeleteScope, reason: str = "page_deleted") -> None:
        """消费预捕获的删除快照，在事务提交后推进全部受影响作用域代次."""
        cls.schedule_scopes_bump(
            set(delete_scope.scopes),
            set(delete_scope.archive_scopes),
            set(delete_scope.author_scopes),
            reason=reason,
        )

    @classmethod
    def schedule_scopes_bump(
        cls,
        listing_scopes: set[ListingScope],
        archive_scopes: set[tuple[int, int]],
        author_scopes: set[tuple[int, int]],
        reason: str = "",
    ) -> None:
        """统一的事务后失效调度器：支持单事务多事件自动去重与原子自增."""
        if not listing_scopes and not archive_scopes and not author_scopes:
            return

        tx_listing = _get_tx_pending_scopes()
        tx_archive = _get_tx_pending_archive_scopes()
        tx_author = _get_tx_pending_author_scopes()

        # 广播推进：若存在通用 (0, 0) 作用域，自动展开为当前所有真实站点与语言
        if (0, 0) in author_scopes:
            from wagtail.models import Site, Locale
            try:
                expanded_scopes = {
                    (s.pk, loc.pk)
                    for s in Site.objects.all()
                    for loc in Locale.objects.all()
                }
                author_scopes = (author_scopes - {(0, 0)}) | (expanded_scopes or {(1, 1)})
            except Exception:
                author_scopes = (author_scopes - {(0, 0)}) | {(1, 1)}

        # 与 Django connection.run_on_commit 队列及当前 savepoint 深度绑定
        # 1. 单事务多事件自动合并去重；
        # 2. 嵌套事务回滚时 Django 自动丢弃内部 savepoint 回调，外部事务不受污染；
        # 3. 外层事务回滚时 Django 清空全部回调，绝不泄漏至后续事务或 Celery 任务。
        from django.db import connection
        current_sids = set(connection.savepoint_ids) if getattr(connection, "in_atomic_block", False) else set()
        existing_handler: _InvalidationCommitHandler | None = None
        if hasattr(connection, "run_on_commit"):
            for item in connection.run_on_commit:
                if len(item) >= 2:
                    sids, func = item[0], item[1]
                    if sids == current_sids and isinstance(func, _InvalidationCommitHandler):
                        existing_handler = func
                        break

        if existing_handler is not None:
            existing_handler.update(listing_scopes, archive_scopes, author_scopes)
        else:
            handler = _InvalidationCommitHandler(cls, reason=reason)
            handler.update(listing_scopes, archive_scopes, author_scopes)
            transaction.on_commit(handler)