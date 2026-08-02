"""项目日志框架。

业务代码使用 ``logging.getLogger(__name__)`` 获取 logger；Django 配置只需调用
:func:`get_logging_config`，日志域路由统一声明在 ``registry`` 中。
"""

from .config import (
    get_email_debug_config,
    get_logging_config,
    get_performance_logging_config,
)
from .helpers import get_context_logger, log_exceptions
from .registry import DOMAIN_BY_KEY, LOG_DOMAINS, LogDomain

__all__ = (
    "DOMAIN_BY_KEY",
    "LOG_DOMAINS",
    "LogDomain",
    "get_context_logger",
    "get_email_debug_config",
    "get_logging_config",
    "get_performance_logging_config",
    "log_exceptions",
)
