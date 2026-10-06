"""验证原生单页、批量删除的意图推进、故障回滚与受控入口单次执行契约。"""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from typing import Any
from uuid import uuid4
from unittest.mock import patch

from django.db import transaction
from django.test import RequestFactory, SimpleTestCase, TestCase
from wagtail import hooks
from wagtail.actions.delete_page import DeletePageAction
from wagtail.admin.views.pages.bulk_actions.delete import DeleteBulkAction
from wagtail.models import Locale, Page, PageLogEntry, Revision

from external_content.deletion import (
    _deletion_context,
    execute_safe_page_deletion,
    record_deletion_intent,
    safe_deletion_scope,
)
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)
from guides.models import GuideIndexPage, GuidePage, GuideTopic


class NativeDeletionAtomicityTests(TestCase):
    """使用独立指南数据走已安装 Wagtail 的真实删除入口，不模拟业务信号。"""

    def setUp(self) -> None:
        """准备合法正文及独立树节点，不创建登录用户或读取外部页面。"""
        super().setUp()
        self.context_existed = hasattr(_deletion_context, "deleting_page_ids")
        self.context_before = deepcopy(
            getattr(_deletion_context, "deleting_page_ids", None)
        )
        self.addCleanup(self._restore_deletion_context)
        self.locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
        root = Page.get_first_root_node()
        if root is None:
            root = Page.add_root(title="测试根节点", locale=self.locale)
        suffix = uuid4().hex[:10]
        self.parent = GuideIndexPage(
            title="原生删除事务测试索引",
            slug=f"native-deletion-{suffix}",
            locale=self.locale,
            live=False,
        )
        root.add_child(instance=self.parent)
        self.topic = GuideTopic.objects.create(
            name="原生删除事务测试主题",
            slug=f"native-deletion-{suffix}",
        )

    def _restore_deletion_context(self) -> None:
        """故障用例结束后恢复原有线程上下文，不污染后续测试。"""
        if self.context_existed:
            _deletion_context.deleting_page_ids = self.context_before
        elif hasattr(_deletion_context, "deleting_page_ids"):
            delattr(_deletion_context, "deleting_page_ids")

    def _create_page(self, *, live: bool) -> GuidePage:
        """创建带主题、导读和步骤的指南，按需通过真实 Revision 发布。"""
        page = GuidePage(
            title="原生删除事务测试指南",
            slug=f"native-guide-{uuid4().hex[:10]}",
            summary="验证删除事务边界",
            topic=self.topic,
            locale=self.locale,
            difficulty="intermediate",
            live=False,
            body=[
                {"type": "lead", "value": "<p>独立测试导读正文</p>"},
                {
                    "type": "step",
                    "value": {
                        "title": "操作步骤一",
                        "description": "<p>独立测试步骤正文</p>",
                        "image": None,
                    },
                },
            ],
        )
        self.parent.add_child(instance=page)
        revision = page.save_revision()
        if live:
            revision.publish()
        page.refresh_from_db()
        return page

    def _assert_native_context(self, pages: list[GuidePage]) -> None:
        """原生入口调用前不得预设受控标志或删除窗口豁免。"""
        deleting_ids = getattr(_deletion_context, "deleting_page_ids", set())
        for page in pages:
            self.assertNotIn(page.pk, deleting_ids)
            self.assertFalse(
                getattr(page, "_controlled_safe_deletion_in_progress", False)
            )

    def _run_native_delete(
        self, pages: list[GuidePage], *, bulk: bool
    ) -> tuple[int, int] | None:
        """复现原生单页视图和批量视图的外层事务，执行真实 Action。"""
        self._assert_native_context(pages)
        with transaction.atomic():
            if bulk:
                return DeleteBulkAction.execute_action(pages)
            self.assertEqual(len(pages), 1)
            DeletePageAction(pages[0]).execute()
        return None

    def _snapshot(self, page_ids: list[int]) -> dict[str, Any]:
        """记录 SQL 实体、指针、意图、树计数与审计日志的完整回滚基线。"""
        return deepcopy(
            {
                "pages": list(
                    Page.objects.filter(pk__in=page_ids).order_by("pk").values()
                ),
                "guides": list(
                    GuidePage.objects.filter(pk__in=page_ids).order_by("pk").values()
                ),
                "states": list(
                    ExternalContentPublicationState.objects.filter(
                        page_id__in=page_ids
                    ).order_by("pk").values()
                ),
                "intents": list(
                    ExternalContentDeletionIntent.objects.filter(
                        page_id__in=page_ids
                    ).order_by("pk").values()
                ),
                "revisions": list(
                    Revision.objects.filter(
                        pk__in=[
                            revision.pk
                            for page in GuidePage.objects.filter(pk__in=page_ids)
                            for revision in page.revisions.all()
                        ]
                    ).order_by("pk").values()
                ),
                "numchild": Page.objects.values_list("numchild", flat=True).get(
                    pk=self.parent.pk
                ),
                "logs": list(PageLogEntry.objects.order_by("pk").values()),
            }
        )

    def _assert_pending_success(self, *, bulk: bool) -> None:
        """已有等待意图的正式页面必须由原生删除推进，不借受控删除窗口。"""
        pages = [self._create_page(live=True) for _ in range(2 if bulk else 1)]
        page_ids = [page.pk for page in pages]
        intents = [record_deletion_intent(page) for page in pages]
        manifests = [deepcopy(intent.manifest) for intent in intents]
        for page, intent in zip(pages, intents):
            self.assertTrue(page.live)
            self.assertEqual(intent.status, DeletionIntentStatus.PENDING)
            self.assertTrue(intent.manifest["body_version_ids"])
        result = self._run_native_delete(pages, bulk=bulk)
        if bulk:
            self.assertEqual(result, (2, 0))
        self.assertFalse(Page.objects.filter(pk__in=page_ids).exists())
        self.assertFalse(GuidePage.objects.filter(pk__in=page_ids).exists())
        self.assertEqual(
            ExternalContentPublicationState.objects.filter(
                page_id__in=page_ids
            ).count(),
            len(pages),
        )
        self.assertEqual(
            ExternalContentDeletionIntent.objects.filter(
                page_id__in=page_ids
            ).count(),
            len(pages),
        )
        for intent, manifest in zip(intents, manifests):
            intent.refresh_from_db()
            self.assertEqual(intent.status, DeletionIntentStatus.MYSQL_DELETED)
            self.assertEqual(intent.manifest, manifest)
        self.assertEqual(
            Page.objects.values_list("numchild", flat=True).get(pk=self.parent.pk),
            0,
        )
        for page_id in page_ids:
            self.assertNotIn(
                page_id, getattr(_deletion_context, "deleting_page_ids", set())
            )

    def _assert_post_delete_rollback(self, *, bulk: bool, at_save: bool) -> None:
        """草稿避免下架门禁遮蔽注入；批量在第二页失败验证首个删除也回滚。"""
        pages = [self._create_page(live=False) for _ in range(2 if bulk else 1)]
        page_ids = [page.pk for page in pages]
        intents = [record_deletion_intent(page) for page in pages]
        target_id = page_ids[-1]
        target_intent_id = intents[-1].pk
        before = self._snapshot(page_ids)
        fault = RuntimeError("测试注入：原生 post_delete 意图推进失败")
        injected: list[int] = []
        advanced: list[int] = []
        original_filter = ExternalContentDeletionIntent.objects.filter
        original_save = ExternalContentDeletionIntent.save

        def filter_intents(*args: Any, **kwargs: Any) -> Any:
            """仅在目标正文行已删除后的状态查询故障，不误伤 pre_delete。"""
            if (
                not at_save
                and kwargs.get("page_id") == target_id
                and kwargs.get("status") == DeletionIntentStatus.PENDING
                and not GuidePage.objects.filter(pk=target_id).exists()
            ):
                injected.append(target_id)
                raise fault
            return original_filter(*args, **kwargs)

        def save_intent(
            instance: ExternalContentDeletionIntent, *args: Any, **kwargs: Any
        ) -> Any:
            """在真实后删除状态保存点注入，其他意图继续调用原始保存。"""
            if (
                instance.page_id in page_ids
                and instance.status == DeletionIntentStatus.MYSQL_DELETED
            ):
                self.assertFalse(
                    GuidePage.objects.filter(pk=instance.page_id).exists()
                )
                if at_save and instance.pk == target_intent_id:
                    injected.append(target_id)
                    raise fault
                result = original_save(instance, *args, **kwargs)
                advanced.append(instance.page_id)
                return result
            return original_save(instance, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    ExternalContentDeletionIntent.objects,
                    "filter",
                    side_effect=filter_intents,
                )
            )
            stack.enter_context(
                patch.object(
                    ExternalContentDeletionIntent,
                    "save",
                    autospec=True,
                    side_effect=save_intent,
                )
            )
            with self.assertRaises(RuntimeError) as caught:
                self._run_native_delete(pages, bulk=bulk)
        self.assertIs(caught.exception, fault)
        self.assertEqual(injected, [target_id])
        self.assertEqual(advanced, page_ids[:-1])
        self.assertEqual(self._snapshot(page_ids), before)

    def _assert_fresh_collector_rollback(
        self, *, cascade: bool, with_sentinel: bool
    ) -> None:
        """同一收集器先自动登记全部意图，最高主键后删除失败须恢复完整窗口。"""
        pages = [self._create_page(live=False) for _ in range(2)]
        page_ids = [page.pk for page in pages]
        target_id = max(page_ids)
        self.assertFalse(
            ExternalContentDeletionIntent.objects.filter(
                page_id__in=page_ids
            ).exists()
        )
        sentinel = -1
        if with_sentinel:
            _deletion_context.deleting_page_ids = {sentinel}
        elif hasattr(_deletion_context, "deleting_page_ids"):
            delattr(_deletion_context, "deleting_page_ids")
        context_before = deepcopy(vars(_deletion_context))
        self._assert_native_context(pages)
        snapshot_ids = [
            *page_ids,
            self.parent.pk,
            self.parent.get_parent().pk,
        ]
        before = self._snapshot(snapshot_ids)
        fault = RuntimeError("测试注入：同一收集器最高主键后删除失败")
        transition_attempts: list[int] = []
        original_save = ExternalContentDeletionIntent.save

        def save_intent(
            instance: ExternalContentDeletionIntent, *args: Any, **kwargs: Any
        ) -> Any:
            """在真正后删除保存时失败，其余自动登记和信号仍执行原始逻辑。"""
            if (
                instance.page_id in page_ids
                and instance.status == DeletionIntentStatus.MYSQL_DELETED
            ):
                transition_attempts.append(instance.page_id)
                if instance.page_id == target_id:
                    self.assertEqual(
                        set(
                            ExternalContentDeletionIntent.objects.filter(
                                page_id__in=page_ids,
                                status=DeletionIntentStatus.PENDING,
                            ).values_list("page_id", flat=True)
                        ),
                        set(page_ids),
                    )
                    deleting_ids = _deletion_context.deleting_page_ids
                    self.assertTrue(set(page_ids).issubset(deleting_ids))
                    if with_sentinel:
                        self.assertIn(sentinel, deleting_ids)
                    self.assertFalse(
                        GuidePage.objects.filter(pk__in=page_ids).exists()
                    )
                    raise fault
            return original_save(instance, *args, **kwargs)

        with patch.object(
            ExternalContentDeletionIntent,
            "save",
            autospec=True,
            side_effect=save_intent,
        ):
            with self.assertRaises(RuntimeError) as caught:
                with transaction.atomic():
                    if cascade:
                        DeletePageAction(self.parent).execute()
                    else:
                        GuidePage.objects.filter(pk__in=page_ids).delete()
        self.assertIs(caught.exception, fault)
        self.assertEqual(transition_attempts, [target_id])
        self.assertEqual(self._snapshot(snapshot_ids), before)
        self.assertFalse(
            ExternalContentDeletionIntent.objects.filter(
                page_id__in=page_ids
            ).exists()
        )
        self.assertEqual(
            hasattr(_deletion_context, "deleting_page_ids"),
            "deleting_page_ids" in context_before,
        )
        self.assertEqual(vars(_deletion_context), context_before)

    def test_native_single_existing_pending_reaches_mysql_deleted(self) -> None:
        """原生单页删除已有等待意图的正式页面，成功推进墓碑状态。"""
        self._assert_pending_success(bulk=False)

    def test_native_bulk_existing_pending_reaches_mysql_deleted(self) -> None:
        """原生批量入口删除两个已有等待意图的正式页面，均推进墓碑。"""
        self._assert_pending_success(bulk=True)

    def test_single_post_delete_failure_before_intent_save_rolls_back(self) -> None:
        """单页后删除查询故障必须传播，并回滚页面、意图与树。"""
        self._assert_post_delete_rollback(bulk=False, at_save=False)

    def test_bulk_post_delete_failure_before_intent_save_rolls_back(self) -> None:
        """批量第二页后删除查询故障必须回滚首个页面的已完成删除。"""
        self._assert_post_delete_rollback(bulk=True, at_save=False)

    def test_single_post_delete_intent_save_failure_rolls_back(self) -> None:
        """单页意图保存故障必须传播，不得吞错并提交物理删除。"""
        self._assert_post_delete_rollback(bulk=False, at_save=True)

    def test_bulk_post_delete_intent_save_failure_rolls_back(self) -> None:
        """批量第二页意图保存故障必须回滚整批页面和意图推进。"""
        self._assert_post_delete_rollback(bulk=True, at_save=True)

    def test_fresh_queryset_bulk_failure_restores_absent_context(self) -> None:
        """无既有意图的原生批量收集器失败，低主键窗口不得残留。"""
        self._assert_fresh_collector_rollback(cascade=False, with_sentinel=False)

    def test_fresh_queryset_bulk_failure_preserves_sentinel(self) -> None:
        """原生批量故障只清本批自动登记窗口，原有无关标记必须保留。"""
        self._assert_fresh_collector_rollback(cascade=False, with_sentinel=True)

    def test_fresh_parent_cascade_failure_restores_absent_context(self) -> None:
        """父页真实级联删除故障，页面和自动登记意图回滚且窗口属性恢复缺席。"""
        self._assert_fresh_collector_rollback(cascade=True, with_sentinel=False)

    def test_fresh_parent_cascade_failure_preserves_sentinel(self) -> None:
        """父页级联故障不得清空原有无关标记，父子树与意图必须整体回滚。"""
        self._assert_fresh_collector_rollback(cascade=True, with_sentinel=True)

    def test_before_delete_hooks_do_not_execute_safe_deletion(self) -> None:
        """单页前置 hook 仅检查门禁，不提前登记或执行安全物理删除。"""
        page = self._create_page(live=True)
        page_ids = [page.pk]
        before = self._snapshot(page_ids)
        request = RequestFactory().post("/admin/pages/test/delete/")
        with (
            patch(
                "external_content.deletion.execute_safe_page_deletion"
            ) as safe_delete,
            patch(
                "external_content.deletion.record_deletion_intent"
            ) as record_intent,
            patch.object(DeletePageAction, "execute", autospec=True) as action,
        ):
            callbacks = hooks.get_hooks("before_delete_page")
            self.assertTrue(callbacks)
            for callback in callbacks:
                self.assertIsNone(callback(request, page))
            safe_delete.assert_not_called()
            record_intent.assert_not_called()
            action.assert_not_called()
        self.assertEqual(self._snapshot(page_ids), before)

    def test_controlled_deletion_executes_native_action_exactly_once(self) -> None:
        """受控入口复用已有意图，只执行一次原生 Action 并推进墓碑。"""
        page = self._create_page(live=True)
        page_id = page.pk
        intent = record_deletion_intent(page)
        manifest = deepcopy(intent.manifest)
        original_execute = DeletePageAction.execute
        with patch.object(
            DeletePageAction,
            "execute",
            autospec=True,
            side_effect=original_execute,
        ) as action:
            completed = execute_safe_page_deletion(page, intent=intent)
        action.assert_called_once()
        self.assertIs(action.call_args.args[0].page, page)
        self.assertEqual(completed.pk, intent.pk)
        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.MYSQL_DELETED)
        self.assertEqual(intent.manifest, manifest)
        self.assertFalse(Page.objects.filter(pk=page_id).exists())
        self.assertTrue(
            ExternalContentPublicationState.objects.filter(page_id=page_id).exists()
        )
        self.assertEqual(
            ExternalContentDeletionIntent.objects.filter(page_id=page_id).count(), 1
        )
        self.assertFalse(
            getattr(page, "_controlled_safe_deletion_in_progress", False)
        )
        self.assertNotIn(
            page_id, getattr(_deletion_context, "deleting_page_ids", set())
        )


