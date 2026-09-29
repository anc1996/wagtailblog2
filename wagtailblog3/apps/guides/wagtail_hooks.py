"""
专题指南应用的 Wagtail 后台扩展、安全门禁与导航缓存治理。

包含：
1. G4 落地前的复制（copy）与删除（delete）后台入口阻断（覆盖直接操作与包含任意正文外置子页的父页面）；
2. 批量删除（bulk_action）入口针对正文外置页面及后代外置页面的安全熔断；
3. 严格遵循 Fail-Closed（失败关闭）安全原则：后代外置属性核查异常一律阻断；
4. 页面列表按钮与编辑页 Action 菜单的受控过滤；
5. 页面发布、下架、编辑、移动与删除后的 main_nav_menu 全局导航缓存 O(1) 瞬时收敛。
"""

from typing import Any, List, Optional, Type

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from wagtail import hooks
from wagtail.models import Page, get_page_models

from external_content.mixins import ExternalContentPageMixin
from guides.services.nav_cache import invalidate_nav_menu_cache


def _get_external_page_models() -> List[Type[Page]]:
    """
    动态获取系统中所有继承 ExternalContentPageMixin 或显式声明 content_storage 的页面模型类。
    """
    external_models: List[Type[Page]] = []
    for model in get_page_models():
        if issubclass(model, ExternalContentPageMixin) or getattr(model, "content_storage", None):
            external_models.append(model)
    return external_models


def _is_external_content_page(page: Any) -> bool:
    """
    判断页面是否属于正文外置页面（如 GuidePage 或具有 content_storage / 继承 Mixin 的页面）。
    """
    if not page:
        return False
    specific_class = getattr(page, "specific_class", None)
    if specific_class:
        if issubclass(specific_class, ExternalContentPageMixin) or getattr(specific_class, "content_storage", None):
            return True
    return isinstance(page, ExternalContentPageMixin) or bool(getattr(page, "content_storage", None))


def _has_external_content_descendants(page: Any) -> bool:
    """
    通用递归检查：指定页面的后代子树中是否包含任何正文外置页面模型（包括 GuidePage 及其他外置子类）。
    遵循严格失败关闭（Fail-Closed）原则：任何检查异常不得静默放行。
    """
    if not page or not hasattr(page, "get_descendants"):
        return False
    descendants = page.get_descendants()
    ext_models = _get_external_page_models()
    for ext_model in ext_models:
        if descendants.type(ext_model).exists():
            return True
    return False


@hooks.register("before_copy_page")
def intercept_external_page_copy(request: HttpRequest, page: Any) -> Optional[HttpResponse]:
    """
    G4 前置保护：在后台视图层安全拦截正文外置页面或含有外置子页的父页面的复制请求。
    遵循 Fail-Closed 原则：属性探测异常时同样予以阻断。
    """
    try:
        is_ext = _is_external_content_page(page)
        has_ext_desc = _has_external_content_descendants(page)
    except Exception as exc:
        messages.error(
            request,
            f"页面「{page.title}」外置属性探测异常（已激活安全熔断阻断复制）：{exc}"
        )
        parent_id = page.get_parent().id if page.get_parent() else page.id
        return redirect(reverse("wagtailadmin_explore", args=[parent_id]))

    if is_ext:
        messages.error(
            request,
            f"页面「{page.title}」正文外置通用协议 G4 阶段尚未就绪，为保障独立版本身份与引用完整性，复制操作已被安全阻断。"
        )
        parent_id = page.get_parent().id if page.get_parent() else page.id
        return redirect(reverse("wagtailadmin_explore", args=[parent_id]))

    if has_ext_desc:
        messages.error(
            request,
            f"页面「{page.title}」的后代子树中包含正文外置页面。G4 阶段就绪前，递归复制已被安全阻断。"
        )
        parent_id = page.get_parent().id if page.get_parent() else page.id
        return redirect(reverse("wagtailadmin_explore", args=[parent_id]))

    return None


