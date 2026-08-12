import json
import os
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from search.models import (
    ContentSearchTarget,
    ContentSearchTargetRole,
    SearchIndexBuild,
    SearchIndexBuildStatus,
)


RETIRE_SETTINGS = {
    "CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED": True,
    "CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX": "wagtailblog-prod-content",
    "CONTENT_SEARCH_INDEX_PREFIX": "wagtailblog-prod-content",
    "CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME": "content_production",
    "CONTENT_SEARCH_CONNECTION_NAME": "content_production",
    "CONTENT_SEARCH_READ_ALIAS": "wagtailblog-prod-content-read",
}


@override_settings(**RETIRE_SETTINGS)
class ProductionContentTargetRetirementTests(TestCase):
    backup_reference = "wagtailblog3-pre-search-20260811-221511"

    def setUp(self):
        self.old_target = ContentSearchTarget.objects.create(
            target_id="prod-content-v001",
            connection_name="content_production",
            index_name="wagtailblog-prod-content-v001",
            role=ContentSearchTargetRole.BUILDING,
            required=False,
            enabled=True,
        )
        self.old_build = SearchIndexBuild.objects.create(
            target=self.old_target,
            mapping_version="v001",
            status=SearchIndexBuildStatus.READY,
        )
        self.serving_target = ContentSearchTarget.objects.create(
            target_id="prod-content-v002",
            connection_name="content_production",
            index_name="wagtailblog-prod-content-v002",
            role=ContentSearchTargetRole.SERVING,
            required=True,
            enabled=True,
        )
        SearchIndexBuild.objects.create(
            target=self.serving_target,
            mapping_version="v002",
            status=SearchIndexBuildStatus.SERVING,
        )

    def _call(self, backup_root, *args, **kwargs):
        with self.settings(CONTENT_SEARCH_PRODUCTION_BACKUP_ROOT=backup_root):
            return call_command(
                "search_retire_production_content_target",
                "--target",
                self.old_target.target_id,
                "--backup-reference",
                self.backup_reference,
                *args,
                **kwargs,
            )

    def _backup_root(self, directory):
        backup = Path(directory) / self.backup_reference
        backup.mkdir()
        (backup / "checksums.sha256").write_text("verified\n", encoding="utf-8")
        return directory

    def test_dry_run_allows_only_non_serving_caught_up_target(self):
        output = StringIO()
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}),
            patch(
                "search.management.commands.search_retire_production_content_target.get_content_search_read_alias_indices",
                return_value=(self.serving_target.index_name,),
            ),
        ):
            self._call(self._backup_root(directory), stdout=output)

        report = json.loads(output.getvalue())
        self.assertTrue(report["ready_for_confirm"])
        self.assertFalse(report["target_retired"])
        self.old_target.refresh_from_db()
        self.assertTrue(self.old_target.enabled)

    def test_dry_run_refuses_target_still_used_by_alias(self):
        output = StringIO()
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}),
            patch(
                "search.management.commands.search_retire_production_content_target.get_content_search_read_alias_indices",
                return_value=(self.old_target.index_name,),
            ),
        ):
            self._call(self._backup_root(directory), stdout=output)

        report = json.loads(output.getvalue())
        self.assertFalse(report["ready_for_confirm"])
        self.assertIn("target_still_serving_alias", report["refused"])

    @override_settings(CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED=False)
    def test_dry_run_requires_enabled_production_cluster(self):
        output = StringIO()
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}),
            patch(
                "search.management.commands.search_retire_production_content_target.get_content_search_read_alias_indices",
                return_value=(self.serving_target.index_name,),
            ),
        ):
            self._call(self._backup_root(directory), stdout=output)

        report = json.loads(output.getvalue())
        self.assertFalse(report["ready_for_confirm"])
        self.assertIn("production_existing_cluster_required", report["refused"])

    def test_confirm_retires_target_and_build_without_deleting_index(self):
        output = StringIO()
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}),
            patch(
                "search.management.commands.search_retire_production_content_target.get_content_search_read_alias_indices",
                return_value=(self.serving_target.index_name,),
            ),
        ):
            self._call(
                self._backup_root(directory),
                "--confirm",
                "--confirm-production-target-retire",
                stdout=output,
            )

        report = json.loads(output.getvalue())
        self.assertTrue(report["target_retired"])
        self.assertFalse(report["index_deleted"])
        self.old_target.refresh_from_db()
        self.old_build.refresh_from_db()
        self.serving_target.refresh_from_db()
        self.assertFalse(self.old_target.enabled)
        self.assertFalse(self.old_target.required)
        self.assertEqual(self.old_target.role, ContentSearchTargetRole.RETIRED)
        self.assertEqual(self.old_build.status, SearchIndexBuildStatus.RETIRED)
        self.assertTrue(self.serving_target.enabled)
        self.assertEqual(self.serving_target.role, ContentSearchTargetRole.SERVING)

    def test_confirm_requires_second_production_confirmation(self):
        output = StringIO()
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}),
            patch(
                "search.management.commands.search_retire_production_content_target.get_content_search_read_alias_indices",
                return_value=(self.serving_target.index_name,),
            ),
            self.assertRaises(CommandError),
        ):
            self._call(self._backup_root(directory), "--confirm", stdout=output)

        self.old_target.refresh_from_db()
        self.assertTrue(self.old_target.enabled)
