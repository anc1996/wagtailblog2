import uuid
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from blog.models import MarkdownImportArtifactCleanupStatus
from blog.tasks import (
    cleanup_markdown_import_artifact,
    dispatch_markdown_import_cleanup_retries,
)
from wagtailblog3.settings.database import get_celery_config


class MarkdownImportCleanupTaskTests(SimpleTestCase):
    def _artifact(self, *, attempts=0, status=MarkdownImportArtifactCleanupStatus.RETRY):
        return SimpleNamespace(
            pk=17,
            artifact_id=uuid.uuid4(),
            cleanup_status=status,
            cleanup_attempts=attempts,
            cleanup_next_attempt_at=None,
            storage_alias="default",
            object_name="markdown-import/17/photo.png",
            media_model="blog.blogimage",
            media_object_id=23,
            save=mock.Mock(),
        )

    @mock.patch("blog.tasks.cleanup_artifact_object", return_value=True)
    @mock.patch("blog.tasks.MarkdownImportArtifact.objects")
    def test_cleanup_is_idempotent_when_object_or_model_is_already_gone(
        self, manager, cleanup
    ):
        artifact = self._artifact()
        manager.filter.return_value.first.return_value = artifact

        with mock.patch("blog.tasks.storages", {"default": object()}):
            result = cleanup_markdown_import_artifact(str(artifact.artifact_id))

        self.assertEqual(result["status"], "cleaned")
        self.assertEqual(artifact.cleanup_attempts, 1)
        cleanup.assert_called_once()
        artifact.save.assert_called_once()

    @mock.patch("blog.tasks.cleanup_artifact_object", return_value=False)
    @mock.patch("blog.tasks.MarkdownImportArtifact.objects")
    @override_settings(
        MARKDOWN_IMPORT_CLEANUP_MAX_ATTEMPTS=2,
        MARKDOWN_IMPORT_CLEANUP_RETRY_BASE_SECONDS=1,
    )
    def test_cleanup_uses_bounded_backoff_and_keeps_retry_audit(
        self, manager, cleanup
    ):
        artifact = self._artifact()
        manager.filter.return_value.first.return_value = artifact

        with mock.patch("blog.tasks.storages", {"default": object()}):
            first = cleanup_markdown_import_artifact(str(artifact.artifact_id))
            second = cleanup_markdown_import_artifact(str(artifact.artifact_id))
            exhausted = cleanup_markdown_import_artifact(str(artifact.artifact_id))

        self.assertEqual(first["status"], "retry")
        self.assertEqual(second["status"], "retry")
        self.assertEqual(exhausted["status"], "exhausted")
        self.assertEqual(artifact.cleanup_attempts, 2)
        self.assertIsNone(artifact.cleanup_next_attempt_at)
        self.assertEqual(artifact.cleanup_status, MarkdownImportArtifactCleanupStatus.RETRY)

    @mock.patch("blog.tasks.cleanup_markdown_import_artifact.delay")
    @mock.patch("blog.tasks.MarkdownImportArtifact.objects")
    def test_dispatch_only_enqueues_explicit_due_artifact_ids(self, manager, delay):
        artifact_id = uuid.uuid4()
        queryset = mock.MagicMock()
        queryset.filter.return_value = queryset
        queryset.order_by.return_value = queryset
        queryset.values_list.return_value = queryset
        queryset.__getitem__.return_value = [artifact_id]
        manager.filter.return_value = queryset

        result = dispatch_markdown_import_cleanup_retries()

        self.assertEqual(result, {"status": "dispatched", "count": 1})
        delay.assert_called_once_with(str(artifact_id))

    def test_invalid_or_missing_artifact_is_safe_noop(self):
        self.assertEqual(
            cleanup_markdown_import_artifact("not-a-uuid")["status"],
            "invalid_artifact_id",
        )

    def test_tasks_use_existing_maintenance_route_and_beat(self):
        config = get_celery_config("UTC", "redis", 6379, "")

        self.assertEqual(
            config["CELERY_TASK_ROUTES"][
                "blog.tasks.cleanup_markdown_import_artifact"
            ]["queue"],
            settings.CELERY_MAINTENANCE_QUEUE,
        )
        self.assertEqual(
            config["CELERY_TASK_ROUTES"][
                "blog.tasks.assemble_markdown_import_session"
            ]["queue"],
            settings.CELERY_MAINTENANCE_QUEUE,
        )
        self.assertEqual(
            config["CELERY_BEAT_SCHEDULE"][
                "dispatch-markdown-import-cleanup-retries"
            ]["options"]["queue"],
            settings.CELERY_MAINTENANCE_QUEUE,
        )
        self.assertEqual(
            config["CELERY_BEAT_SCHEDULE"][
                "expire-markdown-import-sessions"
            ]["options"]["queue"],
            settings.CELERY_MAINTENANCE_QUEUE,
        )
