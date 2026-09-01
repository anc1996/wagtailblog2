import json
from unittest.mock import MagicMock, patch

from django.core.paginator import Paginator
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse

from blog.models import Author
from blog.views import AuthorDetailView, author_posts_api


class AuthorDetailViewPaginationTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.author = Author(pk=3, name="Adrian Holovaty")

    def render_context(self, page_value, posts, search_query="", author_post_count=None):
        request = self.factory.get(
            "/zh-hans/blog/authors/3/", {"page": page_value, "q": search_query}
        )
        view = AuthorDetailView()
        view.setup(request, pk=self.author.pk)
        view.object = self.author

        with patch("blog.views.BlogPage.objects") as manager:
            all_posts = (
                manager.live.return_value
                .public.return_value
                .filter.return_value
                .select_related.return_value
                .prefetch_related.return_value
            )
            ordered_queryset = (
                all_posts.filter.return_value.order_by
                if search_query
                else all_posts.order_by
            )
            ordered_queryset.return_value = posts
            if author_post_count is not None:
                all_posts.count.return_value = author_post_count
            context = view.get_context_data(object=self.author)

        return context, manager, ordered_queryset

    def test_paginates_live_public_posts_ten_per_page(self):
        posts = [MagicMock(pk=index) for index in range(1, 24)]

        context, manager, ordered_queryset = self.render_context("2", posts)

        manager.live.assert_called_once_with()
        manager.live.return_value.public.assert_called_once_with()
        manager.live.return_value.public.return_value.filter.assert_called_once_with(
            authors=self.author
        )
        filtered_posts = manager.live.return_value.public.return_value.filter.return_value
        filtered_posts.select_related.assert_called_once_with("featured_image")
        filtered_posts.select_related.return_value.prefetch_related.assert_called_once_with("tags")
        ordered_queryset.assert_called_once_with("-date", "-pk")
        self.assertEqual(context["page_obj"].number, 2)
        self.assertEqual(list(context["blog_posts"]), posts[10:20])
        self.assertEqual(context["total_posts"], 23)
        self.assertEqual(context["paginator"].num_pages, 3)
        self.assertTrue(context["is_paginated"])

    def test_filters_current_authors_posts_by_title(self):
        posts = [MagicMock(pk=1), MagicMock(pk=2)]

        context, manager, ordered_queryset = self.render_context(
            "1", posts, search_query="django", author_post_count=23
        )

        base_posts = (
            manager.live.return_value.public.return_value.filter.return_value
            .select_related.return_value.prefetch_related.return_value
        )
        base_posts.filter.assert_called_once_with(title__icontains="django")
        ordered_queryset.assert_called_once_with("-date", "-pk")
        self.assertEqual(context["search_query"], "django")
        self.assertEqual(context["total_posts"], 2)
        self.assertEqual(context["author_post_count"], 23)

    def test_strips_search_query_before_filtering(self):
        posts = [MagicMock(pk=1)]

        context, manager, _ = self.render_context(
            "1", posts, search_query="  django  ", author_post_count=23
        )

        base_posts = (
            manager.live.return_value.public.return_value.filter.return_value
            .select_related.return_value.prefetch_related.return_value
        )
        base_posts.filter.assert_called_once_with(title__icontains="django")
        self.assertEqual(context["search_query"], "django")

    def test_non_numeric_page_falls_back_to_first_page(self):
        posts = [MagicMock(pk=index) for index in range(1, 13)]

        context, _, _ = self.render_context("not-a-number", posts)

        self.assertEqual(context["page_obj"].number, 1)
        self.assertEqual(list(context["blog_posts"]), posts[:10])

    def test_out_of_range_page_falls_back_to_last_page(self):
        posts = [MagicMock(pk=index) for index in range(1, 13)]

        context, _, _ = self.render_context("999", posts)

        self.assertEqual(context["page_obj"].number, 2)
        self.assertEqual(list(context["blog_posts"]), posts[10:])


class AuthorPostsApiTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.author = Author(pk=3, name="Adrian Holovaty")

    def make_context(self, page=2, search_query="Python"):
        posts = list(range(12))
        paginator = Paginator(posts, 10)
        page_obj = paginator.get_page(page)
        return {
            "blog_posts": page_obj.object_list,
            "page_obj": page_obj,
            "paginator": paginator,
            "is_paginated": page_obj.has_other_pages(),
            "total_posts": paginator.count,
            "author_post_count": 116,
            "search_query": search_query,
        }

    def test_returns_server_rendered_fragment_and_pagination_metadata(self):
        request = self.factory.get(
            "/zh-hans/blog/api/authors/3/posts/", {"q": "Python", "page": "2"}
        )
        context = self.make_context()

        with (
            patch("blog.views.Author.objects") as author_manager,
            patch("blog.views.get_author_posts_context", return_value=context) as get_context,
            patch(
                "blog.views.render_to_string", return_value="<section>article results</section>"
            ) as render_fragment,
        ):
            author_manager.filter.return_value.first.return_value = self.author
            response = author_posts_api(request, self.author.pk)

        payload = json.loads(response.content)
        expected_url = (
            reverse("blog:author_detail", kwargs={"pk": self.author.pk})
            + "?q=Python&page=2"
        )

        author_manager.filter.assert_called_once_with(pk=self.author.pk)
        get_context.assert_called_once_with(author=self.author, query_params=request.GET)
        render_fragment.assert_called_once_with(
            "blog/partials/_author_post_results.html",
            {"author": self.author, **context},
            request=request,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["query"], "Python")
        self.assertEqual(payload["data"]["author_post_count"], 116)
        self.assertEqual(payload["data"]["result_count"], 12)
        self.assertEqual(payload["data"]["html"], "<section>article results</section>")
        self.assertEqual(payload["data"]["canonical_url"], expected_url)
        self.assertEqual(
            payload["data"]["pagination"],
            {
                "page": 2,
                "page_size": 10,
                "total_pages": 2,
                "has_previous": True,
                "has_next": False,
            },
        )

    def test_returns_structured_json_404_for_unknown_author(self):
        request = self.factory.get("/zh-hans/blog/api/authors/999/posts/")

        with patch("blog.views.Author.objects") as author_manager:
            author_manager.filter.return_value.first.return_value = None
            response = author_posts_api(request, 999)

        payload = json.loads(response.content)
        author_manager.filter.assert_called_once_with(pk=999)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(
            payload,
            {
                "ok": False,
                "error": {
                    "code": "author_not_found",
                    "message": "未找到该作者。",
                },
            },
        )

    def test_rejects_non_get_requests_as_json(self):
        request = self.factory.post("/zh-hans/blog/api/authors/3/posts/")

        response = author_posts_api(request, self.author.pk)

        payload = json.loads(response.content)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(
            payload,
            {
                "ok": False,
                "error": {
                    "code": "method_not_allowed",
                    "message": "仅支持 GET 请求。",
                },
            },
        )
