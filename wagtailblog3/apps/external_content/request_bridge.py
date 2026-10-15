"""仅外置正文单页原生视图接入；不接管全站请求或工作流。"""

import copy
import hashlib
import json
from functools import wraps
from typing import Callable
from urllib.parse import urlsplit

from django import forms
from django.apps import apps
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import Resolver404, resolve, reverse
from django.utils.html import json_script
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from .idempotency import GuardError, IdempotencyService
from .idempotency_store import RedisIdempotencyStore, StoreUnavailable

KINDS = {"add": "create", "edit": "edit", "copy": "copy", "delete": "delete",
         "unpublish": "unpublish", "convert_alias": "convert_alias"}
CARRIERS = {"csrfmiddlewaretoken", "_ec_ticket", "_ec_token", "_ec_target"}
WORKFLOW_ACTIONS = {"action-submit", "action-restart-workflow",
                    "action-workflow-action", "action-cancel-workflow"}


def service() -> IdempotencyService:
    """延迟取得缓存，启动时不连接 Redis，也不降级到进程内缓存。"""
    return IdempotencyService(RedisIdempotencyStore())


def request_digest(request: HttpRequest) -> str:
    """保留正文原字节差异与多值次序，文件读后复位，不在缓存保存输入。"""
    fields = [(key, request.POST.getlist(key)) for key in sorted(request.POST)
              if key not in CARRIERS]
    query = [(key, request.GET.getlist(key)) for key in sorted(request.GET)
             if key not in CARRIERS]
    files = []
    for key in sorted(request.FILES):
        for upload in request.FILES.getlist(key):
            position = upload.tell()
            digest = hashlib.sha256()
            try:
                upload.seek(0)
                for chunk in upload.chunks():
                    digest.update(chunk)
            finally:
                upload.seek(position)
            files.append([key, upload.name, upload.content_type, upload.size, digest.hexdigest()])
    raw = ""
    if request.content_type not in {"multipart/form-data", "application/x-www-form-urlencoded"}:
        raw = hashlib.sha256(request.body).hexdigest()
    snapshot = [fields, query, files, raw, wants_json(request)]
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def wants_json(request: HttpRequest) -> bool:
    return "application/json" in request.headers.get("Accept", "")


def copy_requires_publish(request: HttpRequest) -> bool:
    """与 CopyForm 的复选框解析一致；发布复制或 Alias 均要求目标发布权限。"""
    field = forms.BooleanField(required=False)
    return any(field.clean(field.widget.value_from_datadict(request.POST, request.FILES, name))
               for name in ("publish_copies", "alias"))


def resolve_scope(request: HttpRequest, kind: str, kwargs: dict) -> dict | None:
    """先确定受控类型再复验当前权限；结果重放不绕过这道权限门禁。"""
    from wagtail.models import Page
    from .mixins import ExternalContentPageMixin
    if kind == "create":
        model = apps.get_model(kwargs["content_type_app_name"],
                               kwargs["content_type_model_name"])
        if not issubclass(model, ExternalContentPageMixin):
            return None
        parent = get_object_or_404(Page, pk=kwargs["parent_page_id"]).specific
        perms = parent.permissions_for_user(request.user)
        if (not perms.can_add_subpage() or not model.can_create_at(parent)
                or model not in parent.creatable_subpage_models()):
            raise PermissionDenied
        if request.POST.get("action-publish") and not perms.can_publish_subpage():
            raise PermissionDenied
        scope = {"kind": kind, "model": model._meta.label_lower, "parent": parent.pk}
    else:
        page = get_object_or_404(Page, pk=kwargs["page_id"]).specific
        if not isinstance(page, ExternalContentPageMixin):
            return None
        perms = page.permissions_for_user(request.user)
        permission = {"edit": "can_edit", "copy": "can_copy", "delete": "can_delete",
                      "unpublish": "can_unpublish", "convert_alias": "can_edit"}[kind]
        if not getattr(perms, permission)():
            raise PermissionDenied
        if kind != "copy" and request.POST.get("action-publish") and not perms.can_publish():
            raise PermissionDenied
        if kind == "copy":
            parent_id = request.POST.get("new_parent_page")
            parent = get_object_or_404(Page, pk=parent_id).specific if parent_id else page.get_parent()
            parent_perms = parent.permissions_for_user(request.user)
            if not parent_perms.can_add_subpage() or not perms.can_copy_to(parent):
                raise PermissionDenied
            if copy_requires_publish(request) and not parent_perms.can_publish_subpage():
                raise PermissionDenied
            request._ec_copy_parent_id = parent.pk
        scope = {"kind": kind, "page": page.pk}
    return scope


