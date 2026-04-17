# protfolio/models.py
from wagtail.models import Page
from wagtail.fields import StreamField
from wagtail.admin.panels import FieldPanel

from portfolio.blocks import PortfolioStreamBlock

class PortfolioPage(Page):
    """作品集页面模型"""
    
    parent_page_types = ["home.HomePage"] # 限制此页面只能作为 HomePage 的子页面

    body = StreamField(
        PortfolioStreamBlock(),
        blank=True,
        use_json_field=True,
        help_text="使用此部分列出您的项目和技能。",
    )

    content_panels = Page.content_panels + [
        FieldPanel("body"),
    ]
    
    class Meta:
        verbose_name = "作品集页面"
        verbose_name_plural = "作品集页面"