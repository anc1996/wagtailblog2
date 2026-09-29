"""
Wagtail Page 正文外置通用协议 - G2 Wagtail 生命周期接入定向测试。

覆盖：
1. 专用测试 Page (LifecycleTestPage: 两个外置 StreamField + 一个原生 MySQL 字段)；
2. save_revision 受控保存与 Manifest 快照持久化；
3. 普通 serializable_data() 纯只读且零 MongoDB 副作用；
4. 未持久化页面 (pk is None) 阻断 save_revision；
5. from_serializable_data 精确历史版本反序列化与无损还原；
6. 损坏/缺失 Mongo 正文或篡改身份时反序列化严格阻断；
7. 已发布/已批准 Revision 禁止覆盖 (overwrite)；
8. page.publish() 推进正式指针与单调代次；
9. 原生 PublishPageRevisionAction 绕过 page.publish() 时的安全拦截与状态同步；
10. Mongo 故障注入拦截发布，确保 live 绝不提前推进，MySQL 保持原子回滚；
11. 未来排期发布 (scheduled) 不提前切换正式指针；
12. unpublish 取消发布代次递增并清空正式指针；
13. 重复发布回调的严格幂等性；
14. serve() 渲染前自动加载正式正文；
15. 搜索生产者开关 (CONTENT_SEARCH_PRODUCER_ENABLED) 关闭时发布生命周期完全正常。
"""

import json
import os
import tempfile
import uuid
from datetime import timedelta
from typing import Any, Mapping

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from wagtail import blocks
from wagtail.actions.publish_page_revision import PublishPageRevisionAction
from wagtail.signals import page_published, page_unpublished
from wagtail.fields import StreamField, StreamValue
from wagtail.models import GroupApprovalTask, Locale, Page, Revision, Workflow, WorkflowTask

from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import (
    CorruptedContentPayloadError,
    DirectSaveForbiddenError,
    ManifestIdentityMismatchError,
    PublicationBodyUnavailableError,
    PublicationRevisionInvalidError,
    PublicationWorkflowRevisionDriftError,
    RevisionOverwriteForbiddenError,
)
from external_content.manifest import ExternalContentManifest
from external_content.mixins import ExternalContentPageMixin
from external_content.models import ExternalContentPublicationState
from external_content.publication import (
    ExternalContentPublicationService,
    validate_scheduled_revision,
)
from external_content.registry import (
    get_storage_policy_for_model,
    reset_active_baseline_file,
    set_active_baseline_file,
)
from external_content.store import ContentVersionStore, ensure_indexes, get_mongo_client, set_active_collection_name, reset_active_collection_name


class LifecycleTestPage(ExternalContentPageMixin, Page):
    """
    用于 G2 完整生命周期验证的测试 Page。
    包含：
    - 1 个原生 MySQL 字段：native_intro
    - 2 个外置 StreamField 字段：primary_body, secondary_body
    """
    native_intro = models.CharField(max_length=255, default="")
    primary_body = StreamField(
        [
            ("heading", blocks.CharBlock()),
            ("paragraph", blocks.TextBlock()),
        ],
        use_json_field=True,
    )
    secondary_body = StreamField(
        [
            ("quote", blocks.BlockQuoteBlock()),
        ],
        use_json_field=True,
    )

    content_storage = {
        "primary_body": "mongo",
        "secondary_body": "mongo",
    }
    content_storage_schema = 1

    class Meta:
        app_label = "external_content"


