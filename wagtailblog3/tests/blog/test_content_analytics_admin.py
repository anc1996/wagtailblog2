"""内容分析后台入口的权限与响应集成测试。"""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class ContentAnalyticsAdminTests(TestCase):
    """确保只读报表不会绕过 Wagtail 后台权限，也不会依赖外部图表资源。"""

    url = "/admin/reports/content-analytics/"
    articles_url = "/admin/reports/content-analytics/articles/"

    def test_staff_without_analytics_permission_is_forbidden(self):
        user = get_user_model().objects.create_user(
            username="analytics-staff",
            password="test-only-password",
            is_staff=True,
        )
        # 单独授予后台入口权，才能验证业务报表权限本身确实返回 403。
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="wagtailadmin",
                codename="access_admin",
            )
        )
        self.client.force_login(user)

        response = self.client.get(
            self.url,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 403)

    def test_superuser_can_render_dashboard_with_local_chartjs(self):
        user = get_user_model().objects.create_superuser(
            username="analytics-admin",
            email="analytics@example.test",
            password="test-only-password",
        )
        self.client.force_login(user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "vendor/chartjs/chart.umd.js")
        self.assertContains(response, 'id="content-analytics-trend"')
        self.assertContains(response, "blog/js/content-analytics-admin.js")
        self.assertContains(response, 'data-analytics-articles')
        self.assertContains(response, self.articles_url)

    def test_superuser_can_export_utf8_csv(self):
        user = get_user_model().objects.create_superuser(
            username="analytics-export-admin",
            email="analytics-export@example.test",
            password="test-only-password",
        )
        self.client.force_login(user)

        response = self.client.get(self.url, {"export": "csv"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("content-analytics.csv", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))

    def test_article_fragment_requires_analytics_permission(self):
        user = get_user_model().objects.create_user(
            username="analytics-fragment-staff",
            password="test-only-password",
            is_staff=True,
        )
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="wagtailadmin",
                codename="access_admin",
            )
        )
        self.client.force_login(user)

        response = self.client.get(
            self.articles_url,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 403)

    def test_article_fragment_paginates_and_preserves_filters(self):
        user = get_user_model().objects.create_superuser(
            username="analytics-pagination-admin",
            email="analytics-pagination@example.test",
            password="test-only-password",
        )
        self.client.force_login(user)
        empty_authors = SimpleNamespace(all=lambda: [])
        pages = [
            SimpleNamespace(
                title=f"演示文章 {index:02d}",
                url=f"/demo/{index}/",
                authors=empty_authors,
                analytics_views=100 - index,
                analytics_visitors=50,
                analytics_engaged=30,
                analytics_reached_90=20,
                analytics_active_seconds=2500,
            )
            for index in range(12)
        ]

        with patch(
            "blog.wagtail_hooks.ContentAnalyticsQueryService.article_performance",
            return_value=pages,
        ):
            response = self.client.get(
                self.articles_url,
                {"page": 2, "author": 7},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "演示文章 10")
        self.assertContains(response, "演示文章 11")
        self.assertNotContains(response, "演示文章 09")
        self.assertContains(response, "第 2 / 2 页，共 12 篇")
        self.assertContains(response, "author=7")
