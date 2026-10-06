# -*- coding: utf-8 -*-
"""
Wagtail Page 正文外置通用协议 - G4 生命周期协调器与全局单调锁序纯离线单元测试。

本测试套件完全遵循离线原则（0 真实网络、0 真实数据库连接），
针对 Sol 架构复审提出的 5 大阻断项与全部硬契约进行细粒度、高覆盖率的静态桩断言验证：
1. PublishRevisionAction / PublishPageRevisionAction：
   - 对受控外置页面对象缺失、主键未持久化、策略为 None、策略缺失 external_fields 或模型不匹配时的 Fail-Closed 阻断；
   - 待发布 Revision 未在锁集合命中、或跨 Page Revision (object_id/base_content_type 不匹配) 时的严格阻断；
   - 非受控页面的安全透明透传。
2. UnpublishAction / UnpublishPageAction：
   - 对受控外置页面对象缺失、主键未持久化、策略为 None、策略缺失 external_fields 时的 Fail-Closed 阻断；
   - 锁序先行与受控下架服务调用；
   - 非受控页面的安全透明透传。
3. wrap_workflow_state_finish 与并发锁序：
   - WorkflowState.finish() 在修改自身状态前，必须先获取目标 Page 的 Level 1~4 全级生命周期锁；
   - 消除 WorkflowState 与 Publish 动作之间的跨模型逆序死锁环。
4. mixins.py 关键生命周期契约：
   - copy()：事务首行持有全级排他锁后方可装载正文快照；正文损坏异常严格向上传播（绝不吞异常）；
   - create_alias()：事务内单次调用 super().create_alias；来源围栏和初始化由原生 Action 包装器负责；
   - refresh_from_db()：live=True 页面正文装载失败严格向上传播。
5. apps.py 启动自检门禁：
   - 5 大核心类（PublishRevisionAction, PublishPageRevisionAction, UnpublishAction, UnpublishPageAction, WorkflowState）
     必须全部成功包装并打上 _external_content_atomic_wrapped 标记，缺失任一类立即抛出 RuntimeError。
6. coordinator.py 身份校验契约：
   - Alias 必须通过数据库来源链、正式 State/Revision 和 Manifest 校验；受控 Copy 保留原有契约；
   - 普通场景严格通过 validate_against_page 校验 page_id 与 model_label。
"""

import os
import sys
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import django

# 初始化 Django 离线配置
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "wagtailblog3.settings.dev"
django.setup()

from django.db import transaction
from external_content.exceptions import (
    ExternalContentOperationBlockedError,
    ManifestIdentityMismatchError,
    PublicationRevisionInvalidError,
)
from external_content.locking import PageLifecycleLocks
from external_content.publication import (
    _wrap_single_publish_action,
    _wrap_single_unpublish_action,
    is_external_content_target,
    wrap_workflow_state_finish,
)


class DummyExternalPage:
    """模拟受控外置正文页面类。"""
    content_storage = {"body": {"codec": "markdown"}}

    def __init__(self, pk=101, live=True):
        self.pk = pk
        self.id = pk
        self.live = live
        self.specific = self
        self._meta = SimpleNamespace(label_lower="guides.guidepage", model_name="guidepage")

    def get_base_content_type(self):
        return SimpleNamespace(id=50)


class DummyStandardPage:
    """模拟非外置正文普通页面类。"""
    def __init__(self, pk=202):
        self.pk = pk
        self.id = pk
        self.specific = self
        self._meta = SimpleNamespace(label_lower="wagtailcore.page", model_name="page")


