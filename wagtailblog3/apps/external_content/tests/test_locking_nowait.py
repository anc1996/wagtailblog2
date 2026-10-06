"""验证四层生命周期 NOWAIT 竞争、整笔回滚和数据库错误分类。"""

from __future__ import annotations

from threading import Event, Thread
from time import monotonic
from typing import Any
from unittest.mock import PropertyMock, patch
from uuid import uuid4

from django.db import (
    NotSupportedError,
    OperationalError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import TransactionTestCase
from django.utils import timezone
from wagtail.models import Locale, Page, Revision

from external_content.exceptions import ExternalContentOperationBlockedError
from external_content.locking import _nowait_lock_errors, lock_page_lifecycle_hierarchy
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)


class LifecycleNowaitTests(TransactionTestCase):
    """使用独立 MySQL 连接竞争，不借动态模型表或 Mongo 替身。"""

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(connection.vendor, "mysql")
        self.assertTrue(connection.features.has_select_for_update_nowait)
        locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
        root = Page.get_first_root_node()
        if root is None:
            root = Page.add_root(title="锁竞争测试根", locale=locale)
        self.page = root.add_child(
            instance=Page(
                title="锁竞争测试页",
                slug=f"nowait-{uuid4().hex[:10]}",
                locale=locale,
                live=False,
            )
        )
        self.revision = self.page.save_revision()
        self.state = ExternalContentPublicationState.objects.create(
            page_id=self.page.pk,
            model_label="wagtailcore.page",
        )
        self.intent = ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="wagtailcore.page",
            status=DeletionIntentStatus.CANCELLED,
            safe_gc_after=timezone.now(),
        )

    def _assert_layer_conflict(self, model: Any, target_pk: Any) -> None:
        """删除 NOWAIT 或漏锁任一层时，应因无领域拒绝或留下探针行而失败。"""
        ready = Event()
        release = Event()
        thread_errors: list[BaseException] = []

        def hold_lock() -> None:
            """事件握手确认独立连接已持锁，超时仅用于防止测试挂死。"""
            close_old_connections()
            try:
                with transaction.atomic():
                    model.objects.select_for_update().get(pk=target_pk)
                    ready.set()
                    if not release.wait(timeout=10):
                        raise RuntimeError("NOWAIT 测试等待释放锁超时")
            except BaseException as exc:
                thread_errors.append(exc)
                ready.set()
            finally:
                connections["default"].close()

        holder = Thread(target=hold_lock, name="lifecycle-nowait-holder", daemon=True)
        holder.start()
        marker_pk = self.page.pk + 1000000
        try:
            self.assertTrue(ready.wait(timeout=10), "独立连接未能完成持锁握手")
            self.assertFalse(thread_errors, "独立连接持锁失败")
            started = monotonic()
            with self.assertRaises(ExternalContentOperationBlockedError):
                with transaction.atomic():
                    ExternalContentPublicationState.objects.create(
                        page_id=marker_pk,
                        model_label="wagtailcore.page",
                    )
                    lock_page_lifecycle_hierarchy(self.page.pk, nowait=True)
            self.assertLess(monotonic() - started, 4, "NOWAIT 不应等待持锁连接释放")
            self.assertFalse(
                ExternalContentPublicationState.objects.filter(
                    page_id=marker_pk
                ).exists(),
                "锁冲突必须回滚整个调用事务，不能只回滚局部 SQL",
            )
        finally:
            release.set()
            holder.join(timeout=10)
        self.assertFalse(holder.is_alive(), "持锁线程未退出")
        self.assertFalse(thread_errors, "持锁线程收尾失败")
        with transaction.atomic():
            locked = lock_page_lifecycle_hierarchy(self.page.pk, nowait=True)
            self.assertEqual(locked.page.pk, self.page.pk)
            self.assertEqual(locked.state.pk, self.state.pk)
            self.assertEqual([item.pk for item in locked.revisions], [self.revision.pk])
            self.assertEqual([item.pk for item in locked.intents], [self.intent.pk])

    def test_page_lock_conflict_rolls_back_entire_action(self) -> None:
        self._assert_layer_conflict(Page, self.page.pk)

    def test_state_lock_conflict_rolls_back_entire_action(self) -> None:
        self._assert_layer_conflict(ExternalContentPublicationState, self.state.pk)

    def test_revision_lock_conflict_rolls_back_entire_action(self) -> None:
        self._assert_layer_conflict(Revision, self.revision.pk)

    def test_intent_lock_conflict_rolls_back_entire_action(self) -> None:
        self._assert_layer_conflict(ExternalContentDeletionIntent, self.intent.pk)

    def test_other_database_errors_are_not_reclassified(self) -> None:
        """死锁、超时和其他错误必须保留原异常，不能伪装为可重试拒绝。"""
        for error_number in (1205, 1213, 2006):
            with self.subTest(error_number=error_number):
                fault = OperationalError(error_number, "测试注入数据库故障")
                with self.assertRaises(OperationalError) as caught:
                    with _nowait_lock_errors(self.page.pk, nowait=True):
                        raise fault
                self.assertIs(caught.exception, fault)

    def test_blocking_mode_does_not_reclassify_nowait_error(self) -> None:
        fault = OperationalError(3572, "测试注入非 NOWAIT 模式故障")
        with self.assertRaises(OperationalError) as caught:
            with _nowait_lock_errors(self.page.pk, nowait=False):
                raise fault
        self.assertIs(caught.exception, fault)

    def test_unsupported_nowait_rejects_without_blocking_fallback(self) -> None:
        """后端不支持时必须拒绝，不能静默改用阻塞锁。"""
        with patch.object(
            type(connection.features),
            "has_select_for_update_nowait",
            new_callable=PropertyMock,
            return_value=False,
        ):
            with self.assertRaises(NotSupportedError):
                with transaction.atomic():
                    lock_page_lifecycle_hierarchy(self.page.pk, nowait=True)
