"""独立内容索引的公开 BlogPage 查询适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from django.conf import settings
from django.db.models import Case, When

from blog.models import BlogPage
from search.models import ContentSearchTarget, ContentSearchTargetRole
from search.services.content_index import validate_content_index_name
from search.services.cursor import (
    ContentSearchCursor,
    build_cursor_query_hash,
    decode_content_search_cursor,
    encode_content_search_cursor,
)
from search.services.elasticsearch import (
    ContentSearchElasticsearchError,
    _read_error,
    _response_body,
    get_content_search_client,
)
from search.services.highlights import (
    HIGHLIGHT_END_TAG,
    HIGHLIGHT_START_TAG,
    build_highlight_fields,
    extract_safe_highlights,
)


CONTENT_SEARCH_QUERY_FIELDS = ("title^10", "intro^5", "body_text^2")
CONTENT_SEARCH_HIGHLIGHT_FIELDS = (
    ("title", "title"),
    ("intro", "intro"),
    ("body_text", "body_text"),
)
CONTENT_SEARCH_QUERY_MAX_PAGE_SIZE = 100


class ContentSearchQueryUnavailable(RuntimeError):
    """独立查询未准备好或查询失败时使用的稳定错误。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ContentSearchHitHighlight:
    page_id: int
    matched_field: str
    fragments: tuple
    title_fragment: object = ""


@dataclass(frozen=True)
class ContentSearchQueryPage:
    """只携带公开页面 ID，正文仍由 MySQL/Wagtail 读取。"""

    page_ids: tuple[int, ...]
    total: int
    took_ms: int | None
    sort_values: tuple[tuple, ...] = ()
    pit_id: str | None = None
    highlights: tuple[ContentSearchHitHighlight, ...] = ()


class ContentSearchCursorPage:
    """向模板提供与 Django Page 相近的最小只读接口。"""

    cursor_mode = True

    def __init__(self, object_list, total, previous_cursor=None, next_cursor=None):
        self.object_list = object_list
        self.paginator = SimpleNamespace(count=total)
        self.previous_cursor = previous_cursor
        self.next_cursor = next_cursor

    def __iter__(self):
        return iter(self.object_list)

    def __len__(self):
        return len(self.object_list)

    def has_previous(self):
        return bool(self.previous_cursor)

    def has_next(self):
        return bool(self.next_cursor)

    def has_other_pages(self):
        return self.has_previous() or self.has_next()


def content_search_read_alias():
    alias = getattr(settings, "CONTENT_SEARCH_READ_ALIAS", "")
    if not alias:
        raise ContentSearchQueryUnavailable("content_read_alias_not_configured")
    try:
        validate_content_index_name(alias, settings.CONTENT_SEARCH_INDEX_PREFIX)
    except ValueError as error:
        raise ContentSearchQueryUnavailable("content_read_alias_invalid") from error
    return alias


def get_content_search_serving_target():
    """读取连接元数据，实际索引名始终由稳定 read alias 决定。"""

    try:
        target = (
            ContentSearchTarget.objects.filter(
                connection_name=settings.CONTENT_SEARCH_CONNECTION_NAME,
                enabled=True,
                role=ContentSearchTargetRole.SERVING,
            )
            .order_by("-updated_at", "-pk")
            .first()
        )
    except Exception as error:
        raise ContentSearchQueryUnavailable("content_search_target_unavailable") from error
    if target is None:
        raise ContentSearchQueryUnavailable("content_serving_target_missing")
    return target


def _minimum_should_match(query_string):
    tokens = [token for token in query_string.split() if token]
    if len(tokens) <= 2:
        return "100%"
    if len(tokens) <= 6:
        return "75%"
    return "60%"


def build_content_search_query(query_string, start_date=None, end_date=None, date_field="date"):
    must = [
        {
            "multi_match": {
                "query": query_string,
                "fields": list(CONTENT_SEARCH_QUERY_FIELDS),
                "type": "best_fields",
                "operator": "or",
                "minimum_should_match": _minimum_should_match(query_string),
            }
        }
    ]
    filters = [{"term": {"searchable": True}}]
    if start_date or end_date:
        date_range = {}
        if start_date:
            date_range["gte"] = start_date.isoformat()
        if end_date:
            date_range["lte"] = end_date.isoformat()
        if date_field not in {"date", "first_published_at"}:
            raise ValueError("不允许的内容日期字段")
        filters.append({"range": {date_field: date_range}})
    return {"bool": {"must": must, "filter": filters}}


