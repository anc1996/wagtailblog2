"""外置正文删除意图的受控后台入口。"""

from __future__ import annotations

import logging
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from wagtail.models import Page

from .deletion import cancel_pending_deletion_intent
from .models import ExternalContentDeletionIntent

logger = logging.getLogger(__name__)
SPECIAL_PERMISSION = "external_content.change_externalcontentpublicationstate"


def _get_page_and_intent(request: HttpRequest) -> tuple[Page, ExternalContentDeletionIntent]:
    try:
        page_id = int(request.GET.get("page_id") or request.POST.get("page_id") or "0")
    except (TypeError, ValueError) as exc:
        raise PermissionDenied("页面参数无效") from exc
    intent_id = request.GET.get("intent_id") or request.POST.get("intent_id")
    if page_id <= 0 or not intent_id:
        raise PermissionDenied("必须同时提供 page_id 和 intent_id")
    page = get_object_or_404(Page, pk=page_id).specific
    intent = get_object_or_404(ExternalContentDeletionIntent, pk=intent_id, page_id=page_id)
    return page, intent


def _can_cancel(request: HttpRequest, page: Page) -> bool:
    tester = page.permissions_for_user(request.user)
    return bool(tester.can_edit() and (request.user.is_superuser or request.user.has_perm(SPECIAL_PERMISSION)))


@login_required
@require_http_methods(["GET", "POST"])
def cancel_deletion_intent_view(request: HttpRequest) -> HttpResponse:
    """展示并受控取消一个明确指定的删除意图。"""
    page, intent = _get_page_and_intent(request)
    if not _can_cancel(request, page):
        raise PermissionDenied("没有取消该页面删除意图所需的权限")

    if request.method == "GET":
        return render(
            request,
            "external_content/cancel_deletion_intent.html",
            {"page": page, "intent": intent, "request_id": uuid.uuid4()},
        )

    request_id = request.POST.get("request_id", "").strip()
    if not request_id or len(request_id) > 128:
        messages.error(request, "缺少有效的幂等请求编号，未执行取消操作。")
        return redirect(_page_admin_url(page))

    if str(intent.status) == "cancelled":
        logger.info(
            "external_content_deletion_cancel_idempotent page_id=%s intent_id=%s actor_id=%s request_id=%s",
            page.pk,
            intent.pk,
            request.user.pk,
            request_id,
        )
        messages.info(request, "该删除意图已经取消，页面写入冻结已解除。")
        return redirect(_page_admin_url(page))

    try:
        cancelled = cancel_pending_deletion_intent(intent.pk)
    except Exception as exc:
        logger.warning(
            "external_content_deletion_cancel_failed page_id=%s intent_id=%s actor_id=%s request_id=%s error_type=%s",
            page.pk,
            intent.pk,
            request.user.pk,
            request_id,
            type(exc).__name__,
        )
        messages.error(request, f"删除意图未取消：{type(exc).__name__}")
        return redirect(_page_admin_url(page))

    logger.info(
        "external_content_deletion_cancelled page_id=%s intent_id=%s actor_id=%s request_id=%s new_status=%s",
        page.pk,
        cancelled.pk,
        request.user.pk,
        request_id,
        cancelled.status,
    )
    messages.success(request, f"删除意图 {cancelled.pk} 已取消，页面写入冻结已解除。")
    return redirect(_page_admin_url(page))


def _page_admin_url(page: Page) -> str:
    return reverse("wagtailadmin_pages:edit", args=[page.pk])
