"""基于安全游标的日志页码会话，不使用 OFFSET 或全量计数。"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.core import signing
from django.core.cache import cache

from .elasticsearch_logs import LogSearchUnavailable, is_enabled, search_logs
from .reader import ReadResult, read_logs


PAGE_SESSION_SALT = "observability.log-pages.v1"
PAGE_SESSION_TTL = 30 * 60
PAGE_STATE_VERSION = 1
MAX_JUMP_STEPS = 100
MAX_JUMP_BYTES = 16 * 1024 * 1024
MAX_JUMP_SECONDS = 2.0
MAX_RECENT_CURSORS = 80
CHECKPOINT_INTERVAL = 100


@dataclass(slots=True)
class PageResult:
    records: list
    page: int
    requested_page: int
    next_page: int | None
    previous_page: int | None
    total_pages: int | None
    indexed_through: int
    bytes_read: int
    session_token: str
    jump_pending: bool = False

    @property
    def has_next(self) -> bool:
        """是否存在可请求的下一页。"""
        return self.next_page is not None

    @property
    def has_previous(self) -> bool:
        """是否存在可返回的上一页。"""
        return self.previous_page is not None


def _filter_payload(filters: dict[str, Any]) -> dict[str, Any]:
    """将筛选条件转换为稳定、可签名的 JSON 基本类型。

    参数：``filters`` 为页面筛选字典。
    返回：datetime 已 ISO 化的字典。
    异常：无。
    """
    payload = {}
    for key, value in filters.items():
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
        else:
            payload[key] = value
    return payload


def _filter_hash(filters: dict[str, Any]) -> str:
    """计算筛选条件哈希，用于绑定分页会话。

    参数：``filters`` 为当前查询筛选。
    返回：SHA-256 十六进制摘要。
    异常：不可 JSON 序列化的筛选值会抛出 ``TypeError``。
    """
    raw = json.dumps(
        _filter_payload(filters), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _cache_key(session_id: str) -> str:
    """返回分页状态在缓存中的命名空间键。"""
    return f"observability:log-pages:{session_id}"


def _new_session(owner_id: int, filter_hash: str) -> tuple[str, dict]:
    """创建与用户和筛选条件绑定的分页会话。

    参数：``owner_id`` 为用户主键，``filter_hash`` 为规范化筛选哈希。
    返回：签名 token 与初始缓存状态。
    异常：缓存后端错误向上抛出。

    token 不携带游标明细，避免浏览器端伪造深分页位置。
    """
    session_id = signing.dumps(
        {"owner": owner_id, "filter": filter_hash, "nonce": secrets.token_urlsafe(12)},
        salt=PAGE_SESSION_SALT,
        compress=True,
    )
    state = {
        "version": PAGE_STATE_VERSION,
        "owner": owner_id,
        "filter": filter_hash,
        "cursors": {"1": ""},
        "recent": [1],
        "indexed_through": 1,
        "total_pages": None,
    }
    cache.set(_cache_key(hashlib.sha256(session_id.encode()).hexdigest()), state, PAGE_SESSION_TTL)
    return session_id, state


def _load_session(token: str, owner_id: int, filter_hash: str) -> tuple[str, dict]:
    """验证并读取分页会话。

    参数：``token`` 为浏览器提交的签名会话，其他参数为当前请求身份与筛选。
    返回：有效 token 与会话状态；无效时创建新会话。
    异常：无；无效签名被当作不可复用的会话，而非向用户暴露错误。
    """
    if token:
        try:
            signed = signing.loads(token, salt=PAGE_SESSION_SALT, max_age=PAGE_SESSION_TTL)
        except signing.BadSignature:
            signed = None
        if (
            isinstance(signed, dict)
            and signed.get("owner") == owner_id
            and signed.get("filter") == filter_hash
        ):
            key = _cache_key(hashlib.sha256(token.encode()).hexdigest())
            state = cache.get(key)
            if (
                isinstance(state, dict)
                and state.get("version") == PAGE_STATE_VERSION
                and state.get("owner") == owner_id
                and state.get("filter") == filter_hash
            ):
                return token, state
    # 签名、用户和筛选指纹任一不匹配都不能复用旧游标，避免跨用户或跨查询读取错误页。
    return _new_session(owner_id, filter_hash)


def _save_session(token: str, state: dict) -> None:
    """将更新后的分页状态写回缓存并延长会话有效期。"""
    key = _cache_key(hashlib.sha256(token.encode()).hexdigest())
    cache.set(key, state, PAGE_SESSION_TTL)


def _remember_cursor(state: dict, page: int, cursor: str) -> None:
    """记录一个页面游标并限制最近游标数量。

    参数：``state`` 为会话状态，``page`` 为页码，``cursor`` 为后端游标。
    返回：无。
    异常：无。
    """
    cursors = state.setdefault("cursors", {})
    recent = state.setdefault("recent", [])
    cursors[str(page)] = cursor
    if page in recent:
        recent.remove(page)
    recent.append(page)
    while len(recent) > MAX_RECENT_CURSORS:
        candidate = recent.pop(0)
        if candidate != 1 and candidate % CHECKPOINT_INTERVAL:
            cursors.pop(str(candidate), None)
    state["indexed_through"] = max(int(state.get("indexed_through", 1)), page)


def _forget_from(state: dict, page: int) -> None:
    """丢弃指定页及之后的游标检查点。

    参数：``state`` 为会话状态，``page`` 为失效起点。
    返回：无。
    异常：无。

    文件截断或 ES 快照失效后，旧游标不能继续使用，避免出现重复或跳过记录。
    """
    cursors = state.setdefault("cursors", {})
    for value in list(cursors):
        if int(value) >= page:
            cursors.pop(value, None)
    state["recent"] = [value for value in state.get("recent", []) if value < page]
    state["indexed_through"] = max([int(value) for value in cursors] or [1])
    state["total_pages"] = None


def _nearest_cursor(state: dict, page: int) -> tuple[int, str]:
    """找到不晚于目标页的最近已缓存游标。"""
    candidates = [int(value) for value in state.get("cursors", {}) if int(value) <= page]
    nearest = max(candidates, default=1)
    return nearest, state.get("cursors", {}).get(str(nearest), "")


def _read(filters: dict[str, Any], page_size: int, cursor: str) -> ReadResult:
    """通过受限文件读取器读取一页日志。

    参数：筛选条件、页大小和已签名的文件游标。
    返回：``ReadResult``。
    异常：无效游标、路径安全或文件读取错误由读取器向上抛出。
    """
    return read_logs(**filters, page_size=page_size, cursor=cursor)


def _read_at_page(
    state: dict, filters: dict[str, Any], page_size: int, page: int, cursor: str
) -> tuple[ReadResult, str]:
    """读取指定检查点并在文件快照失效时回到第一页。

    参数：会话状态、筛选、页大小、当前页与游标。
    返回：读取结果及实际使用的游标。
    异常：底层读取错误向上抛出。
    """
    result = _read(filters, page_size, cursor)
    if cursor and not result.snapshot_valid:
        # 文件轮转或 copytruncate 使旧快照失效。丢弃此页之后的检查点，
        # 当前请求从新的第一页继续，避免展示空白或错误页码。
        _forget_from(state, page)
        page = 1
        cursor = ""
        result = _read(filters, page_size, cursor)
    return result, cursor


def _read_elasticsearch_page(
    *,
    owner_id: int,
    requested_page: int,
    page_size: int,
    session_token: str,
    filters: dict[str, Any],
) -> PageResult:
    """Read pages from ES using the same signed session contract as files."""
    requested_page = max(1, requested_page)
    fingerprint = _filter_hash(
        {**filters, "page_size": page_size, "backend": "elasticsearch"}
    )
    session_token, state = _load_session(session_token, owner_id, fingerprint)
    total_pages = state.get("total_pages")
    if total_pages is not None:
        requested_page = min(requested_page, max(1, int(total_pages)))

    current_page, cursor = _nearest_cursor(state, requested_page)
    steps = 0
    jump_started = time.monotonic()
    while current_page < requested_page:
        if steps >= MAX_JUMP_STEPS or (steps and time.monotonic() - jump_started >= MAX_JUMP_SECONDS):
            break
        result = search_logs(**filters, page_size=page_size, cursor=cursor)
        steps += 1
        if not result.has_more:
            state["total_pages"] = current_page if result.records else max(1, current_page - 1)
            requested_page = min(requested_page, state["total_pages"])
            current_page, cursor = _nearest_cursor(state, requested_page)
            break
        current_page += 1
        cursor = result.next_cursor
        _remember_cursor(state, current_page, cursor)

    jump_pending = current_page < requested_page
    display_page = current_page if jump_pending else requested_page
    if jump_pending:
        display_page, cursor = _nearest_cursor(state, display_page)

    result = search_logs(**filters, page_size=page_size, cursor=cursor)
    if result.has_more:
        _remember_cursor(state, display_page + 1, result.next_cursor)
        next_page = display_page + 1
    else:
        state["total_pages"] = display_page if result.records else max(1, display_page - 1)
        next_page = None

    total_pages = state.get("total_pages")
    previous_page = display_page - 1 if display_page > 1 else None
    if total_pages is not None and display_page >= total_pages:
        next_page = None
    _save_session(session_token, state)
    return PageResult(
        records=result.records,
        page=display_page,
        requested_page=requested_page,
        next_page=next_page,
        previous_page=previous_page,
        total_pages=total_pages,
        indexed_through=int(state.get("indexed_through", display_page)),
        bytes_read=0,
        session_token=session_token,
        jump_pending=jump_pending,
    )


def read_log_page(
    *,
    owner_id: int,
    requested_page: int,
    page_size: int,
    session_token: str = "",
    filters: dict[str, Any],
) -> PageResult:
    """读取一个逻辑页；深跳页按请求预算渐进建立游标检查点。"""
    if is_enabled():
        try:
            return _read_elasticsearch_page(
                owner_id=owner_id,
                requested_page=requested_page,
                page_size=page_size,
                session_token=session_token,
                filters=filters,
            )
        except (LogSearchUnavailable, ValueError):
            # ES is an optional read model. A failed query must not take the
            # admin page down; the bounded file reader remains available.
            pass

    requested_page = max(1, requested_page)
    fingerprint = _filter_hash({**filters, "page_size": page_size})
    session_token, state = _load_session(session_token, owner_id, fingerprint)
    total_pages = state.get("total_pages")
    if total_pages is not None:
        requested_page = min(requested_page, max(1, int(total_pages)))

    # 从距离目标最近的检查点开始逐页向前推进；不做 OFFSET/总数查询，成本与跳跃距离相关。
    current_page, cursor = _nearest_cursor(state, requested_page)
    bytes_read = 0
    steps = 0
    jump_started = time.monotonic()
    last_result: ReadResult | None = None

    while current_page < requested_page:
        if (
            steps >= MAX_JUMP_STEPS
            or bytes_read >= MAX_JUMP_BYTES
            or (steps and time.monotonic() - jump_started >= MAX_JUMP_SECONDS)
        ):
            break
        last_result, cursor = _read_at_page(
            state, filters, page_size, current_page, cursor
        )
        if current_page > 1 and not cursor:
            current_page = 1
            requested_page = 1
            break
        steps += 1
        bytes_read += last_result.bytes_read
        if not last_result.has_more:
            state["total_pages"] = current_page if last_result.records else max(1, current_page - 1)
            requested_page = min(requested_page, state["total_pages"])
            current_page, cursor = _nearest_cursor(state, requested_page)
            break
        current_page += 1
        cursor = last_result.next_cursor
        _remember_cursor(state, current_page, cursor)

    # 达到步数、字节或时间预算时先返回已建立的最深页，下一次请求继续推进同一会话。
    jump_pending = current_page < requested_page
    display_page = current_page if jump_pending else requested_page
    if jump_pending:
        # 本次只渲染已经安全建立到的最深页，用户可继续同一目标跳转。
        display_page, cursor = _nearest_cursor(state, display_page)

    result, cursor = _read_at_page(state, filters, page_size, display_page, cursor)
    if display_page > 1 and not cursor:
        display_page = requested_page = 1
    bytes_read += result.bytes_read
    if result.has_more:
        _remember_cursor(state, display_page + 1, result.next_cursor)
        next_page = display_page + 1
    else:
        state["total_pages"] = display_page if result.records else max(1, display_page - 1)
        next_page = None

    total_pages = state.get("total_pages")
    previous_page = display_page - 1 if display_page > 1 else None
    if total_pages is not None and display_page >= total_pages:
        next_page = None
    _save_session(session_token, state)

    return PageResult(
        records=result.records,
        page=display_page,
        requested_page=requested_page,
        next_page=next_page,
        previous_page=previous_page,
        total_pages=total_pages,
        indexed_through=int(state.get("indexed_through", display_page)),
        bytes_read=bytes_read,
        session_token=session_token,
        jump_pending=jump_pending,
    )
