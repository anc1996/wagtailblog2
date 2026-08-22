# 搜索应用的 Wagtail 后台扩展
from wagtail import hooks
from wagtail.admin.menu import MenuItem
from django.urls import path, reverse
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.http import HttpRequest, HttpResponse
from .analytics import SearchAnalytics
import logging

logger = logging.getLogger(__name__)

@hooks.register('register_admin_menu_item')
def register_search_analytics_menu():
    """注册仅 staff 可访问的搜索统计菜单项。"""
    return MenuItem(
        '搜索分析',
        reverse('search_analytics'),
        icon_name='search',
        order=800
    )

@hooks.register('register_admin_urls')
def register_search_admin_urls():
    """注册后台统计 URL；内部视图由 staff_member_required 保护。"""
    @staff_member_required
    def search_analytics_view(request: HttpRequest) -> HttpResponse:
        # 接收前端传来的排序参数，默认为按日期升序
        # 前端只允许传入日期或搜索次数对应的四种排序值。
        order_by_param = request.GET.get('order_by', 'date')

        popular_searches_data = SearchAnalytics.get_popular_searches()
        # 将排序参数交给统计层统一校验和转换。
        search_trends_data = SearchAnalytics.get_search_trends(order_by=order_by_param)

        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            # 异步请求只返回趋势数据，并把日期转换成前端易处理的字符串。
            formatted_trends = [
                {
                    # daily_hits__date 是聚合查询生成的日期字段名。
                    'date': item['daily_hits__date'].strftime('%Y-%m-%d') if item.get('daily_hits__date') else 'N/A',
                    'total_searches': item['total_searches']
                }
                for item in search_trends_data
            ]
            return JsonResponse({'search_trends': formatted_trends, 'current_order_by': order_by_param})

        # 普通请求渲染完整的后台分析页面。
        context = {
            'popular_searches': popular_searches_data,
            'search_trends': search_trends_data,
            'current_order_by': order_by_param, # 将当前排序方式传递给模板
        }
        return render(request, 'search/admin/analytics.html', context)

    return [
        path('search-analytics/', search_analytics_view, name='search_analytics'),
    ]
