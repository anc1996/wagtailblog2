"""
专题指南正文外置受控生命周期集成定向测试。

验证：
1. GuidePage 存草稿：写入不可变 Mongo 版本，MySQL Revision 保留安全空哨兵与 Manifest；
2. GuidePage 发布：原子推进 ExternalContentPublicationState 正式指针与代次；
3. 后台原生 PublishPageRevisionAction 绕过 page.publish() 时，仍被信号拦截并推进正式指针；
4. GuidePage 下架：清空正式指针并推进未发布代次；
5. 前台请求：client.get() 自动从 Mongo 加载正式正文并渲染；
6. 多版本编辑与历史不可变版本隔离。
"""

import json
from unittest import mock
from typing import Any, Dict, List
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from django.conf import settings

TEST_STORAGES = {
    **settings.STORAGES,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}
from wagtail.actions.publish_page_revision import PublishPageRevisionAction
from wagtail.actions.copy_page import CopyPageAction, CopyPagePermissionError
from wagtail.actions.create_alias import CreatePageAliasAction
from wagtail.actions.delete_page import DeletePageAction
from wagtail.models import Page, Revision, Site

import uuid
from external_content.exceptions import (
    PublicationRevisionInvalidError,
    ExternalContentOperationBlockedError,
)
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory
from guides.wagtail_hooks import (
    intercept_external_page_copy,
    intercept_external_page_delete,
    intercept_external_page_bulk_delete,
)
from external_content.models import ExternalContentPublicationState
from external_content.publication import ExternalContentPublicationService
from external_content.store import (
    ContentVersionStore,
    ensure_indexes,
    get_mongo_client,
    reset_active_collection_name,
    set_active_collection_name,
)
from guides.models import GuideIndexPage, GuidePage, GuideTopic

User = get_user_model()


