# -*- coding: utf-8 -*-
"""文章反应类型图标体系与后台选型组件定向测试用例。

验证范围：
1. 模板过滤器 ensure_fa_prefix 对各类图标类名前缀的补齐与容错能力；
2. ReactionType 模型的 get_normalized_icon() 方法及合法字符正则校验；
3. ReactionIconSelectWidget 渲染选项、实时预览节点与安全防护；
4. Wagtail 管理后台 ReactionTypeSnippetViewSet 列表页预览列与编辑页交互。
"""

from typing import List, Tuple
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.conf import settings
from django.test import TestCase, Client, override_settings
from django.urls import reverse

from blog.admin import (
    ReactionIconSelectWidget,
    REACTION_ICON_CHOICES,
)
from blog.models import ReactionType
from blog.templatetags.blog_tags import ensure_fa_prefix, active_reactions


class ReactionTypeIconAndFilterTests(TestCase):
    """测试图标格式化过滤器与模型层校验规范。"""

    def test_ensure_fa_prefix_none_and_empty(self) -> None:
        """验证空值与无意义字符串能平稳回退为空字符串。"""
        self.assertEqual(ensure_fa_prefix(None), "fa-solid fa-heart")
        self.assertEqual(ensure_fa_prefix(""), "fa-solid fa-heart")
        self.assertEqual(ensure_fa_prefix("   "), "fa-solid fa-heart")

    def test_ensure_fa_prefix_short_fa(self) -> None:
        """验证单类名如 fa-thumbs-up 自动补齐为 fa-solid fa-thumbs-up。"""
        self.assertEqual(ensure_fa_prefix("fa-thumbs-up"), "fa-solid fa-thumbs-up")
        self.assertEqual(ensure_fa_prefix("fa-heart"), "fa-solid fa-heart")
        self.assertEqual(ensure_fa_prefix("fa-surprise"), "fa-solid fa-surprise")

    def test_ensure_fa_prefix_with_existing_family(self) -> None:
        """验证已有字体族前缀的类名保持原样，不重复追加。"""
        self.assertEqual(ensure_fa_prefix("fas fa-thumbs-up"), "fas fa-thumbs-up")
        self.assertEqual(ensure_fa_prefix("far fa-clock"), "far fa-clock")
        self.assertEqual(ensure_fa_prefix("fab fa-github"), "fab fa-github")
        self.assertEqual(ensure_fa_prefix("fa-solid fa-fire"), "fa-solid fa-fire")
        self.assertEqual(ensure_fa_prefix("fa-regular fa-smile"), "fa-regular fa-smile")
        self.assertEqual(ensure_fa_prefix("fa-brands fa-weixin"), "fa-brands fa-weixin")

    def test_model_get_normalized_icon(self) -> None:
        """验证模型实例方法 get_normalized_icon() 的返回值与过滤器行为一致。"""
        rt1 = ReactionType(name="点赞", icon="fa-thumbs-up", display_order=1)
        self.assertEqual(rt1.get_normalized_icon(), "fa-solid fa-thumbs-up")

        rt2 = ReactionType(name="代码", icon="fas fa-code", display_order=2)
        self.assertEqual(rt2.get_normalized_icon(), "fas fa-code")

    def test_icon_regex_validator(self) -> None:
        """验证 icon 字段的防注入正则校验器：只允许字母、数字、连字符、下划线与空格。"""
        valid_obj = ReactionType(name="合法图标", icon="fa-solid fa-heart_2", display_order=1)
        valid_obj.full_clean()

        invalid_obj = ReactionType(name="恶意注入", icon="fa-heart<script>alert(1)</script>", display_order=2)
        with self.assertRaises(ValidationError):
            invalid_obj.full_clean()

    def test_active_reactions_filter(self) -> None:
        """验证 active_reactions 过滤器精准过滤 count <= 0 的项。"""
        self.assertEqual(active_reactions(None), [])
        self.assertEqual(active_reactions([]), [])

        data = [
            {"id": 1, "name": "点赞", "count": 0},
            {"id": 2, "name": "喜欢", "count": 3},
            {"id": 3, "name": "惊讶", "count": 0},
            {"id": 4, "name": "思考", "count": 5},
        ]
        filtered = active_reactions(data)
        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0]["name"], "喜欢")
        self.assertEqual(filtered[1]["name"], "思考")



class ReactionIconSelectWidgetTests(TestCase):
    """测试 ReactionIconSelectWidget 前端渲染产物。"""

    def setUp(self) -> None:
        self.widget = ReactionIconSelectWidget()

    def test_render_with_preset_choice(self) -> None:
        """测试预设图标时的 HTML 结构：预设下拉选中、自定义输入隐藏、预览图带有安全类名。"""
        html = self.widget.render(name="icon", value="fa-heart")
        self.assertIn('<select id="id_icon_select"', html)
        self.assertIn('<option value="fa-heart" selected="selected">', html)
        self.assertIn('id="id_icon_preview_icon"', html)
        self.assertIn('class="fa-solid fa-heart"', html)
        self.assertIn('id="id_icon_custom_wrap" style="display: none;"', html)

    def test_render_with_custom_choice(self) -> None:
        """测试非预设图标时的 HTML 结构：自定义输入框展示，__custom__ 预设项被选中。"""
        html = self.widget.render(name="icon", value="my-custom-icon")
        self.assertIn('<option value="__custom__" selected="selected">', html)
        self.assertIn('id="id_icon_custom_wrap" style="display: block;"', html)
        self.assertIn('value="my-custom-icon"', html)

    def test_choices_count(self) -> None:
        """确保精选预设选项覆盖至少 30 款高频反应类型。"""
        valid_choices = [c for c in REACTION_ICON_CHOICES if c[0] and c[0] != "__custom__"]
        self.assertGreaterEqual(len(valid_choices), 30)


