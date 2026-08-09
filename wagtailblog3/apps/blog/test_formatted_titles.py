from pathlib import Path
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.template import Context, Template
from django.test import SimpleTestCase

from .inline_title_renderer import InlineTitleRenderer
from .models import BlogPage


class InlineTitleRendererTests(SimpleTestCase):
    def test_supported_inline_markdown(self):
        source = "`check_highlight`：H~2~O、x^2^、$E=mc^2$、**粗体**、*斜体*"

        rendered = str(InlineTitleRenderer.render(source))

        self.assertIn("<code>check_highlight</code>", rendered)
        self.assertIn("<sub>2</sub>", rendered)
        self.assertIn("<sup>2</sup>", rendered)
        self.assertIn('class="arithmatex"', rendered)
        self.assertIn("<strong>粗体</strong>", rendered)
        self.assertIn("<em>斜体</em>", rendered)
        self.assertNotIn("<p>", rendered)

    def test_plain_text_is_derived_from_native_title(self):
        self.assertEqual(
            InlineTitleRenderer.plain_text(
                "`check_highlight`：H~2~O、x^2^、$E=mc^2$"
            ),
            "check_highlight：H2O、x2、E=mc^2",
        )

    def test_disallowed_block_and_active_content(self):
        invalid_sources = (
            "first line\nsecond line",
            "[link](https://example.com)",
            "![image](https://example.com/image.png)",
            "<script>alert(1)</script>",
            "# heading",
            "$$E=mc^2$$",
            "```python",
        )

        for source in invalid_sources:
            with self.subTest(source=source):
                with self.assertRaises(ValidationError):
                    InlineTitleRenderer.render(source)

    def test_html_like_text_is_safe_inside_inline_code(self):
        rendered = str(InlineTitleRenderer.render("`<script>` example"))

        self.assertEqual(rendered, "<code>&lt;script&gt;</code> example")


class MarkdownTitleTemplateTagTests(SimpleTestCase):
    template = Template(
        "{% load blog_tags %}{% render_display_title page %}"
    )

    def test_native_page_title_is_rendered_with_math_marker(self):
        page = SimpleNamespace(title="$E=mc^2$")

        rendered = self.template.render(Context({"page": page}))

        self.assertIn('class="markdown-title"', rendered)
        self.assertIn('data-title-math="true"', rendered)
        self.assertIn('class="arithmatex"', rendered)

    def test_plain_title_is_rendered_and_escaped(self):
        page = SimpleNamespace(title="plain title")

        rendered = self.template.render(Context({"page": page}))

        self.assertIn("plain title", rendered)
        self.assertIn('class="markdown-title"', rendered)

    def test_disallowed_markdown_falls_back_to_escaped_source(self):
        page = SimpleNamespace(
            pk=42,
            title="[link](https://example.com)",
        )

        with self.assertLogs("blog.templatetags.blog_tags", level="WARNING"):
            rendered = self.template.render(Context({"page": page}))

        self.assertIn("[link](https://example.com)", rendered)
        self.assertNotIn("href", rendered)

    def test_plain_text_filter_removes_markdown_syntax(self):
        template = Template(
            "{% load blog_tags %}{{ title|inline_title_text }}"
        )

        rendered = template.render(Context({"title": "`code`、H~2~O、$E=mc^2$"}))

        self.assertEqual(rendered, "code、H2O、E=mc^2")


class MarkdownTitleIntegrationTests(SimpleTestCase):
    public_title_templates = (
        "templates/blog/blog_page.html",
        "templates/blog/partials/_blog_index_results.html",
        "templates/blog/partials/_author_post_results.html",
        "templates/blog/partials/_tag_article_results.html",
        "templates/archive/partials/_archive_results.html",
        "templates/search/partials/_search_results.html",
        "templates/home/home_page.html",
        "templates/portfolio/blocks/featured_posts_block.html",
    )

    def test_native_title_remains_the_search_field(self):
        indexed_fields = {field.field_name for field in BlogPage.search_fields}

        self.assertIn("title", indexed_fields)
        self.assertNotIn("formatted_title", indexed_fields)

    def test_no_secondary_title_field_exists(self):
        field_names = {field.name for field in BlogPage._meta.get_fields()}

        self.assertNotIn("formatted_title", field_names)

    def test_all_public_title_surfaces_use_shared_template_tag(self):
        project_root = Path(__file__).resolve().parents[2]

        for relative_path in self.public_title_templates:
            with self.subTest(template=relative_path):
                content = (project_root / relative_path).read_text(encoding="utf-8")
                self.assertIn("render_display_title", content)

    def test_seo_title_uses_plain_text_filter(self):
        project_root = Path(__file__).resolve().parents[2]
        base_template = (project_root / "templates/base.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("page.title|inline_title_text", base_template)
        self.assertNotIn("render_display_title page", base_template)
