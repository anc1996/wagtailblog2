import json
from datetime import date
from unittest.mock import MagicMock, call, patch

from django.core.paginator import Paginator
from django.http import Http404
from django.test import Client, RequestFactory, SimpleTestCase

from archive.services.listing import (
    ARCHIVE_PAGE_SIZE,
    get_archive_canonical_url,
    get_archive_listing_context,
)
from archive.views import month_archive, month_archive_results_api, year_archive_results_api


class ArchiveListingContextTests(SimpleTestCase):
    def build_context(self, params, *, year=2025, month=None, item_count=25):
        articles = [MagicMock(pk=index) for index in range(1, item_count + 1)]

        with (
            patch("archive.services.listing.BlogPage.objects") as page_manager,
            patch("archive.services.listing.BlogTagIndexPage.objects") as tag_manager,
        ):
            queryset = (
                page_manager.live.return_value.public.return_value.select_related.return_value.prefetch_related.return_value
            )
            queryset.filter.return_value = queryset
            queryset.order_by.return_value = articles
            tag_manager.live.return_value.public.return_value.first.return_value = None

            context = get_archive_listing_context(
                year=year,
                month=month,
                query_params=params,
            )

        return context, page_manager, queryset

    def test_year_listing_uses_live_public_pages_and_normalized_search(self):
        context, page_manager, queryset = self.build_context(
            {"search": "  Python  ", "page": "2"}
        )

        page_manager.live.assert_called_once_with()
        page_manager.live.return_value.public.assert_called_once_with()
        page_manager.live.return_value.public.return_value.select_related.assert_called_once_with(
            "featured_image"
        )
        page_manager.live.return_value.public.return_value.select_related.return_value.prefetch_related.assert_called_once_with(
            "tags"
        )
        queryset.filter.assert_has_calls(
            [call(date__year=2025), call(title__icontains="Python")]
        )
        queryset.order_by.assert_called_once_with("-date", "-pk")
        self.assertEqual(context["archive_scope"], "year")
        self.assertEqual(context["search_query"], "Python")
        self.assertEqual(context["pages"].number, 2)
        self.assertEqual(context["total_count"], 25)

    def test_month_listing_uses_a_left_closed_right_open_date_range(self):
        context, _, queryset = self.build_context(
            {"search": "git"},
            month=8,
            item_count=8,
        )

        queryset.filter.assert_has_calls(
            [
                call(date__gte=date(2025, 8, 1), date__lt=date(2025, 9, 1)),
                call(title__icontains="git"),
            ]
        )
        self.assertEqual(context["archive_scope"], "month")
        self.assertEqual(context["month_name"], "August")
        self.assertEqual(context["pages"].number, 1)
        self.assertEqual(context["archive_url"], "/zh-hans/archive/year/2025/month/8/")

    def test_invalid_and_out_of_range_pages_follow_existing_fallbacks(self):
        invalid_context, _, _ = self.build_context({"page": "not-a-page"}, item_count=25)
        out_of_range_context, _, _ = self.build_context({"page": "99"}, item_count=25)

        self.assertEqual(invalid_context["pages"].number, 1)
        self.assertEqual(out_of_range_context["pages"].number, 3)

    def test_invalid_month_is_rejected_before_querying_pages(self):
        with patch("archive.services.listing.BlogPage.objects") as page_manager:
            with self.assertRaises(Http404):
                get_archive_listing_context(year=2025, month=13, query_params={})

        page_manager.live.assert_not_called()

    def test_canonical_url_omits_empty_search_and_first_page(self):
        page = Paginator(list(range(25)), ARCHIVE_PAGE_SIZE).get_page(2)
        context = {
            "archive_url": "/zh-hans/archive/year/2025/",
            "search_query": "Python",
            "pages": page,
        }

        self.assertEqual(
            get_archive_canonical_url(context=context),
            "/zh-hans/archive/year/2025/?search=Python&page=2",
        )

        context["search_query"] = ""
        context["pages"] = Paginator([1], ARCHIVE_PAGE_SIZE).get_page(1)
        self.assertEqual(
            get_archive_canonical_url(context=context),
            "/zh-hans/archive/year/2025/",
        )


class ArchiveResultsApiTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.csrf_client = Client(enforce_csrf_checks=True)

    def make_context(self, *, month=None):
        page = Paginator(list(range(25)), ARCHIVE_PAGE_SIZE).get_page(2)
        archive_url = (
            "/zh-hans/archive/year/2025/month/8/"
            if month is not None
            else "/zh-hans/archive/year/2025/"
        )
        return {
            "archive_scope": "month" if month is not None else "year",
            "year": 2025,
            "month": month,
            "search_query": "Python",
            "total_count": page.paginator.count,
            "pages": page,
            "archive_url": archive_url,
        }

    def test_year_api_returns_fragment_metadata_and_normalized_url(self):
        request = self.factory.get(
            "/zh-hans/archive/api/year/2025/results/",
            {"search": "Python", "page": "2"},
        )
        context = self.make_context()

        with (
            patch("archive.views.get_archive_listing_context", return_value=context) as get_context,
            patch("archive.views.render_to_string", return_value="<section>results</section>") as render_fragment,
        ):
            response = year_archive_results_api(request, 2025)

        payload = json.loads(response.content)
        get_context.assert_called_once_with(year=2025, month=None, query_params=request.GET)
        render_fragment.assert_called_once_with(
            "archive/partials/_archive_results.html",
            context,
            request=request,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["result_count"], 25)
        self.assertEqual(payload["data"]["html"], "<section>results</section>")
        self.assertEqual(
            payload["data"]["canonical_url"],
            "/zh-hans/archive/year/2025/?search=Python&page=2",
        )
        self.assertEqual(
            payload["data"]["pagination"],
            {
                "page": 2,
                "page_size": ARCHIVE_PAGE_SIZE,
                "total_pages": 3,
                "has_previous": True,
                "has_next": True,
            },
        )

    def test_month_api_passes_its_path_scope_to_the_shared_context(self):
        request = self.factory.get(
            "/zh-hans/archive/api/year/2025/month/8/results/",
            {"search": "git"},
        )
        context = self.make_context(month=8)

        with (
            patch("archive.views.get_archive_listing_context", return_value=context) as get_context,
            patch("archive.views.render_to_string", return_value="<section>results</section>"),
        ):
            response = month_archive_results_api(request, 2025, 8)

        payload = json.loads(response.content)
        get_context.assert_called_once_with(year=2025, month=8, query_params=request.GET)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["data"]["scope"], "month")
        self.assertEqual(payload["data"]["month"], 8)

    def test_invalid_month_returns_a_structured_json_404(self):
        response = month_archive_results_api(
            self.factory.get("/zh-hans/archive/api/year/2025/month/13/results/"),
            2025,
            13,
        )

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(payload["error"]["code"], "archive_not_found")

    def test_non_get_requests_are_rejected_as_json(self):
        response = year_archive_results_api(
            self.factory.post("/zh-hans/archive/api/year/2025/results/"),
            2025,
        )

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    def test_url_rejects_post_as_json_before_csrf_middleware(self):
        response = self.csrf_client.post(
            "/zh-hans/archive/api/year/2025/results/",
        )

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    def test_html_month_view_raises_404_for_an_invalid_month(self):
        with self.assertRaises(Http404):
            month_archive(
                self.factory.get("/zh-hans/archive/year/2025/month/13/"),
                2025,
                13,
            )
