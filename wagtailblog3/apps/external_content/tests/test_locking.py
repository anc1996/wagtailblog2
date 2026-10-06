# -*- coding: utf-8 -*-
"""
Wagtail Page 正文外置通用协议 - G4 统一冻结与全局锁序定向测试。

覆盖测试契约：
1. lock_writable_page 必须且仅返回基础 Page 实例（非 specific），严禁触发 hydration；
2. lock_writable_page 页面缺失时严格 Fail-Closed 抛出 Page.DoesNotExist；
3. lock_writable_page 与 lock_page_lifecycle_hierarchy 必须在事务内执行，非事务抛出 RuntimeError；
4. lock_page_lifecycle_hierarchy 严格遵循 Page -> State -> Revision(pk升序) -> DeletionIntent 锁序；
5. 无论入参 Revision ID 顺序如何，加锁执行与返回结果严格按 pk 升序单调递增；
6. check_page_write_not_blocked 统一冻结契约：
   - status == COMPLETED 永久墓碑严格阻断写入并抛出 ExternalContentOperationBlockedError；
   - status in (PENDING, CLAIMED, MYSQL_DELETED, GC_ELIGIBLE, FAILED) 均严格阻断写入；
   - 页面缺失或不存在时严格 Fail-Closed 阻断；
   - 仅当意图为 CANCELLED 或不存在删除意图时允许操作；
   - CANCELLED 与阻断状态混存时严格阻断写入；
7. preserve_threadlocal_context 嵌套安全原值恢复、初始属性缺失清理与异常透传。
"""

import json
import os
import tempfile
import threading
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import PropertyMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from wagtail.models import Locale, Page, Revision

from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import ExternalContentOperationBlockedError
from external_content.locking import (
    PageLifecycleLocks,
    check_page_write_not_blocked,
    lock_page_lifecycle_hierarchy,
    lock_writable_page,
    preserve_threadlocal_context,
)
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)
from external_content.registry import (
    get_storage_policy_for_model,
    reset_active_baseline_file,
    set_active_baseline_file,
)
from external_content.store import (
    ContentVersionStore,
    ensure_indexes,
    get_mongo_client,
    reset_active_collection_name,
    set_active_collection_name,
)
from external_content.publication import ExternalContentPublicationService
from external_content.tests.test_lifecycle import LifecycleTestPage


