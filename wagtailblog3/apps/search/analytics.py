"""搜索统计的只读聚合服务。

P0 只读取 Wagtail 的 ``QueryDailyHits``，所以所有时间边界都使用项目本地日期，
并且不会把按日聚合数据伪装成单次事件、小时统计或搜索类型统计。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.core.paginator import Paginator
from django.db.models import Count, IntegerField, Max, QuerySet, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from wagtail.contrib.search_promotions.models import QueryDailyHits
from wagtail.search.utils import normalise_query_string


MAX_ANALYTICS_DAYS = 1830
MAX_TOP_QUERIES = 10
_RANGE_DAYS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
    "today": 1,
    "yesterday": 1,
    "last7": 7,
    "last14": 14,
    "last30": 30,
}
_GRANULARITIES = {"day", "week", "month", "year"}
_DEFAULT_GRANULARITY = {
    "month": "week",
    "year": "month",
    "today": "day",
    "yesterday": "day",
    "last7": "day",
    "last14": "week",
    "last30": "week",
    "this_month": "week",
    "last_month": "week",
    "custom": "day",
}


class AnalyticsValidationError(ValueError):
    """后台报表参数不能安全映射到有限日期范围时抛出。"""


def normalise_search_query(value: object) -> str:
    """使用 Wagtail 的唯一规范化规则收敛检索、统计和联想词。"""

    return normalise_query_string(str(value or ""))


class SearchAnalytics:
    """提供不读取正文、用户信息或搜索索引的按日统计视图。"""

    @staticmethod
    def resolve_date_range(
        range_key: str = "month",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> tuple[str, date, date]:
        """校验并返回含首尾的本地日期范围，阻止后台报表发生无界查询。"""

        today = timezone.localdate()
        if range_key == "today":
            return range_key, today, today
        if range_key == "yesterday":
            yesterday = today - timedelta(days=1)
            return range_key, yesterday, yesterday
        if range_key == "this_month":
            return range_key, today.replace(day=1), today
        if range_key == "last_month":
            this_month = today.replace(day=1)
            month_end = this_month - timedelta(days=1)
            return range_key, month_end.replace(day=1), month_end
        if range_key == "this_year":
            return range_key, today.replace(month=1, day=1), today
        if range_key in _RANGE_DAYS:
            return (
                range_key,
                today - timedelta(days=_RANGE_DAYS[range_key] - 1),
                today,
            )
        if range_key != "custom":
            raise AnalyticsValidationError("无效的时间范围")
        try:
            resolved_start = date.fromisoformat(start_date or "")
            resolved_end = date.fromisoformat(end_date or "")
        except ValueError as error:
            raise AnalyticsValidationError("自定义范围必须提供有效日期") from error
        if resolved_end < resolved_start:
            raise AnalyticsValidationError("结束日期不能早于开始日期")
        if (resolved_end - resolved_start).days + 1 > MAX_ANALYTICS_DAYS:
            raise AnalyticsValidationError(f"自定义范围不能超过 {MAX_ANALYTICS_DAYS} 天")
        return range_key, resolved_start, resolved_end

    @staticmethod
    def _daily_hits(start_date: date, end_date: date) -> QuerySet[QueryDailyHits]:
        """限制在已校验范围内的日聚合行；调用方不得从这里读取页面正文。"""

        return QueryDailyHits.objects.filter(date__range=(start_date, end_date))

    @staticmethod
    def available_granularities(span_days: int) -> list[str]:
        """根据日期跨度返回允许的趋势颗粒度。"""

        if span_days <= 7:
            return ["day"]
        if span_days <= 31:
            return ["day", "week"]
        if span_days <= 180:
            return ["week", "month"]
        if span_days < 365:
            return ["month"]
        return ["month", "year"]

    @staticmethod
    def resolve_granularity(
        range_key: str,
        granularity: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> str:
        """按日期跨度限制趋势颗粒度，避免短区间出现没有意义的周/月桶。"""

        if start_date is None or end_date is None:
            _, start_date, end_date = SearchAnalytics.resolve_date_range(range_key)
        span_days = (end_date - start_date).days + 1
        available = SearchAnalytics.available_granularities(span_days)
        if granularity in available:
            return str(granularity)
        if range_key == "custom":
            # 自定义范围按跨度选择默认值，长期范围保留月级细节并把年级作为可选项。
            if span_days <= 7:
                preferred = "day"
            elif span_days <= 180:
                preferred = "week"
            else:
                preferred = "month"
        else:
            preferred = _DEFAULT_GRANULARITY.get(range_key, "day")
        if preferred in available:
            return preferred
        return available[-1]

    @staticmethod
    def _bucket_start(value: date, granularity: str) -> date:
        """将本地日期映射到日、周一或自然月桶的起始日期。"""

        if granularity == "week":
            return value - timedelta(days=value.weekday())
        if granularity == "month":
            return value.replace(day=1)
        if granularity == "year":
            return value.replace(month=1, day=1)
        return value

    @staticmethod
    def _next_bucket(value: date, granularity: str) -> date:
        """推进连续趋势桶，避免依赖数据库方言的日期截断函数。"""

        if granularity == "week":
            return value + timedelta(days=7)
        if granularity == "month":
            return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)
        if granularity == "year":
            return date(value.year + 1, 1, 1)
        return value + timedelta(days=1)

    @staticmethod
    def _filter_query(
        daily_hits: QuerySet[QueryDailyHits], query: str, *, exact: bool = False
    ) -> QuerySet[QueryDailyHits]:
        """将查询词筛选应用到聚合；仪表盘下钻必须精确匹配同一规范化词。"""

        normalised_query = normalise_search_query(query)
        if normalised_query:
            if exact:
                return daily_hits.filter(query__query_string=normalised_query)
            return daily_hits.filter(query__query_string__icontains=normalised_query)
        return daily_hits

    @classmethod
    def get_popular_searches(
        cls,
        days: int = 30,
        limit: int = 10,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        query: str = "",
        exact_query: bool = False,
    ) -> list[dict[str, Any]]:
        """累计真实 hits 后返回热词，兼容原有 ``days`` 调用方。"""

        if start_date is None or end_date is None:
            window_days = min(max(days, 1), MAX_ANALYTICS_DAYS)
            _, start_date, end_date = cls.resolve_date_range(
                "custom",
                (timezone.localdate() - timedelta(days=window_days - 1)).isoformat(),
                timezone.localdate().isoformat(),
            )
        try:
            safe_limit = min(max(int(limit), 1), 20)
        except (TypeError, ValueError):
            safe_limit = 10
        rows = (
            cls._filter_query(
                cls._daily_hits(start_date, end_date), query, exact=exact_query
            )
            .values("query__query_string")
            .annotate(hits=Coalesce(Sum("hits"), 0, output_field=IntegerField()))
            .filter(hits__gt=0)
            .order_by("-hits", "query__query_string")[:safe_limit]
        )
        return [
            {"query": row["query__query_string"], "hits": row["hits"]}
            for row in rows
        ]

    @classmethod
    def get_search_trends(
        cls,
        days: int = 30,
        order_by: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        query: str = "",
        exact_query: bool = False,
        granularity: str = "day",
    ) -> list[dict[str, Any]]:
        """返回连续趋势桶；缺失的日、周或月在服务层补零以保持时轴等距。"""

        granularity = granularity if granularity in _GRANULARITIES else "day"

        if start_date is None or end_date is None:
            window_days = min(max(days, 1), MAX_ANALYTICS_DAYS)
            _, start_date, end_date = cls.resolve_date_range(
                "custom",
                (timezone.localdate() - timedelta(days=window_days - 1)).isoformat(),
                timezone.localdate().isoformat(),
            )
        totals = {
            row["date"]: row["total_searches"]
            for row in (
                cls._filter_query(
                    cls._daily_hits(start_date, end_date), query, exact=exact_query
                )
                .values("date")
                .annotate(
                    total_searches=Coalesce(
                        Sum("hits"), 0, output_field=IntegerField()
                    )
                )
            )
        }
        rows = []
        current_bucket = cls._bucket_start(start_date, granularity)
        end_bucket = cls._bucket_start(end_date, granularity)
        bucket_totals: dict[date, int] = {}
        for day, hits in totals.items():
            bucket = cls._bucket_start(day, granularity)
            bucket_totals[bucket] = bucket_totals.get(bucket, 0) + hits
        while current_bucket <= end_bucket:
            rows.append(
                {
                    "date": current_bucket,
                    "daily_hits__date": current_bucket,
                    "total_searches": bucket_totals.get(current_bucket, 0),
                }
            )
            current_bucket = cls._next_bucket(current_bucket, granularity)

        sort_key = order_by or "date"
        if sort_key == "-date":
            return list(reversed(rows))
        if sort_key == "searches":
            return sorted(rows, key=lambda row: (row["total_searches"], row["date"]))
        if sort_key == "-searches":
            return sorted(
                rows, key=lambda row: (row["total_searches"], row["date"]), reverse=True
            )
        return rows

    @classmethod
    def get_records(
        cls,
        start_date: date,
        end_date: date,
        query: str = "",
        *,
        exact_query: bool = False,
    ) -> QuerySet[QueryDailyHits]:
        """返回按日、按规范化查询词聚合的明细行，而不是个人行为记录。"""

        records = cls._daily_hits(start_date, end_date).select_related("query")
        normalised_query = normalise_search_query(query)
        if normalised_query:
            if exact_query:
                return records.filter(query__query_string=normalised_query).filter(
                    hits__gt=0
                ).order_by("-date", "-hits", "query__query_string")
            records = records.filter(query__query_string__icontains=normalised_query)
        return records.filter(hits__gt=0).order_by("-date", "-hits", "query__query_string")

    @classmethod
    def get_today_searches(cls, query: str = "") -> QuerySet[QueryDailyHits]:
        """保留旧后台调用的今日聚合查询，实际复用统一记录口径。"""

        today = timezone.localdate()
        return cls.get_records(today, today, query)

    @classmethod
    def build_dashboard(
        cls,
        range_key: str = "month",
        start_date: str | None = None,
        end_date: str | None = None,
        query: str = "",
        page_number: int = 1,
        page_size: int = 20,
        top_n: int = 10,
        granularity: str | None = None,
        analysis_query: str | None = None,
        records_query: str | None = None,
    ) -> dict[str, Any]:
        """构造同一筛选口径的概览、趋势、热词和可分页日聚合记录。"""

        resolved_range, resolved_start, resolved_end = cls.resolve_date_range(
            range_key, start_date, end_date
        )
        resolved_granularity = cls.resolve_granularity(
            resolved_range, granularity, resolved_start, resolved_end
        )
        span_days = (resolved_end - resolved_start).days + 1
        available_granularities = cls.available_granularities(span_days)
        default_granularity = cls.resolve_granularity(
            resolved_range, None, resolved_start, resolved_end
        )
        try:
            safe_page_size = min(max(int(page_size), 10), 100)
        except (TypeError, ValueError):
            safe_page_size = 20
        try:
            # 图表最多展示十个独立词条，其余词条统一汇总，避免图例和标签在窄屏溢出。
            safe_top_n = min(max(int(top_n), 1), MAX_TOP_QUERIES)
        except (TypeError, ValueError):
            safe_top_n = 10
        try:
            safe_page_number = max(int(page_number), 1)
        except (TypeError, ValueError):
            safe_page_number = 1
        # 兼容旧调用方的 query；新后台将分析和记录筛选拆成两个独立口径。
        analysis_filter = query if analysis_query is None else analysis_query
        records_filter = query if records_query is None else records_query
        normalised_query = normalise_search_query(analysis_filter)
        normalised_records_query = normalise_search_query(records_filter)
        bucket_count = len(
            cls.get_search_trends(
                start_date=resolved_start,
                end_date=resolved_end,
                query=normalised_query,
                exact_query=True,
                granularity=resolved_granularity,
            )
        )
        daily_hits = cls._filter_query(
            cls._daily_hits(resolved_start, resolved_end), normalised_query, exact=True
        )
        summary = daily_hits.aggregate(
            total_searches=Coalesce(Sum("hits"), 0, output_field=IntegerField()),
            active_queries=Count("query", distinct=True),
            latest_date=Max("date"),
        )
        page = Paginator(
            cls.get_records(
                resolved_start,
                resolved_end,
                normalised_records_query,
                exact_query=records_query is None,
            ),
            safe_page_size,
        ).get_page(safe_page_number)
        total_searches = summary["total_searches"]
        top_queries = []
        top_query_hits = 0
        for rank, row in enumerate(
            cls.get_popular_searches(
                start_date=resolved_start,
                end_date=resolved_end,
                days=30,
                limit=safe_top_n,
                query=normalised_query,
                exact_query=True,
            ),
            start=1,
        ):
            top_queries.append(
                {
                    "rank": rank,
                    "query": row["query"],
                    "hits": row["hits"],
                    "share": round(row["hits"] / total_searches * 100, 1)
                    if total_searches
                    else 0,
                    "is_other": False,
                }
            )
            top_query_hits += row["hits"]
        other_hits = max(total_searches - top_query_hits, 0)
        if other_hits:
            top_queries.append(
                {
                    "rank": None,
                    "query": "其他",
                    "hits": other_hits,
                    "share": round(other_hits / total_searches * 100, 1)
                    if total_searches
                    else 0,
                    "is_other": True,
                }
            )
        return {
            "range": {
                "key": resolved_range,
                "from": resolved_start.isoformat(),
                "to": resolved_end.isoformat(),
                "granularity": resolved_granularity,
                "default_granularity": default_granularity,
                "available_granularities": available_granularities,
                "days": span_days,
                "bucket_count": bucket_count,
                # QueryDailyHits 未存储类型维度，避免向后台或用户伪造可筛选能力。
                "search_type": "all",
            },
            "summary": {
                "total_searches": total_searches,
                "active_queries": summary["active_queries"],
                "latest_date": summary["latest_date"].isoformat()
                if summary["latest_date"]
                else None,
            },
            "trend": [
                {"date": row["date"].isoformat(), "hits": row["total_searches"]}
                for row in cls.get_search_trends(
                    start_date=resolved_start,
                    end_date=resolved_end,
                    query=normalised_query,
                    exact_query=True,
                    granularity=resolved_granularity,
                )
            ],
            "top_queries": top_queries,
            "records": [
                {"date": row.date.isoformat(), "query": row.query.query_string, "hits": row.hits}
                for row in page.object_list
            ],
            "pagination": {
                "page": page.number,
                "total_pages": page.paginator.num_pages,
                "total_count": page.paginator.count,
                "page_size": safe_page_size,
                "has_previous": page.has_previous(),
                "has_next": page.has_next(),
            },
            "query": normalised_query,
            "records_query": normalised_records_query,
        }
