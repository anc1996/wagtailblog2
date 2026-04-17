# wagtailblog3/apps/blog/templatetags/debug_tags.py
from django import template
from django.conf import settings
import json

register = template.Library()

@register.simple_tag
def debug_markdown_settings():
    """
    这个标签会获取当前运行的 WAGTAILMARKDOWN 配置，
    并以易于阅读的格式打印出来，用于调试。
    """
    print("--- DEBUG: WAGTAILMARKDOWN SETTINGS ---")
    try:
        # 尝试获取配置
        current_config = getattr(settings, 'WAGTAILMARKDOWN', {})
        # 格式化后打印到运行服务器的控制台
        pretty_config = json.dumps(current_config, indent=2)
        print(pretty_config)
        print("--- END DEBUG ---")
        # 你也可以选择在页面上显示，但打印到控制台更干净
        # from django.utils.safestring import mark_safe
        # return mark_safe(f"<pre>{pretty_config}</pre>")
    except Exception as e:
        print(f"Error getting WAGTAILMARKDOWN settings: {e}")

    return "" # 不在页面上输出任何东西