import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.test import RequestFactory
from django.utils import timezone
from wagtail.contrib.search_promotions.models import Query, QueryDailyHits

from search.analytics import SearchAnalytics, normalise_search_query
from search.core import perform_search
from search.services.suggestions import get_popular_query_suggestions
from search.wagtail_hooks import register_search_admin_urls


class SearchAnalyticsTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()

    def _query_with_daily_hits(self, query_string, values):
        query = Query.objects.create(query_string=query_string)
        for offset, hits in enumerate(values):
            QueryDailyHits.objects.create(
                query=query,
                date=self.today - timedelta(days=offset),
                hits=hits,
            )
        return query

    def test_popular_searches_sum_daily_hits(self):
        self._query_with_daily_hits("高频词", [7, 5, 3])

        popular = SearchAnalytics.get_popular_searches(days=30, limit=10)

        self.assertEqual(popular, [{"query": "高频词", "hits": 15}])

    def test_search_trends_sum_hits_for_each_day(self):
        self._query_with_daily_hits("趋势词", [8, 4])

        trends = SearchAnalytics.get_search_trends(days=30)

        by_date = {item["daily_hits__date"]: item["total_searches"] for item in trends}
        self.assertEqual(len(trends), 30)
        self.assertEqual(by_date[self.today], 8)
        self.assertEqual(by_date[self.today - timedelta(days=1)], 4)
        self.assertEqual(by_date[self.today - timedelta(days=2)], 0)

    def test_search_trends_can_group_contiguous_week_buckets(self):
        self._query_with_daily_hits("周趋势词", [2] + [0] * 6 + [3])
        start_date = self.today - timedelta(days=8)

        trends = SearchAnalytics.get_search_trends(
            start_date=start_date, end_date=self.today, granularity="week"
        )

        self.assertEqual(sum(item["total_searches"] for item in trends), 5)
        self.assertGreaterEqual(len(trends), 2)

    def test_dashboard_uses_range_specific_default_granularity(self):
        month_payload = SearchAnalytics.build_dashboard(range_key="month")
        year_payload = SearchAnalytics.build_dashboard(range_key="year")
        week_payload = SearchAnalytics.build_dashboard(range_key="week")

        self.assertEqual(month_payload["range"]["granularity"], "week")
        self.assertEqual(year_payload["range"]["granularity"], "month")
        self.assertEqual(week_payload["range"]["granularity"], "day")

    def test_dashboard_supports_named_calendar_ranges(self):
        today = self.today
        self.assertEqual(SearchAnalytics.resolve_date_range("today")[1:], (today, today))
        self.assertEqual(SearchAnalytics.resolve_date_range("yesterday")[1:], (today - timedelta(days=1), today - timedelta(days=1)))
        self.assertEqual(SearchAnalytics.resolve_date_range("last7")[1:], (today - timedelta(days=6), today))
        self.assertEqual(SearchAnalytics.resolve_date_range("last14")[1:], (today - timedelta(days=13), today))
        self.assertEqual(SearchAnalytics.resolve_date_range("last30")[1:], (today - timedelta(days=29), today))
        self.assertEqual(
            SearchAnalytics.resolve_date_range("this_year")[1:],
            (today.replace(month=1, day=1), today),
        )

    def test_custom_range_supports_five_years_and_rejects_longer_ranges(self):
        end_date = self.today
        start_date = end_date - timedelta(days=1829)

        resolved = SearchAnalytics.resolve_date_range(
            "custom", start_date.isoformat(), end_date.isoformat()
        )

        self.assertEqual(resolved[1:], (start_date, end_date))
        with self.assertRaisesRegex(ValueError, "不能超过"):
            SearchAnalytics.resolve_date_range(
                "custom",
                (end_date - timedelta(days=1830)).isoformat(),
                end_date.isoformat(),
            )

    def test_available_granularities_follow_span_limits(self):
        self.assertEqual(SearchAnalytics.available_granularities(7), ["day"])
        self.assertEqual(SearchAnalytics.available_granularities(14), ["day", "week"])
        self.assertEqual(SearchAnalytics.available_granularities(31), ["day", "week"])
        self.assertEqual(SearchAnalytics.available_granularities(32), ["week", "month"])
        self.assertEqual(SearchAnalytics.available_granularities(181), ["month"])
        self.assertEqual(SearchAnalytics.available_granularities(365), ["month", "year"])

    def test_five_year_dashboard_exposes_only_month_and_year_granularities(self):
        start_date = self.today - timedelta(days=1829)
        payload = SearchAnalytics.build_dashboard(
            range_key="custom",
            start_date=start_date.isoformat(),
            end_date=self.today.isoformat(),
        )

        self.assertEqual(payload["range"]["available_granularities"], ["month", "year"])
        self.assertEqual(payload["range"]["default_granularity"], "month")
        self.assertEqual(payload["range"]["granularity"], "month")

    def test_year_trend_is_contiguous_and_zero_filled(self):
        query = Query.objects.create(query_string="年度趋势词")
        QueryDailyHits.objects.create(query=query, date=self.today.replace(month=1, day=1), hits=4)
        QueryDailyHits.objects.create(query=query, date=self.today.replace(month=6, day=1), hits=6)

        trends = SearchAnalytics.get_search_trends(
            start_date=self.today.replace(year=self.today.year - 1, month=1, day=1),
            end_date=self.today,
            granularity="year",
        )

        self.assertEqual([row["date"] for row in trends], [
            self.today.replace(year=self.today.year - 1, month=1, day=1),
            self.today.replace(month=1, day=1),
        ])
        self.assertEqual([row["total_searches"] for row in trends], [0, 10])

    def test_dashboard_keeps_analysis_and_record_queries_separate(self):
        self._query_with_daily_hits("分析词", [4])
        self._query_with_daily_hits("记录词", [3])

        payload = SearchAnalytics.build_dashboard(
            range_key="today", analysis_query="分析词", records_query="记录词"
        )

        self.assertEqual(payload["summary"]["total_searches"], 4)
        self.assertEqual({row["query"] for row in payload["records"]}, {"记录词"})

    def test_records_endpoint_has_independent_query_filter(self):
        self._query_with_daily_hits("分析词", [4])
        self._query_with_daily_hits("记录词", [3])
        request = RequestFactory().get(
            "/admin/search-analytics/",
            {"view": "records", "range": "today", "records_q": "记录"},
        )
        request.user = type(
            "Staff", (), {"is_authenticated": True, "is_active": True, "is_staff": True}
        )()

        response = register_search_admin_urls()[0].callback(request)

        payload = json.loads(response.content)
        self.assertEqual([row["query"] for row in payload["records"]], ["记录词"])

    def test_dedicated_dashboard_and_records_urls_return_private_json(self):
        self._query_with_daily_hits("专用接口词", [2])
        staff = type(
            "Staff", (), {"is_authenticated": True, "is_active": True, "is_staff": True}
        )()
        routes = register_search_admin_urls()

        dashboard_request = RequestFactory().get(
            "/admin/search-analytics/dashboard/", {"range": "today"}
        )
        dashboard_request.user = staff
        dashboard_response = routes[1].callback(dashboard_request)
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertEqual(dashboard_response["Cache-Control"], "private, no-store")
        self.assertIn("summary", json.loads(dashboard_response.content))

        records_request = RequestFactory().get(
            "/admin/search-analytics/records/", {"range": "today"}
        )
        records_request.user = staff
        records_response = routes[2].callback(records_request)
        self.assertEqual(records_response.status_code, 200)
        self.assertEqual(records_response["Cache-Control"], "private, no-store")
        self.assertIn("records", json.loads(records_response.content))

    def test_dedicated_analytics_urls_reject_non_get_with_json_error(self):
        staff = type(
            "Staff", (), {"is_authenticated": True, "is_active": True, "is_staff": True}
        )()
        request = RequestFactory().post("/admin/search-analytics/dashboard/")
        request.user = staff
        response = register_search_admin_urls()[1].callback(request)

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(
            json.loads(response.content)["error"]["code"], "method_not_allowed"
        )

    def test_popular_suggestions_sum_daily_hits(self):
        self._query_with_daily_hits("建议词", [6, 2])

        suggestions = get_popular_query_suggestions("建议")

        self.assertEqual(
            suggestions,
            [{"query": "建议词", "hits": 8, "source": "popular"}],
        )

    def test_today_searches_filters_and_excludes_previous_days(self):
        self._query_with_daily_hits("今日词", [4])
        self._query_with_daily_hits("昨日词", [9])
        QueryDailyHits.objects.filter(query__query_string="昨日词").update(
            date=self.today - timedelta(days=1)
        )

        rows = SearchAnalytics.get_today_searches("今日")

        self.assertEqual([(row.query.query_string, row.hits) for row in rows], [("今日词", 4)])

    def test_today_ajax_returns_paginated_rows(self):
        self._query_with_daily_hits("筛选词", [3])
        request = RequestFactory().get(
            "/admin/search-analytics/",
            {"view": "today", "q": "筛选", "page": 1, "page_size": 20},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        request.user = type(
            "Staff",
            (),
            {"is_authenticated": True, "is_active": True, "is_staff": True},
        )()
        response = register_search_admin_urls()[0].callback(request)

        payload = json.loads(response.content)
        self.assertEqual(payload["query"], "筛选")
        self.assertEqual(payload["today_searches"][0]["hits"], 3)
        self.assertEqual(payload["pagination"]["total_count"], 1)

    def test_today_ajax_clamps_page_size_and_recovers_out_of_range_page(self):
        self._query_with_daily_hits("边界词", [2])
        request = RequestFactory().get(
            "/admin/search-analytics/",
            {"view": "today", "page": 999, "page_size": 1},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        request.user = type(
            "Staff",
            (),
            {"is_authenticated": True, "is_active": True, "is_staff": True},
        )()

        response = register_search_admin_urls()[0].callback(request)

        payload = json.loads(response.content)
        self.assertEqual(payload["pagination"]["page"], 1)
        self.assertEqual(payload["pagination"]["page_size"], 10)
        self.assertEqual(payload["today_searches"][0]["query"], "边界词")

    def test_today_ajax_empty_result_has_stable_pagination(self):
        request = RequestFactory().get(
            "/admin/search-analytics/",
            {"view": "today", "q": "不存在", "page": 1, "page_size": 20},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        request.user = type(
            "Staff",
            (),
            {"is_authenticated": True, "is_active": True, "is_staff": True},
        )()

        response = register_search_admin_urls()[0].callback(request)

        payload = json.loads(response.content)
        self.assertEqual(payload["today_searches"], [])
        self.assertEqual(payload["pagination"]["total_pages"], 1)
        self.assertFalse(payload["pagination"]["has_next"])

    def test_today_ajax_url_encodes_query_text(self):
        self._query_with_daily_hits("<script>alert(1)</script>", [1])
        request = RequestFactory().get(
            "/admin/search-analytics/",
            {"view": "today", "q": "script", "page": 1, "page_size": 20},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        request.user = type(
            "Staff",
            (),
            {"is_authenticated": True, "is_active": True, "is_staff": True},
        )()

        response = register_search_admin_urls()[0].callback(request)

        payload = json.loads(response.content)
        self.assertEqual(payload["today_searches"][0]["query"], "<script>alert(1)</script>")
        self.assertNotIn("<script>", payload["today_searches"][0]["url"])

    def test_search_analytics_rejects_non_staff_user(self):
        request = RequestFactory().get("/admin/search-analytics/")
        request.user = type(
            "NonStaff",
            (),
            {"is_authenticated": True, "is_active": True, "is_staff": False},
        )()

        response = register_search_admin_urls()[0].callback(request)

        self.assertEqual(response.status_code, 302)

    def test_dashboard_uses_one_filter_for_summary_trend_top_queries_and_records(self):
        self._query_with_daily_hits("统一词", [5, 3])
        self._query_with_daily_hits("统一词扩展", [7])

        payload = SearchAnalytics.build_dashboard(
            range_key="week", query="  统一词  ", page_size=20
        )

        self.assertEqual(payload["summary"]["total_searches"], 8)
        self.assertEqual(
            payload["top_queries"],
            [
                {
                    "rank": 1,
                    "query": "统一词",
                    "hits": 8,
                    "share": 100.0,
                    "is_other": False,
                }
            ],
        )
        self.assertEqual({row["query"] for row in payload["records"]}, {"统一词"})
        self.assertEqual(sum(row["hits"] for row in payload["trend"]), 8)

    def test_dashboard_query_filter_is_exact_but_legacy_today_filter_stays_partial(self):
        self._query_with_daily_hits("搜索", [3])
        self._query_with_daily_hits("搜索扩展", [5])

        dashboard = SearchAnalytics.build_dashboard(range_key="week", query="搜索")
        today_rows = SearchAnalytics.get_today_searches("搜索")

        self.assertEqual(dashboard["summary"]["total_searches"], 3)
        self.assertEqual(
            {row.query.query_string for row in today_rows}, {"搜索", "搜索扩展"}
        )

    def test_dashboard_rejects_invalid_custom_range_without_querying_history(self):
        request = RequestFactory().get(
            "/admin/search-analytics/",
            {"view": "dashboard", "range": "custom", "from": "2026-02-31", "to": "2026-03-01"},
        )
        request.user = type(
            "Staff", (), {"is_authenticated": True, "is_active": True, "is_staff": True}
        )()

        response = register_search_admin_urls()[0].callback(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["error"]["code"], "invalid_analytics_range")

    def test_normalisation_matches_wagtail_query_storage(self):
        normalised = normalise_search_query('  "规范  查询"  ')
        query = Query.objects.create(query_string=normalised)

        self.assertEqual(query.query_string, normalised)

    @patch("search.core.build_federated_search_results", return_value=[])
    def test_all_search_records_query_hit(self, _federated_search):
        perform_search("全站词", search_type="all")

        query = Query.objects.get(query_string="全站词")
        self.assertEqual(
            QueryDailyHits.objects.get(query=query, date=self.today).hits,
            1,
        )

    def test_dashboard_top_queries_aggregates_remaining_hits_as_other(self):
        self._query_with_daily_hits("第一词条", [5])
        self._query_with_daily_hits("第二词条", [3])
        self._query_with_daily_hits("第三词条", [2])

        payload = SearchAnalytics.build_dashboard(range_key="week", top_n=2)

        self.assertEqual(payload["summary"]["total_searches"], 10)
        self.assertEqual(payload["top_queries"][-1]["query"], "其他")
        self.assertEqual(payload["top_queries"][-1]["hits"], 2)
        self.assertTrue(payload["top_queries"][-1]["is_other"])
        self.assertEqual(
            [row["is_other"] for row in payload["top_queries"][:2]], [False, False]
        )

    def test_dashboard_top_n_is_clamped_to_ten(self):
        for index in range(12):
            self._query_with_daily_hits(f"top-{index}", [1])

        payload = SearchAnalytics.build_dashboard(range_key="week", top_n=99)

        self.assertEqual(
            len([row for row in payload["top_queries"] if not row["is_other"]]), 10
        )
        self.assertTrue(payload["top_queries"][-1]["is_other"])

    def test_search_analytics_registered_in_reports_menu_and_hides_search_terms(self):
        """测试搜索分析已归入报告菜单，且原生的 search-terms 菜单项已被安全过滤隐藏。"""
        from wagtail.admin.menu import reports_menu, admin_menu

        request = RequestFactory().get("/admin/")
        request.user = type(
            "Staff",
            (),
            {"is_authenticated": True, "is_active": True, "is_staff": True, "is_superuser": True, "has_perm": lambda self, perm: True},
        )()

        report_items = reports_menu.menu_items_for_request(request)
        report_names = [item.name for item in report_items]

        self.assertIn("search-analytics", report_names)
        self.assertNotIn("search-terms", report_names)

        admin_items = admin_menu.menu_items_for_request(request)
        admin_names = [item.name for item in admin_items]
        self.assertNotIn("search-analytics", admin_names)
