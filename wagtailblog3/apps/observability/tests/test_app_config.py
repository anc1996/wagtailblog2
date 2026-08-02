"""验证 observability 应用的注册身份和权限配置。"""

import importlib.util
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase


class ObservabilityAppConfigTests(TestCase):
    """验证日志应用的注册身份、目录位置和权限迁移兼容性。"""
    def test_app_uses_apps_directory_with_stable_identity(self):
        config = apps.get_app_config("observability")

        self.assertEqual(config.name, "observability")
        self.assertEqual(config.label, "observability")
        self.assertEqual(config.module.__name__, "observability")
        self.assertEqual(
            Path(config.path).resolve(),
            (Path(settings.PROJECT_DIR) / "apps" / "observability").resolve(),
        )
        legacy_module = ".".join(("wagtailblog3", "observability"))
        self.assertIsNone(importlib.util.find_spec(legacy_module))

    def test_migration_content_type_and_permissions_keep_existing_label(self):
        self.assertTrue(
            MigrationRecorder.Migration.objects.filter(
                app="observability", name="0001_initial"
            ).exists()
        )
        self.assertEqual(
            list(
                ContentType.objects.filter(app_label="observability").values_list(
                    "model", flat=True
                )
            ),
            ["logclearaudit"],
        )
        self.assertEqual(
            set(
                Permission.objects.filter(
                    content_type__app_label="observability"
                ).values_list("codename", flat=True)
            ),
            {
                "add_logclearaudit",
                "change_logclearaudit",
                "delete_logclearaudit",
                "manage_logs",
                "view_logclearaudit",
                "view_logs",
            },
        )