@hooks.register("before_delete_page")
def intercept_external_page_delete(request: HttpRequest, page: Any) -> Optional[HttpResponse]:
    """
    G4 前置保护：在后台视图层安全拦截正文外置页面或含有外置子页的父页面的删除请求。
    遵循 Fail-Closed 原则：属性探测异常时同样予以阻断。
    """
    try:
        is_ext = _is_external_content_page(page)
        has_ext_desc = _has_external_content_descendants(page)
    except Exception as exc:
        messages.error(
            request,
            f"页面「{page.title}」外置属性探测异常（已激活安全熔断阻断删除）：{exc}"
        )
        parent_id = page.get_parent().id if page.get_parent() else page.id
        return redirect(reverse("wagtailadmin_explore", args=[parent_id]))

    if is_ext:
        messages.error(
            request,
            f"页面「{page.title}」正文外置通用协议 G4 阶段尚未就绪，为保障延迟回收清单与引用计数保护，删除操作已被安全阻断。"
        )
        parent_id = page.get_parent().id if page.get_parent() else page.id
        return redirect(reverse("wagtailadmin_explore", args=[parent_id]))

    if has_ext_desc:
        messages.error(
            request,
            f"页面「{page.title}」的后代子树中包含正文外置页面。G4 阶段就绪前，递归删除已被安全阻断。"
        )
        parent_id = page.get_parent().id if page.get_parent() else page.id
        return redirect(reverse("wagtailadmin_explore", args=[parent_id]))

    return None


@hooks.register("before_bulk_action")
def intercept_external_page_bulk_delete(
    request: HttpRequest,
    action_type: str,
    objects: List[Any],
    action: Any,
) -> None:
    """
    G4 前置保护：在批量操作层拦截针对正文外置页面及包含外置子页的页面的删除行为。
    遵循 Fail-Closed 原则：属性探测异常时同样抛出 PermissionDenied 熔断。
    """
    if action_type == "delete":
        for obj in objects:
            try:
                is_blocked = _is_external_content_page(obj) or _has_external_content_descendants(obj)
            except Exception as exc:
                raise PermissionDenied(
                    f"所选页面「{getattr(obj, 'title', '')}」外置属性探测异常（Fail-Closed 熔断）：{exc}"
                )
            if is_blocked:
                raise PermissionDenied(
                    "所选页面或其后代中包含正文外置页面。G4 阶段就绪前，删除操作已被安全阻断。"
                )


@hooks.register("construct_page_listing_buttons")
def filter_external_page_listing_buttons(
    buttons: List[Any],
    page: Any,
    page_perms: Any,
    is_parent: bool = False,
    context: Optional[dict[str, Any]] = None,
) -> None:
    """
    在页面列表页（Explorer）过滤移除正文外置页面的 copy 与 delete 操作按钮。
    """
    if is_parent:
        return
    if _is_external_content_page(page):
        buttons[:] = [b for b in buttons if getattr(b, "name", None) not in ("copy", "delete")]


@hooks.register("construct_page_action_menu")
def filter_external_page_action_menu(
    menu_items: List[Any],
    request: HttpRequest,
    context: dict[str, Any],
) -> None:
    """
    在页面编辑页底部动作菜单中移除针对正文外置页面的 delete 操作项。
    """
    page = context.get("page")
    if _is_external_content_page(page):
        menu_items[:] = [item for item in menu_items if getattr(item, "name", None) != "delete"]


@hooks.register("after_publish_page")
def invalidate_nav_on_publish(request: HttpRequest, page: Any) -> None:
    """页面发布成功后自动失效导航缓存。"""
    invalidate_nav_menu_cache(page)


@hooks.register("after_unpublish_page")
def invalidate_nav_on_unpublish(request: HttpRequest, page: Any) -> None:
    """页面下线后自动失效导航缓存。"""
    invalidate_nav_menu_cache(page)


@hooks.register("after_move_page")
def invalidate_nav_on_move(request: HttpRequest, page: Any) -> None:
    """页面移动后自动失效导航缓存。"""
    invalidate_nav_menu_cache(page)


@hooks.register("after_delete_page")
def invalidate_nav_on_delete(request: HttpRequest, page: Any) -> None:
    """页面删除后自动失效导航缓存。"""
    invalidate_nav_menu_cache(page)


@hooks.register("after_edit_page")
def invalidate_nav_on_edit(request: HttpRequest, page: Any) -> None:
    """页面后台编辑保存后自动失效导航缓存。"""
    invalidate_nav_menu_cache(page)
