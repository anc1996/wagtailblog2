"""搜索类型校验不能吞掉分页、游标和运行期故障。"""

import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.core.cache import cache

from search.api import search_api, search_suggestions_api
from search.cache import SearchCache
from search.core import SearchUnavailableError
from search.services.content_query import ContentSearchResults
from search.services.cursor import ContentSearchCursorError
from search.views import search, search_results_api


class SearchErrorBoundaryTests(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()

    @override_settings(CONTENT_SEARCH_CURSOR_ENABLED=False)
    def test_fragment_preserves_result_window_error(self) -> None:
        request = self.factory.get(
            "/search/results/", {"query": "关键词", "type": "pages", "page": "501"}
        )
        response = search_results_api(request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            json.loads(response.content)["error"]["code"], "result_window_exceeded"
        )

    def test_fragment_preserves_cursor_error(self) -> None:
        request = self.factory.get(
            "/search/results/", {"query": "关键词", "type": "guides", "cursor": "invalid"}
        )
        response = search_results_api(request)
        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "invalid_cursor")
        self.assertNotIn("invalid", payload["error"]["message"])

    @override_settings(CONTENT_SEARCH_CURSOR_ENABLED=False)
    def test_legacy_ajax_distinguishes_parameter_errors(self) -> None:
        cases = (
            ({"type": "unsupported"}, "invalid_search_type"),
            ({"type": "pages", "page": "501"}, "result_window_exceeded"),
            ({"type": "guides", "cursor": "invalid"}, "invalid_cursor"),
        )
        for parameters, expected_code in cases:
            with self.subTest(code=expected_code):
                request = self.factory.get(
                    "/search/",
                    {"query": "关键词", **parameters},
                    HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                )
                response = search(request)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(json.loads(response.content)["error"]["code"], expected_code)

    def test_unknown_type_does_not_echo_user_input(self) -> None:
        unknown_type = "<script>private-marker</script>"
        request = self.factory.get("/search/results/", {"type": unknown_type})
        response = search_results_api(request)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("private-marker", response.content.decode())


    def test_runtime_value_error_is_not_an_invalid_type(self) -> None:
        request = self.factory.get("/search/results/", {"query": "关键词", "type": "guides"})
        with (
            patch("search.views.perform_search", side_effect=ValueError("private-marker")),
            patch("search.views.logger.error"),
        ):
            response = search_results_api(request)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["error"]["code"], "search_unavailable")
        self.assertNotIn("private-marker", response.content.decode())

    @override_settings(CONTENT_SEARCH_CURSOR_ENABLED=True)
    def test_rest_api_handles_late_cursor_failure(self) -> None:
        results = MagicMock(spec=ContentSearchResults)
        results.cursor_page.side_effect = ContentSearchCursorError("cursor_query_mismatch")
        request = self.factory.get("/search/api/", {"q": "关键词", "type": "blog"})
        with patch("search.api.perform_search", return_value=results):
            response = search_api(request).render()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.content)["error"]["code"], "cursor_query_mismatch")

    @override_settings(CONTENT_SEARCH_CURSOR_ENABLED=False)
    def test_rest_api_handles_late_result_failure(self) -> None:
        results = MagicMock()
        results.count.side_effect = ValueError("private-marker")
        request = self.factory.get("/search/api/", {"q": "关键词", "type": "guides"})
        with (
            patch("search.api.SearchCache.get_cached_results", return_value=None),
            patch("search.api.perform_search", return_value=results),
            patch("search.api.logger.error"),
        ):
            response = search_api(request).render()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["error"]["code"], "search_unavailable")
        self.assertNotIn("private-marker", response.content.decode())

    @override_settings(CONTENT_SEARCH_CURSOR_ENABLED=False)
    def test_rest_api_contains_cache_backend_failure(self) -> None:
        request = self.factory.get(
            "/search/api/", {"q": "缓存故障边界", "type": "guides"}
        )
        with (
            patch("search.api.SearchCache.get_cached_results", side_effect=ValueError("private-marker")),
            patch("search.api.logger.error"),
        ):
            response = search_api(request).render()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content)["error"]["code"], "search_unavailable")
        self.assertNotIn("private-marker", response.content.decode())

    @override_settings(CONTENT_SEARCH_CURSOR_ENABLED=False)
    def test_rest_api_failure_logs_do_not_echo_backend_details(self) -> None:
        """错误响应和日志均不能回显后端异常携带的用户数据。"""
        for error in (
            ValueError("private-backend-marker"),
            SearchUnavailableError("private-backend-marker"),
        ):
            with self.subTest(error_type=type(error).__name__):
                request = self.factory.get(
                    "/search/api/", {"q": "日志安全边界", "type": "guides"}
                )
                with (
                    patch("search.api.SearchCache.get_cached_results", return_value=None),
                    patch("search.api.perform_search", side_effect=error),
                    self.assertLogs("search.api", level="ERROR") as captured,
                ):
                    response = search_api(request).render()
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("private-backend-marker", "\n".join(captured.output))
                self.assertNotIn("private-backend-marker", response.content.decode())

    def test_suggestions_failure_logs_do_not_echo_backend_details(self) -> None:
        request = self.factory.get("/search/suggestions/", {"q": "日志安全边界"})
        with (
            patch("search.api.get_search_suggestions", side_effect=ValueError("private-backend-marker")),
            self.assertLogs("search.api", level="ERROR") as captured,
        ):
            response = search_suggestions_api(request).render()
        self.assertEqual(json.loads(response.content), {"suggestions": []})
        self.assertNotIn("private-backend-marker", "\n".join(captured.output))


