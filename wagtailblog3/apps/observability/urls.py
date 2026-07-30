"""Wagtail 系统日志后台 URL。"""

from django.urls import path

from .views import (
    LogAuditView,
    LogClearConfirmView,
    LogClearPreviewView,
    LogOverviewView,
    LogRecordsView,
)


app_name = "observability"

urlpatterns = [
    path("", LogOverviewView.as_view(), name="overview"),
    path("records/", LogRecordsView.as_view(), name="records"),
    path("audits/", LogAuditView.as_view(), name="audits"),
    path("clear/", LogClearConfirmView.as_view(), name="clear"),
    path("clear/preview/", LogClearPreviewView.as_view(), name="clear_preview"),
]
