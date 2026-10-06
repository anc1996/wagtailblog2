"""验证真实自动提交环境中原生删除入口的完整事务与线程上下文回滚。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import TransactionTestCase
from wagtail.actions.delete_page import DeletePageAction
from wagtail.models import Locale, Page, PageLogEntry, Revision

from external_content.deletion import _deletion_context
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)
from external_content.store import ContentVersionStore
from guides.models import GuideIndexPage, GuidePage, GuideTopic


class NativeDeletionTransactionTests(TransactionTestCase):
    """不提供外层事务，不替换原生 Action、Collector 或信号，只注入意图保存故障。"""

    def setUp(self) -> None:
        """创建合法索引、主题及两份已写入真实正文存储的草稿 Revision。"""
        super().setUp()
        self._assert_autocommit()
        self.original_context = vars(_deletion_context).copy()
        self.addCleanup(self._restore_original_context)
        self.locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
        suffix = uuid4().hex[:10]
        root = Page.get_first_root_node()
        if root is None:
            root = Page.add_root(
                title="原生事务测试根节点",
                slug=f"native-transaction-root-{suffix}",
                locale=self.locale,
                live=False,
            )
        self.parent = GuideIndexPage(
            title="原生事务测试索引",
            slug=f"native-transactions-{suffix}",
            locale=self.locale,
            live=False,
        )
        root.add_child(instance=self.parent)
        self.topic = GuideTopic.objects.create(
            name="原生事务测试主题",
            slug=f"native-transactions-{suffix}",
        )
        self.store = ContentVersionStore()
        self.pages = [self._create_draft() for _ in range(2)]
        self.page_ids = [page.pk for page in self.pages]
        self.assertFalse(
            ExternalContentDeletionIntent.objects.filter(
                page_id__in=self.page_ids
            ).exists()
        )
        self._assert_autocommit()

    def _restore_original_context(self) -> None:
        """在契约断言完成后恢复测试前线程属性，不把清理动作当成恢复证据。"""
        for attr_name in list(vars(_deletion_context)):
            if attr_name not in self.original_context:
                delattr(_deletion_context, attr_name)
        for attr_name, original_value in self.original_context.items():
            setattr(_deletion_context, attr_name, original_value)

    def _assert_autocommit(self) -> None:
        """明确排除 TestCase 或调用方外层事务掩盖原生提交边界。"""
        self.assertIs(connection.get_autocommit(), True)
        self.assertFalse(connection.in_atomic_block)

    def _create_draft(self) -> GuidePage:
        """通过真实保存入口建立含主题、导读和步骤的草稿及 Mongo 正文版本。"""
        page = GuidePage(
            title="原生事务测试草稿",
            slug=f"native-transaction-guide-{uuid4().hex[:10]}",
            summary="验证原生删除故障回滚",
            topic=self.topic,
            locale=self.locale,
            difficulty="intermediate",
            live=False,
            body=[
                {"type": "lead", "value": "<p>原生事务测试导读</p>"},
                {
                    "type": "step",
                    "value": {
                        "title": "操作步骤一",
                        "description": "<p>原生事务测试步骤说明</p>",
                        "image": None,
                    },
                },
            ],
        )
        self.parent.add_child(instance=page)
        revision = page.save_revision()
        page.refresh_from_db()
        self.assertFalse(page.live)
        state = ExternalContentPublicationState.objects.get(page_id=page.pk)
        manifest = revision.content["_external_content_manifest"]
        self.assertEqual(state.draft_revision_id, revision.pk)
        self.assertEqual(state.draft_manifest, manifest)
        entry = manifest["fields"]["body"]
        document = self.store.get_version(
            entry["version_id"],
            expected_identity={
                "model_label": manifest["model_label"],
                "page_id": page.pk,
                "field_name": "body",
                "codec": entry["codec"],
                "schema_version": entry["schema_version"],
                "payload_sha256": entry["payload_sha256"],
            },
        )
        self.assertTrue(document["payload"])
        return page

    def _snapshot(self) -> dict[str, Any]:
        """捕获完整 SQL 基线，包含全部审计日志及全树结构计数。"""
        return deepcopy(
            {
                "pages": list(Page.objects.order_by("pk").values()),
                "guides": list(GuidePage.objects.order_by("pk").values()),
                "revisions": list(Revision.objects.order_by("pk").values()),
                "states": list(
                    ExternalContentPublicationState.objects.order_by("pk").values()
                ),
                "intents": list(
                    ExternalContentDeletionIntent.objects.order_by("pk").values()
                ),
                "logs": list(PageLogEntry.objects.order_by("pk").values()),
                "tree": list(
                    Page.objects.order_by("path").values_list(
                        "pk", "path", "depth", "numchild"
                    )
                ),
            }
        )

    def _assert_direct_rollback(self, *, cascade: bool) -> None:
        """从自动提交直接删除，最高主键后删除失败须撤销全部 SQL 和窗口变化。"""
        if cascade:
            _deletion_context.deleting_page_ids = {-1}
        elif hasattr(_deletion_context, "deleting_page_ids"):
            delattr(_deletion_context, "deleting_page_ids")
        context_before = deepcopy(vars(_deletion_context))
        for page in self.pages:
            self.assertNotIn(
                page.pk, getattr(_deletion_context, "deleting_page_ids", set())
            )
            self.assertFalse(
                getattr(page, "_controlled_safe_deletion_in_progress", False)
            )
        before = self._snapshot()
        target_id = max(self.page_ids)
        fault = RuntimeError("测试注入：自动提交入口最高主键后删除保存失败")
        transition_attempts: list[int] = []
        original_save = ExternalContentDeletionIntent.save

        def save_intent(
            instance: ExternalContentDeletionIntent, *args: Any, **kwargs: Any
        ) -> Any:
            """仅在真实后删除推进点故障，允许原始预删除登记正常执行。"""
            if (
                instance.page_id in self.page_ids
                and instance.status == DeletionIntentStatus.MYSQL_DELETED
            ):
                transition_attempts.append(instance.page_id)
                if instance.page_id == target_id:
                    self.assertTrue(connection.in_atomic_block)
                    self.assertEqual(
                        set(
                            ExternalContentDeletionIntent.objects.filter(
                                page_id__in=self.page_ids,
                                status=DeletionIntentStatus.PENDING,
                            ).values_list("page_id", flat=True)
                        ),
                        set(self.page_ids),
                    )
                    self.assertFalse(
                        GuidePage.objects.filter(pk__in=self.page_ids).exists()
                    )
                    raise fault
            return original_save(instance, *args, **kwargs)

        with patch.object(
            ExternalContentDeletionIntent,
            "save",
            autospec=True,
            side_effect=save_intent,
        ):
            self._assert_autocommit()
            with self.assertRaises(RuntimeError) as caught:
                if cascade:
                    DeletePageAction(self.parent).execute()
                else:
                    GuidePage.objects.filter(pk__in=self.page_ids).delete()
        self.assertIs(caught.exception, fault)
        self.assertEqual(transition_attempts, [target_id])
        self._assert_autocommit()
        after = self._snapshot()
        for snapshot_name, expected in before.items():
            with self.subTest(snapshot=snapshot_name):
                self.assertEqual(after[snapshot_name], expected)
        with self.subTest(snapshot="threadlocal_presence"):
            self.assertEqual(
                hasattr(_deletion_context, "deleting_page_ids"),
                "deleting_page_ids" in context_before,
            )
        with self.subTest(snapshot="threadlocal_values"):
            self.assertEqual(vars(_deletion_context), context_before)

    def test_direct_parent_action_failure_rolls_back_without_outer_atomic(self) -> None:
        """父页原生 Action 从自动提交执行，日志、全树及无关线程标记须回滚。"""
        self._assert_direct_rollback(cascade=True)

    def test_direct_queryset_failure_rolls_back_without_outer_atomic(self) -> None:
        """两页原生 QuerySet 删除从自动提交执行，所有实体及缺席窗口须恢复。"""
        self._assert_direct_rollback(cascade=False)
