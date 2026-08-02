"""用于统一上下文日志和异常日志的小型辅助函数。"""

from __future__ import annotations

import logging
from functools import wraps


class ContextLoggerAdapter(logging.LoggerAdapter):
    """追加稳定上下文，不要求调用方修改日志格式字段。"""

    def process(self, msg, kwargs):
        return f"{msg} - Context: {self.extra}", kwargs


def log_exceptions(logger=None, level=logging.ERROR, message=None):
    """记录带回溯的异常，并原样重新抛出。"""

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
    """返回带稳定结构化上下文的标准 LoggerAdapter。"""
    return ContextLoggerAdapter(logging.getLogger(name), context)