def error_response(request: HttpRequest, code: str, status: int = 409) -> HttpResponse:
    """安全错误仅提示核对结果，不输出原请求、签名或底层异常。"""
    return JsonResponse({"success": False, "error": "external_content_" + code,
                         "message": "请求未再次执行；请核对原页面或结果后再操作。"},
                        status=status)


def target_route(path: str) -> tuple[str, dict]:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise GuardError("target")
    match = resolve(parsed.path)
    if match.namespace != "wagtailadmin_pages" or match.url_name not in KINDS:
        raise GuardError("target")
    return KINDS[match.url_name], match.kwargs


@csrf_protect
@require_POST
def prepare_view(request: HttpRequest) -> HttpResponse:
    """与执行共用参数快照；CSRF 和原生权限均在签发前复核。"""
    if not request.user.is_authenticated:
        return error_response(request, "permission", 403)
    try:
        target = request.POST.get("_ec_target", "")
        kind, kwargs = target_route(target)
        if any(key in request.POST for key in WORKFLOW_ACTIONS):
            raise GuardError("unsupported")
        # 准备请求使用目标 URL 的 query；不能把准备 endpoint 的 query 当成原生输入。
        from django.http import QueryDict
        original_get = request.GET
        request.GET = QueryDict(urlsplit(target).query)
        try:
            scope = resolve_scope(request, kind, kwargs)
            if scope is None:
                raise GuardError("target")
            token = service().prepare(request.POST.get("_ec_ticket", ""),
                                      request.user.pk, scope, request_digest(request),
                                      authorization=authorization_requirement(request, kind))
        finally:
            request.GET = original_get
        return JsonResponse({"token": token})
    except PermissionDenied:
        return error_response(request, "permission", 403)
    except (GuardError, StoreUnavailable) as exc:
        return error_response(request, exc.code if isinstance(exc, GuardError) else "unavailable")


@csrf_protect
@require_POST
def status_view(request: HttpRequest) -> HttpResponse:
    """状态读取不重新领取，也不凭缓存记录授予页面权限。"""
    if not request.user.is_authenticated:
        return error_response(request, "permission", 403)
    try:
        status = service().status(request.POST.get("_ec_token", ""), request.user.pk)
        scope = status["scope"]
        kwargs = ({"page_id": scope["page"]} if "page" in scope else {
            "content_type_app_name": scope["model"].split(".")[0],
            "content_type_model_name": scope["model"].split(".")[1],
            "parent_page_id": scope["parent"],
        })
        # 状态请求只有载体，不能因此遗忘原 publish 或复制目标父页的授权要求。
        from django.http import QueryDict
        permission_request = copy.copy(request)
        permission_request.POST = QueryDict("", mutable=True)
        authorization = status["authorization"]
        if authorization["publish"]:
            field = "publish_copies" if scope["kind"] == "copy" else "action-publish"
            permission_request.POST[field] = "1"
        if "copy_parent" in authorization:
            permission_request.POST["new_parent_page"] = str(authorization["copy_parent"])
        if resolve_scope(permission_request, scope["kind"], kwargs) is None:
            raise PermissionDenied
        result = status["result"]
        if result:
            visible_result(request, result)
        return JsonResponse({"state": status["state"], "result": result})
    except PermissionDenied:
        return error_response(request, "permission", 403)
    except (GuardError, StoreUnavailable) as exc:
        return error_response(request, exc.code if isinstance(exc, GuardError) else "unavailable")


def authorization_requirement(request: HttpRequest, kind: str) -> dict:
    """缓存只登记最小权限要求；复制父页由服务端解析，而非缓存任意请求值。"""
    requirement = {"publish": bool(request.POST.get("action-publish"))
                   if kind in {"create", "edit"} else False}
    if kind == "copy":
        requirement["publish"] = copy_requires_publish(request)
        requirement["copy_parent"] = request._ec_copy_parent_id
    return requirement


def visible_result(request: HttpRequest, result: dict):
    """原目标权限与结果可访问性必须同时成立；删除后保守拒绝。"""
    from wagtail.models import Page
    page = get_object_or_404(Page, pk=result["page"]).specific
    if not page.permissions_for_user(request.user).can_edit():
        raise PermissionDenied
    return page


def replay_response(request: HttpRequest, result: dict) -> HttpResponse:
    """结果只核对身份；原生 GET 会写订阅/编辑会话，不能用于响应重建。"""
    page = visible_result(request, result)
    if result.get("json"):
        # 不冒充完整 autosave 成功，不发新 ticket 或覆盖浏览器未保存的新输入。
        response = JsonResponse({
            "success": False, "state": "succeeded",
            "error": "external_content_replay_unavailable",
            "result": {"page": page.pk, "revision": result["revision"]},
            "message": "原请求已提交；无法安全重建原响应，请核对页面与版本后再操作。",
        }, status=409)
        response["X-External-Content-Replayed"] = "1"
        return response
    response = HttpResponse(status=302)
    response["Location"] = reverse("wagtailadmin_pages:edit", args=(page.pk,))
    response["X-External-Content-Replayed"] = "1"
    return response


