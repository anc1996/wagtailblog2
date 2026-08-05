"""验证日志概览缓存和清理审计的后台行为。"""

from pathlib import Path
import tempfile
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from observability.models import LogClearAudit
from observability.services import OVERVIEW_CACHE_KEY, get_overview


TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}


@override_settings(STORAGES=TEST_STORAGES)
class BatchFiveOverviewAndAuditTests(TestCase):
    """验证概览缓存、审计默认值和后台筛选上下文。"""
    def test_manual_refresh_invalidates_overview_cache(self):
        cache.set(OVERVIEW_CACHE_KEY, {"stale": True}, timeout=30)
        with patch("observability.services.read_logs", return_value=type("Result", (), {"records": []})()), patch(
            "observability.services.iter_log_files", return_value=()
        ):
            result = get_overview(refresh=True)
        self.assertNotEqual(result, {"stale": True})
        self.assertIsNotNone(cache.get(OVERVIEW_CACHE_KEY))

    def test_audit_legacy_rows_have_safe_state_defaults(self):
        audit = LogClearAudit.objects.create(target="legacy", scope="all")
        self.assertEqual(audit.state, "completed")
        self.assertEqual(audit.target_type, "legacy")
        self.assertEqual(audit.kind, "")

    def test_audit_query_context_preserves_filters(self):
        user = __import__("django.contrib.auth", fromlist=["get_user_model"]).get_user_model().objects.create_user(username="audit-user")
        Permission = __import__("django.contrib.auth.models", fromlist=["Permission"]).Permission
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="wagtailadmin", codename="access_admin"
            ),
            Permission.objects.get(
                content_type__app_label="observability", codename="view_logs"
            ),
        )
        self.client.force_login(user)
        response = self.client.get(reverse("observability:audits"), {"state": "partial", "target": "blog", "page": "2"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("state=partial", response.context["audit_query"])
        self.assertIn("target=blog", response.context["audit_query"])
        self.assertNotIn("page=", response.context["audit_query"])