class TestActionsPublishFailClosed(unittest.TestCase):
    """1. 发布动作包装器的 Fail-Closed 阻断与锁序契约。"""

    def setUp(self):
        class MockPublishAction:
            def __init__(self, obj=None, revision=None):
                self.object = obj
                self.revision = revision
                self.executed = False

            def execute(self, skip_permission_checks=False):
                self.executed = True
                return "ORIGINAL_PUBLISH_RESULT"

        self.action_cls = MockPublishAction
        _wrap_single_publish_action(self.action_cls)

    def test_target_missing_object_raises_blocked_error(self):
        """当受控外置页面 target 确定（由 revision.content_object 反查判定）但 object 为 None 时，严格阻断。"""
        target_page = DummyExternalPage(pk=101)
        rev = SimpleNamespace(pk=1, content_object=target_page, base_content_type_id=50, object_id="101")
        action = self.action_cls(obj=None, revision=rev)

        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action.execute()
        self.assertIn("受控外置页面对象缺失或尚未持久化主键", str(cm.exception))
        self.assertFalse(action.executed)

    def test_target_unpersisted_pk_raises_blocked_error(self):
        """当受控页面对象未持久化主键 (pk=None) 时，严格阻断。"""
        target_page = DummyExternalPage(pk=None)
        rev = SimpleNamespace(pk=1, content_object=target_page)
        action = self.action_cls(obj=target_page, revision=rev)

        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action.execute()
        self.assertIn("尚未持久化主键", str(cm.exception))
        self.assertFalse(action.executed)

    def test_target_missing_revision_or_pk_raises_blocked_error(self):
        """当待发布 Revision 缺失或尚未持久化主键时，严格阻断。"""
        target_page = DummyExternalPage(pk=101)
        action_no_rev = self.action_cls(obj=target_page, revision=None)
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action_no_rev.execute()
        self.assertIn("待发布 Revision 缺失或尚未持久化主键", str(cm.exception))

        action_rev_no_pk = self.action_cls(obj=target_page, revision=SimpleNamespace(pk=None))
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action_rev_no_pk.execute()
        self.assertIn("待发布 Revision 缺失或尚未持久化主键", str(cm.exception))

    @patch("external_content.registry.get_storage_policy_for_model")
    def test_policy_resolution_exception_fail_closed(self, mock_get_policy):
        """当存储策略解析异常时，严格向外抛出 ExternalContentOperationBlockedError，绝不 Fail-Open。"""
        mock_get_policy.side_effect = RuntimeError("策略注册表损坏")
        target_page = DummyExternalPage(pk=101)
        rev = SimpleNamespace(pk=1)
        action = self.action_cls(obj=target_page, revision=rev)

        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action.execute()
        self.assertIn("解析对象", str(cm.exception))
        self.assertIn("策略注册表损坏", str(cm.exception))

    @patch("external_content.registry.get_storage_policy_for_model")
    def test_policy_none_or_missing_fields_fail_closed(self, mock_get_policy):
        """策略返回 None、缺少 external_fields 或模型类型不匹配时，严格阻断。"""
        target_page = DummyExternalPage(pk=101)
        rev = SimpleNamespace(pk=1)
        action = self.action_cls(obj=target_page, revision=rev)

        # 场景 A: 策略为 None
        mock_get_policy.return_value = None
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action.execute()
        self.assertIn("存储策略无效、缺失外置字段或模型不匹配", str(cm.exception))

        # 场景 B: 策略缺少 external_fields
        mock_get_policy.return_value = SimpleNamespace(external_fields={}, model_class=DummyExternalPage)
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action.execute()
        self.assertIn("存储策略无效、缺失外置字段或模型不匹配", str(cm.exception))

        # 场景 C: 模型类型不匹配
        mock_get_policy.return_value = SimpleNamespace(
            external_fields={"body": "mock"},
            model_class=DummyStandardPage
        )
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action.execute()
        self.assertIn("存储策略无效、缺失外置字段或模型不匹配", str(cm.exception))

    @patch("external_content.publication.lock_page_lifecycle_hierarchy")
    @patch("external_content.registry.get_storage_policy_for_model")
    def test_revision_not_in_locked_revisions_fail_closed(self, mock_get_policy, mock_lock):
        """待发布 Revision 未在锁集合命中时，严格阻断，禁止发布跨页面或不存在的 Revision。"""
        target_page = DummyExternalPage(pk=101)
        rev = SimpleNamespace(pk=99)
        mock_get_policy.return_value = SimpleNamespace(
            external_fields={"body": "mock"},
            model_class=DummyExternalPage
        )
        # 模拟返回的锁集合中不包含 Revision 99
        mock_lock.return_value = PageLifecycleLocks(
            page=target_page,
            state=None,
            revisions=[],
            intents=[],
        )
        action = self.action_cls(obj=target_page, revision=rev)

        with self.assertRaises(PublicationRevisionInvalidError) as cm:
            action.execute()
        self.assertIn("未在目标页面 (ID: 101) 的锁集合中命中", str(cm.exception))

    @patch("external_content.publication.lock_page_lifecycle_hierarchy")
    @patch("external_content.registry.get_storage_policy_for_model")
    def test_revision_content_type_or_object_id_mismatch_fail_closed(self, mock_get_policy, mock_lock):
        """锁定 Revision 的 content_type 或 object_id 与目标页面不匹配时，严格阻断。"""
        target_page = DummyExternalPage(pk=101)
        mock_get_policy.return_value = SimpleNamespace(
            external_fields={"body": "mock"},
            model_class=DummyExternalPage
        )

        # 场景 A: content_type_id 不匹配
        rev_locked = SimpleNamespace(pk=1, base_content_type_id=999, object_id="101")
        mock_lock.return_value = PageLifecycleLocks(
            page=target_page,
            state=None,
            revisions=[rev_locked],
            intents=[],
        )
        action = self.action_cls(obj=target_page, revision=SimpleNamespace(pk=1))
        with self.assertRaises(PublicationRevisionInvalidError) as cm:
            action.execute()
        self.assertIn("不匹配", str(cm.exception))

        # 场景 B: object_id 不匹配
        rev_locked_b = SimpleNamespace(pk=1, base_content_type_id=50, object_id="888")
        mock_lock.return_value = PageLifecycleLocks(
            page=target_page,
            state=None,
            revisions=[rev_locked_b],
            intents=[],
        )
        with self.assertRaises(PublicationRevisionInvalidError) as cm:
            action.execute()
        self.assertIn("不匹配", str(cm.exception))

    def test_non_target_page_passthrough_without_locks(self):
        """非受控外置普通页面正常透传执行，不触发外置锁与策略校验。"""
        normal_page = DummyStandardPage(pk=202)
        rev = SimpleNamespace(pk=1, content_object=normal_page)
        action = self.action_cls(obj=normal_page, revision=rev)

        with patch("external_content.publication.lock_page_lifecycle_hierarchy") as mock_lock:
            result = action.execute()
            self.assertEqual(result, "ORIGINAL_PUBLISH_RESULT")
            self.assertTrue(action.executed)
            mock_lock.assert_not_called()


