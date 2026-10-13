"""
专题指南 (GuidePage) 用户前后台真实 HTTP 生命周期与表单交互回归测试套件。

测试定位与架构边界：
1. 本测试核心被测操作通过真实 Django Test Client 发起标准 HTTP POST / GET 请求，严格模拟编辑用户
   在 Wagtail 管理后台操作页面的真实生命周期流程；前置基准环境（站点树、父索引页、分类元数据与测试用户）
   采用标准 ORM 夹具装载，被测核心操作严格走真实 HTTP 表单与管理端点，杜绝在被测业务流中使用底层 PageAction / ORM save()
   绕过表单清洗门禁与视图钩子。
2. 覆盖完整生命周期端点：
   - 创建页面并存草稿 (POST /admin/pages/add/guides/guidepage/<parent_id>/)
   - 编辑页面更新草稿 (POST /admin/pages/<id>/edit/)
   - 编辑中实时预览 (POST & GET /admin/pages/<id>/edit/preview/)
   - 页面发布与差异化门禁 (POST /admin/pages/<id>/edit/ [action-publish])
   - 版本历史查看与恢复 (GET revisions_view & POST revisions_revert)
   - 页面受控下架 (POST /admin/pages/<id>/unpublish/)
   - 页面复制与别名转换 (POST /admin/pages/<id>/copy/ & POST /admin/pages/<id>/convert_alias/)
   - 页面删除意图协议与意图受控取消 (POST /admin/pages/<id>/delete/ & GET/POST cancel_deletion_intent)
3. 隔离规范：
   - 测试密码采用进程内临时动态生成 (secrets.token_urlsafe)，严禁硬编码或日志输出；
   - MongoDB 正文集合使用 UUID 独立隔离命名 (test_guides_http_<uuid>)，测试完毕保留专属集合以备主线程串行审计，禁止 drop/GC；
   - 静态资源配置覆盖为 StaticFilesStorage，规避 Manifest 静态指纹缺失阻断；
   - 遵循双库串行测试门禁，禁止在此阶段执行物理迁移或并行破坏性操作。
"""

from __future__ import annotations

import json
import io
import re
import secrets
import uuid
from copy import deepcopy
from datetime import timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.http import HttpResponse
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from wagtail.models import (
    GroupApprovalTask,
    GroupPagePermission,
    Locale,
    Page,
    Revision,
    Site,
    Workflow,
    WorkflowPage,
    WorkflowState,
    WorkflowTask,
)
from wagtail.test.utils.form_data import inline_formset, nested_form_data, rich_text, streamfield

from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)
from external_content.deletion import record_deletion_intent
from external_content.exceptions import PublicationWorkflowRevisionDriftError
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

