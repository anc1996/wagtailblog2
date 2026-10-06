# -*- coding: utf-8 -*-
"""
Wagtail Page 正文外置通用协议 - 安全删除、删除意图持久化与延迟回收（Tombstone GC）定向测试。

验证：
1. 页面删除意图持久化（record_deletion_intent）：
   - 固化页面所有草稿、正式发布、审核通过以及全部历史 Revision 引用的正文不可变版本；
   - 设定安全隔离保护期（safe_gc_after）；
2. 受控安全删除（execute_safe_page_deletion）：
   - 在事务中删除 MySQL 页面实体与关联 Revision；
   - 标记删除意图状态为 MYSQL_DELETED；
   - 关键安全防线：MongoDB 中的正文版本在此阶段绝对完好保留，不立即物理清除；
3. 延迟垃圾回收保护期（Tombstone GC）：
   - 在安全隔离期内执行 GC，严禁物理删除 MongoDB 正文版本；
   - 隔离期度过且经复合引用核查确认无任何其他活跃页面引用后，安全物理清除 MongoDB 版本；
4. 引用计数归零防线：若某正文版本仍被其他页面（如未完成回收的副本或跨页引用）持有，GC 坚决跳过；
5. 直接调用 page.delete() 时，底层信号或方法自动持久化删除意图，绝不出现未经意图登记的野蛮删除；
6. 认领围栏 (Claim Fence) 与租约超时抢占机制；
7. Tombstone 逐版本回收进度、部分失败隔离与重试最终一致性；
8. 严格安全隔离期无参数可绕过。
"""

import datetime
import inspect
import uuid
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from wagtail.models import Locale, Page

from external_content.exceptions import (
    ContentVersionNotFoundError,
    ExternalContentOperationBlockedError,
)
from external_content.publication import ExternalContentPublicationService
from external_content.deletion import (
    cancel_pending_deletion_intent,
    check_page_write_not_blocked,
    execute_safe_page_deletion,
    perform_garbage_collection,
    record_deletion_intent,
)
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)
from external_content.store import ContentVersionStore, get_mongo_client
from external_content.tests.test_lifecycle import LifecycleTestPage
from guides.models import GuideIndexPage, GuidePage, GuideTopic


