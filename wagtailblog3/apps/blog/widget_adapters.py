from wagtail.admin.telepath import register
from wagtail.admin.telepath.widgets import WidgetAdapter

from .widgets import VditorMarkdownWidget


class VditorMarkdownWidgetAdapter(WidgetAdapter):
    js_constructor = "blog.widgets.VditorMarkdownWidget"


register(VditorMarkdownWidgetAdapter(), VditorMarkdownWidget)
