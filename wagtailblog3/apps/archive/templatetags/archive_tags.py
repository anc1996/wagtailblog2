# 归档应用的模板标签
from __future__ import annotations

from typing import Any

from django import template
from django.conf import settings
from django.db.models import Count
from django.db.models.functions import TruncMonth, TruncYear
from django.urls import reverse

from blog.models import BlogPage
from blog.services.sidebar_cache import ArchiveSidebarService

register = template.Library()


def _legacy_archive_sidebar(context: dict[str, Any], current_year: int | None = None, current_month: int | None = None) -> dict[str, Any]:
    """回退执行旧版侧边栏逻辑（Kill Switch 降级模式）."""
    blog_pages = BlogPage.objects.live()

    yearly_archives = blog_pages.annotate(
        year=TruncYear('date')
    ).values('year').annotate(
        count=Count('id')
    ).order_by('-year')

    monthly_archives = blog_pages.annotate(
        year=TruncYear('date'),
        month=TruncMonth('date')
    ).values('year', 'month').annotate(
        count=Count('id')
    ).order_by('-year', '-month')

    archive_tree = {}
    for item in yearly_archives:
        year = item['year'].year
        archive_tree[year] = {
            'count': item['count'],
            'months': {},
            'should_render_month_grid': True,
        }

    for item in monthly_archives:
        year = item['year'].year
        month = item['month'].month
        month_name = item['month'].strftime('%B')

        if year in archive_tree:
            archive_tree[year]['months'][month] = {
                'count': item['count'],
                'name': month_name,
                'display_name': f"{month}月"
            }

    total_posts = 0
    hidden_year_count = 0
    for index, year in enumerate(archive_tree):
        year_data = archive_tree[year]
        year_data['url'] = reverse('archive:year_archive', args=[year])
        year_data['is_initially_hidden'] = index >= 5 and year != current_year
        if year_data['is_initially_hidden']:
            hidden_year_count += 1
        total_posts += year_data['count']
        month_grid = []
        for month in range(1, 13):
            month_data = year_data['months'].get(month)
            if month_data:
                month_data['url'] = reverse(
                    'archive:month_archive',
                    args=[year, month]
                )
                month_grid.append({
                    'month': month,
                    'display_name': f'{month}月',
                    'count': month_data['count'],
                    'url': month_data['url'],
                    'has_posts': True,
                })
            else:
                month_grid.append({
                    'month': month,
                    'display_name': f'{month}月',
                    'count': 0,
                    'url': None,
                    'has_posts': False,
                })
        year_data['month_grid'] = month_grid

    years = list(archive_tree)
    return {
        'archive_tree': archive_tree,
        'archive_year_count': len(years),
        'archive_total_posts': total_posts,
        'archive_latest_year': years[0] if years else None,
        'archive_earliest_year': years[-1] if years else None,
        'hidden_year_count': hidden_year_count,
        'current_year': current_year,
        'current_month': current_month,
        'request': context.get('request') if context else None,
    }


@register.inclusion_tag('archive/tags/archive_sidebar.html', takes_context=True)
def archive_sidebar(context: dict[str, Any], current_year: int | None = None, current_month: int | None = None) -> dict[str, Any]:
    """生成归档侧边栏（受 BLOG_SIDEBAR_CACHE_V2 控制，关闭时彻底回退旧逻辑，开启时委托至 ArchiveSidebarService）."""
    if not getattr(settings, 'BLOG_SIDEBAR_CACHE_V2', False):
        return _legacy_archive_sidebar(context, current_year=current_year, current_month=current_month)

    request = context.get('request') if context else None
    return ArchiveSidebarService.get_context(
        request=request,
        current_year=current_year,
        current_month=current_month,
    )


# 用于转换日期格式的过滤器
@register.filter
def get_item(dictionary: Any, key: Any) -> Any:
    """允许在模板中通过变量访问字典的键值."""
    if hasattr(dictionary, 'get'):
        return dictionary.get(key)
    return None
