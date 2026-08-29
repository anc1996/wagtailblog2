"""博客文章成功响应后的访问统计中间件。"""

import logging
import re

from django.http import HttpRequest, HttpResponseBase
from django.template.response import TemplateResponse
from django.utils.deprecation import MiddlewareMixin
from django.urls import reverse

from .page_view_counter import PageViewCounter

logger = logging.getLogger(__name__)


class PageViewMiddleware(MiddlewareMixin):
    """只统计 BlogPage 成功呈现后的 GET，预览、重定向和错误页均不计入。"""

    _admin_page_path = re.compile(r"^/admin/pages/(?P<page_id>\d+)/")

    def process_exception(
        self,
        request: HttpRequest,
        exception: BaseException,
    ) -> TemplateResponse | None:
        """把后台历史正文故障转为受控响应，阻止用户在错误版本上继续操作。

        只处理 BlogPage 的历史正文领域异常，其他异常仍交给 Django 默认错误处理。
        缺失或损坏快照不能安全恢复，返回 409；Mongo 暂时不可用时返回 503，允许重试。
        """
        from .models import BlogRevisionBodyUnavailableError

        if not isinstance(exception, BlogRevisionBodyUnavailableError):
            return None

        path_match = self._admin_page_path.match(request.path)
        if path_match is None:
            return None

        page_id = path_match.group("page_id")
        retryable = exception.retryable
        context = {
            "error_code": exception.code,
            "history_url": reverse("wagtailadmin_pages:history", args=[page_id]),
            "retry_url": request.get_full_path(),
            "retryable": retryable,
        }
        status = 503 if retryable else 409
        logger.warning(
            "blog_revision_body_admin_error page_id=%s code=%s status=%s",
            page_id,
            exception.code,
            status,
        )
        return TemplateResponse(
            request,
            "blog/admin/revision_body_unavailable.html",
            context,
            status=status,
        )

    def process_response(
        self,
        request: HttpRequest,
        response: HttpResponseBase,
    ) -> HttpResponseBase:
        page_id = getattr(request, "_blog_analytics_page_id", None)
        if request.method == "GET" and response.status_code == 200 and page_id:
            try:
                PageViewCounter(page_id).record(request)
            except Exception:
                logger.warning("page_view_middleware_failed page_id=%s", page_id, exc_info=True)
        return response
