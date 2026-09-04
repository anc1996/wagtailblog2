"""Wagtail 系统日志后台视图。"""

from __future__ import annotations

import uuid
from datetime import datetime

from django.contrib import messages
from django.core.paginator import Paginator
from django.core import signing
from django.db.models import Q
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView
from wagtail.admin.auth import require_admin_access

from .forms import (
    LogClearForm,
    LogClearSelectionForm,
    LogFilterForm,
    confirmation_text,
)
from .models import LogClearAudit
from .permissions import MANAGE_PERMISSION, VIEW_PERMISSION, require_log_permission
from .pagination import read_log_page
from .registry import LOG_DOMAIN_KEYS, LOG_FILE_BY_KEY
from .services import clear_and_audit, describe_clear, get_overview, select_clear_specs


CLEAR_PREVIEW_SALT = "observability.clear-preview.v1"
CLEAR_PREVIEW_MAX_AGE = 15 * 60


def _selection_data(cleaned_data: dict) -> dict[str, str]:
    return {
        "target_type": cleaned_data["target_type"],
        "target": cleaned_data.get("target", ""),
        "kind": cleaned_data.get("kind", ""),
        "scope": cleaned_data["scope"],
    }


def _target_label(selection: dict[str, str]) -> str:
    target_type = selection["target_type"]
    target = selection["target"]
    if target_type == "file":
        spec = LOG_FILE_BY_KEY.get(target)
        return spec.label if spec else target
    if target_type == "domain":
        return f"{target} 模块"
    if target_type == "business":
        return "全部业务日志"
    return "全部运行日志"


def _issue_preview(selection: dict[str, str]) -> tuple[uuid.UUID, str]:
    idempotency_key = uuid.uuid4()
    token = signing.dumps(
        {**selection, "idempotency_key": str(idempotency_key)},
        salt=CLEAR_PREVIEW_SALT,
        compress=True,
    )
    return idempotency_key, token


def _preview_payload(selection: dict[str, str], specs) -> dict:
    preview = describe_clear(specs, **selection)
    idempotency_key, preview_token = _issue_preview(selection)
    preview.update(
        {
            "target_label": _target_label(selection),
            "kind_label": {"": "全部", "activity": "活动日志", "error": "错误日志"}[
                selection["kind"]
            ],
            "scope_label": {
                "current": "仅当前文件",
                "rotated": "仅轮转历史",
                "all": "当前及轮转历史",
            }[selection["scope"]],
            "confirmation_text": confirmation_text(selection["target_type"]),
            "idempotency_key": str(idempotency_key),
            "preview_token": preview_token,
        }
    )
    return preview


@method_decorator(require_admin_access, name="dispatch")
@method_decorator(never_cache, name="dispatch")
class LogAdminView(TemplateView):
    """统一处理后台访问权限和页面通用上下文。"""

    page_title = "系统日志"

    def dispatch(self, request, *args, **kwargs):
        require_log_permission(request, VIEW_PERMISSION)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "page_title": self.page_title,
                "can_manage_logs": self.request.user.has_perm(MANAGE_PERMISSION),
                "log_domains": LOG_DOMAIN_KEYS,
            }
        )
        return context


class LogOverviewView(LogAdminView):
    template_name = "observability/admin/overview.html"
    page_title = "日志概览"

    def get(self, request, *args, **kwargs):
        # 消费一次性刷新标记后规范化 URL，使浏览器刷新回到普通缓存概览，
        # 避免每次浏览器刷新都重复触发全量统计。
        if request.GET.get("refresh") == "1":
            get_overview(refresh=True)
            return HttpResponseRedirect(reverse("observability:overview"))
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({"overview": get_overview(refresh=self.request.GET.get("refresh") == "1"), "refresh": self.request.GET.get("refresh", "0")})
        return context


