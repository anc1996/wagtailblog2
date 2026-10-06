"""验证后台历史正文故障不会退化为未处理 500 或错误恢复。"""

from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.test import RequestFactory, SimpleTestCase

from blog.middleware import PageViewMiddleware
from blog.models import BlogRevisionBodyUnavailableError
from external_content.exceptions import PublicationBodyUnavailableError


class RevisionBodyErrorMiddlewareTests(SimpleTestCase):
    """验证 Wagtail 后台历史路径与前台正文异常的受控错误状态和可访问提示。"""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.middleware = PageViewMiddleware(lambda request: None)

    def _request(self, path: str) -> HttpRequest:
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

    def test_admin_edit_publication_body_unavailable_returns_controlled_non_500(self) -> None:
        """验证后台编辑页面遇到正文不可用异常时返回受控非 500 页面且包含历史链接与不可重试标记。"""
        request = self._request("/admin/pages/715/edit/")

        response = self.middleware.process_exception(
            request,
            PublicationBodyUnavailableError("正文不可读取或校验失败"),
        )

        self.assertIsNotNone(response, "中间件应拦截 PublicationBodyUnavailableError 并返回受控响应，而非退化为未处理 500")
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.context_data["history_url"], "/admin/pages/715/history/")
        self.assertFalse(response.context_data["retryable"])

    def test_frontend_publication_body_unavailable_returns_controlled_503_without_traceback(self) -> None:
        """验证前台文章路径遇到外置正文不可用异常时返回受控 503 页面，且不泄露 Django traceback。"""
        request = self._request("/zh-hans/pub-ea0bd6/")

        response = self.middleware.process_exception(
            request,
            PublicationBodyUnavailableError("MongoDB 正文版本不可读取或网络中断"),
        )

        self.assertIsNotNone(response, "前台发生正文不可用时中间件应拦截处理，而非返回 None 暴露未捕获异常")
        self.assertEqual(response.status_code, 503)
        response.render()
        content = response.content.decode("utf-8", errors="ignore")
