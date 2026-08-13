"""WP7A：验证共享 Pages 流和联邦结果骨架。"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from search.cache import SearchCache
from search.core import perform_search
from search.services.federated_query import (
	FederatedSearchResults,
	RRF_K,
	build_federated_search_results,
)
from search.services.pages_query import build_public_pages_queryset


class FederatedResultTests(SimpleTestCase):
	def test_rrf_merges_sources_and_deduplicates_by_page_id(self):
		blog_page = SimpleNamespace(pk=10, first_published_at=None)
		page_one = SimpleNamespace(pk=20, first_published_at=None)
		page_two = SimpleNamespace(pk=21, first_published_at=None)
		results = FederatedSearchResults(
			MagicMock(**{"__getitem__.return_value": [blog_page], "count.return_value": 1}),
			MagicMock(**{"__getitem__.return_value": [page_one, page_two], "count.return_value": 2}),
		)

		items = results[:3]

		self.assertEqual([item.pk for item in items], [10, 20, 21])
		self.assertEqual(results.count(), 3)
		self.assertEqual(1 / (RRF_K + 1), 1 / 61)

	def test_date_order_uses_first_published_at(self):
		old_page = SimpleNamespace(pk=20, first_published_at=SimpleNamespace(toordinal=lambda: 10))
		new_page = SimpleNamespace(pk=21, first_published_at=SimpleNamespace(toordinal=lambda: 20))
		results = FederatedSearchResults(
			MagicMock(**{"__getitem__.return_value": [old_page], "count.return_value": 1}),
			MagicMock(**{"__getitem__.return_value": [new_page], "count.return_value": 1}),
			order_by="-date",
		)

		self.assertEqual([item.pk for item in results[:2]], [21, 20])

	def test_date_key_prefers_blog_date_and_falls_back_to_first_published_at(self):
		from search.services.federated_query import _date_key

		blog_page = SimpleNamespace(
			pk=30,
			date=SimpleNamespace(toordinal=lambda: 30),
			first_published_at=SimpleNamespace(toordinal=lambda: 10),
		)
		page = SimpleNamespace(pk=31, first_published_at=SimpleNamespace(toordinal=lambda: 20))

		self.assertEqual(_date_key(blog_page)[1], 30)
		self.assertEqual(_date_key(page)[1], 20)


class SharedPagesQueryTests(SimpleTestCase):
	@patch("search.services.pages_query.BlogPage.objects.values")
	@patch("search.services.pages_query.Page.objects")
	def test_pages_queryset_excludes_blog_and_uses_first_published_at(self, page_objects, blog_values):
		public_pages = page_objects.live.return_value.public.return_value
		filtered_pages = public_pages.exclude.return_value
		filtered_pages.filter.side_effect = lambda **kwargs: filtered_pages
		filtered_pages.order_by.return_value = "ordered"

		result = build_public_pages_queryset("2026-01-01", "2026-12-31", "-date")

		page_objects.live.assert_called_once_with()
		public_pages.exclude.assert_called_once_with(id__in=blog_values.return_value)
		self.assertEqual(
			filtered_pages.filter.call_args_list,
			[
				(( ), {"first_published_at__gte": "2026-01-01"}),
				(( ), {"first_published_at__lte": "2026-12-31"}),
			],
		)


class FederatedRoutingTests(SimpleTestCase):
	def test_all_uses_federated_builder(self):
		federated_results = object()
		with (
			patch("search.core.build_federated_search_results", return_value=federated_results) as builder,
			patch("search.core.Query.get"),
		):
			result = perform_search("django", "all", start_date="2026-01-01")

		self.assertIs(result, federated_results)
		builder.assert_called_once()

	def test_federated_cache_namespace_is_stable(self):
		self.assertEqual(SearchCache.get_implementation_namespace("all"), "federated-all")


class FederatedBuilderTests(SimpleTestCase):
	def test_builder_does_not_return_partial_results(self):
		with (
			patch("search.services.federated_query.build_content_search_results", side_effect=RuntimeError("down")),
			self.assertRaises(Exception),
		):
			build_federated_search_results("django")


class LegacyBlogDocumentCommandTests(SimpleTestCase):
	@override_settings(WAGTAILSEARCH_BACKENDS={"default": {"BACKEND": "test"}})
	def test_cleanup_is_dry_run_by_default(self):
		with patch("search.management.commands.search_remove_legacy_blog_documents.get_search_backend") as get_backend:
			client = get_backend.return_value.es
			client.count.return_value = {"count": 3}
			index = get_backend.return_value.get_index_for_model.return_value
			index.name = "wagtailblog-test-page-v001"
			from django.core.management import call_command
			from io import StringIO

			output = StringIO()
			call_command("search_remove_legacy_blog_documents", stdout=output)

			client.delete_by_query.assert_not_called()
			self.assertIn('"matched_count": 3', output.getvalue())

	@override_settings(WAGTAILSEARCH_BACKENDS={"default": {"BACKEND": "test"}})
	def test_cleanup_confirm_requires_backup_reference_in_production(self):
		from django.core.management import call_command
		from django.core.management.base import CommandError

		with patch("search.management.commands.search_remove_legacy_blog_documents.get_search_backend"):
			with patch.dict("os.environ", {"WAGTAILBLOG_ENV": "production"}):
				with self.assertRaisesMessage(CommandError, "--backup-reference"):
					call_command("search_remove_legacy_blog_documents", "--confirm")
