"""博客文章成功响应后的访问统计中间件。"""

import logging

from django.utils.deprecation import MiddlewareMixin

from .page_view_counter import PageViewCounter

logger = logging.getLogger(__name__)


class PageViewMiddleware(MiddlewareMixin):
    """只统计 BlogPage 成功呈现后的 GET，预览、重定向和错误页均不计入。"""

    def process_response(self, request, response):
        page_id = getattr(request, "_blog_analytics_page_id", None)
        if request.method == "GET" and response.status_code == 200 and page_id:
            try:
                PageViewCounter(page_id).record(request)
            except Exception:
                logger.warning("page_view_middleware_failed page_id=%s", page_id, exc_info=True)
        return response