@override_settings(STORAGES=TEST_STORAGES)
class GuideLifecycleTestCase(TestCase):
    """GuidePage 生命周期与双存储集成测试。"""

    def setUp(self) -> None:
        self.user = User.objects.create_superuser(
            username="guidelifecycleadmin",
            password="testpassword",
            email="lifecycleadmin@test.com",
        )
        self.client = Client()
        self.coll_name = f"test_guides_lifecycle_{uuid.uuid4().hex[:8]}"
        self.mongo_client = get_mongo_client()
        db_name = getattr(settings, "MONGO_DB_NAME", "wagtailblog_test")
        self.collection = self.mongo_client[db_name][self.coll_name]
        ensure_indexes(self.collection)
        set_active_collection_name(self.coll_name)
        self.store = ContentVersionStore(collection=self.collection)

        from wagtail.models import Locale
        self.locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
        root = Page.objects.filter(depth=1).first()
        if not root:
            root = Page.add_root(title="Root", locale=self.locale)
        self.root_page = root

        self.home = Page.objects.filter(depth=2).first()
        if not self.home:
            self.home = Page(title="Home", slug="home", locale=self.locale)
            self.root_page.add_child(instance=self.home)

        self.site = Site.objects.filter(is_default_site=True).first()
        if not self.site:
            self.site = Site.objects.create(
                hostname="localhost",
                port=80,
                site_name="Test Site",
                root_page=self.home,
                is_default_site=True,
            )

        self.topic = GuideTopic.objects.create(
            name="云原生运维",
            slug="cloud-native",
            description="K8s 与容器化部署",
        )

        self.index_page = GuideIndexPage(
            title="技术指南",
            slug="tech-guides",
            intro="专题指南集合",
        )
        self.home.add_child(instance=self.index_page)
        self.index_page.save_revision().publish()

    def tearDown(self) -> None:
        reset_active_collection_name()
        try:
            self.collection.drop()
        except Exception:
            pass

    def test_save_draft_creates_mongo_version_and_mysql_manifest(self) -> None:
        """测试保存草稿：写 Mongo 不可变版本，MySQL 仅存哨兵 [] 与 Manifest。"""
        body_payload = [
            {
                "type": "lead",
                "value": "<p>容器网络核心配置导读</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "配置 CNI 插件",
                    "description": "<p>选择 Cilium 作为底层网络驱动。</p>",
                    "image": None,
                },
            },
        ]
        guide = GuidePage(
            title="Kubernetes CNI 指南",
            slug="k8s-cni-guide",
            summary="CNI 网络配置指南摘要",
            topic=self.topic,
            difficulty="advanced",
            body=body_payload,
            live=False,
            has_unpublished_changes=True,
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store

        rev = guide.save_revision(user=self.user)
        self.assertIsNotNone(rev.pk)

        # 检查 MySQL 中 Revision content
        content_dict = json.loads(rev.content) if isinstance(rev.content, str) else rev.content
        self.assertIn("_external_content_manifest", content_dict)
        manifest = content_dict["_external_content_manifest"]
        self.assertEqual(manifest["model_label"], "guides.guidepage")
        self.assertIn("body", manifest["fields"])
        field_meta = manifest["fields"]["body"]
        self.assertEqual(field_meta["codec"], "streamfield.v1")

        # 检查 body 在 MySQL 中已被替换为空哨兵 []
        body_raw = content_dict.get("body")
        body_sentinel = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
        self.assertEqual(body_sentinel, [])

        # 检查 Mongo 中存在真实 payload 并通过 6 项身份严格交叉重算核验
        version_id = field_meta["version_id"]
        expected_identity = {
            "model_label": "guides.guidepage",
            "page_id": guide.pk,
            "field_name": "body",
            "codec": field_meta["codec"],
            "schema_version": field_meta["schema_version"],
            "payload_sha256": field_meta["payload_sha256"],
        }
        stored_doc = self.store.get_version(version_id, expected_identity=expected_identity)
        self.assertIsNotNone(stored_doc)
        self.assertEqual(stored_doc["page_id"], guide.pk)
        self.assertEqual(stored_doc["field_name"], "body")
        self.assertEqual(len(stored_doc["payload"]), 2)

        # 草稿状态验证
        guide.refresh_from_db()
        self.assertFalse(guide.live)
        self.assertTrue(guide.has_unpublished_changes)

    def test_publish_and_unpublish_cycle(self) -> None:
        """测试完整发布与下架周期，验证正式指针推进与清空。"""
        guide = GuidePage(
            title="Docker 入门指南",
            slug="docker-beginner-guide",
            summary="容器技术初学者指南",
            topic=self.topic,
            difficulty="beginner",
            body=[
                {
                    "type": "step",
                    "value": {
                        "title": "编写 Dockerfile",
                        "description": "<p>使用多阶段构建精简镜像尺寸。</p>",
                        "image": None,
                    },
                }
            ],
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store

        # 1. 保存并发布
        rev = guide.save_revision(user=self.user)
        guide.publish(rev, user=self.user)
        self.assertTrue(guide.live)

        state = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        self.assertEqual(state.published_revision_id, rev.pk)
        self.assertEqual(state.publication_generation, 1)
        self.assertIsNotNone(state.published_manifest)

        # 2. 下架
        guide.unpublish(commit=True)
        guide.refresh_from_db()
        self.assertFalse(guide.live)

        state.refresh_from_db()
        self.assertIsNone(state.published_revision_id)
        self.assertEqual(state.publication_generation, 2)
        self.assertIn(state.published_manifest, ({}, None))

    def test_publish_action_compatibility(self) -> None:
        """测试后台 PublishPageRevisionAction 直接触发时，信号层保障正式指针一致。"""
        guide = GuidePage(
            title="Service Mesh 实践",
            slug="service-mesh-practice",
            summary="Istio 落地实践",
            topic=self.topic,
            difficulty="advanced",
            body=[
                {
                    "type": "step",
                    "value": {
                        "title": "启用 Sidecar 自动注入",
                        "description": "<p>标记命名空间 istio-injection=enabled</p>",
                        "image": None,
                    },
                }
            ],
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store

        rev = guide.save_revision(user=self.user)

        # 模拟后台编辑界面直接调用原生 PublishPageRevisionAction
        action = PublishPageRevisionAction(rev, user=self.user)
        action.execute(skip_permission_checks=True)

        guide.refresh_from_db()
        self.assertTrue(guide.live)

        state = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        self.assertEqual(state.published_revision_id, rev.pk)
        self.assertEqual(state.publication_generation, 1)

    def test_serve_renders_content_from_mongo(self) -> None:
        """测试前台访问页面时，正式正文自动从 MongoDB 加载并在 HTML 中成功输出。"""
        step_title = "第一步：安装 kubectl 命令行工具"
        step_desc = "从官方渠道下载稳定版本并赋予执行权限。"
        guide = GuidePage(
            title="Kubernetes 客户端配置",
            slug="k8s-kubectl-config",
            summary="配置集群操作客户端",
            topic=self.topic,
            difficulty="intermediate",
            body=[
                {
                    "type": "step",
                    "value": {
                        "title": step_title,
                        "description": f"<p>{step_desc}</p>",
                        "image": None,
                    },
                }
            ],
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store
        rev = guide.save_revision(user=self.user)
        guide.publish(rev, user=self.user)

        response = self.client.get(guide.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, guide.title)
        self.assertContains(response, step_title)
        self.assertContains(response, step_desc)
    def test_publish_action_blocked_when_missing_topic(self) -> None:
        """测试后台原生 PublishPageRevisionAction 直接发布缺少主题的 Revision 时被严格阻断。"""
        guide = GuidePage(
            title="无主题指南",
            slug="no-topic-guide",
            summary="缺少 topic 外键的异常指南",
            topic=None,
            difficulty="beginner",
            body=[
                {
                    "type": "step",
                    "value": {
                        "title": "有效步骤",
                        "description": "<p>说明</p>",
                        "image": None,
                    },
                }
            ],
            live=False,
            has_unpublished_changes=True,
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store
        rev = guide.save_revision(user=self.user)

        action = PublishPageRevisionAction(rev, user=self.user)
        with self.assertRaises(PublicationRevisionInvalidError):
            action.execute(skip_permission_checks=True)

        guide.refresh_from_db()
        self.assertFalse(guide.live)
        state = ExternalContentPublicationState.objects.filter(page_id=guide.pk).first()
        self.assertTrue(state is None or state.published_revision_id is None)

    def test_publish_action_blocked_when_missing_step(self) -> None:
        """测试后台原生 PublishPageRevisionAction 直接发布缺少 step 块的 Revision 时被严格阻断。"""
        guide = GuidePage(
            title="无步骤指南",
            slug="no-step-guide",
            summary="仅有导读而无操作步骤的未完成指南",
            topic=self.topic,
            difficulty="beginner",
            body=[
                {
                    "type": "lead",
                    "value": "<p>仅有导读，缺少步骤</p>",
                }
            ],
            live=False,
            has_unpublished_changes=True,
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store
        rev = guide.save_revision(user=self.user)

        action = PublishPageRevisionAction(rev, user=self.user)
        with self.assertRaises(PublicationRevisionInvalidError):
            action.execute(skip_permission_checks=True)

        guide.refresh_from_db()
        self.assertFalse(guide.live)
        state = ExternalContentPublicationState.objects.filter(page_id=guide.pk).first()
        self.assertTrue(state is None or state.published_revision_id is None)

    def test_lock_and_validate_revision_blocked_when_topic_does_not_exist(self) -> None:
        """
        测试持久化非法 Revision 时，lock_and_validate_revision 严格拦截并抛出 PublicationRevisionInvalidError，
        同时核验正式发布指针绝不推进。
        """
        guide = GuidePage(
            title="持久化非法主题指南",
            slug="persisted-invalid-topic-guide",
            summary="测试发布服务对持久化非法主题 Revision 的拦截",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
            live=False,
            has_unpublished_changes=True,
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store
        rev = guide.save_revision(user=self.user)

        # 在 MySQL 数据库中持久化更新该 Revision 的 content，设置 topic_id 为不存在的 999999
        c_dict = dict(rev.content)
        c_dict["topic_id"] = 999999
        c_dict["topic"] = 999999
        Revision.objects.filter(pk=rev.pk).update(content=c_dict)

        # 直接调用发布核心协调服务 lock_and_validate_revision，验证被准入门禁严格拦截
        with self.assertRaises(PublicationRevisionInvalidError) as cm:
            ExternalContentPublicationService.lock_and_validate_revision(
                page_id=guide.pk,
                revision_id=rev.pk,
                store=self.store,
            )

        self.assertIn("不存在或已失效", str(cm.exception))
        self.assertEqual(cm.exception.details.get("topic_id"), 999999)

        # 核验 State 正式指针绝对未推进
        guide.refresh_from_db()
        self.assertFalse(guide.live)
        state = ExternalContentPublicationState.objects.filter(page_id=guide.pk).first()
        self.assertTrue(state is None or state.published_revision_id is None)

    def test_publish_action_failure_boundary_on_unresolvable_topic(self) -> None:
        """
        测试后台原生 PublishPageRevisionAction 面对外键缺失的 Revision 时的实际失败边界：
        Wagtail 反序列化阶段因 PROTECT 级联无法解析而率先熔断，杜绝非法状态进入发布流程。
        """
        guide = GuidePage(
            title="原生Action边界测试指南",
            slug="action-boundary-guide",
            summary="测试原生 Action 面对非法外键时的实际熔断边界",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
            live=False,
            has_unpublished_changes=True,
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store
        rev = guide.save_revision(user=self.user)

        # 在 MySQL 数据库中持久化更新该 Revision 的 content
        c_dict = dict(rev.content)
        c_dict["topic_id"] = 999999
        c_dict["topic"] = 999999
        Revision.objects.filter(pk=rev.pk).update(content=c_dict)
        rev.refresh_from_db()

        # 验证原生 Action 在构造反序列化阶段由于 PROTECT 约束无法解析而安全拦截
        with self.assertRaises(Exception) as cm:
            action = PublishPageRevisionAction(rev, user=self.user)
            action.execute(skip_permission_checks=True)

        self.assertIn("can't currently handle on_delete types other than CASCADE", str(cm.exception))

        guide.refresh_from_db()
        self.assertFalse(guide.live)
        state = ExternalContentPublicationState.objects.filter(page_id=guide.pk).first()
        self.assertTrue(state is None or state.published_revision_id is None)

    def _create_blocker_test_guide(self) -> GuidePage:
        guide = GuidePage(
            title="测试操作阻断指南",
            slug=f"blocker-guide-{uuid.uuid4().hex[:6]}",
            summary="测试 G4 前置操作阻断",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S1", "description": "<p>D1</p>", "image": None}}],
            live=False,
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store
        guide.save_revision(user=self.user)
        return guide

    def test_copy_blocked_before_g4(self) -> None:
        """
        验证 G4 就绪前，直接调用 GuidePage.copy() 被强行阻断。
        """
        guide = self._create_blocker_test_guide()
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            guide.copy()
        self.assertIn("G4", str(cm.exception))

    def test_create_alias_blocked_before_g4(self) -> None:
        """
        验证 G4 就绪前，直接调用 GuidePage.create_alias() 被强行阻断。
        """
        guide = self._create_blocker_test_guide()
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            guide.create_alias()
        self.assertIn("G4", str(cm.exception))

    def test_delete_blocked_before_g4(self) -> None:
        """
        验证 G4 就绪前，直接调用 GuidePage.delete() 被强行阻断以防孤儿数据。
        """
        guide = self._create_blocker_test_guide()
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            guide.delete()
        self.assertIn("G4", str(cm.exception))

    def test_wagtail_admin_hooks_block_copy_and_delete(self) -> None:
        """
        验证 Wagtail 后台钩子在视图层对 GuidePage 复制与删除请求的重定向与拦截。
        """
        guide = self._create_blocker_test_guide()
        rf = RequestFactory()
        request = rf.get("/admin/pages/")
        from django.contrib.messages.storage.fallback import FallbackStorage
        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))

        # 1. 验证复制钩子拦截
        resp_copy = intercept_external_page_copy(request, guide)
        self.assertIsNotNone(resp_copy)
        self.assertEqual(resp_copy.status_code, 302)

        # 2. 验证删除钩子拦截
        resp_delete = intercept_external_page_delete(request, guide)
        self.assertIsNotNone(resp_delete)
        self.assertEqual(resp_delete.status_code, 302)

        # 3. 验证批量删除拦截
        with self.assertRaises(PermissionDenied):
            intercept_external_page_bulk_delete(
                request=request,
                action_type="delete",
                objects=[guide],
                action=None,
            )

    def test_delete_action_blocked_by_pre_delete_signal(self) -> None:
        """
        验证原生 DeletePageAction 执行时，被底层 pre_delete 信号强行拦截并熔断。
        """
        guide = self._create_blocker_test_guide()
        from django.db import transaction
        with transaction.atomic():
            with self.assertRaises(ExternalContentOperationBlockedError) as cm:
                action = DeletePageAction(guide, user=self.user)
                action.execute(skip_permission_checks=True)
        self.assertIn("G4", str(cm.exception))
        # 验证数据库中页面依然健在
        self.assertTrue(GuidePage.objects.filter(pk=guide.pk).exists())

    def test_parent_delete_blocked_when_contains_guide_descendant(self) -> None:
        """
        验证对包含指南后代子页的父页面执行删除时，级联阶段被 pre_delete 信号熔断，
        事务回滚，父页面与指南子页面均完好保留。
        """
        parent_page = Page(title="测试父目录", slug=f"parent-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=parent_page)

        child_guide = GuidePage(
            title="子指南",
            slug=f"child-guide-{uuid.uuid4().hex[:6]}",
            summary="测试父级删除阻断",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
            live=False,
        )
        parent_page.add_child(instance=child_guide)
        child_guide._test_content_store = self.store
        child_guide.save_revision(user=self.user)

        from django.db import transaction
        with transaction.atomic():
            with self.assertRaises(ExternalContentOperationBlockedError) as cm:
                parent_page.delete()
        self.assertIn("G4", str(cm.exception))

        # 验证父页面与子指南未被级联物理删除
        self.assertTrue(Page.objects.filter(pk=parent_page.pk).exists())
        self.assertTrue(GuidePage.objects.filter(pk=child_guide.pk).exists())

    def test_create_alias_action_blocked_before_g4(self) -> None:
        """
        验证原生 CreatePageAliasAction 在创建别名时被模型层保存防线熔断。
        """
        guide = self._create_blocker_test_guide()
        with self.assertRaises(ExternalContentOperationBlockedError) as cm:
            action = CreatePageAliasAction(guide, parent=self.home, user=self.user)
            action.execute(skip_permission_checks=True)
        self.assertIn("G4", str(cm.exception))

    def test_copy_page_action_blocked_before_g4(self) -> None:
        """
        验证原生 CopyPageAction 在执行复制时被权限防线或模型方法拦截。
        """
        guide = self._create_blocker_test_guide()
        action = CopyPageAction(guide, to=self.home, user=self.user)
        # 验证普通用户或管理员在未跳过权限时触发 CopyPagePermissionError
        with self.assertRaises(CopyPagePermissionError):
            action.execute(skip_permission_checks=False)

    def test_wagtail_admin_hooks_block_parent_with_guide_descendants(self) -> None:
        """
        验证后台钩子对包含指南后代子页的父页面安全拦截复制与删除请求。
        """
        parent_page = Page(title="测试含指南父目录", slug=f"parent-tree-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=parent_page)

        child_guide = GuidePage(
            title="子指南2",
            slug=f"child-guide-2-{uuid.uuid4().hex[:6]}",
            summary="测试父级视图钩子阻断",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
            live=False,
        )
        parent_page.add_child(instance=child_guide)

        rf = RequestFactory()
        request = rf.get("/admin/pages/")
        from django.contrib.messages.storage.fallback import FallbackStorage
        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))

        # 1. 验证复制父页面被拦截
        resp_copy = intercept_external_page_copy(request, parent_page)
        self.assertIsNotNone(resp_copy)
        self.assertEqual(resp_copy.status_code, 302)

        # 2. 验证删除父页面被拦截
        resp_delete = intercept_external_page_delete(request, parent_page)
        self.assertIsNotNone(resp_delete)
        self.assertEqual(resp_delete.status_code, 302)

        # 3. 验证批量删除包含此父页面时被拦截
        with self.assertRaises(PermissionDenied):
            intercept_external_page_bulk_delete(
                request=request,
                action_type="delete",
                objects=[parent_page],
                action=None,
            )

    def test_wagtail_admin_hooks_block_generic_external_content_descendants(self) -> None:
        """
        验证后台钩子能动态识别所有外置页面类型（不仅限于 GuidePage）：
        验证包含 LifecycleTestPage（不同于 GuidePage 的另一种外置页面）的父页面同样被拦截。
        """
        from external_content.tests.test_lifecycle import LifecycleTestPage
        from guides.wagtail_hooks import _get_external_page_models, _has_external_content_descendants
        from django.db import connection
        from django.contrib.contenttypes.models import ContentType
        with connection.schema_editor() as editor:
            if LifecycleTestPage._meta.db_table not in connection.introspection.table_names():
                editor.create_model(LifecycleTestPage)
        ContentType.objects.clear_cache()
        ct, _ = ContentType.objects.get_or_create(app_label="external_content", model="lifecycletestpage")

        ext_models = _get_external_page_models()
        self.assertIn(GuidePage, ext_models)
        self.assertIn(LifecycleTestPage, ext_models)

        parent_page = Page(title="测试通用外置父目录", slug=f"parent-gen-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=parent_page)

        # 挂载 LifecycleTestPage（非 GuidePage 外置模型代表）
        other_ext_child = LifecycleTestPage(
            title="另一种外置模型子页",
            slug=f"other-ext-{uuid.uuid4().hex[:6]}",
            native_intro="原生字段",
            primary_body=[{"type": "heading", "value": "标题"}],
            secondary_body=[{"type": "quote", "value": "引述"}],
            live=False,
        )
        other_ext_child.content_type = ct
        parent_page.add_child(instance=other_ext_child)

        self.assertTrue(_has_external_content_descendants(parent_page))

        # 验证钩子对含有非 GuidePage 外置模型的父页面依然实施严格阻断
        rf = RequestFactory()
        request = rf.get("/admin/pages/")
        from django.contrib.messages.storage.fallback import FallbackStorage
        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))

        resp_copy = intercept_external_page_copy(request, parent_page)
        self.assertIsNotNone(resp_copy)
        self.assertEqual(resp_copy.status_code, 302)

        resp_delete = intercept_external_page_delete(request, parent_page)
        self.assertIsNotNone(resp_delete)
        self.assertEqual(resp_delete.status_code, 302)

        with self.assertRaises(PermissionDenied):
            intercept_external_page_bulk_delete(
                request=request,
                action_type="delete",
                objects=[parent_page],
                action=None,
            )

        # 空父页面正常放行（返回 False）
        empty_parent = Page(title="空父目录", slug=f"empty-p-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=empty_parent)
        self.assertFalse(_has_external_content_descendants(empty_parent))

    def test_wagtail_admin_hooks_fail_closed_on_inspection_error(self) -> None:
        """
        验证 Fail-Closed 安全原则：当后代或页面外置探测抛出异常时，
        钩子绝不静默放行，而是全部强制拦截或抛出 PermissionDenied。
        """
        parent_page = Page(title="异常探测父页", slug=f"parent-err-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=parent_page)

        rf = RequestFactory()
        request = rf.get("/admin/pages/")
        from django.contrib.messages.storage.fallback import FallbackStorage
        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))

        with mock.patch("guides.wagtail_hooks._has_external_content_descendants", side_effect=RuntimeError("模拟探测异常")):
            # 复制被安全阻断（302 跳转至列表并提示错误）
            resp_copy = intercept_external_page_copy(request, parent_page)
            self.assertIsNotNone(resp_copy)
            self.assertEqual(resp_copy.status_code, 302)

            # 删除被安全阻断（302 跳转至列表并提示错误）
            resp_delete = intercept_external_page_delete(request, parent_page)
            self.assertIsNotNone(resp_delete)
            self.assertEqual(resp_delete.status_code, 302)

            # 批量删除强制抛出 PermissionDenied
            with self.assertRaises(PermissionDenied):
                intercept_external_page_bulk_delete(
                    request=request,
                    action_type="delete",
                    objects=[parent_page],
                    action=None,
                )
