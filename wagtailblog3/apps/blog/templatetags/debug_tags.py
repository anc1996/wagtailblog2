# wagtailblog3/apps/blog/templatetags/debug_tags.py
import json
import logging

from django import template
from django.conf import settings


logger = logging.getLogger(__name__)

register = template.Library()

@register.simple_tag
def debug_markdown_settings():
    """
    这个标签会获取当前运行的 WAGTAILMARKDOWN 配置，
    并以易于阅读的格式打印出来，用于调试。
    """
    try:
        # 尝试获取配置
        current_config = getattr(settings, 'WAGTAILMARKDOWN', {})
        # 格式化后写入 blog 活动日志。
        pretty_config = json.dumps(current_config, indent=2)
        logger.debug("WAGTAILMARKDOWN settings:\n%s", pretty_config)
    except Exception as e:
        logger.exception("Error getting WAGTAILMARKDOWN settings: %s", e)

    return "" # 不在页面上输出任何东西
