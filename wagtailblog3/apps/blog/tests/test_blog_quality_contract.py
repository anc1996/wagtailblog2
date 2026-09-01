from pathlib import Path

from django.test import TestCase
from wagtail.blocks.stream_block import StreamValue

from blog.models import BlogPage


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class BlogQualityContractTests(TestCase):
    """锁定无需迁移的前台 SEO 和可访问性契约。"""

    def test_base_template_keeps_pages_zoomable_and_canonical(self):
        template = (PROJECT_ROOT / 'templates' / 'base.html').read_text(encoding='utf-8')

        self.assertIn('name="viewport" content="width=device-width, initial-scale=1.0"', template)
        self.assertIn('rel="canonical"', template)
        self.assertIn('property="og:url"', template)
        self.assertIn('property="og:image"', template)

    def test_article_template_emits_structured_data_and_reuses_navigation_context(self):
        template = (PROJECT_ROOT / 'templates' / 'blog' / 'blog_page.html').read_text(encoding='utf-8')

        self.assertIn('article_structured_data|json_script', template)
        self.assertIn('{% if related_posts %}', template)
        self.assertIn('{% if prev_post or next_post %}', template)
        self.assertNotIn('page.get_related_posts_by_tags', template)
        self.assertNotIn('page.get_prev_post', template)
        self.assertNotIn('page.get_next_post', template)

    def test_blog_model_exposes_publish_quality_diagnostics(self):
        model_source = (PROJECT_ROOT / 'apps' / 'blog' / 'models.py').read_text(encoding='utf-8')

        self.assertIn('def get_publish_quality_issues', model_source)
        self.assertIn('缺少文章摘要', model_source)

    def test_publish_quality_diagnostics_report_empty_draft_fields(self):
        page = BlogPage(title='', intro='', body=None, search_description='')

        self.assertEqual(
            page.get_publish_quality_issues(),
            ['缺少文章标题', '缺少文章摘要', '正文为空', '未设置搜索摘要，将使用文章摘要作为 SEO 描述'],
        )

    def test_publish_quality_diagnostics_accept_complete_draft(self):
        page = BlogPage(
            title='测试文章',
            intro='这是摘要',
            body=StreamValue(BlogPage._meta.get_field('body').stream_block, [('paragraph', '正文')], is_lazy=True),
            search_description='SEO 摘要',
        )

        self.assertEqual(page.get_publish_quality_issues(), [])
