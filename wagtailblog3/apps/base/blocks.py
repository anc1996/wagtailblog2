# base/blocks.py

# 导入 Wagtail StreamField 中常用的块类型
from wagtail.blocks import (
    CharBlock,  # 用于简单的文本输入框
    ChoiceBlock, # 用于下拉选择框
    RichTextBlock, # 用于富文本编辑器 (WYSIWYG)
    StreamBlock, # 用于定义 StreamField，包含多个不同类型的块
    StructBlock, # 用于将多个字段组合成一个结构化的块
)

# 导入用于嵌入外部内容的块类型（如 YouTube 视频、Twitter 推文等）
from wagtail.embeds.blocks import EmbedBlock

# 导入用于处理图片的块类型。
# 注意：这里使用的是 Wagtail 内置的 ImageBlock，它默认与 Wagtail 的默认图片模型关联。
# 如果你需要使用自定义图片模型，通常会替换为 ImageChooserBlock 或自定义的 ImageChooserBlock。
from wagtail.images.blocks import ImageBlock


"""Wagtail 提供了内置模板来渲染每个区块。不过，你可以使用自定义模板覆盖内置模板。"""

# 定义一个自定义的 StructBlock，用于包含图片、标题和归属信息
class CaptionedImageBlock(StructBlock):
	
    # 定义一个图片字段。使用 ImageBlock，并设置为必填（required=True）。
    # 在后台，这将提供一个图片选择/上传界面。
    image = ImageBlock(required=True)

    # 定义一个标题字段，用于图片的文字说明，非必填。
    caption = CharBlock(required=False)

    # 定义一个归属字段，用于说明图片的来源或作者，非必填。
    attribution = CharBlock(required=False)

    # Meta 类用于定义块的元数据
    class Meta:
        # icon 定义了在 StreamField 编辑界面中该块显示的图标
        icon = "image"
        # template 定义了在前端渲染该块时使用的模板文件路径
        template = "base/blocks/captioned_image_block.html"


# 定义一个自定义的 StructBlock，用于创建带有不同大小选项的标题
class HeadingBlock(StructBlock):
	
    # 定义标题文本字段，设置为必填，并添加一个 CSS 类名 "title"
    heading_text = CharBlock(classname="title", required=True)

    # 定义标题大小选择字段，使用 ChoiceBlock 提供下拉选项
    size = ChoiceBlock(
        # choices 定义了下拉选项的列表，每个选项是一个元组 (值, 显示文本)
        choices=[
            ("", "选择标题大小"), # 默认选项
            ("h2", "H2"),
            ("h3", "H3"),
            ("h4", "H4"),
        ],
        blank=True,  # 允许为空白选择
        required=False, # 非必填
    )

    # Meta 类用于定义块的元数据
    class Meta:
        # icon 定义了在 StreamField 编辑界面中该块显示的图标
        icon = "title"
        # template 定义了在前端渲染该块时使用的模板文件路径
        template = "base/blocks/heading_block.html"


# 定义一个 BaseStreamBlock，它是一个 StreamField，包含多种不同类型的块
# 这个 StreamBlock 可以被添加到页面的字段中，允许编辑者自由组合这些块来构建内容
class BaseStreamBlock(StreamBlock):
	
    # 将 HeadingBlock 添加到 StreamField 中，命名为 heading_block
    heading_block = HeadingBlock()

    # 将 RichTextBlock 添加到 StreamField 中，命名为 paragraph_block，并指定图标
    paragraph_block = RichTextBlock(icon="pilcrow")

    # 将 CaptionedImageBlock 添加到 StreamField 中，命名为 image_block
    image_block = CaptionedImageBlock()

    # 将 EmbedBlock 添加到 StreamField 中，命名为 embed_block
    # 用于嵌入外部内容，提供帮助文本和图标
    embed_block = EmbedBlock(
        help_text="插入要嵌入的 URL。例如： https://www.youtube.com/watch?v=SGJFWirQ3ks",
        icon="media",
    )
