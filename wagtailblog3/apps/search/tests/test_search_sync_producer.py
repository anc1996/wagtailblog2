from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from wagtail.models import PageViewRestriction

from blog.models import BlogPage
from search.models import (
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchScopeJob,
    ContentSearchState,
)
from search.services.outbox import ContentSearchOutboxService
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


@override_settings(CONTENT_SEARCH_PRODUCER_ENABLED=True)
class ContentSearchProducerLifecycleTests(BlogLifecycleFixtureMixin, TestCase):
    """使用真实 Wagtail Revision API 固定 WP3B 的发布事务和事件边界。"""

    def _publish(self, page):
        revision = page.save_revision()
        with self.captureOnCommitCallbacks(execute=True):
            revision.publish()
        page.refresh_from_db()
        return page

    def test_draft_save_does_not_create_public_search_state_or_event(self):
        page = self._create_draft_page("仅草稿关键词")
        page.save_revision()

        self.assertFalse(ContentSearchState.objects.filter(page_id=page.pk).exists())
        self.assertFalse(ContentSearchOutbox.objects.filter(page_id=page.pk).exists())

    def test_publish_and_republish_create_monotonic_upsert_events(self):
        with patch("search.services.outbox.schedule_content_search_wakeup") as wakeup:
            page = self._publish(self._create_draft_page("第一版正式正文"))
            first_event = ContentSearchOutbox.objects.get(page_id=page.pk, content_version=1)

            self.assertEqual(first_event.operation, ContentSearchOperation.UPSERT)
            self.assertTrue(first_event.searchable)
            self.assertEqual(first_event.mongo_content_id, page.mongo_content_id)
            self.assertEqual(len(first_event.content_hash), 64)

            page.body = self._markdown_body("第二版正式正文")
            page = self._publish(page)
            state = ContentSearchState.objects.get(page_id=page.pk)
            events = list(
                ContentSearchOutbox.objects.filter(page_id=page.pk).order_by("content_version")
            )

        self.assertEqual(state.content_version, 2)
        self.assertEqual([event.content_version for event in events], [1, 2])
        self.assertTrue(all(event.operation == ContentSearchOperation.UPSERT for event in events))
        self.assertEqual(wakeup.call_count, 2)

    def test_unpublish_and_delete_create_tombstones_and_keep_state(self):
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            page = self._publish(self._create_draft_page("待取消发布正文"))
            with self.captureOnCommitCallbacks(execute=True):
                page.unpublish()

            page.refresh_from_db()
            state = ContentSearchState.objects.get(page_id=page.pk)
            unpublish_event = ContentSearchOutbox.objects.get(
                page_id=page.pk,
                content_version=2,
            )
            self.assertFalse(page.live)
            self.assertEqual(state.desired_operation, ContentSearchOperation.TOMBSTONE)
            self.assertEqual(unpublish_event.operation, ContentSearchOperation.TOMBSTONE)
            self.assertFalse(unpublish_event.searchable)

            page.delete()

        delete_event = ContentSearchOutbox.objects.get(
            page_id=state.page_id,
            content_version=3,
        )
        state.refresh_from_db()
        self.assertEqual(state.content_version, 3)
        self.assertEqual(delete_event.operation, ContentSearchOperation.TOMBSTONE)
        self.assertFalse(BlogPage.objects.filter(pk=state.page_id).exists())

    def test_celery_enqueue_failure_keeps_pending_event(self):
        page = self._create_draft_page("唤醒失败正文")
        with patch(
            "search.services.outbox.wake_content_search_delivery.apply_async",
            side_effect=RuntimeError("broker unavailable"),
        ):
            self._publish(page)

        event = ContentSearchOutbox.objects.get(page_id=page.pk)
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.attempts, 0)

    def test_restriction_change_creates_one_scope_job_without_enumerating_pages(self):
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            page = self._publish(self._create_draft_page("访问限制正文"))
            PageViewRestriction.objects.create(
                page=page,
                restriction_type=PageViewRestriction.PASSWORD,
                password="test-only-password",
            )

        self.assertTrue(ContentSearchScopeJob.objects.filter(root_page_id=page.pk).exists())
        self.assertFalse(BlogPage.objects.live().public().filter(pk=page.pk).exists())


@override_settings(CONTENT_SEARCH_PRODUCER_ENABLED=True)
class ContentSearchProducerTransactionTests(BlogLifecycleFixtureMixin, TransactionTestCase):
    """验证 State 与 Outbox 受页面事务保护，并在并发写入时保持单调版本。"""

    def test_outer_transaction_rollback_removes_state_and_outbox(self):
        page = self._create_draft_page("回滚正文")
        revision = page.save_revision()

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                revision.publish()
                raise RuntimeError("force rollback")

        self.assertFalse(ContentSearchState.objects.filter(page_id=page.pk).exists())
        self.assertFalse(ContentSearchOutbox.objects.filter(page_id=page.pk).exists())

    def test_concurrent_publication_records_distinct_monotonic_versions(self):
        page = self._create_draft_page("并发版本正文")
        revision = page.save_revision()
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            revision.publish()
        page.refresh_from_db()

        barrier = Barrier(2)

        def record_publication():
            close_old_connections()
            try:
                published_page = BlogPage.objects.get(pk=page.pk)
                barrier.wait(timeout=10)
                return ContentSearchOutboxService.record_publication(published_page).content_version
            finally:
                close_old_connections()

        with (
            patch("search.services.outbox.schedule_content_search_wakeup"),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            versions = list(executor.map(lambda _unused: record_publication(), range(2)))

        state = ContentSearchState.objects.get(page_id=page.pk)
        self.assertEqual(sorted(versions), [2, 3])
        self.assertEqual(state.content_version, 3)
        self.assertEqual(
            ContentSearchOutbox.objects.filter(page_id=page.pk).count(),
            3,
        )
