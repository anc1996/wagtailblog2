"""P-05 HTTP/UI L0：响应未知总量、partial 和动态类型，不连接任何服务。"""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings


class RegisteredTypesHTTPTests(SimpleTestCase):
    def test_v2_api_delegates_before_legacy_allowlist_and_cache_json(self):
        from search.api import search_api
        request = RequestFactory().get("/search/api/", {"q": "词语", "type": "recipes"})
        try:
            module = import_module("search.services.v2_query")
        except ModuleNotFoundError:
            self.fail("缺少 v2 HTTP 共用查询服务")
        payload = {
            "query": "词语", "results": [], "returned_count": 0,
            "total": None, "total_relation": "unknown", "has_more": None,
            "next_cursor": "opaque", "partial": True, "continuation_kind": "scan",
        }
        with override_settings(CONTENT_SEARCH_V2_ENABLED=True), patch.object(
            module, "search_request", return_value=payload
        ):
            response = search_api(request)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["total"])
        self.assertIsNone(response.data["has_more"])
        self.assertEqual(response.data["next_cursor"], "opaque")

    def test_v2_template_partial_empty_does_not_claim_no_results_or_exact_total(self):
        html = render_to_string("search/partials/_search_results.html", {
            "v2_mode": True, "search_query": "词语", "search_results": [],
            "total": None, "returned_count": 0, "has_more": None,
            "partial": True, "continuation_kind": "scan",
            "next_cursor_url": "/search/?cursor=opaque",
        })
        self.assertIn("继续扫描", html)
        self.assertNotIn("没有找到相关内容", html)
        self.assertNotIn("共找到", html)
        self.assertIn('rel="next"', html)

    def test_unsafe_legacy_cache_cannot_bypass_current_scope(self):
        from search.api import search_api
        from search.cache import SearchCache
        request = RequestFactory().get("/search/api/", {"q": "词语"})
        with override_settings(CONTENT_SEARCH_V2_ENABLED=False), patch.object(
            SearchCache, "get_cached_results", return_value={"results": [{"title": "已撤权秘密"}]}
        ):
            response = search_api(request)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("已撤权秘密", str(response.data))
