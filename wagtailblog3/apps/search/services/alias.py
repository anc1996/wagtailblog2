"""独立内容索引 read alias 的只读检查和原子切换。"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from django.conf import settings
from django.db import connection

from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import (
    ContentSearchElasticsearchError,
    _read_error,
    _response_body,
    get_content_search_client,
)


@dataclass(frozen=True)
class ContentSearchAliasSwitchResult:
    alias: str
    previous_indices: tuple[str, ...]
    new_index: str


class ContentSearchAliasSwitchInProgress(RuntimeError):
    """同一连接已有内容搜索 alias 切换在执行。"""


def _content_search_alias_switch_lock_name(connection_name: str) -> str:
    """生成 MySQL 64 字符限制内的连接级锁名称。"""

    digest = sha256(connection_name.encode("utf-8")).hexdigest()[:32]
    return f"wagtailblog3:content-alias:{digest}"


@contextmanager
def content_search_alias_switch_lock(connection_name: str) -> Iterator[None]:
    """串行化同一连接的 ES alias 切换和 MySQL Target 状态转换。

    MySQL 事务无法覆盖 Elasticsearch 的 aliases API。本锁必须从读取 alias、
    校验门禁开始持续到 Target 状态提交完成，防止两个命令把 ES 与 MySQL 写成相反结果。
    非 MySQL 测试后端不提供跨进程 advisory lock，只保留调用契约以便单元测试。
    """

    if connection.vendor != "mysql":
        yield
        return

    lock_name = _content_search_alias_switch_lock_name(connection_name)
    with connection.cursor() as cursor:
        cursor.execute("SELECT GET_LOCK(%s, 0)", [lock_name])
        row = cursor.fetchone()
    if not row or row[0] != 1:
        raise ContentSearchAliasSwitchInProgress("content_alias_switch_in_progress")
    try:
        yield
    finally:
        # advisory lock 绑定数据库连接；显式释放避免命令结束前阻塞下一次切换。
        with connection.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", [lock_name])


def validate_content_search_alias(alias: str | None = None, index_prefix: str | None = None) -> str:
    """校验 read alias 只属于调用方指定的内容索引命名空间。"""

    if alias is None:
        alias = getattr(settings, "CONTENT_SEARCH_READ_ALIAS", "")
    if index_prefix is None:
        index_prefix = settings.CONTENT_SEARCH_INDEX_PREFIX
    if not alias:
        raise ContentSearchElasticsearchError("content_read_alias_not_configured", retryable=False)
    try:
        validate_content_index_name(alias, index_prefix)
    except ValueError as error:
        raise ContentSearchElasticsearchError("content_read_alias_invalid", retryable=False) from error
    return alias


def get_content_search_read_alias_indices(
    target: Any,
    alias: str | None = None,
    index_prefix: str | None = None,
) -> tuple[str, ...]:
    """读取 alias 的精确物理索引，并拒绝跨命名空间结果。"""

    if index_prefix is None:
        index_prefix = settings.CONTENT_SEARCH_INDEX_PREFIX
    alias = validate_content_search_alias(alias, index_prefix=index_prefix)
    try:
        response = get_content_search_client(target).indices.get_alias(name=alias)
    except Exception as error:
        status = getattr(getattr(error, "meta", None), "status", None)
        if status == 404 or getattr(error, "status_code", None) == 404:
            return ()
        raise _read_error(error) from error
    body = _response_body(response)
    if not isinstance(body, dict):
        raise ContentSearchElasticsearchError("es_invalid_alias_response", retryable=True)
    indices = tuple(sorted(str(index_name) for index_name in body))
    for index_name in indices:
        try:
            validate_content_index_name(index_name, index_prefix)
        except ValueError as error:
            raise ContentSearchElasticsearchError(
                "content_alias_points_outside_prefix",
                retryable=False,
            ) from error
    return indices


def switch_content_search_read_alias(
    target: Any,
    new_index: str,
    alias: str | None = None,
    expected_indices: Iterable[str] | None = None,
    index_prefix: str | None = None,
) -> ContentSearchAliasSwitchResult:
    """用一次 aliases API 将 read alias 切到明确物理索引。"""

    if index_prefix is None:
        index_prefix = settings.CONTENT_SEARCH_INDEX_PREFIX
    alias = validate_content_search_alias(alias, index_prefix=index_prefix)
    try:
        validate_content_index_name(new_index, index_prefix)
    except ValueError as error:
        raise ContentSearchElasticsearchError("content_target_index_invalid", retryable=False) from error
    current_indices = get_content_search_read_alias_indices(
        target,
        alias,
        index_prefix=index_prefix,
    )
    # 预期集合是并发发布保护：若读取后 alias 已变化，拒绝覆盖其他发布者的切换。
    if expected_indices is not None and tuple(sorted(expected_indices)) != current_indices:
        raise ContentSearchElasticsearchError("content_read_alias_changed", retryable=False)
    actions = [
        {"remove": {"index": index_name, "alias": alias}}
        for index_name in current_indices
    ]
    actions.append({"add": {"index": new_index, "alias": alias}})
    try:
        get_content_search_client(target).indices.update_aliases(actions=actions)
    except ContentSearchElasticsearchError:
        raise
    except Exception as error:
        raise _read_error(error) from error
    return ContentSearchAliasSwitchResult(
        alias=alias,
        previous_indices=current_indices,
        new_index=new_index,
    )


def clear_content_search_read_alias(
    target: Any,
    alias: str | None = None,
    expected_indices: Iterable[str] | None = None,
    index_prefix: str | None = None,
) -> tuple[str, ...]:
    """原子移除内容 alias，使前台可回退到旧搜索实现。"""

    if index_prefix is None:
        index_prefix = settings.CONTENT_SEARCH_INDEX_PREFIX
    alias = validate_content_search_alias(alias, index_prefix=index_prefix)
    current_indices = get_content_search_read_alias_indices(
        target,
        alias,
        index_prefix=index_prefix,
    )
    if expected_indices is not None and tuple(sorted(expected_indices)) != current_indices:
        raise ContentSearchElasticsearchError("content_read_alias_changed", retryable=False)
    if not current_indices:
        return current_indices
    actions = [
        {"remove": {"index": index_name, "alias": alias}}
        for index_name in current_indices
    ]
    try:
        get_content_search_client(target).indices.update_aliases(actions=actions)
    except ContentSearchElasticsearchError:
        raise
    except Exception as error:
        raise _read_error(error) from error
    return current_indices
