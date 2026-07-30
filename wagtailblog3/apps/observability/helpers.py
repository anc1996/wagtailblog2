"""Small helpers for consistent contextual and exception logging."""

from __future__ import annotations

import logging
from functools import wraps


class ContextLoggerAdapter(logging.LoggerAdapter):
    """Append stable context without requiring custom format fields."""

    def process(self, msg, kwargs):
        return f"{msg} - Context: {self.extra}", kwargs


def log_exceptions(logger=None, level=logging.ERROR, message=None):
    """Log an exception with its traceback and re-raise it unchanged."""

    def decorator(func):
        target_logger = logger or logging.getLogger(func.__module__)

        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                log_message = message or f"执行 {func.__name__} 时出错"
                target_logger.log(
                    level,
                    "%s: %s",
                    log_message,
                    exc,
                    exc_info=True,
                )
                raise

        return wrapper

    return decorator


def get_context_logger(name, **context):
    """Return a standard LoggerAdapter with stable structured context."""
    return ContextLoggerAdapter(logging.getLogger(name), context)
