from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.db import close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from wagtail.models import PageViewRestriction

from blog.models import BlogPage, BlogPublicationState, PageDeletionIntent, PageDeletionIntentStatus
from search.models import (
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchScopeJob,
    ContentSearchState,
)
from search.services.outbox import ContentSearchOutboxService, schedule_content_search_wakeup
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

    def test_modern_revision_blocks_legacy_collection_write_before_state_exists(self):
        """导入页面尚未建立 State 时，带不可变 Revision 也不得回写旧集合。"""
        page = self._create_draft_page("导入现代正文")
        page.save_revision()
        page.mongo_content_id = "legacy-pointer-from-import"
        page.body = self._markdown_body("编辑后的现代草稿")

        page.save()

        self.assertEqual(self.mongo.live_documents, {})
        self.assertFalse(BlogPublicationState.objects.filter(page_id=page.pk).exists())

    def test_publish_signal_repairs_state_when_live_flag_is_not_set_on_instance(self):
        """Wagtail 8.0 信号 live 时序异常时仍按 Revision 补齐正式 State。"""
        page = self._create_draft_page("信号时序正文")
        revision = page.save_revision()
        # 模拟数据库页面已为 live、而信号实例仍是旧快照的时序。
        BlogPage.objects.filter(pk=page.pk).update(live=True)
        page.live = False

        from search.signals import record_blog_page_published

        with patch("search.services.outbox.schedule_content_search_wakeup"):
            record_blog_page_published(BlogPage, page, revision=revision)

        state = BlogPublicationState.objects.get(page_id=page.pk)
        event = ContentSearchOutbox.objects.get(page_id=page.pk)
        self.assertEqual(state.published_body_version_id, revision.content["mongo_body_version_id"])
        self.assertEqual(event.body_version_id, state.published_body_version_id)
        self.assertEqual(event.operation, ContentSearchOperation.UPSERT)
        self.assertTrue(event.searchable)

    def test_publish_and_republish_create_monotonic_upsert_events(self):
        with patch("search.services.outbox.schedule_content_search_wakeup") as wakeup:
            page = self._publish(self._create_draft_page("第一版正式正文"))
            first_event = ContentSearchOutbox.objects.get(page_id=page.pk, content_version=1)

            self.assertEqual(first_event.operation, ContentSearchOperation.UPSERT)
            self.assertTrue(first_event.searchable)
            self.assertEqual(first_event.mongo_content_id, page.mongo_content_id)
            self.assertEqual(len(first_event.content_hash), 64)
            publication_state = BlogPublicationState.objects.get(page_id=page.pk)
            self.assertEqual(
                first_event.body_version_id,
                publication_state.published_body_version_id,
            )
            self.assertEqual(first_event.publication_generation, 1)

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

    def test_publish_state_and_outbox_are_visible_in_same_transaction(self):
        page = self._create_draft_page("事务一致性正文")
        revision = page.save_revision()
        observed = {}

        def observe(sender, instance, **kwargs):
            from blog.models import BlogPublicationState

            state = BlogPublicationState.objects.get(page_id=instance.pk)
            event = ContentSearchOutbox.objects.get(page_id=instance.pk)
            observed.update(
                in_atomic=connection.in_atomic_block,
                state_body=state.published_body_version_id,
                event_body=event.body_version_id,
                generation=state.publication_generation,
            )

        from django.db import connection
        from wagtail.signals import page_published

        page_published.connect(observe, sender=BlogPage, weak=False)
        self.addCleanup(page_published.disconnect, observe, BlogPage)
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            revision.publish()

        self.assertTrue(observed["in_atomic"])
        self.assertEqual(observed["state_body"], observed["event_body"])
        self.assertEqual(observed["generation"], 1)

    def test_unpublish_and_delete_create_tombstones_and_keep_state(self):
        with (
            patch("search.services.outbox.schedule_content_search_wakeup"),
            patch("blog.tasks.process_page_deletion.apply_async"),
        ):
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
        # 删除请求只登记页面级意图；页面需等 Worker 完成 Mongo 清理后才物理删除。
        self.assertTrue(BlogPage.objects.filter(pk=state.page_id).exists())
        intent = PageDeletionIntent.objects.get(page_id=state.page_id)
        self.assertEqual(intent.status, PageDeletionIntentStatus.DELETING)

    def test_direct_delete_of_live_page_advances_generation_and_writes_tombstone(self):
        """直接删除仍在线页面时，墓碑必须拥有更高公开代次，避免迟到 upsert 复活。"""
        with (
            patch("search.services.outbox.schedule_content_search_wakeup"),
            patch("blog.tasks.process_page_deletion.apply_async"),
        ):
            page = self._publish(self._create_draft_page("直接删除在线正文"))
            state_before = BlogPublicationState.objects.get(page_id=page.pk)
            self.assertEqual(state_before.publication_generation, 1)

            page.delete()

        state = ContentSearchState.objects.get(page_id=page.pk)
        delete_event = ContentSearchOutbox.objects.filter(
            page_id=page.pk,
            operation=ContentSearchOperation.TOMBSTONE,
        ).order_by("-content_version").first()
        self.assertTrue(BlogPage.objects.filter(pk=page.pk).exists())
        intent = PageDeletionIntent.objects.get(page_id=page.pk)
        self.assertEqual(intent.status, PageDeletionIntentStatus.DELETING)
        self.assertGreaterEqual(delete_event.publication_generation, 2)
        self.assertEqual(state.publication_generation, delete_event.publication_generation)

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

    @override_settings(CELERY_MAINTENANCE_QUEUE="search-test-maintenance")
    def test_wakeup_uses_configured_maintenance_queue(self):
        with patch("search.services.outbox.wake_content_search_delivery.apply_async") as apply_async:
            schedule_content_search_wakeup("event-uuid")

        apply_async.assert_called_once_with(
            kwargs={"event_id": "event-uuid"},
            queue="search-test-maintenance",
        )

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
