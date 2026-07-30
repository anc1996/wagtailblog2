"""项目日志域和文件保留策略的唯一注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
SMALL_MAX_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LogDomain:
    """描述业务日志域及其 logger 命名空间。"""

    key: str
    logger_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LogFileSpec:
    """一个可写、可读取和可清理的日志文件完整定义。"""

    key: str
    handler: str
    domain: str
    kind: str
    relative_path: str
    label: str
    backup_count: int = 5
    max_bytes: int = DEFAULT_MAX_BYTES
    business: bool = False


LOG_DOMAINS: tuple[LogDomain, ...] = (
    LogDomain("archive", ("archive",)),
    LogDomain("base", ("base",)),
    LogDomain("blog", ("blog",)),
    LogDomain("comments", ("comments",)),
    LogDomain("home", ("home",)),
    LogDomain("portfolio", ("portfolio",)),
    LogDomain("search", ("search",)),
    LogDomain("mongo", ("wagtailblog3.mongo", "wagtailblog3.mongodb")),
    LogDomain("ai", ("wagtailblog3.ai_backends",)),
    LogDomain("storage", ("wagtailblog3.storage_backends",)),
)

DOMAIN_BY_KEY = {domain.key: domain for domain in LOG_DOMAINS}


def handler_name(domain: LogDomain, kind: str) -> str:
    return f"domain_{domain.key}_{kind}"


def resolve_domain(logger_name: str) -> LogDomain | None:
    """Return the domain owning a logger namespace, if one is registered."""
    matches = (
        domain
        for domain in LOG_DOMAINS
        if any(
            logger_name == namespace or logger_name.startswith(f"{namespace}.")
            for namespace in domain.logger_names
        )
    )
    return next(matches, None)


def _domain_file(domain: LogDomain, kind: str) -> LogFileSpec:
    suffix = "" if kind == "activity" else "_error"
    kind_label = "活动" if kind == "activity" else "错误"
    return LogFileSpec(
        key=f"{domain.key}_{kind}",
        handler=handler_name(domain, kind),
        domain=domain.key,
        kind=kind,
        relative_path=f"{domain.key}/{domain.key}{suffix}.log",
        label=f"{domain.key} {kind_label}日志",
        business=True,
    )


_CATALOG_SPECS: tuple[LogFileSpec, ...] = tuple(
    _domain_file(domain, kind)
    for domain in LOG_DOMAINS
    for kind in ("activity", "error")
) + (
    LogFileSpec("celery_worker", "celery_worker_file", "celery", "activity", "celery/celery_worker.log", "Celery Worker 日志"),
    LogFileSpec("celery_beat", "celery_beat_file", "celery", "activity", "celery/celery_beat.log", "Celery Beat 日志", 3, SMALL_MAX_BYTES),
    LogFileSpec("celery_error", "celery_error_file", "celery", "error", "celery/celery_error.log", "Celery 错误日志"),
    LogFileSpec("django_warning", "django_warning_file", "django", "activity", "system/django_warning.log", "Django 警告日志"),
    LogFileSpec("django_error", "django_error_file", "django", "error", "system/django_error.log", "Django 错误日志"),
    LogFileSpec("wagtail_warning", "wagtail_warning_file", "wagtail", "activity", "system/wagtail_warning.log", "Wagtail 警告日志"),
    LogFileSpec("wagtail_error", "wagtail_error_file", "wagtail", "error", "system/wagtail_error.log", "Wagtail 错误日志"),
    LogFileSpec("project_activity", "project_activity_file", "project", "activity", "system/application.log", "项目活动日志"),
    LogFileSpec("project_error", "project_error_file", "project", "error", "system/application_error.log", "项目错误日志"),
    LogFileSpec("system_error", "fallback_error_file", "system", "error", "system/error.log", "系统兜底错误日志"),
    LogFileSpec("performance", "performance_file", "system", "activity", "system/performance.log", "性能日志", 3),
    LogFileSpec("email_operations", "email_operations_file", "email", "activity", "email/email_operations.log", "邮件操作日志"),
    LogFileSpec("email_rate_limit", "email_rate_limit_file", "email", "activity", "email/rate_limit.log", "邮件限流日志", 3, SMALL_MAX_BYTES),
    LogFileSpec("email_tasks", "email_tasks_file", "email", "activity", "email/email_tasks.log", "邮件任务日志"),
    LogFileSpec("email_debug", "email_debug_file", "email", "activity", "email/email_debug.log", "邮件调试日志", 3, SMALL_MAX_BYTES),
    LogFileSpec("email_error", "email_error_file", "email", "error", "email/email_error.log", "邮件错误日志"),
    LogFileSpec("runtime_runserver", "runtime_runserver_file", "runtime", "activity", "runtime/runserver.log", "Django 开发服务器请求日志"),
)


LOG_FILE_CATALOG: dict[str, LogFileSpec] = {
    spec.key: spec for spec in _CATALOG_SPECS
}
if len(LOG_FILE_CATALOG) != len(_CATALOG_SPECS):
    raise RuntimeError("日志 catalog 存在重复文件键")

LOG_FILE_SPECS: tuple[LogFileSpec, ...] = tuple(LOG_FILE_CATALOG.values())
LOG_FILE_BY_KEY = LOG_FILE_CATALOG
LOG_FILE_BY_HANDLER = {spec.handler: spec for spec in LOG_FILE_SPECS}
if len(LOG_FILE_BY_HANDLER) != len(LOG_FILE_SPECS):
    raise RuntimeError("日志 catalog 存在重复 handler")
if len({spec.relative_path for spec in LOG_FILE_SPECS}) != len(LOG_FILE_SPECS):
    raise RuntimeError("日志 catalog 存在重复路径")
LOG_DOMAIN_KEYS = tuple(dict.fromkeys(spec.domain for spec in LOG_FILE_SPECS))
LOG_DIRECTORIES = tuple(
    dict.fromkeys(str(PurePosixPath(spec.relative_path).parent) for spec in LOG_FILE_SPECS)
)


def iter_log_files(domain: str | None = None, kind: str | None = None):
    """按模块和类型返回已注册文件，调用方无需接触真实路径。"""
    return tuple(
        spec
        for spec in LOG_FILE_SPECS
        if (domain is None or spec.domain == domain)
        and (kind is None or spec.kind == kind)
    )


def find_log_file(domain: str, kind: str) -> LogFileSpec | None:
    """返回一个域/类型的主文件；管理命令用它兼容原有参数。"""
    return next(
        (spec for spec in LOG_FILE_SPECS if spec.domain == domain and spec.kind == kind),
        None,
    )
