from django.test import SimpleTestCase

from blog.services.markdown_import_parser import parse_markdown_blocks
from blog.services.markdown_import_prepare import (
    build_required_artifacts,
    prepare_summary,
    serialize_block,
)


class MarkdownImportPrepareTests(SimpleTestCase):
    def test_plan_groups_repeated_inline_source_and_keeps_occurrences(self):
        source = (
            "| 名称 | 图片 |\n"
            "| --- | --- |\n"
            "| 一 | ![图](https://cdn.example.test/a.png) |\n"
            "| 二 | ![图](https://cdn.example.test/a.png) |\n"
        )
        blocks = parse_markdown_blocks(source)
        artifacts = build_required_artifacts(blocks)

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(
            artifacts[0]["reference_sources"], ["https://cdn.example.test/a.png"]
        )
        self.assertEqual(len(artifacts[0]["occurrence_ids"]), 2)
        self.assertEqual(artifacts[0]["reference_scope"], "inline_image")

    def test_serialization_preserves_markdown_value_and_line_range(self):
        source = "# 标题\n\n正文\n"
        block = parse_markdown_blocks(source)[0]

        payload = serialize_block(block)

        self.assertEqual(payload["block_type"], "markdown_block")
        self.assertEqual(payload["value"], source)
        self.assertEqual(payload["source_start_line"], 1)
        self.assertEqual(payload["source_end_line"], 3)
        self.assertEqual(prepare_summary((block,), source), {
            "block_count": 1,
            "image_count": 0,
            "markdown_chars": len(source),
        })
