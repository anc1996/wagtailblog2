# 作品集应用的 StreamField 块定义


from wagtail.blocks import (
    CharBlock,  # 用于单行文本输入
    ListBlock,  # 用于创建列表，其中每个项都是另一个块类型
    PageChooserBlock,  # 用于选择一个 Wagtail 页面
    RichTextBlock,  # 用于富文本编辑，支持格式化和链接
    StructBlock,  # 用于将多个块组合成一个结构化的块
)
from wagtail.images.blocks import ImageBlock  # 用于选择和显示图片

from base.blocks import BaseStreamBlock


# 定义一个名为 CardBlock 的结构化块
class CardBlock(StructBlock):
    """一个用于创建卡片内容的块，包含标题、文本和可选图片。"""
    
    # 定义卡片的标题字段，类型为 CharBlock
    heading = CharBlock()
    # 定义卡片的文本字段，类型为 RichTextBlock，支持粗体、斜体和链接
    text = RichTextBlock(features=["ai","bold", "italic", "link"])
    # 定义卡片的图片字段，类型为 ImageBlock，required=False 表示图片是可选的
    image = ImageBlock(required=False)

    # 定义块的元数据
    class Meta:
        # 设置块在 Wagtail 后台的图标，这有助于用户识别块类型
        icon = "form"
        # 指定渲染此块时使用的模板文件路径
        template = "portfolio/blocks/card_block.html"


# 定义一个名为 FeaturedPostsBlock 的结构化块
class FeaturedPostsBlock(StructBlock):
    """一个用于展示特色文章列表的块，包含标题、可选的介绍文本和文章列表。"""
    
    
    # 定义特色文章部分的标题字段，类型为 CharBlock
    heading = CharBlock()
    # 定义特色文章部分的介绍文本字段，类型为 RichTextBlock，可选
    text = RichTextBlock(features=["ai","bold", "italic", "link"], required=False)
    # 定义文章列表字段，类型为 ListBlock
    # ListBlock 中的每个项都是一个 PageChooserBlock，用于选择一个类型为 "blog.BlogPage" 的页面
    posts = ListBlock(PageChooserBlock(page_type="blog.BlogPage"))

    # 定义块的元数据
    class Meta:
        # 设置块在 Wagtail 后台的图标
        icon = "folder-open-inverse"
        # 指定渲染此块时使用的模板文件路径
        template = "portfolio/blocks/featured_posts_block.html"


# 定义作品集页面使用的 StreamBlock
# 继承基础内容块，复用通用内容类型并追加作品集专属块。
class PortfolioStreamBlock(BaseStreamBlock):
    """ 用于 Portfolio 页面或模型的主要 StreamField 的块定义。 """
   
    # 将卡片放入后台的“选项”分组。
    card = CardBlock(group="选项")
    # 将特色文章列表放入同一分组；featured_posts 是块在数据中的名称。
    featured_posts = FeaturedPostsBlock(group="选项")

