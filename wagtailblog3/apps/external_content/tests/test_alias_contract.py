"""
Wagtail Page 正文外置通用协议 - Alias（别名）契约回归测试。

针对 Sol 独立复核发现的 P1 问题进行严格回归：
1. 缺 source ID 的 in_alias 窗口不能使非 alias 源绕过正式 Revision 验证；
2. 无关 alias 不可借用别的 source 窗口进行 save；
3. 直接 CreatePageAliasAction 注入 setup 失败后 Page/State/树 numchild/日志无残留（TransactionTestCase 事务回滚）；
4. A -> B -> C 别名链应能创建、读取正文、源再发布后两级 state 级联更新且无本地 Revision；
5. update_aliases 外来合法 Revision/_content 在目标写入前被拒绝，快照保持；
6. 内层 alias 保存失败导致整个 A -> B -> C 同步回滚且 _controlled_alias_context 上下文原样恢复。
"""

import copy
import uuid
from typing import Any
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from wagtail.actions.create_alias import CreatePageAliasAction
from wagtail.models import Locale, Page, PageLogEntry, Revision

from external_content.copying import setup_external_content_for_alias
from external_content.exceptions import (
    ExternalContentError,
    ExternalContentOperationBlockedError,
    ManifestError,
    PublicationRevisionInvalidError,
)
from external_content.locking import preserve_threadlocal_context
from external_content.mixins import _controlled_alias_context
from external_content.models import ExternalContentPublicationState
from external_content.publication import _controlled_save_context
from guides.models import GuideIndexPage, GuidePage, GuideTopic


def _create_test_user(prefix: str = "alias_admin") -> Any:
    """创建无可直接登录密码的安全测试用户。"""
    user_model = get_user_model()
    user = user_model.objects.create_superuser(
        username=f"{prefix}_{uuid.uuid4().hex[:6]}",
        email="admin@example.com",
    )
    user.set_unusable_password()
    user.save(update_fields=["password"])
    return user


def _create_guide_page(
    *,
    parent: Page,
    title: str,
    slug: str,
    topic: GuideTopic,
    locale: Locale,
    user: Any,
    body_text: str = "引导正文内容",
    step_title: str = "操作步骤一",
    step_description: str = "<p>步骤详细指引说明</p>",
) -> GuidePage:
    """创建并发布包含外置正文的标准指南页 fixture（包含 topic 与合法 step 块）。"""
    page = GuidePage(
        title=title,
        slug=slug,
        summary=f"{title} 摘要",
        topic=topic,
        difficulty="intermediate",
        locale=locale,
        body=[
            {
                "type": "lead",
                "value": f"<p>{body_text}</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": step_title,
                    "description": step_description,
                    "image": None,
                },
            },
        ],
        live=False,
    )
    parent.add_child(instance=page)
    revision = page.save_revision(user=user)
    revision.publish(user=user)
    page.refresh_from_db()
    return page


