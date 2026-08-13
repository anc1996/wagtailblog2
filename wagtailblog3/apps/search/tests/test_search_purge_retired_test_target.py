import json
import os
from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from search.models import (
    ContentSearchTarget,
    ContentSearchTargetRole,
    SearchIndexBuild,
    SearchIndexBuildStatus,
)


@override_settings(
    CONTENT_SEARCH_CONNECTION_NAME="default",
    CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
    CONTENT_SEARCH_READ_ALIAS="wagtailblog-test-content-read",
)
class PurgeRetiredTestContentTargetTests(TestCase):
    def setUp(self):
        self.target = ContentSearchTarget.objects.create(
            target_id="content-v003",
            connection_name="default",
            index_name="wagtailblog-test-content-v003",
            role=ContentSearchTargetRole.SERVING,
            enabled=True,
        )
        self.build = SearchIndexBuild.objects.create(
            target=self.target,
            mapping_version="content-v003-balanced",
            status=SearchIndexBuildStatus.SERVING,
        )

    def _call(self, *args, **kwargs):
        return call_command(
            "search_purge_retired_test_content_target",
            "--target",
            self.target.target_id,
            *args,
            **kwargs,
        )

    def test_dry_run_refuses_a_non_test_environment(self):
        output = StringIO()
        with patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}):
            self._call(stdout=output)

        report = json.loads(output.getvalue())
        self.assertFalse(report["ready_for_confirm"])
        self.assertIn("test_environment_required", report["refused"])

    def test_retire_dry_run_refuses_an_alias_target(self):
        output = StringIO()
        with (
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_purge_retired_test_content_target.get_content_search_read_alias_indices",
                return_value=(self.target.index_name,),
            ),
        ):
            self._call("--retire-only", stdout=output)

        report = json.loads(output.getvalue())
        self.assertFalse(report["ready_for_confirm"])
        self.assertIn("target_still_serving_alias", report["refused"])

    def test_confirm_retire_disables_only_the_historical_target(self):
        output = StringIO()
        with (
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_purge_retired_test_content_target.get_content_search_read_alias_indices",
                return_value=("wagtailblog-test-content-v004",),
            ),
        ):
            self._call("--retire-only", "--confirm", stdout=output)

        report = json.loads(output.getvalue())
        self.assertTrue(report["target_retired"])
        self.target.refresh_from_db()
        self.build.refresh_from_db()
        self.assertFalse(self.target.enabled)
        self.assertEqual(self.target.role, ContentSearchTargetRole.RETIRED)
        self.assertEqual(self.build.status, SearchIndexBuildStatus.RETIRED)

    def test_purge_refuses_until_the_target_has_been_retired(self):
        output = StringIO()
        with (
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_purge_retired_test_content_target.get_content_search_read_alias_indices",
                return_value=("wagtailblog-test-content-v004",),
            ),
        ):
            self._call(stdout=output)

        report = json.loads(output.getvalue())
        self.assertFalse(report["ready_for_confirm"])
        self.assertIn("target_must_be_retired_before_purge", report["refused"])

    def test_confirm_purge_deletes_exact_retired_index_and_records(self):
        self.target.enabled = False
        self.target.role = ContentSearchTargetRole.RETIRED
        self.target.save(update_fields=("enabled", "role"))
        self.build.status = SearchIndexBuildStatus.RETIRED
        self.build.save(update_fields=("status",))
        client = Mock()
        client.indices.exists.return_value = True
        client.indices.exists_index_template.return_value = True
        output = StringIO()
        with (
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_purge_retired_test_content_target.get_content_search_read_alias_indices",
                return_value=("wagtailblog-test-content-v004",),
            ),
            patch(
                "search.management.commands.search_purge_retired_test_content_target.get_content_search_client",
                return_value=client,
            ),
        ):
            self._call("--confirm", stdout=output)

        client.indices.delete.assert_called_once_with(index=self.target.index_name)
        self.assertFalse(ContentSearchTarget.objects.filter(pk=self.target.pk).exists())
        report = json.loads(output.getvalue())
        self.assertTrue(report["mysql_records_deleted"])
