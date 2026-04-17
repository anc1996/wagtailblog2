# search/templatetags/search_tags.py
from django import template

register = template.Library()

@register.filter
def verbose_name(obj):
    """获取模型的友好名称"""
    if hasattr(obj, 'content_type'):
        return obj.content_type.name
    elif hasattr(obj, '_meta'):
        return obj._meta.verbose_name
    return obj.__class__.__name__


@register.filter
def model_name(obj):
    """获取模型名称"""
    if hasattr(obj, 'content_type'):
        return obj.content_type.model
    elif hasattr(obj, '_meta'):
        return obj._meta.model_name
    return obj.__class__.__name__.lower()