from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from observability.elasticsearch_logs import (
    LogIndexCleanupPlan,
    LogIndexDeleteResult,
    LogIndexSyncError,
)
from observability.models import LogClearAudit, LogIndexSyncJob
from observability.tasks import enqueue_log_index_sync, run_log_index_sync


TEST_ES_SETTINGS = {
    "ENABLED": True,
    "READ_INDEX": "wagtailblog-test-logs-read",
    "WRITE_INDEX": "wagtailblog-test-logs-write",
    "DELETE_SYNC_MAX_BYTES": 1024,
}


def cleanup_plan():
    return LogIndexCleanupPlan(
        selectors=[
            {
                "bool": {
                    "filter": [
                        {"term": {"source_key": "blog_error"}},
                        {"term": {"source_path": "blog/blog_error.log"}},
                        {"term": {"rotation": 0}},
                    ]
                }
            }
        ],
        cutoff=datetime(2026, 8, 5, 8, 0, 0),
        spec_keys=("blog_error",),
        estimated_bytes=100,
    )


@override_settings(ELASTICSEARCH_LOGGING=TEST_ES_SETTINGS)
class LogIndexSyncTaskTests(TestCase):
    def setUp(self):
        self.audit = LogClearAudit.objects.create(
            target="file:blog_error:*",
            target_type="file",
            scope="current",
            state="completed",
            succeeded=True,
        )

    def _job(self):
        return LogIndexSyncJob.objects.create(
            audit=self.audit,
            selector=cleanup_plan().as_payload(),
            next_retry_at=timezone.now(),
        )

    def test_enqueue_is_one_to_one_and_marks_audit_pending(self):
        with patch("observability.tasks.sync_log_index.apply_async") as apply_async:
            with self.captureOnCommitCallbacks(execute=True):
                first = enqueue_log_index_sync(self.audit, cleanup_plan())
            with self.captureOnCommitCallbacks(execute=True):
                second = enqueue_log_index_sync(self.audit, cleanup_plan())

        self.audit.refresh_from_db()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(LogIndexSyncJob.objects.count(), 1)
        self.assertEqual(self.audit.index_sync_state, "pending")
        self.assertEqual(apply_async.call_count, 2)

    def test_success_runs_delayed_compensation_before_completion(self):
        job = self._job()
        with patch(
            "observability.tasks.delete_logs_by_plan",
            side_effect=(
                LogIndexDeleteResult(deleted=3),
                LogIndexDeleteResult(deleted=1),
            ),
        ):
            first_state = run_log_index_sync(job.pk)
            job.refresh_from_db()
            self.assertEqual(first_state, "pending")
            self.assertEqual(job.selector["phase"], "compensation")
            self.assertEqual(job.deleted_documents, 3)
            job.next_retry_at = timezone.now() - timedelta(seconds=1)
            job.save(update_fields=("next_retry_at",))
            second_state = run_log_index_sync(job.pk)

        job.refresh_from_db()
        self.audit.refresh_from_db()
        self.assertEqual(second_state, "completed")
        self.assertEqual(job.deleted_documents, 4)
        self.assertEqual(self.audit.index_sync_state, "completed")
        self.assertEqual(self.audit.index_sync_deleted, 4)

    def test_transient_failure_is_scheduled_for_retry(self):
        job = self._job()
        with patch(
            "observability.tasks.delete_logs_by_plan",
            side_effect=LogIndexSyncError("offline"),
        ):
            state = run_log_index_sync(job.pk)

        job.refresh_from_db()
        self.assertEqual(state, "pending")
        self.assertIsNotNone(job.next_retry_at)
        self.assertEqual(job.attempts, 1)

    def test_permission_failure_enters_dead_letter(self):
        job = self._job()
        with patch(
            "observability.tasks.delete_logs_by_plan",
            side_effect=LogIndexSyncError("forbidden", status_code=403),
        ):
            state = run_log_index_sync(job.pk)

        job.refresh_from_db()
        self.audit.refresh_from_db()
        self.assertEqual(state, "dead_letter")
        self.assertEqual(self.audit.index_sync_state, "dead_letter")
        self.assertIsNotNone(job.dead_letter_at)
