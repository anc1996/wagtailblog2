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
    _admin_body_read_path = re.compile(
        r"^/admin/pages/(?P<page_id>\d+)/(?:edit|revisions|history|compare)/"
    )

    def process_exception(
        self,
        request: HttpRequest,
        exception: BaseException,
    ) -> TemplateResponse | None:
        """把后台历史正文故障转为受控响应，阻止用户在错误版本上继续操作。

        只在页面读取入口处理正文领域异常，写入冻结及程序错误仍由原边界处理。
        缺失或损坏快照不能安全恢复，返回 409；Mongo 暂时不可用时返回 503，允许重试。
        """
        if request.method not in ("GET", "HEAD"):
            return None

        from .models import BlogRevisionBodyUnavailableError
        from external_content.exceptions import (
            CodecError,
            ContentStoreError,
            ManifestError,
            PublicationBodyUnavailableError,
            PublicationRevisionInvalidError,
        )

        if isinstance(exception, PublicationBodyUnavailableError):
            path_match = self._admin_page_path.match(request.path)
            is_admin_page = path_match is not None
            page_id = path_match.group("page_id") if path_match else None
            status = 409 if is_admin_page else 503
            logger.warning(
                "external_publication_body_unavailable path=%s status=%s",
                request.path,
                status,
            )
            context = {
                "error_code": "publication_body_unavailable",
                "is_external_publication": True,
                "retryable": False,
            }
            if page_id is not None:
                context["history_url"] = reverse("wagtailadmin_pages:history", args=[page_id])
                context["retry_url"] = request.get_full_path()
                template_name = "blog/admin/revision_body_unavailable.html"
            else:
                template_name = "external_content/publication_body_unavailable.html"
            return TemplateResponse(
                request,
                template_name,
                context,
                status=status,
            )

        if isinstance(exception, (CodecError, ContentStoreError, ManifestError, PublicationRevisionInvalidError)):
            path_match = self._admin_body_read_path.match(request.path)
            if path_match is None:
                return None
            # 保留底层 Fail-Closed，只在 HTTP 展示边界映射已分类的读取故障。
            retryable = (
                isinstance(exception, ContentStoreError)
                and exception.details.get("error_category") == "network_timeout"
            )
            error_code = exception.code
        elif isinstance(exception, BlogRevisionBodyUnavailableError):
            path_match = self._admin_page_path.match(request.path)
            retryable = exception.retryable
            error_code = exception.code
        else:
            return None

        if path_match is None:
            return None

        page_id = path_match.group("page_id")
        context = {
            "error_code": error_code,
            "history_url": reverse("wagtailadmin_pages:history", args=[page_id]),
            "retry_url": request.get_full_path(),
            "retryable": retryable,
        }
        status = 503 if retryable else 409
        logger.warning(
            "revision_body_admin_error page_id=%s code=%s status=%s",
            page_id,
            error_code,
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
