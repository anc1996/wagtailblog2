# search/analytics.py
from wagtail.contrib.search_promotions.models import Query
from django.db.models import Count, F, IntegerField
from django.db.models.functions import Coalesce
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class SearchAnalytics:
    """搜索分析工具"""

    @staticmethod
    def get_popular_searches(days=30, limit=10):
        """获取热门搜索词"""
        start_date = datetime.now() - timedelta(days=days)

        popular_searches = Query.objects.filter(
            daily_hits__date__gte=start_date
        ).annotate(
            # 确保按 daily_hits 表中的实际记录计数，通常用主键
            total_hits=Coalesce(Count('daily_hits__id'), 0, output_field=IntegerField())
        ).filter(total_hits__gt=0).order_by('-total_hits')[:limit] # 只选择点击量大于0的

        return [
            {
                'query': q.query_string,
                'hits': q.total_hits
            }
            for q in popular_searches
        ]

    @staticmethod
    def get_search_trends(days=30, order_by=None):
        """获取搜索趋势，支持排序"""
        start_date = datetime.now() - timedelta(days=days)

        # 首先按日期聚合获取每日的总搜索次数
        daily_stats_query = Query.objects.filter(
            daily_hits__date__gte=start_date
        ).values('daily_hits__date').annotate(
            total_searches=Coalesce(Count('daily_hits__id'), 0, output_field=IntegerField())
        ).filter(total_searches__gt=0) # 通常我们只关心有搜索记录的日期

        # 根据 order_by 参数排序
        if order_by:
            valid_order_fields = {
                'date': 'daily_hits__date',
                '-date': '-daily_hits__date',
                'searches': 'total_searches',
                '-searches': '-total_searches',
            }
            # 使用更安全的映射来获取实际的排序字段
            actual_order_by_field = valid_order_fields.get(order_by)

            if actual_order_by_field:
                daily_stats_query = daily_stats_query.order_by(actual_order_by_field)
            else:
                # 默认按日期升序
                daily_stats_query = daily_stats_query.order_by('daily_hits__date')
        else:
            # 默认按日期升序
            daily_stats_query = daily_stats_query.order_by('daily_hits__date')

        logger.debug(f"Search trends SQL query: {str(daily_stats_query.query)}")
        return list(daily_stats_query)

    @staticmethod
    def log_search(query, results_count, search_type='all'):
        """记录搜索行为"""
        try:
            # 这里可以扩展记录更多信息
            logger.info(f"搜索: '{query}' - 类型: {search_type} - 结果数: {results_count}")
        except Exception as e:
            logger.error(f"记录搜索失败: {e}")