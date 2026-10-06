"""
专题指南应用的 Wagtail 后台扩展、安全门禁与导航缓存治理。

包含：
1. 单页正文外置复制与删除的受控后台入口；
2. 批量删除与批量发布入口的 Fail-Closed 安全门禁；
3. 严格遵循 Fail-Closed（失败关闭）安全原则：后代外置属性核查异常一律阻断；
4. 页面列表按钮与编辑页 Action 菜单的受控过滤；
5. 页面发布、下架、编辑、移动与删除后的 main_nav_menu 全局导航缓存 O(1) 瞬时收敛。
"""

from typing import Any, Dict, List, Optional, Type

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from wagtail import hooks
from wagtail.models import Page, get_page_models

from external_content.exceptions import PublicationRevisionInvalidError
from external_content.mixins import ExternalContentPageMixin
from external_content.publication import ExternalContentPublicationService
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
        _is_external_content_page(page)
        has_ext_desc = _has_external_content_descendants(page)
    except Exception as exc:
        messages.error(
            request,
            f"页面「{page.title}」外置属性探测异常（已激活安全熔断阻断复制）：{exc}"
        )
        parent_id = page.get_parent().id if page.get_parent() else page.id
        return redirect(reverse("wagtailadmin_explore", args=[parent_id]))

    # G4 阶段：单页复制已全面通过受控 CopyPageAction 落地支持，放开视图层单页阻断；
    # 仅当后代子树中包含正文外置页面时，继续维持递归复制的安全阻断。
    if has_ext_desc:
        messages.error(
            request,
            f"页面「{page.title}」的后代子树中包含正文外置页面。递归复制已被安全阻断。"
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

    if has_ext_desc:
        messages.error(
            request,
            f"页面「{page.title}」的后代子树中包含正文外置页面。递归删除已被安全阻断；请逐个删除正文外置页面，以便固化安全删除意图。"
        )
        parent_id = page.get_parent().id if page.get_parent() else page.id
        return redirect(reverse("wagtailadmin_explore", args=[parent_id]))

    return None


@hooks.register("before_bulk_action")
def intercept_external_page_bulk_action(
    request: HttpRequest,
    action_type: str,
    objects: List[Any],
    action: Any,
) -> Optional[HttpResponse]:
    """
    后台批量操作安全门禁：
    1. 针对 delete：允许单个正文外置页面进入 DeletionIntent 协议，仅拦截包含正文外置后代的递归删除；
    2. 针对 publish：锁定并校验真正待发布的 latest Revision，聚合错误并整批回滚，避免 Revision 漂移和 500。
    """
    if action_type == "delete":
        for obj in objects:
            try:
                # 单页外置正文删除由 external_content.pre_delete 固化安全删除意图；
                # 只有父节点递归删除才会一次性影响多个正文版本，必须 Fail-Closed。
                is_blocked = _has_external_content_descendants(obj)
            except Exception as exc:
                raise PermissionDenied(
                    f"所选页面「{getattr(obj, 'title', '')}」外置属性探测异常（Fail-Closed 熔断）：{exc}"
                )
            if is_blocked:
                raise PermissionDenied(
                    "所选页面包含正文外置后代，递归删除已被安全阻断；请逐个处理正文外置页面。"
                )

    elif action_type == "publish":
        validation_errors: List[str] = []
        try:
            target_pages = _get_bulk_publish_targets(request, objects, action)
        except Exception as exc:
            messages.error(
                request,
                f"批量发布已取消，无法安全确定发布目标：{_format_publish_validation_error(exc)}",
            )
            return redirect(getattr(action, "next_url", None) or "/admin/pages/")

        # BulkAction.form_valid 已经建立外层 transaction.atomic；这里使用保存点，
        # 确保校验期间更新的 draft_manifest 在发现任一不合规项时整体回滚。
        try:
            with transaction.atomic():
                for page in target_pages:
                    page_id = getattr(page, "pk", "?")
                    page_title = getattr(page, "title", "")
                    try:
                        if not _is_external_content_page(page):
                            continue

                        specific_page = getattr(page, "specific", page)
                        page_id = specific_page.pk
                        page_title = specific_page.title
                        revision = specific_page.get_latest_revision()
                        if revision is None:
                            validation_errors.append(
                                f"ID {page_id}「{page_title}」：没有可发布的 Revision，请先保存草稿。"
                            )
                            continue

                        candidate = ExternalContentPublicationService.lock_and_validate_revision(
                            int(page_id),
                            int(revision.pk),
                            store=getattr(specific_page, "_test_content_store", None),
                        )
                        locked_revision = candidate.revision
                        latest_after_lock = specific_page.get_latest_revision()
                        if latest_after_lock is None or latest_after_lock.pk != locked_revision.pk:
                            raise PublicationRevisionInvalidError(
                                "校验期间 latest Revision 发生变化，请重新加载页面后重试。"
                            )
                        # 后续原生发布动作重新读取 latest Revision；生命周期锁保证其不会漂移。
                        setattr(specific_page, "_external_content_locked_revision_id", locked_revision.pk)
                    except Exception as exc:
                        validation_errors.append(
                            f"ID {page_id}「{page_title}」：{_format_publish_validation_error(exc)}"
                        )

                if validation_errors:
                    raise _BulkPublishValidationFailed
        except _BulkPublishValidationFailed:
            summary = "；".join(validation_errors)
            messages.error(
                request,
                f"批量发布已取消，未执行任何页面（共 {len(validation_errors)} 项不合规）：{summary}",
            )
            return redirect(getattr(action, "next_url", None) or "/admin/pages/")

    return None


class _BulkPublishValidationFailed(Exception):
    """内部保存点异常：让批量发布校验副作用回滚而不产生 500。"""


def _format_publish_validation_error(exc: Exception) -> str:
    """将发布校验异常压缩为后台可读信息，避免向管理员暴露堆栈。"""
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _get_bulk_publish_targets(
    request: HttpRequest,
    objects: List[Any],
    action: Any,
) -> List[Any]:
    """
    计算与 Wagtail PublishBulkAction 一致的发布目标集合。

    include_descendants 由批量动作表单控制；这里只加入有发布权限且尚未 live 的后代，
    保证校验集合与随后原生执行集合一致，避免父页检查通过而子页在执行阶段才 500。
    """
    targets: Dict[int, Any] = {}
    for page in objects:
        targets[int(page.pk)] = page

    cleaned_form = getattr(action, "cleaned_form", None)
    include_descendants = bool(
        cleaned_form
        and getattr(cleaned_form, "cleaned_data", {}).get("include_descendants")
    )
    if not include_descendants:
        return [targets[page_id] for page_id in sorted(targets)]

    user = getattr(request, "user", None)
    for parent in objects:
        descendants = (
            parent.get_descendants()
            .not_live()
            .defer_streamfields()
            .specific()
            .iterator()
        )
        for descendant in descendants:
            if user is None or descendant.permissions_for_user(user).can_publish():
                targets[int(descendant.pk)] = descendant

    return [targets[page_id] for page_id in sorted(targets)]


# 保持向后兼容别名
intercept_external_page_bulk_delete = intercept_external_page_bulk_action


@hooks.register("construct_page_listing_buttons")
def filter_external_page_listing_buttons(
    buttons: List[Any],
    page: Any,
    user: Optional[Any] = None,
    context: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> None:
    """
    在页面列表页（Explorer）受控过滤操作按钮：
    - 单页正文外置页面保留 copy/delete 操作，由底层协议负责安全性；
    - 包含正文外置后代的父页面隐藏 delete，避免递归删除误操作。
    兼容 Wagtail 8.0 钩子签名 (buttons, page, user, context)。
    """
    if _is_external_content_page(page):
        # 单页外置正文允许显示删除入口，由底层 pre_delete 固化 DeletionIntent。
        return
    if _has_external_content_descendants(page):
        # 1. 过滤顶层列表按钮
        buttons[:] = [b for b in buttons if getattr(b, "name", None) != "delete"]
        # 2. Wagtail 8.0 页面操作多折叠在 ButtonWithDropdownFromHook，深入过滤下拉子项中的删除按钮
        for b in buttons:
            if hasattr(b, "dropdown_buttons"):
                b.dropdown_buttons = [
                    d for d in b.dropdown_buttons
                    if getattr(d, "name", None) != "delete" and "/delete/" not in getattr(d, "url", "")
                ]


@hooks.register("construct_page_action_menu")
def filter_external_page_action_menu(
    menu_items: List[Any],
    request: HttpRequest,
    context: dict[str, Any],
) -> None:
    """
    在页面编辑页底部动作菜单中仅隐藏包含正文外置后代的父页面 delete 操作项。
    """
    page = context.get("page")
    if _is_external_content_page(page):
        # 单页外置正文允许进入受控删除协议。
        return
    if _has_external_content_descendants(page):
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