def result_summary(view: object, request: HttpRequest, response: HttpResponse,
                   scope: dict) -> dict | None:
    """只保存页面/Revision 等结果身份，不保存响应正文或表单数据。"""
    pk = getattr(request, "_ec_completed_page_id", None)
    if type(pk) is not int or pk <= 0:
        return None
    if wants_json(request):
        try:
            data = json.loads(response.content)
        except (ValueError, AttributeError):
            return None
        if (not isinstance(data, dict) or data.get("success") is not True
                or type(data.get("pk")) is not int or data["pk"] != pk
                or type(data.get("revision_id")) is not int or data["revision_id"] <= 0):
            return None
        updates = data.get("field_updates", {})
        # Wagtail 字段更新只记录 formset 主键/计数（最小结果身份），不缓存正文。
        if not isinstance(updates, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            or (value != "" and not value.isdecimal())
            for key, value in updates.items()
        ):
            return None
        return {"page": data["pk"], "revision": data["revision_id"], "json": True,
                "created": scope["kind"] == "create", "field_updates": updates}
    if response.status_code not in {302, 303}:
        return None
    return {"page": pk}


def attach_ticket(response: HttpResponse, request: HttpRequest, scope: dict, *,
                  unavailable: bool = False) -> HttpResponse:
    """复用不可用标记阻断未知尝试；此分支不预留新身份，也不改原生模板。"""
    marker = '<script type="application/json" data-external-content-guard>{"unavailable":true}</script>'
    if not unavailable:
        try:
            ticket = service().reserve(request.user.pk, scope)
            config = {"ticket": ticket, "target": request.get_full_path(),
                      "prepare": reverse("external_content:prepare"),
                      "status": reverse("external_content:status")}
            marker = str(json_script(config)).replace(
                "<script ", "<script data-external-content-guard ", 1)
        except (GuardError, StoreUnavailable):
            # 不可用时仍标记受控表单，让浏览器阻断而不是裸提交。
            pass

    def inject(rendered: HttpResponse) -> None:
        if not rendered.streaming:
            content = rendered.content.decode(rendered.charset)
            rendered.content = content.replace("</body>", str(marker) + "</body>", 1)
            if rendered.has_header("Content-Length"):
                del rendered["Content-Length"]
    if hasattr(response, "add_post_render_callback"):
        response.add_post_render_callback(inject)
    else:
        inject(response)
    response["Cache-Control"] = "private, no-store"
    return response


def track_form_valid(native: Callable) -> Callable:
    """记录进入保存动作的边界，后续再返回校验页也不能证明正文未写入。"""
    @wraps(native)
    def tracked(view: object, form: forms.BaseForm) -> HttpResponse:
        if getattr(view, "_ec_tracking_validation", False):
            view._ec_form_valid_entered = True
        return native(view, form)
    tracked._external_content_validation_guard = True
    return tracked


def track_form_invalid(native: Callable) -> Callable:
    """仅认可原生校验失败返回的同一个响应；锁定及工作流不作为无写入证明。"""
    @wraps(native)
    def tracked(view: object, form: forms.BaseForm) -> HttpResponse:
        response = native(view, form)
        if (getattr(view, "_ec_tracking_validation", False)
                and not getattr(view, "_ec_form_valid_entered", False)
                and not getattr(view, "locked_for_user", False)
                and not getattr(view, "is_cancelling_workflow", False)
                and not form.is_valid()):
            view._ec_validation_response = response
        return response
    tracked._external_content_validation_guard = True
    return tracked