@override_settings(
    STORAGES={
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class ReactionTypeAdminUITests(TestCase):
    """测试 Wagtail Admin 后台反应类型 Snippet 列表与编辑界面。"""

    @classmethod
    def setUpTestData(cls) -> None:
        User = get_user_model()
        cls.superuser = User.objects.create_superuser(
            username="reaction_admin_tester",
            email="tester@example.com",
            password="P@ssw0rd123456",
        )
        cls.reaction_type = ReactionType.objects.create(
            name="火箭测试",
            icon="fa-rocket",
            display_order=99,
        )

    def setUp(self) -> None:
        self.client = Client()
        self.client.force_login(self.superuser)

    def test_snippet_list_view_renders_icon_preview(self) -> None:
        """验证 Snippet 列表视图中成功渲染图标预览小卡片并加载全局 FontAwesome 静态文件。"""
        url = reverse("wagtailsnippets_blog_reactiontype:list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")
        self.assertIn("reaction-icon-cell", content)
        self.assertIn("fa-solid fa-rocket", content)
        self.assertIn("vendor/fontawesome/css/all.min.css", content)

    def test_snippet_edit_view_renders_widget(self) -> None:
        """验证 Snippet 编辑页成功渲染可视化选型 Widget。"""
        url = reverse("wagtailsnippets_blog_reactiontype:edit", args=[self.reaction_type.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")
        self.assertIn("reaction-icon-widget-container", content)
        self.assertIn("reaction-preset-select", content)
        self.assertIn("id_icon_preview_icon", content)

    def test_snippet_edit_post_updates_icon(self) -> None:
        """验证通过后台表单提交可更新反应类型图标。"""
        url = reverse("wagtailsnippets_blog_reactiontype:edit", args=[self.reaction_type.id])
        post_data = {
            "name": "火箭测试改",
            "icon": "fa-fire",
            "display_order": 88,
        }
        response = self.client.post(url, post_data)
        self.assertIn(response.status_code, [200, 302])

        self.reaction_type.refresh_from_db()
        self.assertEqual(self.reaction_type.name, "火箭测试改")
        self.assertEqual(self.reaction_type.icon, "fa-fire")
        self.assertEqual(self.reaction_type.display_order, 88)

class ReactionTemplatesRenderTests(TestCase):
    """验证 reactions_block 与 stats_block 模板渲染逻辑。"""

    class DummyPage:
        pk = 123
        id = 123

        def __init__(self, reactions=None, view_counts=None):
            self._reactions = reactions or []
            self._views = view_counts or {"total": 0, "today": 0}

        def get_reactions(self):
            return self._reactions

        def get_view_count(self):
            return self._views

        @property
        def specific(self):
            return self

    def test_reactions_block_includes_name_and_icon(self) -> None:
        """验证详情页反应按钮同时输出图标、中文名称与计数节点。"""
        from django.template import Template, Context

        dummy_page = self.DummyPage(
            reactions=[
                {"id": 1, "name": "点赞", "icon": "fa-thumbs-up", "count": 8},
                {"id": 2, "name": "喜欢", "icon": "fa-heart", "count": 4},
            ]
        )

        template_str = '{% include "blog/reactions_block.html" with page=dummy_page user_reaction=1 %}'
        rendered = Template(template_str).render(Context({"dummy_page": dummy_page, "user_reaction": 1}))

        self.assertIn("fa-solid fa-thumbs-up", rendered)
        self.assertIn('<span class="reaction-name">点赞</span>', rendered)
        self.assertIn('<span class="count">8</span>', rendered)
        self.assertIn("fa-solid fa-heart", rendered)
        self.assertIn('<span class="reaction-name">喜欢</span>', rendered)
        self.assertIn('<span class="count">4</span>', rendered)

    def test_stats_block_modes(self) -> None:
        """验证 stats_block 在 views_only 与 reactions_only 模式下的输出。"""
        from django.template import Template, Context

        dummy_page = self.DummyPage(
            reactions=[
                {"id": 1, "name": "点赞", "icon": "fa-thumbs-up", "count": 5},
                {"id": 2, "name": "惊讶", "icon": "fa-surprise", "count": 0},
            ],
            view_counts={"total": 99, "today": 12},
        )

        # 1. views_only: 仅包含访问统计，不包含反应行
        rendered_views = Template('{% include "blog/stats_block.html" with page=dummy_page mode="views_only" %}').render(
            Context({"dummy_page": dummy_page})
        )
        self.assertIn("view-stats", rendered_views)
        self.assertIn("99", rendered_views)
        self.assertNotIn("reaction-stats-row", rendered_views)

        # 2. reactions_only: 仅包含独立反应行，过滤 count=0 的惊讶
        rendered_reactions = Template('{% include "blog/stats_block.html" with page=dummy_page mode="reactions_only" %}').render(
            Context({"dummy_page": dummy_page})
        )
        self.assertNotIn("view-stats", rendered_reactions)
        self.assertIn("reaction-stats-row", rendered_reactions)
        self.assertIn("点赞", rendered_reactions)
        self.assertIn("5", rendered_reactions)
        self.assertNotIn("惊讶", rendered_reactions)
