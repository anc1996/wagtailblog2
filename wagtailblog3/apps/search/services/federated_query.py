"""全站搜索的 Blog/Pages 联邦结果适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Sequence

from django.conf import settings
from django.core import signing

from .content_query import ContentSearchQueryUnavailable, build_content_search_results
from .cursor import build_cursor_query_hash
from .pages_query import build_public_pages_queryset


RRF_K = 60
FEDERATED_CANDIDATE_LIMIT = 100
FEDERATED_CURSOR_SALT = "search.federated.cursor.v1"
FEDERATED_CURSOR_VERSION = 1


class FederatedSearchUnavailable(ContentSearchQueryUnavailable):
    """联邦搜索任一必需来源不可用时使用的稳定错误。"""


def _public_pages_queryset(
    start_date: Any = None,
    end_date: Any = None,
    order_by: str | None = None,
) -> Any:
    """构造唯一的 Pages 流，`pages` 和 `all` 必须共用相同公开边界。"""
    return build_public_pages_queryset(start_date, end_date, order_by)


def _build_pages_search_results(
    query_string: str,
    start_date: Any = None,
    end_date: Any = None,
    order_by: str | None = None,
) -> Any:
    """通过现有 Wagtail 查询编译器执行 Pages 流，避免复制搜索 DSL。"""
    from search.core import _build_search_results_for_queryset

    return _build_search_results_for_queryset(
        _public_pages_queryset(start_date, end_date, order_by),
        query_string,
        search_type="pages",
        start_date=start_date,
        end_date=end_date,
        order_by=order_by,
    )


def _ranked_candidates(results: Any, limit: int) -> list[tuple[Any, int]]:
    items = list(results[:limit])
    return [(page, position + 1) for position, page in enumerate(items)]


def _date_key(page: Any, reverse: bool = False) -> tuple[int, Any, int]:
    value = getattr(page, "date", None)
    if value is None:
        value = getattr(page, "first_published_at", None)
    if value is None:
        return (1, 0, page.pk)
    ordinal = value.timestamp() if hasattr(value, "timestamp") else value.toordinal()
    return (0, ordinal, page.pk)


@dataclass
class FederatedSearchResults:
    """提供 Django 分页器和签名复合游标需要的只读结果协议。"""

    blog_results: object
    pages_results: object
    order_by: str | None = None

    def count(self) -> int:
        # 两个来源必须按公开页面主键去重后计数，避免同一页面同时落入两条流时总数虚高。
        blog_ids = {page.pk for page in self.blog_results[:FEDERATED_CANDIDATE_LIMIT]}
        page_ids = {page.pk for page in self.pages_results[:FEDERATED_CANDIDATE_LIMIT]}
        if self.blog_results.count() > FEDERATED_CANDIDATE_LIMIT or self.pages_results.count() > FEDERATED_CANDIDATE_LIMIT:
            return self.blog_results.count() + self.pages_results.count() - len(blog_ids & page_ids)
        return len(blog_ids | page_ids)

    def __len__(self) -> int:
        return min(self.count(), 10000)

    def __bool__(self) -> bool:
        return self.count() > 0

    def __getitem__(self, key: int | slice) -> Any:
        if not isinstance(key, slice):
            items = self._fetch(key, 1)
            if not items:
                raise IndexError(key)
            return items[0]
        start = key.start or 0
        stop = key.stop if key.stop is not None else start + 20
        return self._fetch(start, max(stop - start, 0))

    def _sort_key(self, page: Any, rank: int) -> tuple[Any, ...]:
        if self.order_by == "date":
            return _date_key(page)
        if self.order_by == "-date":
            date_key = _date_key(page)
            return (date_key[0], -date_key[1], page.pk)
        return (-(1 / (RRF_K + rank)), page.pk)

    def _merge_items(
        self,
        blog_items: Sequence[Any],
        pages_items: Sequence[Any],
        size: int,
        start: int = 0,
    ) -> list[Any]:
        merged = []
        seen = set()
        for rank, page in enumerate(blog_items, 1):
            if page.pk not in seen:
                seen.add(page.pk)
                merged.append((self._sort_key(page, rank), page))
        for rank, page in enumerate(pages_items, 1):
            if page.pk not in seen:
                seen.add(page.pk)
                merged.append((self._sort_key(page, rank), page))
        merged.sort(key=lambda item: item[0])
        return [page for _, page in merged[start:start + size]]

    def _fetch(self, start: int, size: int) -> list[Any]:
        if size <= 0:
            return []
        limit = min(FEDERATED_CANDIDATE_LIMIT, start + size)
        return self._merge_items(
            [item[0] for item in _ranked_candidates(self.blog_results, limit)],
            [item[0] for item in _ranked_candidates(self.pages_results, limit)],
            size,
            start,
        )

    def cursor_page(
        self,
        token: str | None,
        page_size: int,
        search_type: str = "all",
        locale: str = "",
    ) -> "FederatedCursorPage":
        """签名绑定查询条件，并分别保存 Blog 游标和 Pages 来源偏移。"""
        page_size = max(1, min(int(page_size), FEDERATED_CANDIDATE_LIMIT))
        query_string = getattr(self.blog_results, "query_string", "")
        query_hash = build_cursor_query_hash(
            query_string,
            search_type,
            getattr(self.blog_results, "start_date", ""),
            getattr(self.blog_results, "end_date", ""),
            self.order_by,
            locale,
        )
        state = _decode_federated_cursor(token, query_hash)
        blog_cursor = state.get("blog_cursor") if state else None
        pages_offset = state.get("pages_offset", 0) if state else 0
        blog_page = self.blog_results.cursor_page(
            blog_cursor,
            page_size,
            search_type="blog",
            locale=locale,
        )
        pages_items = list(self.pages_results[pages_offset:pages_offset + page_size])
        items = self._merge_items(blog_page.object_list, pages_items, page_size)
        next_cursor = None
        if blog_page.next_cursor or len(pages_items) == page_size:
            next_cursor = _encode_federated_cursor(
                {
                    "blog_cursor": blog_page.next_cursor,
                    "pages_offset": pages_offset + page_size,
                },
                query_hash,
            )
        return FederatedCursorPage(items, self.count(), next_cursor, bool(state))


class FederatedCursorPage:
    cursor_mode = True

    def __init__(
        self,
        object_list: Sequence[Any],
        total: int,
        next_cursor: str | None = None,
        has_previous: bool = False,
    ) -> None:
        self.object_list = object_list
        self.paginator = type("Paginator", (), {"count": total})()
        self.previous_cursor = None
        self.next_cursor = next_cursor
        self._has_previous = has_previous

    def __iter__(self) -> Iterator[Any]:
        return iter(self.object_list)

    def __len__(self) -> int:
        return len(self.object_list)

    def has_previous(self) -> bool:
        return self._has_previous

    def has_next(self) -> bool:
        return bool(self.next_cursor)


def _encode_federated_cursor(state: dict[str, Any], query_hash: str) -> str:
    payload = {"v": FEDERATED_CURSOR_VERSION, "q": query_hash, **state}
    return signing.dumps(payload, salt=FEDERATED_CURSOR_SALT, compress=True)


def _decode_federated_cursor(
    token: str | None,
    query_hash: str,
) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        payload = signing.loads(
            token,
            salt=FEDERATED_CURSOR_SALT,
            max_age=max(int(settings.CONTENT_SEARCH_CURSOR_MAX_AGE_SECONDS), 1),
        )
    except (signing.BadSignature, signing.SignatureExpired) as error:
        raise ContentSearchQueryUnavailable("federated_cursor_invalid") from error
    if not isinstance(payload, dict) or payload.get("v") != FEDERATED_CURSOR_VERSION:
        raise ContentSearchQueryUnavailable("federated_cursor_invalid")
    if payload.get("q") != query_hash:
        raise ContentSearchQueryUnavailable("federated_cursor_query_mismatch")
    if payload.get("blog_cursor") is not None and not isinstance(payload["blog_cursor"], str):
        raise ContentSearchQueryUnavailable("federated_cursor_invalid")
    try:
        pages_offset = int(payload.get("pages_offset", 0))
    except (TypeError, ValueError) as error:
        raise ContentSearchQueryUnavailable("federated_cursor_invalid") from error
    if pages_offset < 0:
        raise ContentSearchQueryUnavailable("federated_cursor_invalid")
    return {"blog_cursor": payload.get("blog_cursor"), "pages_offset": pages_offset}


def build_federated_search_results(
    query_string: str,
    start_date: Any = None,
    end_date: Any = None,
    order_by: str | None = None,
) -> FederatedSearchResults:
    """构造联邦结果；任一必需来源失败都向上抛出，不返回部分结果。"""
    try:
        blog_results = build_content_search_results(
            query_string, start_date=start_date, end_date=end_date, order_by=order_by
        )
        pages_results = _build_pages_search_results(
            query_string, start_date=start_date, end_date=end_date, order_by=order_by
        )
        return FederatedSearchResults(blog_results, pages_results, order_by)
    except ContentSearchQueryUnavailable:
        raise
    except Exception as error:
        raise FederatedSearchUnavailable("federated_source_unavailable") from error
