import json
from datetime import date
from unittest.mock import MagicMock, call, patch

from django.core.paginator import Paginator
from django.test import RequestFactory, SimpleTestCase

from blog.models import (
    BLOG_INDEX_ITEMS_PER_PAGE,
    BlogIndexPage,
    _normalise_blog_index_date,
)
from blog.views import blog_index_results_api, get_blog_index_canonical_url


class SpecificList(list):
    """List-shaped test queryset that preserves Wagtail's specific() call."""

    def __getitem__(self, key):
        value = super().__getitem__(key)
        return type(self)(value) if isinstance(key, slice) else value

    def specific(self):
        return self


class BlogIndexListingContextTests(SimpleTestCase):
    def setUp(self):
        self.page = MagicMock()
        self.children = MagicMock()
        self.queryset = MagicMock()
        self.children.live.return_value.public.return_value.annotate.return_value = (
            self.queryset
        )
        self.queryset.filter.return_value = self.queryset

    def build_context(self, params, item_count=45):
        self.queryset.order_by.return_value = SpecificList(
            [MagicMock(pk=index) for index in range(1, item_count + 1)]
        )
        self.page.get_children = MagicMock(return_value=self.children)

        with (
            patch("blog.models.Subquery"),
            patch("blog.models.Coalesce"),
            patch("blog.models.Lower"),
            patch("blog.models.BlogPage.objects"),
            patch("blog.models.BlogIndexPage.objects"),
            patch("blog.models.BlogTagIndexPage.objects") as tag_manager,
        ):
            tag_manager.live.return_value.public.return_value.first.return_value = None
            return BlogIndexPage.get_listing_context(self.page, params)

    def test_uses_live_public_children_stable_sorting_and_twenty_item_pages(self):
        context = self.build_context({"page": "2"})

        self.children.live.assert_called_once_with()
        self.children.live.return_value.public.assert_called_once_with()
        self.queryset.order_by.assert_called_once_with(
            "-sort_date", "sort_title", "pk"
        )
        self.assertEqual(context["page_obj"].number, 2)
        self.assertEqual(len(context["blog_pages"]), BLOG_INDEX_ITEMS_PER_PAGE)
        self.assertEqual(context["total_results"], 45)
        self.assertFalse(context["has_active_filters"])

    def test_strips_search_and_applies_valid_dates_and_sorting(self):
        context = self.build_context(
            {
                "search": "  Django  ",
                "start_date": "2025-01-02",
                "end_date": "2025-12-31",
                "sort_primary": "title_desc",
                "sort_secondary": "date_asc",
            },
            item_count=2,
        )

        self.queryset.filter.assert_has_calls(
            [
                call(title__icontains="Django"),
                call(sort_date__gte=date(2025, 1, 2)),
                call(sort_date__lte=date(2025, 12, 31)),
            ]
        )
        self.queryset.order_by.assert_called_once_with(
            "-sort_title", "sort_date", "pk"
        )
        self.assertEqual(context["search_query"], "Django")
        self.assertEqual(context["sort_secondary"], "date_asc")
        self.assertTrue(context["has_active_filters"])

    def test_invalid_dates_sorts_and_page_are_normalized(self):
        context = self.build_context(
            {
                "start_date": "2025-99-99",
                "end_date": "not-a-date",
                "sort_primary": "drop-table",
                "sort_secondary": "also-invalid",
                "page": "not-a-page",
            },
            item_count=3,
        )

        self.queryset.filter.assert_not_called()
        self.queryset.order_by.assert_called_once_with(
            "-sort_date", "sort_title", "pk"
        )
        self.assertEqual(context["start_date"], "")
        self.assertEqual(context["end_date"], "")
        self.assertEqual(context["sort_primary"], "date_desc")
        self.assertEqual(context["sort_secondary"], "title_asc")
        self.assertEqual(context["page_obj"].number, 1)
        self.assertFalse(context["has_active_filters"])

    def test_out_of_range_page_falls_back_to_last_page(self):
        context = self.build_context({"page": "999"}, item_count=45)

        self.assertEqual(context["page_obj"].number, 3)
        self.assertEqual(len(context["blog_pages"]), 5)

    def test_date_normalizer_rejects_well_formed_but_impossible_date(self):
        self.assertEqual(_normalise_blog_index_date("2025-02-30"), ("", None))


class BlogIndexResultsApiTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.page = MagicMock(spec=BlogIndexPage)
        self.page.pk = 368
        self.page.url = "/zh-hans/python/data/"
        self.page.get_url.return_value = "http://example.test/zh-hans/python/data/"

    def make_context(self):
        paginator = Paginator(list(range(25)), BLOG_INDEX_ITEMS_PER_PAGE)
        page_obj = paginator.get_page(2)
        return {
            "blog_pages": page_obj.object_list,
            "search_query": "Python",
            "start_date": "2025-01-01",
            "end_date": "",
            "sort_primary": "title_asc",
            "sort_secondary": "date_desc",
            "secondary_sort_options": (("date_desc", "时间"),),
            "page_obj": page_obj,
            "total_results": paginator.count,
            "has_active_filters": True,
            "blog_tag_index_page": None,
        }

    def test_returns_server_rendered_fragment_metadata_and_same_origin_url(self):
        request = self.factory.get(
            "/zh-hans/blog/api/index-pages/368/results/",
            {
                "search": "Python",
                "start_date": "2025-01-01",
                "sort_primary": "title_asc",
                "sort_secondary": "date_desc",
                "page": "2",
            },
        )
        context = self.make_context()
        self.page.get_listing_context.return_value = context

        with (
            patch("blog.views.BlogIndexPage.objects") as page_manager,
            patch(
                "blog.views.render_to_string", return_value="<section>results</section>"
            ) as render_fragment,
        ):
            page_manager.live.return_value.public.return_value.filter.return_value.first.return_value = (
                self.page
            )
            response = blog_index_results_api(request, self.page.pk)

        payload = json.loads(response.content)
        page_manager.live.assert_called_once_with()
        page_manager.live.return_value.public.assert_called_once_with()
        page_manager.live.return_value.public.return_value.filter.assert_called_once_with(
            pk=self.page.pk
        )
        self.page.get_listing_context.assert_called_once_with(request.GET)
        render_fragment.assert_called_once_with(
            "blog/partials/_blog_index_results.html",
            {"page": self.page, **context},
            request=request,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["result_count"], 25)
        self.assertEqual(payload["data"]["html"], "<section>results</section>")
        self.assertEqual(
            payload["data"]["canonical_url"],
            "/zh-hans/python/data/?search=Python&start_date=2025-01-01"
            "&sort_primary=title_asc&sort_secondary=date_desc&page=2",
        )
        self.assertEqual(
            payload["data"]["pagination"],
            {
                "page": 2,
                "page_size": BLOG_INDEX_ITEMS_PER_PAGE,
                "total_pages": 2,
                "has_previous": True,
                "has_next": False,
            },
        )

    def test_returns_structured_json_404_for_unknown_or_non_public_page(self):
        request = self.factory.get(
            "/zh-hans/blog/api/index-pages/999/results/"
        )

        with patch("blog.views.BlogIndexPage.objects") as page_manager:
            page_manager.live.return_value.public.return_value.filter.return_value.first.return_value = (
                None
            )
            response = blog_index_results_api(request, 999)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(payload["error"]["code"], "blog_index_not_found")

    def test_rejects_non_get_requests_as_json(self):
        request = self.factory.post(
            "/zh-hans/blog/api/index-pages/368/results/"
        )

        response = blog_index_results_api(request, self.page.pk)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    def test_default_state_canonical_url_omits_query_string(self):
        context = self.make_context()
        context.update(
            {
                "search_query": "",
                "start_date": "",
                "sort_primary": "date_desc",
                "sort_secondary": "title_asc",
                "page_obj": Paginator([1], BLOG_INDEX_ITEMS_PER_PAGE).get_page(1),
            }
        )

        url = get_blog_index_canonical_url(
            page=self.page,
            context=context,
            request=self.factory.get("/api/"),
        )

        self.assertEqual(url, "/zh-hans/python/data/")
