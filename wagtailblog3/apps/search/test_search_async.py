import json
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils.translation import override

from search.views import (
    SEARCH_RESULTS_PER_PAGE,
    get_search_canonical_url,
    get_search_results_context,
    search,
    search_results_api,
)


class SearchResultsContextTests(SimpleTestCase):
    def test_builds_paginated_context_from_normalized_parameters(self):
        results = list(range(1, 24))

        with patch("search.views.perform_search", return_value=results) as perform_search:
            context = get_search_results_context(
                {
                    "query": "  Django  ",
                    "type": "blog",
                    "start_date": "2026-01-01",
                    "end_date": "2026-12-31",
                    "order_by": "-date",
                    "page": "2",
                }
            )

        perform_search.assert_called_once_with(
            "Django",
            "blog",
            start_date="2026-01-01",
            end_date="2026-12-31",
            order_by="-date",
        )
        self.assertEqual(context["search_results"].number, 2)
        self.assertEqual(list(context["search_results"].object_list), results[20:])
        self.assertEqual(context["search_results"].paginator.count, 23)
        self.assertEqual(context["search_type"], "blog")

    def test_empty_query_keeps_a_stable_welcome_context(self):
        with patch("search.views.perform_search") as perform_search:
            context = get_search_results_context({"type": "all"})

        perform_search.assert_not_called()
        self.assertEqual(context["search_query"], "")
        self.assertEqual(context["search_results"].number, 1)
        self.assertEqual(context["search_results"].paginator.count, 0)

    def test_canonical_url_preserves_active_filters_and_page(self):
        with patch("search.views.perform_search", return_value=list(range(23))):
            context = get_search_results_context(
                {
                    "query": "Django",
                    "type": "blog",
                    "start_date": "2026-01-01",
                    "order_by": "-date",
                    "page": "2",
                }
            )

        with override("zh-hans"):
            expected_url = (
                f'{reverse("search:search")}?query=Django&type=blog'
                "&start_date=2026-01-01&order_by=-date&page=2"
            )
            self.assertEqual(get_search_canonical_url(context), expected_url)

    def test_fragment_template_renders_the_welcome_state_without_the_page_shell(self):
        context = get_search_results_context({"type": "all"})

        html = render_to_string("search/partials/_search_results.html", context)

        self.assertIn('class="search-results-fragment"', html)
        self.assertIn("输入关键词开始搜索", html)
        self.assertNotIn('class="search-form"', html)


class SearchResultsApiTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_returns_server_rendered_fragment_and_pagination_metadata(self):
        request = self.factory.get(
            "/zh-hans/search/results/",
            {"query": "Django", "type": "blog", "page": "2"},
        )

        with (
            patch("search.views.perform_search", return_value=list(range(23))),
            patch(
                "search.views.render_to_string",
                return_value="<section>search results</section>",
            ) as render_fragment,
        ):
            response = search_results_api(request)

        payload = json.loads(response.content)
        fragment_context = render_fragment.call_args.args[1]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["query"], "Django")
        self.assertEqual(payload["data"]["result_count"], 23)
        self.assertEqual(payload["data"]["html"], "<section>search results</section>")
        self.assertEqual(fragment_context["search_results"].number, 2)
        self.assertEqual(
            payload["data"]["pagination"],
            {
                "page": 2,
                "page_size": SEARCH_RESULTS_PER_PAGE,
                "total_pages": 2,
                "has_previous": True,
                "has_next": False,
            },
        )
        self.assertEqual(payload["data"]["filters"]["type"], "blog")
        self.assertTrue(payload["data"]["canonical_url"].endswith("?query=Django&type=blog&page=2"))

    def test_rejects_non_get_requests_as_structured_json(self):
        response = search_results_api(self.factory.post("/zh-hans/search/results/"))

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    def test_returns_a_safe_error_payload_when_search_fails(self):
        request = self.factory.get("/zh-hans/search/results/", {"query": "Django"})

        with (
            patch("search.views.perform_search", side_effect=RuntimeError("unavailable")),
            patch("search.views.logger.error"),
        ):
            response = search_results_api(request)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(payload["error"]["code"], "search_unavailable")
        self.assertEqual(payload["error"]["message"], "搜索结果暂时无法加载，请稍后重试。")

    def test_preserves_the_existing_raw_ajax_search_contract(self):
        request = self.factory.get(
            "/zh-hans/search/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        response = search(request)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            payload,
            {
                "query": "",
                "results": [],
                "has_next": False,
                "has_previous": False,
                "total_count": 0,
                "current_page": 1,
                "total_pages": 1,
                "search_type": "all",
                "start_date": "",
                "end_date": "",
                "order_by": "",
            },
        )
