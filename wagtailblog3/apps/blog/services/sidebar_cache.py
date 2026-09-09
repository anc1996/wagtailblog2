"""侧边栏归档与随机作者缓存服务 (SidebarCacheService).

本模块负责博客侧边栏高开销组件的高性能装配与缓存治理：
1. ArchiveSidebarService: 归档年份与月份聚合缓存、初始 DOM 瘦身与渐进展开上下文；
2. RandomAuthorSidebarService: 杜绝 ORDER BY RAND()，候选 ID 集合与小时级时间桶缓存，Fail-Open 确定性哈希降级。
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from django.conf import settings
from django.core.cache import caches
from django.db.models import Count
from django.db.models.functions import TruncMonth, TruncYear
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from wagtail.models import Locale, Site

from blog.models import Author, BlogPage

logger = logging.getLogger(__name__)

ARCHIVE_POLICY_VERSION = "1"
ARCHIVE_AGGREGATE_TTL = 21600  # 6 小时
ARCHIVE_FRAGMENT_TTL = 3600    # 1 小时
AUTHOR_CANDIDATES_TTL = 1800   # 30 分钟
AUTHOR_PICK_TTL = 3600         # 1 小时


def _get_cache():
    """获取默认 Redis 缓存后端."""
    return caches['default']


def _get_site_and_locale_ids(request: HttpRequest | None) -> tuple[int, int]:
    """安全解析请求对应的 Wagtail Site 与 Locale 主键，具备全场景容错兜底."""
    site_id = 0
    locale_id = 0
    if request:
        try:
            site = Site.find_for_request(request)
            if site and site.pk:
                site_id = site.pk
        except Exception:
            pass
        try:
            active_locale = Locale.get_active()
            if active_locale and active_locale.pk:
                locale_id = active_locale.pk
        except Exception:
            pass

    if site_id == 0:
        try:
            default_site = Site.objects.filter(is_default_site=True).first()
            if default_site and default_site.pk:
                site_id = default_site.pk
        except Exception:
            pass

    if locale_id == 0:
        try:
            default_locale = Locale.objects.filter(language_code=settings.LANGUAGE_CODE).first()
            if default_locale and default_locale.pk:
                locale_id = default_locale.pk
        except Exception:
            pass

    return site_id, locale_id


def _get_generation(key_prefix: str, site_id: int, locale_id: int) -> int:
    """获取特定侧边栏的作用域代次."""
    from blog.services.listing_invalidation import ListingInvalidationService
    if key_prefix == "archive":
        return ListingInvalidationService.get_archive_generation(site_id, locale_id)
    elif key_prefix == "author":
        return ListingInvalidationService.get_author_generation(site_id, locale_id)
    return 1

@dataclass(frozen=True, slots=True)
class AuthorCardDTO:
    """侧边栏随机作者轻量级展示 DTO，隔离富文本正文与敏感字段."""
    pk: int
    name: str
    slug: str
    bio_preview_html: str
    has_bio: bool
    detail_url: str
    author_image_url: str | None = None
    author_image: Any = None

    def get_bio_preview_html(self, word_limit: int = 3) -> str:
        """模板兼容调用代理."""
        return self.bio_preview_html


class ArchiveSidebarService:
    """归档侧边栏聚合与模板上下文装配服务."""

    @classmethod
    def get_aggregate_cache_key(cls, site_id: int, locale_id: int, gen: int) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.sidebar_archive_aggregate_key(site_id, locale_id, gen, ARCHIVE_POLICY_VERSION)

    @classmethod
    def get_lock_cache_key(cls, site_id: int, locale_id: int, gen: int) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.sidebar_archive_lock_key(site_id, locale_id, gen, ARCHIVE_POLICY_VERSION)

    @classmethod
    def calculate_aggregate(cls, site_id: int, locale_id: int) -> dict[int, dict[str, Any]]:
        """执行优化后的单次/两次轻量聚合计算归档树（无模板 N+1）."""
        pages = BlogPage.objects.live().public()
        if locale_id > 0:
            pages = pages.filter(locale_id=locale_id)
        if site_id > 0:
            from wagtail.models import Site
            try:
                site = Site.objects.filter(id=site_id).select_related("root_page").first()
                if site and site.root_page:
                    pages = pages.descendant_of(site.root_page)
                else:
                    return {}
            except Exception as e:
                logger.warning(f"archive_aggregate_filter_site_failed: {e}")
                return {}

        yearly_archives = (
            pages.annotate(year=TruncYear('date'))
            .values('year')
            .annotate(count=Count('id'))
            .order_by('-year')
        )
        monthly_archives = (
            pages.annotate(year=TruncYear('date'), month=TruncMonth('date'))
            .values('year', 'month')
            .annotate(count=Count('id'))
            .order_by('-year', '-month')
        )

        archive_tree: dict[int, dict[str, Any]] = {}
        for item in yearly_archives:
            if not item.get('year'):
                continue
            year_val = item['year'].year
            archive_tree[year_val] = {
                'count': item['count'],
                'months': {},
            }

        for item in monthly_archives:
            if not item.get('year') or not item.get('month'):
                continue
            year_val = item['year'].year
            month_val = item['month'].month
            if year_val in archive_tree:
                archive_tree[year_val]['months'][month_val] = {
                    'count': item['count'],
                    'name': item['month'].strftime('%B'),
                    'display_name': f"{month_val}月",
                }

        return archive_tree

    @classmethod
    def get_archive_aggregate(cls, site_id: int, locale_id: int) -> dict[int, dict[str, Any]]:
        """获取带 Redis 缓存与 Single-Flight 保护的归档全量聚合数据."""
        cache_enabled = getattr(settings, 'BLOG_SIDEBAR_CACHE_V2', False)
        gen = _get_generation('archive', site_id, locale_id)
        cache_key = cls.get_aggregate_cache_key(site_id, locale_id, gen)

        cache = _get_cache()
        if cache_enabled:
            try:
                cached_data = cache.get(cache_key)
                if cached_data is not None:
                    return cached_data
            except Exception as e:
                logger.warning(f"archive_cache_read_failed: {e}")

        # Single-Flight 互斥锁保护，防止归档冷缓存并发重算击穿
        lock_key = cls.get_lock_cache_key(site_id, locale_id, gen)
        token = uuid.uuid4().hex
        acquired = False
        if cache_enabled:
            try:
                acquired = bool(cache.add(lock_key, token, timeout=5))
                if not acquired:
                    # 遭遇并发计算，短暂停顿后尝试读取其他进程已写入的缓存
                    for wait_interval in (0.05, 0.1, 0.2):
                        time.sleep(wait_interval)
                        cached_data = cache.get(cache_key)
                        if cached_data is not None:
                            return cached_data
            except Exception as e:
                logger.warning(f"archive_lock_failed: {e}")

        try:
            # 未命中缓存且获取锁（或锁降级）后计算聚合
            data = cls.calculate_aggregate(site_id, locale_id)

            if cache_enabled:
                try:
                    jitter = random.randint(0, int(ARCHIVE_AGGREGATE_TTL * 0.2))
                    cache.set(cache_key, data, timeout=ARCHIVE_AGGREGATE_TTL + jitter)
                except Exception as e:
                    logger.warning(f"archive_cache_write_failed: {e}")

            return data
        finally:
            if acquired and lock_key and token and cache_enabled:
                try:
                    if cache.get(lock_key) == token:
                        cache.delete(lock_key)
                except Exception:
                    pass

    @classmethod
    def get_context(
        cls,
        request: HttpRequest | None,
        current_year: int | None = None,
        current_month: int | None = None,
    ) -> dict[str, Any]:
        """构建侧边栏渲染所需的精简上下文（支持最多三年展开与 DOM 极致瘦身）."""
        site_id, locale_id = _get_site_and_locale_ids(request)
        archive_tree_raw = cls.get_archive_aggregate(site_id, locale_id)

        progressive_enabled = getattr(settings, 'BLOG_ARCHIVE_PROGRESSIVE_V2', True)

        # 构造渲染展示树
        display_tree: dict[int, dict[str, Any]] = {}
        sorted_years = sorted(archive_tree_raw.keys(), reverse=True)

        total_posts = 0
        hidden_year_count = 0

        # 最新年份及过去两年（最多3年）默认展开详细月份，更早年份折叠仅显示摘要入口
        top_years_to_expand = set(sorted_years[:3])
        if current_year and current_year in archive_tree_raw:
            top_years_to_expand.add(current_year)

        for index, year in enumerate(sorted_years):
            raw_year_data = archive_tree_raw[year]
            year_count = raw_year_data['count']
            total_posts += year_count

            is_initially_hidden = index >= 3 and year != current_year
            if is_initially_hidden:
                hidden_year_count += 1

            should_render_month_grid = True
            if progressive_enabled and year not in top_years_to_expand:
                should_render_month_grid = False

            try:
                year_url = reverse('archive:year_archive', args=[year])
            except Exception:
                year_url = f"/archive/year/{year}/"

            month_grid = []
            raw_months = raw_year_data.get('months', {})

            if should_render_month_grid:
                for m in range(1, 13):
                    m_data = raw_months.get(m)
                    if m_data:
                        try:
                            m_url = reverse('archive:month_archive', args=[year, m])
                        except Exception:
                            m_url = f"/archive/month/{year}/{m}/"
                        month_grid.append({
                            'month': m,
                            'display_name': f"{m}月",
                            'count': m_data['count'],
                            'url': m_url,
                            'has_posts': True,
                        })
                    else:
                        month_grid.append({
                            'month': m,
                            'display_name': f"{m}月",
                            'count': 0,
                            'url': None,
                            'has_posts': False,
                        })

            display_tree[year] = {
                'count': year_count,
                'url': year_url,
                'is_initially_hidden': is_initially_hidden,
                'should_render_month_grid': should_render_month_grid,
                'months': raw_months,
                'month_grid': month_grid,
            }

        return {
            'archive_tree': display_tree,
            'archive_year_count': len(sorted_years),
            'archive_total_posts': total_posts,
            'archive_latest_year': sorted_years[0] if sorted_years else None,
            'archive_earliest_year': sorted_years[-1] if sorted_years else None,
            'hidden_year_count': hidden_year_count,
            'current_year': current_year,
            'current_month': current_month,
            'request': request,
        }


class RandomAuthorSidebarService:
    """随机作者侧边栏服务：杜绝 ORDER BY RAND()，采用小时级桶缓存与确定性哈希降级."""

    @classmethod
    def get_candidates_cache_key(cls, site_id: int, locale_id: int, gen: int) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.sidebar_author_candidates_key(site_id, locale_id, gen)

    @classmethod
    def get_pick_cache_key(cls, site_id: int, locale_id: int, gen: int, time_bucket: str) -> str:
        from blog.services.redis_protocol import RedisKeyProtocol
        return RedisKeyProtocol.sidebar_author_pick_key(site_id, locale_id, gen, time_bucket)

    @classmethod
    def get_candidate_ids(cls, site_id: int, locale_id: int) -> list[int]:
        """获取有公开文章上线的作者 ID 列表（轻量有序整数清单）."""
        cache_enabled = getattr(settings, 'BLOG_SIDEBAR_CACHE_V2', False)
        gen = _get_generation('author', site_id, locale_id)
        cache_key = cls.get_candidates_cache_key(site_id, locale_id, gen)

        cache = _get_cache()
        if cache_enabled:
            try:
                cached_ids = cache.get(cache_key)
                if cached_ids is not None:
                    return cached_ids
            except Exception as e:
                logger.warning(f"author_candidates_cache_read_failed: {e}")

        # 仅选择绑定了已上线页面的作者，避免推荐无内容作者
        # 候选过滤：必须关联已发布、公开且属于对应 site / locale 的文章作者
        pages_qs = BlogPage.objects.live().public()
        if locale_id > 0:
            pages_qs = pages_qs.filter(locale_id=locale_id)
        if site_id > 0:
            from wagtail.models import Site
            try:
                site = Site.objects.filter(id=site_id).select_related("root_page").first()
                if site and site.root_page:
                    pages_qs = pages_qs.descendant_of(site.root_page)
                else:
                    return []
            except Exception as e:
                logger.warning(f"author_candidates_filter_site_failed: {e}")
                return []

        valid_page_ids = pages_qs.values_list("id", flat=True)
        author_ids = list(
            Author.objects.filter(blogpage__in=valid_page_ids)
            .distinct()
            .order_by("id")
            .values_list("id", flat=True)
        )

        if cache_enabled:
            try:
                cache.set(cache_key, author_ids, timeout=AUTHOR_CANDIDATES_TTL)
            except Exception as e:
                logger.warning(f"author_candidates_cache_write_failed: {e}")

        return author_ids

    @classmethod
    def pick_author_id(cls, candidate_ids: list[int], site_id: int, locale_id: int, gen: int) -> int | None:
        """基于小时时间桶与 Redis/哈希算法稳定选取作者 ID，绝对不使用 ORDER BY RAND()."""
        if not candidate_ids:
            return None

        time_bucket = time.strftime('%Y%m%d%H')
        cache_enabled = getattr(settings, 'BLOG_SIDEBAR_CACHE_V2', False)
        pick_key = cls.get_pick_cache_key(site_id, locale_id, gen, time_bucket)

        cache = _get_cache()
        if cache_enabled:
            try:
                picked = cache.get(pick_key)
                if picked is not None and picked in candidate_ids:
                    return int(picked)
            except Exception as e:
                logger.warning(f"author_pick_cache_read_failed: {e}")

        # 确定性伪随机计算：利用 generation + time_bucket 散列，Redis 宕机时完全确定无异常
        seed_str = f"{gen}:{site_id}:{locale_id}:{time_bucket}"
        hash_val = int(hashlib.md5(seed_str.encode('utf-8')).hexdigest()[:8], 16)
        chosen_id = candidate_ids[hash_val % len(candidate_ids)]

        if cache_enabled:
            try:
                cache.set(pick_key, chosen_id, timeout=AUTHOR_PICK_TTL)
            except Exception as e:
                logger.warning(f"author_pick_cache_write_failed: {e}")

        return chosen_id

    @classmethod
    def get_context(cls, request: HttpRequest | None) -> dict[str, Any]:
        """构建侧边栏随机作者卡片上下文，安全空兜底，零正文泄露."""
        site_id, locale_id = _get_site_and_locale_ids(request)
        gen = _get_generation('author', site_id, locale_id)

        candidate_ids = cls.get_candidate_ids(site_id, locale_id)
        if not candidate_ids:
            return {'random_author': None, 'request': request}

        chosen_id = cls.pick_author_id(candidate_ids, site_id, locale_id, gen)
        if not chosen_id:
            return {'random_author': None, 'request': request}

        try:
            author = (
                Author.objects.select_related('author_image')
                .filter(pk=chosen_id)
                .first()
            )
        except Exception:
            author = None

        if not author:
            return {'random_author': None, 'request': request}

        # 构造轻量 DTO
        detail_url = ''
        try:
            detail_url = reverse('blog:author_detail', args=[author.pk])
        except Exception:
            pass

        bio_preview = ''
        try:
            bio_preview = author.get_bio_preview_html(word_limit=3)
        except Exception:
            pass

        author_img_url = None
        if author.author_image:
            try:
                author_img_url = author.author_image.file.url
            except Exception:
                pass

        dto = AuthorCardDTO(
            pk=author.pk,
            name=author.name,
            slug=author.slug,
            bio_preview_html=bio_preview,
            has_bio=bool(author.bio),
            detail_url=detail_url,
            author_image_url=author_img_url,
            author_image=author.author_image,
        )

        return {'random_author': dto, 'request': request}