class ExternalContentAliasContractTests(TestCase):
    """别名契约与保护上下文回归测试。"""

    def setUp(self) -> None:
        super().setUp()
        self.user = _create_test_user("alias_test_admin")
        self.locale_zh, _ = Locale.objects.get_or_create(language_code="zh-hans")

        root = Page.objects.filter(depth=1).first()
        if not root:
            root = Page.add_root(title="Root", locale=self.locale_zh)
        self.root = root

        self.guide_index = GuideIndexPage(
            title="专题指南索引",
            slug=f"guides-{uuid.uuid4().hex[:6]}",
            locale=self.locale_zh,
            live=True,
        )
        self.root.add_child(instance=self.guide_index)
        self.guide_index.save_revision(user=self.user).publish(user=self.user)

        self.topic = GuideTopic.objects.create(
            name="架构治理",
            slug=f"arch-{uuid.uuid4().hex[:6]}",
        )

        self.guide_a = _create_guide_page(
            parent=self.guide_index,
            title="源页面 A",
            slug=f"guide-a-{uuid.uuid4().hex[:6]}",
            topic=self.topic,
            locale=self.locale_zh,
            user=self.user,
            body_text="页面 A 原始正文",
        )

        self.guide_b = _create_guide_page(
            parent=self.guide_index,
            title="源页面 B",
            slug=f"guide-b-{uuid.uuid4().hex[:6]}",
            topic=self.topic,
            locale=self.locale_zh,
            user=self.user,
            body_text="页面 B 原始正文",
        )

    def test_missing_source_id_in_alias_window_cannot_bypass_revision_validation(self) -> None:
        """
        回归 1：缺 source ID 的 in_alias 窗口不能使非 alias 源绕过正式 Revision 验证。
        使用错误归属的 live_revision + in_publication_action 断言抛出领域异常 PublicationRevisionInvalidError。
        """
        source_page = self.guide_a
        self.assertIsNone(source_page.alias_of_id)

        # 将 live_revision_id 指向不属于 source_page 的 revision（页面 B 的 revision）
        foreign_revision = self.guide_b.live_revision
        self.assertIsNotNone(foreign_revision)
        source_page.live_revision_id = foreign_revision.pk

        # 模拟在缺失 alias_update_source_page_id 的 in_alias 窗口中保存非 alias 源页面
        with preserve_threadlocal_context(_controlled_alias_context, "in_alias", True):
            with preserve_threadlocal_context(_controlled_alias_context, "alias_update_source_page_id", None):
                with preserve_threadlocal_context(_controlled_save_context, "in_publication_action", True):
                    with self.assertRaises(PublicationRevisionInvalidError):
                        source_page.save()

    def test_unrelated_alias_cannot_borrow_foreign_source_window_to_save(self) -> None:
        """
        回归 2：无关 alias 不可借用别的 source 窗口 save（涵盖普通元数据保存与 publication 动作）。
        页面 B 的别名 alias_b (alias_of_id == B.pk)：
        1. 在非 publication 动作（普通元数据 save）下，即使 in_alias=True，若窗口为源 A 或无源匹配，
           单凭布尔值 in_alias=True 绝不能越权放行，必须抛出 ExternalContentOperationBlockedError；
        2. 在 publication 动作下借用源 A 窗口携带外来 revision 试图保存，必须抛出领域异常。
        """
        alias_b = self.guide_b.create_alias(
            parent=self.guide_index,
            update_slug=f"alias-b-{uuid.uuid4().hex[:6]}",
            user=self.user,
        )
        self.assertEqual(alias_b.alias_of_id, self.guide_b.pk)
        original_title = alias_b.title

        # 1. 普通元数据 save（in_publication_action=False）：借用源 A 的 alias_update_source_page_id 窗口
        alias_b.title = "被借窗篡改的标题"
        with preserve_threadlocal_context(_controlled_alias_context, "in_alias", True):
            with preserve_threadlocal_context(_controlled_alias_context, "alias_update_source_page_id", self.guide_a.pk):
                with self.assertRaises(ExternalContentOperationBlockedError):
                    alias_b.save()

        # 同样，若仅设置 in_alias=True 但缺少与 alias_b 匹配的 source ID，不可绕过保护
        with preserve_threadlocal_context(_controlled_alias_context, "in_alias", True):
            with preserve_threadlocal_context(_controlled_alias_context, "alias_update_source_page_id", None):
                with self.assertRaises(ExternalContentOperationBlockedError):
                    alias_b.save()

        alias_b.refresh_from_db()
        self.assertEqual(alias_b.title, original_title)

        # 2. 在 publication 动作下（带错误归属 revision）：
        alias_b.live_revision_id = self.guide_a.live_revision_id
        with preserve_threadlocal_context(_controlled_alias_context, "in_alias", True):
            with preserve_threadlocal_context(_controlled_alias_context, "alias_update_source_page_id", self.guide_a.pk):
                with preserve_threadlocal_context(_controlled_save_context, "in_publication_action", True):
                    with self.assertRaises((PublicationRevisionInvalidError, ExternalContentOperationBlockedError)):
                        alias_b.save()

    def test_alias_chain_a_b_c_creation_body_read_and_republish_sync(self) -> None:
        """
        回归 4：A -> B -> C alias 链严格拓扑契约。
        必须严格满足：
        1. alias_c.alias_of_id 严格等于 B（不允许直接指 A）；
        2. state_c 的直属 source_page_id 严格等于 B；
        3. state_c 的 source_revision_id 记录根 A 的正式 Revision；
        4. state_c 的 published_manifest 保持根 A 的身份（page_id == A.pk）；
        5. B 和 C 均严格不创建本地 Revision；
        6. 源 A 再发布后，两级 state 与正文均同步推进，拓扑归属严格保持。
        """
        # 1. 创建别名 B (源自 A)
        alias_b = self.guide_a.create_alias(
            parent=self.guide_index,
            update_slug=f"chain-b-{uuid.uuid4().hex[:6]}",
            user=self.user,
        )
        self.assertEqual(alias_b.alias_of_id, self.guide_a.pk)

        # 2. 从别名 B 创建别名 C (形成 A -> B -> C 链)，严格断言 alias_of_id 为 B
        alias_c = alias_b.create_alias(
            parent=self.guide_index,
            update_slug=f"chain-c-{uuid.uuid4().hex[:6]}",
            user=self.user,
        )
        self.assertEqual(alias_c.alias_of_id, alias_b.pk)

        state_a_initial = ExternalContentPublicationState.objects.get(page_id=self.guide_a.pk)
        state_b_initial = ExternalContentPublicationState.objects.get(page_id=alias_b.pk)
        state_c_initial = ExternalContentPublicationState.objects.get(page_id=alias_c.pk)

        # 验证初始状态拓扑与根指针
        self.assertEqual(state_b_initial.source_page_id, self.guide_a.pk)
        self.assertEqual(state_c_initial.source_page_id, alias_b.pk)
        self.assertEqual(state_c_initial.source_revision_id, state_a_initial.published_revision_id)
        self.assertIsNotNone(state_c_initial.published_manifest)
        self.assertEqual(state_c_initial.published_manifest["page_id"], self.guide_a.pk)

        # 3. 验证三者正文均可正常读取且与根 A 一致
        self.guide_a.refresh_from_db()
        alias_b.refresh_from_db()
        alias_c.refresh_from_db()
        self.assertEqual(len(self.guide_a.body), 2)
        self.assertEqual(len(alias_b.body), 2)
        self.assertEqual(len(alias_c.body), 2)
        self.assertEqual(alias_b.body[0].value, self.guide_a.body[0].value)
        self.assertEqual(alias_c.body[0].value, self.guide_a.body[0].value)
        self.assertEqual(alias_b.body[1].value["title"], self.guide_a.body[1].value["title"])
        self.assertEqual(alias_c.body[1].value["title"], self.guide_a.body[1].value["title"])

        # 4. 严格验证 B 和 C 均无本地 Revision
        self.assertEqual(Revision.objects.for_instance(alias_b).count(), 0)
        self.assertEqual(Revision.objects.for_instance(alias_c).count(), 0)

        # 5. 源页面 A 重新发布新正文（保留合法 step 块）
        self.guide_a.body = [
            {
                "type": "lead",
                "value": "<p>页面 A 级联更新后的新正文</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "级联步骤",
                    "description": "<p>级联更新后的步骤内容</p>",
                    "image": None,
                },
            },
        ]
        new_revision = self.guide_a.save_revision(user=self.user)
        new_revision.publish(user=self.user)
        self.guide_a.refresh_from_db()

        state_a = ExternalContentPublicationState.objects.get(page_id=self.guide_a.pk)
        state_b = ExternalContentPublicationState.objects.get(page_id=alias_b.pk)
        state_c = ExternalContentPublicationState.objects.get(page_id=alias_c.pk)

        # 6. 两级 state 均需与源 A 的发布代次对齐，直属 source_page_id 保持不变，根 Revision 与 Manifest 身份正确
        self.assertEqual(state_b.source_page_id, self.guide_a.pk)
        self.assertEqual(state_c.source_page_id, alias_b.pk)
        self.assertEqual(state_b.publication_generation, state_a.publication_generation)
        self.assertEqual(state_c.publication_generation, state_a.publication_generation)
        self.assertEqual(state_b.source_revision_id, state_a.published_revision_id)
        self.assertEqual(state_c.source_revision_id, state_a.published_revision_id)
        self.assertEqual(state_c.published_manifest["page_id"], self.guide_a.pk)

        # 依然严格无本地 Revision
        self.assertEqual(Revision.objects.for_instance(alias_b).count(), 0)
        self.assertEqual(Revision.objects.for_instance(alias_c).count(), 0)

        # 正文读取与源 A 保持一致
        alias_b.refresh_from_db()
        alias_c.refresh_from_db()
        self.assertEqual(alias_b.body[0].value.source, "<p>页面 A 级联更新后的新正文</p>")
        self.assertEqual(alias_c.body[0].value.source, "<p>页面 A 级联更新后的新正文</p>")
        self.assertEqual(alias_b.body[1].value["title"], "级联步骤")
        self.assertEqual(alias_c.body[1].value["title"], "级联步骤")

    def test_update_aliases_rejects_foreign_revision_and_foreign_snapshot(self) -> None:
        """
        回归 5.1：update_aliases 传入完整合法 foreign snapshot + foreign revision 时在目标写入前被拒绝。
        只允许抛出领域异常（禁止放行或退化为 TypeError），验证目标别名元数据与 state 保持不变。
        """
        alias_a = self.guide_a.create_alias(
            parent=self.guide_index,
            update_slug=f"alias-snapshot-{uuid.uuid4().hex[:6]}",
            user=self.user,
        )
        alias_a.refresh_from_db()
        title_before = alias_a.title
        slug_before = alias_a.slug
        state_before = ExternalContentPublicationState.objects.get(page_id=alias_a.pk)
        manifest_before = state_before.published_manifest
        generation_before = state_before.publication_generation

        foreign_revision = self.guide_b.live_revision
        self.assertIsNotNone(foreign_revision)
        foreign_snapshot = copy.deepcopy(foreign_revision.content)

        # 传入外来页面的合法 revision 与合法 content 快照
        with self.assertRaises((PublicationRevisionInvalidError, ManifestError, ExternalContentError)):
            self.guide_a.update_aliases(
                revision=foreign_revision,
                _content=foreign_snapshot,
            )

        # 验证目标别名元数据与 state 快照未被修改
        alias_a.refresh_from_db()
        self.assertEqual(alias_a.title, title_before)
        self.assertEqual(alias_a.slug, slug_before)
        state_after = ExternalContentPublicationState.objects.get(page_id=alias_a.pk)
        self.assertEqual(state_after.published_manifest, manifest_before)
        self.assertEqual(state_after.publication_generation, generation_before)

    def test_update_aliases_rejects_root_revision_with_foreign_snapshot(self) -> None:
        """
        回归 5.2：update_aliases 传入合法 root revision + foreign snapshot 时被拒绝。
        只允许抛出领域异常，验证元数据与 state 快照保持不变。
        """
        alias_a = self.guide_a.create_alias(
            parent=self.guide_index,
            update_slug=f"alias-snapshot2-{uuid.uuid4().hex[:6]}",
            user=self.user,
        )
        alias_a.refresh_from_db()
        title_before = alias_a.title
        slug_before = alias_a.slug
        state_before = ExternalContentPublicationState.objects.get(page_id=alias_a.pk)
        manifest_before = state_before.published_manifest
        generation_before = state_before.publication_generation

        root_revision = self.guide_a.live_revision
        self.assertIsNotNone(root_revision)
        foreign_revision = self.guide_b.live_revision
        self.assertIsNotNone(foreign_revision)
        foreign_snapshot = copy.deepcopy(foreign_revision.content)

        with self.assertRaises((PublicationRevisionInvalidError, ManifestError, ExternalContentError)):
            self.guide_a.update_aliases(
                revision=root_revision,
                _content=foreign_snapshot,
            )

        alias_a.refresh_from_db()
        self.assertEqual(alias_a.title, title_before)
        self.assertEqual(alias_a.slug, slug_before)
        state_after = ExternalContentPublicationState.objects.get(page_id=alias_a.pk)
        self.assertEqual(state_after.published_manifest, manifest_before)
        self.assertEqual(state_after.publication_generation, generation_before)

    def test_update_aliases_rejects_root_revision_with_tampered_non_manifest_content(self) -> None:
        """
        回归 5.3：update_aliases 传入合法 root revision + 非 manifest 字段篡改的内容快照时被拒绝。
        只允许抛出领域异常（禁止退化为 TypeError），验证元数据与 state 快照保持不变。
        """
        alias_a = self.guide_a.create_alias(
            parent=self.guide_index,
            update_slug=f"alias-snapshot3-{uuid.uuid4().hex[:6]}",
            user=self.user,
        )
        alias_a.refresh_from_db()
        title_before = alias_a.title
        slug_before = alias_a.slug
        state_before = ExternalContentPublicationState.objects.get(page_id=alias_a.pk)
        manifest_before = state_before.published_manifest
        generation_before = state_before.publication_generation

        root_revision = self.guide_a.live_revision
        self.assertIsNotNone(root_revision)
        tampered_content = copy.deepcopy(root_revision.content)
        tampered_content["title"] = "被篡改的非manifest标题"

        with self.assertRaises((PublicationRevisionInvalidError, ManifestError, ExternalContentError)):
            self.guide_a.update_aliases(
                revision=root_revision,
                _content=tampered_content,
            )

        alias_a.refresh_from_db()
        self.assertEqual(alias_a.title, title_before)
        self.assertEqual(alias_a.slug, slug_before)
        state_after = ExternalContentPublicationState.objects.get(page_id=alias_a.pk)
        self.assertEqual(state_after.published_manifest, manifest_before)
        self.assertEqual(state_after.publication_generation, generation_before)

    def test_inner_alias_failure_triggers_complete_rollback_and_context_restoration(self) -> None:
        """
        回归 6：末级 alias 保存失败导致整个 A -> B -> C 同步回滚且上下文原样恢复。
        使用原生 new_revision.publish(user=self.user) 触发完整级联发布流程，
        通过 patch GuidePage.save autospec 仅在内层 C 失败、其余原样 save，
        断言：
        1. 根页面 A 发布指针与 state 保持原样（无推进）；
        2. 中间别名 B 标题、正文与 state 绝无部分推进；
        3. 末级别名 C 标题、正文与 state 绝无部分推进；
        4. 线程上下文 _controlled_alias_context 的属性存在性与属性值均精确恢复。
        """
        alias_b = self.guide_a.create_alias(
            parent=self.guide_index,
            update_slug=f"sync-b-{uuid.uuid4().hex[:6]}",
            user=self.user,
        )
        alias_c = alias_b.create_alias(
            parent=self.guide_index,
            update_slug=f"sync-c-{uuid.uuid4().hex[:6]}",
            user=self.user,
        )

        self.guide_a.refresh_from_db()
        rev_a_before = self.guide_a.live_revision_id
        state_a_before = ExternalContentPublicationState.objects.get(page_id=self.guide_a.pk)
        gen_a_before = state_a_before.publication_generation

        alias_b.refresh_from_db()
        initial_b_title = alias_b.title
        initial_b_lead = alias_b.body[0].value
        initial_b_step_title = alias_b.body[1].value["title"]
        state_b_before = ExternalContentPublicationState.objects.get(page_id=alias_b.pk)
        gen_b_before = state_b_before.publication_generation
        manifest_b_before = state_b_before.published_manifest

        alias_c.refresh_from_db()
        initial_c_title = alias_c.title
        initial_c_lead = alias_c.body[0].value
        initial_c_step_title = alias_c.body[1].value["title"]
        state_c_before = ExternalContentPublicationState.objects.get(page_id=alias_c.pk)
        gen_c_before = state_c_before.publication_generation
        manifest_c_before = state_c_before.published_manifest

        # 精确记录初始线程上下文属性的存在性与值
        has_in_alias_before = hasattr(_controlled_alias_context, "in_alias")
        val_in_alias_before = getattr(_controlled_alias_context, "in_alias", None)
        has_source_id_before = hasattr(_controlled_alias_context, "alias_update_source_page_id")
        val_source_id_before = getattr(_controlled_alias_context, "alias_update_source_page_id", None)

        # 源页面 A 更新标题与正文（保留合法 step 块），生成新 Revision
        self.guide_a.title = "源 A 更新标题"
        self.guide_a.body = [
            {
                "type": "lead",
                "value": "<p>源 A 全新正文内容</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "新步骤标题",
                    "description": "<p>新步骤正文内容</p>",
                    "image": None,
                },
            },
        ]
        new_revision = self.guide_a.save_revision(user=self.user)

        orig_guide_save = GuidePage.save

        def broken_guide_save(page_inst: Any, *args: Any, **kwargs: Any) -> Any:
            if page_inst.pk == alias_c.pk:
                raise RuntimeError("Simulated failure when saving inner alias C")
            return orig_guide_save(page_inst, *args, **kwargs)

        # 使用 patch GuidePage.save autospec 在内层 C 失败，执行原生 new_revision.publish
        with patch.object(GuidePage, "save", autospec=True, side_effect=broken_guide_save):
            with self.assertRaises(RuntimeError):
                new_revision.publish(user=self.user)

        # 1. 验证根页面 A 发布指针与 state 保持原样（无推进）
        self.guide_a.refresh_from_db()
        self.assertEqual(self.guide_a.live_revision_id, rev_a_before)
        state_a_after = ExternalContentPublicationState.objects.get(page_id=self.guide_a.pk)
        self.assertEqual(state_a_after.publication_generation, gen_a_before)
        self.assertEqual(state_a_after.published_revision_id, rev_a_before)

        # 2. 验证 B 的 title / body / state 绝无部分推进
        alias_b.refresh_from_db()
        self.assertEqual(alias_b.title, initial_b_title)
        self.assertEqual(alias_b.body[0].value, initial_b_lead)
        self.assertEqual(alias_b.body[1].value["title"], initial_b_step_title)
        state_b_after = ExternalContentPublicationState.objects.get(page_id=alias_b.pk)
        self.assertEqual(state_b_after.publication_generation, gen_b_before)
        self.assertEqual(state_b_after.published_manifest, manifest_b_before)

        # 3. 验证末级 C 的 title / body / state 绝无部分推进
        alias_c.refresh_from_db()
        self.assertEqual(alias_c.title, initial_c_title)
        self.assertEqual(alias_c.body[0].value, initial_c_lead)
        self.assertEqual(alias_c.body[1].value["title"], initial_c_step_title)
        state_c_after = ExternalContentPublicationState.objects.get(page_id=alias_c.pk)
        self.assertEqual(state_c_after.publication_generation, gen_c_before)
        self.assertEqual(state_c_after.published_manifest, manifest_c_before)

        # 4. 验证线程局部上下文属性存在性与属性值均精确恢复
        self.assertEqual(hasattr(_controlled_alias_context, "in_alias"), has_in_alias_before)
        self.assertEqual(getattr(_controlled_alias_context, "in_alias", None), val_in_alias_before)
        self.assertEqual(
            hasattr(_controlled_alias_context, "alias_update_source_page_id"),
            has_source_id_before,
        )
        self.assertEqual(
            getattr(_controlled_alias_context, "alias_update_source_page_id", None),
            val_source_id_before,
        )


