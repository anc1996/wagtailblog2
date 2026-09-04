"""Wagtail 系统日志后台 URL。"""

from django.urls import path  # 声明系统日志后台的页面路由

from .views import (
    LogAuditDetailView,
    LogAuditView,
    LogClearConfirmView,
    LogClearPreviewView,
    LogOverviewView,
    LogRecordsView,
)


app_name = "observability"  # 日志后台路由的反向解析命名空间

urlpatterns = [
    # /admin/reports/system-logs/：日志概览首页，展示各日志域的大小、轮转和近期错误统计。
    path("", LogOverviewView.as_view(), name="overview"),
    # /admin/reports/system-logs/records/：日志记录查询页，支持按域、类型、级别、时间和关键词筛选。
    path("records/", LogRecordsView.as_view(), name="records"),
    # /admin/reports/system-logs/audits/：日志清理审计页，查看清理目标、状态、操作人和执行结果。
    path("audits/", LogAuditView.as_view(), name="audits"),
    # /admin/reports/system-logs/audits/<id>/detail/：异步获取单条清理审计的完整详情 JSON。
    path("audits/<int:audit_id>/detail/", LogAuditDetailView.as_view(), name="audit_detail"),
    # /admin/reports/system-logs/clear/：日志清理确认页，提交前展示清理范围并要求二次确认。
    path("clear/", LogClearConfirmView.as_view(), name="clear"),
    # /admin/reports/system-logs/clear/preview/：异步生成清理预览，返回匹配文件数和待释放容量。
    path("clear/preview/", LogClearPreviewView.as_view(), name="clear_preview"),
]
