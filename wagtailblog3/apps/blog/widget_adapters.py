# Vditor 控件的 Telepath 适配器
from wagtail.admin.telepath import register
from wagtail.admin.telepath.widgets import WidgetAdapter

from .widgets import VditorMarkdownWidget


class VditorMarkdownWidgetAdapter(WidgetAdapter):
    """把 Django 控件映射到前端公开注册的 Vditor 构造器。"""
    js_constructor = "blog.widgets.VditorMarkdownWidget"


register(VditorMarkdownWidgetAdapter(), VditorMarkdownWidget)
