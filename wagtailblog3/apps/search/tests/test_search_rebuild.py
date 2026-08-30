import json
import os
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from search.models import (
    ContentSearchState,
    ContentSearchTarget,
    ContentSearchTargetRole,
    SearchIndexBuild,
    SearchIndexBuildStatus,
)
from search.services.elasticsearch import (
    ContentSearchBulkWriteResult,
    ContentSearchElasticsearchError,
)
from search.services.rebuild import (
    get_content_search_build_gate,
    rebuild_content_search_index,
    start_content_search_build,
)
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


@override_settings(
    CONTENT_SEARCH_PRODUCER_ENABLED=True,
    CONTENT_SEARCH_CONSUMER_ENABLED=True,
    CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
    CONTENT_SEARCH_REBUILD_BATCH_SIZE=1,
    CONTENT_SEARCH_REBUILD_MAX_BATCH_BYTES=4096,
)
class ContentSearchRebuildTests(BlogLifecycleFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            self.page = self._publish(self._create_draft_page("回填正式正文"))
        self.state = ContentSearchState.objects.get(page_id=self.page.pk)
        self.target = ContentSearchTarget.objects.create(
            target_id="content-rebuild-test",
            connection_name="default",
            index_name="wagtailblog-test-content-v001",
            role=ContentSearchTargetRole.BUILDING,
            required=False,
            enabled=False,
        )
        self.build = SearchIndexBuild.objects.create(
            target=self.target,
            mapping_version="content-v001-balanced",
        )

    def _formal_contents(self):
        return {self.page.mongo_content_id: self.mongo.get_blog_content(self.page.mongo_content_id)}

    def test_online_rebuild_uses_formal_batch_and_stops_in_catching_up(self):
        with (
            patch("search.services.rebuild.verify_content_search_index", return_value=True),
            patch("search.services.rebuild.BlogPublicationState.objects.in_bulk", return_value={}),
            patch(
                "search.services.rebuild.read_formal_contents_by_id",
                return_value=self._formal_contents(),
            ),
            patch(
                "search.services.rebuild.write_content_search_documents",
                return_value=ContentSearchBulkWriteResult(succeeded=1, superseded=0),
            ) as write_documents,
        ):
            start_content_search_build(self.target.target_id)
            _build, batches, last_batch = rebuild_content_search_index(
                self.target.target_id,
                batch_size=1,
                max_batch_bytes=4096,
            )

        self.build.refresh_from_db()
        self.target.refresh_from_db()
        self.assertEqual(batches, 2)
        self.assertTrue(last_batch.done)
        self.assertEqual(self.build.status, SearchIndexBuildStatus.CATCHING_UP)
        self.assertEqual(self.build.checkpoint_page_id, self.page.pk)
        self.assertEqual(self.build.scanned, 1)
        self.assertEqual(self.build.succeeded, 1)
        self.assertTrue(self.target.enabled)
        write_documents.assert_called_once()
        document = write_documents.call_args.args[1][0]
        self.assertEqual(document["content_version"], self.state.content_version)
        self.assertNotIn("mongo_content_id", document)

    def test_bulk_failure_keeps_previous_checkpoint_and_resume_continues(self):
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            second_page = self._publish(self._create_draft_page("第二篇回填正文"))
        second_state = ContentSearchState.objects.get(page_id=second_page.pk)
        formal_contents = self._formal_contents()
        formal_contents[second_page.mongo_content_id] = self.mongo.get_blog_content(
            second_page.mongo_content_id
        )
        responses = [
            ContentSearchBulkWriteResult(succeeded=1, superseded=0),
            ContentSearchElasticsearchError("es_bulk_item_http_503", retryable=True),
        ]

        def write_documents(_target, _documents):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with (
            patch("search.services.rebuild.verify_content_search_index", return_value=True),
            patch("search.services.rebuild.BlogPublicationState.objects.in_bulk", return_value={}),
            patch("search.services.rebuild.read_formal_contents_by_id", return_value=formal_contents),
            patch(
                "search.services.rebuild.write_content_search_documents",
                side_effect=write_documents,
            ),
        ):
            start_content_search_build(self.target.target_id)
            with self.assertRaises(Exception):
                rebuild_content_search_index(
                    self.target.target_id,
                    batch_size=1,
                    max_batch_bytes=4096,
                    max_batches=2,
                )

        self.build.refresh_from_db()
        self.assertEqual(self.build.status, SearchIndexBuildStatus.FAILED)
        self.assertEqual(self.build.checkpoint_page_id, self.page.pk)
        self.assertEqual(self.build.failed, 1)
        self.assertEqual(self.build.last_error_code, "es_bulk_item_http_503")
        self.assertEqual(second_state.content_version, 1)

        with (
            patch("search.services.rebuild.verify_content_search_index", return_value=True),
            patch("search.services.rebuild.BlogPublicationState.objects.in_bulk", return_value={}),
            patch("search.services.rebuild.read_formal_contents_by_id", return_value=formal_contents),
            patch(
                "search.services.rebuild.write_content_search_documents",
                return_value=ContentSearchBulkWriteResult(succeeded=1, superseded=0),
            ),
        ):
            start_content_search_build(self.target.target_id, resume=True)
            _build, _batches, last_batch = rebuild_content_search_index(
                self.target.target_id,
                batch_size=1,
                max_batch_bytes=4096,
            )

        self.build.refresh_from_db()
        self.assertTrue(last_batch.done)
        self.assertEqual(self.build.status, SearchIndexBuildStatus.CATCHING_UP)
        self.assertEqual(self.build.checkpoint_page_id, second_page.pk)

    def test_catch_up_requires_two_clean_checks_before_ready(self):
        self.target.enabled = True
        self.target.save(update_fields=("enabled",))
        self.build.status = SearchIndexBuildStatus.CATCHING_UP
        self.build.scan_upper_bound_page_id = self.page.pk
        self.build.checkpoint_page_id = self.page.pk
        self.build.save(update_fields=("status", "scan_upper_bound_page_id", "checkpoint_page_id"))
        clean_consistency = {"counts": {}, "samples": {}}

        with (
            patch("search.services.rebuild.reclaim_expired_content_search_deliveries"),
            patch("search.services.rebuild.materialize_content_search_deliveries"),
            patch(
                "search.services.rebuild._check_build_index_consistency",
                return_value=clean_consistency,
            ),
        ):
            first = get_content_search_build_gate(self.target.target_id, mutate=True)
            second = get_content_search_build_gate(self.target.target_id, mutate=True)

        self.assertTrue(first["clean"])
        self.assertEqual(first["catch_up_clean_streak"], 1)
        self.assertEqual(second["status"], SearchIndexBuildStatus.READY)
        self.assertEqual(second["catch_up_clean_streak"], 2)


class ContentSearchRebuildCommandTests(TestCase):
    @override_settings(CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content")
    def test_default_is_read_only_plan(self):
        target = ContentSearchTarget.objects.create(
            target_id="content-rebuild-command-test",
            connection_name="default",
            index_name="wagtailblog-test-content-v001",
            role=ContentSearchTargetRole.BUILDING,
            required=False,
            enabled=False,
        )
        SearchIndexBuild.objects.create(target=target, mapping_version="content-v001-balanced")
        output = StringIO()

        with patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"}):
            call_command(
                "search_rebuild_content_index",
                "--target",
                target.target_id,
                "--dry-run",
                stdout=output,
            )

        report = json.loads(output.getvalue())
        target.refresh_from_db()
        self.assertTrue(report["dry_run"])
        self.assertFalse(target.enabled)

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-prod-content",
        CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME="content_production",
        CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX="wagtailblog-prod-content",
        CONTENT_SEARCH_PRODUCTION_BACKUP_ROOT="/backups",
        CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED=True,
        CONTENT_SEARCH_PRODUCTION_REBUILD_ENABLED=True,
    )
    def test_production_confirm_requires_second_confirmation_before_rebuild(self):
        target = ContentSearchTarget.objects.create(
            target_id="prod-content-rebuild-command-test",
            connection_name="content_production",
            index_name="wagtailblog-prod-content-v001",
            role=ContentSearchTargetRole.BUILDING,
            required=False,
            enabled=False,
        )
        SearchIndexBuild.objects.create(target=target, mapping_version="content-v001-balanced")
        output = StringIO()

        with patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}), patch(
            "search.management.commands.search_rebuild_content_index.Path"
        ) as path, patch(
            "search.management.commands.search_rebuild_content_index.start_content_search_build"
        ) as start_build, self.assertRaises(CommandError):
            path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True
            call_command(
                "search_rebuild_content_index",
                "--target",
                target.target_id,
                "--confirm",
                "--backup-reference",
                "wagtailblog3-pre-search-20260811-221511",
                stdout=output,
            )

        start_build.assert_not_called()
        self.assertIn("second_production_confirmation_required", output.getvalue())