class TestActionsUnpublishFailClosed(unittest.TestCase):
    """2. 下架动作包装器的 Fail-Closed 阻断与锁序契约。"""

    def setUp(self):
        class MockUnpublishAction:
            def __init__(self, obj=None):
                self.object = obj
                self.executed = False

            def execute(self, skip_permission_checks=False):
                self.executed = True
                return "ORIGINAL_UNPUBLISH_RESULT"

        self.action_cls = MockUnpublishAction
        _wrap_single_unpublish_action(self.action_cls)

    def test_unpublish_target_unpersisted_pk_raises_blocked_error(self):
        """受控页面对象未持久化主键时，严格阻断下架。"""
        target_page = DummyExternalPage(pk=None)
        action = self.action_cls(obj=target_page)

        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action.execute()
        self.assertIn("受控外置页面对象缺失或尚未持久化主键", str(cm.exception))
        self.assertFalse(action.executed)

    @patch("external_content.registry.get_storage_policy_for_model")
    def test_unpublish_policy_none_or_missing_fields_fail_closed(self, mock_get_policy):
        """下架时策略缺失或无效，严格阻断。"""
        target_page = DummyExternalPage(pk=101)
        action = self.action_cls(obj=target_page)

        # 策略为 None
        mock_get_policy.return_value = None
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action.execute()
        self.assertIn("存储策略无效、缺失外置字段或模型不匹配", str(cm.exception))

        # 策略缺少 external_fields
        mock_get_policy.return_value = SimpleNamespace(external_fields={}, model_class=DummyExternalPage)
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action.execute()
        self.assertIn("存储策略无效、缺失外置字段或模型不匹配", str(cm.exception))

    @patch("external_content.publication.ExternalContentPublicationService.execute_controlled_unpublish")
    @patch("external_content.registry.get_storage_policy_for_model")
    def test_unpublish_controlled_service_invoked_with_lock_protection(self, mock_get_policy, mock_unpublish_svc):
        """受控页面下架受 ExternalContentPublicationService 保护执行。"""
        target_page = DummyExternalPage(pk=101)
        mock_get_policy.return_value = SimpleNamespace(
            external_fields={"body": "mock"},
            model_class=DummyExternalPage
        )
        mock_unpublish_svc.return_value = "CONTROLLED_UNPUBLISHED"

        action = self.action_cls(obj=target_page)
        res = action.execute()

        self.assertEqual(res, "CONTROLLED_UNPUBLISHED")
        mock_unpublish_svc.assert_called_once()
        self.assertEqual(mock_unpublish_svc.call_args.kwargs["page_id"], 101)

    def test_unpublish_non_target_passthrough(self):
        """非受控页面直接透传执行原生下架。"""
        normal_page = DummyStandardPage(pk=202)
        action = self.action_cls(obj=normal_page)

        res = action.execute()
        self.assertEqual(res, "ORIGINAL_UNPUBLISH_RESULT")
        self.assertTrue(action.executed)


