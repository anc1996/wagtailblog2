"""Project logging framework.

Use ``logging.getLogger(__name__)`` in application code.  Django settings only
need :func:`get_logging_config`; domain routing is declared in ``registry``.
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