class ExternalContentLockingTests(TransactionTestCase):
    """G4 统一锁序与写入冻结行为测试套件。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with connection.schema_editor() as editor:
            if LifecycleTestPage._meta.db_table not in connection.introspection.table_names():
                editor.create_model(LifecycleTestPage)

    def setUp(self) -> None:
        super().setUp()
        self.temp_baseline = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.temp_baseline.close()
        with open(self.temp_baseline.name, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "policies": {}}, f)
        set_active_baseline_file(self.temp_baseline.name)
        get_storage_policy_for_model(LifecycleTestPage, allow_record_new=True)

        self.mongo_client = get_mongo_client()
        db_name = getattr(settings, "MONGO_DB", {}).get("NAME", "wagtailblog_test")
        self.coll_name = f"test_locking_{uuid.uuid4().hex[:12]}"
        self.collection = self.mongo_client[db_name][self.coll_name]
        ensure_indexes(self.collection)
        set_active_collection_name(self.coll_name)
        self.store = ContentVersionStore(collection=self.collection)

        User = get_user_model()
        self.user = User.objects.create_superuser(
            username=f"lock_admin_{uuid.uuid4().hex[:6]}",
            email="admin@example.com",
            password="password",
        )

        self.locale, _ = Locale.objects.get_or_create(language_code=settings.LANGUAGE_CODE)
        root = Page.objects.filter(depth=1).first()
        if not root:
            root = Page.add_root(title="Root", locale=self.locale)
        self.root_page = root

        ContentType.objects.clear_cache()
        ct, _ = ContentType.objects.get_or_create(
            app_label=LifecycleTestPage._meta.app_label,
            model=LifecycleTestPage._meta.model_name,
        )

        self.page = LifecycleTestPage(
            title="锁序测试页面",
            slug=f"locking-test-{uuid.uuid4().hex[:6]}",
            native_intro="原生摘要",
            live=False,
            primary_body=[("heading", "标题一")],
            secondary_body=[("quote", "引用一")],
        )
        self.page.content_type = ct
        self.root_page.add_child(instance=self.page)
        self.page._test_content_store = self.store

    def tearDown(self) -> None:
        reset_active_baseline_file()
        reset_active_collection_name()
        if os.path.exists(self.temp_baseline.name):
            os.remove(self.temp_baseline.name)
        lock_file = self.temp_baseline.name + ".lock"
        if os.path.exists(lock_file):
            os.remove(lock_file)
        self.collection.drop()
        super().tearDown()

    def test_lock_writable_page_returns_base_page_without_specific(self) -> None:
        """测试 lock_writable_page 必须且仅返回基础 Page 实例，绝不调用 .specific，并实际执行 SELECT ... FOR UPDATE。"""
        with patch.object(Page, "specific", new_callable=PropertyMock) as mock_specific:
            with transaction.atomic():
                with CaptureQueriesContext(connection) as ctx:
                    locked_page = lock_writable_page(self.page.pk)
                mock_specific.assert_not_called()
                self.assertIs(type(locked_page), Page)
                self.assertNotIsInstance(locked_page, LifecycleTestPage)
                self.assertEqual(locked_page.pk, self.page.pk)

                # 验证实际针对 wagtailcore_page 执行了 SELECT ... FOR UPDATE 行排他锁
                page_queries = [
                    q["sql"]
                    for q in ctx.captured_queries
                    if "wagtailcore_page" in q["sql"].lower()
                ]
                self.assertTrue(len(page_queries) > 0, "必须生成针对 wagtailcore_page 的 SQL 查询")
                self.assertTrue(
                    any("for update" in q.lower() for q in page_queries),
                    "针对 wagtailcore_page 必须施加 SELECT ... FOR UPDATE 排他行锁",
                )

    def test_lock_writable_page_no_hydration(self) -> None:
        """测试 lock_writable_page 绝不触发正文 hydration 或从 MongoDB 载入字段。"""
        with patch.object(Page, "specific", new_callable=PropertyMock) as mock_specific,              patch.object(ContentVersionStore, "get_version") as mock_get_ver,              patch.object(ExternalContentCoordinator, "load_external_content_from_manifest") as mock_load:
            with transaction.atomic():
                locked_page = lock_writable_page(self.page.pk)
                mock_specific.assert_not_called()
                mock_get_ver.assert_not_called()
                mock_load.assert_not_called()
                # 基础 Page 不应有子类特定的外置 StreamField 字段
                self.assertFalse(hasattr(locked_page, "primary_body"))
                self.assertFalse(hasattr(locked_page, "_external_content_loaded_hashes"))

    def test_lock_writable_page_missing_page_fail_closed(self) -> None:
        """测试 lock_writable_page 在页面不存在时严格 Fail-Closed 抛出 Page.DoesNotExist。"""
        non_existent_page_id = 9999999
        with transaction.atomic():
            with self.assertRaises(Page.DoesNotExist):
                lock_writable_page(non_existent_page_id)

    def test_lock_writable_page_requires_atomic_transaction(self) -> None:
        """测试 lock_writable_page 若脱离 transaction.atomic() 执行必须抛出 RuntimeError。"""
        with self.assertRaises(RuntimeError) as cm:
            lock_writable_page(self.page.pk)
        self.assertIs(type(cm.exception), RuntimeError)

    def test_lock_ordering_hierarchy_requires_atomic_transaction(self) -> None:
        """测试 lock_page_lifecycle_hierarchy 脱离事务执行时必须抛出 RuntimeError。"""
        with self.assertRaises(RuntimeError) as cm:
            lock_page_lifecycle_hierarchy(self.page.pk)
        self.assertIs(type(cm.exception), RuntimeError)

    def test_lock_ordering_hierarchy_acquires_all_levels_in_order(self) -> None:
        """测试 lock_page_lifecycle_hierarchy 按序获取 Page、State、Revision 与 DeletionIntent 锁，验证实际 SQL 顺序。"""
        # 预先生成 Revision 与 State
        rev1 = self.page.save_revision(user=self.user)
        self.page.primary_body = [("heading", "标题二")]
        rev2 = self.page.save_revision(user=self.user)

        # 预置 CANCELLED 意图以确保 DeletionIntent 存在且不阻断加锁流程
        ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="external_content.lifecycletestpage",
            status=DeletionIntentStatus.CANCELLED,
            safe_gc_after=timezone.now() + timedelta(days=7),
        )

        with transaction.atomic():
            with CaptureQueriesContext(connection) as ctx:
                locks = lock_page_lifecycle_hierarchy(self.page.pk)
            self.assertIsInstance(locks, PageLifecycleLocks)
            self.assertIs(type(locks.page), Page)
            self.assertEqual(locks.page.pk, self.page.pk)
            self.assertIsNotNone(locks.state)
            self.assertEqual(locks.state.page_id, self.page.pk)
            self.assertEqual(len(locks.revisions), 2)
            # 验证按 pk 严格升序排序
            self.assertEqual(locks.revisions[0].pk, min(rev1.pk, rev2.pk))
            self.assertEqual(locks.revisions[1].pk, max(rev1.pk, rev2.pk))
            self.assertTrue(len(locks.intents) > 0)

            # 验证底层 SQL 锁序：Page -> State -> Revision -> Intent
            query_sqls = [q["sql"].lower() for q in ctx.captured_queries if "for update" in q["sql"].lower()]
            idx_page = next((i for i, sql in enumerate(query_sqls) if "wagtailcore_page" in sql), -1)
            idx_state = next((i for i, sql in enumerate(query_sqls) if "external_content_publication_state" in sql), -1)
            idx_rev = next((i for i, sql in enumerate(query_sqls) if "wagtailcore_revision" in sql), -1)
            idx_intent = next((i for i, sql in enumerate(query_sqls) if "external_content_deletion_intent" in sql), -1)

            self.assertNotEqual(idx_page, -1, "必须对 wagtailcore_page 执行 SELECT ... FOR UPDATE")
            self.assertNotEqual(idx_state, -1, "必须对 external_content_publication_state 执行 SELECT ... FOR UPDATE")
            self.assertNotEqual(idx_rev, -1, "必须对 wagtailcore_revision 执行 SELECT ... FOR UPDATE")
            self.assertNotEqual(idx_intent, -1, "必须对 external_content_deletion_intent 执行 SELECT ... FOR UPDATE")
            self.assertTrue(
                idx_page < idx_state < idx_rev < idx_intent,
                f"SQL 排他锁加锁顺序必须严格满足 Page({idx_page}) < State({idx_state}) < Revision({idx_rev}) < Intent({idx_intent})",
            )

    def test_lock_revisions_strictly_sorted_by_pk_even_if_passed_reversed(self) -> None:
        """测试 lock_page_lifecycle_hierarchy 即使传入逆序 Revision ID 列表，仍严格按 pk 升序加锁且 SQL 包含 ORDER BY。"""
        rev1 = self.page.save_revision(user=self.user)
        self.page.primary_body = [("heading", "标题二")]
        rev2 = self.page.save_revision(user=self.user)
        self.page.primary_body = [("heading", "标题三")]
        rev3 = self.page.save_revision(user=self.user)

        reversed_ids = [rev3.pk, rev1.pk, rev2.pk]
        with transaction.atomic():
            with CaptureQueriesContext(connection) as ctx:
                locks = lock_page_lifecycle_hierarchy(self.page.pk, revision_ids=reversed_ids)
            rev_pks = [r.pk for r in locks.revisions]
            self.assertEqual(rev_pks, sorted(reversed_ids))

            rev_queries = [
                q["sql"]
                for q in ctx.captured_queries
                if "wagtailcore_revision" in q["sql"].lower() and "for update" in q["sql"].lower()
            ]
            self.assertTrue(len(rev_queries) > 0, "必须生成针对 wagtailcore_revision 的 FOR UPDATE 查询")
            self.assertTrue(
                any("order by" in q.lower() and ("id" in q.lower() or "pk" in q.lower()) for q in rev_queries),
                "针对 wagtailcore_revision 的锁查询必须包含 ORDER BY id ASC，保证物理锁序严格升序避免死锁",
            )

    def test_check_page_write_not_blocked_executes_select_for_update_when_requested(self) -> None:
        """测试 check_page_write_not_blocked 在 for_update=True 时施加排他锁，for_update=False 时仅普通查询。"""
        with transaction.atomic():
            with CaptureQueriesContext(connection) as ctx_locked:
                check_page_write_not_blocked(self.page.pk, for_update=True)
            intent_queries = [
                q["sql"]
                for q in ctx_locked.captured_queries
                if "external_content_deletion_intent" in q["sql"].lower()
            ]
            self.assertTrue(len(intent_queries) > 0, "for_update=True 时必须生成针对 DeletionIntent 的查询")
            self.assertTrue(
                any("for update" in q.lower() for q in intent_queries),
                "for_update=True 时必须在 SQL 中执行 SELECT ... FOR UPDATE",
            )

        with CaptureQueriesContext(connection) as ctx_unlocked:
            check_page_write_not_blocked(self.page.pk, for_update=False)
        intent_queries_unlocked = [
            q["sql"]
            for q in ctx_unlocked.captured_queries
            if "external_content_deletion_intent" in q["sql"].lower()
        ]
        self.assertTrue(len(intent_queries_unlocked) > 0, "for_update=False 时必须生成针对 DeletionIntent 的查询")
        self.assertFalse(
            any("for update" in q.lower() for q in intent_queries_unlocked),
            "for_update=False 时严禁执行 FOR UPDATE",
        )

    def test_check_page_write_blocked_on_completed_tombstone(self) -> None:
        """测试存在 COMPLETED 状态的永久墓碑时，check_page_write_not_blocked 必须抛出异常阻断写入。"""
        ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="external_content.lifecycletestpage",
            status=DeletionIntentStatus.COMPLETED,
            safe_gc_after=timezone.now() - timedelta(days=1),
            completed_at=timezone.now(),
        )

        with self.assertRaises(ExternalContentOperationBlockedError):
            check_page_write_not_blocked(self.page.pk)

    def test_check_page_write_blocked_on_all_non_cancelled_statuses(self) -> None:
        """测试除 CANCELLED 外的所有意图状态（PENDING, CLAIMED, MYSQL_DELETED, GC_ELIGIBLE, FAILED）均阻断写入。"""
        blocking_statuses = [
            DeletionIntentStatus.PENDING,
            DeletionIntentStatus.CLAIMED,
            DeletionIntentStatus.MYSQL_DELETED,
            DeletionIntentStatus.GC_ELIGIBLE,
            DeletionIntentStatus.FAILED,
        ]

        for status in blocking_statuses:
            intent = ExternalContentDeletionIntent.objects.create(
                page_id=self.page.pk,
                model_label="external_content.lifecycletestpage",
                status=status,
                safe_gc_after=timezone.now() + timedelta(days=7),
            )
            with self.assertRaises(
                ExternalContentOperationBlockedError,
                msg=f"状态 {status} 必须触发写入阻断",
            ):
                check_page_write_not_blocked(self.page.pk)
            intent.delete()

    def test_check_page_write_blocked_on_mixed_statuses_orderings(self) -> None:
        """测试存在 CANCELLED 与阻断状态（PENDING/COMPLETED）混存时，无论插入先后均严格阻断写入。"""
        # 情况 1: CANCELLED 在前，COMPLETED 在后
        intent_can = ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="external_content.lifecycletestpage",
            status=DeletionIntentStatus.CANCELLED,
            safe_gc_after=timezone.now() + timedelta(days=7),
        )
        intent_comp = ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="external_content.lifecycletestpage",
            status=DeletionIntentStatus.COMPLETED,
            safe_gc_after=timezone.now() - timedelta(days=1),
            completed_at=timezone.now(),
        )
        with self.assertRaises(ExternalContentOperationBlockedError):
            check_page_write_not_blocked(self.page.pk)
        intent_comp.delete()

        # 情况 2: PENDING 在后
        intent_pend = ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="external_content.lifecycletestpage",
            status=DeletionIntentStatus.PENDING,
            safe_gc_after=timezone.now() + timedelta(days=7),
        )
        with self.assertRaises(ExternalContentOperationBlockedError):
            check_page_write_not_blocked(self.page.pk)
        intent_pend.delete()
        intent_can.delete()

        # 情况 3: PENDING 在前，CANCELLED 在后
        intent_pend2 = ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="external_content.lifecycletestpage",
            status=DeletionIntentStatus.PENDING,
            safe_gc_after=timezone.now() + timedelta(days=7),
        )
        intent_can2 = ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="external_content.lifecycletestpage",
            status=DeletionIntentStatus.CANCELLED,
            safe_gc_after=timezone.now() + timedelta(days=7),
        )
        with self.assertRaises(ExternalContentOperationBlockedError):
            check_page_write_not_blocked(self.page.pk)
        intent_pend2.delete()
        intent_can2.delete()

    def test_check_page_write_allowed_only_when_cancelled_or_no_intent(self) -> None:
        """测试无删除意图或仅有 CANCELLED 意图时，写入操作正常放行。"""
        # 1. 无意图放行
        check_page_write_not_blocked(self.page.pk)

        # 2. 仅有 CANCELLED 意图放行
        cancelled_intent = ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="external_content.lifecycletestpage",
            status=DeletionIntentStatus.CANCELLED,
            safe_gc_after=timezone.now() + timedelta(days=7),
        )
        check_page_write_not_blocked(self.page.pk)
        cancelled_intent.delete()

    def test_check_page_write_missing_page_fail_closed(self) -> None:
        """测试当目标页面缺失（数据库中不存在）时，check_page_write_not_blocked 严格 Fail-Closed 阻断。"""
        non_existent_page_id = 8888888
        with self.assertRaises(ExternalContentOperationBlockedError):
            check_page_write_not_blocked(non_existent_page_id)

    def test_clear_published_pointer_holds_full_hierarchy_lock_and_blocks_on_intent(self) -> None:
        """测试 clear_published_pointer 严格在事务内持有全级锁且存在删除意图时被阻断 (P0-2闭环)。"""
        intent = ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="external_content.lifecycletestpage",
            status=DeletionIntentStatus.PENDING,
            safe_gc_after=timezone.now() + timedelta(days=7),
        )
        try:
            with self.assertRaises(ExternalContentOperationBlockedError):
                ExternalContentPublicationService.clear_published_pointer(self.page.pk)
        finally:
            intent.delete()

        with CaptureQueriesContext(connection) as queries:
            ExternalContentPublicationService.clear_published_pointer(self.page.pk)

        sql_statements = [q["sql"].upper() for q in queries.captured_queries]
        has_for_update = any("FOR UPDATE" in sql for sql in sql_statements)
        self.assertTrue(has_for_update, "clear_published_pointer 必须在事务内持有排他锁")

    def test_lifecycle_action_entrance_locks_and_blocking_coverage(self) -> None:
        """测试生命周期核心入口（save_revision / publish / unpublish）均受到统一冻结与排他锁保护。"""
        rev = self.page.save_revision(user=self.user)

        ExternalContentDeletionIntent.objects.create(
            page_id=self.page.pk,
            model_label="external_content.lifecycletestpage",
            status=DeletionIntentStatus.COMPLETED,
            safe_gc_after=timezone.now() - timedelta(days=1),
            completed_at=timezone.now(),
        )

        with self.assertRaises(ExternalContentOperationBlockedError):
            self.page.save_revision(user=self.user)

        with self.assertRaises(ExternalContentOperationBlockedError):
            self.page.publish(rev)

        with self.assertRaises(ExternalContentOperationBlockedError):
            self.page.unpublish()

    def test_threadlocal_nested_context_restores_original_value(self) -> None:
        """测试 preserve_threadlocal_context 在多层嵌套离开时精准恢复原值，严禁粗暴重置。"""
        ctx = threading.local()
        ctx.flag = "initial_val"

        with preserve_threadlocal_context(ctx, "flag", "nested_level_1"):
            self.assertEqual(ctx.flag, "nested_level_1")
            with preserve_threadlocal_context(ctx, "flag", "nested_level_2"):
                self.assertEqual(ctx.flag, "nested_level_2")
            self.assertEqual(ctx.flag, "nested_level_1")

        self.assertEqual(ctx.flag, "initial_val")

    def test_threadlocal_nested_context_initial_nonexistent_cleans_up(self) -> None:
        """测试 preserve_threadlocal_context 当属性初始不存在时，退出上下文后属性被彻底清理 (delattr)。"""
        ctx = threading.local()
        self.assertFalse(hasattr(ctx, "temp_attr"))
        with preserve_threadlocal_context(ctx, "temp_attr", "active"):
            self.assertTrue(hasattr(ctx, "temp_attr"))
            self.assertEqual(ctx.temp_attr, "active")
        self.assertFalse(hasattr(ctx, "temp_attr"))

    def test_threadlocal_nested_context_restores_on_exception(self) -> None:
        """测试 preserve_threadlocal_context 三层嵌套抛出异常时各层精准恢复且异常原样透传。"""
        ctx = threading.local()
        ctx.flag = "level_0"

        class CustomTestError(Exception):
            pass

        with self.assertRaises(CustomTestError):
            with preserve_threadlocal_context(ctx, "flag", "level_1"):
                self.assertEqual(ctx.flag, "level_1")
                with preserve_threadlocal_context(ctx, "flag", "level_2"):
                    self.assertEqual(ctx.flag, "level_2")
                    with preserve_threadlocal_context(ctx, "flag", "level_3"):
                        self.assertEqual(ctx.flag, "level_3")
                        raise CustomTestError("三层深嵌套异常测试还原")

        self.assertEqual(ctx.flag, "level_0")
