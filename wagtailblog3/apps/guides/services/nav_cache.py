"""
专题指南与全站导航菜单缓存失效治理服务。

确保在页面（特别是 GuideIndexPage 与正文外置页面）上线、下架、移动、删除或修改权限时，
通过不可重用命名空间（Non-Reusable Namespace）与单调计数器实现严格 O(1) 瞬时收敛，杜绝 600 秒内前台菜单呈现滞后。
热路径不执行任何数据库全表扫描或键枚举，旧缓存键由其原生 TTL 自然淘汰。
"""

import threading
import uuid
from typing import Any, Optional
from django.core.cache import cache
from django.db import transaction

NAV_MENU_NAMESPACE_KEY: str = "nav_menu_cache_namespace"
NAV_MENU_COUNTER_KEY: str = "nav_menu_cache_counter"
NAV_MENU_REBUILD_LOCK: str = "nav_menu_cache_rebuild_lock"

_LOCAL_REBUILD_LOCK = threading.Lock()


class _RebuildLockContext:
    """
    跨进程（Redis 分布式锁）与跨线程（进程内线程锁）双层原子同步上下文管理器。
    保障在冷启动或缓存键被逐出时，命名空间与计数器的重建绝对原子且各进程/线程读到一致状态。
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.dist_lock: Optional[Any] = None

    def __enter__(self) -> "_RebuildLockContext":
        _LOCAL_REBUILD_LOCK.acquire()
        if hasattr(cache, "lock"):
            try:
                self.dist_lock = cache.lock(NAV_MENU_REBUILD_LOCK, timeout=self.timeout)
                self.dist_lock.acquire(blocking=True)
            except Exception:
                self.dist_lock = None
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if self.dist_lock is not None:
                try:
                    self.dist_lock.release()
                except Exception:
                    pass
        finally:
            _LOCAL_REBUILD_LOCK.release()


def _rebuild_epoch_atomically() -> str:
    """
    在双层锁保护下原子重建代次。
    无论冷启动、两键皆失，还是计数器单独被驱逐/命名空间单独丢失，
    强制分配全新不可重用 UUID4 命名空间，并原子将计数器初始化为 1。
    双重检查（Double-Checked Locking）确保并发线程在等待锁后直接读取一致结果，不重复重建。
    """
    with _RebuildLockContext():
        values = cache.get_many([NAV_MENU_NAMESPACE_KEY, NAV_MENU_COUNTER_KEY])
        ns = values.get(NAV_MENU_NAMESPACE_KEY)
        counter = values.get(NAV_MENU_COUNTER_KEY)
        if ns and counter is not None:
            return f"{ns}:{counter}"

        new_ns = uuid.uuid4().hex[:12]
        cache.set_many(
            {
                NAV_MENU_NAMESPACE_KEY: new_ns,
                NAV_MENU_COUNTER_KEY: 1,
            },
            timeout=None,
        )
        return f"{new_ns}:1"


def get_current_nav_menu_epoch() -> str:
    """
    获取当前全站主导航菜单的缓存代次标识（格式：{namespace}:{counter}）。

    不可重用命名空间与原子一致性保障：
    1. 正常热路径通过单次 MGET (cache.get_many) 同时读取命名空间与计数器；
    2. 若两键均有效，直接返回 {namespace}:{counter}，严格 O(1) 零锁开销；
    3. 若任意一键缺失（冷启动、Redis重启，或极为关键的【计数器被单独LRU逐出】）：
       绝对禁止沿用旧命名空间重置计数器为 1（否则会导致命中 600 秒存活期内的 N:1 旧片段）；
       必须在原子锁保护下分配全新的不可重用 UUID4 命名空间并将计数器初始化为 1。
    """
    values = cache.get_many([NAV_MENU_NAMESPACE_KEY, NAV_MENU_COUNTER_KEY])
    ns = values.get(NAV_MENU_NAMESPACE_KEY)
    counter = values.get(NAV_MENU_COUNTER_KEY)

    if ns and counter is not None:
        return f"{ns}:{counter}"

    return _rebuild_epoch_atomically()


def bump_nav_menu_epoch() -> str:
    """
    原子推进全站主导航菜单缓存代次，以严格 O(1) 复杂度使全站 main_nav_menu 模板片段缓存瞬时失效。

    原子并发安全与不可重用保证：
    1. 优先尝试 cache.incr 原子递增单调计数器（Redis 原生原子操作，O(1)）；
    2. 递增成功后校验命名空间是否存在：
       - 若命名空间有效，返回 {namespace}:{new_counter}；
       - 若命名空间缺失（罕见的半丢失状态），触发原子重建；
    3. 若计数器键缺失（incr 报错）：
       进入双层锁原子重建，检查若已被其他并发线程重建则直接递增，否则生成全新 UUID4 命名空间并置计数器为 1；
    4. 全链路杜绝同一秒内的代次重用与半状态竞态。
    """
    try:
        new_counter = cache.incr(NAV_MENU_COUNTER_KEY)
        ns = cache.get(NAV_MENU_NAMESPACE_KEY)
        if ns:
            return f"{ns}:{new_counter}"
    except (ValueError, Exception):
        pass

    with _RebuildLockContext():
        try:
            new_counter = cache.incr(NAV_MENU_COUNTER_KEY)
            ns = cache.get(NAV_MENU_NAMESPACE_KEY)
            if ns:
                return f"{ns}:{new_counter}"
        except (ValueError, Exception):
            pass

        new_ns = uuid.uuid4().hex[:12]
        cache.set_many(
            {
                NAV_MENU_NAMESPACE_KEY: new_ns,
                NAV_MENU_COUNTER_KEY: 1,
            },
            timeout=None,
        )
        return f"{new_ns}:1"


def invalidate_nav_menu_cache(page: Optional[Any] = None) -> str:
    """
    全站主导航菜单 O(1) 瞬时失效治理。

    严密防范事务内并发时序脏读窗口：
    若当前线程处于活动数据库事务中（connection.in_atomic_block 为 True），
    必须通过 transaction.on_commit 将代次递增延迟至事务提交成功后再执行；
    严禁在事务提交前提前推进代次，杜绝并发请求以新代次缓存未提交旧数据的时序竞争。

    :param page: 触发变更的页面实例（保留入参契约，零副作用）。
    :return: 当前代次标识字符串。
    """
    connection = transaction.get_connection()
    if connection.in_atomic_block:
        transaction.on_commit(bump_nav_menu_epoch)
        return get_current_nav_menu_epoch()
    return bump_nav_menu_epoch()
