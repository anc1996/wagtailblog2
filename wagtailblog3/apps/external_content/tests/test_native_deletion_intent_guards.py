"""验证既有删除意图的拒绝门禁，不借测试额外事务掩盖原生 Action 的部分写入。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

from django.test import TestCase
from django.utils import timezone
from wagtail.actions.delete_page import DeletePageAction
from wagtail.models import Locale, Page, PageLogEntry, Revision

from external_content.deletion import (
    _deletion_context,
    execute_safe_page_deletion,
    record_deletion_intent,
)
from external_content.exceptions import ExternalContentOperationBlockedError
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)
from external_content.store import ContentVersionStore
from guides.models import GuideIndexPage, GuidePage, GuideTopic


class NativeDeletionIntentGuardTests(TestCase):
    """仅复用必要的数据形状，不继承或重复发现既有删除事务测试。"""

    forbidden_statuses = (
        DeletionIntentStatus.CLAIMED,
        DeletionIntentStatus.MYSQL_DELETED,
        DeletionIntentStatus.GC_ELIGIBLE,
        DeletionIntentStatus.COMPLETED,
        DeletionIntentStatus.FAILED,
    )

    def setUp(self) -> None:
        """准备独立指南树，并保留进入测试前全部删除线程上下文。"""
        super().setUp()
        self.context_before = vars(_deletion_context).copy()
        self.addCleanup(self._restore_deletion_context)
        vars(_deletion_context).clear()
        self.locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
        root = Page.get_first_root_node()
        if root is None:
            root = Page.add_root(title="删除意图测试根节点", locale=self.locale)
        suffix = uuid4().hex[:10]
        self.parent = GuideIndexPage(
            title="删除意图门禁测试索引",
            slug=f"intent-guard-{suffix}",
            locale=self.locale,
            live=False,
        )
        root.add_child(instance=self.parent)
        self.topic = GuideTopic.objects.create(
            name="删除意图门禁测试主题",
            slug=f"intent-guard-{suffix}",
        )

    def _restore_deletion_context(self) -> None:
        """恢复原对象引用及属性缺席状态，不清理其他测试的既有上下文。"""
        vars(_deletion_context).clear()
        vars(_deletion_context).update(self.context_before)

    def _create_page(self) -> GuidePage:
        """使用真实 Revision 保存合法草稿，避免下架门禁遮蔽删除意图缺陷。"""
        page = GuidePage(
            title="删除意图门禁测试指南",
            slug=f"intent-guard-guide-{uuid4().hex[:10]}",
            summary="验证既有意图不得被原生删除重新推进",
            topic=self.topic,
            locale=self.locale,
            difficulty="intermediate",
            live=False,
            body=[
                {"type": "lead", "value": "<p>删除意图门禁测试正文</p>"},
                {
                    "type": "step",
                    "value": {
                        "title": "门禁测试步骤",
                        "description": "<p>删除意图门禁测试步骤正文</p>",
                        "image": None,
                    },
                },
            ],
        )
        self.parent.add_child(instance=page)
        page.save_revision()
        page.refresh_from_db()
        return page

    def _create_intent(
        self,
        page: GuidePage,
        status: DeletionIntentStatus,
        *,
        intent_id: UUID | None = None,
    ) -> ExternalContentDeletionIntent:
        """直接预置既有意图，避免用被测登记函数生成拒绝用例的前置条件。"""
        return ExternalContentDeletionIntent.objects.create(
            id=intent_id or uuid4(),
            page_id=page.pk,
            model_label=page._meta.label_lower,
            status=status,
            manifest={
                "body_version_ids": [],
                "revision_ids": list(page.revisions.values_list("pk", flat=True)),
                "alias_page_ids": [],
                "versions": [],
            },
            safe_gc_after=timezone.now() + timedelta(days=14),
            actor_id="original-test-actor",
            request_id="original-test-request",
            generation=3,
            retry_count=2,
            claimed_worker="original-test-worker",
            claim_token=uuid4(),
            claimed_at=timezone.now(),
            claim_expires_at=timezone.now() + timedelta(hours=1),
            collected_versions=[],
            failed_versions={},
        )

    def _snapshot(self) -> dict[str, Any]:
        """直接读取 SQL 值，包含树路径、父计数、Revision、发布指针及审计日志。"""
        return deepcopy(
            {
                "pages": list(Page.objects.order_by("pk").values()),
                "guides": list(GuidePage.objects.order_by("pk").values()),
                "states": list(
                    ExternalContentPublicationState.objects.order_by("pk").values()
                ),
                "intents": list(
                    ExternalContentDeletionIntent.objects.order_by("pk").values()
                ),
                "revisions": list(Revision.objects.order_by("pk").values()),
                "logs": list(PageLogEntry.objects.order_by("pk").values()),
            }
        )

    def _assert_rejected_without_changes(
        self, page: GuidePage, operation: Callable[[], Any], *, with_sentinel: bool
    ) -> None:
        """拒绝后立即检查状态，不额外包 atomic 让测试自身替被测入口回滚。"""
        if with_sentinel:
            _deletion_context.deleting_page_ids = {-1}
        elif hasattr(_deletion_context, "deleting_page_ids"):
            delattr(_deletion_context, "deleting_page_ids")
        context_before = deepcopy(vars(_deletion_context))
        original_ids = getattr(_deletion_context, "deleting_page_ids", None)
        page_id_before = page.pk
        controlled_existed = hasattr(page, "_controlled_safe_deletion_in_progress")
        controlled_before = getattr(page, "_controlled_safe_deletion_in_progress", None)
        before = self._snapshot()
        try:
            with self.assertRaises(ExternalContentOperationBlockedError) as caught:
                operation()
            self.assertEqual(
                caught.exception.code, "external_content_operation_blocked"
            )
        finally:
            self.assertEqual(self._snapshot(), before)
            self.assertEqual(vars(_deletion_context), context_before)
            if with_sentinel:
                self.assertIs(_deletion_context.deleting_page_ids, original_ids)
            self.assertEqual(page.pk, page_id_before)
            self.assertEqual(
                hasattr(page, "_controlled_safe_deletion_in_progress"),
                controlled_existed,
            )
            self.assertIs(
                getattr(page, "_controlled_safe_deletion_in_progress", None),
                controlled_before,
            )

    def _assert_forbidden_statuses(self, *, native: bool) -> None:
        """五种禁止状态均须在真实登记或原生单页 Action 入口拒绝。"""
        for status in self.forbidden_statuses:
            with self.subTest(status=status):
                page = self._create_page()
                self._create_intent(page, status)
                operation = (
                    DeletePageAction(page).execute
                    if native
                    else lambda: record_deletion_intent(
                        page,
                        actor_id="replacement-test-actor",
                        request_id="replacement-test-request",
                    )
                )
                self._assert_rejected_without_changes(
                    page, operation, with_sentinel=False
                )

    def _assert_pending_mixed_with_forbidden(self, *, native: bool) -> None:
        """控制 UUID 升序而非插入顺序，防止只检查第一条意图漏掉禁止状态。"""
        for status in self.forbidden_statuses:
            for pending_first in (True, False):
                with self.subTest(status=status, pending_first=pending_first):
                    page = self._create_page()
                    prefix = uuid4().int >> 1
                    first_id = UUID(int=prefix << 1)
                    second_id = UUID(int=(prefix << 1) + 1)
                    self._create_intent(
                        page,
                        DeletionIntentStatus.PENDING,
                        intent_id=first_id if pending_first else second_id,
                    )
                    self._create_intent(
                        page,
                        status,
                        intent_id=second_id if pending_first else first_id,
                    )
                    operation = (
                        DeletePageAction(page).execute
                        if native
                        else lambda: record_deletion_intent(
                            page,
                            actor_id="replacement-test-actor",
                            request_id="replacement-test-request",
                        )
                    )
                    self._assert_rejected_without_changes(
                        page, operation, with_sentinel=True
                    )

    def test_record_rejects_each_forbidden_status(self) -> None:
        """禁止状态不得被重新登记成另一条等待意图。"""
        self._assert_forbidden_statuses(native=False)

    def test_native_action_rejects_each_forbidden_status(self) -> None:
        """原生 Action 不得绕过既有禁止状态物理删除草稿。"""
        self._assert_forbidden_statuses(native=True)

    def test_record_rejects_pending_mixed_with_each_forbidden_status(self) -> None:
        """存在等待意图也不得掩盖任一禁止状态或更新其邻近意图。"""
        self._assert_pending_mixed_with_forbidden(native=False)

    def test_native_action_rejects_pending_mixed_with_each_forbidden_status(
        self,
    ) -> None:
        """原生 Action 拒绝混存意图，且保留调用前无关上下文对象。"""
        self._assert_pending_mixed_with_forbidden(native=True)

    def test_cancelled_intent_allows_new_pending_registration(self) -> None:
        """仅有已取消意图时允许新登记，原取消记录及其他 SQL 实体不得变化。"""
        page = self._create_page()
        cancelled = self._create_intent(page, DeletionIntentStatus.CANCELLED)
        before = self._snapshot()
        context_before = deepcopy(vars(_deletion_context))
        earliest_safe_gc_after = timezone.now() + timedelta(days=7)

        intent = record_deletion_intent(
            page,
            actor_id="new-test-actor",
            request_id="new-test-request",
        )

        self.assertNotEqual(intent.pk, cancelled.pk)
        self.assertEqual(intent.page_id, page.pk)
        self.assertEqual(intent.model_label, "guides.guidepage")
        self.assertEqual(intent.status, DeletionIntentStatus.PENDING)
        self.assertEqual(intent.actor_id, "new-test-actor")
        self.assertEqual(intent.request_id, "new-test-request")
        self.assertGreaterEqual(intent.safe_gc_after, earliest_safe_gc_after)
        self.assertTrue(intent.manifest["body_version_ids"])
        self.assertEqual(
            intent.manifest["revision_ids"],
            list(page.revisions.order_by("pk").values_list("pk", flat=True)),
        )
        after = self._snapshot()
        self.assertEqual(len(after["intents"]), len(before["intents"]) + 1)
        self.assertEqual(
            [row for row in after["intents"] if row["id"] != intent.pk],
            before["intents"],
        )
        for table in ("pages", "guides", "states", "revisions", "logs"):
            self.assertEqual(after[table], before[table])
        self.assertEqual(vars(_deletion_context), context_before)

    def test_executor_rejects_intent_owned_by_another_page(self) -> None:
        """外页意图及伪造本页归属的外页实例均不得授权删除目标页面。"""
        for forge_page_id in (False, True):
            with self.subTest(forge_page_id=forge_page_id):
                page = self._create_page()
                other_page = self._create_page()
                self._create_intent(page, DeletionIntentStatus.PENDING)
                foreign_intent = self._create_intent(
                    other_page, DeletionIntentStatus.PENDING
                )
                if forge_page_id:
                    foreign_intent.page_id = page.pk
                self._assert_rejected_without_changes(
                    page,
                    lambda: execute_safe_page_deletion(page, intent=foreign_intent),
                    with_sentinel=True,
                )

    def test_executor_rejects_locked_cancelled_and_forbidden_intents(self) -> None:
        """受控执行只接受锁内等待意图，已取消与其他禁止状态不得被推进。"""
        for status in (DeletionIntentStatus.CANCELLED, *self.forbidden_statuses):
            with self.subTest(status=status):
                page = self._create_page()
                intent = self._create_intent(page, status)
                self._assert_rejected_without_changes(
                    page,
                    lambda: execute_safe_page_deletion(page, intent=intent),
                    with_sentinel=False,
                )

    def test_executor_rejects_forged_pending_when_database_status_blocks(
        self,
    ) -> None:
        """调用者把实例伪造为等待状态，仍须以锁内数据库状态拒绝删除。"""
        for status in (DeletionIntentStatus.CANCELLED, *self.forbidden_statuses):
            with self.subTest(database_status=status):
                page = self._create_page()
                intent = self._create_intent(page, status)
                forged_intent = deepcopy(intent)
                forged_intent.status = DeletionIntentStatus.PENDING
                self._assert_rejected_without_changes(
                    page,
                    lambda: execute_safe_page_deletion(page, intent=forged_intent),
                    with_sentinel=True,
                )

    def test_executor_rejects_multiple_pending_intents(self) -> None:
        """多条等待意图不能由传入第一条、第二条或自动登记任意消解歧义。"""
        for selection in ("first", "second", "automatic"):
            with self.subTest(selection=selection):
                page = self._create_page()
                first_intent = self._create_intent(page, DeletionIntentStatus.PENDING)
                second_intent = self._create_intent(page, DeletionIntentStatus.PENDING)
                intent = {
                    "first": first_intent,
                    "second": second_intent,
                    "automatic": None,
                }[selection]
                self._assert_rejected_without_changes(
                    page,
                    lambda: execute_safe_page_deletion(page, intent=intent),
                    with_sentinel=False,
                )

    def test_executor_rejects_mismatched_model_label(self) -> None:
        """数据库意图或传入实例的模型身份错误均须拒绝，不能只核对页面 ID。"""
        for persist_mismatch in (False, True):
            with self.subTest(persist_mismatch=persist_mismatch):
                page = self._create_page()
                intent = self._create_intent(page, DeletionIntentStatus.PENDING)
                intent.model_label = "guides.guideindexpage"
                if persist_mismatch:
                    intent.save(update_fields=["model_label"])
                self._assert_rejected_without_changes(
                    page,
                    lambda: execute_safe_page_deletion(page, intent=intent),
                    with_sentinel=True,
                )

    def test_executor_rejects_pending_mixed_with_each_forbidden_status(self) -> None:
        """明确传入等待意图也不能掩盖同页其他禁止意图，覆盖两种锁序。"""
        for status in self.forbidden_statuses:
            for pending_first in (True, False):
                with self.subTest(status=status, pending_first=pending_first):
                    page = self._create_page()
                    prefix = uuid4().int >> 1
                    first_id = UUID(int=prefix << 1)
                    second_id = UUID(int=(prefix << 1) + 1)
                    pending_intent = self._create_intent(
                        page,
                        DeletionIntentStatus.PENDING,
                        intent_id=first_id if pending_first else second_id,
                    )
                    self._create_intent(
                        page,
                        status,
                        intent_id=second_id if pending_first else first_id,
                    )
                    self._assert_rejected_without_changes(
                        page,
                        lambda: execute_safe_page_deletion(page, intent=pending_intent),
                        with_sentinel=True,
                    )

    def _controlled_flag_cases(self) -> tuple[tuple[str, bool, Any], ...]:
        """使用对象哨兵检测原值身份，不能用统一 False 掩盖调用前状态。"""
        return (
            ("absent", False, None),
            ("false", True, False),
            ("true", True, True),
            ("sentinel", True, object()),
        )

    def _prepare_controlled_flag(
        self, page: GuidePage, *, existed: bool, value: Any
    ) -> None:
        """缺席用例保留线程窗口缺席，已有标志用例另保留无关窗口对象。"""
        if existed:
            setattr(page, "_controlled_safe_deletion_in_progress", value)
            _deletion_context.deleting_page_ids = {-1}
        else:
            if hasattr(page, "_controlled_safe_deletion_in_progress"):
                delattr(page, "_controlled_safe_deletion_in_progress")
            if hasattr(_deletion_context, "deleting_page_ids"):
                delattr(_deletion_context, "deleting_page_ids")

    def _assert_controlled_flag_restored(
        self, page: GuidePage, *, existed: bool, value: Any
    ) -> None:
        """成功和失败均必须恢复属性存在性与原对象，而非写回布尔默认值。"""
        self.assertEqual(
            hasattr(page, "_controlled_safe_deletion_in_progress"), existed
        )
        if existed:
            self.assertIs(
                getattr(page, "_controlled_safe_deletion_in_progress"), value
            )

    def _complete_version_manifest(self, page: GuidePage) -> dict[str, Any]:
        """从真实指针与历史 Revision 独立构造完整快照，不调用被测登记入口。"""
        state = ExternalContentPublicationState.objects.get(page_id=page.pk)
        revisions = list(page.revisions.order_by("pk"))
        manifests = [
            state.published_manifest,
            state.approved_manifest,
            state.draft_manifest,
            *[
                revision.content["_external_content_manifest"]
                for revision in revisions
            ],
        ]
        versions: dict[str, dict[str, Any]] = {}
        for manifest in manifests:
            if not manifest:
                continue
            for field_name, pointer in manifest["fields"].items():
                version_id = str(pointer["version_id"])
                entry = {
                    "version_id": version_id,
                    "expected_identity": {
                        "model_label": "guides.guidepage",
                        "page_id": page.pk,
                        "field_name": field_name,
                        "codec": pointer["codec"],
                        "schema_version": pointer["schema_version"],
                        "payload_sha256": pointer["payload_sha256"],
                    },
                }
                if version_id in versions:
                    self.assertEqual(versions[version_id], entry)
                versions[version_id] = entry
        self.assertTrue(versions)
        return {
            "body_version_ids": sorted(versions),
            "revision_ids": [revision.pk for revision in revisions],
            "alias_page_ids": list(
                page.aliases.order_by("pk").values_list("pk", flat=True)
            ),
            "versions": list(versions.values()),
        }

    def test_executor_success_restores_controlled_flag_and_context(self) -> None:
        """真实受控删除成功后恢复缺席、False、True、哨兵四种调用前标志。"""
        for name, existed, value in self._controlled_flag_cases():
            with self.subTest(controlled_flag=name):
                page = self._create_page()
                page_id = page.pk
                revision_ids = list(page.revisions.values_list("pk", flat=True))
                intent = self._create_intent(page, DeletionIntentStatus.PENDING)
                manifest_before = self._complete_version_manifest(page)
                intent.manifest = deepcopy(manifest_before)
                intent.save(update_fields=["manifest"])
                store = ContentVersionStore()
                documents_before = {
                    entry["version_id"]: store.get_version(
                        entry["version_id"],
                        expected_identity=entry["expected_identity"],
                    )
                    for entry in manifest_before["versions"]
                }
                parent_numchild_before = Page.objects.values_list(
                    "numchild", flat=True
                ).get(pk=self.parent.pk)
                before = self._snapshot()
                self._prepare_controlled_flag(page, existed=existed, value=value)
                context_before = deepcopy(vars(_deletion_context))
                original_ids = getattr(_deletion_context, "deleting_page_ids", None)

                try:
                    completed = execute_safe_page_deletion(page, intent=intent)
                    self.assertEqual(completed.pk, intent.pk)
                    intent.refresh_from_db()
                    self.assertEqual(intent.status, DeletionIntentStatus.MYSQL_DELETED)
                    self.assertEqual(intent.manifest, manifest_before)
                    completed.refresh_from_db()
                    self.assertEqual(completed.manifest, manifest_before)
                    for entry in manifest_before["versions"]:
                        self.assertEqual(
                            store.get_version(
                                entry["version_id"],
                                expected_identity=entry["expected_identity"],
                            ),
                            documents_before[entry["version_id"]],
                        )
                    self.assertFalse(Page.objects.filter(pk=page_id).exists())
                    self.assertFalse(GuidePage.objects.filter(pk=page_id).exists())
                    self.assertFalse(
                        Revision.objects.filter(pk__in=revision_ids).exists()
                    )
                    after = self._snapshot()
                    self.assertEqual(after["states"], before["states"])
                    self.assertEqual(len(after["intents"]), len(before["intents"]))
                    self.assertEqual(
                        [row for row in after["intents"] if row["id"] != intent.pk],
                        [row for row in before["intents"] if row["id"] != intent.pk],
                    )
                    self.assertEqual(
                        Page.objects.values_list("numchild", flat=True).get(
                            pk=self.parent.pk
                        ),
                        parent_numchild_before - 1,
                    )
                finally:
                    self._assert_controlled_flag_restored(
                        page, existed=existed, value=value
                    )
                    self.assertEqual(vars(_deletion_context), context_before)
                    if existed:
                        self.assertIs(_deletion_context.deleting_page_ids, original_ids)

    def test_executor_late_failure_restores_controlled_flag_and_context(self) -> None:
        """真实删除返回后意图刷新故障须回滚 SQL，四种受控标志均精确恢复。"""
        for name, existed, value in self._controlled_flag_cases():
            with self.subTest(controlled_flag=name):
                page = self._create_page()
                page_id = page.pk
                intent = self._create_intent(page, DeletionIntentStatus.PENDING)
                self._prepare_controlled_flag(page, existed=existed, value=value)
                context_before = deepcopy(vars(_deletion_context))
                original_ids = getattr(_deletion_context, "deleting_page_ids", None)
                before = self._snapshot()
                fault = RuntimeError("测试注入：受控删除返回后意图刷新失败")
                refresh_attempts: list[UUID] = []
                original_refresh = ExternalContentDeletionIntent.refresh_from_db

                def refresh_intent(
                    instance: ExternalContentDeletionIntent,
                    *args: Any,
                    **kwargs: Any,
                ) -> Any:
                    """让原生 post_delete 完整执行，仅在执行器删除返回后的刷新点故障。"""
                    if instance.pk == intent.pk:
                        self.assertFalse(Page.objects.filter(pk=page_id).exists())
                        self.assertFalse(GuidePage.objects.filter(pk=page_id).exists())
                        self._assert_controlled_flag_restored(
                            page, existed=existed, value=value
                        )
                        self.assertEqual(vars(_deletion_context), context_before)
                        if existed:
                            self.assertIs(
                                _deletion_context.deleting_page_ids, original_ids
                            )
                        refresh_attempts.append(instance.pk)
                        raise fault
                    return original_refresh(instance, *args, **kwargs)

                try:
                    with patch.object(
                        ExternalContentDeletionIntent,
                        "refresh_from_db",
                        autospec=True,
                        side_effect=refresh_intent,
                    ):
                        with self.assertRaises(RuntimeError) as caught:
                            execute_safe_page_deletion(page, intent=intent)
                    self.assertIs(caught.exception, fault)
                    self.assertEqual(refresh_attempts, [intent.pk])
                finally:
                    self.assertEqual(self._snapshot(), before)
                    self._assert_controlled_flag_restored(
                        page, existed=existed, value=value
                    )
                    self.assertEqual(vars(_deletion_context), context_before)
                    if existed:
                        self.assertIs(_deletion_context.deleting_page_ids, original_ids)