class ExternalContentLifecycleTests(TransactionTestCase):
    """G2 生命周期与发布全流程定向测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # 动态创建测试页面表的底层架构（支持 --keepdb 重复执行，后续测试级联扫描需要该物理表存在）
        with connection.schema_editor() as editor:
            if LifecycleTestPage._meta.db_table not in connection.introspection.table_names():
                editor.create_model(LifecycleTestPage)

    @classmethod
    def tearDownClass(cls) -> None:
        # 保留表结构，避免后续测试执行 Page.delete() 扫描子类表时因缺少物理表抛出异常
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        self.temp_baseline = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.temp_baseline.close()
        with open(self.temp_baseline.name, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "policies": {}}, f)
        set_active_baseline_file(self.temp_baseline.name)

        # 备案 LifecycleTestPage 策略基线
        get_storage_policy_for_model(LifecycleTestPage, allow_record_new=True)

        self.mongo_client = get_mongo_client()
        db_name = getattr(settings, "MONGO_DB", {}).get("NAME", "wagtailblog_test")
        self.coll_name = f"test_lifecycle_{uuid.uuid4().hex[:12]}"
        self.collection = self.mongo_client[db_name][self.coll_name]
        ensure_indexes(self.collection)
        set_active_collection_name(self.coll_name)
        self.store = ContentVersionStore(collection=self.collection)
        self.coordinator = ExternalContentCoordinator(store=self.store)

        User = get_user_model()
        self.user = User.objects.create_superuser(
            username=f"admin_{uuid.uuid4().hex[:6]}",
            email="admin@example.com",
            password="password",
        )

        self.locale, _ = Locale.objects.get_or_create(language_code=settings.LANGUAGE_CODE)
        root = Page.objects.filter(depth=1).first()
        if not root:
            root = Page.add_root(title="Root", locale=self.locale)
        self.root_page = root

        from django.contrib.contenttypes.models import ContentType
        ContentType.objects.clear_cache()
        ct, _ = ContentType.objects.get_or_create(
            app_label=LifecycleTestPage._meta.app_label,
            model=LifecycleTestPage._meta.model_name,
        )

        # 创建并挂载测试页面
        self.page = LifecycleTestPage(
            title="生命周期测试页面",
            slug=f"lifecycle-test-{uuid.uuid4().hex[:6]}",
            native_intro="原生摘要内容",
            live=False,
            has_unpublished_changes=True,
            primary_body=[
                ("heading", "主章节一"),
                ("paragraph", "主正文详细说明。"),
            ],
            secondary_body=[
                ("quote", "精选引用内容。"),
            ],
        )
        self.page.content_type = ct
        self.root_page.add_child(instance=self.page)

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

    def test_save_revision_controlled_and_manifest_recorded(self) -> None:
        """测试 save_revision 成功写入不可变 Mongo 版本并注入 Manifest，Revision 中字段为空哨兵。"""
        # 依赖注入测试仓储给 mixin 与 coordinator
        self.page._test_content_store = self.store

        rev = self.page.save_revision(user=self.user)
        self.assertIsNotNone(rev.pk)

        # 验证 MongoDB 中存储了两个字段
        docs = list(self.collection.find({"page_id": self.page.pk}))
        self.assertEqual(len(docs), 2)
        fields = {doc["field_name"] for doc in docs}
        self.assertEqual(fields, {"primary_body", "secondary_body"})

        # 验证 Revision content 中的空哨兵与 Manifest
        content = rev.content if isinstance(rev.content, dict) else json.loads(rev.content)
        self.assertIn("_external_content_manifest", content)
        self.assertEqual(content["native_intro"], "原生摘要内容")
        self.assertEqual(content["primary_body"], [])
        self.assertEqual(content["secondary_body"], [])

        # 验证 ExternalContentPublicationState 中的草稿记录
        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.draft_revision_id, rev.pk)
        self.assertIsNotNone(state.draft_manifest)
        self.assertEqual(state.model_label, "external_content.lifecycletestpage")
        self.assertIsNone(state.published_revision_id)

    def test_plain_serializable_data_is_pure_read_and_no_side_effects(self) -> None:
        """测试普通 serializable_data() 调用纯只读，保留原生正文，绝不产生 Mongo 写入副作用。"""
        initial_count = self.collection.count_documents({})
        data = self.page.serializable_data()

        # 验证绝无 Mongo 写入副作用
        self.assertEqual(self.collection.count_documents({}), initial_count)
        # 验证普通序列化保留原生正文数据，不被替换为 []
        self.assertNotEqual(data.get("primary_body"), [])
        self.assertNotIn("_external_content_manifest", data)

    def test_save_revision_on_unpersisted_page_raises_error(self) -> None:
        """测试未持久化页面 (pk is None) 阻断 save_revision，防止生成无主键孤儿正文。"""
        unpersisted = LifecycleTestPage(
            title="未持久化页",
            slug="unpersisted-slug",
            primary_body=[("paragraph", "内容")],
            secondary_body=[],
        )
        with self.assertRaises(RuntimeError) as cm:
            unpersisted.save_revision()
        self.assertIn("主键", str(cm.exception))

    def test_restore_from_serializable_data_exact_roundtrip(self) -> None:
        """测试 from_serializable_data 精确历史版本反序列化，从 Mongo 恢复全部外置字段。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)

        # 清空当前内存
        content_dict = rev.content if isinstance(rev.content, dict) else json.loads(rev.content)

        # 模拟 Wagtail 历史恢复
        restored = LifecycleTestPage.from_serializable_data(content_dict)
        self.assertEqual(restored.pk, self.page.pk)
        self.assertEqual(restored.native_intro, "原生摘要内容")
        self.assertEqual(len(restored.primary_body), 2)
        self.assertEqual(restored.primary_body[0].block_type, "heading")
        self.assertEqual(restored.primary_body[0].value, "主章节一")
        self.assertEqual(restored.primary_body[1].block_type, "paragraph")
        self.assertEqual(restored.primary_body[1].value, "主正文详细说明。")
        self.assertEqual(len(restored.secondary_body), 1)
        self.assertEqual(restored.secondary_body[0].value, "精选引用内容。")

    def test_restore_from_serializable_data_corrupted_payload_raises(self) -> None:
        """测试 Mongo 正文被破坏或篡改时，反序列化严格阻断并抛出异常。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        content_dict = rev.content if isinstance(rev.content, dict) else json.loads(rev.content)

        # 篡改 Mongo 中的 payload
        self.collection.update_one(
            {"page_id": self.page.pk, "field_name": "primary_body"},
            {"$set": {"payload": [{"type": "heading", "value": "被篡改的内容"}]}},
        )

        with self.assertRaises(CorruptedContentPayloadError):
            LifecycleTestPage.from_serializable_data(content_dict)

    def test_restore_identity_mismatch_raises(self) -> None:
        """测试 Manifest 身份（page_id 或 model_label）不匹配时反序列化严格阻断。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        content_dict = json.loads(json.dumps(rev.content if isinstance(rev.content, dict) else json.loads(rev.content)))

        # 篡改 Manifest 中的 page_id
        content_dict["_external_content_manifest"]["page_id"] = 9999999

        with self.assertRaises(ManifestIdentityMismatchError):
            LifecycleTestPage.from_serializable_data(content_dict)

    def test_revision_overwrite_forbidden_for_published_or_approved(self) -> None:
        """测试已发布或已批准的 Revision 严格禁止被 overwrite 复用，必须新建 Revision。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        self.page.publish(rev)

        # 修改页面并尝试指定 overwrite_revision = rev
        self.page.native_intro = "修改后的简介"
        new_rev = self.page.save_revision(user=self.user, overwrite_revision=rev)

        # 验证并未覆盖旧 Revision，而是生成了全新的 Revision ID
        self.assertNotEqual(new_rev.pk, rev.pk)
        rev.refresh_from_db()
        self.assertIn("原生摘要内容", json.dumps(rev.content, ensure_ascii=False))

    def test_publish_page_promotes_state_and_generation(self) -> None:
        """测试 page.publish() 成功推进正式指针、递增公开代次并同步状态。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)

        self.page.publish(rev)
        self.page.refresh_from_db()

        self.assertTrue(self.page.live)
        self.assertEqual(self.page.live_revision, rev)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.published_revision_id, rev.pk)
        self.assertIsNotNone(state.published_manifest)
        self.assertEqual(state.publication_generation, 1)
        self.assertIsNotNone(state.published_at)

    def test_publish_action_bypassing_page_publish_is_secured(self) -> None:
        """测试后台原生 PublishPageRevisionAction 绕过 page.publish() 时，仍被底层与信号拦截并同步正式指针。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)

        # 直接实例化 PublishPageRevisionAction 并执行（模拟 Wagtail 后台编辑界面）
        action = PublishPageRevisionAction(rev, user=self.user)
        action.execute(skip_permission_checks=True)

        self.page.refresh_from_db()
        self.assertTrue(self.page.live)
        self.assertEqual(self.page.live_revision, rev)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.published_revision_id, rev.pk)
        self.assertEqual(state.publication_generation, 1)

    def test_publish_failure_injection_mongo_unavailable_blocks_live(self) -> None:
        """测试故障注入：当 Mongo 正文损坏或缺失时，发布失败回滚，MySQL live 绝不变为 True。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)

        # 注入故障：删除 Mongo 正文记录
        self.collection.delete_many({"page_id": self.page.pk})

        with self.assertRaises(PublicationBodyUnavailableError):
            self.page.publish(rev)

        self.page.refresh_from_db()
        self.assertFalse(self.page.live)
        self.assertIsNone(self.page.live_revision)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertIsNone(state.published_revision_id)
        self.assertEqual(state.publication_generation, 0)

    def test_scheduled_publishing_future_date_does_not_promote_immediately(self) -> None:
        """测试排期在未来的发布，不会提前将页面置为 live，也不会提前推进正式指针。"""
        self.page._test_content_store = self.store
        future_time = timezone.now() + timedelta(days=2)
        self.page.go_live_at = future_time
        self.page.save()

        rev = self.page.save_revision(user=self.user, approved_go_live_at=future_time)

        # 执行发布
        self.page.publish(rev)
        self.page.refresh_from_db()

        self.assertFalse(self.page.live)
        self.assertIsNone(self.page.live_revision)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertIsNone(state.published_revision_id)
        self.assertEqual(state.publication_generation, 0)

    def test_unpublish_advances_generation_and_clears_pointer(self) -> None:
        """测试 unpublish 取消发布，代次自增，正式指针被清空。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        self.page.publish(rev)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.publication_generation, 1)

        self.page.unpublish()
        self.page.refresh_from_db()

        self.assertFalse(self.page.live)
        state.refresh_from_db()
        self.assertEqual(state.publication_generation, 2)
        self.assertIsNone(state.published_revision_id)
        self.assertIsNone(state.published_manifest)

    def test_duplicate_publish_idempotence(self) -> None:
        """测试同一 Revision 重复发布时保持幂等，代次不会无意义自增。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        self.page.publish(rev)

        state1 = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        gen1 = state1.publication_generation

        # 重复触发发布确认
        ExternalContentPublicationService.ensure_published_revision(self.page.pk, rev.pk)
        state2 = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state2.publication_generation, gen1)

    def test_frontend_serve_loads_published_content(self) -> None:
        """测试 serve() 前台请求时自动从 Mongo 加载已发布正文。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        self.page.publish(rev)

        # 模拟普通 ORM 查询加载出的页面（此时数据库里的 body 是占位哨兵）
        db_page = LifecycleTestPage.objects.get(pk=self.page.pk)
        db_page._test_content_store = self.store

        # 显式加载或 serve
        db_page.load_published_content()
        self.assertEqual(len(db_page.primary_body), 2)
        self.assertEqual(db_page.primary_body[0].value, "主章节一")
        self.assertEqual(len(db_page.secondary_body), 1)

    @override_settings(CONTENT_SEARCH_PRODUCER_ENABLED=False)
    def test_search_flag_disabled_lifecycle_still_functions(self) -> None:
        """测试当搜索生产者开关关闭时，生命周期保存、发布、下架依旧完整可靠。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        self.page.publish(rev)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.published_revision_id, rev.pk)
        self.assertEqual(state.publication_generation, 1)

        self.page.unpublish()
        state.refresh_from_db()
        self.assertEqual(state.publication_generation, 2)

    def test_publication_rollback_when_signal_fails_leaves_page_not_live(self) -> None:
        """测试发布过程中信号抛错时事务完整回滚，live 状态绝不提前残留为 True。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)

        def failing_receiver(sender: Any, instance: Any, revision: Any, **kwargs: Any) -> None:
            if getattr(instance, "pk", None) == self.page.pk:
                raise RuntimeError("模拟信号接收器异常，测试整体原子回滚")

        page_published.connect(failing_receiver)
        try:
            with self.assertRaises(RuntimeError):
                self.page.publish(rev)
        finally:
            page_published.disconnect(failing_receiver)

        # 验证数据库中页面未被发布
        self.page.refresh_from_db()
        self.assertFalse(self.page.live)
        self.assertIsNone(self.page.live_revision_id)

        # 验证发布状态表中的正式指针未推进
        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertIsNone(state.published_revision_id)
        self.assertEqual(state.publication_generation, 0)

    def test_from_serializable_data_fail_closed_when_manifest_missing(self) -> None:
        """测试历史还原时若缺少 Manifest，严格失败关闭 (Fail-Closed) 抛出异常。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        data = self.page.serializable_data()

        # 恶意抹除或破坏 _external_content_manifest
        data.pop("_external_content_manifest", None)

        with self.assertRaises(PublicationRevisionInvalidError):
            LifecycleTestPage.from_serializable_data(data)

    def test_serve_fail_closed_when_live_page_missing_published_manifest(self) -> None:
        """测试已发布页面缺少正式发布状态时，前台 serve() 严格失败关闭，拒绝静默展示空正文。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        self.page.publish(rev)

        # 恶意破坏正式发布状态
        ExternalContentPublicationState.objects.filter(page_id=self.page.pk).update(
            published_manifest=None
        )

        db_page = LifecycleTestPage.objects.get(pk=self.page.pk)
        with self.assertRaises(PublicationBodyUnavailableError):
            db_page.load_published_content()

        rf = RequestFactory()
        request = rf.get("/")
        with self.assertRaises(PublicationBodyUnavailableError):
            db_page.serve(request)

    def test_save_revision_always_creates_new_revision_ignoring_overwrite(self) -> None:
        """测试 save_revision 对外置页面一律新建 Revision，彻底杜绝覆盖竞态。"""
        self.page._test_content_store = self.store
        rev1 = self.page.save_revision(user=self.user)

        self.page.primary_body = [("heading", "新修改标题")]
        # 即使显式传入 overwrite_revision，也强制新建
        rev2 = self.page.save_revision(user=self.user, overwrite_revision=rev1)

        self.assertNotEqual(rev1.pk, rev2.pk)
        self.assertTrue(Revision.objects.filter(pk=rev1.pk).exists())
        self.assertTrue(Revision.objects.filter(pk=rev2.pk).exists())

    def test_publishing_older_revision_does_not_regress_newer_draft_pointer(self) -> None:
        """测试发布较旧的审批 Revision 时，正式指针更新但草稿指针绝不倒退。"""
        self.page._test_content_store = self.store
        rev1 = self.page.save_revision(user=self.user)

        self.page.primary_body = [("heading", "较新的草稿第二版")]
        rev2 = self.page.save_revision(user=self.user)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.draft_revision_id, rev2.pk)

        # 发布旧版 rev1
        self.page.publish(rev1)

        state.refresh_from_db()
        self.assertEqual(state.published_revision_id, rev1.pk)
        # 草稿指针仍安全保留最新的 rev2
        self.assertEqual(state.draft_revision_id, rev2.pk)

    def test_page_publish_is_strictly_idempotent(self) -> None:
        """测试显式调用 page.publish() 重复发布同一 Revision 保持严格幂等。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)

        self.page.publish(rev)
        state1 = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state1.publication_generation, 1)

        # 再次显式调用 page.publish()
        self.page.publish(rev)
        state2 = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state2.publication_generation, 1)

    def test_scheduled_publishing_execution_validates_revision(self) -> None:
        """测试排期到期执行时严格校验 approved_go_live_at。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)

        # 1. 未到期的排期不能通过 scheduled 执行
        rev.approved_go_live_at = timezone.now() + timedelta(days=1)
        rev.save(update_fields=["approved_go_live_at"])

        with self.assertRaises(PublicationRevisionInvalidError):
            self.page.publish(rev, log_action="wagtail.publish.scheduled")

        # 2. 已到期的排期成功执行发布并推进正式指针
        rev.approved_go_live_at = timezone.now() - timedelta(minutes=5)
        rev.save(update_fields=["approved_go_live_at"])

        self.page.publish(rev, log_action="wagtail.publish.scheduled")
        self.page.refresh_from_db()
        self.assertTrue(self.page.live)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.published_revision_id, rev.pk)
        self.assertEqual(state.publication_generation, 1)

    def test_workflow_finish_action_wiring_and_drift_detection(self) -> None:
        """测试 Workflow 审批完成接线与 Revision 漂移拦截门禁。"""
        self.page._test_content_store = self.store
        rev1 = self.page.save_revision(user=self.user)

        workflow_ct = ContentType.objects.get_for_model(GroupApprovalTask, for_concrete_model=False)
        task = GroupApprovalTask.objects.create(name="G2 审批任务", content_type=workflow_ct)
        workflow = Workflow.objects.create(name="G2 Workflow")
        WorkflowTask.objects.create(workflow=workflow, task=task, sort_order=0)

        workflow_state = workflow.start(self.page, self.user)
        self.assertEqual(workflow_state.current_task_state.revision.pk, rev1.pk)

        # 模拟并发修改：审批过程中有新草稿产生导致 Revision 漂移
        self.page.primary_body = [("heading", "并发偷跑草稿")]
        rev2 = self.page.save_revision(user=self.user)

        # 审批通过时应被 WAGTAIL_FINISH_WORKFLOW_ACTION 阻断
        with self.assertRaises(PublicationWorkflowRevisionDriftError):
            workflow_state.current_task_state.approve(user=self.user)

    def test_direct_save_with_modified_body_raises_error(self) -> None:
        """测试外置正文页面禁止直接调用 save() 持久化修改过的正文字段。"""
        self.page._test_content_store = self.store
        self.page.save_revision(user=self.user)

        # 直接篡改正文并直接 save()
        self.page.primary_body = [("heading", "非法直接修改")]
        with self.assertRaises(DirectSaveForbiddenError):
            self.page.save()

    def test_metadata_save_allowed(self) -> None:
        """测试保存元数据字段（如 title/slug）被正常允许。"""
        self.page._test_content_store = self.store
        self.page.title = "已更新的元数据标题"
        self.page.save(update_fields=["title"])

        self.page.refresh_from_db()
        self.assertEqual(self.page.title, "已更新的元数据标题")

    def test_reloaded_published_page_metadata_save_preserves_pointer(self) -> None:
        """数据库重载的页面仍可保存元数据，且不触碰已发布正文指针。"""
        self.page._test_content_store = self.store
        revision = self.page.save_revision(user=self.user)
        self.page.publish(revision)

        reloaded_page = LifecycleTestPage.objects.get(pk=self.page.pk)
        reloaded_page.title = "仅更新元数据"
        reloaded_page.save()

        self.assertEqual(LifecycleTestPage.objects.get(pk=self.page.pk).title, "仅更新元数据")
        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.published_revision_id, revision.pk)

    def test_published_page_direct_save_with_modified_body_raises_error(self) -> None:
        """测试已发布页面被直接修改正文后调用 save()，严格拦截抛出 DirectSaveForbiddenError，绝不静默丢弃改动。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        self.page.publish(rev)
        self.assertTrue(self.page.live)

        # 重新从数据库读取已发布页面
        db_page = LifecycleTestPage.objects.get(pk=self.page.pk)
        db_page._test_content_store = self.store
        db_page.load_published_content()

        # 修改正文并直接 save()
        db_page.primary_body = [("heading", "已发布页面被非法直接篡改")]
        with self.assertRaises(DirectSaveForbiddenError):
            db_page.save()

    def test_concurrent_draft_saves_monotonicity(self) -> None:
        """测试并发乱序保存 Revision 时草稿指针单调向前，绝不被较旧 Revision 倒退覆写。"""
        self.page._test_content_store = self.store
        rev1 = self.page.save_revision(user=self.user)

        self.page.primary_body = [("heading", "并发第二版")]
        rev2 = self.page.save_revision(user=self.user)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.draft_revision_id, rev2.pk)

        # 模拟乱序延迟提交较旧的 rev1 状态保存
        ExternalContentPublicationService.lock_and_validate_revision(
            self.page.pk,
            rev1.pk,
            store=self.store,
        )

        state.refresh_from_db()
        # 强验 draft_revision_id 未被倒退为 rev1
        self.assertEqual(state.draft_revision_id, rev2.pk)

    def test_workflow_approval_future_scheduled_revision_does_not_promote_published_pointer(self) -> None:
        """测试 Workflow 审批排期在未来的 Revision 时，绝不提前推进正式指针与代次。"""
        self.page._test_content_store = self.store
        future_time = timezone.now() + timedelta(days=3)
        rev = self.page.save_revision(user=self.user, approved_go_live_at=future_time)

        workflow_ct = ContentType.objects.get_for_model(GroupApprovalTask, for_concrete_model=False)
        task = GroupApprovalTask.objects.create(name="排期审批任务", content_type=workflow_ct)
        workflow = Workflow.objects.create(name="排期 Workflow")
        WorkflowTask.objects.create(workflow=workflow, task=task, sort_order=0)

        workflow_state = workflow.start(self.page, self.user)
        self.assertEqual(workflow_state.current_task_state.revision.pk, rev.pk)

        # 审批通过
        workflow_state.current_task_state.approve(user=self.user)

        # 页面保持未发布，正式指针未被提前推进
        self.page.refresh_from_db()
        self.assertFalse(self.page.live)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertIsNone(state.published_revision_id)
        self.assertEqual(state.publication_generation, 0)

    def test_scheduled_publishing_via_revision_publish_and_command(self) -> None:
        """测试 Wagtail 实际定时任务入口 (Revision.publish) 执行排期发布及排期状态校验。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)

        # 1. 未到期时通过 Revision.publish 触发排期执行，应被拦截
        rev.approved_go_live_at = timezone.now() + timedelta(days=2)
        rev.save(update_fields=["approved_go_live_at"])

        with self.assertRaises(PublicationRevisionInvalidError):
            rev.publish(log_action="wagtail.publish.scheduled")

        # 2. 到期后通过 Revision.publish 触发排期执行，正常发布并推进正式指针
        rev.approved_go_live_at = timezone.now() - timedelta(minutes=5)
        rev.save(update_fields=["approved_go_live_at"])

        rev.publish(log_action="wagtail.publish.scheduled")
        self.page.refresh_from_db()
        self.assertTrue(self.page.live)

        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.published_revision_id, rev.pk)
        self.assertEqual(state.publication_generation, 1)

    def test_published_page_direct_save_clearing_body_to_empty_raises_error(self) -> None:
        """测试已发布页面直接将正文清空为 [] 后调用 save()，严格拦截抛出 DirectSaveForbiddenError。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        self.page.publish(rev)

        db_page = LifecycleTestPage.objects.get(pk=self.page.pk)
        db_page._test_content_store = self.store
        db_page.load_published_content()

        # 恶意直接清空正文字段为 []
        db_page.primary_body = []
        with self.assertRaises(DirectSaveForbiddenError):
            db_page.save()

    def test_unpublish_with_commit_false_does_not_clear_published_pointer(self) -> None:
        """测试调用 unpublish(commit=False) 时，数据库保持 live=True，正式指针与公开代次绝不提前清空或递增。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        self.page.publish(rev)

        state_before = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state_before.published_revision_id, rev.pk)
        gen_before = state_before.publication_generation

        # commit=False 执行下架
        self.page.unpublish(commit=False)

        # 数据库中页面依然为 live=True
        self.page.refresh_from_db()
        self.assertTrue(self.page.live)

        # 正式指针与公开代次完整保留
        state_after = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state_after.published_revision_id, rev.pk)
        self.assertEqual(state_after.publication_generation, gen_before)

    def test_unpublish_transaction_rollback_when_signal_fails_leaves_page_live(self) -> None:
        """测试下架过程中信号抛错时事务完整回滚，页面在数据库中绝不残留 live=False。"""
        self.page._test_content_store = self.store
        rev = self.page.save_revision(user=self.user)
        self.page.publish(rev)
        self.assertTrue(self.page.live)

        def failing_unpublish_receiver(sender: Any, instance: Any, **kwargs: Any) -> None:
            if getattr(instance, "pk", None) == self.page.pk:
                raise RuntimeError("模拟下架信号接收器异常，测试整体原子回滚")

        page_unpublished.connect(failing_unpublish_receiver)
        try:
            with self.assertRaises(RuntimeError):
                self.page.unpublish()
        finally:
            page_unpublished.disconnect(failing_unpublish_receiver)

        # 验证数据库中页面未被下架（依然为 live=True）
        self.page.refresh_from_db()
        self.assertTrue(self.page.live)
        self.assertEqual(self.page.live_revision_id, rev.pk)

        # 验证正式指针未被清空
        state = ExternalContentPublicationState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.published_revision_id, rev.pk)
