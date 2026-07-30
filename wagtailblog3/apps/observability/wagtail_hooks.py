from django.contrib.auth.models import Permission
from django.urls import include, path, reverse
from wagtail import hooks
from wagtail.admin.menu import MenuItem

from .permissions import VIEW_PERMISSION
from .urls import urlpatterns as observability_urlpatterns


class SystemLogsMenuItem(MenuItem):
    def is_shown(self, request):
        return request.user.has_perm(VIEW_PERMISSION)


@hooks.register("register_admin_urls")
def register_observability_urls():
    """将日志中心挂载到 Wagtail Admin URL 空间。"""
    return [
        path(
            "reports/system-logs/",
            include((observability_urlpatterns, "observability"), namespace="observability"),
        )
    ]


@hooks.register("register_reports_menu_item")
def register_system_logs_menu_item():
    return SystemLogsMenuItem(
        "系统日志",
        reverse("observability:overview"),
        name="system-logs",
        icon_name="warning",
        order=1050,
    )


@hooks.register("register_permissions")
def register_observability_permissions():
    """让日志权限出现在 Wagtail 用户组权限配置页面。"""
    return Permission.objects.filter(
        content_type__app_label="observability",
        codename__in=("view_logs", "manage_logs"),
    )