class TestWorkflowStateFinishOrdering(unittest.TestCase):
    """3. WorkflowState.finish() 锁序与消除逆序死锁环契约。"""

    def setUp(self):
        class MockWorkflowState:
            def __init__(self, content_obj=None, object_id=None):
                self.content_object = content_obj
                self.object_id = object_id
                self.finished = False

            def finish(self, user=None):
                self.finished = True
                return "WORKFLOW_FINISHED"

        self.wf_cls = MockWorkflowState

    def test_workflow_finish_acquires_page_lifecycle_locks_before_original_finish(self):
        """受控页面在执行原生 finish() 自身状态变更前，必须先获取目标 Page 的 Level 1~4 全级生命周期锁。"""
        call_order = []

        def mock_lock_fn(page_id, revision_ids=None, check_write=True):
            call_order.append(("lock_page_lifecycle_hierarchy", page_id))
            return PageLifecycleLocks(page=None, state=None, revisions=[], intents=[])

        target_page = DummyExternalPage(pk=101)
        wf_state = self.wf_cls(content_obj=target_page, object_id="101")

        with patch("wagtail.models.WorkflowState", self.wf_cls), \
             patch("external_content.publication.lock_page_lifecycle_hierarchy", side_effect=mock_lock_fn):
            wrap_workflow_state_finish()
            wf_state.finish(user=None)

        self.assertEqual(len(call_order), 1)
        self.assertEqual(call_order[0], ("lock_page_lifecycle_hierarchy", 101))
        self.assertTrue(wf_state.finished)

    def test_workflow_concurrency_ordering_consistency(self):
        """验证并发下 Workflow finish 与 Publish 动作获取同一页面的锁序完全单调一致。"""
        locks_acquired = []

        def fake_lock(page_id, revision_ids=None, check_write=True):
            locks_acquired.append(("LOCK_PAGE", page_id))
            return PageLifecycleLocks(page=None, state=None, revisions=[], intents=[])

        target_page = DummyExternalPage(pk=101)
        with patch("wagtail.models.WorkflowState", self.wf_cls), \
             patch("external_content.publication.lock_page_lifecycle_hierarchy", side_effect=fake_lock):
            wrap_workflow_state_finish()
            wf = self.wf_cls(content_obj=target_page)
            wf.finish()

        self.assertIn(("LOCK_PAGE", 101), locks_acquired)


