"""
专题指南与导航菜单生命周期信号处理器。

监听 Wagtail 核心事件（发布、下架、移动、别名、权限变更），
自动触发全站主导航菜单缓存代次递增与精准键失效，
保障前后台一致性，无需手动干预。
"""

from typing import Any, Optional
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from wagtail.models import Page, PageViewRestriction
from wagtail.signals import page_published, page_unpublished, post_page_move, page_slug_changed

from guides.services.nav_cache import invalidate_nav_menu_cache


@receiver(page_published)
def on_page_published(sender: Any, instance: Page, **kwargs: Any) -> None:
    """页面发布成功信号：自动触发主导航缓存收敛。"""
    invalidate_nav_menu_cache(instance)


@receiver(page_unpublished)
def on_page_unpublished(sender: Any, instance: Page, **kwargs: Any) -> None:
    """页面下架信号：自动触发主导航缓存收敛。"""
    invalidate_nav_menu_cache(instance)


@receiver(post_page_move)
def on_post_page_move(sender: Any, instance: Page, **kwargs: Any) -> None:
    """页面在树中移动信号：自动触发主导航缓存收敛。"""
    invalidate_nav_menu_cache(instance)


@receiver(page_slug_changed)
def on_page_slug_changed(sender: Any, instance: Page, **kwargs: Any) -> None:
    """页面 slug 变更信号：自动触发主导航缓存收敛。"""
    invalidate_nav_menu_cache(instance)


@receiver(post_save, sender=PageViewRestriction)
def on_page_view_restriction_saved(sender: Any, instance: PageViewRestriction, **kwargs: Any) -> None:
    """页面权限或隐私限制设置信号：自动触发主导航缓存收敛。"""
    if getattr(instance, "page", None):
        invalidate_nav_menu_cache(instance.page)


@receiver(post_delete, sender=PageViewRestriction)
def on_page_view_restriction_deleted(sender: Any, instance: PageViewRestriction, **kwargs: Any) -> None:
    """页面权限或隐私限制解除信号：自动触发主导航缓存收敛。"""
    if getattr(instance, "page", None):
        invalidate_nav_menu_cache(instance.page)
