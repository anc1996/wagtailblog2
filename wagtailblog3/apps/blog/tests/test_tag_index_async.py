import json
from datetime import date
from unittest.mock import MagicMock, call, patch

from django.core.paginator import Paginator
from django.test import RequestFactory, SimpleTestCase

from blog.models import BlogTagIndexPage
from blog.services.tag_listing import get_tag_index_context, normalise_date_filter
from blog.views import get_tag_index_canonical_url, tag_index_results_api


class TagIndexContextTests(SimpleTestCase):
    def test_normalises_valid_and_invalid_dates(self):
        self.assertEqual(normalise_date_filter(" 2026-08-07 "), ("2026-08-07", date(2026, 8, 7)))
        self.assertEqual(normalise_date_filter("2026-02-30"), ("", None))
        self.assertEqual(normalise_date_filter("not-a-date"), ("", None))

    def test_builds_filtered_paginated_tag_directory_from_live_public_pages(self):
        tags = [MagicMock(pk=index) for index in range(1, 56)]

        with (
            patch("blog.services.tag_listing.BlogPage.objects") as page_manager,
            patch("blog.services.tag_listing.Tag.objects") as tag_manager,
        ):
            live_public_pages = page_manager.live.return_value.public.return_value
            live_public_pages.values.return_value = "live-public-page-ids"
            tag_queryset = tag_manager.filter.return_value
            annotated_tags = tag_queryset.annotate.return_value
            filtered_tags = annotated_tags.filter.return_value
            filtered_tags.order_by.return_value = tags

            context = get_tag_index_context(
                query_params={"q": "  Python  ", "page": "2"},
                tag_page_size=50,
                article_page_size=20,
            )

        page_manager.live.assert_called_once_with()
        page_manager.live.return_value.public.assert_called_once_with()
        live_public_pages.values.assert_called_once_with("id")
        tag_manager.filter.assert_called_once_with(
            blog_blogpagetag_items__content_object_id__in="live-public-page-ids"
        )
        tag_queryset.annotate.assert_called_once()
        annotated_tags.filter.assert_called_once_with(name__icontains="Python")
        filtered_tags.order_by.assert_called_once_with("-count", "name", "pk")
        self.assertEqual(context["mode"], "tag_list")
        self.assertEqual(context["search_query"], "Python")
        self.assertEqual(context["paged_items"].number, 2)
        self.assertEqual(list(context["paged_items"].object_list), tags[50:])
        self.assertEqual(context["total_results"], 55)

    def test_builds_filtered_tag_detail_from_live_public_articles(self):
        tag = MagicMock(slug="python", name="Python")
        articles = [MagicMock(pk=index) for index in range(1, 26)]

        with (
            patch("blog.services.tag_listing.Tag.objects") as tag_manager,
            patch("blog.services.tag_listing.BlogPage.objects") as page_manager,
        ):
            tag_manager.filter.return_value.first.return_value = tag
            public_articles = page_manager.live.return_value.public.return_value
            tagged_articles = public_articles.filter.return_value
            selected_articles = tagged_articles.select_related.return_value
            selected_articles.filter.return_value = selected_articles
            selected_articles.order_by.return_value = articles

            context = get_tag_index_context(
                query_params={
                    "tag": " python ",
                    "q": "  async  ",
                    "start_date": "2026-01-01",
                    "end_date": "2026-12-31",
                    "page": "2",
                },
                tag_page_size=50,
                article_page_size=20,
            )

        tag_manager.filter.assert_called_once_with(slug="python")
        page_manager.live.assert_called_once_with()
        page_manager.live.return_value.public.assert_called_once_with()
        public_articles.filter.assert_called_once_with(tags=tag)
        tagged_articles.select_related.assert_called_once_with("featured_image")
        selected_articles.filter.assert_has_calls(
            [
                call(title__icontains="async"),
                call(date__gte=date(2026, 1, 1)),
                call(date__lte=date(2026, 12, 31)),
            ]
        )
        selected_articles.order_by.assert_called_once_with("-date", "-pk")
        self.assertEqual(context["mode"], "tag_detail")
        self.assertEqual(context["current_tag"], tag)
        self.assertEqual(context["paged_items"].number, 2)
        self.assertEqual(list(context["paged_items"].object_list), articles[20:])
        self.assertEqual(context["total_results"], 25)

    def test_missing_tag_avoids_article_query(self):
        with (
            patch("blog.services.tag_listing.Tag.objects") as tag_manager,
            patch("blog.services.tag_listing.BlogPage.objects") as page_manager,
        ):
            tag_manager.filter.return_value.first.return_value = None
            context = get_tag_index_context(
                query_params={"tag": "missing"},
                tag_page_size=50,
                article_page_size=20,
            )

        self.assertEqual(context["mode"], "tag_missing")
        self.assertEqual(context["requested_tag_slug"], "missing")
        self.assertIsNone(context["paged_items"])
        page_manager.live.assert_not_called()

    def test_invalid_page_and_dates_fall_back_safely(self):
        tag = MagicMock(slug="python")
        articles = [MagicMock(pk=1)]

        with (
            patch("blog.services.tag_listing.Tag.objects") as tag_manager,
            patch("blog.services.tag_listing.BlogPage.objects") as page_manager,
        ):
            tag_manager.filter.return_value.first.return_value = tag
            selected_articles = (
                page_manager.live.return_value.public.return_value.filter.return_value.select_related.return_value
            )
            selected_articles.order_by.return_value = articles
            context = get_tag_index_context(
                query_params={
                    "tag": "python",
                    "start_date": "2026-99-99",
                    "end_date": "invalid",
                    "page": "not-a-page",
                },
                tag_page_size=50,
                article_page_size=20,
            )

        selected_articles.filter.assert_not_called()
        self.assertEqual(context["start_date"], "")
        self.assertEqual(context["end_date"], "")
        self.assertEqual(context["paged_items"].number, 1)


class TagIndexResultsApiTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.page = MagicMock(spec=BlogTagIndexPage)
        self.page.pk = 42
        self.page.items_tag_page = 50
        self.page.items_per_page = 20
        self.page.url = "/zh-hans/tags/"
        self.page.get_url.return_value = "http://example.test/zh-hans/tags/"

    def make_context(self, mode="tag_detail"):
        page_obj = Paginator(list(range(25)), 20).get_page(2)
        return {
            "mode": mode,
            "requested_tag_slug": "python",
            "search_query": "async",
            "start_date": "2026-01-01",
            "end_date": "",
            "current_tag": MagicMock(slug="python", name="Python") if mode == "tag_detail" else None,
            "paged_items": page_obj if mode != "tag_missing" else None,
            "paginator": page_obj.paginator if mode != "tag_missing" else None,
            "is_paginated": True,
            "total_results": 25 if mode != "tag_missing" else 0,
        }

    def test_returns_rendered_fragment_metadata_and_canonical_url(self):
        request = self.factory.get(
            "/zh-hans/blog/api/tag-index-pages/42/results/",
            {
                "tag": "python",
                "q": "async",
                "start_date": "2026-01-01",
                "page": "2",
            },
        )
        context = self.make_context()

        with (
            patch("blog.views.BlogTagIndexPage.objects") as page_manager,
            patch("blog.views.get_tag_index_context", return_value=context) as get_context,
            patch("blog.views.render_to_string", return_value="<section>tags</section>") as render_fragment,
        ):
            page_manager.live.return_value.public.return_value.filter.return_value.first.return_value = self.page
            response = tag_index_results_api(request, self.page.pk)

        payload = json.loads(response.content)
        get_context.assert_called_once_with(
            query_params=request.GET,
            tag_page_size=50,
            article_page_size=20,
        )
        render_fragment.assert_called_once_with(
            "blog/partials/_blog_tag_index_results.html",
            {"page": self.page, **context},
            request=request,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["mode"], "tag_detail")
        self.assertEqual(payload["data"]["result_count"], 25)
        self.assertEqual(payload["data"]["html"], "<section>tags</section>")
        self.assertEqual(
            payload["data"]["canonical_url"],
            "/zh-hans/tags/?tag=python&q=async&start_date=2026-01-01&page=2",
        )
        self.assertEqual(
            payload["data"]["pagination"],
            {
                "page": 2,
                "page_size": 20,
                "total_pages": 2,
                "has_previous": True,
                "has_next": False,
            },
        )

    def test_missing_tag_returns_successful_empty_fragment_state(self):
        request = self.factory.get(
            "/zh-hans/blog/api/tag-index-pages/42/results/", {"tag": "missing"}
        )
        context = self.make_context(mode="tag_missing")

        with (
            patch("blog.views.BlogTagIndexPage.objects") as page_manager,
            patch("blog.views.get_tag_index_context", return_value=context),
            patch("blog.views.render_to_string", return_value="<div>missing</div>"),
        ):
            page_manager.live.return_value.public.return_value.filter.return_value.first.return_value = self.page
            response = tag_index_results_api(request, self.page.pk)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["data"]["mode"], "tag_missing")
        self.assertIsNone(payload["data"]["pagination"])
        self.assertEqual(payload["data"]["canonical_url"], "/zh-hans/tags/?tag=python&q=async")

    def test_returns_structured_404_for_unknown_or_non_public_page(self):
        request = self.factory.get("/zh-hans/blog/api/tag-index-pages/999/results/")

        with patch("blog.views.BlogTagIndexPage.objects") as page_manager:
            page_manager.live.return_value.public.return_value.filter.return_value.first.return_value = None
            response = tag_index_results_api(request, 999)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(payload["error"]["code"], "tag_index_not_found")

    def test_rejects_non_get_requests_as_json(self):
        response = tag_index_results_api(
            self.factory.post("/zh-hans/blog/api/tag-index-pages/42/results/"),
            self.page.pk,
        )

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    def test_canonical_url_omits_irrelevant_directory_dates(self):
        context = self.make_context(mode="tag_list")
        context.update(
            {
                "requested_tag_slug": "",
                "search_query": "Python",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "paged_items": Paginator(list(range(51)), 50).get_page(2),
            }
        )

        url = get_tag_index_canonical_url(
            page=self.page,
            context=context,
            request=self.factory.get("/api/"),
        )

        self.assertEqual(url, "/zh-hans/tags/?q=Python&page=2")


class BlogTagIndexPageContextTests(SimpleTestCase):
    def test_page_context_delegates_to_shared_query_service(self):
        page = MagicMock(spec=BlogTagIndexPage)
        page.items_tag_page = 50
        page.items_per_page = 20
        request = self.factory = RequestFactory().get("/zh-hans/tags/", {"q": "Python"})
        listing_context = {"mode": "tag_list", "total_results": 3}

        with (
            patch("wagtail.models.Page.get_context", return_value={"page": page}),
            patch("blog.services.tag_listing.get_tag_index_context", return_value=listing_context) as get_context,
        ):
            context = BlogTagIndexPage.get_context(page, request)

        get_context.assert_called_once_with(
            query_params=request.GET,
            tag_page_size=50,
            article_page_size=20,
        )
        self.assertEqual(context["mode"], "tag_list")
        self.assertEqual(context["total_results"], 3)
