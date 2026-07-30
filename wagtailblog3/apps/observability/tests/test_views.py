import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.test.client import Client, RequestFactory
from django.test import TestCase, override_settings
from django.urls import reverse

from observability.models import LogClearAudit
from observability.permissions import MANAGE_PERMISSION, VIEW_PERMISSION, require_log_permission
from observability.views import _issue_preview
from observability.wagtail_hooks import SystemLogsMenuItem


TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=TEST_STORAGES)
class LogAdminViewTests(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "blog").mkdir()
        self.user = get_user_model().objects.create_user(
            username="editor", password="test", is_staff=True
        )
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="wagtailadmin", codename="access_admin")
        )
        self.client.force_login(self.user)

    def _grant(self, *codenames):
        permissions = Permission.objects.filter(
            content_type__app_label="observability", codename__in=codenames
        )
        self.user.user_permissions.add(*permissions)
        self.user = get_user_model().objects.get(pk=self.user.pk)

    def _preview(self, **overrides):
        query = {
            "target_type": "file",
            "target": "blog_error",
            "kind": "error",
            "scope": "all",
        }
        query.update(overrides)
        response = self.client.get(reverse("observability:clear_preview"), query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    @override_settings(LOG_DIR="/tmp/observability-view-denied")
    def test_view_requires_explicit_permission(self):
        response = self.client.get(reverse("observability:overview"))
        self.assertNotEqual(response.status_code, 200)

    def test_permission_helper_enforces_both_permissions(self):
        request = RequestFactory().get("/admin/reports/system-logs/")
        request.user = self.user
        with self.assertRaises(PermissionDenied):
            require_log_permission(request, VIEW_PERMISSION)
        with self.assertRaises(PermissionDenied):
            require_log_permission(request, MANAGE_PERMISSION)

    def test_menu_visibility_follows_view_permission(self):
        request = RequestFactory().get("/admin/")
        request.user = self.user
        menu_item = SystemLogsMenuItem("系统日志", "/admin/reports/system-logs/")
        self.assertFalse(menu_item.is_shown(request))
        self._grant("view_logs")
        request.user = self.user
        self.assertTrue(menu_item.is_shown(request))

    def test_manage_permission_does_not_implicitly_grant_view_access(self):
        self._grant("manage_logs")
        response = self.client.get(reverse("observability:overview"))
        self.assertNotEqual(response.status_code, 200)

    def test_group_view_permission_controls_menu_and_url_together(self):
        group = Group.objects.create(name="日志查看员")
        group.permissions.add(Permission.objects.get(codename="view_logs"))
        self.user.groups.add(group)
        self.user = get_user_model().objects.get(pk=self.user.pk)
        request = RequestFactory().get("/admin/")
        request.user = self.user
        menu_item = SystemLogsMenuItem("系统日志", "/admin/reports/system-logs/")
        self.assertTrue(menu_item.is_shown(request))
        with override_settings(LOG_DIR=self.root):
            response = self.client.get(reverse("observability:overview"))
        self.assertEqual(response.status_code, 200)

    def test_authorized_user_can_open_all_read_pages(self):
        self._grant("view_logs")
        with override_settings(LOG_DIR=self.root):
            for name in ("overview", "records", "audits"):
                response = self.client.get(reverse(f"observability:{name}"))
                self.assertEqual(response.status_code, 200, name)
                self.assertEqual(response.headers["Cache-Control"], "max-age=0, no-cache, no-store, must-revalidate, private")

    def test_records_page_uses_numbered_cursor_pagination(self):
        self._grant("view_logs")
        current = self.root / "blog/blog_error.log"
        current.write_text("".join(
            f"[2026-07-29 15:00:{second:02d}] ERROR [blog.views:run:10] "
            f"[pid=1 thread=MainThread] record {second}\n"
            for second in range(4)
        ), encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            response = self.client.get(
                reverse("observability:records"),
                {"domain": "blog", "kind": "error", "period": "all", "page_size": "50"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "上一页")
        self.assertContains(response, "下一页")
        self.assertContains(response, "跳转到")
        self.assertNotContains(response, "加载更早记录")

    def test_audit_page_exposes_file_level_details_for_success(self):
        self._grant("view_logs")
        LogClearAudit.objects.create(
            user=self.user,
            target="file:blog_error:error",
            scope="current",
            succeeded=True,
            details={
                "file_results": [
                    {"file": "blog/blog_error.log", "outcome": "truncated"}
                ]
            },
        )
        response = self.client.get(reverse("observability:audits"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "查看逐文件明细")
        self.assertContains(response, "blog/blog_error.log")

    def test_log_content_is_html_escaped(self):
        self._grant("view_logs")
        current = self.root / "blog/blog_error.log"
        current.write_text(
            "[2026-07-29 15:00:00] ERROR [blog.views:run:10] "
            "[pid=1 thread=MainThread] <script>alert(1)</script>\n",
            encoding="utf-8",
        )
        with override_settings(LOG_DIR=self.root):
            response = self.client.get(
                reverse("observability:records"),
                {"domain": "blog", "kind": "error", "period": "all", "page_size": "100"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;", html=False)
        self.assertNotContains(response, "<script>alert(1)</script>", html=False)

    def test_records_page_and_copy_sources_are_redacted_and_relative(self):
        self._grant("view_logs")
        current = self.root / "blog/blog_error.log"
        current.write_text(
            "[2026-07-29 15:00:00] ERROR "
            "[blog.views|wagtailblog3/apps/blog/views.py:save:42] "
            "[pid=1 thread=MainThread] password=hunter2 token=raw-token\n"
            "Traceback (most recent call last):\n"
            "  File \"/home/source/Django/wagtail/wagtailblog2/"
            "wagtailblog3/apps/blog/views.py\", line 42\n",
            encoding="utf-8",
        )
        with override_settings(LOG_DIR=self.root):
            response = self.client.get(
                reverse("observability:records"),
                {"domain": "blog", "kind": "error", "period": "all", "page_size": "100"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "hunter2")
        self.assertNotContains(response, "raw-token")
        self.assertNotContains(response, "/home/source/")
        self.assertContains(response, "[REDACTED]")
        self.assertContains(response, "wagtailblog3/apps/blog/views.py")
        self.assertContains(response, "函数：save")
        self.assertContains(response, "代码行：42")

    def test_partial_filter_query_keeps_required_defaults(self):
        self._grant("view_logs")
        with override_settings(LOG_DIR=self.root):
            response = self.client.get(reverse("observability:records"), {"domain": "blog"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["filter_form"].errors)
        self.assertEqual(response.context["filter_form"].cleaned_data["kind"], "error")
        self.assertEqual(response.context["filter_form"].cleaned_data["period"], "24h")

    def test_manage_permission_is_required_for_clear_page(self):
        self._grant("view_logs")
        with override_settings(LOG_DIR=self.root):
            response = self.client.get(reverse("observability:clear"))
        self.assertNotEqual(response.status_code, 200)

    def test_overview_renders_native_dialog_and_all_clear_granularities(self):
        self._grant("view_logs", "manage_logs")
        with override_settings(LOG_DIR=self.root):
            response = self.client.get(reverse("observability:overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="log-clear-dialog"')
        self.assertContains(response, 'data-target-type="file"')
        self.assertContains(response, 'data-target-type="domain"')
        self.assertContains(response, 'data-target-type="business"')
        self.assertNotContains(response, 'data-target-type="all"')

    def test_preview_api_returns_structured_counts_and_fresh_execution_claim(self):
        self._grant("view_logs", "manage_logs")
        current = self.root / "blog/blog_error.log"
        current.write_bytes(b"current")
        Path(f"{current}.1").write_bytes(b"rotated")
        with override_settings(LOG_DIR=self.root):
            first = self._preview()
            second = self._preview()
        self.assertEqual(first["current"]["file_count"], 1)
        self.assertEqual(first["rotated"]["file_count"], 1)
        self.assertEqual(first["total"]["total_bytes"], 14)
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertNotEqual(first["preview_token"], second["preview_token"])

    def test_preview_api_requires_manage_permission(self):
        self._grant("view_logs")
        with override_settings(LOG_DIR=self.root):
            response = self.client.get(
                reverse("observability:clear_preview"),
                {"target_type": "file", "target": "blog_error", "scope": "all"},
            )
        self.assertNotEqual(response.status_code, 200)

    def test_non_superuser_cannot_clear_all_runtime_logs(self):
        self._grant("view_logs", "manage_logs")
        selection = {"target_type": "all", "target": "", "kind": "", "scope": "all"}
        key, preview_token = _issue_preview(selection)
        with override_settings(LOG_DIR=self.root):
            response = self.client.post(
                reverse("observability:clear"),
                {
                    "target_type": "all",
                    "target": "",
                    "kind": "",
                    "scope": "all",
                    "confirmation": "清空全部日志",
                    "idempotency_key": key,
                    "preview_token": preview_token,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "仅限超级管理员")
        self.assertEqual(LogClearAudit.objects.count(), 0)

    def test_signed_preview_rejects_target_tampering(self):
        self._grant("view_logs", "manage_logs")
        current = self.root / "blog/blog_error.log"
        current.write_text("keep", encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            preview = self._preview()
            response = self.client.post(
                reverse("observability:clear"),
                {
                    "target_type": "file",
                    "target": "blog_activity",
                    "kind": "activity",
                    "scope": "all",
                    "confirmation": "确认清理日志",
                    "idempotency_key": preview["idempotency_key"],
                    "preview_token": preview["preview_token"],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "提交目标与确认预览不一致")
        self.assertEqual(current.read_text(encoding="utf-8"), "keep")
        self.assertEqual(LogClearAudit.objects.count(), 0)

    def test_clear_endpoint_rejects_get_execution(self):
        self._grant("view_logs", "manage_logs")
        current = self.root / "blog/blog_error.log"
        current.write_text("keep", encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            response = self.client.get(
                reverse("observability:clear"),
                {"target_type": "file", "target": "blog_error", "scope": "all"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(current.read_text(encoding="utf-8"), "keep")

    def test_clear_post_requires_csrf(self):
        self._grant("view_logs", "manage_logs")
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        with override_settings(LOG_DIR=self.root):
            response = csrf_client.post(
                reverse("observability:clear"),
                {
                    "target_type": "file",
                    "target": "blog_error",
                    "kind": "",
                    "scope": "all",
                    "idempotency_key": uuid.uuid4(),
                },
            )
        self.assertEqual(response.status_code, 403)

    def test_superuser_can_clear_all_and_duplicate_post_is_idempotent(self):
        superuser = get_user_model().objects.create_superuser(
            username="root-operator", password="test", email="root@example.com"
        )
        self.client.force_login(superuser)
        current = self.root / "blog/blog_error.log"
        current.write_text("clear me", encoding="utf-8")
        key = uuid.uuid4()
        with override_settings(LOG_DIR=self.root):
            preview = self._preview(target_type="all", target="", kind="", scope="all")
            key = preview["idempotency_key"]
            payload = {
                "target_type": "all",
                "target": "",
                "kind": "",
                "scope": "all",
                "confirmation": "清空全部日志",
                "idempotency_key": key,
                "preview_token": preview["preview_token"],
            }
            first = self.client.post(reverse("observability:clear"), payload)
            second = self.client.post(reverse("observability:clear"), payload)
        self.assertRedirects(first, reverse("observability:audits"), fetch_redirect_response=False)
        self.assertRedirects(second, reverse("observability:audits"), fetch_redirect_response=False)
        self.assertEqual(current.stat().st_size, 0)
        self.assertEqual(LogClearAudit.objects.filter(idempotency_key=key).count(), 1)

    def test_partial_failure_message_names_failed_file_and_error(self):
        self._grant("view_logs", "manage_logs")
        selection = {
            "target_type": "file",
            "target": "blog_error",
            "kind": "error",
            "scope": "all",
        }
        key, preview_token = _issue_preview(selection)
        audit = LogClearAudit.objects.create(
            idempotency_key=key,
            user=self.user,
            target="file:blog_error:error",
            scope="all",
            succeeded=False,
            details={
                "failed_files": [
                    {
                        "file": "blog/blog_error.log.1",
                        "error": "模拟权限不足",
                    }
                ]
            },
        )
        with patch(
            "observability.views.clear_and_audit", return_value=(audit, True)
        ):
            response = self.client.post(
                reverse("observability:clear"),
                {
                    **selection,
                    "confirmation": "确认清理日志",
                    "idempotency_key": key,
                    "preview_token": preview_token,
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "日志部分清理失败，共 1 个文件")
        self.assertContains(response, "blog/blog_error.log.1：模拟权限不足")

    def test_duplicate_post_reports_running_claim_as_in_progress(self):
        self._grant("view_logs", "manage_logs")
        selection = {
            "target_type": "file",
            "target": "blog_error",
            "kind": "error",
            "scope": "current",
        }
        key, preview_token = _issue_preview(selection)
        LogClearAudit.objects.create(
            idempotency_key=key,
            user=self.user,
            target="file:blog_error:error",
            scope="current",
            succeeded=False,
            details={"state": "running"},
        )
        response = self.client.post(
            reverse("observability:clear"),
            {
                **selection,
                "confirmation": "确认清理日志",
                "idempotency_key": key,
                "preview_token": preview_token,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "相同清理请求仍在处理中")
        self.assertNotContains(response, "已返回原结果")