def build_content_search_sort(order_by=None, date_field="date"):
    if date_field not in {"date", "first_published_at"}:
        raise ValueError("不允许的内容日期字段")
    if order_by == "date":
        return [{date_field: {"order": "asc", "missing": "_last"}}, {"page_id": "asc"}]
    if order_by == "-date":
        return [{date_field: {"order": "desc", "missing": "_last"}}, {"page_id": "asc"}]
    return [{"_score": "desc"}, {"page_id": "asc"}]


def reverse_content_search_sort(sort):
    """上一页查询反转全部排序方向，结果返回后再恢复展示顺序。"""

    reversed_sort = []
    for item in sort:
        field_name, definition = next(iter(item.items()))
        if isinstance(definition, str):
            reversed_sort.append({field_name: "asc" if definition == "desc" else "desc"})
            continue
        reversed_definition = dict(definition)
        reversed_definition["order"] = "asc" if definition.get("order") == "desc" else "desc"
        reversed_sort.append({field_name: reversed_definition})
    return reversed_sort


def _total_hits(hits):
    total = hits.get("total", 0)
    if isinstance(total, dict):
        total = total.get("value", 0)
    try:
        return max(int(total), 0)
    except (TypeError, ValueError):
        raise ContentSearchElasticsearchError("es_invalid_total_hits", retryable=True)


def _public_page_ids_in_order(page_ids):
    if not page_ids:
        return ()
    public_ids = set(
        BlogPage.objects.live().public().filter(pk__in=page_ids).values_list("pk", flat=True)
    )
    return tuple(page_id for page_id in page_ids if page_id in public_ids)


def _public_hits_in_order(page_ids, sort_values):
    public_page_ids = _public_page_ids_in_order(page_ids)
    public_ids = set(public_page_ids)
    filtered = [
        (page_id, sort_value)
        for page_id, sort_value in zip(page_ids, sort_values)
        if page_id in public_ids
    ]
    public_page_ids = tuple(item[0] for item in filtered)
    if any(item[1] is None for item in filtered):
        return public_page_ids, ()
    return public_page_ids, tuple(item[1] for item in filtered)


def _public_highlights_in_order(public_page_ids, highlights_by_page_id):
    return tuple(
        highlights_by_page_id[page_id]
        for page_id in public_page_ids
        if page_id in highlights_by_page_id
    )


