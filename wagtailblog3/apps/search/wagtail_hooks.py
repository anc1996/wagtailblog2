# search/wagtail_hooks.py
from wagtail import hooks
from wagtail.admin.menu import MenuItem
from django.urls import path, reverse
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from .analytics import SearchAnalytics
import logging

logger = logging.getLogger(__name__)

@hooks.register('register_admin_menu_item')
def register_search_analytics_menu():
    return MenuItem(
        '搜索分析',
        reverse('search_analytics'),
        icon_name='search',
        order=800
    )

@hooks.register('register_admin_urls')
def register_search_admin_urls():
    @staff_member_required
    def search_analytics_view(request):
        # 接收前端传来的排序参数，默认为按日期升序
        # 前端会传 'date', '-date', 'searches', '-searches'
        order_by_param = request.GET.get('order_by', 'date')

        popular_searches_data = SearchAnalytics.get_popular_searches()
        # 将前端的排序参数传递给 get_search_trends
        search_trends_data = SearchAnalytics.get_search_trends(order_by=order_by_param)

        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            # 如果是 AJAX 请求，只返回搜索趋势的 JSON 数据
            # 格式化日期以便在 JavaScript 中使用
            formatted_trends = [
                {
                    # daily_hits__date 是 QuerySet annotate 后的字段名
                    'date': item['daily_hits__date'].strftime('%Y-%m-%d') if item.get('daily_hits__date') else 'N/A',
                    'total_searches': item['total_searches']
                }
                for item in search_trends_data
            ]
            return JsonResponse({'search_trends': formatted_trends, 'current_order_by': order_by_param})

        # 如果是普通请求，渲染完整页面
        context = {
            'popular_searches': popular_searches_data,
            'search_trends': search_trends_data,
            'current_order_by': order_by_param, # 将当前排序方式传递给模板
        }
        return render(request, 'search/admin/analytics.html', context)

    return [
        path('search-analytics/', search_analytics_view, name='search_analytics'),
    ]