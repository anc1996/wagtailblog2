"""Mongo 清理意图的事务边界与共享 Revision 指针回归测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from datetime import timedelta

from django.test import TestCase
from django.db import transaction
from django.utils import timezone

from blog.models import MongoCleanupIntent, MongoCleanupIntentStatus
from blog.signals import _record_page_cleanup_intents, _record_revision_cleanup_intent
from blog.tasks import (
    cleanup_mongo_intent,
    dispatch_pending_mongo_cleanup_retries,
    reclaim_expired_mongo_cleanup_intents,
)


class MongoCleanupIntentTaskTests(TestCase):
    """验证清理任务只删除无 MySQL 引用的 Mongo 指针。"""

    def create_intent(self, *, kind: str = "revision", pointer: str = "rev_1_test") -> MongoCleanupIntent:
        """创建独立意图，避免测试之间共享幂等键。"""
        return MongoCleanupIntent.objects.create(
            page_id=1,
            pointer=pointer,
            kind=kind,
            dedupe_key=f"{kind}:{pointer}",
        )

    @patch("blog.tasks.BlogPage.objects.filter")
    @patch("blog.tasks.Revision.objects.filter")
    @patch("blog.tasks.MongoManager")
    def test_worker_deletes_unreferenced_string_revision_pointer(
        self,
        mongo_manager: MagicMock,
        revision_filter: MagicMock,
        page_filter: MagicMock,
    ) -> None:
        """历史字符串主键在没有引用时交给 Mongo 网关，不能被 ObjectId 假设阻断。"""
        intent = self.create_intent()
        revision_filter.return_value.only.return_value.iterator.return_value = []
        page_filter.return_value.exists.return_value = False

        result = cleanup_mongo_intent.run(str(intent.intent_id))

        self.assertEqual(result["status"], "succeeded")
        mongo_manager.return_value.delete_single_revision.assert_called_once_with(
            "rev_1_test",
            raise_on_error=True,
        )
        intent.refresh_from_db()
        self.assertEqual(intent.status, MongoCleanupIntentStatus.SUCCEEDED)

    @patch("blog.tasks.cleanup_mongo_intent.apply_async")
    @patch("blog.tasks.Revision.objects.filter")
    def test_worker_defers_shared_revision_pointer(
        self,
        revision_filter: MagicMock,
        schedule_retry: MagicMock,
    ) -> None:
        """共享快照仍被 Revision 引用时不得删除，并保留后续回收机会。"""
        intent = self.create_intent(pointer="shared-pointer")
        revision_filter.return_value.only.return_value.iterator.return_value = [
            SimpleNamespace(content={"mongo_draft_pointer": "shared-pointer"}),
        ]

        result = cleanup_mongo_intent.run(str(intent.intent_id))

        self.assertEqual(result["status"], "referenced")
        intent.refresh_from_db()
        self.assertEqual(intent.status, MongoCleanupIntentStatus.RETRY)
        schedule_retry.assert_called_once()

    @patch("blog.tasks.cleanup_mongo_intent.apply_async")
    @patch("blog.tasks.BlogPage.objects.filter")
    @patch("blog.tasks.MongoManager")
    def test_worker_retries_mongo_failure(
        self,
        mongo_manager: MagicMock,
        page_filter: MagicMock,
        schedule_retry: MagicMock,
    ) -> None:
        """Mongo 故障必须留在 MySQL 意图表中，不能被误标为成功。"""
        intent = self.create_intent(kind="formal", pointer="formal-pointer")
        page_filter.return_value.exists.return_value = False
        mongo_manager.return_value.delete_blog_content.side_effect = ConnectionError("offline")

        result = cleanup_mongo_intent.run(str(intent.intent_id))

        self.assertEqual(result["status"], MongoCleanupIntentStatus.RETRY)
        intent.refresh_from_db()
        self.assertEqual(intent.status, MongoCleanupIntentStatus.RETRY)
        self.assertEqual(intent.attempts, 1)
        schedule_retry.assert_called_once()

    def test_expired_processing_lease_is_reclaimed(self) -> None:
        """Worker 崩溃留下的 processing 意图必须回到可重试状态。"""
        intent = self.create_intent(pointer="expired-pointer")
        intent.status = MongoCleanupIntentStatus.PROCESSING
        intent.locked_by = "dead-worker"
        intent.lock_expires_at = timezone.now() - timedelta(seconds=1)
        intent.save(update_fields=("status", "locked_by", "lock_expires_at"))

        self.assertEqual(reclaim_expired_mongo_cleanup_intents(), 1)
        intent.refresh_from_db()
        self.assertEqual(intent.status, MongoCleanupIntentStatus.RETRY)
        self.assertEqual(intent.locked_by, "")
        self.assertIsNone(intent.lock_expires_at)
        self.assertEqual(intent.lease_reclaims, 1)

    @patch("blog.tasks.cleanup_mongo_intent.apply_async")
    def test_dispatcher_schedules_due_intents_on_maintenance(self, dispatch: MagicMock) -> None:
        """Beat 只投递到期意图，且固定使用 maintenance 队列。"""
        intent = self.create_intent(pointer="due-pointer")
        intent.available_at = timezone.now() - timedelta(seconds=1)
        intent.save(update_fields=("available_at",))

        self.assertEqual(dispatch_pending_mongo_cleanup_retries(), 1)
        dispatch.assert_called_once_with(args=(str(intent.intent_id),), queue="maintenance")


class MongoCleanupIntentSignalTests(TestCase):
    """验证 Revision 删除信号只记录意图，Mongo I/O 不在信号阶段发生。"""

    @patch("blog.signals._schedule_mongo_cleanup")
    def test_revision_signal_records_target_level_intent(self, schedule_cleanup: MagicMock) -> None:
        """同一指针的重复删除事件共享一个目标级幂等意图。"""
        revision = SimpleNamespace(
            pk=101,
            object_id="38",
            content={"mongo_draft_pointer": "shared-pointer"},
        )

        _record_revision_cleanup_intent(revision)
        _record_revision_cleanup_intent(revision)

        intents = MongoCleanupIntent.objects.filter(dedupe_key="revision:shared-pointer")
        self.assertEqual(intents.count(), 1)
        schedule_cleanup.assert_called_with(intents.get().intent_id)

    @patch("blog.signals._schedule_mongo_cleanup")
    def test_page_cleanup_intent_rolls_back_with_outer_transaction(
        self,
        schedule_cleanup: MagicMock,
    ) -> None:
        """外层删除事务回滚时，MySQL 意图和后续 Mongo 清理均不得保留。"""
        page = SimpleNamespace(pk=38, mongo_content_id="formal-pointer")

        with transaction.atomic():
            _record_page_cleanup_intents(page)
            self.assertTrue(MongoCleanupIntent.objects.filter(pointer="formal-pointer").exists())
            transaction.set_rollback(True)

        self.assertFalse(MongoCleanupIntent.objects.filter(pointer="formal-pointer").exists())
        schedule_cleanup.assert_called_once()
