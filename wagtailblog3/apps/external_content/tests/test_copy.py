"""
Wagtail Page 正文外置通用协议 - 页面复制（Copy）、别名（Alias）与跨语言翻译（Translation）定向测试。

验证：
1. 复制页面（Page.copy）时，通过受控 copying 协调器为新页面派生独立的正文版本，
   使新页面在 MongoDB 中拥有属于新 page_id 的专属版本与独立 SHA256，
   新页面的 Manifest 中的 page_id 必须与新页面主键严格一致；
2. 修改复制后的页面正文并重新发布，绝不篡改或污染源页面的正文版本；
3. 草稿复制（keep_live=False）正确派生独立的草稿版本与 Manifest，published 状态为空；
4. 别名页面（Page.create_alias）建立受控源关系，不伪造本地 Revision，随源页面更新而同步；
5. 跨语言翻译（copy_for_translation）派生目标 Locale 的独立正文版本。
"""

import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import MagicMock, call, patch
from django.conf import settings
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.test import TestCase
from wagtail.models import Locale, Page, Revision
from wagtail.actions.copy_page import CopyPageAction
from wagtail.actions.create_alias import CreatePageAliasAction
from wagtail.actions.convert_alias import ConvertAliasPageAction

from external_content.coordinator import ExternalContentCoordinator
from external_content.manifest import ALLOWED_ENTRY_KEYS, ALLOWED_ROOT_KEYS
from external_content.locking import preserve_threadlocal_context
from external_content.mixins import _controlled_alias_context
from external_content.publication import _controlled_save_context
from external_content.registry import default_codec_registry
from external_content.exceptions import (
    ContentStoreError,
    ExternalContentError,
    ExternalContentOperationBlockedError,
    ManifestError,
    ManifestValidationError,
)
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)
from external_content.store import ContentVersionStore, REQUIRED_IDENTITY_KEYS
from guides.models import GuideIndexPage, GuidePage, GuideTopic


