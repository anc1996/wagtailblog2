"""取消意图必须核验锁内状态，不能将已进入回收流程的页面假性恢复。"""

from __future__ import annotations

from datetime import timedelta
from threading import Event, Thread
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from django.db import close_old_connections, connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from wagtail.models import Locale, Page

from external_content.deletion import cancel_pending_deletion_intent
from external_content.exceptions import ExternalContentOperationBlockedError
from external_content.locking import lock_page_lifecycle_hierarchy
from external_content.models import DeletionIntentStatus, ExternalContentDeletionIntent


class DeletionCancellationTests(TransactionTestCase):
    """使用真实 MySQL 行锁与合成页面，不读取历史页或启动 GC。"""

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(connection.vendor, "mysql")
        locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
        root = Page.get_first_root_node()
        if root is None:
            root = Page.add_root(title="取消意图测试根", locale=locale)
        self.page = root.add_child(
            instance=Page(
                title="取消意图测试页",
                slug=f"cancel-{uuid4().hex[:10]}",
                locale=locale,
                live=False,
            )
        )

    def _make_intent(
        self, status: str, **overrides: Any
    ) -> ExternalContentDeletionIntent:
        values = {
            "page_id": self.page.pk,
            "model_label": "wagtailcore.page",
            "status": status,
            "safe_gc_after": timezone.now() + timedelta(days=7),
            "manifest": {"body_version_ids": [], "revision_ids": [], "versions": []},
            "generation": 3,
        }
        values.update(overrides)
        return ExternalContentDeletionIntent.objects.create(**values)

    def test_string_uuid_cancels_the_locked_pending_intent(self) -> None:
        """入口不能因字符串与 UUID 类型不同而误报目标不存在。"""
        intent = self._make_intent(DeletionIntentStatus.PENDING)
        returned = cancel_pending_deletion_intent(str(intent.pk))
        intent.refresh_from_db()
        self.assertEqual(returned.pk, intent.pk)
        self.assertEqual(intent.status, DeletionIntentStatus.CANCELLED)
        self.assertTrue(Page.objects.filter(pk=self.page.pk).exists())

    def test_allowed_states_clear_all_lease_fields_and_preserve_audit(self) -> None:
        """取消必须清理整套认领围栏，不能遗留 claimed_at 或旧令牌。"""
        for status in (DeletionIntentStatus.PENDING, DeletionIntentStatus.FAILED):
            with self.subTest(status=status):
                intent = self._make_intent(
                    status,
                    claimed_at=timezone.now() - timedelta(minutes=3),
                    claimed_worker="synthetic-worker",
                    claim_expires_at=timezone.now() - timedelta(minutes=1),
                    claim_token=uuid4(),
                    failed_versions={"synthetic-version": "fixture-failure"},
                    retry_count=2,
                )
                manifest = intent.manifest
                safe_gc_after = intent.safe_gc_after
                cancel_pending_deletion_intent(intent.pk)
                intent.refresh_from_db()
                self.assertEqual(intent.status, DeletionIntentStatus.CANCELLED)
                for field in (
                    "claimed_at",
                    "claimed_worker",
                    "claim_expires_at",
                    "claim_token",
                ):
                    self.assertIsNone(getattr(intent, field), field)
                self.assertEqual(intent.manifest, manifest)
                self.assertEqual(intent.safe_gc_after, safe_gc_after)
                self.assertEqual(intent.generation, 3)
                self.assertEqual(intent.retry_count, 2)
                self.assertEqual(
                    intent.failed_versions, {"synthetic-version": "fixture-failure"}
                )

    def test_gc_states_and_unknown_states_cannot_be_cancelled(self) -> None:
        """状态白名单不能被未过期租约的单一条件替代。"""
        scenarios = (
            (DeletionIntentStatus.MYSQL_DELETED, None),
            (DeletionIntentStatus.GC_ELIGIBLE, None),
            (DeletionIntentStatus.COMPLETED, None),
            (DeletionIntentStatus.CLAIMED, timezone.now() + timedelta(minutes=1)),
            (DeletionIntentStatus.CLAIMED, timezone.now() - timedelta(minutes=1)),
            (DeletionIntentStatus.CLAIMED, None),
            ("future_unknown_state", None),
        )
        for status, expires_at in scenarios:
            with self.subTest(status=status, expires_at=expires_at):
                intent = self._make_intent(status, claim_expires_at=expires_at)
                before = ExternalContentDeletionIntent.objects.values().get(pk=intent.pk)
                with self.assertRaises(ExternalContentOperationBlockedError):
                    cancel_pending_deletion_intent(intent.pk)
                self.assertEqual(
                    ExternalContentDeletionIntent.objects.values().get(pk=intent.pk),
                    before,
                )

    def test_cancelled_retry_is_a_read_only_no_op(self) -> None:
        """已取消请求重试不得修改审计时间或其他记录字段。"""
        intent = self._make_intent(DeletionIntentStatus.CANCELLED)
        before = ExternalContentDeletionIntent.objects.values().get(pk=intent.pk)
        returned = cancel_pending_deletion_intent(intent.pk)
        self.assertEqual(returned.pk, intent.pk)
        self.assertEqual(
            ExternalContentDeletionIntent.objects.values().get(pk=intent.pk), before
        )

    def test_failed_intent_with_collected_versions_cannot_fake_restoration(self) -> None:
        """已有回收墓碑表示正文可能已丢失，存活页面也不能据此解除冻结。"""
        intent = self._make_intent(
            DeletionIntentStatus.FAILED, collected_versions=["synthetic-collected"]
        )
        before = ExternalContentDeletionIntent.objects.values().get(pk=intent.pk)
        with self.assertRaises(ExternalContentOperationBlockedError):
            cancel_pending_deletion_intent(intent.pk)
        self.assertEqual(
            ExternalContentDeletionIntent.objects.values().get(pk=intent.pk), before
        )

    def test_missing_page_cannot_release_a_pending_intent(self) -> None:
        intent = self._make_intent(
            DeletionIntentStatus.PENDING, page_id=self.page.pk + 1000000
        )
        with self.assertRaises(Page.DoesNotExist):
            cancel_pending_deletion_intent(intent.pk)
        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.PENDING)

    def test_save_failure_rolls_back_status_and_all_lease_fields(self) -> None:
        intent = self._make_intent(
            DeletionIntentStatus.PENDING,
            claimed_at=timezone.now(),
            claimed_worker="synthetic-worker",
            claim_expires_at=timezone.now() + timedelta(minutes=1),
            claim_token=uuid4(),
        )
        before = ExternalContentDeletionIntent.objects.values().get(pk=intent.pk)
        fault = RuntimeError("测试注入：取消意图保存失败")
        original_save = ExternalContentDeletionIntent.save

        def failing_save(instance: Any, *args: Any, **kwargs: Any) -> None:
            original_save(instance, *args, **kwargs)
            if instance.pk == intent.pk and instance.status == DeletionIntentStatus.CANCELLED:
                raise fault

        with patch.object(ExternalContentDeletionIntent, "save", new=failing_save):
            with self.assertRaises(RuntimeError) as caught:
                cancel_pending_deletion_intent(intent.pk)
        self.assertIs(caught.exception, fault)
        self.assertEqual(
            ExternalContentDeletionIntent.objects.values().get(pk=intent.pk), before
        )

    def test_status_changed_before_lock_is_rechecked_on_real_connection(self) -> None:
        """预读后竞争推进为 GC_ELIGIBLE，取消仍须使用获锁后的当前状态。"""
        intent = self._make_intent(DeletionIntentStatus.PENDING)
        entered_lock = Event()
        finished = Event()
        outcomes: list[Any] = []

        def observed_lock(*args: Any, **kwargs: Any) -> Any:
            entered_lock.set()
            return lock_page_lifecycle_hierarchy(*args, **kwargs)

        def cancel_on_other_connection() -> None:
            close_old_connections()
            try:
                outcomes.append(cancel_pending_deletion_intent(intent.pk))
            except BaseException as exc:
                outcomes.append(exc)
            finally:
                connections["default"].close()
                finished.set()

        worker = Thread(
            target=cancel_on_other_connection, name="cancel-intent-contender", daemon=True
        )
        with patch(
            "external_content.locking.lock_page_lifecycle_hierarchy", new=observed_lock
        ):
            try:
                with transaction.atomic():
                    Page.objects.select_for_update().get(pk=self.page.pk)
                    worker.start()
                    self.assertTrue(
                        entered_lock.wait(timeout=10), "取消线程未完成预读握手"
                    )
                    ExternalContentDeletionIntent.objects.filter(pk=intent.pk).update(
                        status=DeletionIntentStatus.GC_ELIGIBLE
                    )
                self.assertTrue(finished.wait(timeout=10), "取消线程未在释放行锁后结束")
            finally:
                worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], ExternalContentOperationBlockedError)
        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.GC_ELIGIBLE)
