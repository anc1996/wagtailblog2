"""验证 Markdown、Mongo 存储和 Vditor 控件之间的兼容契约。"""

import hashlib
from types import SimpleNamespace

from django.test import SimpleTestCase

from blog.blocks import VditorMarkdownBlock
from blog.models import BlogPage
from blog.widgets import VditorMarkdownWidget
from blog.markdown_renderer import MarkdownRenderer
from wagtailblog3.mongodb import MongoDBStreamFieldAdapter


class MarkdownStorageCompatibilityTests(SimpleTestCase):
    """验证 Markdown 在 Mongo 存储、恢复和渲染之间保持原样。"""
    markdown = """# Existing article

```python
print('unchanged')
```

$$E = mc^2$$
"""

    def test_markdown_block_storage_keeps_plain_string(self):
        block = SimpleNamespace(block_type="markdown_block", value=self.markdown)

        self.assertEqual(
            MongoDBStreamFieldAdapter._process_block_value(block), self.markdown
        )

    def test_legacy_markdown_dictionary_is_normalized_to_string(self):
        stream_block = BlogPage.body.field.stream_block
        payload = [
            {
                "type": "markdown_block",
                "id": "legacy-markdown-block",
                "value": {"raw": self.markdown},
            }
        ]

        stream_value = MongoDBStreamFieldAdapter.from_mongodb(payload, stream_block)

        self.assertEqual(stream_value.raw_data[0]["value"], self.markdown)

    def test_round_trip_sample_does_not_change_markdown_hash(self):
        block = SimpleNamespace(block_type="markdown_block", value=self.markdown)
        before = hashlib.sha256(self.markdown.encode("utf-8")).hexdigest()
        stored = MongoDBStreamFieldAdapter._process_block_value(block)
        after = hashlib.sha256(stored.encode("utf-8")).hexdigest()

        self.assertEqual(before, after)


class VditorWidgetCompatibilityTests(SimpleTestCase):
    """验证 Vditor 只增强编辑体验，不改变提交字段和值类型。"""
    markdown = "# title"

    def test_widget_keeps_textarea_as_submission_source(self):
        widget = VditorMarkdownWidget()
        html = widget.render("body", self.markdown, attrs={"id": "body-id"})

        self.assertIn('name="body"', html)
        self.assertIn('id="body-id"', html)
        self.assertIn('data-vditor-markdown="true"', html)
        self.assertNotIn('data-controller="easymde"', html)
        self.assertIn("blog-vditor-editor", html)

    def test_vditor_block_uses_widget_without_changing_value_type(self):
        block = VditorMarkdownBlock()

        self.assertIsInstance(block.field.widget, VditorMarkdownWidget)
        self.assertEqual(block.to_python(self.markdown), self.markdown)

    def test_blog_page_uses_vditor_block_for_markdown_key(self):
        block = BlogPage.body.field.stream_block.child_blocks["markdown_block"]

        self.assertIsInstance(block, VditorMarkdownBlock)
        self.assertIsInstance(block.field.widget, VditorMarkdownWidget)


class MarkdownRendererCompatibilityTests(SimpleTestCase):
    """验证表格、代码、公式、Mermaid 和 HTML 清洗等渲染契约。"""
    sample = (
        "# Compatibility\n\n"
        "| Name | Value |\n| --- | --- |\n| old | new |\n\n"
        "```python\nprint('safe')\n```\n\n"
        "Inline math: $x^2$.\n\n"
        "```mermaid\ngraph TD\n  A --> B\n```\n"
    )

    def test_existing_markdown_features_render_once(self):
        html = MarkdownRenderer.render(self.sample)

        self.assertIn("<table>", html)
        self.assertIn("language-python", html)
        self.assertIn("arithmatex", html)
        self.assertIn("language-mermaid", html)

    def test_output_is_sanitised(self):
        html = MarkdownRenderer.render(
            '<script>alert(1)</script>\n\n![x](javascript:alert(1) "bad")'
        )

        self.assertNotIn("<script", html.lower())
        self.assertNotIn("javascript:", html.lower())

    def test_rendering_does_not_mutate_stream_source(self):
        payload = [{"type": "markdown_block", "value": self.sample, "id": "1"}]
        original = payload[0]["value"]

        MarkdownRenderer.render(payload[0]["value"])

        self.assertEqual(payload[0]["value"], original)
