"""验证后台历史正文故障不会退化为未处理 500 或错误恢复。"""

from pathlib import Path

from django.contrib.auth.models import AnonymousUser
from django.conf import settings
from django.test import RequestFactory, SimpleTestCase

from blog.middleware import PageViewMiddleware
from blog.models import BlogRevisionBodyUnavailableError


class RevisionBodyErrorMiddlewareTests(SimpleTestCase):
    """验证 Wagtail 后台历史路径的受控错误状态和可访问提示。"""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.middleware = PageViewMiddleware(lambda request: None)

    def _request(self, path: str):
        request = self.factory.get(path)
        request.user = AnonymousUser()
        return request

    def _template_source(self) -> str:
        template = (
            Path(settings.PROJECT_DIR)
            / "templates"
            / "blog"
            / "admin"
            / "revision_body_unavailable.html"
        )
        return template.read_text(encoding="utf-8")

    def test_missing_snapshot_returns_conflict_page_with_history_link(self):
        request = self._request("/admin/pages/38/revisions/947/view/")

        response = self.middleware.process_exception(
            request,
            BlogRevisionBodyUnavailableError("revision_snapshot_missing"),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.template_name, "blog/admin/revision_body_unavailable.html")
        self.assertEqual(response.context_data["history_url"], "/admin/pages/38/history/")
        self.assertFalse(response.context_data["retryable"])
        self.assertIn('role="alert"', self._template_source())

    def test_unavailable_store_returns_retryable_service_error(self):
        request = self._request("/admin/pages/38/revisions/947/view/?mode=default")

        response = self.middleware.process_exception(
            request,
            BlogRevisionBodyUnavailableError(
                "revision_store_unavailable",
                retryable=True,
            ),
        )

        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.context_data["retryable"])
        self.assertEqual(response.context_data["retry_url"], request.get_full_path())
        self.assertIn("重试读取", self._template_source())

    def test_non_admin_path_uses_default_exception_handling(self):
        request = self._request("/articles/38/")

        response = self.middleware.process_exception(
            request,
            BlogRevisionBodyUnavailableError("revision_snapshot_missing"),
        )

        self.assertIsNone(response)