def guard_dispatch(native: Callable, kind: str) -> Callable:
    """原生 dispatch 外围领取与事务桥；工作流不在受控首批接入范围。"""
    @wraps(native)
    def guarded(view, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        # 只接管冻结的原生 Web 路由，内部直接调用仍由既有生命周期围栏处理。
        # 不依赖可缺失的 resolver_match；真实路由即便直接构造请求也不能裸提交。
        try:
            route_kind, route_kwargs = target_route(request.path_info)
        except (GuardError, Resolver404):
            return native(view, request, *args, **kwargs)
        if route_kind != kind or route_kwargs != kwargs:
            return native(view, request, *args, **kwargs)
        scope = resolve_scope(request, kind, kwargs)
        if scope is None:
            return native(view, request, *args, **kwargs)
        if request.method == "POST" and any(key in request.POST for key in WORKFLOW_ACTIONS):
            # 原生 selector 会把无效或混合字段退化成保存/发布，不能据此允许裸执行。
            return error_response(request, "unsupported")
        if request.method != "POST":
            response = native(view, request, *args, **kwargs)
            if request.method == "GET" and response.status_code == 200:
                return attach_ticket(response, request, scope)
            return response
        token = request.POST.get("_ec_token", "")
        if not token:
            return error_response(request, "token")
        try:
            current_service = service()
            claim = current_service.claim(token, request.user.pk, scope, request_digest(request))
        except (GuardError, StoreUnavailable) as exc:
            return error_response(request, exc.code if isinstance(exc, GuardError) else "unavailable")
        if claim.replay:
            return replay_response(request, claim.record["result"])
        # 不把 dispatch 异常或 COMMIT 错误推断为跨存储整体回滚：
        # 旧围栏负责回滚，防重层保守 unknown，不自动再次执行。
        outcome = {"registered": False}
        try:
            view._ec_tracking_validation = kind in {"create", "edit"}
            view._ec_form_valid_entered = False
            view._ec_validation_response = None
            request._ec_expected_effect = (
                "publish" if kind in {"edit", "create"} and request.POST.get("action-publish")
                else kind)
            request._ec_source_page_id = scope.get("page")
            with transaction.atomic():
                response = native(view, request, *args, **kwargs)
                result = result_summary(view, request, response, scope)
                # 普通 200 或 hook 返回页不能证明未保存；只接受尚未进入动作的
                # 原生无效表单响应，且排除 JSON、锁定、工作流与完成标记。
                validation_rejected = (
                    kind in {"create", "edit"}
                    and response is view._ec_validation_response
                    and response.status_code == 200
                    and hasattr(response, "add_post_render_callback")
                    and not wants_json(request)
                    and not view._ec_form_valid_entered
                    and not getattr(request, "_ec_completed_page_id", None)
                )
                if result is not None:
                    def register_completion() -> None:
                        outcome["registered"] = current_service.complete(claim, result)
                    transaction.on_commit(register_completion)
                elif validation_rejected:
                    def register_validation_rejection() -> None:
                        outcome["registered"] = current_service.fail(claim, rollback_proven=True)
                    transaction.on_commit(register_validation_rejection)
                else:
                    transaction.on_commit(lambda: current_service.fail(claim))
        except Exception:
            current_service.fail(claim)
            raise
        if result is not None and result.get("json"):
            data = json.loads(response.content)
            if not outcome["registered"]:
                data["_ec_guard_unavailable"] = True
            else:
                try:
                    data["_ec_ticket"] = current_service.reserve(
                        request.user.pk, {"kind": "edit", "page": result["page"]})
                except (GuardError, StoreUnavailable):
                    data["_ec_guard_unavailable"] = True
            # 仅追加下一次非执行尝试身份，原生字段更新/HTML/评论完整保留。
            response.content = json.dumps(data).encode(response.charset)
            return response
        if result is not None and not outcome["registered"]:
            # 不撤销业务响应；显式区分业务完成与防重结果尚待核对。
            response["X-External-Content-Outcome"] = "unknown"
        if response.status_code == 200 and hasattr(response, "add_post_render_callback"):
            if validation_rejected and outcome["registered"]:
                # 旧身份仍绑定原输入；校验登记成功后另发身份供用户补填提交。
                return attach_ticket(response, request, scope)
            # before hook 可能在 Page/Revision 已写入后返回模板，200 不是无副作用证明。
            response["X-External-Content-Outcome"] = "unknown"
            return attach_ticket(response, request, scope, unavailable=True)
        return response
    guarded._external_content_request_guard = True
    return guarded


def install_request_bridge() -> None:
    """重复安装无副作用；只包裹明确原生类，不包裹动作/后台任务。"""
    from wagtail.admin.views.pages.create import CreateView
    from wagtail.admin.views.pages.edit import EditView
    from wagtail.admin.views.pages.copy import CopyView
    from wagtail.admin.views.pages.delete import DeleteView
    from wagtail.admin.views.pages.unpublish import UnpublishView
    from wagtail.admin.views.pages.convert_alias import ConvertAliasView
    for view_class in (CreateView, EditView):
        if not getattr(view_class.form_valid, "_external_content_validation_guard", False):
            view_class.form_valid = track_form_valid(view_class.form_valid)
        if not getattr(view_class.form_invalid, "_external_content_validation_guard", False):
            view_class.form_invalid = track_form_invalid(view_class.form_invalid)
    for view_class, kind in [(CreateView, "create"), (EditView, "edit"), (CopyView, "copy"),
                             (DeleteView, "delete"), (UnpublishView, "unpublish"),
                             (ConvertAliasView, "convert_alias")]:
        if not getattr(view_class.dispatch, "_external_content_request_guard", False):
            view_class.dispatch = guard_dispatch(view_class.dispatch, kind)
