"""验证后台历史正文故障不会退化为未处理 500 或错误恢复。"""

from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.test import RequestFactory, TestCase

from blog.middleware import PageViewMiddleware
from blog.models import BlogRevisionBodyUnavailableError
from external_content.exceptions import (
    ContentStoreError,
    ContentVersionIdentityMismatchError,
    ContentVersionNotFoundError,
    CorruptedContentPayloadError,
    ExternalContentOperationBlockedError,
    ManifestIdentityMismatchError,
    ManifestValidationError,
    PublicationBodyUnavailableError,
    PublicationRevisionInvalidError,
)


class RevisionBodyErrorMiddlewareTests(TestCase):
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
        self.assertNotIn("MongoDB 正文版本不可读取或网络中断", content)

    def test_external_revision_integrity_errors_are_controlled_and_redacted(self) -> None:
        """读取异常只能展示稳定错误码，不回显正文、连接信息或底层异常详情。"""
        for error_type in (
            ContentVersionNotFoundError,
            ContentVersionIdentityMismatchError,
            CorruptedContentPayloadError,
            ManifestIdentityMismatchError,
            ManifestValidationError,
            PublicationRevisionInvalidError,
        ):
            with self.subTest(error_type=error_type.__name__):
                request = self._request("/admin/pages/38/revisions/947/view/")
                response = self.middleware.process_exception(
                    request,
                    error_type("synthetic-private-body", details={"payload": "private-details"}),
                )
                self.assertIsNotNone(response)
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.context_data["retryable"])
                self.assertEqual(response.context_data["error_code"], error_type.code)
                response.render()
                self.assertNotIn("synthetic-private-body", response.content.decode())
                self.assertNotIn("private-details", response.content.decode())

    def test_external_revision_network_timeout_is_retryable(self) -> None:
        """只把仓储已分类的网络超时认作暂时故障，不能猜测未知异常可重试。"""
        response = self.middleware.process_exception(
            self._request("/admin/pages/38/edit/"),
            ContentStoreError("synthetic-storage-error", details={"error_category": "network_timeout"}),
        )
        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.context_data["retryable"])

    def test_external_revision_other_store_errors_are_not_retryable(self) -> None:
        for category in ("operation_failure", "settings_error", "storage_error"):
            with self.subTest(category=category):
                response = self.middleware.process_exception(
                    self._request("/admin/pages/38/revisions/947/revert/"),
                    ContentStoreError("synthetic-error", details={"error_category": category}),
                )
                self.assertIsNotNone(response)
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.context_data["retryable"])

    def test_external_read_error_outside_page_read_boundary_is_not_intercepted(self) -> None:
        for path in ("/articles/38/", "/admin/pages/38/delete/", "/admin/bulk/wagtailcore/page/delete/"):
            with self.subTest(path=path):
                self.assertIsNone(self.middleware.process_exception(
                    self._request(path), ContentVersionNotFoundError("synthetic-missing"),
                ))

    def test_unrelated_and_write_freeze_errors_are_not_disguised_as_read_errors(self) -> None:
        for exception in (ValueError("synthetic-bug"), ExternalContentOperationBlockedError("frozen")):
            with self.subTest(error_type=type(exception).__name__):
                self.assertIsNone(self.middleware.process_exception(
                    self._request("/admin/pages/38/edit/"), exception,
                ))

    def test_admin_post_failures_keep_the_original_write_error_boundary(self) -> None:
        """编辑和恢复提交不能被包装为读取冲突，避免误导用户重试写入。"""
        for path in ("/admin/pages/38/edit/", "/admin/pages/38/revisions/947/revert/"):
            for exception in (
                ContentVersionNotFoundError("synthetic-missing"),
                ContentStoreError("synthetic-timeout", details={"error_category": "network_timeout"}),
                PublicationBodyUnavailableError("synthetic-publication-missing"),
                BlogRevisionBodyUnavailableError("revision_snapshot_missing"),
            ):
                with self.subTest(path=path, error_type=type(exception).__name__):
                    request = self.factory.post(path)
                    request.user = AnonymousUser()
                    self.assertIsNone(self.middleware.process_exception(request, exception))

    def test_admin_head_retains_the_controlled_read_error_response(self) -> None:
        """HEAD 属于安全读取，仍需保留正文缺失与网络故障的状态区分。"""
        for exception, expected_status in (
            (ContentVersionNotFoundError("synthetic-missing"), 409),
            (PublicationBodyUnavailableError("synthetic-publication-missing"), 409),
            (BlogRevisionBodyUnavailableError("revision_snapshot_missing"), 409),
            (ContentStoreError("synthetic-timeout", details={"error_category": "network_timeout"}), 503),
        ):
            with self.subTest(error_type=type(exception).__name__):
                request = self.factory.head("/admin/pages/38/edit/")
                request.user = AnonymousUser()
                response = self.middleware.process_exception(request, exception)
                self.assertIsNotNone(response)
                self.assertEqual(response.status_code, expected_status)

    def test_frontend_post_is_not_mislabeled_as_a_publication_read_failure(self) -> None:
        """前台非读取请求同样不得被页面读取提示吞掉原始操作错误。"""
        request = self.factory.post("/zh-hans/synthetic-guide/")
        request.user = AnonymousUser()
        self.assertIsNone(self.middleware.process_exception(
            request, PublicationBodyUnavailableError("synthetic-missing"),
        ))
