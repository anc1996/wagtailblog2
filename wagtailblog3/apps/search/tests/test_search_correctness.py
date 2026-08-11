"""WP1：验证公开边界、缓存版本和错误契约。"""

import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings
from wagtail.search.index import AutocompleteField
from wagtail.models import Page, PageViewRestriction

from blog.models import BlogPage
from search.api import search_api
from search.cache import SearchCache
from search.core import (
	HIGHLIGHT_END_TAG,
	HIGHLIGHT_START_TAG,
	BODY_HIGHLIGHT_MAX_ANALYZER_OFFSET,
	HighlightedSearchResults,
	SearchUnavailableError,
	_get_search_field_specs,
	_build_quality_query,
	_minimum_should_match,
	_safe_highlight_fragment,
	perform_search,
)
from search.views import SearchResultWindowError, get_search_results_context


class SearchPublicBoundaryTests(SimpleTestCase):
    """搜索入口必须先应用 Wagtail 公开 QuerySet。"""

    def test_relevance_search_runs_on_the_public_queryset_and_does_not_fake_a_fallback(self):
        query_set = MagicMock()
        query_set.search.return_value = ["result"]

        with (
            patch("search.core._build_base_qs", return_value=query_set),
            patch("search.core.Query.get") as get_query,
        ):
            result = perform_search("公开关键词", "blog")

        self.assertEqual(result, ["result"])
        query_set.search.assert_called_once_with(
            "公开关键词", operator="or", order_by_relevance=True
        )
        get_query.return_value.add_hit.assert_called_once_with()

    def test_search_backend_failure_is_not_converted_to_zero_results(self):
        query_set = MagicMock()
        query_set.search.side_effect = RuntimeError("ES unavailable")

        with (
            patch("search.core._build_base_qs", return_value=query_set),
            self.assertRaises(SearchUnavailableError),
        ):
            perform_search("公开关键词", "blog")

    def test_blog_page_does_not_duplicate_wagtail_core_autocomplete_field(self):
        core_count = sum(isinstance(field, AutocompleteField) for field in BlogPage.__mro__[1].search_fields)
        page_count = sum(isinstance(field, AutocompleteField) for field in BlogPage.search_fields)
        self.assertEqual(page_count, core_count)


class SearchPaginationAndCacheTests(SimpleTestCase):
    """固定分页上限和缓存代次，避免旧公开结果继续复用。"""

    def test_html_context_rejects_pages_after_the_search_window(self):
        with self.assertRaises(SearchResultWindowError):
            get_search_results_context({"query": "关键词", "page": "501"})

    def test_cache_key_contains_the_implementation_version(self):
        key = SearchCache.get_cache_key("关键词")
        self.assertIn("search:v3:", key)

    def test_cache_clear_deletes_only_the_current_search_namespace(self):
        with patch("search.cache.cache.delete_pattern") as delete_pattern:
            SearchCache.clear_search_cache()

        delete_pattern.assert_called_once_with("search:v3:*")

    def test_restriction_change_schedules_search_cache_invalidation_after_commit(self):
        from blog.signals import invalidate_feed_on_restriction_changed

        with (
            patch("blog.signals.BlogFeedInvalidationService.schedule_all"),
            patch("blog.signals.transaction.on_commit") as on_commit,
        ):
            invalidate_feed_on_restriction_changed(
                sender=PageViewRestriction,
                instance=MagicMock(),
            )

        callback = on_commit.call_args.args[0]
        with patch("search.cache.SearchCache.clear_search_cache") as clear_cache:
            callback()
        clear_cache.assert_called_once_with()


class SearchApiErrorContractTests(SimpleTestCase):
    """API 在搜索后端故障或超出窗口时返回可识别错误。"""

    def setUp(self):
        self.factory = RequestFactory()

    def test_search_api_returns_503_when_backend_is_unavailable(self):
        request = self.factory.get("/search/api/", {"q": "关键词"})
        with patch("search.api.perform_search", side_effect=SearchUnavailableError()):
            response = search_api(request).render()

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["error"]["code"], "search_unavailable")

    def test_search_api_rejects_offsets_after_ten_thousand_results(self):
        request = self.factory.get("/search/api/", {"q": "关键词", "page": "1001", "per_page": "10"})

        response = search_api(request).render()

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"]["code"], "result_window_exceeded")


