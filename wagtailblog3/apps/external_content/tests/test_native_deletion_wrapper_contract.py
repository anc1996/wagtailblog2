"""验证删除包装重复注册及非 Page 树节点的行为，不重复事务故障回滚用例。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.db import connection
from django.db.models.deletion import Collector
from django.db.models.signals import post_save, pre_delete
from django.test import TransactionTestCase
from treebeard.mp_tree import MP_NodeQuerySet
from wagtail.actions.delete_page import DeletePageAction
from wagtail.models import Collection, Locale, Page, PageLogEntry, Revision

from external_content.deletion import _deletion_context, wrap_native_page_deletion
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)
from guides.models import GuideIndexPage, GuidePage, GuideTopic


class NativeDeletionWrapperContractTests(TransactionTestCase):
    """真实自动提交入口观察日志、意图和 Collection 树更新，不借外层 atomic。"""

    def setUp(self) -> None:
        """只隔离线程属性；页面与 Collection 均在需要时使用既有模型创建。"""
        super().setUp()
        self.context_before = vars(_deletion_context).copy()
        self.addCleanup(self._restore_context)
        vars(_deletion_context).clear()
        self.assertTrue(connection.get_autocommit())
        self.assertFalse(connection.in_atomic_block)

    def _restore_context(self) -> None:
        """恢复原属性和对象引用，不影响其他测试留下的合法调用方上下文。"""
        vars(_deletion_context).clear()
        vars(_deletion_context).update(self.context_before)

    def _create_page(self) -> GuidePage:
        """使用真实合法草稿及 Revision，不继承其他测试类或创建动态表。"""
        if not hasattr(self, "parent"):
            locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
            root = Page.get_first_root_node()
            if root is None:
                root = Page.add_root(
                    title="包装契约测试根节点", locale=locale, live=False
                )
            suffix = uuid4().hex[:10]
            self.parent = root.add_child(
                instance=GuideIndexPage(
                    title="包装契约测试索引",
                    slug=f"wrapper-contract-{suffix}",
                    locale=locale,
                    live=False,
                )
            )
            self.topic = GuideTopic.objects.create(
                name="包装契约测试主题", slug=f"wrapper-contract-{suffix}"
            )
        page = self.parent.add_child(
            instance=GuidePage(
                title="包装契约测试指南",
                slug=f"wrapper-guide-{uuid4().hex[:10]}",
                summary="验证原生包装单次执行",
                topic=self.topic,
                locale=self.parent.locale,
                difficulty="intermediate",
                live=False,
                body=[
                    {"type": "lead", "value": "<p>包装契约导读正文</p>"},
                    {
                        "type": "step",
                        "value": {
                            "title": "包装契约测试步骤",
                            "description": "<p>包装契约步骤正文</p>",
                            "image": None,
                        },
                    },
                ],
            )
        )
        page.save_revision()
        page.refresh_from_db()
        return page

    def _register_repeatedly(self, register: Callable[[], None]) -> None:
        """重复注册必须保留已安装入口，且随后仍执行真实组件而非源码匹配。"""
        targets = (
            (DeletePageAction, "execute"),
            (MP_NodeQuerySet, "delete"),
            (Collector, "delete"),
        )
        originals = [getattr(target, name) for target, name in targets]
        for _ in range(3):
            register()
        for (target, name), original in zip(targets, originals):
            self.assertIs(getattr(target, name), original)

    def _assert_single_action(self, register: Callable[[], None]) -> None:
        """真实 Action 只新增一份删除审计、一条意图并推进一次墓碑状态。"""
        page = self._create_page()
        page_id = page.pk
        revision_ids = list(page.revisions.values_list("pk", flat=True))
        parent_count = Page.objects.get(pk=self.parent.pk).numchild
        logs_before = PageLogEntry.objects.count()
        events: list[tuple[int, bool, str]] = []

        def observe_intent(
            sender: Any,
            instance: ExternalContentDeletionIntent,
            created: bool,
            **kwargs: Any,
        ) -> None:
            if instance.page_id == page_id:
                events.append((instance.page_id, created, instance.status))

        self._register_repeatedly(register)
        post_save.connect(observe_intent, sender=ExternalContentDeletionIntent)
        try:
            result = DeletePageAction(page).execute()
        finally:
            post_save.disconnect(observe_intent, sender=ExternalContentDeletionIntent)
        self.assertEqual(result[1]["guides.GuidePage"], 1)
        self.assertEqual(result[1]["wagtailcore.Page"], 1)
        self.assertEqual(result[0], sum(result[1].values()))
        self.assertFalse(Page.objects.filter(pk=page_id).exists())
        self.assertFalse(GuidePage.objects.filter(pk=page_id).exists())
        self.assertFalse(Revision.objects.filter(pk__in=revision_ids).exists())
        self.assertEqual(PageLogEntry.objects.count(), logs_before + 1)
        log = PageLogEntry.objects.get(page_id=page_id, action="wagtail.delete")
        self.assertTrue(log.deleted)
        intent = ExternalContentDeletionIntent.objects.get(page_id=page_id)
        self.assertEqual(intent.status, DeletionIntentStatus.MYSQL_DELETED)
        self.assertTrue(intent.manifest["body_version_ids"])
        self.assertEqual(intent.manifest["revision_ids"], revision_ids)
        self.assertEqual(
            events,
            [
                (page_id, True, DeletionIntentStatus.PENDING),
                (page_id, False, DeletionIntentStatus.MYSQL_DELETED),
            ],
        )
        self.assertEqual(
            Page.objects.get(pk=self.parent.pk).numchild, parent_count - 1
        )
        self.assertEqual(vars(_deletion_context), {})
        self.assertTrue(connection.get_autocommit())
        self.assertFalse(connection.in_atomic_block)

    def test_repeated_registration_runs_action_once(self) -> None:
        """包装注册不得叠层，真实单页删除日志和意图均只写一份。"""
        self._assert_single_action(wrap_native_page_deletion)

    def test_repeated_ready_runs_action_once(self) -> None:
        """重复应用 ready 不叠加包装或信号，仍只有一次真实删除推进。"""
        self._assert_single_action(apps.get_app_config("external_content").ready)

    def test_repeated_registration_creates_one_intent_per_queryset_page(self) -> None:
        """真实批量树删除重复注册后每页仅一条意图，不额外生成 Action 日志。"""
        pages = [self._create_page(), self._create_page()]
        page_ids = [page.pk for page in pages]
        parent_count = Page.objects.get(pk=self.parent.pk).numchild
        logs_before = list(PageLogEntry.objects.order_by("pk").values())
        self._register_repeatedly(wrap_native_page_deletion)

        result = GuidePage.objects.filter(pk__in=page_ids).delete()

        self.assertEqual(result[1]["guides.GuidePage"], 2)
        self.assertEqual(result[1]["wagtailcore.Page"], 2)
        self.assertFalse(Page.objects.filter(pk__in=page_ids).exists())
        for page_id in page_ids:
            intent = ExternalContentDeletionIntent.objects.get(page_id=page_id)
            self.assertEqual(intent.status, DeletionIntentStatus.MYSQL_DELETED)
            self.assertTrue(intent.manifest["body_version_ids"])
        self.assertEqual(
            Page.objects.get(pk=self.parent.pk).numchild, parent_count - 2
        )
        self.assertEqual(list(PageLogEntry.objects.order_by("pk").values()), logs_before)
        self.assertEqual(vars(_deletion_context), {})
        self.assertTrue(connection.get_autocommit())
        self.assertFalse(connection.in_atomic_block)

    def test_non_page_treebeard_delete_keeps_autocommit_and_context(self) -> None:
        """Collection 父计数更新仍为自动提交，不进入 Page 事务或删除授权窗口。"""
        for with_sentinel in (False, True):
            with self.subTest(with_sentinel=with_sentinel):
                if with_sentinel:
                    _deletion_context.deleting_page_ids = {-1}
                elif hasattr(_deletion_context, "deleting_page_ids"):
                    delattr(_deletion_context, "deleting_page_ids")
                context_before = vars(_deletion_context).copy()
                root = Collection.get_first_root_node()
                if root is None:
                    root = Collection.add_root(name="包装契约资源根节点")
                parent = root.add_child(name=f"wrapper-collection-{uuid4().hex[:10]}")
                branch = parent.add_child(name="a-delete")
                leaf = branch.add_child(name="leaf")
                sibling = parent.add_child(name="z-keep")
                removed_ids = [branch.pk, leaf.pk]
                parent.refresh_from_db()
                parent_count = parent.numchild
                before = {
                    model: deepcopy(list(model.objects.order_by("pk").values()))
                    for model in (
                        Page,
                        GuidePage,
                        Revision,
                        PageLogEntry,
                        ExternalContentDeletionIntent,
                        ExternalContentPublicationState,
                    )
                }
                update_atomic_states: list[bool] = []
                signal_ids: list[int] = []

                def observe_sql(
                    execute: Callable[..., Any],
                    sql: str,
                    params: Any,
                    many: bool,
                    context: dict[str, Any],
                ) -> Any:
                    if (
                        sql.lstrip().upper().startswith("UPDATE")
                        and Collection._meta.db_table in sql
                    ):
                        update_atomic_states.append(connection.in_atomic_block)
                    return execute(sql, params, many, context)

                def observe_collection(
                    sender: Any, instance: Collection, **kwargs: Any
                ) -> None:
                    signal_ids.append(instance.pk)
                    self.assertEqual(vars(_deletion_context), context_before)
                    if with_sentinel:
                        self.assertIs(
                            _deletion_context.deleting_page_ids,
                            context_before["deleting_page_ids"],
                        )

                queryset = Collection.objects.filter(pk=branch.pk)
                self.assertIsInstance(queryset, MP_NodeQuerySet)
                self._register_repeatedly(wrap_native_page_deletion)
                pre_delete.connect(observe_collection, sender=Collection)
                try:
                    with connection.execute_wrapper(observe_sql):
                        result = queryset.delete()
                finally:
                    pre_delete.disconnect(observe_collection, sender=Collection)
                self.assertEqual(result, (2, {"wagtailcore.Collection": 2}))
                self.assertEqual(update_atomic_states, [False])
                self.assertCountEqual(signal_ids, removed_ids)
                self.assertFalse(Collection.objects.filter(pk__in=removed_ids).exists())
                self.assertTrue(Collection.objects.filter(pk=sibling.pk).exists())
                parent.refresh_from_db()
                self.assertEqual(parent.numchild, parent_count - 1)
                for model, rows in before.items():
                    self.assertEqual(list(model.objects.order_by("pk").values()), rows)
                self.assertEqual(vars(_deletion_context), context_before)
                if with_sentinel:
                    self.assertIs(
                        _deletion_context.deleting_page_ids,
                        context_before["deleting_page_ids"],
                    )
                self.assertTrue(connection.get_autocommit())
                self.assertFalse(connection.in_atomic_block)

    def test_wrappers_forward_original_arguments_and_return_identity(self) -> None:
        """仅此用受控原方法替身捕获透传；其余用例保留真实删除及信号行为。"""
        action_page = self._create_page()
        collector_page = self._create_page()
        self.assertIsNotNone(action_page.pk)
        self.assertIsNotNone(collector_page.pk)
        self.assertNotEqual(action_page.pk, collector_page.pk)
        external_collector = Collector(using="default")
        external_collector.data = {GuidePage: {collector_page}}
        collection_root = Collection.get_first_root_node()
        if collection_root is None:
            collection_root = Collection.add_root(name="透传测试资源根节点")
        collection = collection_root.add_child(
            name=f"wrapper-forward-{uuid4().hex[:10]}"
        )
        self.assertIsNotNone(collection.pk)
        collection_collector = Collector(using="default")
        collection_collector.data = {Collection: {collection}}
        cases = (
            (DeletePageAction, "execute", DeletePageAction(action_page), True, False),
            (MP_NodeQuerySet, "delete", Page.objects.none(), True, False),
            (MP_NodeQuerySet, "delete", Collection.objects.none(), False, False),
            (Collector, "delete", external_collector, False, True),
            (Collector, "delete", collection_collector, False, False),
        )
        for target, name, instance, expected_atomic, expected_scope in cases:
            with self.subTest(target=target.__name__, model=type(instance).__name__):
                original_ids = {-1}
                _deletion_context.deleting_page_ids = original_ids
                argument = object()
                keyword = object()
                returned = object()
                observations: list[tuple[Any, ...]] = []

                def original_method(
                    received: Any, *args: Any, **kwargs: Any
                ) -> Any:
                    observations.append(
                        (
                            received,
                            args,
                            kwargs,
                            connection.in_atomic_block,
                            _deletion_context.deleting_page_ids,
                        )
                    )
                    return returned

                with patch.object(target, name, original_method):
                    wrap_native_page_deletion()
                    result = getattr(instance, name)(argument, marker=keyword)
                self.assertIs(result, returned)
                self.assertEqual(len(observations), 1)
                received, args, kwargs, atomic_state, scoped_ids = observations[0]
                self.assertIs(received, instance)
                self.assertEqual(args, (argument,))
                self.assertEqual(kwargs, {"marker": keyword})
                self.assertEqual(atomic_state, expected_atomic)
                self.assertEqual(scoped_ids, {-1})
                if expected_scope:
                    self.assertIsNot(scoped_ids, original_ids)
                else:
                    self.assertIs(scoped_ids, original_ids)
                self.assertIs(_deletion_context.deleting_page_ids, original_ids)
                self.assertEqual(original_ids, {-1})
                self.assertFalse(connection.in_atomic_block)