class TestMixinLifecycleMethods(unittest.TestCase):
    """4. mixins.py copy(), create_alias() 与 refresh_from_db() 硬契约。"""

    def test_copy_locks_before_content_hydration_and_propagates_corrupt_content_error(self):
        """copy() 必须在持有全级锁之后方可装载正文快照；正文损坏时严格向上传播异常，绝不静默吞掉。"""
        from external_content.mixins import ExternalContentPageMixin

        events = []

        class DummyParentPage:
            pk = 101
            live = True

            def copy(self, *args, **kwargs):
                events.append("super_copy")
                return DummyParentPage()

        class MockControlledPage(ExternalContentPageMixin, DummyParentPage):
            def load_published_content(self):
                events.append("load_published_content")
                raise RuntimeError("正文在不可变存储中损坏或丢失")

        page = MockControlledPage()

        def fake_lock(page_id, revision_ids=None, check_write=True):
            events.append(("lock_page_lifecycle_hierarchy", page_id))
            return PageLifecycleLocks(page=None, state=None, revisions=[], intents=[])

        with patch("external_content.mixins.lock_page_lifecycle_hierarchy", side_effect=fake_lock):
            with self.assertRaises(RuntimeError) as cm:
                page.copy(copy_revisions=False, keep_live=True)
            self.assertIn("正文在不可变存储中损坏或丢失", str(cm.exception))

        # 锁必须排在 load_published_content 之前！
        self.assertEqual(events[0], ("lock_page_lifecycle_hierarchy", 101))
        self.assertEqual(events[1], "load_published_content")
        self.assertNotIn("super_copy", events)

    def test_create_alias_single_call_and_returns_page_instance(self):
        """Mixin 仅提供事务及单次透传，不抢先阻塞加锁或重复执行 Action 的初始化。"""
        from external_content.mixins import ExternalContentPageMixin, _controlled_alias_context

        events = []
        super_alias_called_count = 0
        kwargs = {"update_slug": "new-alias", "recursive": True}
        previous_context = vars(_controlled_alias_context).copy()

        alias_instance = SimpleNamespace(
            pk=102,
            id=102,
            alias_of_id=101,
            _external_content_alias_processed=False
        )

        class DummyParentPage:
            pk = 101

            def create_alias(self, *args, **kwargs):
                nonlocal super_alias_called_count
                super_alias_called_count += 1
                events.append(("super_create_alias", args, kwargs))
                return alias_instance

        class MockControlledPage(ExternalContentPageMixin, DummyParentPage):
            pass

        page = MockControlledPage()

        @contextmanager
        def fake_atomic():
            events.append("atomic_enter")
            try:
                yield
            finally:
                events.append("atomic_exit")

        with patch("external_content.mixins.transaction.atomic", side_effect=fake_atomic), \
             patch(
                 "external_content.mixins.lock_page_lifecycle_hierarchy",
                 side_effect=AssertionError("Mixin 不得在 Alias NOWAIT 围栏前请求阻塞锁"),
             ), \
             patch("external_content.copying.setup_external_content_for_alias") as mock_setup:
            result = page.create_alias("parent", **kwargs)

            self.assertEqual(super_alias_called_count, 1)
            self.assertIs(result, alias_instance)
            self.assertEqual(
                events,
                ["atomic_enter", ("super_create_alias", ("parent",), kwargs), "atomic_exit"],
            )
            mock_setup.assert_not_called()
            self.assertFalse(alias_instance._external_content_alias_processed)
            self.assertEqual(vars(_controlled_alias_context), previous_context)

    def test_create_alias_action_fence_failure_prevents_native_creation(self):
        """围栏移入 Action 后，锁冲突仍须在原生建页之前传播并退出事务。"""
        from external_content.copying import wrap_create_page_alias_action
        from external_content.mixins import _controlled_alias_context

        events = []
        fault = ExternalContentOperationBlockedError("测试 Alias 围栏冲突")
        previous_context = vars(_controlled_alias_context).copy()

        @contextmanager
        def fake_atomic():
            events.append("atomic_enter")
            try:
                yield
            finally:
                events.append("atomic_exit")

        class DummyAliasAction:
            def execute(self, *args, **kwargs):
                return self._create_alias(DummyExternalPage(pk=101))

            def _create_alias(self, page, **kwargs):
                events.append("native_create_alias")
                return SimpleNamespace(pk=102)

        def blocked_fence(page_id):
            events.append(("alias_fence", page_id))
            raise fault

        with patch("wagtail.actions.create_alias.CreatePageAliasAction", DummyAliasAction), \
             patch("external_content.copying.transaction.atomic", side_effect=fake_atomic), \
             patch("external_content.copying.lock_alias_lifecycle_closure", side_effect=blocked_fence):
            wrap_create_page_alias_action()
            with self.assertRaises(ExternalContentOperationBlockedError) as caught:
                DummyAliasAction().execute()

        self.assertIs(caught.exception, fault)
        self.assertEqual(
            events,
            ["atomic_enter", "atomic_enter", ("alias_fence", 101), "atomic_exit", "atomic_exit"],
        )
        self.assertEqual(vars(_controlled_alias_context), previous_context)

    def test_refresh_from_db_propagates_corrupt_content_exception(self):
        """refresh_from_db() 在 live=True 时，正文装载异常必须直接向上传播。"""
        from external_content.mixins import ExternalContentPageMixin

        class DummyParentPage:
            live = True
            def refresh_from_db(self, *args, **kwargs):
                pass

        class MockControlledPage(ExternalContentPageMixin, DummyParentPage):
            def load_published_content(self):
                raise RuntimeError("不可变存储连接中断")

        page = MockControlledPage()
        with self.assertRaises(RuntimeError) as cm:
            page.refresh_from_db()
        self.assertIn("不可变存储连接中断", str(cm.exception))


