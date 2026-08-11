import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core import signing
from django.test import RequestFactory, SimpleTestCase, override_settings

from search.services.content_query import (
    ContentSearchCursorPage,
    ContentSearchResults,
    query_content_search_page,
    reverse_content_search_sort,
)
from search.services.cursor import (
    ContentSearchCursor,
    ContentSearchCursorError,
    build_cursor_query_hash,
    decode_content_search_cursor,
    encode_content_search_cursor,
)
from search.services.suggestions import (
    get_popular_query_suggestions,
    get_public_search_suggestions,
    get_public_title_suggestions,
)
from search.views import SEARCH_RESULTS_PER_PAGE, get_search_results_context, search_results_api


@override_settings(CONTENT_SEARCH_CURSOR_MAX_AGE_SECONDS=900)
class ContentSearchCursorTests(SimpleTestCase):
    def setUp(self):
        self.query_hash = build_cursor_query_hash(
            "Django", "blog", "2026-01-01", "", "-date", "zh-hans"
        )

    def test_signed_cursor_round_trip(self):
        token = encode_content_search_cursor(
            ContentSearchCursor("next", ("2026-01-03", 38), "pit-1"),
            self.query_hash,
        )

        cursor = decode_content_search_cursor(token, self.query_hash)

        self.assertEqual(cursor.direction, "next")
        self.assertEqual(cursor.sort, ("2026-01-03", 38))
        self.assertEqual(cursor.pit_id, "pit-1")

    def test_rejects_tampering_and_cross_query_reuse(self):
        token = encode_content_search_cursor(
            ContentSearchCursor("next", (1.5, 38)), self.query_hash
        )

        with self.assertRaises(ContentSearchCursorError) as tampered:
            decode_content_search_cursor(f"{token}x", self.query_hash)
        self.assertEqual(tampered.exception.code, "cursor_invalid")

        with self.assertRaises(ContentSearchCursorError) as mismatch:
            decode_content_search_cursor(token, "another-query-hash")
        self.assertEqual(mismatch.exception.code, "cursor_query_mismatch")

    def test_classifies_expired_cursor(self):
        with (
            patch("search.services.cursor.signing.loads", side_effect=signing.SignatureExpired),
            self.assertRaises(ContentSearchCursorError) as raised,
        ):
            decode_content_search_cursor("expired", self.query_hash)
        self.assertEqual(raised.exception.code, "cursor_expired")


class ContentSearchAfterTests(SimpleTestCase):
    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_READ_ALIAS="wagtailblog-test-content-read",
        CONTENT_SEARCH_PIT_KEEP_ALIVE="1m",
    )
    def test_search_after_uses_stable_sort_without_offset(self):
        client = Mock()
        client.search.return_value = {
            "pit_id": "pit-2",
            "hits": {
                "total": {"value": 10001, "relation": "gte"},
                "hits": [
                    {"_source": {"page_id": 39}, "sort": [8.5, 39]},
                    {"_source": {"page_id": 40}, "sort": [8.5, 40]},
                ],
            },
        }
        target = SimpleNamespace(connection_name="default")
        with (
            patch("search.services.content_query.get_content_search_client", return_value=client),
            patch(
                "search.services.content_query._public_page_ids_in_order",
                side_effect=lambda page_ids: tuple(page_ids),
            ),
        ):
            result = query_content_search_page(
                target,
                "Django",
                size=2,
                search_after=(9.0, 38),
                pit_id="pit-1",
            )

        request = client.search.call_args.kwargs
        self.assertNotIn("index", request)
        self.assertNotIn("from_", request)
        self.assertEqual(request["search_after"], [9.0, 38])
        self.assertEqual(request["sort"], [{"_score": "desc"}, {"page_id": "asc"}])
        self.assertEqual(request["pit"], {"id": "pit-1", "keep_alive": "1m"})
        self.assertEqual(result.sort_values, ((8.5, 39), (8.5, 40)))
        self.assertEqual(result.pit_id, "pit-2")

    def test_previous_direction_reverses_every_sort_field(self):
        self.assertEqual(
            reverse_content_search_sort(
                [{"date": {"order": "desc", "missing": "_last"}}, {"page_id": "asc"}]
            ),
            [{"date": {"order": "asc", "missing": "_last"}}, {"page_id": "desc"}],
        )


class ContentSearchCursorViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(
        CONTENT_SEARCH_QUERY_ENABLED=True,
        CONTENT_SEARCH_CURSOR_ENABLED=True,
    )
    def test_context_uses_cursor_page_for_independent_blog_search(self):
        results = ContentSearchResults(SimpleNamespace(), "Django")
        cursor_page = ContentSearchCursorPage([1, 2], 10001, "prev-token", "next-token")
        with (
            patch("search.views.perform_search", return_value=results),
            patch.object(results, "cursor_page", return_value=cursor_page) as fetch,
        ):
            context = get_search_results_context(
                {"query": "Django", "type": "blog", "cursor": "signed-token"},
                locale="zh-hans",
            )

        fetch.assert_called_once_with(
            "signed-token",
            SEARCH_RESULTS_PER_PAGE,
            search_type="blog",
            locale="zh-hans",
        )
        self.assertTrue(context["cursor_mode"])
        self.assertEqual(context["search_results"].paginator.count, 10001)

    @override_settings(
        CONTENT_SEARCH_QUERY_ENABLED=True,
        CONTENT_SEARCH_CURSOR_ENABLED=True,
    )
    def test_fragment_api_returns_cursor_metadata(self):
        results = ContentSearchResults(SimpleNamespace(), "Django")
        cursor_page = ContentSearchCursorPage([1], 10001, None, "next-token")
        request = self.factory.get(
            "/zh-hans/search/results/", {"query": "Django", "type": "blog"}
        )
        request.LANGUAGE_CODE = "zh-hans"
        with (
            patch("search.views.perform_search", return_value=results),
            patch.object(results, "cursor_page", return_value=cursor_page),
            patch(
                "search.views.render_to_string", return_value="<section></section>"
            ) as render_fragment,
        ):
            response = search_results_api(request)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            payload["data"]["pagination"],
            {
                "mode": "cursor",
                "page_size": SEARCH_RESULTS_PER_PAGE,
                "has_previous": False,
                "has_next": True,
                "previous_cursor": None,
                "next_cursor": "next-token",
            },
        )
        self.assertIn("cursor=next-token", render_fragment.call_args.args[1]["next_cursor_url"])

    @override_settings(CONTENT_SEARCH_CURSOR_ENABLED=False)
    def test_disabled_flag_keeps_numbered_page_contract(self):
        with patch("search.views.perform_search", return_value=list(range(23))):
            context = get_search_results_context(
                {"query": "Django", "type": "blog", "page": "2"}
            )

        self.assertFalse(context["cursor_mode"])
        self.assertEqual(context["search_results"].number, 2)


class ContentSearchSuggestionTests(SimpleTestCase):
    @override_settings(
        SEARCH_SUGGESTIONS_V2_ENABLED=True,
        SEARCH_POPULAR_SUGGESTIONS_ENABLED=True,
        SEARCH_TITLE_SUGGESTIONS_ENABLED=False,
    )
    def test_v2_suggestions_use_the_separate_popular_channel(self):
        item = SimpleNamespace(query_string="django", total_hits_count=8)
        with patch(
            "search.services.suggestions.Query.objects.filter"
        ) as query_filter:
            query_filter.return_value.annotate.return_value.filter.return_value.order_by.return_value.__getitem__.return_value = [item]
            suggestions = get_public_search_suggestions("django")

        self.assertEqual(suggestions, [{"query": "django", "hits": 8, "source": "popular"}])

    def test_popular_channel_drops_markup_and_control_text(self):
        items = [
            SimpleNamespace(query_string="<script>", total_hits_count=20),
            SimpleNamespace(query_string="safe query", total_hits_count=2),
        ]
        with patch(
            "search.services.suggestions.Query.objects.filter"
        ) as query_filter:
            query_filter.return_value.annotate.return_value.filter.return_value.order_by.return_value.__getitem__.return_value = items
            suggestions = get_popular_query_suggestions("query")

        self.assertEqual(suggestions, [{"query": "safe query", "hits": 2, "source": "popular"}])

    @override_settings(
        CONTENT_SEARCH_TITLE_SUGGESTIONS_READ_ALIAS="wagtailblog-test-content-title-read",
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
    )
    def test_title_channel_filters_results_against_public_pages(self):
        client = Mock()
        client.search.return_value = {
            "hits": {
                "hits": [
                    {"_source": {"page_id": 38}},
                    {"_source": {"page_id": 487}},
                ]
            }
        }
        target = SimpleNamespace(connection_name="default")
        public_page = SimpleNamespace(id=38, title="公开 Django")
        with (
            patch("search.services.suggestions.get_content_search_serving_target", return_value=target),
            patch("search.services.suggestions.get_content_search_client", return_value=client),
            patch("search.services.suggestions.BlogPage.objects.live") as live,
        ):
            live.return_value.public.return_value.filter.return_value = [public_page]
            suggestions = get_public_title_suggestions("django")

        self.assertEqual(
            suggestions,
            [{"query": "公开 Django", "page_id": 38, "source": "title"}],
        )
        request = client.search.call_args.kwargs
        self.assertEqual(request["index"], "wagtailblog-test-content-title-read")
        self.assertEqual(request["query"]["bool"]["filter"], [{"term": {"searchable": True}}])