def query_content_search_page(
    target,
    query_string,
    start=0,
    size=20,
    start_date=None,
    end_date=None,
    order_by=None,
    index_name=None,
    request_timeout=None,
    search_after=None,
    reverse=False,
    pit_id=None,
    date_field="date",
):
    """查询精简索引后只返回经过 live/public guard 的页面 ID。"""

    try:
        start = max(int(start), 0)
        size = min(max(int(size), 0), CONTENT_SEARCH_QUERY_MAX_PAGE_SIZE)
    except (TypeError, ValueError, OverflowError) as error:
        raise ContentSearchQueryUnavailable("content_search_paging_invalid") from error
    sort = build_content_search_sort(order_by, date_field)
    body = {
        "query": build_content_search_query(query_string, start_date, end_date, date_field),
        "sort": reverse_content_search_sort(sort) if reverse else sort,
        "size": size,
        "track_total_hits": True,
        "source_includes": ("page_id",),
    }
    if getattr(settings, "SEARCH_HIGHLIGHTS_ENABLED", True) and size:
        body["highlight"] = {
            "pre_tags": [HIGHLIGHT_START_TAG],
            "post_tags": [HIGHLIGHT_END_TAG],
            "order": "score",
            "fields": build_highlight_fields(CONTENT_SEARCH_HIGHLIGHT_FIELDS),
        }
    if search_after is not None:
        body["search_after"] = list(search_after)
    else:
        body["from_"] = start
    if request_timeout is not None:
        body["request_timeout"] = max(float(request_timeout), 0.01)
    index = index_name or content_search_read_alias()
    if pit_id:
        body["pit"] = {
            "id": pit_id,
            "keep_alive": settings.CONTENT_SEARCH_PIT_KEEP_ALIVE,
        }
    try:
        client = get_content_search_client(target)
        response = client.search(**body) if pit_id else client.search(index=index, **body)
    except ContentSearchElasticsearchError:
        raise
    except Exception as error:
        raise _read_error(error) from error

    response_body = _response_body(response)
    hits = response_body.get("hits") if isinstance(response_body, dict) else None
    raw_hits = hits.get("hits") if isinstance(hits, dict) else None
    if not isinstance(hits, dict) or not isinstance(raw_hits, list):
        raise ContentSearchElasticsearchError("es_invalid_content_search_response", retryable=True)
    page_ids = []
    sort_values = []
    highlights_by_page_id = {}
    for hit in raw_hits:
        if not isinstance(hit, dict):
            continue
        source = hit.get("_source") or {}
        try:
            page_id = int(source.get("page_id", hit.get("_id")))
        except (TypeError, ValueError):
            continue
        page_ids.append(page_id)
        hit_sort = hit.get("sort")
        sort_values.append(tuple(hit_sort) if isinstance(hit_sort, list) else None)
        matched_field, fragments, title_fragment = extract_safe_highlights(
            hit,
            CONTENT_SEARCH_HIGHLIGHT_FIELDS,
        )
        if matched_field:
            highlights_by_page_id[page_id] = ContentSearchHitHighlight(
                page_id=page_id,
                matched_field=matched_field,
                fragments=fragments,
                title_fragment=title_fragment,
            )
    public_page_ids, public_sort_values = _public_hits_in_order(page_ids, sort_values)
    return ContentSearchQueryPage(
        page_ids=public_page_ids,
        total=_total_hits(hits),
        took_ms=response_body.get("took") if isinstance(response_body, dict) else None,
        sort_values=public_sort_values,
        pit_id=response_body.get("pit_id", pit_id) if isinstance(response_body, dict) else pit_id,
        highlights=_public_highlights_in_order(public_page_ids, highlights_by_page_id),
    )