class TestAppsStartupGuards(unittest.TestCase):
    """5. apps.py 启动自检门禁硬断言。"""

    def test_apps_ready_guards_all_five_classes_fail_closed(self):
        """当 5 大核心类中任一类未被包装时，ready() 必须立即抛出 RuntimeError，严禁 Fail-Open。"""
        from external_content.apps import ExternalContentConfig

        app_config = ExternalContentConfig("external_content", sys.modules[ExternalContentConfig.__module__])

        # 模拟 Wagtail 动作类
        dummy_classes = []
        for name in ["PublishRevisionAction", "PublishPageRevisionAction", "UnpublishAction", "UnpublishPageAction", "WorkflowState"]:
            cls = type(name, (), {"_external_content_atomic_wrapped": False})
            dummy_classes.append(cls)

        with patch("external_content.publication.wrap_publish_page_revision_action"), \
             patch("external_content.publication.wrap_unpublish_page_action"), \
             patch("external_content.publication.wrap_workflow_state_finish"), \
             patch("external_content.copying.wrap_copy_page_action"), \
             patch("external_content.copying.wrap_create_page_alias_action"), \
             patch("external_content.copying.wrap_convert_alias_page_action"), \
             patch("wagtail.actions.publish_revision.PublishRevisionAction", dummy_classes[0]), \
             patch("wagtail.actions.publish_page_revision.PublishPageRevisionAction", dummy_classes[1]), \
             patch("wagtail.actions.unpublish.UnpublishAction", dummy_classes[2]), \
             patch("wagtail.actions.unpublish_page.UnpublishPageAction", dummy_classes[3]), \
             patch("wagtail.models.WorkflowState", dummy_classes[4]):

            # 未打标记，抛出 RuntimeError
            with self.assertRaises(RuntimeError) as cm:
                app_config.ready()
            self.assertIn("未被 external_content 成功包装，Fail-Closed", str(cm.exception))

            # 标记为 True 后通过
            for cls in dummy_classes:
                cls._external_content_atomic_wrapped = True
            app_config.ready()