class SearchHighlightSecurityTests(SimpleTestCase):
	"""高亮只允许服务端生成的标记，并且必须先完成公开页面回查。"""

	@override_settings(SEARCH_HIGHLIGHTS_ENABLED=False)
	def test_feature_flag_returns_wp1_public_queryset_search_without_highlight_backend(self):
		query_set = MagicMock()
		compiled_results = MagicMock()
		query_set.search.return_value = compiled_results

		with (
			patch("search.core._build_base_qs", return_value=query_set),
			patch("search.core.Query.get"),
			patch("search.core.get_search_backend") as get_search_backend,
		):
			result = perform_search("公开关键词", "blog")

		self.assertIs(result, compiled_results)
		get_search_backend.assert_not_called()

	def test_minimum_should_match_changes_with_query_length(self):
		self.assertEqual(_minimum_should_match("django"), "100%")
		self.assertEqual(_minimum_should_match("中文搜索"), "75%")
		self.assertEqual(_minimum_should_match("一二三四五六七"), "60%")

	def test_quality_query_contains_weighted_phrase_and_dynamic_threshold(self):
		query = _build_quality_query(
			"中文搜索",
			[("title", 10.0, "title"), ("body_text", 2.0, "body_text")],
		)

		self.assertEqual(query["bool"]["minimum_should_match"], 1)
		multi_match = query["bool"]["should"][-1]["multi_match"]
		self.assertEqual(multi_match["minimum_should_match"], "75%")
		self.assertIn("title^10", multi_match["fields"])
		self.assertIn("body_text^2", multi_match["fields"])
		self.assertEqual(
			query["bool"]["should"][0]["match_phrase"]["title"]["boost"],
			100.0,
		)

	def test_all_search_uses_blog_intro_and_body_fields(self):
		query_compiler = MagicMock()
		query_compiler.queryset.model = Page
		query_compiler.get_searchable_fields.return_value = []
		query_compiler.mapping_class.return_value.get_field_column_name.side_effect = (
			lambda field: field.field_name
		)

		field_specs = _get_search_field_specs(query_compiler, include_blog_fields=True)

		self.assertTrue({"title", "intro", "body_text"}.issubset({label for _, _, label in field_specs}))

	def test_highlight_fragment_escapes_html_and_keeps_only_mark(self):
		fragment = _safe_highlight_fragment(
			f'<script>alert("x")</script><p>命中 {HIGHLIGHT_START_TAG}关键词{HIGHLIGHT_END_TAG}</p>'
		)

		self.assertNotIn("<script", fragment)
		self.assertEqual(str(fragment), 'alert(&quot;x&quot;)命中 <mark>关键词</mark>')

	def test_highlight_result_rechecks_public_queryset_before_attaching_fragments(self):
		page = MagicMock(pk=7, title="安全标题")
		public_pages = MagicMock()
		public_pages.specific.return_value = [page]
		queryset = MagicMock()
		queryset.filter.return_value = public_pages
		backend = MagicMock(use_new_elasticsearch_api=True)
		backend.es.search.return_value = {
			"hits": {
				"hits": [
					{
						"fields": {"pk": ["7"]},
						"highlight": {
							"body_text": [
								f"正文 {HIGHLIGHT_START_TAG}关键词{HIGHLIGHT_END_TAG}",
								"第二段",
								"第三段",
								"第四段不应返回",
							],
						},
					}
				]
			}
		}
		results = HighlightedSearchResults(
			queryset=queryset,
			backend=backend,
			index_name="search-index",
			query={"match_all": {}},
			sort=None,
			field_specs=[("body_text", 2.0, "body_text")],
		)

		items = results[:1]

		queryset.filter.assert_called_once_with(pk__in=[7])
		self.assertEqual(items, [page])
		self.assertEqual(page.search_matched_field, "body_text")
		self.assertEqual(len(page.search_highlight_fragments), 3)
		self.assertIn("<mark>关键词</mark>", page.search_highlight_fragments[0])
		self.assertNotIn("第四段", "".join(page.search_highlight_fragments))
		body_highlight = backend.es.search.call_args.kwargs["highlight"]["fields"]["body_text"]
		self.assertEqual(body_highlight["max_analyzed_offset"], BODY_HIGHLIGHT_MAX_ANALYZER_OFFSET)

	def test_title_highlight_is_not_repeated_in_result_fragments(self):
		results = HighlightedSearchResults(
			queryset=MagicMock(),
			backend=MagicMock(),
			index_name="search-index",
			query={"match_all": {}},
			sort=None,
			field_specs=[("title", 10.0, "title"), ("intro", 5.0, "intro")],
		)

		matched_field, fragments, title_fragment = results._extract_highlights(
			{
				"highlight": {
					"title": [f"{HIGHLIGHT_START_TAG}标题{HIGHLIGHT_END_TAG}"],
					"intro": [f"简介 {HIGHLIGHT_START_TAG}命中{HIGHLIGHT_END_TAG}"],
				}
			}
		)

		self.assertEqual(matched_field, "title")
		self.assertEqual(str(title_fragment), "<mark>标题</mark>")
		self.assertEqual([str(fragment) for fragment in fragments], ["简介 <mark>命中</mark>"])
