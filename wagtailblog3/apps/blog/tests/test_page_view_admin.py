from datetime import date, datetime

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from wagtail.models import Page
from wagtail.models import Locale
from wagtail.permissions import policy_registry

from ..admin import PageViewAdmin, PageViewSnippetViewSet
from ..models import PageView


@override_settings(
    STORAGES={
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class PageViewAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.root = Page.get_first_root_node()
        if cls.root is None:
            Locale.objects.get_or_create(language_code=settings.LANGUAGE_CODE)
            cls.root = Page(title="PageView 测试根节点", slug="page-view-test-root")
            Page.add_root(instance=cls.root)
        cls.page = cls.root.add_child(
            instance=Page(title="访问测试页面", slug="page-view-admin-test")
        )
        cls.user = get_user_model().objects.create_user(
            username="page-view-user",
            password="test-password",
        )
        cls.admin_user = get_user_model().objects.create_superuser(
            username="page-view-admin",
            password="test-password",
            email="admin@example.com",
        )
        cls.last_viewed_at = timezone.make_aware(datetime(2026, 8, 9, 7, 8, 9))
        cls.logged_view = PageView.objects.create(
            page=cls.page,
            date=date(2026, 8, 9),
            user=cls.user,
            ip_address="192.0.2.10",
            user_agent="test-agent",
            last_viewed_at=cls.last_viewed_at,
        )
        cls.anonymous_view = PageView.objects.create(
            page=cls.page,
            date=date(2026, 8, 8),
            user=None,
            ip_address="192.0.2.11",
            last_viewed_at=timezone.make_aware(datetime(2026, 8, 8, 6, 5, 4)),
        )

    def test_page_view_has_human_readable_labels(self):
        self.assertEqual(self.logged_view.admin_page_title(), "访问测试页面")
        self.assertEqual(self.logged_view.admin_user(), "page-view-user")
        self.assertEqual(self.anonymous_view.admin_user(), "访客")
        self.assertIn("访问测试页面", str(self.logged_view))
        self.assertIn("用户：page-view-user", str(self.logged_view))
        self.assertIn("IP：192.0.2.10", str(self.logged_view))
        self.assertIn("最后访问：2026-08-09 07:08:09", str(self.logged_view))

    def test_wagtail_listing_has_readable_columns_and_latest_first_order(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("wagtailsnippets_blog_pageview:list"))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        for label in ("访问页面", "用户", "IP 地址", "访问日期", "最后访问时间"):
            self.assertIn(label, content)
        self.assertIn("访问测试页面", content)
        self.assertIn("page-view-user", content)
        self.assertIn("192.0.2.10", content)
        self.assertNotIn("PageView object", content)
        self.assertLess(
            content.index("2026-08-09 07:08:09"),
            content.index("2026-08-08 06:05:04"),
        )

    def test_wagtail_page_view_is_read_only(self):
        self.client.force_login(self.admin_user)
        add_url = reverse("wagtailsnippets_blog_pageview:add")
        edit_url = reverse(
            "wagtailsnippets_blog_pageview:edit",
            args=[self.logged_view.pk],
        )
        delete_url = reverse(
            "wagtailsnippets_blog_pageview:delete",
            args=[self.logged_view.pk],
        )

        for url in (add_url, edit_url, delete_url):
            response = self.client.get(url)
            self.assertIn(response.status_code, (302, 403))
            if response.status_code == 302:
                self.assertNotIn("/add/", response["Location"])
                self.assertNotIn("/edit/", response["Location"])
                self.assertNotIn("/delete/", response["Location"])
        self.assertFalse(PageViewAdmin(PageView, admin.site).has_add_permission(None))
        self.assertFalse(
            PageViewAdmin(PageView, admin.site).has_change_permission(None)
        )
        self.assertFalse(
            PageViewAdmin(PageView, admin.site).has_delete_permission(None)
        )


class PageViewSnippetQueryTests(TestCase):
    def test_read_only_policy_is_registered_explicitly(self):
        policy = policy_registry.get_by_type(PageView, fallback=False)
        self.assertIsNotNone(policy)
        self.assertFalse(policy.user_has_permission(None, "change"))

    def test_index_view_uses_related_objects_and_default_ordering(self):
        viewset = PageViewSnippetViewSet()
        queryset = viewset.get_queryset(None)

        self.assertIn("page", queryset.query.select_related)
        self.assertIn("user", queryset.query.select_related)
        self.assertEqual(viewset.ordering, ("-last_viewed_at", "-pk"))