class TestCoordinatorManifestIdentity(unittest.TestCase):
    """6. coordinator.py 身份校验与 Alias/Copy 上下文契约。"""

    @contextmanager
    def _alias_source_fixture(self):
        """仅替代数据库与正文 IO，真实执行来源链、State、Revision 和 Manifest 门禁。"""
        from external_content.coordinator import ExternalContentCoordinator
        from external_content.locking import preserve_threadlocal_context
        from external_content.manifest import ExternalContentManifest, FieldManifestEntry
        from external_content.mixins import _controlled_copy_context
        from wagtail.models import Page

        class RootPage(DummyExternalPage):
            _meta = SimpleNamespace(label_lower="guides.guidepage")

        root = RootPage(pk=100)
        root.alias_of_id = None
        root.content_type_id = 60
        root.specific_class = RootPage
        root.specific_deferred = root
        root.live_revision_id = 5
        root.get_base_content_type = lambda: SimpleNamespace(pk=50, id=50)
        alias_page = DummyExternalPage(pk=200)
        alias_page.alias_of_id = 100
        alias_page.content_type_id = 60
        alias_page._meta.get_field = lambda field_name: field_name
        alias_page.body = "original_body"
        manifest = ExternalContentManifest(
            protocol_version=1,
            content_storage_schema=1,
            model_label="guides.guidepage",
            page_id=100,
            fields={
                "body": FieldManifestEntry(
                    codec="markdown", schema_version=1,
                    version_id="ver_001", payload_sha256="a" * 64,
                ),
            },
        )
        revision = SimpleNamespace(
            pk=5, content_type_id=60, base_content_type_id=50, object_id="100",
            content_type=SimpleNamespace(app_label="guides", model="guidepage"),
            content={"id": 100, "_external_content_manifest": manifest.to_dict()},
        )
        root_state = SimpleNamespace(
            page_id=100, model_label="guides.guidepage",
            source_page_id=None, source_revision_id=None,
            published_revision_id=5, published_manifest=manifest.to_dict(),
            publication_generation=1,
        )
        alias_state = SimpleNamespace(
            page_id=200, model_label="guides.guidepage",
            source_page_id=100, source_revision_id=5,
            published_revision_id=None, draft_revision_id=None, approved_revision_id=None,
            draft_manifest=None, approved_manifest=None,
            published_manifest=manifest.to_dict(), publication_generation=1,
        )
        policy = SimpleNamespace(
            schema_version=1,
            external_fields={
                "body": SimpleNamespace(
                    codec=SimpleNamespace(
                        decode=lambda payload, field: payload,
                        codec_id="markdown", schema_version=1,
                    ),
                ),
            },
        )
        store = MagicMock()
        store.get_version.return_value = {"payload": "decoded_body"}
        root_revisions = MagicMock()
        root_revisions.order_by.return_value = [revision]
        alias_revisions = MagicMock()
        alias_revisions.exists.return_value = False
        pages = {100: root, 200: alias_page}

        def get_page(*, pk):
            if pk not in pages:
                raise Page.DoesNotExist
            return pages[pk]

        def for_instance(page):
            if page is root:
                return root_revisions
            self.assertIs(page, alias_page)
            return alias_revisions

        with ExitStack() as stack:
            stack.enter_context(preserve_threadlocal_context(_controlled_copy_context, "in_copy", False))
            stack.enter_context(patch("external_content.locking.Page.objects.get", side_effect=get_page))
            stack.enter_context(patch(
                "external_content.copying.ExternalContentPublicationState.objects.filter",
                return_value=[root_state, alias_state],
            ))
            stack.enter_context(patch("external_content.copying.Revision.objects.for_instance", side_effect=for_instance))
            stack.enter_context(patch("external_content.copying.get_storage_policy_for_model", return_value=policy))
            stack.enter_context(patch("external_content.copying.get_policy_for_version", return_value=policy))
            yield SimpleNamespace(
                page=alias_page, root=root, root_state=root_state, alias_state=alias_state,
                revision=revision, manifest=manifest, policy=policy, store=store,
                coordinator=ExternalContentCoordinator(store=store),
                alias_revisions=alias_revisions, pages=pages,
            )

    def test_coordinator_alias_allows_source_page_id_with_matching_model_label(self):
        """匹配模型还不够；完整数据库来源及正式快照证明后才加载源正文。"""
        with self._alias_source_fixture() as fixture:
            fixture.coordinator.load_external_content_from_manifest(
                fixture.page, fixture.manifest, policy=fixture.policy,
            )
            self.assertEqual(fixture.page.body, "decoded_body")
            fixture.store.get_version.assert_called_once_with(
                "ver_001",
                expected_identity={
                    "model_label": "guides.guidepage",
                    "page_id": 100,
                    "field_name": "body",
                    "codec": "markdown",
                    "schema_version": 1,
                    "payload_sha256": "a" * 64,
                },
            )

    def test_coordinator_alias_requires_persisted_source_chain(self):
        """只在内存设置 alias_of_id，不能冒充数据库 Alias 获得跨页读取资格。"""
        from external_content.coordinator import ExternalContentCoordinator
        from external_content.locking import preserve_threadlocal_context
        from external_content.manifest import ExternalContentManifest, FieldManifestEntry
        from external_content.mixins import _controlled_copy_context
        from wagtail.models import Page

        alias_page = DummyExternalPage(pk=200)
        alias_page.alias_of_id = 100
        alias_page.body = "original_body"
        manifest = ExternalContentManifest(
            protocol_version=1,
            content_storage_schema=1,
            model_label="guides.guidepage",
            page_id=100,
            fields={
                "body": FieldManifestEntry(
                    codec="markdown", schema_version=1,
                    version_id="ver_001", payload_sha256="a" * 64,
                ),
            },
        )
        store = MagicMock()
        with preserve_threadlocal_context(_controlled_copy_context, "in_copy", False), patch(
            "external_content.locking.Page.objects.get",
            side_effect=Page.DoesNotExist,
        ), self.assertRaises(ExternalContentOperationBlockedError):
            ExternalContentCoordinator(store=store).load_external_content_from_manifest(alias_page, manifest)
        self.assertEqual(alias_page.body, "original_body")
        store.get_version.assert_not_called()

    def test_coordinator_alias_rejects_invalid_state_revision_and_manifest(self):
        """任何正式来源证明缺口都须在读取正文或修改实例前失败关闭。"""
        from external_content.manifest import ExternalContentManifest, FieldManifestEntry

        for invalid_part in ("source_page", "source_revision", "local_revision", "root_revision", "manifest"):
            with self.subTest(invalid_part=invalid_part), self._alias_source_fixture() as fixture:
                manifest = fixture.manifest
                if invalid_part == "source_page":
                    fixture.alias_state.source_page_id = 999
                elif invalid_part == "source_revision":
                    fixture.alias_state.source_revision_id = 999
                elif invalid_part == "local_revision":
                    fixture.alias_revisions.exists.return_value = True
                elif invalid_part == "root_revision":
                    fixture.revision.object_id = "999"
                else:
                    manifest = ExternalContentManifest(
                        protocol_version=1, content_storage_schema=1,
                        model_label="guides.guidepage", page_id=100,
                        fields={
                            "body": FieldManifestEntry(
                                codec="markdown", schema_version=1,
                                version_id="foreign_version", payload_sha256="a" * 64,
                            ),
                        },
                    )
                with self.assertRaises(PublicationRevisionInvalidError):
                    fixture.coordinator.load_external_content_from_manifest(
                        fixture.page, manifest, policy=fixture.policy,
                    )
                self.assertEqual(fixture.page.body, "original_body")
                fixture.store.get_version.assert_not_called()

    def test_coordinator_alias_mismatched_model_label_raises_error(self):
        """在 Alias 场景下，若 model_label 不一致，抛出 ManifestIdentityMismatchError。"""
        from external_content.coordinator import ExternalContentCoordinator
        from external_content.manifest import ExternalContentManifest, FieldManifestEntry

        alias_page = DummyExternalPage(pk=200)
        alias_page.alias_of_id = 100

        entry = FieldManifestEntry(
            codec="markdown",
            schema_version=1,
            version_id="ver_001",
            payload_sha256="a" * 64,
        )
        manifest = ExternalContentManifest(
            protocol_version=1,
            content_storage_schema=1,
            model_label="blog.blogpage",  # 不一致的 model_label
            page_id=100,
            fields={"body": entry},
        )

        coordinator = ExternalContentCoordinator(store=MagicMock())
        with self.assertRaises(ManifestIdentityMismatchError) as cm:
            coordinator.load_external_content_from_manifest(alias_page, manifest)
        self.assertIn("不一致", str(cm.exception))

    def test_coordinator_normal_page_mismatched_page_id_raises_error(self):
        """非 Alias 非 Copy 的普通页面，若 manifest.page_id 与 page.pk 不一致，抛出 ManifestIdentityMismatchError。"""
        from external_content.coordinator import ExternalContentCoordinator
        from external_content.manifest import ExternalContentManifest, FieldManifestEntry

        normal_page = DummyExternalPage(pk=200)
        normal_page.alias_of_id = None
        normal_page.body = "original_body"
        normal_page.in_alias = True
        normal_page.in_copy = True
        normal_page._external_content_copy_processed = True

        entry = FieldManifestEntry(
            codec="markdown",
            schema_version=1,
            version_id="ver_001",
            payload_sha256="a" * 64,
        )
        manifest = ExternalContentManifest(
            protocol_version=1,
            content_storage_schema=1,
            model_label="guides.guidepage",
            page_id=999,  # 不一致的 page_id
            fields={"body": entry},
        )

        from external_content.locking import preserve_threadlocal_context
        from external_content.mixins import _controlled_alias_context, _controlled_copy_context

        store = MagicMock()
        coordinator = ExternalContentCoordinator(store=store)
        with preserve_threadlocal_context(_controlled_alias_context, "in_alias", True), \
             preserve_threadlocal_context(_controlled_copy_context, "in_copy", False), \
             patch(
                 "external_content.copying.validate_alias_manifest_source",
                 side_effect=AssertionError("普通跨页不得尝试解析 Alias 来源"),
             ), \
             self.assertRaises(ManifestIdentityMismatchError):
            coordinator.load_external_content_from_manifest(normal_page, manifest)
        self.assertEqual(normal_page.body, "original_body")
        store.get_version.assert_not_called()


if __name__ == "__main__":
    unittest.main()