class SafeDeletionScopeContractTests(SimpleTestCase):
    """不访问数据库，验证删除窗口的嵌套和异常恢复契约。"""

    def setUp(self) -> None:
        """保留真实线程属性原对象，测试清理不得替换外部集合的身份。"""
        super().setUp()
        self.context_existed = hasattr(_deletion_context, "deleting_page_ids")
        self.original_context = getattr(_deletion_context, "deleting_page_ids", None)
        self.addCleanup(self._restore_original_context)

    def _restore_original_context(self) -> None:
        """恢复进入测试前的属性存在性及原对象，不干预其他线程属性。"""
        if self.context_existed:
            _deletion_context.deleting_page_ids = self.original_context
        elif hasattr(_deletion_context, "deleting_page_ids"):
            delattr(_deletion_context, "deleting_page_ids")

    def test_nested_overlapping_ids_restore_original_objects_and_values(self) -> None:
        """嵌套重叠窗口退出后，外层及原始集合对象和内容必须分别恢复。"""
        original_ids = {-1, 101}
        original_values = original_ids.copy()
        _deletion_context.deleting_page_ids = original_ids
        with safe_deletion_scope({101, 102}):
            outer_ids = _deletion_context.deleting_page_ids
            outer_values = outer_ids.copy()
            self.assertEqual(outer_values, {-1, 101, 102})
            with safe_deletion_scope({102, 103}):
                self.assertEqual(
                    _deletion_context.deleting_page_ids, {-1, 101, 102, 103}
                )
            self.assertIs(_deletion_context.deleting_page_ids, outer_ids)
            self.assertEqual(_deletion_context.deleting_page_ids, outer_values)
        self.assertIs(_deletion_context.deleting_page_ids, original_ids)
        self.assertEqual(_deletion_context.deleting_page_ids, original_values)

    def test_empty_scope_failure_restores_attribute_presence_and_values(self) -> None:
        """空窗口内部新增标记并抛错后，缺席属性及无关原标记均精确恢复。"""
        for with_sentinel in (False, True):
            with self.subTest(with_sentinel=with_sentinel):
                original_ids = {-1}
                if with_sentinel:
                    _deletion_context.deleting_page_ids = original_ids
                elif hasattr(_deletion_context, "deleting_page_ids"):
                    delattr(_deletion_context, "deleting_page_ids")
                context_before = deepcopy(vars(_deletion_context))
                fault = RuntimeError("测试注入：空删除窗口内新增标记后失败")
                with self.assertRaises(RuntimeError) as caught:
                    with safe_deletion_scope(set()):
                        _deletion_context.deleting_page_ids.add(202)
                        self.assertIn(202, _deletion_context.deleting_page_ids)
                        raise fault
                self.assertIs(caught.exception, fault)
                self.assertEqual(
                    hasattr(_deletion_context, "deleting_page_ids"),
                    "deleting_page_ids" in context_before,
                )
                self.assertEqual(vars(_deletion_context), context_before)
                if with_sentinel:
                    self.assertIs(_deletion_context.deleting_page_ids, original_ids)