@override_settings(
    CACHES={"default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "search-permission-boundary",
    }}
)
class SearchLocalCacheBoundaryTests(SimpleTestCase):
    @override_settings(CONTENT_SEARCH_CURSOR_ENABLED=False)
    def test_permission_invalidation_during_search_cannot_repopulate_current_cache(self) -> None:
        """权限变更发生在查询途中时，旧结果不能写入新的公开缓存代次。"""
        results = MagicMock()
        results.count.return_value = 1
        results.__getitem__.return_value = [{"id": 103}]

        def invalidate_during_query(*args, **kwargs):
            SearchCache.clear_search_cache()
            return results

        request = RequestFactory().get(
            "/search/api/", {"q": "并发权限边界", "type": "guides"}
        )
        with (
            patch("search.api.perform_search", side_effect=invalidate_during_query),
            patch("search.api.format_search_results_for_api", side_effect=list),
        ):
            response = search_api(request).render()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(SearchCache.get_cached_results("并发权限边界", "guides"))

    @override_settings(CONTENT_SEARCH_CURSOR_ENABLED=False)
    def test_api_cache_does_not_mix_page_sizes(self) -> None:
        """同页码不同分页大小必须分别执行查询并返回正确结果数量。"""
        results = MagicMock()
        rows = [{"id": 104}, {"id": 105}, {"id": 106}]
        results.count.return_value = len(rows)
        results.__getitem__.side_effect = lambda requested_slice: rows[requested_slice]
        factory = RequestFactory()
        with (
            patch("search.api.perform_search", return_value=results),
            patch("search.api.format_search_results_for_api", side_effect=list),
        ):
            first = search_api(factory.get(
                "/search/api/", {"q": "分页缓存边界", "type": "guides", "per_page": 1}
            )).render()
            second = search_api(factory.get(
                "/search/api/", {"q": "分页缓存边界", "type": "guides", "per_page": 2}
            )).render()
        self.assertEqual(json.loads(first.content)["results"], [{"id": 104}])
        self.assertEqual(json.loads(second.content)["results"], [{"id": 104}, {"id": 105}])
        self.assertEqual(json.loads(second.content)["per_page"], 2)

    def test_invalidation_hides_old_results_without_clearing_other_caches(self) -> None:
        SearchCache.set_cached_results("权限边界", {"results": [{"id": 101}]}, "guides")
        cache.set("unrelated-permission-marker", "keep")
        SearchCache.clear_search_cache()
        self.assertIsNone(SearchCache.get_cached_results("权限边界", "guides"))
        self.assertEqual(cache.get("unrelated-permission-marker"), "keep")
        SearchCache.set_cached_results("权限边界", {"results": []}, "guides")
        self.assertEqual(SearchCache.get_cached_results("权限边界", "guides"), {"results": []})

    def test_generation_eviction_does_not_revive_old_public_results(self) -> None:
        SearchCache.set_cached_results("失效边界", {"results": [{"id": 102}]}, "guides")
        cache.delete("search:v5:generation")
        self.assertIsNone(SearchCache.get_cached_results("失效边界", "guides"))
