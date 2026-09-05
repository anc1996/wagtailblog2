"""
首页全链路性能与缓存契约单元测试。
严格验证 P0 字体阻断消除、P1 标签与预取查询治理、P2 模板片段缓存与精准失效机制。
"""

from pathlib import Path
from django.test import SimpleTestCase, TestCase
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from blog.wagtail_hooks import _invalidate_home_fragments


class HomePerformanceTemplateContractTests(SimpleTestCase):
    """
    首页模板与静态资源静态契约核验测试。
    """

    def setUp(self) -> None:
        self.app_root = Path(__file__).resolve().parents[3]

    def test_google_fonts_removed_from_style_css(self) -> None:
        """
        验证 P0：style.css 已彻底移除境外 Google Fonts 外部阻塞依赖。
        """
        stylesheet = (self.app_root / "static/gretzia/css/style.css").read_text(encoding="utf-8")
        self.assertNotIn("fonts.googleapis.com", stylesheet)
        self.assertNotIn("fonts.gstatic.com", stylesheet)
        self.assertIn("PingFang SC", stylesheet)
        self.assertIn("Microsoft YaHei", stylesheet)

    def test_home_page_template_fragment_caches_configured(self) -> None:
        """
        验证 P2：home_page.html 中关键高频耗时组件均已配置片段缓存。
        """
        template = (self.app_root / "templates/home/home_page.html").read_text(encoding="utf-8")
        self.assertIn("{% load wagtailcore_tags wagtailimages_tags blog_tags static archive_tags cache %}", template)
        self.assertIn("{% cache 600 home_hero_carousel request.LANGUAGE_CODE %}", template)
        self.assertIn("{% cache 300 home_popular_posts request.LANGUAGE_CODE %}", template)
        self.assertIn("{% cache 600 home_archive_sidebar request.LANGUAGE_CODE %}", template)
        self.assertIn("{% cache 600 home_top_tags_sidebar request.LANGUAGE_CODE %}", template)

    def test_header_template_nav_menu_cache_configured(self) -> None:
        """
        验证 P2：header.html 中主导航菜单已配置片段缓存并按语言和当前页面隔离。
        """
        template = (self.app_root / "templates/includes/header.html").read_text(encoding="utf-8")
        self.assertIn("{% load wagtailcore_tags wagtailimages_tags blog_tags static i18n cache %}", template)
        self.assertIn("{% cache 600 main_nav_menu request.LANGUAGE_CODE page.pk %}", template)


class HomeCacheInvalidationTests(TestCase):
    """
    首页缓存失效治理测试。
    """

    def test_invalidate_home_fragments_clears_targeted_keys_only(self) -> None:
        """
        验证 P2：_invalidate_home_fragments 仅精准清除首页片段，不破坏全站其他业务缓存。
        """
        # 设置测试探针键
        unrelated_key = "unrelated_user_session_cache_key"
        cache.set(unrelated_key, "preserve_value", 300)

        # 设置首页片段测试键
        home_pop_key = make_template_fragment_key("home_popular_posts", ["zh-hans"])
        cache.set(home_pop_key, "rendered_html", 300)

        home_tags_key = make_template_fragment_key("home_top_tags_sidebar", ["zh-hans"])
        cache.set(home_tags_key, "rendered_tags_html", 300)

        self.assertIsNotNone(cache.get(unrelated_key))
        self.assertIsNotNone(cache.get(home_pop_key))
        self.assertIsNotNone(cache.get(home_tags_key))

        # 执行精准失效
        _invalidate_home_fragments()

        # 验证首页片段已被清除
        self.assertIsNone(cache.get(home_pop_key))
        self.assertIsNone(cache.get(home_tags_key))

        # 核心红线验证：无关业务缓存必须完好保留，严禁全站清库
        self.assertEqual(cache.get(unrelated_key), "preserve_value")
