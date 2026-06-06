# blog/middleware.py
from django.utils.deprecation import MiddlewareMixin
import logging

logger = logging.getLogger(__name__)


class PageViewMiddleware(MiddlewareMixin):
    """占位保留，计数逻辑已移入 BlogPage.serve()"""

    def process_response(self, request, response):
        return response