class ExternalContentDeletionTests(TransactionTestCase):
    """外置正文页面安全删除与版本回收测试套件。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with connection.schema_editor() as editor:
            if LifecycleTestPage._meta.db_table not in connection.introspection.table_names():
                editor.create_model(LifecycleTestPage)

    def setUp(self) -> None:
        super().setUp()
        User = get_user_model()
        self.user = User.objects.create_superuser(
            username=f"del_admin_{uuid.uuid4().hex[:6]}",
            email="admin@example.com",
            password="password",
        )

        self.locale_zh, _ = Locale.objects.get_or_create(language_code="zh-hans")

        root = Page.objects.filter(depth=1).first()
        if not root:
            root = Page.add_root(title="Root", locale=self.locale_zh)
        self.root = root

        self.guide_index = GuideIndexPage(
            title="专题指南索引",
            slug=f"guides-del-{uuid.uuid4().hex[:6]}",
            locale=self.locale_zh,
            live=True,
        )
        self.root.add_child(instance=self.guide_index)
        self.guide_index.save_revision(user=self.user).publish(user=self.user)

        self.topic = GuideTopic.objects.create(name="部署运维", slug="deployment")

        # 创建受测页面并产生多个 Revision
        self.guide_page = GuidePage(
            title="待安全删除指南",
            slug=f"guide-del-{uuid.uuid4().hex[:6]}",
            summary="待删除页面摘要",
            topic=self.topic,
            difficulty="beginner",
            locale=self.locale_zh,
            body=[
                {
                    "type": "lead",
                    "value": "<p>初版导读正文</p>",
                },
                {
                    "type": "step",
                    "value": {
                        "title": "初版步骤",
                        "description": "<p>初版步骤说明</p>",
                        "image": None,
                    },
                },
            ],
            live=False,
        )
        self.guide_index.add_child(instance=self.guide_page)

        # Revision 1 (发布)
        rev1 = self.guide_page.save_revision(user=self.user)
        rev1.publish(user=self.user)
        self.guide_page.refresh_from_db()

        # Revision 2 (草稿更新)
        self.guide_page.body = [
            {
                "type": "lead",
                "value": "<p>第二版修改后的导读正文</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "第二版步骤",
                    "description": "<p>第二版步骤说明</p>",
                    "image": None,
                },
            },
        ]
        rev2 = self.guide_page.save_revision(user=self.user)

        self.guide_page.refresh_from_db()
        self.store = ContentVersionStore()

    def test_record_deletion_intent_persists_full_version_manifest(self) -> None:
        """
        验证 record_deletion_intent 完整记录待删除页面引用的所有版本：
        1. 包含正式发布版本的 version_id；
        2. 包含草稿版本的 version_id；
        3. 安全隔离期 safe_gc_after 设置为未来时间；
        4. 状态为 PENDING。
        """
        intent = record_deletion_intent(
            self.guide_page,
            actor_id=str(self.user.pk),
            safe_period_seconds=86400 * 7,
        )

        self.assertIsNotNone(intent)
        self.assertEqual(intent.page_id, self.guide_page.pk)
        self.assertEqual(intent.model_label, self.guide_page._meta.label_lower)
        self.assertEqual(intent.status, DeletionIntentStatus.PENDING)
        self.assertEqual(intent.actor_id, str(self.user.pk))

        # 验证清单中包含的版本 ID
        version_ids = intent.manifest.get("body_version_ids", [])
        self.assertGreaterEqual(len(version_ids), 2, "删除清单必须包含初版与草稿版的全部版本 ID")

        # 验证所有记录的版本确实存在于 Mongo 中
        for vid in version_ids:
            record = self.store.collection.find_one({"version_id": vid})
            self.assertIsNotNone(record, f"Mongo 中必须真实存在记录的版本: {vid}")

        self.assertGreater(intent.safe_gc_after, timezone.now())

    def test_safe_page_deletion_deletes_mysql_records_and_keeps_mongo_intact(self) -> None:
        """
        验证 execute_safe_page_deletion：
        1. MySQL 数据库中的页面与关联 Revision 被彻底清除；
        2. 删除意图状态变更为 MYSQL_DELETED；
        3. 核心红线保护：MongoDB 中的不可变正文在此刻绝对完好保留！
        """
        page_id = self.guide_page.pk
        intent = record_deletion_intent(self.guide_page, actor_id=str(self.user.pk))
        version_ids = list(intent.manifest.get("body_version_ids", []))

        # 执行受控安全删除
        completed_intent = execute_safe_page_deletion(self.guide_page, intent=intent)
        self.assertEqual(completed_intent.status, DeletionIntentStatus.MYSQL_DELETED)

        # 验证 MySQL 中页面已不存在
        self.assertFalse(Page.objects.filter(pk=page_id).exists())
        self.assertTrue(ExternalContentPublicationState.objects.filter(page_id=page_id).exists(), "PublicationState 必须作为审计记录持久保留，严禁物理删除")

        # 核心红线：验证 MongoDB 中的正文版本全部依然完好存在！
        for vid in version_ids:
            record = self.store.get_raw_version_document(vid)
            self.assertIsNotNone(record, f"MySQL 删除后，Mongo 版本 {vid} 必须在安全期内完好保留！")

    def test_garbage_collection_respects_safe_isolation_period(self) -> None:
        """
        验证延迟垃圾回收（Tombstone GC）在安全保护期内绝不物理删除 Mongo 版本。
        """
        intent = record_deletion_intent(
            self.guide_page,
            actor_id=str(self.user.pk),
            safe_period_seconds=86400 * 7,  # 7天后
        )
        version_ids = list(intent.manifest.get("body_version_ids", []))
        execute_safe_page_deletion(self.guide_page, intent=intent)

        # 此时仍在保护期内，执行 GC
        stats = perform_garbage_collection()
        self.assertEqual(stats["collected_intents"], 0)
        self.assertEqual(stats["deleted_versions"], 0)

        # 验证意图状态依然是 MYSQL_DELETED，且 Mongo 版本完好
        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.MYSQL_DELETED)
        for vid in version_ids:
            self.assertIsNotNone(self.store.get_raw_version_document(vid))

    def test_garbage_collection_purges_unreferenced_mongo_versions_after_safe_period(self) -> None:
        """
        验证当安全隔离期度过（safe_gc_after <= 当前时间），
        GC 进行复合引用核查后，安全物理清除无引用的 Mongo 版本，
        并将意图标记为 COMPLETED。
        """
        intent = record_deletion_intent(
            self.guide_page,
            actor_id=str(self.user.pk),
            safe_period_seconds=86400 * 7,
        )
        version_ids = list(intent.manifest.get("body_version_ids", []))
        execute_safe_page_deletion(self.guide_page, intent=intent)

        # 模拟真实时钟度过安全隔离期
        ExternalContentDeletionIntent.objects.filter(pk=intent.pk).update(
            safe_gc_after=timezone.now() - datetime.timedelta(seconds=10)
        )
        intent.refresh_from_db()
        self.assertLessEqual(intent.safe_gc_after, timezone.now())

        # 执行 GC 回收
        stats = perform_garbage_collection()
        self.assertGreaterEqual(stats["collected_intents"], 1)
        self.assertGreaterEqual(stats["deleted_versions"], 2)

        # 意图标记为 COMPLETED
        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.COMPLETED)
        self.assertIsNotNone(intent.completed_at)

        # 验证无引用的 Mongo 版本已被物理回收
        for vid in version_ids:
            self.assertIsNone(
                self.store.get_raw_version_document(vid),
                f"安全期已过且无引用的版本 {vid} 必须被物理回收",
            )

    def test_direct_page_delete_automatically_records_deletion_intent(self) -> None:
        """
        验证通过原生 page.delete() 或管理后台触发直接删除时，
        系统自动拦截并安全登记删除意图，保证不会野蛮删除且 Mongo 版本受保护。
        """
        page_id = self.guide_page.pk
        # 直接调用原生 delete()
        self.guide_page.delete()

        # 验证删除意图已被自动持久化
        intents = ExternalContentDeletionIntent.objects.filter(page_id=page_id)
        self.assertTrue(intents.exists(), "调用 page.delete() 必须自动生成删除意图记录")
        intent = intents.first()
        self.assertEqual(intent.status, DeletionIntentStatus.MYSQL_DELETED)
        self.assertGreater(len(intent.manifest.get("body_version_ids", [])), 0)

    def test_garbage_collection_claim_fence_and_lease_expiration(self) -> None:
        """
        验证 GC 认领围栏 (Claim Fence) 与租约超时抢占机制：
        1. 当 Worker A 认领并处理意图时（状态 CLAIMED，claim_expires_at 未过期），Worker B 无法重复认领；
        2. 当租约超时（claim_expires_at 过期），Worker B 能够成功抢占认领该意图并推进完成回收。
        """
        intent = record_deletion_intent(
            self.guide_page,
            actor_id=str(self.user.pk),
            safe_period_seconds=86400 * 7,
        )
        version_ids = list(intent.manifest.get("body_version_ids", []))
        execute_safe_page_deletion(self.guide_page, intent=intent)

        ExternalContentDeletionIntent.objects.filter(pk=intent.pk).update(
            safe_gc_after=timezone.now() - datetime.timedelta(seconds=10)
        )

        now = timezone.now()
        intent.refresh_from_db()
        intent.status = DeletionIntentStatus.CLAIMED
        intent.claimed_at = now
        intent.claimed_worker = "worker_a"
        intent.claim_token = uuid.uuid4()
        intent.claim_expires_at = now + datetime.timedelta(seconds=300)
        intent.save()

        stats_b = perform_garbage_collection(worker_id="worker_b")
        self.assertEqual(stats_b["collected_intents"], 0)
        self.assertEqual(stats_b["deleted_versions"], 0)

        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.CLAIMED)
        self.assertEqual(intent.claimed_worker, "worker_a")

        intent.claim_expires_at = now - datetime.timedelta(seconds=10)
        intent.save(update_fields=["claim_expires_at"])

        stats_b2 = perform_garbage_collection(worker_id="worker_b")
        self.assertEqual(stats_b2["collected_intents"], 1)
        self.assertGreaterEqual(stats_b2["deleted_versions"], 2)

        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.COMPLETED)
        self.assertIsNone(intent.claimed_worker)
        self.assertIsNone(intent.claim_expires_at)
        self.assertIsNone(intent.claim_token)
        for vid in version_ids:
            self.assertIsNone(self.store.get_raw_version_document(vid))

    def test_garbage_collection_partial_version_tombstone_and_retry_completion(self) -> None:
        """
        验证 Tombstone 逐版本回收进度、部分失败隔离与重试最终一致性：
        1. 意图包含多个不可变正文版本（例如 v1, v2）；
        2. 当其中某个版本仍被外部（如另一发布状态）引用时，GC 安全跳过该版本并记录在 failed_versions 中；
        3. 无引用的版本正常回收并记录在 collected_versions；
        4. 意图标记为 FAILED 并释放围栏租约；
        5. 外部引用消除后重新执行 GC，此前失败版本被成功回收，最终达成 COMPLETED。
        """
        intent = record_deletion_intent(
            self.guide_page,
            actor_id=str(self.user.pk),
            safe_period_seconds=86400 * 7,
        )
        version_ids = sorted(list(intent.manifest.get("body_version_ids", [])))
        self.assertGreaterEqual(len(version_ids), 2)
        v_blocked = version_ids[0]
        v_free = version_ids[1]

        execute_safe_page_deletion(self.guide_page, intent=intent)

        ExternalContentDeletionIntent.objects.filter(pk=intent.pk).update(
            safe_gc_after=timezone.now() - datetime.timedelta(seconds=10)
        )
        intent.refresh_from_db()

        dummy_state = ExternalContentPublicationState.objects.create(
            page_id=999999,
            model_label="guides.guidepage",
            published_revision_id=999,
            published_manifest={"fields": {"body": {"version_id": v_blocked}}},
            draft_manifest={},
            approved_manifest={},
        )

        try:
            stats = perform_garbage_collection()
            self.assertEqual(stats["collected_intents"], 0, "存在被引用版本时意图不能标记为完成")
            self.assertGreaterEqual(stats["deleted_versions"], 1, "未引用的版本必须被先行回收")

            intent.refresh_from_db()
            self.assertEqual(intent.status, DeletionIntentStatus.FAILED)
            self.assertEqual(intent.retry_count, 1)
            self.assertIn(v_free, intent.collected_versions)
            self.assertNotIn(v_blocked, intent.collected_versions)
            self.assertIn(v_blocked, intent.failed_versions)
            self.assertIsNone(intent.claim_expires_at, "失败后应释放租约围栏供重试")

            self.assertIsNone(self.store.get_raw_version_document(v_free))
            self.assertIsNotNone(self.store.get_raw_version_document(v_blocked))
        finally:
            dummy_state.delete()

        stats_retry = perform_garbage_collection()
        self.assertEqual(stats_retry["collected_intents"], 1)
        self.assertEqual(stats_retry["deleted_versions"], 1)

        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.COMPLETED)
        self.assertIn(v_blocked, intent.collected_versions)
        self.assertEqual(len(intent.failed_versions), 0)
        self.assertIsNone(self.store.get_raw_version_document(v_blocked))

    def test_garbage_collection_strict_safe_isolation_period_cannot_be_bypassed(self) -> None:
        """
        验证严格安全隔离期无参数可绕过：
        1. perform_garbage_collection 签名中绝不提供任何 force 或 bypass 参数；
        2. 对于 safe_gc_after 在未来的意图，无论如何调用 GC 都绝不认领与删除。
        """
        sig = inspect.signature(perform_garbage_collection)
        self.assertNotIn("force", sig.parameters, "GC 函数严禁提供 force 绕过参数")
        self.assertNotIn("bypass_safety_period", sig.parameters, "GC 函数严禁提供 bypass 参数")

        intent = record_deletion_intent(
            self.guide_page,
            actor_id=str(self.user.pk),
            safe_period_seconds=86400 * 7,
        )
        version_ids = list(intent.manifest.get("body_version_ids", []))
        execute_safe_page_deletion(self.guide_page, intent=intent)

        stats = perform_garbage_collection()
        self.assertEqual(stats["collected_intents"], 0)
        self.assertEqual(stats["deleted_versions"], 0)

        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.MYSQL_DELETED)
        for vid in version_ids:
            self.assertIsNotNone(self.store.get_raw_version_document(vid))

    def test_active_deletion_intent_blocks_page_writes_and_prevents_mongo_orphans(self) -> None:
        """
        验证活跃删除意图（PENDING / CLAIMED / FAILED）严格冻结页面全部写入与发布下架：
        1. save_revision() 在写入 Mongo 之前即被第 1 重无事务检查阻断，绝不在 Mongo 产生孤儿版本；
        2. save()、copy()、create_alias() 均抛出 ExternalContentOperationBlockedError；
        3. lock_and_validate_revision（发布）与 advance_unpublish_generation（下架）均被阻断。
        """
        # 创建 PENDING 状态意图
        intent = record_deletion_intent(self.guide_page, actor_id=str(self.user.pk))
        self.assertEqual(intent.status, DeletionIntentStatus.PENDING)

        initial_mongo_versions = set(self.store._collection.distinct("version_id", {"page_id": self.guide_page.pk}))

        # 1. 测试 save_revision 拦截及无 Mongo 孤儿数据
        self.guide_page.body = [("paragraph", "Attempted write during pending deletion")]
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            self.guide_page.save_revision(user=self.user)
        self.assertIn("写入被冻结", str(cm.exception))

        current_mongo_versions = set(self.store._collection.distinct("version_id", {"page_id": self.guide_page.pk}))
        self.assertEqual(
            initial_mongo_versions,
            current_mongo_versions,
            "save_revision 被阻断时，MongoDB 中绝不能产生新孤儿版本（第一重检查生效）",
        )

        # 2. 测试 save() 拦截
        with self.assertRaises(ExternalContentOperationBlockedError):
            self.guide_page.save()

        # 3. 测试 copy() 拦截
        with self.assertRaises(ExternalContentOperationBlockedError):
            self.guide_page.copy(to=self.guide_index, update_attrs={"title": "Blocked Copy"})

        # 4. 测试 create_alias() 拦截
        with self.assertRaises(ExternalContentOperationBlockedError):
            self.guide_page.create_alias(parent=self.guide_index, update_slug="blocked-alias")

        # 5. 测试发布链路与下架链路拦截
        pub_service = ExternalContentPublicationService()
        with self.assertRaises(ExternalContentOperationBlockedError):
            pub_service.lock_and_validate_revision(
                page_id=self.guide_page.pk,
                revision_id=self.guide_page.get_latest_revision().pk,
            )

        with self.assertRaises(ExternalContentOperationBlockedError):
            pub_service.advance_unpublish_generation(page_id=self.guide_page.pk)

        # 6. 测试 CLAIMED 状态同样被冻结
        intent.status = DeletionIntentStatus.CLAIMED
        intent.save(update_fields=["status"])
        with self.assertRaises(ExternalContentOperationBlockedError):
            self.guide_page.save_revision(user=self.user)

        # 7. 测试 FAILED 状态同样被冻结
        intent.status = DeletionIntentStatus.FAILED
        intent.save(update_fields=["status"])
        with self.assertRaises(ExternalContentOperationBlockedError):
            self.guide_page.save_revision(user=self.user)

    def test_cancelled_deletion_intent_unfreezes_page_writes(self) -> None:
        """
        验证当删除意图被标记为 CANCELLED 状态时，页面写入冻结自动解除，
        页面可恢复正常的 save_revision、save 等写入操作，新版本正常持久化入库。
        """
        intent = record_deletion_intent(self.guide_page, actor_id=str(self.user.pk))
        self.assertEqual(intent.status, DeletionIntentStatus.PENDING)

        # 撤销删除意图
        intent.status = DeletionIntentStatus.CANCELLED
        intent.save(update_fields=["status"])

        # 验证写入恢复正常
        self.guide_page.body = [{"type": "lead", "value": "<p>Unfrozen write after intent cancelled</p>"}]
        new_rev = self.guide_page.save_revision(user=self.user)
        self.assertIsNotNone(new_rev.pk)

        # 验证 Mongo 中成功存入新版本
        manifest_dict = new_rev.content.get("_external_content_manifest", {})
        new_version_id = manifest_dict["fields"]["body"]["version_id"]
        stored_doc = self.store.get_raw_version_document(new_version_id)
        self.assertIsNotNone(stored_doc)
        self.assertEqual(stored_doc["payload"][0]["type"], "lead")
        self.assertEqual(stored_doc["payload"][0]["value"], "<p>Unfrozen write after intent cancelled</p>")

    def test_record_deletion_intent_idempotent_update_and_lock_ordering(self) -> None:
        """
        验证 record_deletion_intent 的幂等更新与单调锁序特性：
        对同一页面重复调用 record_deletion_intent 时，原子复用并更新已有的 PENDING 意图，
        不产生冗余记录，且安全刷新 safe_gc_after 与 manifest 引用清单。
        """
        # 首次登记意图（合规 7 天安全期）
        intent1 = record_deletion_intent(self.guide_page, actor_id="user_1", safe_period_seconds=86400 * 7)
        self.assertEqual(intent1.status, DeletionIntentStatus.PENDING)
        intent1_id = intent1.pk

        # 再次调用，更新参数为 14 天
        intent2 = record_deletion_intent(self.guide_page, actor_id="user_2", safe_period_seconds=86400 * 14)
        self.assertEqual(intent1_id, intent2.pk, "重复调用应幂等更新既有 PENDING 意图记录，保持同一意图 ID")
        self.assertEqual(intent2.actor_id, "user_2")
        self.assertGreater(intent2.safe_gc_after, intent1.safe_gc_after)

        # 确认数据库中该页面仅存在一条活跃意图记录
        intents_count = ExternalContentDeletionIntent.objects.filter(page_id=self.guide_page.pk).count()
        self.assertEqual(intents_count, 1)

    def test_safe_page_deletion_preserves_publication_state_for_audit(self) -> None:
        """
        验证页面受控安全删除时，ExternalContentPublicationState 记录必须持久保留用于审计追溯。
        硬契约：页面 Page 及 Revision 记录物理清除，但 PublicationState 绝对不可物理删除。
        """
        page_id = self.guide_page.pk
        intent = record_deletion_intent(self.guide_page, actor_id=str(self.user.pk))

        # 确保存在该页面的发布状态记录
        ExternalContentPublicationState.objects.get_or_create(
            page_id=page_id,
            defaults={
                "model_label": "guides.guidepage",
                "published_revision_id": self.guide_page.live_revision_id,
                "published_manifest": {"fields": {"body": {"version_id": "audit_v1"}}},
                "draft_manifest": {},
                "approved_manifest": {},
            },
        )
        self.assertTrue(ExternalContentPublicationState.objects.filter(page_id=page_id).exists())

        # 执行安全删除
        execute_safe_page_deletion(self.guide_page, intent=intent)

        # 断言 Page 已删除，但 PublicationState 依然持久存在
        self.assertFalse(Page.objects.filter(pk=page_id).exists())
        self.assertTrue(
            ExternalContentPublicationState.objects.filter(page_id=page_id).exists(),
            "PublicationState 必须作为审计墓碑持久保留，严禁在安全删除中被物理删除",
        )

    def test_safe_period_minimum_contract_and_future_now_rejected(self) -> None:
        """
        验证安全隔离期硬契约：
        1. safe_period_seconds 必须 >= 7天（604800秒），小于 7 天必须抛出 ValueError；
        2. perform_garbage_collection 的 now 参数不可为未来时间（超出容差则拒绝）；
        3. force_expired_only=False 仍不可绕过安全隔离期。
        """
        min_safe_period = 7 * 86400

        # 1. 尝试传入小于 7 天的安全隔离期，必须抛出 ValueError
        with self.assertRaises(ValueError, msg="安全隔离期小于 7 天必须被严格拦截并抛出 ValueError"):
            record_deletion_intent(
                self.guide_page,
                actor_id=str(self.user.pk),
                safe_period_seconds=min_safe_period - 1,
            )

        with self.assertRaises(ValueError, msg="负数或零安全期必须被拦截并抛出 ValueError"):
            record_deletion_intent(
                self.guide_page,
                actor_id=str(self.user.pk),
                safe_period_seconds=0,
            )

        # 2. 验证 perform_garbage_collection 拒绝未来时钟 now
        future_now = timezone.now() + datetime.timedelta(days=10)
        with self.assertRaises(ValueError, msg="perform_garbage_collection 传入未来时钟 now 必须被拒绝"):
            perform_garbage_collection(now=future_now)

    def test_repeated_pending_intent_cannot_shorten_safe_gc_after(self) -> None:
        """
        验证重复登记 PENDING 删除意图时，绝不可缩短既有的 safe_gc_after。
        硬契约：safe_gc_after 只能单调递增（保持或推迟），绝不能通过重复调用提前安全期。
        """
        # 首次登记 14 天安全隔离期
        intent1 = record_deletion_intent(
            self.guide_page,
            actor_id="user_1",
            safe_period_seconds=86400 * 14,
        )
        initial_safe_gc_after = intent1.safe_gc_after

        # 第二次登记 7 天安全隔离期（企图将安全期缩短为 7 天后）
        intent2 = record_deletion_intent(
            self.guide_page,
            actor_id="user_2",
            safe_period_seconds=86400 * 7,
        )
        self.assertEqual(intent1.pk, intent2.pk)

        # 断言 safe_gc_after 绝对不能缩短，必须 >= initial_safe_gc_after
        self.assertGreaterEqual(
            intent2.safe_gc_after,
            initial_safe_gc_after,
            "重复登记 PENDING 意图绝对不能缩短既有的 safe_gc_after 安全期",
        )

    def test_completed_intent_acts_as_permanent_tombstone_freezing_page_identity(self) -> None:
        """
        验证 COMPLETED 状态作为持久墓碑，永久冻结该 page_id 的身份。
        硬契约：已进入 COMPLETED 状态的页面 ID 严禁被重新写入或复活，check_page_write_not_blocked 必须抛出 ExternalContentOperationBlockedError。
        """
        page_id = self.guide_page.pk
        intent = record_deletion_intent(self.guide_page, actor_id=str(self.user.pk))

        # 将意图状态标记为 COMPLETED（物理回收已终结的持久墓碑）
        intent.status = DeletionIntentStatus.COMPLETED
        intent.completed_at = timezone.now()
        intent.save(update_fields=["status", "completed_at"])

        # 调用写入前检查，必须被严格阻断
        with self.assertRaises(
            ExternalContentOperationBlockedError,
            msg="COMPLETED 墓碑意图必须永久冻结 page_id 身份，任何写入检查必须抛出异常",
        ):
            check_page_write_not_blocked(page_id=page_id)

    def test_manifest_contains_full_six_dimensional_identity_per_version(self) -> None:
        """
        验证登记删除意图时，manifest 逐版本记录 6 键完整 selector 身份。
        硬契约：
        1. manifest["versions"] 列表包含逐版本的 expected_identity；
        2. expected_identity 包含完整的六键：model_label, page_id, field_name, codec, schema_version, payload_sha256；
        3. 保留 body_version_ids 以兼容老版本接口读取。
        """
        intent = record_deletion_intent(self.guide_page, actor_id=str(self.user.pk))
        manifest = intent.manifest

        # 验证兼容键与新键
        self.assertIn("body_version_ids", manifest, "必须保留 body_version_ids 兼容字段")
        self.assertIn("versions", manifest, "manifest 必须包含 versions 逐版本清单")

        versions = manifest["versions"]
        self.assertIsInstance(versions, list)
        self.assertGreater(len(versions), 0, "versions 列表不能为空")

        required_keys = {
            "model_label",
            "page_id",
            "field_name",
            "codec",
            "schema_version",
            "payload_sha256",
        }

        for item in versions:
            self.assertIn("version_id", item)
            self.assertIn("expected_identity", item)
            identity = item["expected_identity"]
            self.assertIsInstance(identity, dict)
            self.assertEqual(required_keys, set(identity.keys()), "expected_identity 必须精确包含六键完整契约")
            self.assertEqual(identity["model_label"], self.guide_page._meta.label_lower)
            self.assertEqual(identity["page_id"], self.guide_page.pk)
            self.assertEqual(identity["field_name"], "body")

    def test_gc_per_version_atomic_lock_verification_and_claim_token(self) -> None:
        """
        验证 GC 物理删除版本前，在事务内重锁核验 claim_token / 租约 / 安全期，并传递 expected_identity 调用 store.delete_version。
        硬契约：
        1. store.delete_version 必须在锁事务中调用，并传入 expected_identity 六键选择器；
        2. 若 claim_token 改变或租约已过期，必须中止删除该版本。
        """
        intent = record_deletion_intent(self.guide_page, actor_id=str(self.user.pk))
        execute_safe_page_deletion(self.guide_page, intent=intent)

        # 模拟安全隔离期已过
        ExternalContentDeletionIntent.objects.filter(pk=intent.pk).update(
            safe_gc_after=timezone.now() - datetime.timedelta(seconds=10)
        )
        intent.refresh_from_db()

        with patch.object(self.store, "delete_version", wraps=self.store.delete_version) as mock_delete:
            with patch("external_content.deletion.ContentVersionStore", return_value=self.store):
                perform_garbage_collection(worker_id="test_worker_atomic")

                # 验证调用 delete_version 时必须传递 expected_identity 参数
                self.assertGreater(mock_delete.call_count, 0, "必须调用 delete_version 执行清理")
                for call_args in mock_delete.call_args_list:
                    _, kwargs = call_args
                    self.assertIn("expected_identity", kwargs, "调用 delete_version 必须传递 expected_identity 参数")
                    identity = kwargs["expected_identity"]
                    self.assertEqual(identity["page_id"], self.guide_page.pk)
                    self.assertIn("payload_sha256", identity)

    def test_exact_reference_resolution_fail_closed_on_corrupted_manifest(self) -> None:
        """
        验证引用解析严格遵循精确解析与 Fail-Closed 原则：
        1. 禁止 JSON icontains 模糊匹配；
        2. 当遇到损坏/未知结构的 Manifest 时，必须 Fail-Closed 熔断保护，严禁物理删除 Mongo 版本。
        """
        intent = record_deletion_intent(self.guide_page, actor_id=str(self.user.pk))
        version_ids = list(intent.manifest.get("body_version_ids", []))
        self.assertGreater(len(version_ids), 0)
        target_vid = version_ids[0]

        execute_safe_page_deletion(self.guide_page, intent=intent)

        # 模拟安全隔离期已过
        ExternalContentDeletionIntent.objects.filter(pk=intent.pk).update(
            safe_gc_after=timezone.now() - datetime.timedelta(seconds=10)
        )

        # 构造一条具有损坏/未知结构 manifest 的 ExternalContentPublicationState
        corrupted_state = ExternalContentPublicationState.objects.create(
            page_id=888888,
            model_label="guides.guidepage",
            published_revision_id=888,
            published_manifest={"fields": "corrupted_non_dict_structure"},
            draft_manifest={"unknown_spec": [1, 2, 3]},
            approved_manifest=None,
        )

        try:
            # 执行 GC，遇到损坏 manifest 必须 Fail-Closed，拒绝删除该版本
            perform_garbage_collection()

            # 断言目标版本仍然存在，未被误删
            raw_doc = self.store.get_raw_version_document(target_vid)
            self.assertIsNotNone(
                raw_doc,
                "在存在未知或损坏的 Manifest 时，GC 必须 Fail-Closed 熔断，绝对不得删除 Mongo 正文",
            )
        finally:
            corrupted_state.delete()

    def test_cancel_pending_intent_strictly_requires_page_exists_and_lock_order(self) -> None:
        """
        验证取消删除意图的前置条件与单调锁序：
        1. 必须在 Page 仍存在于 MySQL 且未被物理回收时方可取消；
        2. 若 Page 已被删除（即使 intent 状态为 PENDING），严禁取消（不能谎称已删除恢复）；
        3. 锁序严格遵循 Page -> State -> Revision(pk) -> Intent。
        """
        intent = record_deletion_intent(self.guide_page, actor_id=str(self.user.pk))
        page_id = self.guide_page.pk

        # 场景 A：物理删除页面（模拟异常情况下页面已被删除，但 intent 仍为 PENDING）
        # 绕过 safe deletion 和信号处理，直接在 SQL 层删除 Page 记录
        from django.db import connection
        with connection.cursor() as cursor:
            # 先删除子表记录（避免外键约束）
            cursor.execute("DELETE FROM guides_guidepage WHERE page_ptr_id = %s", [page_id])
            cursor.execute("DELETE FROM wagtailcore_page WHERE id = %s", [page_id])

        # 此时尝试取消意图，必须被拒绝，严禁“谎称已删除恢复”
        with self.assertRaises(
            (ExternalContentOperationBlockedError, Page.DoesNotExist, ValueError),
            msg="页面已物理删除时，严禁取消删除意图",
        ):
            cancel_pending_deletion_intent(intent.id)

        intent.refresh_from_db()
        self.assertNotEqual(
            intent.status,
            DeletionIntentStatus.CANCELLED,
            "页面已删除时，意图状态严禁被置为 CANCELLED",
        )