class ContentSearchResults:
    """把独立索引的 ID 结果转换成 Wagtail 页面列表。"""

    def __init__(self, target, query_string, start_date=None, end_date=None, order_by=None, date_field="date"):
        self.target = target
        self.query_string = query_string
        self.start_date = start_date
        self.end_date = end_date
        self.order_by = order_by
        self.date_field = date_field
        self._count_cache = None

    def count(self):
        if self._count_cache is None:
            try:
                page = query_content_search_page(
                    self.target,
                    self.query_string,
                    size=0,
                    start_date=self.start_date,
                    end_date=self.end_date,
                    order_by=self.order_by,
                    date_field=self.date_field,
                )
            except ContentSearchElasticsearchError as error:
                raise ContentSearchQueryUnavailable(error.code) from error
            self._count_cache = page.total
        return self._count_cache

    def __len__(self):
        return min(self.count(), 10000)

    def __bool__(self):
        return self.count() > 0

    def __getitem__(self, key):
        if isinstance(key, slice):
            start = key.start or 0
            stop = key.stop if key.stop is not None else start + 20
            items = self._fetch(start, max(stop - start, 0))
            return items
        items = self._fetch(key, 1)
        if not items:
            raise IndexError(key)
        return items[0]

    def _fetch(self, start, size):
        try:
            result = query_content_search_page(
                self.target,
                self.query_string,
                start=start,
                size=size,
                start_date=self.start_date,
                end_date=self.end_date,
                order_by=self.order_by,
                date_field=self.date_field,
            )
        except ContentSearchElasticsearchError as error:
            raise ContentSearchQueryUnavailable(error.code) from error
        if not result.page_ids:
            return []
        preserved = Case(*[When(pk=page_id, then=position) for position, page_id in enumerate(result.page_ids)])
        pages = list(
            BlogPage.objects.live().public().filter(pk__in=result.page_ids).specific().order_by(preserved)
        )
        self._attach_highlights(pages, result.highlights)
        return pages

    def cursor_page(self, token, page_size, search_type="blog", locale=""):
        """使用签名 search_after 游标读取一页公开结果。"""

        query_hash = build_cursor_query_hash(
            self.query_string,
            search_type,
            self.start_date,
            self.end_date,
            self.order_by,
            locale,
        )
        cursor = decode_content_search_cursor(token, query_hash)
        pit_id = cursor.pit_id if cursor else None
        if getattr(settings, "CONTENT_SEARCH_PIT_ENABLED", False) and not pit_id:
            try:
                response = get_content_search_client(self.target).open_point_in_time(
                    index=content_search_read_alias(),
                    keep_alive=settings.CONTENT_SEARCH_PIT_KEEP_ALIVE,
                )
                response_body = _response_body(response)
                pit_id = response_body.get("id") if isinstance(response_body, dict) else None
            except Exception as error:
                raise ContentSearchQueryUnavailable("content_search_pit_unavailable") from error
            if not pit_id:
                raise ContentSearchQueryUnavailable("content_search_pit_invalid")

        direction = cursor.direction if cursor else "next"
        try:
            result = query_content_search_page(
                self.target,
                self.query_string,
                size=page_size + 1,
                start_date=self.start_date,
                end_date=self.end_date,
                order_by=self.order_by,
                search_after=cursor.sort if cursor else None,
                reverse=direction == "previous",
                pit_id=pit_id,
                date_field=self.date_field,
            )
        except ContentSearchElasticsearchError as error:
            raise ContentSearchQueryUnavailable(error.code) from error

        page_ids = list(result.page_ids[:page_size])
        sort_values = list(result.sort_values[:page_size])
        has_more = len(result.page_ids) > page_size
        if direction == "previous":
            page_ids.reverse()
            sort_values.reverse()
        pages = self._pages_for_ids(page_ids)
        self._attach_highlights(pages, result.highlights)
        previous_cursor = None
        next_cursor = None
        active_pit_id = result.pit_id or pit_id
        if sort_values and (cursor is not None or direction == "previous"):
            if direction != "previous" or has_more:
                previous_cursor = encode_content_search_cursor(
                    ContentSearchCursor("previous", sort_values[0], active_pit_id),
                    query_hash,
                )
        if sort_values and (direction == "previous" or has_more):
            next_cursor = encode_content_search_cursor(
                ContentSearchCursor("next", sort_values[-1], active_pit_id),
                query_hash,
            )
        return ContentSearchCursorPage(pages, result.total, previous_cursor, next_cursor)

    @staticmethod
    def _pages_for_ids(page_ids):
        if not page_ids:
            return []
        preserved = Case(*[When(pk=page_id, then=position) for position, page_id in enumerate(page_ids)])
        return list(
            BlogPage.objects.live().public().filter(pk__in=page_ids).specific().order_by(preserved)
        )

    def _attach_highlights(self, pages, highlights):
        highlights_by_id = {highlight.page_id: highlight for highlight in highlights}
        for page in pages:
            highlight = highlights_by_id.get(page.pk)
            if highlight is None:
                continue
            # 页面已通过 live/public 二次回查，此后才允许挂载来自 ES 的展示片段。
            setattr(page, "search_matched_field", highlight.matched_field)
            setattr(page, "search_highlight_fragments", list(highlight.fragments))
            setattr(page, "search_title_highlight", highlight.title_fragment)
            if highlight.title_fragment:
                setattr(page, "search_title_query", self.query_string)


def build_content_search_results(query_string, start_date=None, end_date=None, order_by=None):
    target = get_content_search_serving_target()
    return ContentSearchResults(target, query_string, start_date, end_date, order_by)


def build_content_search_results_for_date_field(
    query_string, start_date=None, end_date=None, order_by=None, date_field="date"
):
    """为联邦查询显式绑定日期字段，避免把 BlogPage.date 误用于普通页面。"""
    target = get_content_search_serving_target()
    return ContentSearchResults(target, query_string, start_date, end_date, order_by, date_field)
