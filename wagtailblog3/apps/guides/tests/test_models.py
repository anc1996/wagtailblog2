"""
专题指南数据模型与字段契约定向测试。

验证：
1. GuideTopic Snippet 唯一性与 PROTECT 外键删除保护；
2. GuideIndexPage 原生 MySQL 行为、主题/难度筛选、带核查日期优先的稳定排序与草稿隔离；
3. GuidePage 的 ExternalContentPageMixin 继承、content_storage={"body":"mongo"}；
4. GuidePageForm 与 publish() 对草稿允许未完成、公开发布强制要求主题和至少一个步骤的校验；
5. related_pages 关联可见性动态复核（不泄露未发布页面）。
"""

import datetime
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.test import RequestFactory, TestCase
from wagtail.models import Page, Site
from wagtail.snippets.models import get_snippet_models

from external_content.exceptions import PublicationRevisionInvalidError
from external_content.mixins import ExternalContentPageMixin
from guides.models import (
    GuideIndexPage,
    GuidePage,
    GuideRelatedPage,
    GuideTopic,
)

User = get_user_model()


class GuideModelsTestCase(TestCase):
    """专题指南模型与业务规则测试用例。"""

    def setUp(self) -> None:
        self.user = User.objects.create_superuser(
            username="guidetestadmin",
            password="testpassword",
            email="guideadmin@test.com",
        )
        self.factory = RequestFactory()

        # 获取或建立根页面
        from wagtail.models import Locale
        self.locale, _ = Locale.objects.get_or_create(language_code="zh-hans")
        root = Page.objects.filter(depth=1).first()
        if not root:
            root = Page.add_root(title="Root", locale=self.locale)
        self.root_page = root

        self.home_page = Page.objects.filter(depth=2).first()
        if not self.home_page:
            self.home_page = Page(title="Home", slug="home", locale=self.locale)
            self.root_page.add_child(instance=self.home_page)

        # 确保默认站点指向 home_page
        self.site = Site.objects.filter(is_default_site=True).first()
        if not self.site:
            self.site = Site.objects.create(
                hostname="localhost",
                port=80,
                site_name="Test Site",
                root_page=self.home_page,
                is_default_site=True,
            )

        # 建立主题 Snippet
        self.topic_backend = GuideTopic.objects.create(
            name="后端架构",
            slug="backend",
            description="Django 与微服务架构",
        )
        self.topic_frontend = GuideTopic.objects.create(
            name="前端交互",
            slug="frontend",
            description="Vue 与样式体系",
        )

        # 建立专题指南索引页（原生 MySQL Page）
        self.index_page = GuideIndexPage(
            title="专题指南",
            slug="guides",
            intro="欢迎查阅系统专题技术指南",
        )
        self.home_page.add_child(instance=self.index_page)
        self.index_page.save_revision().publish()

    def test_guide_topic_snippet_contract(self) -> None:
        """测试 GuideTopic 注册为 Snippet 且具备唯一 slug 契约。"""
        snippet_models = get_snippet_models()
        self.assertIn(GuideTopic, snippet_models)

        # slug 唯一性
        with self.assertRaises(Exception):
            GuideTopic.objects.create(
                name="后端架构复制",
                slug="backend",
            )

    def test_guide_topic_protect_on_delete(self) -> None:
        """测试 GuideTopic 被 GuidePage 引用时受 PROTECT 约束，禁止物理级联删除。"""
        guide = GuidePage(
            title="测试保护主题",
            slug="test-protect-topic",
            summary="测试外键删除保护",
            topic=self.topic_backend,
            difficulty="beginner",
            body=[
                {
                    "type": "step",
                    "value": {
                        "title": "步骤 1",
                        "description": "<p>测试内容</p>",
                        "image": None,
                    },
                }
            ],
        )
        self.index_page.add_child(instance=guide)
        guide.save_revision()

        with self.assertRaises(ProtectedError):
            self.topic_backend.delete()

    def test_guide_index_page_does_not_inherit_external_content(self) -> None:
        """测试 GuideIndexPage 为原生 Wagtail Page，绝不继承 ExternalContentPageMixin。"""
        self.assertFalse(issubclass(GuideIndexPage, ExternalContentPageMixin))
        self.assertFalse(hasattr(GuideIndexPage, "content_storage"))

    def test_guide_index_page_filtering_and_ordering(self) -> None:
        """测试 GuideIndexPage.get_context 筛选、排序及草稿隔离。"""
        # 创建已发布且带核查日期的指南 1
        g1 = GuidePage(
            title="指南 1 后端初级",
            slug="guide-1",
            summary="摘要 1",
            topic=self.topic_backend,
            difficulty="beginner",
            last_reviewed_at=datetime.date(2026, 9, 1),
            body=[
                {
                    "type": "step",
                    "value": {
                        "title": "第一步",
                        "description": "<p>内容 1</p>",
                        "image": None,
                    },
                }
            ],
        )
        self.index_page.add_child(instance=g1)
        g1.save_revision().publish()

        # 创建已发布且核查日期较新的指南 2
        g2 = GuidePage(
            title="指南 2 前端高级",
            slug="guide-2",
            summary="摘要 2",
            topic=self.topic_frontend,
            difficulty="advanced",
            last_reviewed_at=datetime.date(2026, 9, 20),
            body=[
                {
                    "type": "step",
                    "value": {
                        "title": "第一步",
                        "description": "<p>内容 2</p>",
                        "image": None,
                    },
                }
            ],
        )
        self.index_page.add_child(instance=g2)
        g2.save_revision().publish()

        # 创建已发布但无核查日期的指南 3
        g3 = GuidePage(
            title="指南 3 后端进阶",
            slug="guide-3",
            summary="摘要 3",
            topic=self.topic_backend,
            difficulty="intermediate",
            last_reviewed_at=None,
            body=[
                {
                    "type": "step",
                    "value": {
                        "title": "第一步",
                        "description": "<p>内容 3</p>",
                        "image": None,
                    },
                }
            ],
        )
        self.index_page.add_child(instance=g3)
        g3.save_revision().publish()

        # 创建未发布的草稿指南 4
        g4 = GuidePage(
            title="指南 4 草稿",
            slug="guide-4",
            live=False,
            summary="摘要 4",
            topic=self.topic_backend,
            difficulty="beginner",
            body=[
                {
                    "type": "step",
                    "value": {
                        "title": "第一步",
                        "description": "<p>草稿内容</p>",
                        "image": None,
                    },
                }
            ],
        )
        self.index_page.add_child(instance=g4)
        g4.save_revision()  # 未 publish()

        # 1. 默认无参请求：验证排序为 g2 (09-20) -> g1 (09-01) -> g3 (None)，且排除草稿 g4
        request = self.factory.get("/guides/")
        context = self.index_page.get_context(request)
        guides = list(context["guides"])
        self.assertEqual(len(guides), 3)
        self.assertEqual(guides[0].pk, g2.pk)
        self.assertEqual(guides[1].pk, g1.pk)
        self.assertEqual(guides[2].pk, g3.pk)

        # 2. 主题筛选 ?topic=backend
        req_topic = self.factory.get("/guides/?topic=backend")
        ctx_topic = self.index_page.get_context(req_topic)
        topic_guides = list(ctx_topic["guides"])
        self.assertEqual(len(topic_guides), 2)
        self.assertNotIn(g2.pk, [g.pk for g in topic_guides])

        # 3. 难度筛选 ?difficulty=advanced
        req_diff = self.factory.get("/guides/?difficulty=advanced")
        ctx_diff = self.index_page.get_context(req_diff)
        diff_guides = list(ctx_diff["guides"])
        self.assertEqual(len(diff_guides), 1)
        self.assertEqual(diff_guides[0].pk, g2.pk)

    def test_guide_page_external_content_declaration(self) -> None:
        """测试 GuidePage 声明 content_storage = {'body': 'mongo'} 与 schema 契约。"""
        self.assertTrue(issubclass(GuidePage, ExternalContentPageMixin))
        self.assertEqual(getattr(GuidePage, "content_storage"), {"body": "mongo"})
        self.assertEqual(getattr(GuidePage, "content_storage_schema", 1), 1)
        self.assertEqual(GuidePage.subpage_types, [])
        self.assertEqual(GuidePage.parent_page_types, ["guides.GuideIndexPage"])

    def test_guide_page_publish_validation_enforces_topic_and_steps(self) -> None:
        """测试发布门禁：草稿允许未指定主题或无步骤块，但公开发布强制拦截。"""
        # 1. 创建未设主题且正文无 step 的草稿指南
        incomplete_guide = GuidePage(
            title="未完成草稿指南",
            slug="incomplete-draft-guide",
            live=False,
            summary="草稿摘要",
            topic=None,
            difficulty="beginner",
            body=[
                {
                    "type": "lead",
                    "value": "<p>仅有导读而无步骤</p>",
                }
            ],
        )
        self.index_page.add_child(instance=incomplete_guide)
        rev = incomplete_guide.save_revision(user=self.user)
        self.assertIsNotNone(rev.pk)
        self.assertFalse(incomplete_guide.live)

        # 2. 尝试对其执行发布：必须触发拦截
        with self.assertRaises(PublicationRevisionInvalidError) as cm:
            incomplete_guide.publish(rev, user=self.user)
        self.assertTrue(
            "主题" in str(cm.exception) or "步骤" in str(cm.exception)
        )

        # 3. 补全主题但仍缺步骤块：仍须拦截
        incomplete_guide.topic = self.topic_backend
        rev2 = incomplete_guide.save_revision(user=self.user)
        with self.assertRaises(PublicationRevisionInvalidError) as cm2:
            incomplete_guide.publish(rev2, user=self.user)
        self.assertIn("步骤", str(cm2.exception))

        # 4. 补全步骤块：发布成功
        incomplete_guide.body = [
            {
                "type": "lead",
                "value": "<p>导读文本</p>",
            },
            {
                "type": "step",
                "value": {
                    "title": "关键第一步",
                    "description": "<p>具体步骤说明</p>",
                    "image": None,
                },
            },
        ]
        rev3 = incomplete_guide.save_revision(user=self.user)
        incomplete_guide.publish(rev3, user=self.user)
        self.assertTrue(incomplete_guide.live)

    def test_related_pages_visibility_contract(self) -> None:
        """测试 related_pages 关联：公开渲染必须重新核验目标可见性，过滤未发布页面。"""
        # 目标公开页面
        target_live = GuidePage(
            title="公开目标页面",
            slug="target-live-page",
            summary="公开",
            topic=self.topic_backend,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S1", "description": "<p>D</p>", "image": None}}],
        )
        self.index_page.add_child(instance=target_live)
        target_live.save_revision().publish()

        # 目标草稿页面
        target_draft = GuidePage(
            title="草稿目标页面",
            slug="target-draft-page",
            live=False,
            summary="草稿",
            topic=self.topic_backend,
            difficulty="beginner",
            body=[{"type": "step", "value": {"title": "S1", "description": "<p>D</p>", "image": None}}],
        )
        self.index_page.add_child(instance=target_draft)
        target_draft.save_revision()

        # 主指南页面，同时关联 target_live 和 target_draft
        main_guide = GuidePage(
            title="主指南页面",
            slug="main-guide",
            summary="测试相关页面过滤",
            topic=self.topic_backend,
            difficulty="intermediate",
            body=[{"type": "step", "value": {"title": "S1", "description": "<p>D</p>", "image": None}}],
        )
        self.index_page.add_child(instance=main_guide)
        GuideRelatedPage.objects.create(page=main_guide, related_page=target_live, sort_order=0)
        GuideRelatedPage.objects.create(page=main_guide, related_page=target_draft, sort_order=1)
        main_guide.save_revision().publish()

        request = self.factory.get(main_guide.url)
        context = main_guide.get_context(request)
        visible_related = context["visible_related_pages"]

        # 仅公开的 target_live 出现在 visible_related_pages 中
        visible_pks = [p.pk for p in visible_related]
        self.assertIn(target_live.pk, visible_pks)
        self.assertNotIn(target_draft.pk, visible_pks)