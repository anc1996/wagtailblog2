"""
专题指南 StreamField 正文块定义。

严格限定七类有序顶层块：
1. lead: 导读说明（RichTextBlock，富文本导读段落）；
2. step: 操作步骤（标题、说明、可选图片）；
3. code: 源代码块（语言、代码原文、说明）；
4. tip: 提示与警告（提示类型、正文）；
5. faq: 常见问题（问题、回答）；
6. reference: 参考资料（标题、外链、来源说明）；
7. markdown_block: 原始长段落 Markdown（保持原始字符串，模板渲染阶段转换为 HTML）。
"""

from typing import Any
from wagtail import blocks
from wagtail.images.blocks import ImageChooserBlock

from blog.blocks import VditorMarkdownBlock


class GuideLeadBlock(blocks.RichTextBlock):
    """指南导读块，概述全文核心目标与预期收获。"""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("features", ["bold", "italic", "link"])
        kwargs.setdefault("icon", "openquote")
        kwargs.setdefault("label", "导读说明")
        kwargs.setdefault("help_text", "简要介绍本指南的核心目标与读者收获")
        kwargs.setdefault("template", "guides/blocks/lead_block.html")
        super().__init__(**kwargs)


class GuideStepBlock(blocks.StructBlock):
    """指南操作步骤块，包含标题、正文与可选示例图片。"""

    title = blocks.CharBlock(
        max_length=120,
        label="步骤标题",
        help_text="如：步骤 1：配置系统依赖",
        required=True,
    )
    description = blocks.RichTextBlock(
        features=["bold", "italic", "link", "ol", "ul", "code"],
        label="步骤说明",
        required=True,
    )
    image = ImageChooserBlock(
        label="步骤示意图",
        required=False,
        help_text="可选的步骤示意截图或架构图",
    )

    class Meta:
        icon = "tasks"
        label = "操作步骤"
        template = "guides/blocks/step_block.html"


class GuideCodeBlock(blocks.StructBlock):
    """源代码块，保持代码原文入库，不预转义。"""

    language = blocks.ChoiceBlock(
        choices=[
            ("python", "Python"),
            ("bash", "Bash / Shell"),
            ("javascript", "JavaScript"),
            ("typescript", "TypeScript"),
            ("html", "HTML"),
            ("css", "CSS"),
            ("sql", "SQL"),
            ("json", "JSON"),
            ("yaml", "YAML"),
            ("markdown", "Markdown"),
            ("text", "纯文本"),
        ],
        default="python",
        label="编程语言",
        required=True,
    )
    code = blocks.TextBlock(
        label="源代码内容",
        help_text="粘贴原始源代码，入库时严格保持字面量原样",
        required=True,
    )
    caption = blocks.CharBlock(
        max_length=120,
        label="代码说明 / 文件名",
        required=False,
        help_text="如：/etc/nginx/nginx.conf 或 示例代码 2.1",
    )

    class Meta:
        icon = "code"
        label = "代码块"
        template = "guides/blocks/code_block.html"


class GuideTipBlock(blocks.StructBlock):
    """提示与警告卡片，提供不同警示级别的上下文说明。"""

    tip_type = blocks.ChoiceBlock(
        choices=[
            ("tip", "提示 (Tip)"),
            ("warning", "警告 (Warning)"),
            ("info", "注意 (Info)"),
            ("danger", "高危 (Danger)"),
        ],
        default="tip",
        label="提示类型",
        required=True,
    )
    content = blocks.RichTextBlock(
        features=["bold", "italic", "link", "code"],
        label="提示正文",
        required=True,
    )

    class Meta:
        icon = "help"
        label = "提示 / 警告"
        template = "guides/blocks/tip_block.html"


class GuideFAQBlock(blocks.StructBlock):
    """常见问题问答对。"""

    question = blocks.CharBlock(
        max_length=200,
        label="常见问题",
        required=True,
    )
    answer = blocks.RichTextBlock(
        features=["bold", "italic", "link", "code"],
        label="问题解答",
        required=True,
    )

    class Meta:
        icon = "comment"
        label = "常见问题 (FAQ)"
        template = "guides/blocks/faq_block.html"


class GuideReferenceBlock(blocks.StructBlock):
    """参考资料与外链。"""

    title = blocks.CharBlock(
        max_length=150,
        label="资料标题",
        required=True,
    )
    url = blocks.URLBlock(
        label="链接地址",
        required=True,
    )
    source = blocks.CharBlock(
        max_length=100,
        label="来源 / 机构说明",
        required=False,
        help_text="如：官方技术手册 / IETF RFC",
    )

    class Meta:
        icon = "link"
        label = "参考资料"
        template = "guides/blocks/reference_block.html"


class GuideStreamBlock(blocks.StreamBlock):
    """专题指南统一 StreamBlock 顶级块定义。"""

    lead = GuideLeadBlock(required=False)
    step = GuideStepBlock()
    code = GuideCodeBlock()
    tip = GuideTipBlock()
    faq = GuideFAQBlock()
    reference = GuideReferenceBlock()
    markdown_block = VditorMarkdownBlock(
        label="原始 Markdown",
        icon="code",
        required=False,
    )

    class Meta:
        block_counts = {
            "lead": {"max_num": 1},
        }