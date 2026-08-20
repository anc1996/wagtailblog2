from django.test import SimpleTestCase

from blog.services.markdown_import_parser import parse_markdown_blocks


class MarkdownImportParserTests(SimpleTestCase):
    def test_mermaid_fence_is_extracted_without_copying_code_to_markdown(self):
        source = (
            "# 标题\n\n"
            "前置说明。\n\n"
            "```mermaid\n"
            "graph TD\n"
            "  A --> B\n"
            "```\n\n"
            "后置说明。\n"
        )

        blocks = parse_markdown_blocks(source)

        self.assertEqual(
            [block.block_type for block in blocks],
            ["markdown_block", "mermaid_chart", "markdown_block"],
        )
        self.assertEqual(blocks[1].value["code"], "graph TD\n  A --> B\n")
        self.assertEqual(blocks[1].value["renderer"], "modern-v11.12")
        markdown = "".join(
            block.value
            for block in blocks
            if block.block_type == "markdown_block"
        )
        self.assertIn("# 标题", markdown)
        self.assertIn("后置说明。", markdown)
        self.assertNotIn("graph TD", markdown)
        self.assertNotIn("```mermaid", markdown)

    def test_non_mermaid_fence_remains_in_markdown(self):
        source = (
            "说明。\n\n"
            "```python\n"
            "print('保留')\n"
            "```\n\n"
            "结尾。\n"
        )

        blocks = parse_markdown_blocks(source)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].block_type, "markdown_block")
        self.assertEqual(blocks[0].value, source)

    def test_empty_markdown_segments_are_not_emitted(self):
        source = "```MerMaid\nflowchart LR\n  A --> B\n```\n"

        blocks = parse_markdown_blocks(source)

        self.assertEqual([block.block_type for block in blocks], ["mermaid_chart"])
        self.assertEqual(blocks[0].source_start_line, 1)
        self.assertEqual(blocks[0].source_end_line, 4)

    def test_standalone_media_are_extracted_in_source_order(self):
        source = (
            "前言。\n\n"
            '![架构图](assets/diagram.png "系统结构")\n\n'
            "[课程视频](https://www.bilibili.com/video/BV1xx411c7mD/)\n\n"
            '<video controls src="media/demo.mp4"></video>\n\n'
            "<audio src='media/theme.mp3' controls></audio>\n\n"
            "结尾。\n"
        )

        blocks = parse_markdown_blocks(source)

        self.assertEqual(
            [block.block_type for block in blocks],
            [
                "markdown_block",
                "image_block",
                "embed_block",
                "video_block",
                "audio_block",
                "markdown_block",
            ],
        )
        self.assertEqual(
            blocks[1].value,
            {
                "source": "assets/diagram.png",
                "alt": "架构图",
                "title": "系统结构",
                "source_kind": "local",
            },
        )
        self.assertEqual(
            blocks[2].value,
            {
                "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
                "title": "课程视频",
            },
        )
        self.assertEqual(
            blocks[3].value,
            {"source": "media/demo.mp4", "source_kind": "local"},
        )
        self.assertEqual(
            blocks[4].value,
            {"source": "media/theme.mp3", "source_kind": "local"},
        )

    def test_reference_image_is_resolved_without_losing_definition(self):
        source = (
            "![流程图][flow]\n\n"
            '[flow]: images/flow.png "流程"\n'
        )

        blocks = parse_markdown_blocks(source)

        self.assertEqual(
            [block.block_type for block in blocks],
            ["image_block", "markdown_block"],
        )
        self.assertEqual(blocks[0].value["source"], "images/flow.png")
        self.assertEqual(blocks[0].value["alt"], "流程图")
        self.assertEqual(blocks[0].value["title"], "流程")
        self.assertEqual(blocks[1].value, '[flow]: images/flow.png "流程"\n')

    def test_remote_image_is_only_classified_and_not_downloaded(self):
        source = "![远程图](https://cdn.example.com/pic.png)\n"

        blocks = parse_markdown_blocks(source)

        self.assertEqual(blocks[0].block_type, "image_block")
        self.assertEqual(blocks[0].value["source_kind"], "remote_https")
        self.assertEqual(
            blocks[0].value["source"], "https://cdn.example.com/pic.png"
        )

    def test_html_images_are_extracted_as_image_blocks(self):
        source = (
            '<img src="assets/local.png" alt="本地" style="zoom:50%;" />\n\n'
            '<img src="https://cdn.example.com/remote.png" alt="远程" />\n'
        )

        blocks = parse_markdown_blocks(source)

        self.assertEqual([block.block_type for block in blocks], ["image_block", "image_block"])
        self.assertEqual(blocks[0].value["source_kind"], "local")
        self.assertEqual(blocks[0].value["alt"], "本地")
        self.assertEqual(blocks[1].value["source_kind"], "remote_https")
        self.assertEqual(blocks[1].value["source"], "https://cdn.example.com/remote.png")

    def test_tables_formulas_and_inline_links_remain_markdown(self):
        source = (
            "| 名称 | 公式 |\n"
            "| --- | --- |\n"
            "| 视频 | $E=mc^2$ |\n\n"
            "正文中的 [视频链接](https://www.youtube.com/watch?v=abc) 不拆块。\n"
        )

        blocks = parse_markdown_blocks(source)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].block_type, "markdown_block")
        self.assertEqual(blocks[0].value, source)

    def test_table_images_are_located_without_splitting_the_markdown_block(self):
        source = (
            '<table>\n'
            '<tr><td rowspan="2">本地</td><td><img src="assets/local.jpg" '
            'alt="本地图" style="zoom:30%;" /></td></tr>\n'
            '<tr><td colspan="2"><img src="https://cdn.example.com/remote.png?width=800&v=2" '
            'alt="远程图" /></td></tr>\n'
            '</table>\n\n'
            '| 类型 | 图片 |\n'
            '| --- | --- |\n'
            '| 远程 | ![链接图](https://cdn.example.com/table.png?x=1&y=2) |\n'
            '| 本地 | ![本地图](assets/table.jpeg) |\n'
            '| HTML | <img src="assets/inline.png" style="zoom:50%;" /> |\n'
        )

        blocks = parse_markdown_blocks(source)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].block_type, "markdown_block")
        self.assertEqual(blocks[0].value, source)
        references = blocks[0].inline_images
        self.assertEqual(len(references), 5)
        self.assertEqual(
            [reference.source for reference in references],
            [
                "assets/local.jpg",
                "https://cdn.example.com/remote.png?width=800&v=2",
                "https://cdn.example.com/table.png?x=1&y=2",
                "assets/table.jpeg",
                "assets/inline.png",
            ],
        )
        self.assertEqual(
            [reference.source_kind for reference in references],
            ["local", "remote_https", "remote_https", "local", "local"],
        )
        self.assertEqual(len({reference.occurrence_id for reference in references}), 5)
        self.assertEqual([reference.table_index for reference in references], [1, 1, 2, 2, 2])
        self.assertIn('rowspan="2"', blocks[0].value)
        self.assertIn('colspan="2"', blocks[0].value)

    def test_table_image_locator_skips_code_and_plain_links(self):
        source = (
            "```html\n"
            '<table><tr><td><img src="ignored.png"></td></tr></table>\n'
            "```\n\n"
            "`<table><img src=\"also-ignored.png\"></table>`\n\n"
            "| 类型 | 内容 |\n"
            "| --- | --- |\n"
            "| 链接 | [不是图片](https://example.com/photo.png) |\n"
            "| 代码 | `![也不是图片](ignored-2.png)` |\n"
        )

        blocks = parse_markdown_blocks(source)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].value, source)
        self.assertEqual(blocks[0].inline_images, ())

    def test_unsupported_or_complex_html_media_remains_markdown(self):
        source = (
            '<video><source src="one.mp4"><source src="two.webm"></video>\n\n'
            '<audio src="https://example.com/live.mp3"></audio>\n'
        )

        blocks = parse_markdown_blocks(source)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].block_type, "markdown_block")
        self.assertEqual(blocks[0].value, source)