class ExternalContentAliasActionTransactionTests(TransactionTestCase):
    """涉及真实数据库事务回滚的 CreatePageAliasAction 测试。"""

    def setUp(self) -> None:
        super().setUp()
        self.user = _create_test_user("alias_tx_admin")
        self.locale_zh, _ = Locale.objects.get_or_create(language_code="zh-hans")

        root = Page.objects.filter(depth=1).first()
        if not root:
            root = Page.add_root(title="Root", locale=self.locale_zh)
        self.root = root

        self.guide_index = GuideIndexPage(
            title="事务测试指南索引",
            slug=f"tx-guides-{uuid.uuid4().hex[:6]}",
            locale=self.locale_zh,
            live=True,
        )
        self.root.add_child(instance=self.guide_index)
        self.guide_index.save_revision(user=self.user).publish(user=self.user)

        self.topic = GuideTopic.objects.create(
            name="事务治理",
            slug=f"tx-topic-{uuid.uuid4().hex[:6]}",
        )

        self.guide_a = _create_guide_page(
            parent=self.guide_index,
            title="事务源页面 A",
            slug=f"tx-guide-a-{uuid.uuid4().hex[:6]}",
            topic=self.topic,
            locale=self.locale_zh,
            user=self.user,
            body_text="事务页面 A 原始正文",
        )

    def test_create_page_alias_action_setup_failure_leaves_no_residue(self) -> None:
        """
        回归 3：直接 CreatePageAliasAction 注入 setup 失败后 Page/State/树 numchild/日志无残留。
        必须在 TransactionTestCase 真实 autocommit 下验证整条 action 的原子回滚。
        """
        self.guide_index.refresh_from_db()
        initial_numchild = self.guide_index.numchild
        target_slug = f"tx-alias-failed-{uuid.uuid4().hex[:6]}"

        # 记录初始相关对象计数
        initial_state_count = ExternalContentPublicationState.objects.count()
        initial_page_count = Page.objects.count()
        initial_log_count = PageLogEntry.objects.count()

        # 注入 setup_external_content_for_alias 失败
        with patch(
            "external_content.copying.setup_external_content_for_alias",
            side_effect=ExternalContentError("Injected failure during setup_external_content_for_alias"),
        ):
            action = CreatePageAliasAction(
                self.guide_a,
                parent=self.guide_index,
                user=self.user,
                update_slug=target_slug,
            )
            with self.assertRaises(ExternalContentError):
                action.execute(skip_permission_checks=True)

        # 断言 1：无任何残留 Page
        self.assertFalse(Page.objects.filter(slug=target_slug).exists())
        self.assertEqual(Page.objects.count(), initial_page_count)

        # 断言 2：父级页面树结构 numchild 无残留变化
        self.guide_index.refresh_from_db()
        self.assertEqual(self.guide_index.numchild, initial_numchild)

        # 断言 3：ExternalContentPublicationState 无残留
        self.assertEqual(ExternalContentPublicationState.objects.count(), initial_state_count)

        # 断言 4：Wagtail PageLogEntry 审计日志无残留
        self.assertEqual(PageLogEntry.objects.count(), initial_log_count)
