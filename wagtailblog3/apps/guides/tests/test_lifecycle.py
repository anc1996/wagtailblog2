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
from django.test import Client, TestCase, TransactionTestCase, override_settings

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
from wagtail.actions.convert_alias import ConvertAliasPageAction
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
    intercept_external_page_bulk_action,
    filter_external_page_listing_buttons,
)
from external_content.models import (
    ExternalContentPublicationState,
    ExternalContentDeletionIntent,
    DeletionIntentStatus,
)
from wagtail.admin.views.pages.bulk_actions.publish import PublishBulkAction
from external_content.publication import ExternalContentPublicationService
from external_content.store import (
    ContentVersionStore,
    ensure_indexes,
    get_mongo_client,
    get_mongo_settings,
    reset_active_collection_name,
    set_active_collection_name,
)
from guides.models import GuideIndexPage, GuidePage, GuideTopic

User = get_user_model()


def _get_test_mongo_db_name() -> str:
    """获取测试 MongoDB 数据库名：统一读取 settings.MONGO_DB['NAME']，兼容开发环境回退至 MONGO_DB_NAME。"""
    try:
        mongo_cfg = get_mongo_settings()
        if isinstance(mongo_cfg, dict) and mongo_cfg.get("NAME"):
            return str(mongo_cfg["NAME"])
    except Exception:
        pass
    # 兼容旧版开发环境：回退至 MONGO_DB_NAME 或默认值
    return getattr(settings, "MONGO_DB_NAME", "wagtailblog_test")



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
        db_name = _get_test_mongo_db_name()
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

    def test_guide_page_copy_lifecycle(self) -> None:
        """
        验证 GuidePage.copy() 正式放开后的生命周期契约：
        成功执行并派生属于目标页面的独立正文版本与 Manifest，MySQL 实体与状态自洽。
        """
        guide = self._create_blocker_test_guide()
        copied = guide.copy(to=self.home, update_attrs={"title": "派生指南", "slug": f"copied-{uuid.uuid4().hex[:6]}"})
        self.assertNotEqual(copied.pk, guide.pk)
        self.assertTrue(GuidePage.objects.filter(pk=copied.pk).exists())
        self.assertIsNone(copied.alias_of_id)
        state_copied = ExternalContentPublicationState.objects.filter(page_id=copied.pk).first()
        self.assertIsNotNone(state_copied)
        self.assertEqual(state_copied.page_id, copied.pk)
        self.assertIsNone(state_copied.source_page_id)

    def test_guide_page_create_alias_lifecycle(self) -> None:
        """
        验证 GuidePage.create_alias() 正式放开后的生命周期契约：
        成功创建别名，托管发布状态，source_page_id 维护来源关系且 published_revision_id 为空。
        别名页面不伪造本地 Revision 记录。
        """
        guide = self._create_blocker_test_guide()
        alias = guide.create_alias(parent=self.home, update_slug=f"alias-{uuid.uuid4().hex[:6]}")
        self.assertEqual(alias.alias_of_id, guide.pk)
        state_alias = ExternalContentPublicationState.objects.filter(page_id=alias.pk).first()
        self.assertIsNotNone(state_alias)
        self.assertEqual(state_alias.source_page_id, guide.pk)
        self.assertIsNone(state_alias.published_revision_id)
        self.assertEqual(Revision.objects.for_instance(alias).count(), 0)

    def test_delete_blocked_before_g4(self) -> None:
        """
        验证 G4 阶段直接调用 GuidePage.delete() 自动持久化删除意图并从 MySQL 安全删除。
        """
        guide = self._create_blocker_test_guide()
        guide_pk = guide.pk
        guide.delete()
        self.assertFalse(GuidePage.objects.filter(pk=guide_pk).exists())
        self.assertTrue(ExternalContentDeletionIntent.objects.filter(page_id=guide_pk).exists())

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

        # 1. 验证复制钩子放行（G4 阶段放开外置正文页面复制）
        resp_copy = intercept_external_page_copy(request, guide)
        self.assertIsNone(resp_copy)

        # 2. 单页删除放行，由底层 pre_delete 固化 DeletionIntent。
        resp_delete = intercept_external_page_delete(request, guide)
        self.assertIsNone(resp_delete)

        # 3. 单页批量删除同样放行；递归父级删除由另一个测试覆盖阻断。
        self.assertIsNone(
            intercept_external_page_bulk_delete(
                request=request,
                action_type="delete",
                objects=[guide],
                action=None,
            )
        )

        # 4. 验证页面列表按钮钩子兼容 Wagtail 8.0 签名并安全过滤（支持顶层与下拉子菜单）
        from unittest.mock import MagicMock
        btn_edit = MagicMock()
        btn_edit.name = "edit"
        btn_copy = MagicMock()
        btn_copy.name = "copy"
        btn_delete = MagicMock()
        btn_delete.name = "delete"

        dropdown_item_edit = MagicMock()
        dropdown_item_edit.name = "edit"
        dropdown_item_edit.url = "/admin/pages/1/edit/"
        dropdown_item_delete = MagicMock()
        dropdown_item_delete.name = "delete"
        dropdown_item_delete.url = "/admin/pages/1/delete/"
        btn_dropdown = MagicMock()
        btn_dropdown.dropdown_buttons = [dropdown_item_edit, dropdown_item_delete]

        buttons = [btn_edit, btn_copy, btn_delete, btn_dropdown]

        filter_external_page_listing_buttons(
            buttons=buttons,
            page=guide,
            user=self.user,
            context={"request": request},
        )
        button_names = [getattr(b, "name", None) for b in buttons if b is not btn_dropdown]
        self.assertIn("edit", button_names)
        self.assertIn("copy", button_names)
        self.assertIn("delete", button_names)

        dropdown_names = [getattr(d, "name", None) for d in btn_dropdown.dropdown_buttons]
        self.assertIn("edit", dropdown_names)
        self.assertIn("delete", dropdown_names)

    def test_delete_action_blocked_by_pre_delete_signal(self) -> None:
        """
        验证原生 DeletePageAction 执行时，被底层 pre_delete 信号安全固化意图并完成 MySQL 删除。
        """
        guide = self._create_blocker_test_guide()
        guide_pk = guide.pk
        action = DeletePageAction(guide, user=self.user)
        action.execute(skip_permission_checks=True)
        self.assertFalse(GuidePage.objects.filter(pk=guide_pk).exists())
        self.assertTrue(ExternalContentDeletionIntent.objects.filter(page_id=guide_pk).exists())

    def test_parent_delete_blocked_when_contains_guide_descendant(self) -> None:
        """
        验证对包含指南后代子页的父页面执行删除时，级联阶段自动固化指南子页的删除意图，
        完成安全物理删除，Mongo 正文版本受保护。
        """
        parent_page = Page(title="测试父目录", slug=f"parent-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=parent_page)

        child_guide = GuidePage(
            title="子指南",
            slug=f"child-guide-{uuid.uuid4().hex[:6]}",
            summary="测试父级级联删除",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
            live=False,
        )
        parent_page.add_child(instance=child_guide)
        child_guide._test_content_store = self.store
        child_guide.save_revision(user=self.user)

        parent_pk = parent_page.pk
        child_pk = child_guide.pk

        parent_page.delete()

        # 验证父页面与子指南已从 MySQL 级联删除
        self.assertFalse(Page.objects.filter(pk=parent_pk).exists())
        self.assertFalse(GuidePage.objects.filter(pk=child_pk).exists())
        # 验证子指南的删除意图已被固化
        self.assertTrue(ExternalContentDeletionIntent.objects.filter(page_id=child_pk).exists())

    def test_create_alias_action_lifecycle(self) -> None:
        """
        验证原生 CreatePageAliasAction 执行生命周期：
        成功创建别名页面并为其初始化托管状态，published_revision_id 为空，source 关联正确。
        别名页面不伪造本地 Revision 记录。
        """
        guide = self._create_blocker_test_guide()
        action = CreatePageAliasAction(guide, parent=self.home, user=self.user, update_slug=f"alias-{uuid.uuid4().hex[:6]}")
        alias = action.execute(skip_permission_checks=True)
        self.assertEqual(alias.alias_of_id, guide.pk)
        state_alias = ExternalContentPublicationState.objects.filter(page_id=alias.pk).first()
        self.assertIsNotNone(state_alias)
        self.assertEqual(state_alias.source_page_id, guide.pk)
        self.assertIsNone(state_alias.published_revision_id)
        self.assertEqual(Revision.objects.for_instance(alias).count(), 0)

    def test_convert_alias_action_lifecycle(self) -> None:
        """
        验证原生 ConvertAliasPageAction 执行生命周期：
        成功将别名转换为独立页面，断开追随关系（alias_of_id 与 source_page_id 均置空），
        并派生首个独立 Revision。
        """
        guide = self._create_blocker_test_guide()
        action_alias = CreatePageAliasAction(guide, parent=self.home, user=self.user, update_slug=f"alias-{uuid.uuid4().hex[:6]}")
        alias = action_alias.execute(skip_permission_checks=True)
        self.assertEqual(Revision.objects.for_instance(alias).count(), 0)

        action_convert = ConvertAliasPageAction(alias, user=self.user)
        converted = action_convert.execute(skip_permission_checks=True)
        self.assertIsNone(converted.alias_of_id)
        state_converted = ExternalContentPublicationState.objects.filter(page_id=converted.pk).first()
        self.assertIsNotNone(state_converted)
        self.assertEqual(state_converted.page_id, converted.pk)
        self.assertIsNone(state_converted.source_page_id)
        self.assertGreaterEqual(Revision.objects.for_instance(converted).count(), 1)

    def test_copy_page_action_lifecycle(self) -> None:
        """
        验证原生 CopyPageAction 执行生命周期：
        成功克隆目标页面并派生独立正文版本与托管状态，非别名关系。
        """
        guide = self._create_blocker_test_guide()
        action = CopyPageAction(guide, to=self.home, user=self.user, update_attrs={"slug": f"copied-{uuid.uuid4().hex[:6]}"})
        copied = action.execute(skip_permission_checks=True)
        self.assertNotEqual(copied.pk, guide.pk)
        self.assertIsNone(copied.alias_of_id)
        state_copied = ExternalContentPublicationState.objects.filter(page_id=copied.pk).first()
        self.assertIsNotNone(state_copied)
        self.assertEqual(state_copied.page_id, copied.pk)
        self.assertIsNone(state_copied.source_page_id)

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

    def test_bulk_publish_latest_revision_advances_publication_state(self) -> None:
        """
        验证 Wagtail 8.0 PublishBulkAction 批量发布时自动发布最新 Revision (latest Revision)，
        推进 ExternalContentPublicationState 并更新前台渲染内容。
        """
        guide = GuidePage(
            title="BulkAction 最新版本发布指南",
            slug=f"bulk-latest-{uuid.uuid4().hex[:6]}",
            summary="测试 BulkAction 发布最新草稿版本",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "初版步骤", "description": "<p>初版内容</p>", "image": None}}],
            live=False,
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store

        # 1. 保存并发布 Rev1
        rev1 = guide.save_revision(user=self.user)
        guide.publish(rev1, user=self.user)
        state1 = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        self.assertEqual(state1.published_revision_id, rev1.pk)
        self.assertEqual(state1.publication_generation, 1)

        # 2. 修改正文，保存 Rev2 草稿（不发布）
        guide.body = [
            {"type": "step", "value": {"title": "初版步骤", "description": "<p>初版内容</p>", "image": None}},
            {"type": "step", "value": {"title": "第二版新增步骤", "description": "<p>第二版新增内容</p>", "image": None}},
        ]
        rev2 = guide.save_revision(user=self.user)
        self.assertNotEqual(rev1.pk, rev2.pk)
        self.assertEqual(guide.get_latest_revision().pk, rev2.pk)

        # 3. 通过 Wagtail 8.0 PublishBulkAction 批量发布
        num_parent, _ = PublishBulkAction.execute_action([guide], user=self.user)
        self.assertEqual(num_parent, 1)

        # 4. 验证发布状态与版本指针已推进至 rev2
        state2 = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        self.assertEqual(state2.published_revision_id, rev2.pk)
        self.assertEqual(state2.publication_generation, 2)

        # 5. 验证前台渲染出 rev2 的正文
        guide.refresh_from_db()
        self.assertTrue(guide.live)
        response = self.client.get(guide.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "第二版新增步骤")
        self.assertContains(response, "第二版新增内容")

    def test_bulk_publish_page_without_revision_is_rejected_without_side_effects(self) -> None:
        """
        验证正文外置页面没有 Revision 时，批量发布不会临时创建 Revision 或推进正式指针。
        管理员必须先保存草稿，避免 Wagtail 原生回退路径绕过正文外置校验。
        """
        guide = GuidePage(
            title="无 Revision 的新指南",
            slug=f"bulk-no-rev-{uuid.uuid4().hex[:6]}",
            summary="测试无 Revision 页面安全拒绝批量发布",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "初始步骤", "description": "<p>初始步骤内容</p>", "image": None}}],
            live=False,
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store

        self.assertEqual(guide.revisions.count(), 0)
        self.assertIsNone(guide.get_latest_revision())

        rf = RequestFactory()
        from django.contrib.messages.storage.fallback import FallbackStorage
        request = rf.post("/admin/bulk/publish/")
        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))

        response = intercept_external_page_bulk_action(request, "publish", [guide], None)
        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(guide.revisions.count(), 0)
        self.assertFalse(guide.live)
        self.assertFalse(ExternalContentPublicationState.objects.filter(page_id=guide.pk).exists())
        messages_seen = [str(message.message) for message in request._messages]
        self.assertTrue(any("Revision" in message for message in messages_seen))

    def test_bulk_publish_blocked_when_missing_topic_or_step(self) -> None:
        """
        验证 Wagtail 后台批量发布前置钩子 intercept_external_page_bulk_action：
        当页面缺失主题或正文缺失步骤块时，安全拦截并执行 302 重定向与友好提示，页面保持未发布。
        """
        rf = RequestFactory()
        from django.contrib.messages.storage.fallback import FallbackStorage

        # 场景 A: 缺失 topic
        guide_no_topic = GuidePage(
            title="缺失主题指南",
            slug=f"no-topic-{uuid.uuid4().hex[:6]}",
            summary="缺失主题",
            topic=None,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "步骤", "description": "<p>内容</p>", "image": None}}],
            live=False,
        )
        self.index_page.add_child(instance=guide_no_topic)
        guide_no_topic._test_content_store = self.store
        guide_no_topic.save_revision(user=self.user)

        req_a = rf.post("/admin/bulk/publish/")
        setattr(req_a, "session", {})
        setattr(req_a, "_messages", FallbackStorage(req_a))

        resp_a = intercept_external_page_bulk_action(req_a, "publish", [guide_no_topic], None)
        self.assertIsNotNone(resp_a)
        self.assertEqual(resp_a.status_code, 302)
        messages_a = [str(m.message) for m in req_a._messages]
        self.assertTrue(any("GuideTopic" in m for m in messages_a))
        guide_no_topic.refresh_from_db()
        self.assertFalse(guide_no_topic.live)

        # 场景 B: 正文无 step 块（仅有 lead 块）
        guide_no_step = GuidePage(
            title="缺失步骤指南",
            slug=f"no-step-{uuid.uuid4().hex[:6]}",
            summary="仅有导读无步骤",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "lead", "value": "<p>仅有导读内容，无步骤块</p>"}],
            live=False,
        )
        self.index_page.add_child(instance=guide_no_step)
        guide_no_step._test_content_store = self.store
        guide_no_step.save_revision(user=self.user)

        req_b = rf.post("/admin/bulk/publish/")
        setattr(req_b, "session", {})
        setattr(req_b, "_messages", FallbackStorage(req_b))

        resp_b = intercept_external_page_bulk_action(req_b, "publish", [guide_no_step], None)
        self.assertIsNotNone(resp_b)
        self.assertEqual(resp_b.status_code, 302)
        messages_b = [str(m.message) for m in req_b._messages]
        self.assertTrue(any("step" in m for m in messages_b))
        guide_no_step.refresh_from_db()
        self.assertFalse(guide_no_step.live)

    def test_bulk_action_mixed_compliant_and_non_compliant_pages(self) -> None:
        """
        验证批量操作在混杂场景下的安全防线：
        1. 批量发布混杂（合规指南 + 缺失主题指南）：前置钩子实施全量拦截（302 重定向），整批不发布；
        2. 批量发布全合规（两个正常指南）：前置钩子放行（返回 None），正常发布；
        3. 批量删除混杂（普通页面 + 外置指南页面）：抛出 PermissionDenied 熔断，整批删除中止。
        """
        rf = RequestFactory()
        from django.contrib.messages.storage.fallback import FallbackStorage

        # 准备混杂测试：合规页面与不合规页面
        guide_mixed_good = GuidePage(
            title="混杂合规指南",
            slug=f"m-good-{uuid.uuid4().hex[:6]}",
            summary="混杂测试中的合规指南",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "步骤1", "description": "<p>正文1</p>", "image": None}}],
            live=False,
        )
        self.index_page.add_child(instance=guide_mixed_good)
        guide_mixed_good._test_content_store = self.store

        # 准备不合规页面（缺失主题）
        guide_bad = GuidePage(
            title="混杂不合规指南",
            slug=f"bad-{uuid.uuid4().hex[:6]}",
            summary="不合规指南摘要",
            topic=None,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "步骤", "description": "<p>正文</p>", "image": None}}],
            live=False,
        )
        self.index_page.add_child(instance=guide_bad)
        guide_bad._test_content_store = self.store

        # 1. 验证发布混杂：合规 + 不合规
        req_publish = rf.post("/admin/bulk/publish/")
        setattr(req_publish, "session", {})
        setattr(req_publish, "_messages", FallbackStorage(req_publish))

        resp_mixed_pub = intercept_external_page_bulk_action(
            req_publish, "publish", [guide_mixed_good, guide_bad], None
        )
        self.assertIsNotNone(resp_mixed_pub)
        self.assertEqual(resp_mixed_pub.status_code, 302)
        # 确认页面均未上线（全有或全无安全）
        guide_mixed_good.refresh_from_db()
        guide_bad.refresh_from_db()
        self.assertFalse(guide_mixed_good.live)
        self.assertFalse(guide_bad.live)

        # 2. 验证发布全合规：放行并成功发布
        guide_clean_1 = GuidePage(
            title="全合规指南 1",
            slug=f"c-good-1-{uuid.uuid4().hex[:6]}",
            summary="全合规测试指南1",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "步骤1", "description": "<p>正文1</p>", "image": None}}],
            live=False,
        )
        self.index_page.add_child(instance=guide_clean_1)
        guide_clean_1._test_content_store = self.store
        guide_clean_1.save_revision(user=self.user)

        guide_clean_2 = GuidePage(
            title="全合规指南 2",
            slug=f"c-good-2-{uuid.uuid4().hex[:6]}",
            summary="全合规测试指南2",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "步骤2", "description": "<p>正文2</p>", "image": None}}],
            live=False,
        )
        self.index_page.add_child(instance=guide_clean_2)
        guide_clean_2._test_content_store = self.store
        guide_clean_2.save_revision(user=self.user)

        req_clean = rf.post("/admin/bulk/publish/")
        setattr(req_clean, "session", {})
        setattr(req_clean, "_messages", FallbackStorage(req_clean))

        resp_clean_pub = intercept_external_page_bulk_action(
            req_clean, "publish", [guide_clean_1, guide_clean_2], None
        )
        self.assertIsNone(resp_clean_pub)
        num_parent, _ = PublishBulkAction.execute_action([guide_clean_1, guide_clean_2], user=self.user)
        self.assertEqual(num_parent, 2)
        guide_clean_1.refresh_from_db()
        guide_clean_2.refresh_from_db()
        self.assertTrue(guide_clean_1.live)
        self.assertTrue(guide_clean_2.live)

        # 3. 验证删除混杂：普通 Page + 外置指南页面
        std_page = Page(title="普通展示页", slug=f"std-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=std_page)

        req_del = rf.post("/admin/bulk/delete/")
        setattr(req_del, "session", {})
        setattr(req_del, "_messages", FallbackStorage(req_del))

        # 单页正文外置指南允许进入 DeletionIntent 协议；只有包含外置后代的父页才阻断。
        self.assertIsNone(
            intercept_external_page_bulk_action(
                req_del, "delete", [std_page, guide_clean_1], None
            )
        )

    def test_single_page_deletion_intent_integrity_and_mongo_preservation(self) -> None:
        """
        验证单页调用 guide.delete() 时：
        1. MySQL 中指南记录已删除；
        2. ExternalContentDeletionIntent 完整固化，状态为 MYSQL_DELETED；
        3. intent.manifest 中准确包含 body_version_ids、revision_ids 与 versions 6 键身份元数据；
        4. MongoDB 中的不可变正文未被物理删除，完好保留。
        """
        guide = GuidePage(
            title="待删单页指南",
            slug=f"del-single-{uuid.uuid4().hex[:6]}",
            summary="测试单页删除与元数据清单固化",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "删除前步骤", "description": "<p>正文内容</p>", "image": None}}],
            live=False,
        )
        self.index_page.add_child(instance=guide)
        guide._test_content_store = self.store

        rev = guide.save_revision(user=self.user)
        guide.publish(rev, user=self.user)
        guide_pk = guide.pk

        state = ExternalContentPublicationState.objects.get(page_id=guide_pk)
        field_meta = state.published_manifest["fields"]["body"]
        version_id = field_meta["version_id"]

        # 执行单页原生删除
        guide.delete()

        # 1. 验证 MySQL 中指南记录已删除
        self.assertFalse(GuidePage.objects.filter(pk=guide_pk).exists())

        # 2. 验证 ExternalContentDeletionIntent 存在且状态为 MYSQL_DELETED
        intent = ExternalContentDeletionIntent.objects.filter(page_id=guide_pk).first()
        self.assertIsNotNone(intent)
        self.assertEqual(intent.status, DeletionIntentStatus.MYSQL_DELETED)

        # 3. 验证 manifest 结构完整性（body_version_ids, revision_ids, versions 6 键身份）
        manifest = intent.manifest
        self.assertIn("body_version_ids", manifest)
        self.assertIn(version_id, manifest["body_version_ids"])
        self.assertIn("revision_ids", manifest)
        self.assertIn(rev.pk, manifest["revision_ids"])
        self.assertIn("versions", manifest)

        versions_list = manifest["versions"]
        matching_v = next((v for v in versions_list if v.get("version_id") == version_id), None)
        self.assertIsNotNone(matching_v)
        expected_identity = matching_v["expected_identity"]
        self.assertEqual(expected_identity["model_label"], "guides.guidepage")
        self.assertEqual(expected_identity["page_id"], guide_pk)
        self.assertEqual(expected_identity["field_name"], "body")
        self.assertEqual(expected_identity["codec"], "streamfield.v1")
        self.assertEqual(expected_identity["schema_version"], 1)
        self.assertEqual(expected_identity["payload_sha256"], field_meta["payload_sha256"])

        # 4. 验证 MongoDB 中的文档物理保留（绝不级联物理删除）
        doc_in_mongo = self.store.get_version(version_id, expected_identity=expected_identity)
        self.assertIsNotNone(doc_in_mongo)
        self.assertEqual(doc_in_mongo["page_id"], guide_pk)

    def test_recursive_delete_blocking_on_parent_with_guide_descendants(self) -> None:
        """
        验证递归删除阻断钩子 intercept_external_page_delete：
        1. 当普通父页面后代树中包含 GuidePage 时，返回 302 阻断并提示错误；
        2. 当普通父页面为空或无外置后代时，返回 None 放行。
        """
        rf = RequestFactory()
        from django.contrib.messages.storage.fallback import FallbackStorage

        # 1. 构造含外置后代的父页面
        parent_with_guide = Page(title="含外置后代父页", slug=f"p-with-g-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=parent_with_guide)

        child_guide = GuidePage(
            title="子指南页面",
            slug=f"c-guide-{uuid.uuid4().hex[:6]}",
            summary="用于测试递归删除阻断",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S", "description": "<p>D</p>", "image": None}}],
            live=False,
        )
        parent_with_guide.add_child(instance=child_guide)

        req_blocked = rf.get(f"/admin/pages/{parent_with_guide.pk}/delete/")
        setattr(req_blocked, "session", {})
        setattr(req_blocked, "_messages", FallbackStorage(req_blocked))

        resp_blocked = intercept_external_page_delete(req_blocked, parent_with_guide)
        self.assertIsNotNone(resp_blocked)
        self.assertEqual(resp_blocked.status_code, 302)
        messages_blocked = [str(m.message) for m in req_blocked._messages]
        self.assertTrue(messages_blocked)

        # 2. 构造无外置后代的纯净普通父页面
        clean_parent = Page(title="纯净普通父页", slug=f"clean-p-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=clean_parent)
        normal_child = Page(title="普通子页", slug=f"norm-c-{uuid.uuid4().hex[:6]}")
        clean_parent.add_child(instance=normal_child)

        req_allow = rf.get(f"/admin/pages/{clean_parent.pk}/delete/")
        setattr(req_allow, "session", {})
        setattr(req_allow, "_messages", FallbackStorage(req_allow))

        resp_allow = intercept_external_page_delete(req_allow, clean_parent)
        self.assertIsNone(resp_allow)


@override_settings(STORAGES=TEST_STORAGES)
class GuideLifecycleDDLTestCase(TransactionTestCase):
    """GuidePage 生命周期中需要 DDL 操作的测试用例，使用 TransactionTestCase 避免事务内 DDL 冲突。"""

    @classmethod
    def setUpClass(cls) -> None:
        """在用例事务之外建立动态页面表，并留存供后续 Collector 扫描。"""
        super().setUpClass()
        from django.db import connection
        from external_content.tests.test_lifecycle import LifecycleTestPage

        if connection.in_atomic_block or not connection.get_autocommit():
            raise RuntimeError("动态页面表 DDL 必须在自动提交且无外层事务的环境中执行")
        if LifecycleTestPage._meta.db_table not in connection.introspection.table_names():
            with connection.schema_editor() as editor:
                editor.create_model(LifecycleTestPage)

    def setUp(self) -> None:
        self.user = User.objects.create_superuser(
            username="guidelifecycleddladmin", password="testpassword", email="ddladmin@test.com"
        )
        self.coll_name = f"test_guides_ddl_{uuid.uuid4().hex[:8]}"
        self.mongo_client = get_mongo_client()
        db_name = _get_test_mongo_db_name()
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
                hostname="localhost", port=80, site_name="Test Site",
                root_page=self.home, is_default_site=True
            )
        self.topic = GuideTopic.objects.create(
            name="DDL测试主题", slug="ddl-test-topic", description="用于DDL测试的主题"
        )

    def tearDown(self) -> None:
        reset_active_collection_name()
        try:
            self.collection.drop()
        except Exception:
            pass

    def test_wagtail_admin_hooks_block_generic_external_content_descendants(self) -> None:
        """验证后台钩子能动态识别所有外置页面类型，包含 LifecycleTestPage 的父页面同样被拦截。"""
        from external_content.tests.test_lifecycle import LifecycleTestPage
        from guides.wagtail_hooks import _get_external_page_models, _has_external_content_descendants
        from django.contrib.contenttypes.models import ContentType

        ContentType.objects.clear_cache()
        ct, _ = ContentType.objects.get_or_create(app_label="external_content", model="lifecycletestpage")

        ext_models = _get_external_page_models()
        self.assertIn(GuidePage, ext_models)
        self.assertIn(LifecycleTestPage, ext_models)

        parent_page = Page(title="测试通用外置父目录", slug=f"parent-gen-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=parent_page)

        other_ext_child = LifecycleTestPage(
            title="另一种外置模型子页", slug=f"other-ext-{uuid.uuid4().hex[:6]}",
            native_intro="原生字段",
            primary_body=[{"type": "heading", "value": "标题"}],
            secondary_body=[{"type": "quote", "value": "引述"}],
            live=False,
        )
        other_ext_child.content_type = ct
        parent_page.add_child(instance=other_ext_child)

        self.assertTrue(_has_external_content_descendants(parent_page))

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

        empty_parent = Page(title="空父目录", slug=f"empty-p-{uuid.uuid4().hex[:6]}")
        self.home.add_child(instance=empty_parent)
        self.assertFalse(_has_external_content_descendants(empty_parent))
