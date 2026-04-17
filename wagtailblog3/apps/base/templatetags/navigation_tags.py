# base/tamplatetags/navigation_tags.py
from django import template
from wagtail.models import Site

from base.models import FooterText

# 注册模板标签库
register = template.Library()

# 获取站点根页脚
@register.inclusion_tag("base/includes/footer_text.html", takes_context=True) # 注册了名为 get_footer_text 的包含标签的装饰器
# takes_context=True 表示该标签将接收上下文参数
def get_footer_text(context):
	
	# 获取上下文中的 footer_text
    footer_text = context.get("footer_text", "")

    if not footer_text:
	    
	    # 如果上下文中没有 footer_text，则从数据库中获取
        instance = FooterText.objects.filter(live=True).first()
        footer_text = instance.body if instance else ""

    return {
        "footer_text": footer_text,
    }

# 注册了一个名为 get_site_root 的简单模板标签。simple_tag 与 inclusion_tag 不同，它直接返回一个值，
# 而不是渲染一个模板。takes_context=True 表示标签函数会接收上下文作为参数。
@register.simple_tag(takes_context=True)
def get_site_root(context):
	"""获取当前请求的站点根页面"""
	return Site.find_for_request(context["request"]).root_page # 根据当前请求找到对应的网站对象。