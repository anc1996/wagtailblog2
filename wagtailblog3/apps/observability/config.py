"""仅依据日志文件注册表生成 Django 日志配置。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .registry import (
    LOG_DIRECTORIES,
    LOG_DOMAINS,
    LOG_FILE_BY_KEY,
    LogFileSpec,
    handler_name,
)


FILTER_MODULE = "observability.filters"
SANITIZER_MODULE = "observability.sanitizer"


def _resolve_log_dir(log_dir: str | os.PathLike[str] | None = None) -> Path:
    # 相对路径统一相对于 Django 项目根目录解析，避免开发服务器和任务进程
    # 因当前工作目录不同而把日志写到不同位置。
    if log_dir is None:
        from django.conf import settings

        log_dir = getattr(settings, "LOG_DIR", Path(settings.BASE_DIR) / "logs")

    path = Path(log_dir).expanduser()
    if not path.is_absolute():
        from django.conf import settings

        path = Path(settings.BASE_DIR) / path
    return path.resolve()


def ensure_log_dirs(log_dir: str | os.PathLike[str] | None = None) -> str:
    """创建注册表需要的全部目录，并返回绝对路径根目录。"""
    root = _resolve_log_dir(log_dir)
    root.mkdir(parents=True, exist_ok=True)
    for directory in LOG_DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)
    return str(root)


def _handler_filters(filters: list[str] | None = None) -> list[str]:
    # 使用字典去重并保持顺序，保证基础脱敏过滤器始终存在且不会重复执行。
    return list(dict.fromkeys(["project_relative_path", "sensitive_data", *(filters or [])]))


def _rotating_handler(
    root: Path,
    spec: LogFileSpec,
    *,
    level: str,
    formatter: str = "verbose",
    filters: list[str] | None = None,
) -> dict:
    """仅使用注册表元数据构造支持多进程的日志处理器。"""
    return {
        "class": "concurrent_log_handler.ConcurrentRotatingFileHandler",
        "level": level,
        "filename": str(root / spec.relative_path),
        "maxBytes": spec.max_bytes,
        "backupCount": spec.backup_count,
        "encoding": "utf-8",
        "delay": True,
        "formatter": formatter,
        "filters": _handler_filters(filters),
    }


def _catalog_handler(
    root: Path,
    key: str,
    *,
    level: str,
    formatter: str = "verbose",
    filters: list[str] | None = None,
) -> dict:
    return _rotating_handler(
        root,
        LOG_FILE_BY_KEY[key],
        level=level,
        formatter=formatter,
        filters=filters,
    )


def _domain_logger(activity_handler: str, error_handler: str) -> dict:
    return {
        "handlers": ["console", activity_handler, error_handler],
        "level": "INFO",
        "propagate": False,
    }


def _install_domain_routes(root: Path, handlers: dict, loggers: dict) -> None:
    # 日志域、处理器和 logger 路由全部从注册表生成，新增日志域时无需复制配置块。
    for domain in LOG_DOMAINS:
        activity_key = f"{domain.key}_activity"
        error_key = f"{domain.key}_error"
        activity_handler = handler_name(domain, "activity")
        error_handler = handler_name(domain, "error")
        handlers[activity_handler] = _catalog_handler(
            root, activity_key, level="INFO", filters=["max_warning"]
        )
        handlers[error_handler] = _catalog_handler(root, error_key, level="ERROR")
        for logger_name in domain.logger_names:
            loggers[logger_name] = _domain_logger(activity_handler, error_handler)


def get_logging_config(
    modules_filter: Iterable[str] | None = None,
    *,
    log_dir: str | os.PathLike[str] | None = None,
) -> dict:
    """构造项目完整的日志配置字典。"""
    root = Path(ensure_log_dirs(log_dir))
    filters: dict[str, dict] = {
        "project_relative_path": {
            "()": f"{FILTER_MODULE}.ProjectRelativePathFilter",
        },
        "sensitive_data": {
            "()": f"{SANITIZER_MODULE}.SensitiveDataFilter",
        },
        "max_warning": {
            "()": f"{FILTER_MODULE}.MaxLevelFilter",
            "max_level": "WARNING",
        },
    }
    handlers: dict[str, dict] = {
        "console": {
            "class": "logging.StreamHandler",
            "level": "WARNING",
            "formatter": "colored",
            "filters": _handler_filters(),
        },
        "fallback_error_file": _catalog_handler(root, "system_error", level="ERROR"),
        "django_warning_file": _catalog_handler(
            root, "django_warning", level="WARNING", filters=["max_warning"]
        ),
        "django_error_file": _catalog_handler(root, "django_error", level="ERROR"),
        "wagtail_warning_file": _catalog_handler(
            root, "wagtail_warning", level="WARNING", filters=["max_warning"]
        ),
        "wagtail_error_file": _catalog_handler(root, "wagtail_error", level="ERROR"),
        "project_activity_file": _catalog_handler(
            root, "project_activity", level="INFO", filters=["max_warning"]
        ),
        "project_error_file": _catalog_handler(root, "project_error", level="ERROR"),
        "email_operations_file": _catalog_handler(
            root, "email_operations", level="INFO", formatter="email_verbose", filters=["max_warning"]
        ),
        "email_rate_limit_file": _catalog_handler(
            root, "email_rate_limit", level="INFO", formatter="email_verbose", filters=["max_warning"]
        ),
        "email_tasks_file": _catalog_handler(
            root, "email_tasks", level="INFO", formatter="email_verbose", filters=["max_warning"]
        ),
        "email_error_file": _catalog_handler(
            root, "email_error", level="ERROR", formatter="email_verbose"
        ),
        "celery_worker_file": _catalog_handler(
            root, "celery_worker", level="INFO", filters=["max_warning"]
        ),
        "celery_beat_file": _catalog_handler(
            root, "celery_beat", level="INFO", filters=["max_warning"]
        ),
        "celery_error_file": _catalog_handler(root, "celery_error", level="ERROR"),
        "runtime_runserver_file": _catalog_handler(
            root, "runtime_runserver", level="INFO"
        ),
        "runtime_uwsgi_file": _catalog_handler(
            root, "runtime_uwsgi", level="INFO"
        ),
    }
    loggers: dict[str, dict] = {
        "django": {
            "handlers": ["console", "django_warning_file", "django_error_file"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.server": {
            "handlers": ["console", "runtime_runserver_file"],
            "level": "INFO",
            "propagate": False,
        },
        "wagtail": {
            "handlers": ["console", "wagtail_warning_file", "wagtail_error_file"],
            "level": "WARNING",
            "propagate": False,
        },
        "wagtailblog3": {
            "handlers": ["console", "project_activity_file", "project_error_file"],
            "level": "INFO",
            "propagate": False,
        },
        "django.core.mail": {
            "handlers": ["console", "email_operations_file", "email_error_file"],
            "level": "INFO",
            "propagate": False,
        },
        "wagtail.contrib.forms": {
            "handlers": ["console", "email_operations_file", "email_error_file"],
            "level": "INFO",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console", "celery_worker_file", "celery_error_file"],
            "level": "INFO",
            "propagate": False,
        },
        "celery.worker": {
            "handlers": ["console", "celery_worker_file", "celery_error_file"],
            "level": "INFO",
            "propagate": False,
        },
        "celery.beat": {
            "handlers": ["console", "celery_beat_file", "celery_error_file"],
            "level": "INFO",
            "propagate": False,
        },
        "celery.task": {
            "handlers": ["console", "celery_worker_file", "celery_error_file", "email_tasks_file"],
            "level": "INFO",
            "propagate": False,
        },
    }
    _install_domain_routes(root, handlers, loggers)

    base_domain = next(domain for domain in LOG_DOMAINS if domain.key == "base")
    base_activity = handler_name(base_domain, "activity")
    base_error = handler_name(base_domain, "error")
    email_routes = {
        "base.models": "email_operations_file",
        "base.tasks": "email_tasks_file",
        "base.utils": "email_operations_file",
        "base.rate_limit": "email_rate_limit_file",
    }
    for logger_name, specialty_handler in email_routes.items():
        loggers[logger_name] = {
            "handlers": [
                "console",
                base_activity,
                base_error,
                specialty_handler,
                "email_error_file",
            ],
            "level": "INFO",
            "propagate": False,
        }

    selected_modules = [name.strip() for name in (modules_filter or []) if name.strip()]
    if selected_modules:
        # 模块筛选只附加到控制台处理器，文件日志仍保持完整，便于故障后审计。
        filters["module_filter"] = {
            "()": f"{FILTER_MODULE}.ModuleFilter",
            "modules": selected_modules,
        }
        handlers["console"]["filters"].append("module_filter")

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": filters,
        "formatters": {
            "verbose": {
                "()": f"{SANITIZER_MODULE}.RedactingFormatter",
                "format": (
                    "[{asctime}] {levelname} "
                    "[{name}|{relative_path}:{funcName}:{lineno}] "
                    "[pid={process} thread={threadName}] {message}"
                ),
                "style": "{",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "colored": {
                "()": f"{SANITIZER_MODULE}.RedactingColoredFormatter",
                "format": "%(log_color)s[%(levelname)s] %(name)s: %(message)s",
                "log_colors": {
                    "DEBUG": "cyan",
                    "INFO": "green",
                    "WARNING": "yellow",
                    "ERROR": "red",
                    "CRITICAL": "bold_red",
                },
            },
            "email_verbose": {
                "()": f"{SANITIZER_MODULE}.RedactingFormatter",
                "format": (
                    "[{asctime}] {levelname} "
                    "[{name}|{relative_path}:{funcName}:{lineno}] "
                    "[pid={process} thread={threadName}] {message}"
                ),
                "style": "{",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": handlers,
        "loggers": loggers,
        "root": {
            "handlers": ["console", "fallback_error_file"],
            "level": "WARNING",
        },
    }


def get_email_debug_config(
    *, log_dir: str | os.PathLike[str] | None = None
) -> dict:
    root = Path(ensure_log_dirs(log_dir))
    spec = LOG_FILE_BY_KEY["email_debug"]
    return {
        "handlers": {
            spec.handler: _rotating_handler(
                root, spec, level="DEBUG", formatter="email_verbose"
            )
        },
        "loggers": {
            logger_name: {
                "handlers": [spec.handler],
                "level": "DEBUG",
                "propagate": False,
            }
            for logger_name in (
                "base.models",
                "base.tasks",
                "base.utils",
                "base.rate_limit",
            )
        },
    }


def get_performance_logging_config(
    *, log_dir: str | os.PathLike[str] | None = None
) -> dict:
    root = Path(ensure_log_dirs(log_dir))
    spec = LOG_FILE_BY_KEY["performance"]
    return {
        "handlers": {
            spec.handler: _rotating_handler(root, spec, level="INFO")
        },
        "loggers": {
            "performance": {
                "handlers": [spec.handler],
                "level": "INFO",
                "propagate": False,
            }
        },
    }
