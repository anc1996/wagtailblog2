"""验证 Markdown、Mongo 存储和 Vditor 控件之间的兼容契约。"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.urls import reverse
from django.test import SimpleTestCase

from blog.blocks import (
    MERMAID_RENDERER_LEGACY,
    MERMAID_RENDERER_MODERN,
    MermaidBlock,
    VditorMarkdownBlock,
)
from blog.models import BlogPage
from blog.widgets import VditorMarkdownWidget
from blog.markdown_renderer import MarkdownRenderer
from wagtailblog3.mongodb import MongoDBStreamFieldAdapter
from wagtail.images.formats import get_image_format
from wagtail.images.forms import ImageInsertionForm


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

    def test_multipart_file_limit_covers_large_markdown_imports(self):
        self.assertEqual(settings.DATA_UPLOAD_MAX_NUMBER_FILES, 256)


class MermaidRendererCompatibilityTests(SimpleTestCase):
    """验证 Mermaid 渲染器标识兼容旧 Mongo 正文且不触发批量迁移。"""

    def test_missing_renderer_is_legacy_only_in_memory(self):
        source = {"code": "graph TD\n  A --> B"}
        value = MermaidBlock().to_python(source)

        self.assertEqual(value["code"], source["code"])
        self.assertEqual(value["renderer"], MERMAID_RENDERER_LEGACY)
        self.assertNotIn("renderer", source)

    def test_modern_renderer_is_preserved(self):
        value = MermaidBlock().to_python(
            {"code": "flowchart TD\n  A --> B", "renderer": MERMAID_RENDERER_MODERN}
        )

        self.assertEqual(value["renderer"], MERMAID_RENDERER_MODERN)

    def test_new_renderer_choice_defaults_to_modern(self):
        renderer = MermaidBlock().child_blocks["renderer"]

        self.assertEqual(renderer.get_default(), MERMAID_RENDERER_MODERN)

    def test_mongo_round_trip_keeps_renderer_and_code(self):
        source = {
            "code": "flowchart TD\n  A --> B",
            "renderer": MERMAID_RENDERER_MODERN,
        }
        block = SimpleNamespace(block_type="mermaid_chart", value=source)

        stored = MongoDBStreamFieldAdapter._process_block_value(block)

        self.assertEqual(stored, source)
        self.assertEqual(source["code"], "flowchart TD\n  A --> B")

    def test_renderer_assets_are_local_and_distinct(self):
        static_root = Path(settings.PROJECT_DIR) / "static"
        modern_editor = static_root / "vendor" / "modern-mermaid" / "index.html"
        modern_runtime = static_root / "vendor" / "mermaid-modern-v11.12" / "mermaid.esm.min.mjs"
        legacy_runtime = static_root / "vendor" / "mermaid" / "mermaid.esm.min.mjs"

        self.assertTrue(modern_editor.is_file())
        self.assertTrue(modern_runtime.is_file())
        self.assertTrue(legacy_runtime.is_file())
        self.assertNotEqual(modern_runtime.read_bytes(), legacy_runtime.read_bytes())

    def test_modern_editor_uses_relative_bundled_assets(self):
        editor_html = (
            Path(settings.PROJECT_DIR) / "static" / "vendor" / "modern-mermaid" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('src="./assets/', editor_html)
        self.assertIn('href="./assets/', editor_html)
        self.assertNotIn("https://", editor_html)

    def test_admin_editor_keeps_separate_modern_and_legacy_modes(self):
        static_root = Path(settings.PROJECT_DIR) / "static"
        form_template = (
            Path(settings.PROJECT_DIR) / "templates" / "blog" / "admin" / "mermaid_block_form.html"
        ).read_text(encoding="utf-8")
        bridge = (static_root / "blog" / "js" / "modern-mermaid-wagtail-bridge.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('data-mermaid-mode="modern"', form_template)
        self.assertIn('data-mermaid-mode="legacy"', form_template)
        self.assertIn('data-mermaid-editor-frame-host', form_template)
        self.assertIn('data-contentpath="code"', form_template)
        self.assertIn("LEGACY_MERMAID_PATH", bridge)
        self.assertIn("renderLegacy", bridge)

        embed_css = static_root / "vendor" / "modern-mermaid" / "wagtail-embed.css"
        self.assertTrue(embed_css.is_file())
        self.assertIn("data-wagtail-preview-toolbar", embed_css.read_text(encoding="utf-8"))
        self.assertIn("@container (max-width: 620px)", embed_css.read_text(encoding="utf-8"))

    def test_frontend_renderer_identity_is_explicit_and_isolated(self):
        static_root = Path(settings.PROJECT_DIR) / "static"
        template = (
            Path(settings.PROJECT_DIR) / "templates" / "blog" / "streams" / "mermaid_block.html"
        ).read_text(encoding="utf-8")
        script = (static_root / "content" / "js" / "mermaid-block.js").read_text(encoding="utf-8")
        stylesheet = (static_root / "content" / "css" / "mermaid-block.css").read_text(encoding="utf-8")

        self.assertIn("data-mermaid-renderer", template)
        self.assertIn("mermaid-diagram-wrapper--modern", template)
        self.assertIn("mermaid-diagram-wrapper--legacy", template)
        self.assertIn("data-mermaid-renderer-badge", template)
        self.assertIn("data-mermaid-error-text", template)
        self.assertIn("const RENDERERS", script)
        self.assertIn("/static/vendor/mermaid/mermaid.esm.min.mjs", script)
        self.assertIn("/static/vendor/mermaid-modern-v11.12/mermaid.esm.min.mjs", script)
        self.assertIn("mermaidPromises.delete(renderer)", script)
        self.assertIn("未切换其他渲染器", script)
        self.assertIn("mermaid-diagram-wrapper--modern", stylesheet)
        self.assertIn("mermaid-diagram-wrapper--legacy", stylesheet)


class VditorWidgetCompatibilityTests(SimpleTestCase):
    """验证 Vditor 只增强编辑体验，不改变提交字段和值类型。"""
    markdown = "# title"

    def test_widget_keeps_textarea_as_submission_source(self):
        widget = VditorMarkdownWidget()
        html = widget.render("body", self.markdown, attrs={"id": "body-id"})

        self.assertIn('name="body"', html)
        self.assertIn('id="body-id"', html)
        self.assertIn('data-vditor-markdown="true"', html)
        self.assertIn("data-vditor-page-chooser-url", html)
        self.assertIn("data-vditor-image-chooser-url", html)
        self.assertIn("data-vditor-image-upload-url", html)
        self.assertNotIn('data-controller="easymde"', html)
        self.assertIn("blog-vditor-editor", html)

    def test_widget_exposes_the_wagtail_page_chooser(self):
        widget = VditorMarkdownWidget()
        attrs = widget.build_attrs({})

        self.assertEqual(
            attrs["data-vditor-page-chooser-url"],
            reverse("wagtailadmin_choose_page"),
        )
        with patch(
            "blog.widgets.versioned_static",
            side_effect=lambda path: f"/static/{path}",
        ):
            self.assertTrue(
                any("page-chooser-modal.js" in path for path in widget.media._js)
            )

    def test_widget_exposes_wagtail_image_chooser_and_upload_urls(self):
        widget = VditorMarkdownWidget()
        attrs = widget.build_attrs({})

        self.assertEqual(
            attrs["data-vditor-image-chooser-url"],
            f'{reverse("wagtailimages_chooser:choose")}?select_format=true',
        )
        self.assertEqual(
            attrs["data-vditor-image-upload-url"],
            reverse("blog_vditor_image_upload"),
        )
        with patch(
            "blog.widgets.versioned_static",
            side_effect=lambda path: f"/static/{path}",
        ):
            self.assertTrue(
                any("image-chooser-modal.js" in path for path in widget.media._js)
            )

    def test_vditor_block_uses_widget_without_changing_value_type(self):
        block = VditorMarkdownBlock()

        self.assertIsInstance(block.field.widget, VditorMarkdownWidget)
        self.assertEqual(block.to_python(self.markdown), self.markdown)

    def test_vditor_preview_wraps_wide_tables_for_keyboard_scrolling(self):
        static_root = Path(settings.PROJECT_DIR) / "static"
        script = (static_root / "blog" / "js" / "vditor_markdown.js").read_text(
            encoding="utf-8"
        )
        stylesheet = (static_root / "blog" / "css" / "vditor_admin.css").read_text(
            encoding="utf-8"
        )

        self.assertIn("blog-markdown-table-scroll", script)
        self.assertIn('role", "region"', script)
        self.assertIn("mathBlockPreview: true", script)
        self.assertIn("transformTableMath", script)
        self.assertIn('"code, pre, script, style, textarea, .language-math"', script)
        self.assertIn("collectMarkdownTableImageSpecs", script)
        self.assertIn("restoreMarkdownTableImages", script)
        self.assertIn("encodeMarkdownTableImageEmbedsForEditor", script)
        self.assertIn("decodeMarkdownTableImageEmbedsFromEditor", script)
        self.assertIn("TABLE_IMAGE_EMBED_PATTERN", script)
        self.assertIn("isSafePreviewImageSource", script)
        self.assertIn("data-blog-inline-image-id", script)
        self.assertIn("blog-markdown-table-scroll", stylesheet)
        self.assertIn("overflow-x: auto", stylesheet)

    def test_blog_page_uses_vditor_block_for_markdown_key(self):
        block = BlogPage.body.field.stream_block.child_blocks["markdown_block"]

        self.assertIsInstance(block, VditorMarkdownBlock)
        self.assertIsInstance(block.field.widget, VditorMarkdownWidget)

    def test_extended_image_upload_formats_are_shared_with_wagtail(self):
        self.assertEqual(
            settings.WAGTAILIMAGES_EXTENSIONS,
            [
                "avif",
                "gif",
                "jpg",
                "jpeg",
                "png",
                "webp",
                "heic",
                "tiff",
                "bmp",
            ],
        )
        self.assertEqual(
            settings.WAGTAILIMAGES_FORMAT_CONVERSIONS,
            {
                "bmp": "png",
                "heic": "jpeg",
                "tiff": "jpeg",
            },
        )

    def test_web_compatible_fullwidth_image_format_is_registered(self):
        image_format = get_image_format("fullwidth_web")
        rich_text_choices = dict(
            ImageInsertionForm.base_fields["format"].choices
        )

        self.assertEqual(image_format.classname, "richtext-image full-width")
        self.assertEqual(image_format.filter_spec, "width-800|format-jpeg")
        self.assertEqual(rich_text_choices["fullwidth_web"], "全宽（网页兼容）")


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

    def test_complex_html_table_preserves_row_and_column_spans(self):
        source = (
            "<table><thead><tr>"
            '<th rowspan="2">A</th><th colspan="2">B</th>'
            "</tr><tr><th>C</th><th>D</th></tr></thead>"
            "<tbody><tr><td rowspan=\"2\">x</td>"
            '<td colspan="2">$$x^2$$</td></tr>'
            "<tr><td>y</td><td>z</td></tr></tbody></table>"
        )

        html = MarkdownRenderer.render(source)

        self.assertIn('rowspan="2"', html)
        self.assertIn('colspan="2"', html)
        self.assertIn("arithmatex", html)

    def test_table_span_attributes_do_not_allow_event_handlers(self):
        html = MarkdownRenderer.render(
            '<table><tr><td rowspan="2" onclick="alert(1)">safe</td></tr></table>'
        )

        self.assertIn('rowspan="2"', html)
        self.assertNotIn("onclick", html.lower())

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

    @patch("blog.markdown_renderer.PageLinkHandler.expand_db_attributes_many")
    def test_wagtail_page_link_uses_current_url_at_render_time(self, expand_pages):
        source = '<a linktype="page" id="36" href="/stale-path/">python</a>'
        expand_pages.return_value = ['<a href="/current-path/">']

        html = MarkdownRenderer.render(source)

        self.assertIn('<a href="/current-path/">python</a>', html)
        self.assertNotIn("stale-path", html)
        expand_pages.assert_called_once_with(
            [{"linktype": "page", "id": "36", "href": "/stale-path/"}]
        )

    @patch("blog.markdown_renderer.PageLinkHandler.expand_db_attributes_many")
    def test_invalid_wagtail_page_link_id_does_not_query_pages(self, expand_pages):
        html = MarkdownRenderer.render(
            '<a linktype="page" id="not-a-page-id" href="/stale-path/">python</a>'
        )

        self.assertIn("<a>python</a>", html)
        self.assertNotIn("stale-path", html)
        expand_pages.assert_not_called()

    @patch("blog.markdown_renderer.PageLinkHandler.expand_db_attributes_many")
    def test_wagtail_page_link_rendering_keeps_markdown_source_unchanged(
        self, expand_pages
    ):
        source = '<a linktype="page" id="36" href="/stale-path/">python</a>'
        before = hashlib.sha256(source.encode("utf-8")).hexdigest()
        expand_pages.return_value = ['<a href="/current-path/">']

        MarkdownRenderer.render(source)

        after = hashlib.sha256(source.encode("utf-8")).hexdigest()
        self.assertEqual(before, after)

    @patch("blog.markdown_renderer.PageLinkHandler.expand_db_attributes_many")
    def test_fenced_page_link_example_is_not_resolved(self, expand_pages):
        source = (
            "```html\n"
            '<a linktype="page" id="36" href="/stale-path/">python</a>\n'
            "```"
        )

        html = MarkdownRenderer.render(source)

        self.assertIn("&lt;a linktype=", html)
        expand_pages.assert_not_called()

    @patch("blog.markdown_renderer.ImageEmbedHandler.expand_db_attributes_many")
    def test_wagtail_image_embed_uses_current_rendition_at_render_time(
        self, expand_images
    ):
        source = (
            '<embed embedtype="image" id="42" format="fullwidth" '
            'alt="diagram" src="/stale-image.jpg" width="800" height="450" />'
        )
        expand_images.return_value = [
            '<img src="/current-rendition.jpg" alt="diagram" width="800">'
        ]

        html = MarkdownRenderer.render(source)

        self.assertIn('src="/current-rendition.jpg"', html)
        self.assertNotIn("stale-image", html)
        expand_images.assert_called_once_with(
            [
                {
                    "embedtype": "image",
                    "id": "42",
                    "format": "fullwidth",
                    "alt": "diagram",
                    "src": "/stale-image.jpg",
                    "width": "800",
                    "height": "450",
                }
            ]
        )

    @patch("blog.markdown_renderer.ImageEmbedHandler.expand_db_attributes_many")
    def test_table_image_embed_keeps_structure_and_uses_current_rendition(
        self, expand_images
    ):
        source = (
            '<table><tr><td rowspan="2">'
            '<embed embedtype="image" id="42" format="fullwidth_web" '
            'alt="表格图" src="/original.jpg" />'
            "</td></tr></table>"
        )
        expand_images.return_value = [
            '<img src="/current-rendition.jpg" alt="表格图" class="richtext-image full-width">'
        ]

        html = MarkdownRenderer.render(source)

        self.assertIn('rowspan="2"', html)
        self.assertIn('src="/current-rendition.jpg"', html)
        self.assertNotIn("/original.jpg", html)
        expand_images.assert_called_once_with(
            [
                {
                    "embedtype": "image",
                    "id": "42",
                    "format": "fullwidth_web",
                    "alt": "表格图",
                    "src": "/original.jpg",
                }
            ]
        )

    @patch("blog.markdown_renderer.ImageEmbedHandler.expand_db_attributes_many")
    def test_invalid_wagtail_image_embed_does_not_query_images(
        self, expand_images
    ):
        html = MarkdownRenderer.render(
            '<embed embedtype="image" id="not-an-id" format="fullwidth" '
            'alt="bad" src="https://attacker.invalid/image.jpg" />'
        )

        self.assertNotIn("attacker.invalid", html)
        expand_images.assert_not_called()

    @patch("blog.markdown_renderer.ImageEmbedHandler.expand_db_attributes_many")
    def test_unknown_wagtail_image_format_does_not_query_images(
        self, expand_images
    ):
        html = MarkdownRenderer.render(
            '<embed embedtype="image" id="42" format="not-registered" '
            'alt="bad" src="/stale.jpg" />'
        )

        self.assertNotIn("stale.jpg", html)
        expand_images.assert_not_called()

    @patch("blog.markdown_renderer.ImageEmbedHandler.expand_db_attributes_many")
    def test_wagtail_image_rendering_keeps_markdown_source_unchanged(
        self, expand_images
    ):
        source = (
            '<embed embedtype="image" id="42" format="fullwidth" '
            'alt="diagram" src="/stale.jpg" />'
        )
        before = hashlib.sha256(source.encode("utf-8")).hexdigest()
        expand_images.return_value = ['<img src="/current.jpg" alt="diagram">']

        MarkdownRenderer.render(source)

        after = hashlib.sha256(source.encode("utf-8")).hexdigest()
        self.assertEqual(before, after)

    @patch("blog.markdown_renderer.ImageEmbedHandler.expand_db_attributes_many")
    def test_fenced_image_embed_example_is_not_resolved(self, expand_images):
        source = (
            "```html\n"
            '<embed embedtype="image" id="42" format="fullwidth" '
            'alt="diagram" src="/stale.jpg" />\n'
            "```"
        )

        html = MarkdownRenderer.render(source)

        self.assertIn("&lt;embed embedtype=", html)
        expand_images.assert_not_called()