TEST_STORAGES = {
    **settings.STORAGES,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


def _get_test_mongo_db_name() -> str:
    """
    获取专用测试环境 MongoDB 数据库名称。

    通过 get_mongo_settings() 获取确切配置，若缺失或未配置有效库名则立即失败抛出异常，
    坚决杜绝静默降级或猜库行为。
    """
    mongo_cfg = get_mongo_settings()
    name = mongo_cfg.get("NAME")
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError("MongoDB 测试配置缺失有效的 'NAME' 项，显式阻断并禁止猜库回退")
    return name.strip()


@override_settings(STORAGES=TEST_STORAGES)
class GuideUserLifecycleHttpTestCase(TestCase):
    """专题指南后台 HTTP 表单真实生命周期回归测试集。"""

    def setUp(self) -> None:
        """初始化后台超级用户登录态、MongoDB 专属隔离集合、站点树与基础分类元数据。"""
        self.random_password = secrets.token_urlsafe(32)
        self.username = f"http_tester_{uuid.uuid4().hex[:8]}"
        self.user = User.objects.create_superuser(
            username=self.username,
            password=self.random_password,
            email=f"{self.username}@test.com",
        )

        self.client = Client()
        self.client.force_login(self.user)

        # MongoDB UUID 专属集合隔离
        self.coll_name = f"test_guides_http_{uuid.uuid4().hex[:8]}"
        self.mongo_client = get_mongo_client()
        db_name = _get_test_mongo_db_name()
        self.collection = self.mongo_client[db_name][self.coll_name]
        ensure_indexes(self.collection)
        set_active_collection_name(self.coll_name)
        self.store = ContentVersionStore(collection=self.collection)

        # 根页面与站点初始化
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

        # 基础分类元数据
        self.topic = GuideTopic.objects.create(
            name="云原生与可观测性",
            slug="cloud-observability",
            description="Kubernetes, Prometheus 与分布式追踪实战",
        )

        # 指南索引父页面
        self.index_page = GuideIndexPage(
            title="技术演进指南",
            slug="tech-evolution-guides",
            intro="企业级系统演进与架构实战指南集合",
            locale=self.locale,
        )
        self.home.add_child(instance=self.index_page)
        self.index_page.save_revision().publish()

    def tearDown(self) -> None:
        """
        测试清理阶段：仅重置线程局部变量中的活动集合名称。

        严格契约红线：
        禁止在本阶段执行 collection.drop() 等任何物理清理操作，严禁静默吞并异常，
        完整保留测试生成的专属 MongoDB 集合以备主线程串行审计与回归验证。
        """
        reset_active_collection_name()

    def _post_controlled(
        self, endpoint: str, data: Optional[Dict[str, Any]] = None, **headers: Any,
    ) -> HttpResponse:
        """走真实 GET ticket → HTTP prepare → 原生 POST，不替换 Redis/防重门禁。

        每次有意保存领取新身份；相同正文再次保存也不是旧请求重放。
        TestCase 外层事务不会真实提交，不能以本 helper 证明 on_commit 完成登记。
        权限、CSRF 负例和网关不支持的工作流仍裸请求验证其原有拒绝边界。
        """
        payload = deepcopy(data) if data is not None else {}
        self.assertFalse(
            {"_ec_ticket", "_ec_token", "_ec_target"}.intersection(payload),
            "受控测试载荷不得复用旧防重身份",
        )
        page = self.client.get(endpoint, **headers)
        self.assertEqual(page.status_code, 200, "受控页面 GET 未成功")
        marker = re.search(
            r'<script\b(?=[^>]*\bdata-external-content-guard)[^>]*>(.*?)</script>',
            page.content.decode(page.charset), re.DOTALL,
        )
        self.assertIsNotNone(marker, "受控页面缺少防重配置")
        config = json.loads(marker.group(1))
        self.assertFalse(config.get("unavailable"), "防重存储不可用，不允许降级裸提交")
        self.assertEqual(config["target"], endpoint)
        self.assertEqual(config["prepare"], reverse("external_content:prepare"))
        prepared = self.client.post(
            config["prepare"],
            {**payload, "_ec_ticket": config["ticket"], "_ec_target": endpoint},
            **headers,
        )
        # 错误只报告状态，绝不输出签发载体或完整请求。
        self.assertEqual(prepared.status_code, 200, "防重 HTTP prepare 未成功")
        token = prepared.json().get("token")
        self.assertTrue(isinstance(token, str) and bool(token), "防重 prepare 未签发有效载体")
        return self.client.post(endpoint, {**payload, "_ec_token": token}, **headers)

    def _build_guide_form_payload(
        self,
        title: str = "分布式追踪排障全景指南",
        slug: str = "distributed-tracing-guide",
        summary: str = "基于 OpenTelemetry 与 Jaeger 的全链路分析",
        topic_id: Optional[int] = None,
        difficulty: str = "intermediate",
        body_blocks: Optional[List[tuple[str, Any]]] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """利用 Wagtail 官方 nested_form_data 辅助函数构建合规的后台表单载荷。"""
        if body_blocks is None:
            body_blocks = [
                ("lead", "<p>全链路追踪是微服务架构高可用运维的核心观测基石。</p>"),
                (
                    "step",
                    {
                        "title": "部署 OTel Collector 守护进程",
                        "description": "<p>使用 DaemonSet 部署采集代理并开启 gRPC 接收端点。</p>",
                        "image": "",
                    },
                ),
            ]

        form_blocks: List[tuple[str, Any]] = []
        rich_text_fields = {"step": "description", "tip": "content", "faq": "answer"}
        for block_type, block_value in body_blocks:
            if block_type == "lead":
                block_value = rich_text(block_value, features=["bold", "italic", "link"])
            elif block_type in rich_text_fields:
                block_value = dict(block_value)
                field_name = rich_text_fields[block_type]
                block_value[field_name] = rich_text(block_value[field_name])
            form_blocks.append((block_type, block_value))

        raw_dict: Dict[str, Any] = {
            "title": title,
            "slug": slug,
            "summary": summary,
            "difficulty": difficulty,
            "body": streamfield(form_blocks),
            "related_pages": inline_formset([]),
        }
        if topic_id is not None:
            raw_dict["topic"] = str(topic_id)

        if extra_fields:
            raw_dict.update(extra_fields)

        return nested_form_data(raw_dict)

    def test_01_http_create_page_save_draft(self) -> None:
        """
        测试 1：通过 Wagtail 后台 add 视图提交表单创建页面并存为草稿。

        业务不变量断言：
        - HTTP 响应重定向到 edit 视图；
        - 页面实例成功写入 MySQL，live=False，has_unpublished_changes=True；
        - 自动创建初始 Revision 并生成合规 Manifest 快照；
        - MongoDB 专属集合中成功固化初始不可变正文版本。
        """
        add_url = reverse(
            "wagtailadmin_pages:add",
            args=["guides", "guidepage", self.index_page.pk],
        )

        # 草稿阶段容忍 topic 为空（is_deferred_validation=True 业务契约）
        post_data = self._build_guide_form_payload(
            title="零信任网络实战演练",
            slug="zero-trust-guide",
            summary="企业级网络边界微隔离落地手册",
            topic_id=None,
            body_blocks=[
                ("lead", "<p>零信任核心思想：从不信任，始终验证。</p>"),
            ],
        )

        response = self._post_controlled(add_url, post_data)

        # Wagtail 页面保存成功后会重定向（302）
        self.assertEqual(response.status_code, 302)

        # 验证 MySQL 中页面状态
        guide = GuidePage.objects.get(slug="zero-trust-guide")
        self.assertEqual(guide.title, "零信任网络实战演练")
        self.assertFalse(guide.live)
        self.assertTrue(guide.has_unpublished_changes)
        self.assertIsNone(guide.topic)

        # 验证 Revision 与 Manifest 快照
        latest_rev = guide.get_latest_revision()
        self.assertIsNotNone(latest_rev)
        content_dict = latest_rev.content if isinstance(latest_rev.content, dict) else json.loads(latest_rev.content)
        self.assertIn("_external_content_manifest", content_dict)
        manifest = content_dict["_external_content_manifest"]
        self.assertIn("body", manifest.get("fields", {}))

        # 验证 Mongo 不可变版本落盘
        mongo_version_id = manifest["fields"]["body"]["version_id"]
        doc = self.collection.find_one({"version_id": mongo_version_id})
        self.assertIsNotNone(doc)
        self.assertEqual(doc["page_id"], guide.pk)
        self.assertEqual(doc["field_name"], "body")

    def test_02_http_edit_page_update_draft(self) -> None:
        """
        测试 2：通过 Wagtail 后台 edit 视图更新已有草稿。

        业务不变量断言：
        - POST edit 视图提交更新返回 302 重定向；
        - 产生递增的新 Revision，MySQL 页面保持未发布状态；
        - Mongo 专属集合新增不同 version_id 的不可变版本；
        - 页面最新草稿正文正确更新。
        """
        # 预置初始草稿
        guide = GuidePage(
            title="Kafka 核心调优指南",
            slug="kafka-tuning-guide",
            summary="高吞吐消息队列配置调优",
            topic=self.topic,
            difficulty="intermediate",
            body=[{"type": "lead", "value": "<p>初始导读内容</p>"}],
            live=False,
            has_unpublished_changes=True,
        )
        self.index_page.add_child(instance=guide)
        rev1 = guide.save_revision()
        guide.refresh_from_db()

        edit_url = reverse("wagtailadmin_pages:edit", args=[guide.pk])

        # 发起 HTTP 表单编辑
        update_data = self._build_guide_form_payload(
            title="Kafka 核心调优与故障排查",
            slug="kafka-tuning-guide",
            summary="更新后的摘要：吞吐量提升 300%",
            topic_id=self.topic.pk,
            difficulty="advanced",
            body_blocks=[
                ("lead", "<p>更新后的深入导读内容</p>"),
                (
                    "step",
                    {
                        "title": "调整 linger.ms 与 batch.size",
                        "description": "<p>提高批处理聚合比率以降低网络系统调用频率。</p>",
                        "image": "",
                    },
                ),
            ],
        )

        response = self._post_controlled(edit_url, update_data)
        self.assertEqual(response.status_code, 302)

        guide.refresh_from_db()
        self.assertEqual(guide.title, "Kafka 核心调优与故障排查")
        self.assertEqual(guide.difficulty, "advanced")
        self.assertFalse(guide.live)

        rev2 = guide.get_latest_revision()
        self.assertNotEqual(rev1.pk, rev2.pk)

        # 验证新不可变正文已存入 Mongo 专属集合
        rev2_content = rev2.content if isinstance(rev2.content, dict) else json.loads(rev2.content)
        v2_id = rev2_content["_external_content_manifest"]["fields"]["body"]["version_id"]
        doc2 = self.collection.find_one({"version_id": v2_id})
        self.assertIsNotNone(doc2)
        self.assertIn("调整 linger.ms", str(doc2["payload"]))

    def test_03_http_preview_page_on_edit(self) -> None:
        """
        测试 3：通过 preview_on_edit 端点执行编辑时实时预览。

        业务不变量断言：
        - POST preview_on_edit 表单数据通过校验并返回 JSON {"is_valid": true}；
        - 紧接着 GET preview_on_edit 返回 200 预览 HTML；
        - 预览属于纯只读探测行为，无持久化副作用，不改变页面 live 状态与发布代次。
        """
        guide = GuidePage(
            title="服务网格 Istio 实战",
            slug="istio-mesh-guide",
            summary="Envoy 数据面流量接管",
            topic=self.topic,
            difficulty="beginner",
            body=[{"type": "lead", "value": "<p>服务网格导读</p>"}],
            live=False,
            has_unpublished_changes=True,
        )
        self.index_page.add_child(instance=guide)
        guide.save_revision()

        state_before = ExternalContentPublicationState.objects.filter(
            page_id=guide.pk
        ).values().get()
        revision_count_before = guide.revisions.count()
        version_count_before = self.collection.count_documents({})
        preview_url = reverse("wagtailadmin_pages:preview_on_edit", args=[guide.pk])

        preview_data = self._build_guide_form_payload(
            title="服务网格 Istio 实战（预览新标题）",
            slug="istio-mesh-guide",
            summary="预览修改摘要",
            topic_id=self.topic.pk,
            body_blocks=[
                ("lead", "<p>这是在表单中实时预览的待提交正文</p>"),
            ],
        )

        # 步骤 1：POST 预览表单数据，建立 Session 预览缓存
        post_resp = self.client.post(preview_url, preview_data)
        self.assertEqual(post_resp.status_code, 200)
        preview_json = post_resp.json()
        self.assertTrue(preview_json.get("is_valid"))

        # 步骤 2：GET 预览渲染页面
        get_resp = self.client.get(preview_url)
        self.assertEqual(get_resp.status_code, 200)
        html_content = get_resp.content.decode("utf-8")
        self.assertIn("服务网格 Istio 实战（预览新标题）", html_content)
        self.assertIn("这是在表单中实时预览的待提交正文", html_content)

        # 步骤 3：验证无持久化副作用
        guide.refresh_from_db()
        self.assertFalse(guide.live)
        self.assertEqual(guide.title, "服务网格 Istio 实战")
        self.assertEqual(
            ExternalContentPublicationState.objects.filter(page_id=guide.pk).values().get(),
            state_before,
        )
        self.assertEqual(guide.revisions.count(), revision_count_before)
        self.assertEqual(self.collection.count_documents({}), version_count_before)
        self.assertIsNone(state_before["published_revision_id"])

    def test_04_http_publish_page_gate_and_state_advancement(self) -> None:
        """
        测试 4：通过后台 edit 表单携带 action-publish 发布页面，覆盖差异化门禁与状态流转。

        业务不变量断言：
        - 缺少必填主题或缺少必填 step 块时，表单门禁生效返回 200 并暴露字段校验错误；
        - 补齐合规字段后提交发布，返回 302 重定向；
        - 页面 live=True；
        - 正式发布指针匹配 live_revision，publication_generation 从 0 前进至 1；
        - 前台 client.get(url) 返回 200 且动态渲染 Mongo 正文。
        """
        guide = GuidePage(
            title="微服务网关选型指南",
            slug="api-gateway-guide",
            summary="Envoy Gateway vs Apache APISIX",
            difficulty="intermediate",
            body=[{"type": "lead", "value": "<p>初始草稿仅有导读块</p>"}],
            live=False,
            has_unpublished_changes=True,
        )
        self.index_page.add_child(instance=guide)
        guide.save_revision()

        edit_url = reverse("wagtailadmin_pages:edit", args=[guide.pk])

        # 1. 尝试直接发布不合规草稿（缺少 topic 和 step 块）
        invalid_publish_data = self._build_guide_form_payload(
            title="微服务网关选型指南",
            slug="api-gateway-guide",
            summary="未选择主题且无操作步骤",
            topic_id=None,
            body_blocks=[
                ("lead", "<p>仅有导读块，缺少必选 step 块</p>"),
            ],
            extra_fields={"action-publish": "action-publish"},
        )
        resp_fail = self._post_controlled(edit_url, invalid_publish_data)
        # 门禁阻断，页面不跳转，返回 200 包含表单错误
        self.assertEqual(resp_fail.status_code, 200)
        guide.refresh_from_db()
        self.assertFalse(guide.live)

        # 2. 补齐合法字段与 step 块，发起真实发布
        valid_publish_data = self._build_guide_form_payload(
            title="微服务网关选型指南",
            slug="api-gateway-guide",
            summary="包含主题与完整步骤的合规指南",
            topic_id=self.topic.pk,
            body_blocks=[
                ("lead", "<p>全景对比与技术选型决策树。</p>"),
                (
                    "step",
                    {
                        "title": "性能压测与内存占用评估",
                        "description": "<p>基于 wrk2 对路由转发吞吐与 P99 延迟进行基准压测。</p>",
                        "image": "",
                    },
                ),
            ],
            extra_fields={"action-publish": "action-publish"},
        )
        resp_ok = self._post_controlled(edit_url, valid_publish_data)
        self.assertEqual(resp_ok.status_code, 302)

        guide.refresh_from_db()
        self.assertTrue(guide.live)
        self.assertFalse(guide.has_unpublished_changes)

        # 验证 ExternalContentPublicationState 状态推进
        pub_state = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        self.assertEqual(pub_state.published_revision_id, guide.live_revision_id)
        self.assertEqual(pub_state.publication_generation, 1)
        self.assertIsNotNone(pub_state.published_manifest)

        # 验证前台 HTTP 访问与动态渲染
        front_resp = self.client.get(guide.url)
        self.assertEqual(front_resp.status_code, 200)
        front_html = front_resp.content.decode("utf-8")
        self.assertIn("微服务网关选型指南", front_html)
        self.assertIn("性能压测与内存占用评估", front_html)

    def test_05_http_revisions_view_and_revert(self) -> None:
        """
        测试 5：通过 HTTP 访问历史版本查看与版本恢复。

        业务不变量断言：
        - GET revisions_view 返回 200 并在内容中正确呈现该版本对应的历史 Mongo 正文；
        - POST revisions_revert 恢复历史版本，返回 302 重定向至编辑页；
        - 页面最新草稿内容恢复为历史版本快照，但 live 状态不被篡改。
        """
        # 步骤 1：创建版本 1（已发布）
        guide = GuidePage(
            title="数据库分库分表实践",
            slug="db-sharding-guide",
            summary="版本 1 摘要",
            topic=self.topic,
            difficulty="intermediate",
            body=[
                {"type": "lead", "value": "<p>版本 1 导读内容</p>"},
                {
                    "type": "step",
                    "value": {
                        "title": "版本 1 第一步：基因法路由",
                        "description": "<p>将分片键编码嵌入订单号后四位。</p>",
                        "image": None,
                    },
                },
            ],
        )
        self.index_page.add_child(instance=guide)
        rev1 = guide.save_revision()
        guide.publish(rev1)

        # 步骤 2：通过 HTTP 编辑保存版本 2 草稿
        edit_url = reverse("wagtailadmin_pages:edit", args=[guide.pk])
        v2_data = self._build_guide_form_payload(
            title="数据库分库分表实践（草稿修订）",
            slug="db-sharding-guide",
            summary="版本 2 修订摘要",
            topic_id=self.topic.pk,
            body_blocks=[
                ("lead", "<p>版本 2 导读内容</p>"),
                (
                    "step",
                    {
                        "title": "版本 2 第二步：双写一致性校对",
                        "description": "<p>增量订阅 Binlog 并借助 Canal 进行补偿。</p>",
                        "image": "",
                    },
                ),
            ],
        )
        response_v2 = self._post_controlled(edit_url, v2_data)
        self.assertEqual(response_v2.status_code, 302)

        guide.refresh_from_db()
        rev2 = guide.get_latest_revision()
        self.assertNotEqual(rev1.pk, rev2.pk)

        # 步骤 3：GET 历史版本查看视图（revisions_view）
        view_rev1_url = reverse("wagtailadmin_pages:revisions_view", args=[guide.pk, rev1.pk])
        resp_view_rev1 = self.client.get(view_rev1_url)
        self.assertEqual(resp_view_rev1.status_code, 200)
        html_v1 = resp_view_rev1.content.decode("utf-8")
        self.assertIn("版本 1 第一步：基因法路由", html_v1)

        # 步骤 4：POST 恢复历史版本（revisions_revert）
        revert_url = reverse("wagtailadmin_pages:revisions_revert", args=[guide.pk, rev1.pk])
        revert_data = self._build_guide_form_payload(
            title="数据库分库分表实践",
            slug="db-sharding-guide",
            summary="版本 1 摘要",
            topic_id=self.topic.pk,
            body_blocks=[
                ("lead", "<p>版本 1 导读内容</p>"),
                (
                    "step",
                    {
                        "title": "版本 1 第一步：基因法路由",
                        "description": "<p>将分片键编码嵌入订单号后四位。</p>",
                        "image": "",
                    },
                ),
            ],
        )
        resp_revert = self.client.post(revert_url, revert_data)
        self.assertEqual(resp_revert.status_code, 302)

        # 验证恢复结果：产生新的草稿 revision，其内容与 rev1 相同
        guide.refresh_from_db()
        rev_after = guide.get_latest_revision()
        self.assertNotEqual(rev_after.pk, rev2.pk)
        rev_after_content = rev_after.content if isinstance(rev_after.content, dict) else json.loads(rev_after.content)
        self.assertEqual(rev_after_content["title"], "数据库分库分表实践")
        restored = rev_after.as_object()
        self.assertIn("版本 1 第一步：基因法路由", str(restored.body))
        self.assertTrue(guide.live)
        self.assertEqual(guide.live_revision_id, rev1.pk)
        self.assertEqual(
            ExternalContentPublicationState.objects.get(page_id=guide.pk).published_revision_id,
            rev1.pk,
        )

    def test_06_http_unpublish_page_clears_formal_state_and_preserves_mongo_versions(self) -> None:
        """
        测试 6：通过 HTTP POST wagtailadmin_pages:unpublish 视图下架页面。

        业务不变量断言：
        - 下架操作返回 302 重定向；
        - 页面 live 变为 False；
        - 正式 Revision 与 Manifest 清空，publication_generation 代次前进；
        - 前台 client.get(url) 返回 404；
        - MongoDB 中已写入的历史版本保持完整留存，绝不发生物理删除或版本悬空。
        """
        guide = GuidePage(
            title="混沌工程演练指南",
            slug="chaos-engineering-guide",
            summary="基于 Chaos Mesh 进行故障注入",
            topic=self.topic,
            difficulty="advanced",
            body=[
                {"type": "lead", "value": "<p>混沌工程导读</p>"},
                {
                    "type": "step",
                    "value": {
                        "title": "注入网络延迟",
                        "description": "<p>在 Pod 网卡上模拟 200ms 的随机丢包与延迟。</p>",
                        "image": None,
                    },
                },
            ],
        )
        self.index_page.add_child(instance=guide)
        rev = guide.save_revision()
        guide.publish(rev)
        guide_url = guide.url

        # 记录下架前的 Mongo 版本号
        pub_state_before = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        v_id_before = pub_state_before.published_manifest["fields"]["body"]["version_id"]
        self.assertIsNotNone(self.collection.find_one({"version_id": v_id_before}))

        unpublish_url = reverse("wagtailadmin_pages:unpublish", args=[guide.pk])
        resp_unpub = self._post_controlled(unpublish_url)
        self.assertEqual(resp_unpub.status_code, 302)

        guide.refresh_from_db()
        self.assertFalse(guide.live)

        pub_state_after = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        self.assertIsNone(pub_state_after.published_revision_id)
        self.assertIsNone(pub_state_after.published_manifest)
        self.assertGreater(
            pub_state_after.publication_generation,
            pub_state_before.publication_generation,
        )

        # 前台访问返回 404
        front_resp = self.client.get(guide_url)
        self.assertEqual(front_resp.status_code, 404)

        # 核心不变量：下架后 Mongo 不可变历史版本完好留存
        doc_still_exists = self.collection.find_one({"version_id": v_id_before})
        self.assertIsNotNone(doc_still_exists)
        self.assertEqual(doc_still_exists["page_id"], guide.pk)

    def test_07_http_copy_page_and_alias_conversion(self) -> None:
        """
        测试 7：通过 HTTP 页面复制与别名转换流程。

        业务不变量断言：
        - POST copy 视图成功创建独立复制副本；
        - 复制副本在 Mongo 中拥有独立或复用合规版本的 Manifest；
        - 页面创建别名时，通过 convert_alias POST 请求转为常规普通页面，解除 alias_of 关联并固化新状态。
        """
        guide = GuidePage(
            title="Prometheus 监控告警最佳实践",
            slug="prometheus-alerting-guide",
            summary="告警收敛与抑制规则编排",
            topic=self.topic,
            difficulty="intermediate",
            body=[
                {"type": "lead", "value": "<p>监控系统告警导读</p>"},
                {
                    "type": "step",
                    "value": {
                        "title": "编写 Alertmanager 路由规则",
                        "description": "<p>按 service 和 severity 维度聚合发送。</p>",
                        "image": None,
                    },
                },
            ],
        )
        self.index_page.add_child(instance=guide)
        rev = guide.save_revision()
        guide.publish(rev)

        copy_url = reverse("wagtailadmin_pages:copy", args=[guide.pk])

        # 1. 发起常规页面复制（非别名）
        copy_data = {
            "new_title": "Prometheus 告警进阶（副本）",
            "new_slug": "prometheus-alerting-copy",
            "new_parent_page": str(self.index_page.pk),
            "copy_subpages": False,
            "publish_copies": False,
            "alias": False,
        }
        resp_copy = self._post_controlled(copy_url, copy_data)
        self.assertEqual(resp_copy.status_code, 302)

        copied_page = GuidePage.objects.get(slug="prometheus-alerting-copy")
        self.assertEqual(copied_page.title, "Prometheus 告警进阶（副本）")
        self.assertFalse(copied_page.live)
        self.assertIsNone(copied_page.alias_of)

        # 2. 发起别名复制
        alias_data = {
            "new_title": "Prometheus 告警快捷别名",
            "new_slug": "prometheus-alerting-alias",
            "new_parent_page": str(self.index_page.pk),
            "copy_subpages": False,
            "publish_copies": False,
            "alias": True,
        }
        resp_alias = self._post_controlled(copy_url, alias_data)
        self.assertEqual(resp_alias.status_code, 302)

        alias_page = GuidePage.objects.get(slug="prometheus-alerting-alias")
        self.assertEqual(alias_page.alias_of_id, guide.pk)

        # 3. 通过 POST convert_alias 视图转换为常规页面
        convert_url = reverse("wagtailadmin_pages:convert_alias", args=[alias_page.pk])
        resp_convert = self._post_controlled(convert_url)
        self.assertEqual(resp_convert.status_code, 302)

        alias_page.refresh_from_db()
        self.assertIsNone(alias_page.alias_of)
        self.assertTrue(alias_page.get_latest_revision() is not None)

    def test_08_http_delete_records_intent_and_cancel_intent_via_http(self) -> None:
        """
        测试 8：后台删除意图协议与通过 HTTP 受控取消删除意图。

        业务不变量断言：
        - 预置 PENDING 意图后，通过 GET 与 POST cancel_deletion_intent 视图成功取消意图；
        - 取消后意图状态变迁为 CANCELLED，页面写锁解除；
        - 执行真实后台 POST delete 页面时，触发受控删除协议，持久化 MYSQL_DELETED 意图；
        - MongoDB 不可变历史版本在安全隔离期内完整存留，杜绝物理双删。
        """
        guide = GuidePage(
            title="待废弃的实验性指南",
            slug="deprecated-experimental-guide",
            summary="即将归档的过渡方案",
            topic=self.topic,
            difficulty="beginner",
            body=[
                {"type": "lead", "value": "<p>过渡导读</p>"},
                {
                    "type": "step",
                    "value": {
                        "title": "清理临时容器",
                        "description": "<p>安全移除已停止的容器与卷挂载。</p>",
                        "image": None,
                    },
                },
            ],
        )
        self.index_page.add_child(instance=guide)
        rev = guide.save_revision()
        guide.publish(rev)
        guide_pk = guide.pk

        pub_state = ExternalContentPublicationState.objects.get(page_id=guide_pk)
        v_id = pub_state.published_manifest["fields"]["body"]["version_id"]

        # 步骤 1：构造一个 PENDING 意图模拟冻结，测试 HTTP cancel 入口
        test_req_id = str(uuid.uuid4())
        intent = record_deletion_intent(
            guide,
            actor_id=str(self.user.pk),
            request_id=test_req_id,
        )

        cancel_url = reverse("external_content:cancel_deletion_intent")

        # GET 访问取消确认页面
        get_cancel_resp = self.client.get(f"{cancel_url}?page_id={guide_pk}&intent_id={intent.pk}")
        self.assertEqual(get_cancel_resp.status_code, 200)
        self.assertIn("取消删除意图", get_cancel_resp.content.decode("utf-8"))

        # POST 提交取消操作
        post_cancel_resp = self.client.post(
            cancel_url,
            {
                "page_id": str(guide_pk),
                "intent_id": str(intent.pk),
                "request_id": test_req_id,
            },
        )
        self.assertEqual(post_cancel_resp.status_code, 302)

        # 验证意图状态变迁为 CANCELLED
        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.CANCELLED)

        # 步骤 2：通过 Wagtail 原生 POST delete 视图触发页面删除
        delete_url = reverse("wagtailadmin_pages:delete", args=[guide_pk])
        resp_del = self._post_controlled(delete_url)
        self.assertEqual(resp_del.status_code, 302)

        # 验证 MySQL 中页面已被安全移除
        self.assertFalse(GuidePage.objects.filter(pk=guide_pk).exists())

        # 验证已创建新的持久化删除意图，状态为 MYSQL_DELETED
        new_intent = ExternalContentDeletionIntent.objects.filter(
            page_id=guide_pk,
            status=DeletionIntentStatus.MYSQL_DELETED,
        ).first()
        self.assertIsNotNone(new_intent)

        # 验证 Mongo 中正文版本在隔离期绝对存留
        mongo_doc = self.collection.find_one({"version_id": v_id})
        self.assertIsNotNone(mongo_doc)
        self.assertEqual(mongo_doc["page_id"], guide_pk)

    def _create_boundary_guide(self) -> GuidePage:
        """只为边界用例准备合法已发布页面；被测操作仍走真实 HTTP 入口。"""
        guide = GuidePage(
            title="HTTP 安全边界指南",
            slug=f"http-boundary-{uuid.uuid4().hex[:12]}",
            summary="权限、删除与缺正文响应的合成测试",
            topic=self.topic,
            locale=self.locale,
            difficulty="beginner",
            live=False,
            body=[
                {"type": "lead", "value": "<p>安全边界导读</p>"},
                {
                    "type": "step",
                    "value": {
                        "title": "验证操作边界",
                        "description": "<p>仅操作专属合成页面。</p>",
                        "image": None,
                    },
                },
            ],
        )
        self.index_page.add_child(instance=guide)
        guide.save_revision().publish()
        guide.refresh_from_db()
        return guide

    def _bulk_url(self, action: str, pages: List[Page]) -> str:
        """构造与后台批量操作一致的页面选择参数。"""
        endpoint = reverse("wagtail_bulk_action", args=["wagtailcore", "page", action])
        query = "&".join(f"id={page.pk}" for page in pages)
        return f"{endpoint}?{query}&next=/admin/pages/{self.index_page.pk}/"

    def test_anonymous_cannot_cancel_deletion_intent(self) -> None:
        """未登录访问不得改变删除意图或正式指针。"""
        guide = self._create_boundary_guide()
        intent = record_deletion_intent(guide)
        client = Client()
        response = client.post(
            reverse("external_content:cancel_deletion_intent"),
            {"page_id": guide.pk, "intent_id": intent.pk, "request_id": uuid.uuid4()},
        )
        self.assertEqual(response.status_code, 302)
        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.PENDING)
        self.assertEqual(
            ExternalContentPublicationState.objects.get(page_id=guide.pk).published_revision_id,
            guide.live_revision_id,
        )

    def test_editor_without_special_permission_cannot_cancel(self) -> None:
        """已有页面编辑权限也不能绕过取消意图的专门授权。"""
        guide = self._create_boundary_guide()
        intent = record_deletion_intent(guide)
        editor = User.objects.create_user(
            username=f"http_editor_{uuid.uuid4().hex[:8]}",
            password=secrets.token_urlsafe(32),
        )
        group = Group.objects.create(name=f"http_editors_{uuid.uuid4().hex[:8]}")
        group.permissions.add(Permission.objects.get(
            content_type__app_label="wagtailadmin", codename="access_admin",
        ))
        GroupPagePermission.objects.create(
            group=group,
            page=self.index_page,
            permission=Permission.objects.get(
                content_type__app_label="wagtailcore", codename="change_page",
            ),
        )
        editor.groups.add(group)
        self.assertTrue(guide.permissions_for_user(editor).can_edit())
        self.client.force_login(editor)
        response = self.client.post(
            reverse("external_content:cancel_deletion_intent"),
            {"page_id": guide.pk, "intent_id": intent.pk, "request_id": uuid.uuid4()},
        )
        self.assertEqual(response.status_code, 403)
        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.PENDING)

    def test_cancel_requires_csrf_and_valid_request_id(self) -> None:
        """登录态不能代替 CSRF；缺失幂等编号也不得解除冻结。"""
        guide = self._create_boundary_guide()
        intent = record_deletion_intent(guide)
        endpoint = reverse("external_content:cancel_deletion_intent")
        data = {"page_id": guide.pk, "intent_id": intent.pk}
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        response = csrf_client.post(endpoint, {**data, "request_id": uuid.uuid4()})
        self.assertEqual(response.status_code, 403)
        for request_id in ("", "x" * 129):
            with self.subTest(request_id_length=len(request_id)):
                response = self.client.post(endpoint, {**data, "request_id": request_id})
                self.assertEqual(response.status_code, 302)
                intent.refresh_from_db()
                self.assertEqual(intent.status, DeletionIntentStatus.PENDING)

    def test_cancel_retry_is_idempotent_and_preserves_body(self) -> None:
        """重复 HTTP 取消不能新增意图、推进发布代次或删除正文。"""
        guide = self._create_boundary_guide()
        intent = record_deletion_intent(guide)
        before_state = ExternalContentPublicationState.objects.filter(
            page_id=guide.pk,
        ).values().get()
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        data = {"page_id": guide.pk, "intent_id": intent.pk, "request_id": uuid.uuid4()}
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                response = self.client.post(
                    reverse("external_content:cancel_deletion_intent"), data,
                )
                self.assertEqual(response.status_code, 302)
        intent.refresh_from_db()
        self.assertEqual(intent.status, DeletionIntentStatus.CANCELLED)
        self.assertEqual(ExternalContentDeletionIntent.objects.filter(page_id=guide.pk).count(), 1)
        self.assertEqual(
            ExternalContentPublicationState.objects.filter(page_id=guide.pk).values().get(),
            before_state,
        )
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)

    def test_bulk_delete_resumes_pending_intent_without_self_blocking(self) -> None:
        """真实批量删除必须复用合法 PENDING 意图，不能被自己的下架冻结阻断。"""
        guides = [self._create_boundary_guide(), self._create_boundary_guide()]
        page_ids = [guide.pk for guide in guides]
        pending = record_deletion_intent(guides[0])
        before_docs = list(self.collection.find({"page_id": {"$in": page_ids}}))
        response = self.client.post(self._bulk_url("delete", guides), {})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Page.objects.filter(pk__in=page_ids).exists())
        pending.refresh_from_db()
        self.assertEqual(pending.status, DeletionIntentStatus.MYSQL_DELETED)
        self.assertEqual(
            ExternalContentDeletionIntent.objects.filter(
                page_id__in=page_ids, status=DeletionIntentStatus.MYSQL_DELETED,
            ).count(),
            2,
        )
        self.assertEqual(
            list(self.collection.find({"page_id": {"$in": page_ids}})), before_docs,
        )

    def test_bulk_recursive_delete_is_blocked_without_side_effects(self) -> None:
        """选中包含外置后代的索引页时，整棵子树和正文版本保持不变。"""
        guide = self._create_boundary_guide()
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        for headers, expected_status in (
            ({}, 302),
            ({"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}, 403),
        ):
            with self.subTest(expected_status=expected_status):
                response = self.client.post(
                    self._bulk_url("delete", [self.index_page]), {}, **headers,
                )
                self.assertEqual(response.status_code, expected_status)
                if expected_status == 302:
                    self.assertEqual(response["Location"], "/admin/")
                self.assertTrue(Page.objects.filter(pk=self.index_page.pk).exists())
                self.assertTrue(GuidePage.objects.filter(pk=guide.pk).exists())
                self.assertFalse(
                    ExternalContentDeletionIntent.objects.filter(page_id=guide.pk).exists(),
                )
                self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)

    def test_frontend_missing_version_returns_503_without_mutation(self) -> None:
        """在合成 SQL 指针注入不存在版本，不删 Mongo；前台必须返回受控 503。"""
        guide = self._create_boundary_guide()
        guide_url = guide.url
        state = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        invalid_manifest = deepcopy(state.published_manifest)
        invalid_manifest["fields"]["body"]["version_id"] = f"missing-{uuid.uuid4().hex}"
        ExternalContentPublicationState.objects.filter(pk=state.pk).update(
            published_manifest=invalid_manifest,
        )
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        response = self.client.get(guide_url)
        self.assertEqual(response.status_code, 503)
        state.refresh_from_db()
        self.assertEqual(state.published_manifest, invalid_manifest)
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)

    def test_admin_missing_revision_body_returns_409(self) -> None:
        """合成 Revision 指针缺失必须阻断历史查看，不能回退为当前草稿。"""
        guide = self._create_boundary_guide()
        revision = guide.get_latest_revision()
        invalid_content = deepcopy(revision.content)
        invalid_content["_external_content_manifest"]["fields"]["body"]["version_id"] = (
            f"missing-{uuid.uuid4().hex}"
        )
        Revision.objects.filter(pk=revision.pk).update(content=invalid_content)
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        response = self.client.get(
            reverse("wagtailadmin_pages:revisions_view", args=[guide.pk, revision.pk]),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)

    def test_anonymous_cannot_read_draft_body(self) -> None:
        """公开 URL 不能泄漏尚未发布的草稿，读取也不能推进正式指针。"""
        guide = self._create_boundary_guide()
        response = self._post_controlled(reverse("wagtailadmin_pages:unpublish", args=[guide.pk]))
        self.assertEqual(response.status_code, 302)
        guide.refresh_from_db()
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        response = Client().get(guide.url)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("仅操作专属合成页面", response.content.decode())
        state = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        self.assertIsNone(state.published_revision_id)
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)

    def test_live_page_draft_edit_keeps_previous_public_body(self) -> None:
        """保存新草稿不能替换匿名访客所读的已发布正文与正式 manifest。"""
        guide = self._create_boundary_guide()
        publication_fields = ("published_revision_id", "published_manifest", "publication_generation")
        before_state = ExternalContentPublicationState.objects.filter(page_id=guide.pk).values(
            *publication_fields,
        ).get()
        response = self._post_controlled(
            reverse("wagtailadmin_pages:edit", args=[guide.pk]),
            self._build_guide_form_payload(
                title=guide.title, slug=guide.slug, topic_id=self.topic.pk,
                body_blocks=[
                    ("lead", "<p>draft-only-marker</p>"),
                    ("step", {"title": "新草稿步骤", "description": "<p>新草稿内容</p>", "image": ""}),
                ],
            ),
        )
        self.assertEqual(response.status_code, 302)
        guide.refresh_from_db()
        self.assertTrue(guide.live)
        self.assertTrue(guide.has_unpublished_changes)
        self.assertEqual(
            ExternalContentPublicationState.objects.filter(page_id=guide.pk).values(*publication_fields).get(),
            before_state,
        )
        public_response = Client().get(guide.url)
        self.assertEqual(public_response.status_code, 200)
        self.assertContains(public_response, "安全边界导读")
        self.assertNotContains(public_response, "draft-only-marker")

    def test_repeated_draft_save_reuses_immutable_body_version(self) -> None:
        """相同正文允许产生保存记录，但不能膨胀不可变正文版本。"""
        guide = self._create_boundary_guide()
        payload = self._build_guide_form_payload(
            title=guide.title, slug=guide.slug, topic_id=self.topic.pk,
        )
        payload["body-0-id"] = str(uuid.uuid4())
        payload["body-1-id"] = str(uuid.uuid4())
        endpoint = reverse("wagtailadmin_pages:edit", args=[guide.pk])
        self.assertEqual(self._post_controlled(endpoint, payload).status_code, 302)
        guide.refresh_from_db()
        first_version = guide.get_latest_revision().content["_external_content_manifest"]["fields"]["body"]["version_id"]
        version_count = self.collection.count_documents({"page_id": guide.pk})
        self.assertEqual(self._post_controlled(endpoint, payload).status_code, 302)
        guide.refresh_from_db()
        self.assertEqual(
            guide.get_latest_revision().content["_external_content_manifest"]["fields"]["body"]["version_id"],
            first_version,
        )
        self.assertEqual(self.collection.count_documents({"page_id": guide.pk}), version_count)

    def test_edit_without_csrf_cannot_write_revision_or_body(self) -> None:
        """真实 CSRF 检查失败必须先于生命周期写入，超级用户也不能绕过。"""
        guide = self._create_boundary_guide()
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        revision_ids = list(guide.revisions.values_list("pk", flat=True))
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        response = csrf_client.post(
            reverse("wagtailadmin_pages:edit", args=[guide.pk]),
            self._build_guide_form_payload(title=guide.title, slug=guide.slug, topic_id=self.topic.pk),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(list(guide.revisions.values_list("pk", flat=True)), revision_ids)
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)

    def test_admin_access_without_page_permission_cannot_write(self) -> None:
        """后台准入不等于页面写权限；发布、删除、还原都不得绕过页面授权。"""
        guide = self._create_boundary_guide()
        revision = guide.get_latest_revision()
        viewer = User.objects.create_user(
            username=f"http_viewer_{uuid.uuid4().hex[:8]}",
            password=secrets.token_urlsafe(32),
        )
        viewer.user_permissions.add(Permission.objects.get(
            content_type__app_label="wagtailadmin", codename="access_admin",
        ))
        self.client.force_login(viewer)
        before_state = ExternalContentPublicationState.objects.filter(page_id=guide.pk).values().get()
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        revision_ids = list(guide.revisions.values_list("pk", flat=True))
        for endpoint, data in (
            (
                reverse("wagtailadmin_pages:edit", args=[guide.pk]),
                {**self._build_guide_form_payload(title=guide.title, slug=guide.slug, topic_id=self.topic.pk),
                 "action-publish": "1"},
            ),
            (reverse("wagtailadmin_pages:delete", args=[guide.pk]), {}),
            (reverse("wagtailadmin_pages:revisions_revert", args=[guide.pk, revision.pk]), {}),
        ):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(endpoint, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                self.assertEqual(response.status_code, 403)
                self.assertTrue(GuidePage.objects.filter(pk=guide.pk).exists())
                self.assertEqual(list(guide.revisions.values_list("pk", flat=True)), revision_ids)
                self.assertEqual(
                    ExternalContentPublicationState.objects.filter(page_id=guide.pk).values().get(),
                    before_state,
                )
                self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)

    def test_index_creation_and_publication_do_not_write_external_body(self) -> None:
        """索引页走原生后台创建发布，不产生正文版本或外置正式指针。"""
        before_docs = list(self.collection.find({}))
        response = self.client.post(
            reverse("wagtailadmin_pages:add", args=["guides", "guideindexpage", self.home.pk]),
            {
                "title": "HTTP 新专题入口",
                "slug": "http-created-index",
                "intro": "仅操作合成索引页",
                "action-publish": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        index_page = GuideIndexPage.objects.get(slug="http-created-index")
        self.assertTrue(index_page.live)
        self.assertEqual(index_page.get_parent().pk, self.home.pk)
        self.assertFalse(
            ExternalContentPublicationState.objects.filter(page_id=index_page.pk).exists(),
        )
        self.assertEqual(list(self.collection.find({})), before_docs)
        self.assertEqual(Client().get(index_page.url).status_code, 200)

    def test_move_between_guide_indexes_preserves_body_and_publication(self) -> None:
        """移动只改变页面树与 URL，不重新写入不可变正文或推进发布代次。"""
        guide = self._create_boundary_guide()
        destination = GuideIndexPage(
            title="HTTP 移动目标",
            slug="http-move-destination",
            locale=self.locale,
            live=False,
        )
        self.home.add_child(instance=destination)
        destination.save_revision().publish()
        before_state = ExternalContentPublicationState.objects.filter(page_id=guide.pk).values().get()
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        old_url = guide.url
        response = self.client.post(
            reverse("wagtailadmin_pages:move_confirm", args=[guide.pk, destination.pk]),
        )
        self.assertEqual(response.status_code, 302)
        guide.refresh_from_db()
        self.assertEqual(guide.get_parent().pk, destination.pk)
        self.assertNotEqual(guide.url, old_url)
        self.assertEqual(
            ExternalContentPublicationState.objects.filter(page_id=guide.pk).values().get(),
            before_state,
        )
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)
        public_response = Client().get(guide.url)
        self.assertContains(public_response, "安全边界导读")

    def test_move_to_invalid_parent_has_no_lifecycle_side_effects(self) -> None:
        """非法父页面必须拒绝移动，页面路径、指针、历史正文均保持不变。"""
        guide = self._create_boundary_guide()
        before_path = guide.path
        before_state = ExternalContentPublicationState.objects.filter(page_id=guide.pk).values().get()
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        response = self.client.post(
            reverse("wagtailadmin_pages:move_confirm", args=[guide.pk, self.home.pk]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 403)
        guide.refresh_from_db()
        self.assertEqual(guide.path, before_path)
        self.assertEqual(guide.get_parent().pk, self.index_page.pk)
        self.assertEqual(
            ExternalContentPublicationState.objects.filter(page_id=guide.pk).values().get(),
            before_state,
        )
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)

    def test_live_copy_keeps_published_body_separate_from_latest_draft(self) -> None:
        """带发布复制不能把源最新草稿冒充正式正文，也不能改动源页面。"""
        guide = self._create_boundary_guide()
        draft_response = self._post_controlled(
            reverse("wagtailadmin_pages:edit", args=[guide.pk]),
            self._build_guide_form_payload(
                title=guide.title,
                slug=guide.slug,
                topic_id=self.topic.pk,
                body_blocks=[
                    ("lead", "<p>copy-draft-only-marker</p>"),
                    ("step", {"title": "副本草稿步骤", "description": "<p>尚未发布</p>", "image": ""}),
                ],
            ),
        )
        self.assertEqual(draft_response.status_code, 302)
        guide.refresh_from_db()
        before_state = ExternalContentPublicationState.objects.filter(page_id=guide.pk).values().get()
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        response = self._post_controlled(
            reverse("wagtailadmin_pages:copy", args=[guide.pk]),
            {
                "new_title": "正式与草稿分离的 HTTP 副本",
                "new_slug": "http-live-draft-copy",
                "new_parent_page": str(self.index_page.pk),
                "publish_copies": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        copied_page = GuidePage.objects.get(slug="http-live-draft-copy")
        self.assertTrue(copied_page.live)
        self.assertIsNone(copied_page.alias_of_id)
        copied_state = ExternalContentPublicationState.objects.get(page_id=copied_page.pk)
        self.assertEqual(copied_state.published_revision_id, copied_page.live_revision_id)
        self.assertNotEqual(copied_page.live_revision_id, guide.live_revision_id)
        public_response = Client().get(copied_page.url)
        self.assertContains(public_response, "安全边界导读")
        self.assertNotContains(public_response, "copy-draft-only-marker")
        copied_draft = copied_page.get_latest_revision_as_object()
        self.assertIn("copy-draft-only-marker", str(copied_draft.body))
        self.assertEqual(
            ExternalContentPublicationState.objects.filter(page_id=guide.pk).values().get(),
            before_state,
        )
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)

    def _configure_http_workflow(self) -> tuple[Any, Any]:
        """夹具授予编辑者改稿权、审核者审批权，避免超级用户掩盖权限问题。"""
        editor_group = Group.objects.create(name=f"http_workflow_editors_{uuid.uuid4().hex[:8]}")
        reviewer_group = Group.objects.create(name=f"http_workflow_reviewers_{uuid.uuid4().hex[:8]}")
        admin_permission = Permission.objects.get(
            content_type__app_label="wagtailadmin", codename="access_admin",
        )
        for group in (editor_group, reviewer_group):
            group.permissions.add(admin_permission)
        for codename in ("add_page", "change_page"):
            GroupPagePermission.objects.create(
                group=editor_group,
                page=self.index_page,
                permission=Permission.objects.get(
                    content_type__app_label="wagtailcore", codename=codename,
                ),
            )
        editor = User.objects.create_user(
            username=f"http_workflow_editor_{uuid.uuid4().hex[:8]}",
            password=secrets.token_urlsafe(32),
        )
        reviewer = User.objects.create_user(
            username=f"http_workflow_reviewer_{uuid.uuid4().hex[:8]}",
            password=secrets.token_urlsafe(32),
        )
        editor.groups.add(editor_group)
        reviewer.groups.add(reviewer_group)
        task = GroupApprovalTask.objects.create(
            name="HTTP 专属审核任务",
            content_type=ContentType.objects.get_for_model(
                GroupApprovalTask, for_concrete_model=False,
            ),
        )
        task.groups.add(reviewer_group)
        workflow = Workflow.objects.create(name="HTTP 专属指南工作流")
        WorkflowTask.objects.create(workflow=workflow, task=task, sort_order=0)
        WorkflowPage.objects.create(page=self.index_page, workflow=workflow)
        return editor, reviewer

    def _http_workflow_payload(self, guide: GuidePage, marker: str) -> Dict[str, Any]:
        """审批被测操作仍经真实表单；使用独立标记验证草稿不会提前公开。"""
        return self._build_guide_form_payload(
            title=guide.title,
            slug=guide.slug,
            topic_id=self.topic.pk,
            body_blocks=[
                ("lead", f"<p>{marker}</p>"),
                ("step", {"title": "审核步骤", "description": "<p>审核后才能公开</p>", "image": ""}),
            ],
            extra_fields={"action-submit": "submit"},
        )

    def _formal_snapshot(self, guide: GuidePage) -> Dict[str, Any]:
        """正式契约不包含会随提审正常更新的草稿与审批指针。"""
        return ExternalContentPublicationState.objects.filter(page_id=guide.pk).values(
            "published_revision_id", "published_manifest", "publication_generation",
        ).get()

    def test_http_editor_submits_reviewer_approves_without_early_publication(self) -> None:
        """提交审核不动正式版本；真实审核角色批准后才同事务切换 Page 与 State。"""
        guide = self._create_boundary_guide()
        editor, reviewer = self._configure_http_workflow()
        old_state = self._formal_snapshot(guide)
        self.assertFalse(guide.permissions_for_user(editor).can_publish())
        self.client.force_login(editor)
        response = self.client.post(
            reverse("wagtailadmin_pages:edit", args=[guide.pk]),
            self._http_workflow_payload(guide, "http-approved-new-body"),
        )
        self.assertEqual(response.status_code, 302)
        guide = GuidePage.objects.get(pk=guide.pk)
        workflow_state = guide.current_workflow_state
        self.assertIsNotNone(workflow_state)
        self.assertEqual(workflow_state.status, WorkflowState.STATUS_IN_PROGRESS)
        self.assertEqual(
            self._formal_snapshot(guide),
            old_state,
        )
        self.assertContains(Client().get(guide.url), "安全边界导读")
        self.assertNotContains(Client().get(guide.url), "http-approved-new-body")
        task_state = workflow_state.current_task_state
        self.client.force_login(reviewer)
        self.assertEqual(
            self.client.get(reverse("wagtailadmin_pages:edit", args=[guide.pk])).status_code,
            200,
        )
        approval_response = self.client.post(
            reverse("wagtailadmin_pages:workflow_action", args=[guide.pk, "approve", task_state.pk]),
            {"comment": "合成案例审核通过"},
        )
        self.assertEqual(approval_response.status_code, 302)
        task_state.refresh_from_db()
        self.assertEqual(
            task_state.status,
            "approved",
            {
                "redirect": approval_response.headers.get("Location"),
                "messages": [str(message) for message in get_messages(approval_response.wsgi_request)],
                "workflow_in_progress": Page.objects.get(pk=guide.pk).workflow_in_progress,
            },
        )
        workflow_state.refresh_from_db()
        self.assertEqual(
            workflow_state.status,
            WorkflowState.STATUS_APPROVED,
            {
                "current_task_id": workflow_state.current_task_state_id,
                "approved_task_id": task_state.pk,
                "approved_revision_id": task_state.revision_id,
                "latest_revision_id": Page.objects.get(pk=guide.pk).latest_revision_id,
                "tasks": list(workflow_state.task_states.values("pk", "task_id", "status", "revision_id")),
            },
        )
        guide.refresh_from_db()
        state = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        self.assertEqual(state.published_revision_id, guide.live_revision_id)
        self.assertEqual(state.published_revision_id, task_state.revision_id)
        self.assertGreater(state.publication_generation, old_state["publication_generation"])
        self.assertContains(Client().get(guide.url), "http-approved-new-body")
        version_id = state.published_manifest["fields"]["body"]["version_id"]
        self.assertIsNotNone(self.collection.find_one({"version_id": version_id, "page_id": guide.pk}))
        old_version_id = old_state["published_manifest"]["fields"]["body"]["version_id"]
        self.assertIsNotNone(self.collection.find_one({"version_id": old_version_id}))

    def test_http_reject_resubmit_and_editor_cannot_self_approve(self) -> None:
        """编辑者不能自审；退回、修订、重提审不会提前发布或丢失上一正式正文。"""
        guide = self._create_boundary_guide()
        editor, reviewer = self._configure_http_workflow()
        before_state = self._formal_snapshot(guide)
        self.client.force_login(editor)
        edit_url = reverse("wagtailadmin_pages:edit", args=[guide.pk])
        self.assertEqual(
            self.client.post(edit_url, self._http_workflow_payload(guide, "http-rejected-body")).status_code,
            302,
        )
        guide = GuidePage.objects.get(pk=guide.pk)
        workflow_state = guide.current_workflow_state
        task_state = workflow_state.current_task_state
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        forbidden_response = self.client.post(
            reverse("wagtailadmin_pages:workflow_action", args=[guide.pk, "approve", task_state.pk]),
            {"comment": "不可自审"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(forbidden_response.status_code, 403)
        workflow_state.refresh_from_db()
        self.assertEqual(workflow_state.status, WorkflowState.STATUS_IN_PROGRESS)
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)
        self.client.force_login(reviewer)
        rejected_response = self.client.post(
            reverse("wagtailadmin_pages:workflow_action", args=[guide.pk, "reject", task_state.pk]),
            {"comment": "请完善合成案例"},
        )
        self.assertEqual(rejected_response.status_code, 302)
        workflow_state.refresh_from_db()
        self.assertEqual(workflow_state.status, WorkflowState.STATUS_NEEDS_CHANGES)
        self.assertEqual(
            self._formal_snapshot(guide),
            before_state,
        )
        self.client.force_login(editor)
        self.assertEqual(
            self.client.post(edit_url, self._http_workflow_payload(guide, "http-resubmitted-body")).status_code,
            302,
        )
        guide = GuidePage.objects.get(pk=guide.pk)
        resumed_state = guide.current_workflow_state
        self.assertEqual(resumed_state.status, WorkflowState.STATUS_IN_PROGRESS)
        self.assertEqual(
            self._formal_snapshot(guide),
            before_state,
        )
        self.client.force_login(reviewer)
        approved_response = self.client.post(
            reverse(
                "wagtailadmin_pages:workflow_action",
                args=[guide.pk, "approve", resumed_state.current_task_state_id],
            ),
            {"comment": "修订后通过"},
        )
        self.assertEqual(approved_response.status_code, 302)
        guide.refresh_from_db()
        state = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        self.assertEqual(state.published_revision_id, guide.live_revision_id)
        public_response = Client().get(guide.url)
        self.assertContains(public_response, "http-resubmitted-body")
        self.assertNotContains(public_response, "http-rejected-body")

    def test_workflow_finish_rejects_incomplete_approval(self) -> None:
        """不能借内部完成入口发布仍待审批的 Revision，即使调用人属于审核组。"""
        guide = self._create_boundary_guide()
        editor, reviewer = self._configure_http_workflow()
        before_state = self._formal_snapshot(guide)
        self.client.force_login(editor)
        response = self.client.post(
            reverse("wagtailadmin_pages:edit", args=[guide.pk]),
            self._http_workflow_payload(guide, "http-unapproved-body"),
        )
        self.assertEqual(response.status_code, 302)
        guide = GuidePage.objects.get(pk=guide.pk)
        workflow_state = guide.current_workflow_state
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        with self.assertRaises(PublicationWorkflowRevisionDriftError):
            ExternalContentPublicationService.finish_workflow_action(workflow_state, user=reviewer)
        self.assertEqual(self._formal_snapshot(guide), before_state)
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)
        self.assertNotContains(Client().get(guide.url), "http-unapproved-body")

    def test_reviewer_cannot_directly_publish_outside_workflow(self) -> None:
        """审批权不升级为通用发布权；原生直接发布仍拒绝审核组角色。"""
        from wagtail.actions.publish_page_revision import PublishPageRevisionAction

        guide = self._create_boundary_guide()
        editor, reviewer = self._configure_http_workflow()
        before_state = self._formal_snapshot(guide)
        before_docs = list(self.collection.find({"page_id": guide.pk}))
        self.assertFalse(guide.permissions_for_user(reviewer).can_publish())
        with self.assertRaises(PermissionDenied):
            PublishPageRevisionAction(guide.get_latest_revision(), user=reviewer).execute()
        self.assertEqual(self._formal_snapshot(guide), before_state)
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), before_docs)

    def test_http_scheduled_publication_and_expiry_keep_immutable_versions(self) -> None:
        """后台预约不提前公开；真实调度命令到期发布、到期下架且保留全部正文版本。"""
        guide = self._create_boundary_guide()
        before_state = self._formal_snapshot(guide)
        scheduled_time = timezone.localtime().replace(second=0, microsecond=0) + timedelta(days=1)
        expiry_time = scheduled_time + timedelta(days=1)
        payload = self._build_guide_form_payload(
            title=guide.title,
            slug=guide.slug,
            topic_id=self.topic.pk,
            body_blocks=[
                ("lead", "<p>http-scheduled-body</p>"),
                ("step", {"title": "排期步骤", "description": "<p>到期可见</p>", "image": ""}),
            ],
            extra_fields={
                "action-publish": "publish",
                "go_live_at": scheduled_time.strftime("%Y-%m-%d %H:%M"),
                "expire_at": expiry_time.strftime("%Y-%m-%d %H:%M"),
            },
        )
        response = self._post_controlled(reverse("wagtailadmin_pages:edit", args=[guide.pk]), payload)
        self.assertEqual(response.status_code, 302)
        guide.refresh_from_db()
        revision = guide.get_latest_revision()
        self.assertEqual(revision.approved_go_live_at, scheduled_time)
        self.assertEqual(
            self._formal_snapshot(guide),
            before_state,
        )
        self.assertContains(Client().get(guide.url), "安全边界导读")
        self.assertNotContains(Client().get(guide.url), "http-scheduled-body")
        call_command("publish_scheduled", stdout=io.StringIO())
        guide.refresh_from_db()
        self.assertEqual(guide.live_revision_id, before_state["published_revision_id"])
        with patch("django.utils.timezone.now", return_value=scheduled_time + timedelta(minutes=1)):
            call_command("publish_scheduled", stdout=io.StringIO())
        guide.refresh_from_db()
        state = ExternalContentPublicationState.objects.get(page_id=guide.pk)
        self.assertTrue(guide.live)
        self.assertEqual(guide.live_revision_id, revision.pk)
        self.assertEqual(state.published_revision_id, revision.pk)
        self.assertContains(Client().get(guide.url), "http-scheduled-body")
        all_docs = list(self.collection.find({"page_id": guide.pk}))
        with patch("django.utils.timezone.now", return_value=expiry_time + timedelta(minutes=1)):
            call_command("publish_scheduled", stdout=io.StringIO())
        guide.refresh_from_db()
        state.refresh_from_db()
        self.assertFalse(guide.live)
        self.assertTrue(guide.expired)
        self.assertIsNone(state.published_revision_id)
        self.assertEqual(Client().get(guide.url).status_code, 404)
        self.assertEqual(list(self.collection.find({"page_id": guide.pk})), all_docs)

    def _create_richtext_roundtrip_guide(self) -> GuidePage:
        """F2 合成夹具保留旧 HTML 空白与块 ID，不依赖现场页面或正文。"""
        guide = GuidePage(
            title="富文本往返合成指南",
            slug=f"http-richtext-{uuid.uuid4().hex[:12]}",
            summary="原摘要",
            topic=self.topic,
            locale=self.locale,
            difficulty="beginner",
            live=False,
            body=[
                {
                    "type": "lead", "id": str(uuid.uuid4()),
                    "value": "<p> 导读 <b>重点</b> </p>",
                },
                {
                    "type": "step", "id": str(uuid.uuid4()),
                    "value": {
                        "title": "核验步骤",
                        "description": '<p> 步骤 <a href="https://example.org/old">资料</a> </p>',
                        "image": None,
                    },
                },
            ],
        )
        self.index_page.add_child(instance=guide)
        guide.save_revision().publish()
        guide.refresh_from_db()
        return guide

    def _richtext_roundtrip_payload(
        self, guide: GuidePage, body: List[Dict[str, Any]],
        *, summary: str, topic_id: int, publish: bool = False,
    ) -> Dict[str, Any]:
        """使用编辑页相同的 widget/features 序列化；保留原生表单块身份。"""
        payload = self._build_guide_form_payload(
            title=guide.title,
            slug=guide.slug,
            summary=summary,
            topic_id=topic_id,
            difficulty=guide.difficulty,
            body_blocks=[
                ("lead", body[0]["value"]),
                ("step", {**body[1]["value"], "image": ""}),
            ],
            extra_fields={"action-publish": "publish"} if publish else None,
        )
        stream = GuidePage._meta.get_field("body").stream_block
        payload["body-0-value"] = stream.child_blocks["lead"].field.widget.format_value(
            body[0]["value"],
        )
        payload["body-1-value-description"] = (
            stream.child_blocks["step"].child_blocks["description"].field.widget.format_value(
                body[1]["value"]["description"],
            )
        )
        for index, item in enumerate(body):
            payload[f"body-{index}-id"] = item["id"]
        return payload

    def test_http_metadata_only_richtext_roundtrip_reuses_exact_body_version(self) -> None:
        """摘要/主题编辑及发布均不扩增 Mongo 正文；SQL Revision 仍正常递增。"""
        alternative_topic = GuideTopic.objects.create(name="往返主题", slug="roundtrip-topic")
        for metadata, publish in [("summary", False), ("topic", False), ("summary", True)]:
            with self.subTest(metadata=metadata, publish=publish):
                guide = self._create_richtext_roundtrip_guide()
                previous_revision = guide.get_latest_revision()
                original_entry = deepcopy(
                    previous_revision.content["_external_content_manifest"]["fields"]["body"],
                )
                field = GuidePage._meta.get_field("body")
                original_body = deepcopy(field.stream_block.get_prep_value(guide.body))
                original_docs = list(self.collection.find({"page_id": guide.pk}))
                formal_before = self._formal_snapshot(guide)
                summary = "仅更新摘要" if metadata == "summary" else guide.summary
                topic_id = alternative_topic.pk if metadata == "topic" else self.topic.pk
                response = self._post_controlled(
                    reverse("wagtailadmin_pages:edit", args=[guide.pk]),
                    self._richtext_roundtrip_payload(
                        guide, original_body, summary=summary, topic_id=topic_id, publish=publish,
                    ),
                )
                self.assertEqual(response.status_code, 302)
                guide.refresh_from_db()
                revision = guide.get_latest_revision()
                self.assertNotEqual(revision.pk, previous_revision.pk)
                self.assertEqual(
                    revision.content["_external_content_manifest"]["fields"]["body"], original_entry,
                )
                self.assertEqual(list(self.collection.find({"page_id": guide.pk})), original_docs)
                restored = revision.as_object()
                self.assertEqual(restored.summary, summary)
                self.assertEqual(restored.topic_id, topic_id)
                self.assertEqual(field.stream_block.get_prep_value(restored.body), original_body)
                state = ExternalContentPublicationState.objects.get(page_id=guide.pk)
                self.assertEqual(state.draft_revision_id, revision.pk)
                self.assertEqual(state.draft_manifest["fields"]["body"], original_entry)
                if publish:
                    self.assertEqual(guide.live_revision_id, revision.pk)
                    self.assertEqual(state.published_revision_id, revision.pk)
                    self.assertEqual(state.published_manifest["fields"]["body"], original_entry)
                else:
                    self.assertEqual(self._formal_snapshot(guide), formal_before)
                    self.assertEqual(guide.live_revision_id, previous_revision.pk)

    def test_http_real_richtext_edit_with_summary_creates_immutable_new_version(self) -> None:
        """真实导读文字/步骤链接修改不能被摘要复用逻辑误吞，也不能提前改正式正文。"""
        for changed_field in ["lead", "step.description"]:
            with self.subTest(changed_field=changed_field):
                guide = self._create_richtext_roundtrip_guide()
                previous_revision = guide.get_latest_revision()
                original_entry = deepcopy(
                    previous_revision.content["_external_content_manifest"]["fields"]["body"],
                )
                original_docs = list(self.collection.find({"page_id": guide.pk}))
                formal_before = self._formal_snapshot(guide)
                field = GuidePage._meta.get_field("body")
                submitted_body = deepcopy(field.stream_block.get_prep_value(guide.body))
                if changed_field == "lead":
                    submitted_body[0]["value"] = "<p>新增导读文字 <b>重点</b></p>"
                    changed_text = "新增导读文字"
                else:
                    submitted_body[1]["value"]["description"] = (
                        '<p>步骤 <a href="https://example.org/new">资料</a></p>'
                    )
                    changed_text = "https://example.org/new"
                response = self._post_controlled(
                    reverse("wagtailadmin_pages:edit", args=[guide.pk]),
                    self._richtext_roundtrip_payload(
                        guide, submitted_body, summary="摘要与正文一起更新", topic_id=self.topic.pk,
                    ),
                )
                self.assertEqual(response.status_code, 302)
                guide.refresh_from_db()
                revision = guide.get_latest_revision()
                new_entry = revision.content["_external_content_manifest"]["fields"]["body"]
                self.assertNotEqual(revision.pk, previous_revision.pk)
                self.assertNotEqual(new_entry["version_id"], original_entry["version_id"])
                self.assertNotEqual(new_entry["payload_sha256"], original_entry["payload_sha256"])
                docs_after = list(self.collection.find({"page_id": guide.pk}))
                self.assertEqual(len(docs_after), len(original_docs) + 1)
                for original_doc in original_docs:
                    self.assertEqual(
                        self.collection.find_one({"_id": original_doc["_id"]}), original_doc,
                    )
                new_doc = self.collection.find_one({
                    "page_id": guide.pk, "version_id": new_entry["version_id"],
                })
                self.assertIsNotNone(new_doc)
                self.assertEqual(new_doc["payload_sha256"], new_entry["payload_sha256"])
                restored = revision.as_object()
                self.assertEqual(restored.summary, "摘要与正文一起更新")
                self.assertIn(changed_text, str(field.stream_block.get_prep_value(restored.body)))
                state = ExternalContentPublicationState.objects.get(page_id=guide.pk)
                self.assertEqual(state.draft_revision_id, revision.pk)
                self.assertEqual(state.draft_manifest["fields"]["body"], new_entry)
                self.assertEqual(self._formal_snapshot(guide), formal_before)
                self.assertEqual(guide.live_revision_id, previous_revision.pk)