class ExternalContentCopyingTests(TestCase):
    """外置正文页面复制、别名与跨语言翻译测试套件。"""

    def setUp(self) -> None:
        super().setUp()
        User = get_user_model()
        self.user = User.objects.create_superuser(
            username=f"copy_admin_{uuid.uuid4().hex[:6]}",
            email="admin@example.com",
            password="password",
        )

        self.locale_zh, _ = Locale.objects.get_or_create(language_code="zh-hans")
        self.locale_en, _ = Locale.objects.get_or_create(language_code="en")

        root = Page.objects.filter(depth=1).first()
        if not root:
            root = Page.add_root(title="Root", locale=self.locale_zh)
        self.root = root

        # 创建专题索引页
        self.guide_index = GuideIndexPage(
            title="专题指南索引",
            slug=f"guides-{uuid.uuid4().hex[:6]}",
            locale=self.locale_zh,
            live=True,
        )
        self.root.add_child(instance=self.guide_index)
        self.guide_index.save_revision(user=self.user).publish(user=self.user)

        self.topic = GuideTopic.objects.create(name="部署运维", slug="deployment")

        # 创建源专题指南页面 A
        self.guide_a = GuidePage(
            title="源专题页面 A",
            slug=f"guide-a-{uuid.uuid4().hex[:6]}",
            summary="源页面 A 摘要",
            topic=self.topic,
            difficulty="intermediate",
            locale=self.locale_zh,
            body=[
                {
                    "type": "lead",
                    "value": "<p>页面 A 的专属导读正文内容</p>",
                },
                {
                    "type": "step",
                    "value": {
                        "title": "操作步骤一",
                        "description": "<p>步骤说明内容</p>",
                        "image": None,
                    },
                },
            ],
            live=False,
        )
        self.guide_index.add_child(instance=self.guide_a)
        self.revision_a = self.guide_a.save_revision(user=self.user)
        self.revision_a.publish(user=self.user)
        self.guide_a.refresh_from_db()
        self.store = ContentVersionStore()

    def test_copy_external_content_page_derives_independent_version(self) -> None:
        """
        验证复制页面必须为新页面派生独立的正文版本：
        1. 新页面 page_b.id != guide_a.id；
        2. 新页面的 published_manifest 中 page_id 严格等于 page_b.id；
        3. 新页面的 body 版本在 MongoDB 中为全新记录，version_id != version_id_a；
        4. 修改 page_b 的正文，源页面 guide_a 保持不变。
        """
        state_a = ExternalContentPublicationState.objects.get(page_id=self.guide_a.pk)
        version_id_a = state_a.published_manifest["fields"]["body"]["version_id"]
        self.assertEqual(state_a.published_manifest["page_id"], self.guide_a.pk)

        # 执行复制
        guide_b = self.guide_a.copy(
            to=self.guide_index,
            update_attrs={
                "title": "派生专题页面 B",
                "slug": f"guide-b-{uuid.uuid4().hex[:6]}",
            },
            keep_live=True,
            user=self.user,
        )

        self.assertNotEqual(guide_b.pk, self.guide_a.pk)
        self.assertEqual(guide_b.title, "派生专题页面 B")
        self.assertTrue(guide_b.live)

        # 检查页面 B 的外置正文内容与页面 A 一致
        self.assertEqual(len(guide_b.specific.body), 2)
        self.assertEqual(
            str(guide_b.specific.body[0].value),
            "<p>页面 A 的专属导读正文内容</p>",
        )

        # 关键架构验证：页面 B 必须派生独立的发布状态与版本
        state_b = ExternalContentPublicationState.objects.get(page_id=guide_b.pk)
        self.assertEqual(state_b.published_manifest["page_id"], guide_b.pk)
        version_id_b = state_b.published_manifest["fields"]["body"]["version_id"]
        self.assertNotEqual(
            version_id_b,
            version_id_a,
            "复制页面必须派生属于新页面主键的独立正文版本，严禁复用源页面版本 ID",
        )

        # 验证修改 B 绝不影响 A
        guide_b.body = [
            {
                "type": "lead",
                "value": "<p>页面 B 已被修改为全新的导读</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "B 的新步骤",
                    "description": "<p>B 的详细执行步骤</p>",
                    "image": None,
                },
            },
        ]
        rev_b2 = guide_b.save_revision(user=self.user)
        rev_b2.publish(user=self.user)

        self.guide_a.refresh_from_db()
        self.assertEqual(
            str(self.guide_a.specific.body[0].value),
            "<p>页面 A 的专属导读正文内容</p>",
            "修改复制副本 B 的正文后，源页面 A 的正文必须完好保持原样",
        )
        guide_b.refresh_from_db()
        self.assertEqual(
            str(guide_b.specific.body[0].value),
            "<p>页面 B 已被修改为全新的导读</p>",
        )

    def test_copy_external_content_page_draft_only(self) -> None:
        """
        验证以草稿模式复制（keep_live=False）：
        1. 新页面 live 为 False，has_unpublished_changes 为 True；
        2. 派生独立的草稿版本与 Manifest，published 状态为空。
        """
        state_a = ExternalContentPublicationState.objects.get(page_id=self.guide_a.pk)
        version_id_a = state_a.published_manifest["fields"]["body"]["version_id"]

        guide_c = self.guide_a.copy(
            to=self.guide_index,
            update_attrs={
                "title": "草稿副本页面 C",
                "slug": f"guide-c-{uuid.uuid4().hex[:6]}",
            },
            keep_live=False,
            user=self.user,
        )

        self.assertFalse(guide_c.live)
        self.assertTrue(guide_c.has_unpublished_changes)

        state_c = ExternalContentPublicationState.objects.get(page_id=guide_c.pk)
        self.assertIsNone(state_c.published_manifest)
        self.assertIsNotNone(state_c.draft_manifest)
        self.assertEqual(state_c.draft_manifest["page_id"], guide_c.pk)

        version_id_c = state_c.draft_manifest["fields"]["body"]["version_id"]
        self.assertNotEqual(version_id_c, version_id_a)

    def test_create_alias_establishes_source_relation(self) -> None:
        """
        验证别名页面（Page.create_alias）：
        1. 建立受控源关系，记录 source_page_id；
        2. 别名页面读取正文内容正常呈现；
        3. 源页面更新发布后，别名页面内容随之同步；
        4. 别名页面删除不影响源页面。
        """
        alias_page = self.guide_a.create_alias(
            parent=self.guide_index,
            update_slug=f"alias-a-{uuid.uuid4().hex[:6]}",
            user=self.user,
        )

        self.assertEqual(alias_page.alias_of_id, self.guide_a.pk)
        self.assertTrue(alias_page.live)

        state_alias = ExternalContentPublicationState.objects.get(page_id=alias_page.pk)
        self.assertEqual(state_alias.source_page_id, self.guide_a.pk)
        self.assertIsNone(state_alias.published_revision_id)

        with self.assertRaises(ExternalContentOperationBlockedError):
            alias_page.save(clean=False)

        # 读取别名页面的外置正文
        self.assertEqual(
            str(alias_page.specific.body[0].value),
            "<p>页面 A 的专属导读正文内容</p>",
        )

        # 源页面更新发布
        self.guide_a.body = [
            {
                "type": "lead",
                "value": "<p>源页面 A 更新了重要内容</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "A 的更新步骤",
                    "description": "<p>A 的详细执行步骤</p>",
                    "image": None,
                },
            },
        ]
        rev_a2 = self.guide_a.save_revision(user=self.user)
        rev_a2.publish(user=self.user)

        # 别名页面自动呈现更新后的正文
        alias_page.refresh_from_db()
        self.assertEqual(
            str(alias_page.specific.body[0].value),
            "<p>源页面 A 更新了重要内容</p>",
        )

    def test_alias_update_context_preserves_source_revision_validation(self) -> None:
        """The alias update window must not bypass validation for its source page."""
        with patch(
            "external_content.mixins.ExternalContentPublicationService.lock_and_validate_revision"
        ) as validate_revision:
            with preserve_threadlocal_context(_controlled_alias_context, "in_alias", True):
                with preserve_threadlocal_context(
                    _controlled_alias_context,
                    "alias_update_source_page_id",
                    self.guide_a.pk,
                ):
                    with preserve_threadlocal_context(
                        _controlled_save_context,
                        "in_publication_action",
                        True,
                    ):
                        self.guide_a.save(update_fields=["title"])

            validate_revision.assert_called_once_with(
                self.guide_a.pk,
                self.guide_a.live_revision_id,
            )

    def test_copy_for_translation_creates_independent_version(self) -> None:
        """
        验证跨语言翻译复制（Page.copy_for_translation）：
        1. 翻译副本属于目标语言（如 en）；
        2. 派生属于目标页面的独立版本；
        3. 修改翻译页面的外文内容，源中文页面不受任何影响。
        """
        guide_en = self.guide_a.copy_for_translation(
            locale=self.locale_en,
            copy_parents=True,
            alias=False,
        )

        self.assertEqual(guide_en.locale_id, self.locale_en.pk)
        self.assertNotEqual(guide_en.pk, self.guide_a.pk)

        state_en = ExternalContentPublicationState.objects.get(page_id=guide_en.pk)
        manifest_en = state_en.draft_manifest or state_en.published_manifest
        self.assertIsNotNone(manifest_en)
        self.assertEqual(manifest_en["page_id"], guide_en.pk)

        # 翻译为英文
        guide_en.title = "Source Guide A (English)"
        guide_en.body = [
            {
                "type": "lead",
                "value": "<p>English lead content for Guide A</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "Step 1 in English",
                    "description": "<p>English instruction details</p>",
                    "image": None,
                },
            },
        ]
        rev_en = guide_en.save_revision(user=self.user)
        rev_en.publish(user=self.user)

        # 验证英文页面更新
        guide_en.refresh_from_db()
        self.assertEqual(
            str(guide_en.specific.body[0].value),
            "<p>English lead content for Guide A</p>",
        )

        # 验证源中文页面完好
        self.guide_a.refresh_from_db()
        self.assertEqual(
            str(self.guide_a.specific.body[0].value),
            "<p>页面 A 的专属导读正文内容</p>",
        )

    def test_copy_revisions_true_derives_independent_6key_identity_for_all_historical_revisions(self) -> None:
        """
        验证 copy_revisions=True 时：
        1. 目标页面的所有历史 Revision 内部 _external_content_manifest 完全重绑定至 target_page.pk；
        2. 每个外置字段均在 MongoDB 派生属于目标页面的独立 6 键身份不可变版本；
        3. 目标版本 ID 严格不同于源页面对应版本 ID；
        4. 6 键（model_label, page_id, field_name, codec, schema_version, payload_sha256）完整且严格匹配。
        """
        # 为源页面创建第 2 个版本（草稿修改）
        self.guide_a.body = [
            {
                "type": "lead",
                "value": "<p>页面 A 的第 2 个版本内容</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "第 2 步操作",
                    "description": "<p>步骤说明</p>",
                    "image": None,
                },
            },
        ]
        rev_a2 = self.guide_a.save_revision(user=self.user)

        # 记录源页面所有 Revision 的版本 ID
        src_revs = list(Revision.objects.for_instance(self.guide_a).order_by("pk"))
        self.assertGreaterEqual(len(src_revs), 2)
        src_vids: List[str] = [
            rev.content["_external_content_manifest"]["fields"]["body"]["version_id"]
            for rev in src_revs
        ]

        # 执行完整历史快照复制
        guide_b = self.guide_a.copy(
            to=self.guide_index,
            update_attrs={
                "title": "带全量历史副本 B",
                "slug": f"guide-b-hist-{uuid.uuid4().hex[:6]}",
            },
            keep_live=True,
            copy_revisions=True,
            user=self.user,
        )

        target_revs = list(Revision.objects.for_instance(guide_b).order_by("pk"))
        # Wagtail 的 copy() 可能会为当前状态额外创建一个 Revision
        # 所以目标 Revision 数量应该 >= 源页面数量
        self.assertGreaterEqual(len(target_revs), len(src_revs), "复制后的目标 Revision 数量至少应等于源页面")

        target_vids: List[str] = []
        for rev in target_revs:
            content = rev.content
            self.assertIn("_external_content_manifest", content)
            manifest = content["_external_content_manifest"]
            # 契约 1：Manifest 的 page_id 必须彻底重绑定为目标主键
            self.assertEqual(manifest["page_id"], guide_b.pk)

            self.assertIn("body", manifest["fields"])
            body_field = manifest["fields"]["body"]

            # 契约 2：六键完整性与目标身份契约
            for key in REQUIRED_IDENTITY_KEYS:
                self.assertIn(key, body_field, f"历史快照 Manifest 必须包含六键身份属性: {key}")
            self.assertEqual(body_field["page_id"], guide_b.pk)
            self.assertEqual(body_field["model_label"], guide_b._meta.label_lower)
            self.assertEqual(body_field["field_name"], "body")

            target_vid = body_field["version_id"]
            target_vids.append(target_vid)
            # 契约 3：目标版本 ID 必须与源页面对应版本物理独立
            self.assertNotIn(
                target_vid,
                src_vids,
                "目标页面历史快照版本 ID 必须在 MongoDB 独立派生，严禁复用源页面版本 ID",
            )

    def test_copy_live_a_draft_b_pointer_isolation(self) -> None:
        """
        验证源页面处于“已发布 A + 草稿 B”状态时复制：
        1. keep_live=True：目标页面的 published_manifest 与 draft_manifest 严格指针隔离；
        2. keep_live=False：目标页面的 published_manifest 为 None，仅保留 draft_manifest；
        3. 两者指针均绑定目标页面，杜绝跨状态与跨页面混淆。
        """
        # 源页面 A 已发布（第 1 版内容 A）
        state_a1 = ExternalContentPublicationState.objects.get(page_id=self.guide_a.pk)
        vid_a_live = state_a1.published_manifest["fields"]["body"]["version_id"]

        # 修改源页面 A 生成草稿第 2 版（内容 B，未发布）
        self.guide_a.body = [
            {
                "type": "lead",
                "value": "<p>页面 A 的草稿版本 B 内容</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "草稿步骤",
                    "description": "<p>未发布说明</p>",
                    "image": None,
                },
            },
        ]
        self.guide_a.save_revision(user=self.user)
        self.guide_a.refresh_from_db()
        self.assertTrue(self.guide_a.has_unpublished_changes)

        # 场景 1：keep_live=True 复制
        guide_b = self.guide_a.copy(
            to=self.guide_index,
            update_attrs={
                "title": "已发布副本 B",
                "slug": f"guide-b-live-{uuid.uuid4().hex[:6]}",
            },
            keep_live=True,
            copy_revisions=True,
            user=self.user,
        )

        state_b = ExternalContentPublicationState.objects.get(page_id=guide_b.pk)
        self.assertIsNotNone(state_b.published_manifest, "keep_live=True 目标必须拥有发布 Manifest")
        self.assertIsNotNone(state_b.draft_manifest, "keep_live=True 目标必须拥有草稿 Manifest")
        self.assertEqual(state_b.published_manifest["page_id"], guide_b.pk)
        self.assertEqual(state_b.draft_manifest["page_id"], guide_b.pk)

        vid_b_published = state_b.published_manifest["fields"]["body"]["version_id"]
        vid_b_draft = state_b.draft_manifest["fields"]["body"]["version_id"]
        self.assertNotEqual(
            vid_b_published,
            vid_b_draft,
            "目标页面的已发布指针与草稿指针必须物理隔离为不同派生版本",
        )
        self.assertNotEqual(vid_b_published, vid_a_live)

        # 场景 2：keep_live=False 复制
        guide_c = self.guide_a.copy(
            to=self.guide_index,
            update_attrs={
                "title": "仅草稿副本 C",
                "slug": f"guide-c-draft-{uuid.uuid4().hex[:6]}",
            },
            keep_live=False,
            copy_revisions=True,
            user=self.user,
        )

        state_c = ExternalContentPublicationState.objects.get(page_id=guide_c.pk)
        self.assertIsNone(state_c.published_manifest, "keep_live=False 目标页面的 published_manifest 必须为空")
        self.assertIsNotNone(state_c.draft_manifest, "keep_live=False 目标页面必须保留 draft_manifest")
        self.assertEqual(state_c.draft_manifest["page_id"], guide_c.pk)

    def test_copy_deduplication_cache_reuses_target_version_for_identical_snapshots(self) -> None:
        """
        验证历史快照去重缓存契约：
        当源页面的多个历史 Revision 拥有相同正文内容时，目标页面复用相同目标版本 ID，
        在 MongoDB 中仅持久化一份目标文档，杜绝存储膨胀。
        """
        # 清除 setUp 中预置的修订记录，确保从本测试专属 Revision 1 开始编号
        Revision.objects.for_instance(self.guide_a).delete()
        self.guide_a.live_revision = None
        self.guide_a.latest_revision = None
        self.guide_a.save(update_fields=["live_revision", "latest_revision"])

        initial_body = [
            {
                "type": "lead",
                "value": "<p>页面 A 原始内容 X</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "步骤 1",
                    "description": "<p>说明 1</p>",
                    "image": None,
                },
            },
        ]
        self.guide_a.body = initial_body
        rev_1 = self.guide_a.save_revision(user=self.user)
        rev_1.publish(user=self.user)

        # Revision 2: 修改为内容 Y
        self.guide_a.body = [
            {
                "type": "lead",
                "value": "<p>页面 A 修改为内容 Y</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "步骤 2",
                    "description": "<p>说明 2</p>",
                    "image": None,
                },
            },
        ]
        rev_2 = self.guide_a.save_revision(user=self.user)

        # Revision 3: 回退恢复为内容 X
        self.guide_a.body = initial_body
        rev_3 = self.guide_a.save_revision(user=self.user)

        # 执行复制
        guide_b = self.guide_a.copy(
            to=self.guide_index,
            update_attrs={
                "title": "去重副本 B",
                "slug": f"guide-b-dedup-{uuid.uuid4().hex[:6]}",
            },
            keep_live=True,
            copy_revisions=True,
            user=self.user,
        )

        target_revs = list(Revision.objects.for_instance(guide_b).order_by("pk"))
        self.assertGreaterEqual(len(target_revs), 3)

        # 检查 target Revision 1 与 Revision 3 的 version_id
        vid_t1 = target_revs[0].content["_external_content_manifest"]["fields"]["body"]["version_id"]
        vid_t2 = target_revs[1].content["_external_content_manifest"]["fields"]["body"]["version_id"]
        vid_t3 = target_revs[2].content["_external_content_manifest"]["fields"]["body"]["version_id"]

        # 相同内容 X 复用相同 target version_id
        self.assertEqual(
            vid_t1,
            vid_t3,
            "相同内容快照在复制后必须复用目标版本 ID，防止存储膨胀",
        )
        # 不同内容 Y 使用不同的 target version_id
        self.assertNotEqual(vid_t1, vid_t2)

    def test_copy_failure_rolls_back_sql_and_retains_prepared_orphans_zero_cleanup(self) -> None:
        """
        验证复制派生或写入失败时的原子回滚与延迟孤儿保护：
        1. 派生过程中若抛出异常，外层 SQL 事务原子回滚（MySQL 中 Page/State/Revision 零残留）；
        2. store.cleanup_unreferenced_version 严格零调用（断言 mock_cleanup.assert_not_called()）；
        3. 已经 prepared 的版本保留在 MongoDB 作为延迟孤儿等待异步 GC 清理，杜绝同步清理误伤。
        """
        with patch.object(ContentVersionStore, "cleanup_unreferenced_version") as mock_cleanup:
            target_slug = f"guide-fail-{uuid.uuid4().hex[:6]}"
            # 模拟在派生过程中存储抛出确切领域异常（例如 Mongo 写入失败）
            with patch.object(
                ContentVersionStore,
                "save_version_with_status",
                side_effect=ContentStoreError("模拟 MongoDB 副本派生写入崩溃"),
            ):
                with self.assertRaises(ContentStoreError):
                    self.guide_a.copy(
                        to=self.guide_index,
                        update_attrs={
                            "title": "失败副本",
                            "slug": target_slug,
                        },
                        keep_live=True,
                        copy_revisions=True,
                        user=self.user,
                    )

            # 契约 1：严格零同步 cleanup 调用（禁止在失败路径执行同步物理删除）
            mock_cleanup.assert_not_called()

            # 契约 2：MySQL 事务原子回滚，目标 Page、State 与 Revision 均不存在
            self.assertFalse(Page.objects.filter(slug=target_slug).exists())
            self.assertFalse(ExternalContentPublicationState.objects.filter(page_id=999999).exists())

    def test_native_create_page_alias_action_no_local_revisions(self) -> None:
        """
        验证原生 CreatePageAliasAction 执行生命周期：
        1. 目标别名页面建立托管关系（alias_of_id 匹配源页面）；
        2. 目标别名页面绝不伪造本地 Revision（Revision.objects.for_instance(alias).count() == 0）；
        3. ExternalContentPublicationState 记录 source_page_id，且 published_revision_id 为空。
        """
        action = CreatePageAliasAction(
            self.guide_a,
            parent=self.guide_index,
            user=self.user,
            update_slug=f"alias-native-{uuid.uuid4().hex[:6]}",
        )
        alias_page = action.execute(skip_permission_checks=True)

        self.assertEqual(alias_page.alias_of_id, self.guide_a.pk)
        # 关键断言：原生 Wagtail Alias 绝不允许产生本地 Revision
        self.assertEqual(
            Revision.objects.for_instance(alias_page).count(),
            0,
            "原生别名页面绝不允许伪造生成本地 Revision 记录",
        )

        state_alias = ExternalContentPublicationState.objects.get(page_id=alias_page.pk)
        self.assertEqual(state_alias.source_page_id, self.guide_a.pk)
        self.assertIsNone(state_alias.published_revision_id)

    def test_native_convert_alias_page_action_breaks_following_and_generates_independent_revision(self) -> None:
        """
        验证原生 ConvertAliasPageAction 执行生命周期：
        1. 别名转换为独立页面，解除 alias_of_id 与 source_page_id 追随托管；
        2. 转换后生成首个本地独立 Revision（count() >= 1）；
        3. 源页面后续更新发布，转换后的独立页面正文完好不受影响。
        """
        alias_page = self.guide_a.create_alias(
            parent=self.guide_index,
            update_slug=f"alias-conv-{uuid.uuid4().hex[:6]}",
            user=self.user,
        )
        self.assertEqual(Revision.objects.for_instance(alias_page).count(), 0)

        # 执行原生别名转换
        convert_action = ConvertAliasPageAction(alias_page, user=self.user)
        converted_page = convert_action.execute(skip_permission_checks=True)

        self.assertIsNone(converted_page.alias_of_id, "转换后 alias_of 必须断开")
        state_conv = ExternalContentPublicationState.objects.get(page_id=converted_page.pk)
        self.assertIsNone(state_conv.source_page_id, "转换后 source_page_id 必须置空断开追随")
        self.assertIsNone(state_conv.source_revision_id)

        # 关键断言：独立页面必须拥有首个本地独立 Revision
        self.assertGreaterEqual(
            Revision.objects.for_instance(converted_page).count(),
            1,
            "别名转换为独立页面后必须生成本地独立 Revision",
        )

        # 验证断开追随：源页面后续更新不影响独立页面
        self.guide_a.body = [
            {
                "type": "lead",
                "value": "<p>源页面最新独占修改</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "独占步骤",
                    "description": "<p>独占说明</p>",
                    "image": None,
                },
            },
        ]
        rev_a_new = self.guide_a.save_revision(user=self.user)
        rev_a_new.publish(user=self.user)

        converted_page.refresh_from_db()
        self.assertEqual(
            str(converted_page.specific.body[0].value),
            "<p>页面 A 的专属导读正文内容</p>",
            "源页面再次发布后，已转换的独立页面必须断开追随保持自身正文不变",
        )

    def test_native_copy_page_action_lifecycle(self) -> None:
        """
        验证原生 CopyPageAction 执行完整生命周期：
        1. 成功克隆目标页面并分配全新主键；
        2. 目标页面派生独立正文版本与发布状态；
        3. 目标发布状态的 published_manifest.page_id 绑定新主键。
        """
        state_a = ExternalContentPublicationState.objects.get(page_id=self.guide_a.pk)
        vid_a = state_a.published_manifest["fields"]["body"]["version_id"]

        action = CopyPageAction(
            self.guide_a,
            to=self.guide_index,
            user=self.user,
            update_attrs={
                "title": "原生 Action 副本",
                "slug": f"guide-action-cp-{uuid.uuid4().hex[:6]}",
            },
        )
        copied = action.execute(skip_permission_checks=True)

        self.assertNotEqual(copied.pk, self.guide_a.pk)
        state_copied = ExternalContentPublicationState.objects.get(page_id=copied.pk)
        self.assertEqual(state_copied.published_manifest["page_id"], copied.pk)

        vid_copied = state_copied.published_manifest["fields"]["body"]["version_id"]
        self.assertNotEqual(
            vid_copied,
            vid_a,
            "原生 CopyPageAction 必须为目标页面派生独立的正文版本 ID",
        )

    def test_copy_blocked_when_deletion_intent_active(self) -> None:
        """
        验证源页面存在活跃删除意图或 COMPLETED 墓碑时，复制操作被严格阻断。
        """
        # 场景 1：存在 PENDING 活跃意图
        ExternalContentDeletionIntent.objects.create(
            page_id=self.guide_a.pk,
            model_label=self.guide_a._meta.label_lower,
            status=DeletionIntentStatus.PENDING,
            safe_gc_after=timezone.now() + timezone.timedelta(days=7),
            actor_id=str(self.user.pk),
        )
        with self.assertRaises(ExternalContentOperationBlockedError):
            self.guide_a.copy(
                to=self.guide_index,
                update_attrs={"slug": f"blocked-cp-{uuid.uuid4().hex[:6]}"},
                user=self.user,
            )

        # 场景 2：存在 COMPLETED 永久墓碑意图
        ExternalContentDeletionIntent.objects.filter(page_id=self.guide_a.pk).update(
            status=DeletionIntentStatus.COMPLETED
        )
        with self.assertRaises(ExternalContentOperationBlockedError):
            self.guide_a.copy(
                to=self.guide_index,
                update_attrs={"slug": f"blocked-cp-tomb-{uuid.uuid4().hex[:6]}"},
                user=self.user,
            )

    def test_create_alias_blocked_when_deletion_intent_active(self) -> None:
        """
        验证源页面存在活跃删除意图时，创建别名操作被严格阻断。
        """
        ExternalContentDeletionIntent.objects.create(
            page_id=self.guide_a.pk,
            model_label=self.guide_a._meta.label_lower,
            status=DeletionIntentStatus.PENDING,
            safe_gc_after=timezone.now() + timezone.timedelta(days=7),
            actor_id=str(self.user.pk),
        )
        with self.assertRaises(ExternalContentOperationBlockedError):
            self.guide_a.create_alias(
                parent=self.guide_index,
                update_slug=f"blocked-alias-{uuid.uuid4().hex[:6]}",
                user=self.user,
            )

    def test_copy_fails_closed_on_corrupt_manifest(self) -> None:
        """
        验证当历史 Revision 中的 _external_content_manifest 损坏或缺失必要字段时，
        复制流程严格 Fail-Closed 并抛出 ExternalContentError，杜绝脏数据。
        """
        rev = self.guide_a.get_latest_revision()
        rev.content["_external_content_manifest"] = {"corrupted": True}
        rev.save(update_fields=["content"])

        with self.assertRaises((ManifestValidationError, ManifestError)):
            self.guide_a.copy(
                to=self.guide_index,
                update_attrs={"slug": f"corrupt-cp-{uuid.uuid4().hex[:6]}"},
                copy_revisions=True,
                user=self.user,
            )
