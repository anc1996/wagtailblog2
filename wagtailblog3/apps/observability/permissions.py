from django.core.exceptions import PermissionDenied


VIEW_PERMISSION = "observability.view_logs"
MANAGE_PERMISSION = "observability.manage_logs"


def require_log_permission(request, permission=VIEW_PERMISSION):
    """统一执行后台日志权限检查，超级管理员天然拥有权限。"""
    if not request.user.has_perm(permission):
        raise PermissionDenied