class LogRecordsView(LogAdminView):
    template_name = "observability/admin/records.html"
    page_title = "日志记录"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        defaults = {"kind": "error", "period": "24h", "page_size": 100, "page": 1}
        query_data = defaults.copy()
        query_data.update(self.request.GET.dict())
        form = LogFilterForm(query_data)
        result = None
        # 读取、筛选和分页均复用同一份表单清洗结果，避免 GET 参数在不同层重复解释。
        if form.is_valid():
            try:
                filters = {
                    "domain": form.cleaned_data["domain"],
                    "kind": form.cleaned_data["kind"],
                    "level": form.cleaned_data["level"],
                    "keyword": form.cleaned_data["keyword"],
                    "since": form.since(),
                    "until": form.until(),
                    "include_rotated": form.cleaned_data["include_rotated"],
                }
                result = read_log_page(
                    owner_id=self.request.user.pk,
                    requested_page=form.cleaned_data.get("page") or 1,
                    page_size=form.cleaned_data["page_size"],
                    session_token=form.cleaned_data.get("page_session", ""),
                    filters=filters,
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
        query = self.request.GET.copy()
        query.pop("page", None)
        query.pop("page_session", None)
        context.update(
            {
                "filter_form": form,
                "records": result.records if result else [],
                "bytes_read": result.bytes_read if result else 0,
                "base_query": query.urlencode(),
                "log_page": result,
            }
        )
        return context


class LogAuditView(LogAdminView):
    template_name = "observability/admin/audits.html"
    page_title = "清理记录"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.copy()
        audits = LogClearAudit.objects.select_related("user")
        filters = {
            "user": query.get("user", "").strip(),
            "ip": query.get("ip", "").strip(),
            "target": query.get("target", "").strip(),
            "module": query.get("module", "").strip(),
            "file": query.get("file", "").strip(),
            "target_type": query.get("target_type", "").strip(),
            "kind": query.get("kind", "").strip(),
            "scope": query.get("scope", "").strip(),
            "state": query.get("state", "").strip(),
            "index_sync_state": query.get("index_sync_state", "").strip(),
            "q": query.get("q", "").strip(),
            "start": query.get("start", "").strip(),
            "end": query.get("end", "").strip(),
        }
        if filters["user"]:
            audits = audits.filter(Q(user__username__icontains=filters["user"]) | Q(user__email__icontains=filters["user"]))
        if filters["ip"]:
            audits = audits.filter(ip_address__icontains=filters["ip"])
        for field in ("target_type", "kind", "scope", "state"):
            if filters[field] in {"activity", "error", "current", "rotated", "all", "completed", "partial", "failed", "file", "domain", "business", "legacy"}:
                audits = audits.filter(**{field: filters[field]})
        if filters["index_sync_state"] in {
            "not_required",
            "pending",
            "running",
            "completed",
            "partial",
            "failed",
            "dead_letter",
        }:
            audits = audits.filter(index_sync_state=filters["index_sync_state"])
        if filters["target"]:
            audits = audits.filter(target__icontains=filters["target"])
        if filters["module"]:
            audits = audits.filter(target__icontains=f"domain:{filters['module']}:")
        if filters["file"]:
            audits = audits.filter(target__icontains=f"file:{filters['file']}:")
        if filters["q"]:
            audits = audits.filter(Q(target__icontains=filters["q"]) | Q(details__icontains=filters["q"]))
        for name, lookup in (("start", "created_at__gte"), ("end", "created_at__lte")):
            if filters[name]:
                try:
                    value = datetime.fromisoformat(filters[name])
                    if name == "end":
                        value = value.replace(hour=23, minute=59, second=59)
                    audits = audits.filter(**{lookup: value})
                except ValueError:
                    filters[name] = ""
        paginator = Paginator(audits, 50)
        context["audit_page"] = paginator.get_page(self.request.GET.get("page"))
        query.pop("page", None)
        context["audit_filters"] = filters
        context["audit_query"] = query.urlencode()
        return context


class LogClearConfirmView(LogAdminView):
    template_name = "observability/admin/clear_confirm.html"
    page_title = "清理日志"

    def dispatch(self, request, *args, **kwargs):
        require_log_permission(request, MANAGE_PERMISSION)
        return super().dispatch(request, *args, **kwargs)

    def _initial(self):
        return {
            "target_type": self.request.GET.get("target_type", "domain"),
            "target": self.request.GET.get("target", "blog"),
            "kind": self.request.GET.get("kind", ""),
            "scope": self.request.GET.get("scope", "all"),
        }

    def _selection_form(self):
        data = self._initial()
        data.update(self.request.GET.dict())
        return LogClearSelectionForm(data)

    def _build_context(self, selection_form, clear_form=None, preview=None):
        context = super().get_context_data()
        context.update(
            {
                "selection_form": selection_form,
                "clear_form": clear_form,
                "preview": preview,
                "preview_url": reverse("observability:clear_preview"),
            }
        )
        return context

    def get_context_data(self, **kwargs):
        selection_form = self._selection_form()
        preview = None
        clear_form = None
        if selection_form.is_valid():
            selection = _selection_data(selection_form.cleaned_data)
            if selection["target_type"] == "all" and not self.request.user.is_superuser:
                selection_form.add_error(None, "全部运行日志仅限超级管理员")
            else:
                specs = select_clear_specs(
                    selection["target_type"], selection["target"], selection["kind"]
                )
                if not specs:
                    selection_form.add_error(None, "没有找到可清理的注册日志")
                else:
                    try:
                        preview = _preview_payload(selection, specs)
                    except ValueError as exc:
                        selection_form.add_error(None, str(exc))
                    else:
                        clear_form = LogClearForm(
                            initial={
                                **selection,
                                "idempotency_key": preview["idempotency_key"],
                                "preview_token": preview["preview_token"],
                            }
                        )
        return self._build_context(selection_form, clear_form, preview)

    def _render_invalid_post(self, form):
        selection = {
            name: form.data.get(name, "")
            for name in ("target_type", "target", "kind", "scope")
        }
        selection_form = LogClearSelectionForm(initial=selection)
        return self.render_to_response(self._build_context(selection_form, form, None))

    def post(self, request, *args, **kwargs):
        form = LogClearForm(request.POST)
        if not form.is_valid():
            return self._render_invalid_post(form)
        data = form.cleaned_data
        if data["target_type"] == "all" and not request.user.is_superuser:
            form.add_error(None, "一键清空全部运行日志仅限超级管理员")
            return self._render_invalid_post(form)
        selection = _selection_data(data)
        try:
            signed_selection = signing.loads(
                data["preview_token"],
                salt=CLEAR_PREVIEW_SALT,
                max_age=CLEAR_PREVIEW_MAX_AGE,
            )
        except signing.BadSignature:
            form.add_error(None, "清理预览已失效，请重新获取预览")
            return self._render_invalid_post(form)
        expected_selection = {
            **selection,
            "idempotency_key": str(data["idempotency_key"]),
        }
        if signed_selection != expected_selection:
            form.add_error(None, "提交目标与确认预览不一致，请重新确认")
            return self._render_invalid_post(form)
        specs = select_clear_specs(data["target_type"], data["target"], data["kind"])
        if not specs:
            form.add_error(None, "没有找到可清理的注册日志")
            return self._render_invalid_post(form)

        audit, executed = clear_and_audit(
            user=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
            idempotency_key=data["idempotency_key"],
            target=f"{data['target_type']}:{data['target'] or '*'}:{data['kind'] or '*'}",
            target_type=data["target_type"],
            kind=data["kind"],
            scope=data["scope"],
            specs=specs,
            request_metadata={
                "method": request.method,
                "request_id": request.headers.get("X-Request-ID", "")[:120],
                "user_agent": request.headers.get("User-Agent", "")[:500],
            },
        )
        if not executed:
            state = audit.details.get("state")
            if state == "running":
                messages.warning(request, "相同清理请求仍在处理中，请稍后在清理记录中查看最终结果。")
            elif state == "failed":
                messages.error(request, "该请求已处理过，原执行结果为失败；未重复执行清理。")
            else:
                messages.warning(
                    request,
                    f"该请求已处理过，已返回原结果：{audit.files_before} 个现有文件，"
                    f"释放 {audit.bytes_freed} 字节。",
                )
        elif audit.succeeded:
            if audit.index_sync_state in {"pending", "running"}:
                messages.warning(
                    request,
                    f"本地日志已清理，处理 {audit.files_before} 个文件，释放 "
                    f"{audit.bytes_freed} 字节；Elasticsearch 索引正在同步。",
                )
            elif audit.index_sync_state in {"failed", "dead_letter"}:
                messages.warning(
                    request,
                    f"本地日志已清理，处理 {audit.files_before} 个文件；"
                    "Elasticsearch 索引同步失败，请查看清理记录。",
                )
            else:
                messages.success(
                    request,
                    f"日志清理完成，处理 {audit.files_before} 个文件，释放 "
                    f"{audit.bytes_freed} 字节。当前主文件已原地截断（运行中的服务实时产生新日志属正常业务），历史轮转与孤儿临时文件已彻底清理。",
                )
        else:
            failed = audit.details.get("failed_files", [])
            summary = "；".join(
                f"{item['file']}：{item['error']}" for item in failed[:3]
            )
            messages.error(
                request,
                f"日志部分清理失败，共 {len(failed)} 个文件。{summary}",
            )
        return HttpResponseRedirect(reverse("observability:audits"))


class LogClearPreviewView(LogAdminView):
    """为对话框和降级确认页提供同一份结构化预览。"""

    def dispatch(self, request, *args, **kwargs):
        require_log_permission(request, MANAGE_PERMISSION)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        form = LogClearSelectionForm(request.GET)
        if not form.is_valid():
            return JsonResponse({"errors": form.errors.get_json_data()}, status=400)
        selection = _selection_data(form.cleaned_data)
        if selection["target_type"] == "all" and not request.user.is_superuser:
            return JsonResponse(
                {"errors": {"target_type": [{"message": "全部运行日志仅限超级管理员"}]}},
                status=403,
            )
        # 目标只通过注册表键解析，客户端不能提交任意服务器路径。
        specs = select_clear_specs(
            selection["target_type"], selection["target"], selection["kind"]
        )
        if not specs:
            return JsonResponse({"errors": {"target": [{"message": "没有匹配的注册日志"}]}}, status=400)
        try:
            payload = _preview_payload(selection, specs)
        except ValueError as exc:
            return JsonResponse({"errors": {"target": [{"message": str(exc)}]}}, status=400)
        return JsonResponse(payload)
