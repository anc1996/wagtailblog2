from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from wagtail.models import PageViewRestriction

from search.models import (
    ContentSearchOperation,
    ContentSearchScopeJob,
    ContentSearchScopeJobStatus,
    ContentSearchState,
)
from search.services.scope import (
    _advance_scope_job,
    claim_scope_job,
    due_scope_job_ids,
    process_scope_job,
)
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


@override_settings(
    CONTENT_SEARCH_PRODUCER_ENABLED=True,
    CONTENT_SEARCH_CONSUMER_ENABLED=True,
    CONTENT_SEARCH_SCOPE_BATCH_SIZE=1,
)
class ContentSearchScopeJobTests(BlogLifecycleFixtureMixin, TestCase):
    def _publish(self, page):
        revision = page.save_revision()
        with self.captureOnCommitCallbacks(execute=True):
            revision.publish()
        page.refresh_from_db()
        return page

    def test_scope_job_emits_tombstone_once_and_advances_checkpoint(self):
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            page = self._publish(self._create_draft_page("范围任务正文"))
            PageViewRestriction.objects.create(
                page=page,
                restriction_type=PageViewRestriction.PASSWORD,
                password="scope-test-password",
            )

        job = ContentSearchScopeJob.objects.get(root_page_id=page.pk)
        self.assertEqual(process_scope_job(job.pk, limit=1), ContentSearchScopeJobStatus.PENDING)
        state = ContentSearchState.objects.get(page_id=page.pk)
        self.assertEqual(state.desired_operation, ContentSearchOperation.TOMBSTONE)
        self.assertEqual(state.content_version, 2)
        self.assertEqual(process_scope_job(job.pk, limit=1), ContentSearchScopeJobStatus.SUCCEEDED)
        self.assertEqual(ContentSearchState.objects.get(page_id=page.pk).content_version, 2)

    def test_scope_job_accepts_non_blog_root_and_reconciles_blog_descendants(self):
        """父级限制挂在 BlogIndexPage 时，仍需覆盖其 BlogPage 子树。"""
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            page = self._publish(self._create_draft_page("索引父级限制"))
            PageViewRestriction.objects.create(
                page=self.index,
                restriction_type=PageViewRestriction.PASSWORD,
                password="index-scope-password",
            )

        job = ContentSearchScopeJob.objects.get(root_page_id=self.index.pk)
        while process_scope_job(job.pk, limit=1) == ContentSearchScopeJobStatus.PENDING:
            pass
        state = ContentSearchState.objects.get(page_id=page.pk)
        self.assertEqual(state.desired_operation, ContentSearchOperation.TOMBSTONE)

    def test_scope_job_requeues_when_restriction_changes_during_processing(self):
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            page = self._publish(self._create_draft_page("范围重扫正文"))
            job = ContentSearchScopeJob.objects.create(root_page_id=page.pk)

        claimed, owner = claim_scope_job(job.pk)
        self.assertIsNotNone(claimed)
        self.assertTrue(owner)
        ContentSearchScopeJob.objects.filter(pk=job.pk).update(rescan_requested=True)
        self.assertEqual(_advance_scope_job(job.pk, owner, page.pk), ContentSearchScopeJobStatus.PENDING)
        job.refresh_from_db()
        self.assertEqual(job.checkpoint_page_id, 0)
        self.assertFalse(job.rescan_requested)

    def test_successful_batches_reset_failure_attempts(self):
        """大子树分页成功后不应继承早先失败批次的重试预算。"""
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            page = self._publish(self._create_draft_page("范围重试预算"))
        job = ContentSearchScopeJob.objects.create(root_page_id=page.pk, attempts=1)

        self.assertEqual(process_scope_job(job.pk, limit=1), ContentSearchScopeJobStatus.PENDING)
        job.refresh_from_db()
        self.assertEqual(job.attempts, 0)

    @override_settings(CONTENT_SEARCH_SCOPE_MAX_ATTEMPTS=2)
    def test_failed_batches_consume_retry_budget_then_dead(self):
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            page = self._publish(self._create_draft_page("范围失败重试"))
        job = ContentSearchScopeJob.objects.create(root_page_id=page.pk)

        with patch("search.services.scope._reconcile_page", side_effect=RuntimeError("boom")):
            self.assertEqual(process_scope_job(job.pk, limit=1), ContentSearchScopeJobStatus.RETRY)
            job.refresh_from_db()
            self.assertEqual(job.attempts, 1)
            self.assertEqual(process_scope_job(job.pk, limit=1), ContentSearchScopeJobStatus.DEAD)
        job.refresh_from_db()
        self.assertEqual(job.attempts, 2)

    @override_settings(CONTENT_SEARCH_SCOPE_MAX_ATTEMPTS=2)
    def test_expired_lease_reclaim_counts_as_failure(self):
        job = ContentSearchScopeJob.objects.create(
            root_page_id=1,
            status=ContentSearchScopeJobStatus.PROCESSING,
            lock_expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertEqual(due_scope_job_ids(), [job.pk])
        job.refresh_from_db()
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.status, ContentSearchScopeJobStatus.RETRY)
