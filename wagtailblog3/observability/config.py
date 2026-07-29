"""Django logging configuration built from the domain registry."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from .registry import LOG_DIRECTORIES, LOG_DOMAINS, handler_name


MAX_BYTES = 10 * 1024 * 1024
FILTER_MODULE = "wagtailblog3.observability.filters"


def _resolve_log_dir(log_dir: str | os.PathLike[str] | None = None) -> Path:
    if log_dir is None:
        from django.conf import settings

        log_dir = getattr(settings, "LOG_DIR", Path(settings.BASE_DIR) / "logs")

    path = Path(log_dir).expanduser()
    if not path.is_absolute():
        from django.conf import settings

        path = Path(settings.BASE_DIR) / path
    return path.resolve()


def ensure_log_dirs(log_dir: str | os.PathLike[str] | None = None) -> str:
    """Create all registered log directories and return the absolute root."""
    root = _resolve_log_dir(log_dir)
    root.mkdir(parents=True, exist_ok=True)
    for directory in LOG_DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)
    return str(root)


def _rotating_handler(
    filename: Path,
    *,
    level: str,
    formatter: str = "verbose",
    backup_count: int = 5,
    max_bytes: int = MAX_BYTES,
    filters: list[str] | None = None,
) -> dict:
    """Build a process-safe local rotating file handler."""
    handler = {
        "class": "concurrent_log_handler.ConcurrentRotatingFileHandler",
        "level": level,
        "filename": str(filename),
        "maxBytes": max_bytes,
        "backupCount": backup_count,
        "encoding": "utf-8",
        "delay": True,
        "formatter": formatter,
    }
    if filters:
        handler["filters"] = filters
    return handler


def _domain_logger(activity_handler: str, error_handler: str) -> dict:
    return {
        "handlers": ["console", activity_handler, error_handler],
        "level": "INFO",
        "propagate": False,
    }


def _install_domain_routes(root: Path, handlers: dict, loggers: dict) -> None:
    for domain in LOG_DOMAINS:
        activity_handler = handler_name(domain, "activity")
        error_handler = handler_name(domain, "error")
        handlers[activity_handler] = _rotating_handler(
            root / domain.directory / domain.activity_file,
            level="INFO",
            filters=["max_warning"],
        )
        handlers[error_handler] = _rotating_handler(
            root / domain.directory / domain.error_file,
            level="ERROR",
        )
        for logger_name in domain.logger_names:
            loggers[logger_name] = _domain_logger(activity_handler, error_handler)


def get_logging_config(
    modules_filter: Iterable[str] | None = None,
    *,
    log_dir: str | os.PathLike[str] | None = None,
) -> dict:
    """Build the complete project logging dictionary.

    Each registered domain has one activity file (INFO through WARNING) and
    one error file (ERROR and CRITICAL).  Infrastructure domains are isolated,
    and the root handler is only an error fallback for unregistered loggers.
    """
    root = Path(ensure_log_dirs(log_dir))
    filters: dict[str, dict] = {
        "max_warning": {
            "()": f"{FILTER_MODULE}.MaxLevelFilter",
            "max_level": "WARNING",
        }
    }
    handlers: dict[str, dict] = {
        "console": {
            "class": "logging.StreamHandler",
            "level": "WARNING",
            "formatter": "colored",
        },
        "fallback_error_file": _rotating_handler(
            root / "system/error.log", level="ERROR"
        ),
        "django_warning_file": _rotating_handler(
            root / "system/django_warning.log",
            level="WARNING",
            filters=["max_warning"],
        ),
        "django_error_file": _rotating_handler(
            root / "system/django_error.log", level="ERROR"
        ),
        "wagtail_warning_file": _rotating_handler(
            root / "system/wagtail_warning.log",
            level="WARNING",
            filters=["max_warning"],
        ),
        "wagtail_error_file": _rotating_handler(
            root / "system/wagtail_error.log", level="ERROR"
        ),
        "project_activity_file": _rotating_handler(
            root / "system/application.log",
            level="INFO",
            filters=["max_warning"],
        ),
        "project_error_file": _rotating_handler(
            root / "system/application_error.log", level="ERROR"
        ),
        "email_operations_file": _rotating_handler(
            root / "email/email_operations.log",
            level="INFO",
            formatter="email_verbose",
            filters=["max_warning"],
        ),
        "email_rate_limit_file": _rotating_handler(
            root / "email/rate_limit.log",
            level="INFO",
            formatter="email_verbose",
            backup_count=3,
            max_bytes=5 * 1024 * 1024,
            filters=["max_warning"],
        ),
        "email_tasks_file": _rotating_handler(
            root / "email/email_tasks.log",
            level="INFO",
            formatter="email_verbose",
            filters=["max_warning"],
        ),
        "email_error_file": _rotating_handler(
            root / "email/email_error.log",
            level="ERROR",
            formatter="email_verbose",
        ),
        "celery_worker_file": _rotating_handler(
            root / "celery/celery_worker.log",
            level="INFO",
            filters=["max_warning"],
        ),
        "celery_beat_file": _rotating_handler(
            root / "celery/celery_beat.log",
            level="INFO",
            backup_count=3,
            max_bytes=5 * 1024 * 1024,
            filters=["max_warning"],
        ),
        "celery_error_file": _rotating_handler(
            root / "celery/celery_error.log", level="ERROR"
        ),
    }
    loggers: dict[str, dict] = {
        "django": {
            "handlers": ["console", "django_warning_file", "django_error_file"],
            "level": "WARNING",
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
            "handlers": [
                "console",
                "celery_worker_file",
                "celery_error_file",
                "email_tasks_file",
            ],
            "level": "INFO",
            "propagate": False,
        },
    }
    _install_domain_routes(root, handlers, loggers)

    base_activity = handler_name(next(d for d in LOG_DOMAINS if d.key == "base"), "activity")
    base_error = handler_name(next(d for d in LOG_DOMAINS if d.key == "base"), "error")
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
        filters["module_filter"] = {
            "()": f"{FILTER_MODULE}.ModuleFilter",
            "modules": selected_modules,
        }
        handlers["console"]["filters"] = ["module_filter"]

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": filters,
        "formatters": {
            "verbose": {
                "format": (
                    "[{asctime}] {levelname} [{name}:{funcName}:{lineno}] "
                    "[pid={process} thread={threadName}] {message}"
                ),
                "style": "{",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "colored": {
                "()": "colorlog.ColoredFormatter",
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
                "format": (
                    "[{asctime}] {levelname} [{name}] "
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
    return {
        "handlers": {
            "email_debug_file": _rotating_handler(
                root / "email/email_debug.log",
                level="DEBUG",
                formatter="email_verbose",
                backup_count=2,
                max_bytes=5 * 1024 * 1024,
            )
        },
        "loggers": {
            logger_name: {"handlers": ["email_debug_file"], "level": "DEBUG"}
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
    return {
        "handlers": {
            "performance_file": _rotating_handler(
                root / "system/performance.log", level="INFO", backup_count=3
            )
        },
        "loggers": {
            "performance": {
                "handlers": ["performance_file"],
                "level": "INFO",
                "propagate": False,
            }
        },
    }
