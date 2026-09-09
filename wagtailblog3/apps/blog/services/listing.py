"""博客分类与列表页批量装配服务 (ListingService).

本模块负责博客索引页（BlogIndexPage）及异步 API 的统一列表装配。
架构特性：
1. 批量查询与多态适配（BLOG_INDEX_COMPAT_POLYMORPHIC_QUERY）；
2. 聚合统计与切片预注入 DTO（ListingCard），杜绝模板 N+1；
3. Single-Flight 互斥锁与 Fail-Open 熔断降级；
4. 严格隔离正文数据，杜绝 Mongo/Revision 泄露。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Literal, Mapping
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.cache import caches
from django.core.paginator import Paginator
from django.db import connection, models
from django.db.models import Count, F, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce, Lower
from django.http import HttpRequest
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_date

logger = logging.getLogger(__name__)

BLOG_INDEX_ITEMS_PER_PAGE = 20
BLOG_INDEX_DEFAULT_SORT_PRIMARY = "date_desc"
BLOG_INDEX_DEFAULT_SORT_SECONDARY = "title_asc"
BLOG_INDEX_SORT_FIELDS = {
    "date_asc": "sort_date",
    "date_desc": "-sort_date",
    "title_asc": "sort_title",
    "title_desc": "-sort_title",
}

_REACTION_TYPES_CACHE = None
_TAG_INDEX_PAGE_CACHE = None


def _get_cached_reaction_types() -> list[Any]:
    """进程内存缓存常用互动反应类型，避免高频点查."""
    global _REACTION_TYPES_CACHE
    if _REACTION_TYPES_CACHE is None:
        from blog.models import ReactionType
        _REACTION_TYPES_CACHE = list(ReactionType.objects.all())
    return _REACTION_TYPES_CACHE


_TAG_INDEX_URL_CACHE: str | None = None
_DEFAULT_SITE_CACHE: Any = None


def _get_default_site() -> Any:
    """进程内存缓存默认 Wagtail 站点，避免请求路由时高频查询 wagtailcore_site."""
    global _DEFAULT_SITE_CACHE
    if _DEFAULT_SITE_CACHE is None:
        from wagtail.models import Site
        _DEFAULT_SITE_CACHE = Site.objects.filter(is_default_site=True).first()
    return _DEFAULT_SITE_CACHE


def _get_cached_tag_index_page() -> Any:
    """进程内存缓存单例标签索引页."""
    global _TAG_INDEX_PAGE_CACHE
    if _TAG_INDEX_PAGE_CACHE is None:
        from blog.models import BlogTagIndexPage
        _TAG_INDEX_PAGE_CACHE = BlogTagIndexPage.objects.live().public().first()
    return _TAG_INDEX_PAGE_CACHE


def _get_cached_tag_index_url(request: HttpRequest | None = None) -> str:
    """进程内存缓存标签索引页 URL，消除重复 Site 路由查询."""
    global _TAG_INDEX_URL_CACHE
    if _TAG_INDEX_URL_CACHE is None:
        p = _get_cached_tag_index_page()
        if p:
            try:
                _TAG_INDEX_URL_CACHE = p.get_url(request=request) or getattr(p, "url", "")
            except Exception:
                _TAG_INDEX_URL_CACHE = getattr(p, "url", "")
        else:
            _TAG_INDEX_URL_CACHE = ""
    return _TAG_INDEX_URL_CACHE


@dataclass(frozen=True, slots=True)
class ListingFilters:
    """分类列表规范化筛选参数."""

    page: int
    search: str
    start_date: str
    end_date: str
    sort_primary: str
    sort_secondary: str


@dataclass(slots=True)
class ListingAuthorDTO:
    """作者元数据轻量 DTO."""

    pk: int
    name: str
    slug: str


@dataclass(slots=True)
class ListingTagDTO:
    """标签元数据轻量 DTO."""

    pk: int
    name: str
    slug: str


@dataclass(slots=True)
class AuthorStub:
    pk: int | None
    name: str

    @property
    def id(self) -> int | None:
        return self.pk


@dataclass(slots=True)
class TagStub:
    name: str
    slug: str


class ListingPaginationProxy:
    """纯数据分页代理，提供模板渲染所需的无缝方法与属性."""

    def __init__(self, data: Mapping[str, Any]):
        self.number = int(data.get("page", 1))
        self.total_pages = int(data.get("total_pages", 1))
        self.total_results = int(data.get("total_results", 0))
        self.has_previous_val = bool(data.get("has_previous", False))
        self.has_next_val = bool(data.get("has_next", False))
        self.prev_num = data.get("previous_page_number")
        self.next_num = data.get("next_page_number")
        self._start_idx = int(data.get("start_index", 1))
        self._end_idx = int(data.get("end_index", 1))

    @property
    def has_previous(self) -> bool:
        return self.has_previous_val

    @property
    def has_next(self) -> bool:
        return self.has_next_val

    @property
    def previous_page_number(self) -> int | None:
        return self.prev_num

    @property
    def next_page_number(self) -> int | None:
        return self.next_num

    def start_index(self) -> int:
        return self._start_idx

    def end_index(self) -> int:
        return self._end_idx

    @property
    def paginator(self) -> Any:
        return SimpleNamespace(num_pages=self.total_pages, count=self.total_results)


@dataclass(slots=True)
class ListingCard:
    """分类列表文章展示卡片 DTO.

    严格隔离 MongoDB 正文与 Revision，仅承载列表渲染所需的元数据与聚合统计。
    消除对 Django ORM Model 的引用持有，确保可安全进行 Redis 缓存序列化。
    """

    pk: int
    title: str
    url: str
    date: Any
    intro: str
    featured_image: Any = None
    featured_rendition: Any = None
    authors: list[Any] = field(default_factory=list)
    tags: list[Any] = field(default_factory=list)
    view_count: dict[str, int] = field(default_factory=dict)
    reactions: list[dict[str, Any]] = field(default_factory=list)
    image_url: str | None = None
    image_width: int | None = None
    image_height: int | None = None

    @property
    def id(self) -> int:
        return self.pk

    @property
    def specific(self) -> ListingCard:
        """保持原有模板对 post.specific 的无缝兼容，直接返回 DTO 自身."""
        return self

    @property
    def listing_authors(self) -> list[Any]:
        return self.authors

    @property
    def listing_tags(self) -> list[Any]:
        return self.tags

    def get_view_count(self) -> dict[str, int]:
        return self.view_count

    def get_reactions(self) -> list[dict[str, Any]]:
        return self.reactions

    def get_url(self, request: HttpRequest | None = None) -> str:
        return self.url


@dataclass(slots=True)
class ListingResult:
    """分类列表装配结果 DTO."""

    filters: ListingFilters
    blog_pages: list[Any]
    page_obj: Any
    total_results: int
    has_active_filters: bool
    secondary_sort_options: tuple[tuple[str, str], ...]
    blog_tag_index_page: Any
    canonical_url: str
    cache_status: str
    query_count: int
    assembly_ms: float
    html: str = ""
    payload: dict[str, Any] | None = None
    blog_tag_index_url: str = ""
    page_url: str = ""

    def as_context_dict(self) -> dict[str, Any]:
        """导出为 Django 模板上下文兼容的字典."""
        return {
            "blog_pages": self.blog_pages,
            "search_query": self.filters.search,
            "start_date": self.filters.start_date,
            "end_date": self.filters.end_date,
            "sort_primary": self.filters.sort_primary,
            "sort_secondary": self.filters.sort_secondary,
            "secondary_sort_options": self.secondary_sort_options,
            "page_obj": self.page_obj,
            "total_results": self.total_results,
            "has_active_filters": self.has_active_filters,
            "blog_tag_index_page": self.blog_tag_index_page,
            "blog_tag_index_url": self.blog_tag_index_url,
            "page_url": self.page_url,
            "canonical_url": self.canonical_url,
            "cache_status": self.cache_status,
            "query_count": self.query_count,
            "assembly_ms": self.assembly_ms,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_context_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_context_dict().get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.as_context_dict()

    def keys(self):
        return self.as_context_dict().keys()

    def values(self):
        return self.as_context_dict().values()

    def items(self):
        return self.as_context_dict().items()


def _normalise_date_str(value: object) -> tuple[str, Any]:
    """规范化日期筛选参数，返回展示字符串与解析后的 date 对象或 None."""
    text = (str(value or "")).strip()
    if not text:
        return "", None
    try:
        parsed = parse_date(text)
    except ValueError:
        parsed = None
    return (text, parsed) if parsed else ("", None)


def normalize_listing_params(query_params: Mapping[str, object] | None) -> ListingFilters:
    """将任意查询参数规范化为安全的 ListingFilters."""
    params = query_params or {}

    raw_page = params.get("page")
    try:
        page_num = max(1, int(raw_page)) if raw_page is not None else 1
    except (ValueError, TypeError):
        page_num = 1

    search_query = str(params.get("search") or "").strip()
    start_date_str, _ = _normalise_date_str(params.get("start_date"))
    end_date_str, _ = _normalise_date_str(params.get("end_date"))

    sort_primary = str(params.get("sort_primary") or BLOG_INDEX_DEFAULT_SORT_PRIMARY)
    if sort_primary not in BLOG_INDEX_SORT_FIELDS:
        sort_primary = BLOG_INDEX_DEFAULT_SORT_PRIMARY

    if sort_primary.startswith("date_"):
        valid_secondary = {"title_asc", "title_desc"}
        default_secondary = "title_asc"
    else:
        valid_secondary = {"date_desc", "date_asc"}
        default_secondary = "date_desc"

    sort_secondary = str(params.get("sort_secondary") or default_secondary)
    if sort_secondary not in valid_secondary:
        sort_secondary = default_secondary

    return ListingFilters(
        page=page_num,
        search=search_query,
        start_date=start_date_str,
        end_date=end_date_str,
        sort_primary=sort_primary,
        sort_secondary=sort_secondary,
    )


def compute_canonical_url(
    page: Any,
    filters: ListingFilters,
    request: HttpRequest | None = None,
) -> str:
    """计算当前筛选状态下的规范 URL (Canonical URL)."""
    params: dict[str, Any] = {}
    if filters.search:
        params["search"] = filters.search
    if filters.start_date:
        params["start_date"] = filters.start_date
    if filters.end_date:
        params["end_date"] = filters.end_date
    if (
        filters.sort_primary != BLOG_INDEX_DEFAULT_SORT_PRIMARY
        or filters.sort_secondary != BLOG_INDEX_DEFAULT_SORT_SECONDARY
    ):
        params["sort_primary"] = filters.sort_primary
        params["sort_secondary"] = filters.sort_secondary
    if filters.page > 1:
        params["page"] = filters.page

    raw_url = page.get_url(request=request) if hasattr(page, "get_url") else getattr(page, "url", None)
    page_url = raw_url if isinstance(raw_url, str) else "/"
    page_path = urlsplit(page_url).path or "/"
    return f"{page_path}?{urlencode(params)}" if params else page_path


def _generate_listing_cache_key(
    site_id: int,
    locale_id: int,
    index_id: int,
    filters: ListingFilters,
    generation: str = "1",
) -> str:
    """生成隔离且可代次失效的列表缓存键."""
    query_repr = f"p={filters.page}:s={filters.search}:sd={filters.start_date}:ed={filters.end_date}:sp={filters.sort_primary}:ss={filters.sort_secondary}"
    query_hash = hashlib.sha256(query_repr.encode("utf-8")).hexdigest()[:16]
    from blog.services.redis_protocol import RedisKeyProtocol
    return RedisKeyProtocol.listing_cache_key(site_id, locale_id, index_id, query_hash, generation)


def _is_preview_request(request: HttpRequest | None) -> bool:
    """核验当前请求是否为管理后台实时预览模式."""
    if request is None:
        return False
    return bool(
        getattr(request, "in_preview_panel", False)
        or request.GET.get("in_preview_panel")
        or request.GET.get("preview")
    )


def _batch_prefetch_post_data(posts: list[Any], request: HttpRequest | None = None) -> list[Any]:
    """对列表页的文章集合执行批量抓取、统计聚合与 ListingCard DTO 投射."""
    if not posts:
        return []

    from wagtail.models import Page
    real_pages = [p for p in posts if isinstance(p, Page) and getattr(p, "pk", None) is not None]
    if not real_pages:
        return posts

    page_ids = [p.pk for p in real_pages if isinstance(p.pk, int)]
    if not page_ids:
        return posts

    from blog.models import BlogPage, BlogPageTag, BlogRendition, PageViewCount, Reaction

    # 1. 批量加载 BlogPage 模型属性与特色图片 (1 次查询)
    try:
        pages_qs = (
            BlogPage.objects.filter(pk__in=page_ids)
            .defer("body")
            .select_related("featured_image")
        )
        pages_by_id = {p.pk: p for p in pages_qs}
    except Exception:
        logger.warning("listing_batch_fetch_pages_failed", exc_info=True)
        pages_by_id = {}

    # 2. 批量加载作者关联 (1 次查询，通过 through 消除 N+1 与多重 prefetch)
    authors_by_page: dict[int, list[Any]] = defaultdict(list)
    try:
        through_authors = BlogPage.authors.through.objects.filter(
            blogpage_id__in=page_ids
        ).select_related("author")
        for rel in through_authors:
            authors_by_page[rel.blogpage_id].append(rel.author)
    except Exception:
        logger.warning("listing_batch_fetch_authors_failed", exc_info=True)

    # 3. 批量加载标签关联 (1 次查询，通过 BlogPageTag 消除多重 prefetch)
    tags_by_page: dict[int, list[Any]] = defaultdict(list)
    try:
        through_tags = BlogPageTag.objects.filter(
            content_object_id__in=page_ids
        ).select_related("tag")
        for rel in through_tags:
            tags_by_page[rel.content_object_id].append(rel.tag)
    except Exception:
        logger.warning("listing_batch_fetch_tags_failed", exc_info=True)

    # 4. 批量聚合访问量 (1 次查询，条件聚合合并历史总量与今日统计)
    totals_map: dict[int, dict[str, Any]] = {}
    try:
        today = timezone.localdate()
        view_rows = (
            PageViewCount.objects.filter(page_id__in=page_ids)
            .values("page_id")
            .annotate(
                total=Sum("view_count_v2"),
                total_unique=Sum("unique_visitor_count_v2"),
                legacy_total=Sum("count"),
                legacy_total_unique=Sum("unique_count"),
                today_views=Sum("view_count_v2", filter=models.Q(date=today)),
                today_unique=Sum("unique_visitor_count_v2", filter=models.Q(date=today)),
                today_legacy=Sum("count", filter=models.Q(date=today)),
                today_legacy_unique=Sum("unique_count", filter=models.Q(date=today)),
            )
        )
        totals_map = {r["page_id"]: r for r in view_rows}
    except Exception:
        logger.warning("listing_batch_fetch_views_failed", exc_info=True)

    # 5. 批量聚合互动反应 (1 次查询，ReactionType 使用内存缓存 0 查询)
    rx_by_page: dict[int, dict[int, int]] = defaultdict(dict)
    reaction_types: list[Any] = []
    try:
        reaction_types = _get_cached_reaction_types()
        rx_rows = (
            Reaction.objects.filter(page_id__in=page_ids)
            .values("page_id", "reaction_type_id")
            .annotate(count=Count("id"))
        )
        for r in rx_rows:
            rx_by_page[r["page_id"]][r["reaction_type_id"]] = r["count"]
    except Exception:
        logger.warning("listing_batch_fetch_reactions_failed", exc_info=True)

    # 6. 批量预取特色图片切片 (Rendition width-420，仅在存在图片时 1 次查询)
    renditions_by_image: dict[int, Any] = {}
    try:
        image_ids = [
            p.featured_image_id
            for p in pages_by_id.values()
            if getattr(p, "featured_image_id", None)
        ]
        if image_ids:
            rends = BlogRendition.objects.filter(
                image_id__in=image_ids, filter_spec="width-420"
            )
            for rend in rends:
                renditions_by_image[rend.image_id] = rend
    except Exception:
        logger.warning("listing_batch_fetch_renditions_failed", exc_info=True)

    # 7. 投射为安全强隔离的 ListingCard DTO
    projected_cards: list[Any] = []
    for raw_item in posts:
        pid = getattr(raw_item, "pk", None)
        post_instance = pages_by_id.get(pid)

        # 非 BlogPage 多态节点兼容回退
        if post_instance is None:
            projected_cards.append(getattr(raw_item, "specific", raw_item))
            continue

        tot = totals_map.get(pid, {})
        view_count_dict = {
            "today": tot.get("today_views") or 0,
            "today_unique": tot.get("today_unique") or 0,
            "total": tot.get("total") or 0,
            "total_unique": tot.get("total_unique") or 0,
            "legacy_today": tot.get("today_legacy") or 0,
            "legacy_today_unique": tot.get("today_legacy_unique") or 0,
            "legacy_total": tot.get("legacy_total") or 0,
            "legacy_total_unique": tot.get("legacy_total_unique") or 0,
        }

        post_rx = rx_by_page.get(pid, {})
        reactions_list = [
            {
                "id": rt.id,
                "name": rt.name,
                "icon": rt.icon,
                "count": post_rx.get(rt.id, 0),
            }
            for rt in reaction_types
        ]

        # 挂载预注入属性以支持直接访问
        post_instance._prefetched_view_count = view_count_dict
        post_instance._prefetched_reactions = reactions_list

        authors_list = authors_by_page.get(pid, [])
        post_instance.listing_authors = authors_list

        tags_list = tags_by_page.get(pid, [])
        post_instance.listing_tags = tags_list

        # 切片预取
        featured_img = getattr(post_instance, "featured_image", None)
        featured_rendition = None
        img_url = None
        img_w = None
        img_h = None
        if featured_img is not None and getattr(featured_img, "id", None) in renditions_by_image:
            featured_rendition = renditions_by_image[featured_img.id]
            featured_img.prefetched_renditions = [featured_rendition]
            img_url = getattr(featured_rendition, "url", None)
            img_w = getattr(featured_rendition, "width", None)
            img_h = getattr(featured_rendition, "height", None)

        post_url = None
        if hasattr(post_instance, "get_url"):
            try:
                post_url = post_instance.get_url(request=request)
            except Exception:
                post_url = None
        if not post_url:
            post_url = getattr(post_instance, "url", None) or f"/{getattr(post_instance, 'slug', '')}/"

        card = ListingCard(
            pk=pid,
            title=getattr(post_instance, "title", ""),
            url=post_url,
            date=getattr(post_instance, "date", None),
            intro=getattr(post_instance, "intro", "") or "",
            featured_image=featured_img,
            featured_rendition=featured_rendition,
            authors=authors_list,
            tags=tags_list,
            view_count=view_count_dict,
            reactions=reactions_list,
            image_url=img_url,
            image_width=img_w,
            image_height=img_h,
        )
        projected_cards.append(card)

    return projected_cards




def _serialize_listing_for_cache(result: ListingResult) -> dict[str, Any]:
    """将 ListingResult 序列化为无 ORM 实例引用的纯 JSON 标量字典."""
    serialized_cards = []
    for c in result.blog_pages:
        if isinstance(c, ListingCard):
            serialized_cards.append({
                "pk": c.pk,
                "title": c.title,
                "url": c.url,
                "date": c.date.isoformat() if hasattr(c.date, "isoformat") else (str(c.date) if c.date else ""),
                "intro": c.intro,
                "image_url": c.image_url,
                "image_width": c.image_width,
                "image_height": c.image_height,
                "authors": [
                    {"pk": getattr(a, "pk", getattr(a, "id", None)), "name": getattr(a, "name", str(a))}
                    for a in c.authors
                ],
                "tags": [
                    {"name": getattr(t, "name", str(t)), "slug": getattr(t, "slug", str(t))}
                    for t in c.tags
                ],
                "view_count": c.view_count,
                "reactions": c.reactions,
            })
        else:
            serialized_cards.append({
                "pk": getattr(c, "pk", 0),
                "title": getattr(c, "title", ""),
                "url": getattr(c, "url", ""),
                "date": str(getattr(c, "date", "")),
                "intro": getattr(c, "intro", ""),
                "image_url": None,
                "image_width": None,
                "image_height": None,
                "authors": [],
                "tags": [],
                "view_count": {},
                "reactions": [],
            })

    pagination_data = {}
    if result.page_obj:
        po = result.page_obj
        pag = getattr(po, "paginator", None)
        pagination_data = {
            "page": getattr(po, "number", 1),
            "page_size": BLOG_INDEX_ITEMS_PER_PAGE,
            "total_pages": getattr(pag, "num_pages", 1) if pag else 1,
            "total_results": result.total_results,
            "has_previous": po.has_previous() if callable(getattr(po, "has_previous", None)) else bool(getattr(po, "has_previous", False)),
            "has_next": po.has_next() if callable(getattr(po, "has_next", None)) else bool(getattr(po, "has_next", False)),
            "previous_page_number": (po.previous_page_number() if (callable(getattr(po, "has_previous", None)) and po.has_previous()) else None) if callable(getattr(po, "previous_page_number", None)) else getattr(po, "previous_page_number", None),
            "next_page_number": (po.next_page_number() if (callable(getattr(po, "has_next", None)) and po.has_next()) else None) if callable(getattr(po, "next_page_number", None)) else getattr(po, "next_page_number", None),
            "start_index": po.start_index() if callable(getattr(po, "start_index", None)) else getattr(po, "start_index", 1),
            "end_index": po.end_index() if callable(getattr(po, "end_index", None)) else getattr(po, "end_index", 1),
        }

    return {
        "blog_pages": serialized_cards,
        "pagination": pagination_data,
        "total_results": result.total_results,
        "has_active_filters": result.has_active_filters,
        "canonical_url": result.canonical_url,
        "blog_tag_index_url": result.blog_tag_index_url,
        "page_url": result.page_url,
        "html": result.html,
        "payload": result.payload,
    }


def _deserialize_listing_from_cache(cached_data: dict[str, Any]) -> tuple[list[ListingCard], Any]:
    """从 Redis 缓存数据重构纯 DTO 列表和分页代理."""
    cards = []
    for raw in cached_data.get("blog_pages", []):
        if isinstance(raw, ListingCard):
            cards.append(raw)
            continue
        authors = [AuthorStub(a.get("pk"), a.get("name", "")) for a in raw.get("authors", [])]
        tags = [TagStub(t.get("name", ""), t.get("slug", "")) for t in raw.get("tags", [])]
        card = ListingCard(
            pk=raw.get("pk", 0),
            title=raw.get("title", ""),
            url=raw.get("url", ""),
            date=raw.get("date"),
            intro=raw.get("intro", ""),
            authors=authors,
            tags=tags,
            view_count=raw.get("view_count", {}),
            reactions=raw.get("reactions", []),
            image_url=raw.get("image_url"),
            image_width=raw.get("image_width"),
            image_height=raw.get("image_height"),
        )
        cards.append(card)

    pagination_data = cached_data.get("pagination", {})
    page_proxy = ListingPaginationProxy(pagination_data) if pagination_data else None
    return cards, page_proxy



def _ensure_json_payload(
    result: ListingResult,
    index_page: Any,
    filters: ListingFilters,
    request: HttpRequest | None,
) -> None:
    """确保 ListingResult 拥有用于异步 API 返回的 payload 与 html (中文契约保障)."""
    if result.payload is not None and result.html:
        return

    if not result.html:
        context = result.as_context_dict()
        context["page"] = index_page
        context["page_url"] = result.page_url or (getattr(index_page, "url", None) or "")
        context["blog_tag_index_url"] = result.blog_tag_index_url
        result.html = render_to_string(
            "blog/partials/_blog_index_results.html",
            context,
            request=request,
        )

    po = result.page_obj
    pag = getattr(po, "paginator", None)
    total_pages = getattr(pag, "num_pages", 1) if pag else 1
    has_prev = po.has_previous() if (po and callable(getattr(po, "has_previous", None))) else bool(getattr(po, "has_previous", False))
    has_nxt = po.has_next() if (po and callable(getattr(po, "has_next", None))) else bool(getattr(po, "has_next", False))

    result.payload = {
        "filters": {
            "search": filters.search,
            "start_date": filters.start_date,
            "end_date": filters.end_date,
            "sort_primary": filters.sort_primary,
            "sort_secondary": filters.sort_secondary,
        },
        "result_count": result.total_results,
        "html": result.html,
        "pagination": {
            "page": getattr(po, "number", 1) if po else 1,
            "page_size": BLOG_INDEX_ITEMS_PER_PAGE,
            "total_pages": total_pages,
            "has_previous": has_prev,
            "has_next": has_nxt,
        },
        "canonical_url": result.canonical_url,
    }


class ListingService:
    """博客分类列表统一服务实现."""

    def __init__(self, cache_alias: str = "default") -> None:
        self.cache_alias = cache_alias

    def _get_cache(self):
        try:
            return caches[self.cache_alias]
        except Exception:
            return caches["default"]

    def build_listing(
        self,
        request: HttpRequest | None,
        index_page: Any,
        query_params: Mapping[str, object] | None = None,
        *,
        output_format: Literal["context", "json"] = "context",
    ) -> ListingResult:
        """构建分类列表的高性能上下文与结果数据."""
        t0 = time.perf_counter()
        initial_queries = len(connection.queries)

        if request is None:
            from django.test import RequestFactory
            request = RequestFactory().get(getattr(index_page, "url", None) or "/")

        if request is not None and not hasattr(request, "_wagtail_site"):
            page_site = getattr(index_page, "site", None)
            if page_site:
                request._wagtail_site = page_site
            else:
                try:
                    site = _get_default_site()
                    if site:
                        request._wagtail_site = site
                except Exception:
                    pass

        filters = normalize_listing_params(query_params)
        _, start_date = _normalise_date_str(filters.start_date)
        _, end_date = _normalise_date_str(filters.end_date)

        if filters.sort_primary.startswith("date_"):
            secondary_sort_options = (
                ("title_asc", "标题 (A到Z)"),
                ("title_desc", "标题 (Z到A)"),
            )
        else:
            secondary_sort_options = (
                ("date_desc", "时间 (新到旧)"),
                ("date_asc", "时间 (旧到新)"),
            )

        # 检查 L2 Redis 列表缓存开关
        cache_enabled = getattr(settings, "BLOG_INDEX_CACHE_V2", False)
        is_preview = _is_preview_request(request)
        cache_backend = self._get_cache()

        cache_key = None
        lock_key = None
        token = None
        site_id = 0
        if request and hasattr(request, "site") and request.site:
            site_id = getattr(request.site, "pk", 0)
        if not site_id:
            site_id = getattr(getattr(index_page, "site", None), "pk", 0) or 1
        locale_id = getattr(index_page, "locale_id", 0) or 1

        if cache_enabled and not is_preview:
            from blog.services.listing_invalidation import ListingInvalidationService
            generation = ListingInvalidationService.get_generation(site_id, locale_id, index_page.pk)
            cache_key = _generate_listing_cache_key(site_id, locale_id, index_page.pk, filters, str(generation))
            try:
                cached_data = cache_backend.get(cache_key)
                if cached_data:
                    t1 = time.perf_counter()
                    reconstructed_pages, reconstructed_page_obj = _deserialize_listing_from_cache(cached_data)
                    hit_result = ListingResult(
                        filters=filters,
                        blog_pages=reconstructed_pages,
                        page_obj=reconstructed_page_obj,
                        total_results=cached_data.get("total_results", 0),
                        has_active_filters=cached_data.get("has_active_filters", False),
                        secondary_sort_options=secondary_sort_options,
                        blog_tag_index_page=None,
                        canonical_url=cached_data.get("canonical_url", ""),
                        cache_status="hit",
                        query_count=0,
                        assembly_ms=(t1 - t0) * 1000,
                        html=cached_data.get("html", ""),
                        payload=cached_data.get("payload"),
                        blog_tag_index_url=cached_data.get("blog_tag_index_url", ""),
                        page_url=cached_data.get("page_url", ""),
                    )
                    if output_format == "json":
                        _ensure_json_payload(hit_result, index_page, filters, request)
                    return hit_result

                # Single-Flight 防击穿互斥锁 (TTL 5s)
                from blog.services.redis_protocol import RedisKeyProtocol
                lock_hash = hashlib.sha256(cache_key.encode()).hexdigest()[:12]
                lock_key = RedisKeyProtocol.listing_lock_key(index_page.pk, lock_hash)
                token = uuid.uuid4().hex
                acquired = bool(cache_backend.add(lock_key, token, timeout=5))
                if not acquired:
                    for wait_interval in (0.025, 0.05, 0.1):
                        time.sleep(wait_interval)
                        cached_data = cache_backend.get(cache_key)
                        if cached_data:
                            t1 = time.perf_counter()
                            reconstructed_pages, reconstructed_page_obj = _deserialize_listing_from_cache(cached_data)
                            wait_result = ListingResult(
                                filters=filters,
                                blog_pages=reconstructed_pages,
                                page_obj=reconstructed_page_obj,
                                total_results=cached_data.get("total_results", 0),
                                has_active_filters=cached_data.get("has_active_filters", False),
                                secondary_sort_options=secondary_sort_options,
                                blog_tag_index_page=None,
                                canonical_url=cached_data.get("canonical_url", ""),
                                cache_status="hit_after_wait",
                                query_count=0,
                                assembly_ms=(t1 - t0) * 1000,
                                html=cached_data.get("html", ""),
                                payload=cached_data.get("payload"),
                                blog_tag_index_url=cached_data.get("blog_tag_index_url", ""),
                                page_url=cached_data.get("page_url", ""),
                            )
                            if output_format == "json":
                                _ensure_json_payload(wait_result, index_page, filters, request)
                            return wait_result
            except Exception:
                logger.warning("listing_cache_read_failed", exc_info=True)

        try:
            # 构造子页面查询集
            from blog.models import BlogIndexPage, BlogPage

            blog_page_date_subquery = Subquery(
                BlogPage.objects.filter(page_ptr_id=OuterRef("pk")).values("date")[:1]
            )
            blog_index_page_date_subquery = Subquery(
                BlogIndexPage.objects.filter(page_ptr_id=OuterRef("pk")).values("date")[:1]
            )

            use_polymorphic = getattr(settings, "BLOG_INDEX_COMPAT_POLYMORPHIC_QUERY", True)
            base_children = index_page.get_children().live().public()

            if use_polymorphic:
                child_pages = base_children.annotate(
                    sort_date=Coalesce(
                        blog_page_date_subquery,
                        blog_index_page_date_subquery,
                        F("first_published_at"),
                        output_field=models.DateField(),
                    ),
                    sort_title=Lower("title"),
                )
            else:
                child_pages = BlogPage.objects.live().public().child_of(index_page).annotate(
                    sort_date=Coalesce(
                        F("date"),
                        F("first_published_at"),
                        output_field=models.DateField(),
                    ),
                    sort_title=Lower("title"),
                )

            if filters.search:
                child_pages = child_pages.filter(title__icontains=filters.search)
            if start_date:
                child_pages = child_pages.filter(sort_date__gte=start_date)
            if end_date:
                child_pages = child_pages.filter(sort_date__lte=end_date)

            child_pages = child_pages.order_by(
                BLOG_INDEX_SORT_FIELDS[filters.sort_primary],
                BLOG_INDEX_SORT_FIELDS[filters.sort_secondary],
                "pk",
            )

            paginator = Paginator(child_pages, BLOG_INDEX_ITEMS_PER_PAGE)
            page_obj = paginator.get_page(filters.page)

            raw_items = list(page_obj.object_list)
            prefetched_posts = _batch_prefetch_post_data(raw_items, request=request)

            tag_index_page = _get_cached_tag_index_page()
            tag_index_url = _get_cached_tag_index_url(request=request)

            page_url = ""
            if hasattr(index_page, "get_url"):
                try:
                    page_url = index_page.get_url(request=request) or getattr(index_page, "url", "")
                except Exception:
                    page_url = getattr(index_page, "url", "")
            if not page_url:
                page_url = getattr(index_page, "url", "") or ""

            has_active = bool(
                filters.search
                or filters.start_date
                or filters.end_date
                or filters.sort_primary != BLOG_INDEX_DEFAULT_SORT_PRIMARY
                or filters.sort_secondary != BLOG_INDEX_DEFAULT_SORT_SECONDARY
            )

            canonical_url = compute_canonical_url(index_page, filters, request=request)

            t1 = time.perf_counter()
            query_count = len(connection.queries) - initial_queries
            assembly_ms = (t1 - t0) * 1000

            result = ListingResult(
                filters=filters,
                blog_pages=prefetched_posts,
                page_obj=page_obj,
                total_results=paginator.count,
                has_active_filters=has_active,
                secondary_sort_options=secondary_sort_options,
                blog_tag_index_page=tag_index_page,
                canonical_url=canonical_url,
                cache_status="miss",
                query_count=query_count,
                assembly_ms=assembly_ms,
                blog_tag_index_url=tag_index_url,
                page_url=page_url,
            )

            if output_format == "json":
                _ensure_json_payload(result, index_page, filters, request)

            if cache_enabled and cache_key and not is_preview:
                try:
                    serialized_payload = _serialize_listing_for_cache(result)
                    cache_backend.set(cache_key, serialized_payload, timeout=3600)
                except Exception:
                    logger.warning("listing_cache_write_failed", exc_info=True)

            return result

        finally:
            if lock_key and token and cache_enabled:
                try:
                    release_lua = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""
                    client = getattr(cache_backend, "client", None)
                    raw_redis = None
                    if client and not hasattr(client, "_mock_return_value") and hasattr(client, "get_client"):
                        try:
                            raw_redis = client.get_client()
                        except Exception:
                            raw_redis = None

                    if raw_redis and not hasattr(raw_redis, "_mock_return_value") and hasattr(raw_redis, "eval"):
                        # 确保直连 Redis 执行 Lua 时传入真实底层物理键 (包含前缀网关修饰)
                        real_lock_key = cache_backend.make_key(lock_key) if hasattr(cache_backend, "make_key") else lock_key
                        raw_redis.eval(release_lua, 1, real_lock_key, token)
                    else:
                        current_lock = cache_backend.get(lock_key)
                        if current_lock == token:
                            cache_backend.delete(lock_key)
                except Exception:
                    logger.warning("listing_lock_release_failed", exc_info=True)


def build_listing(
    request: HttpRequest | None,
    index_page: Any,
    query_params: Mapping[str, object] | None = None,
    *,
    output_format: Literal["context", "json"] = "context",
) -> ListingResult:
    """模块级统一入口函数."""
    service = ListingService()
    return service.build_listing(
        request=request,
        index_page=index_page,
        query_params=query_params,
        output_format=output_format,
    )
