"""Single source of truth for project log domains and filenames."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LogDomain:
    key: str
    logger_names: tuple[str, ...]
    directory: str
    activity_file: str
    error_file: str


LOG_DOMAINS: tuple[LogDomain, ...] = (
    LogDomain("archive", ("archive",), "archive", "archive.log", "archive_error.log"),
    LogDomain("base", ("base",), "base", "base.log", "base_error.log"),
    LogDomain("blog", ("blog",), "blog", "blog.log", "blog_error.log"),
    LogDomain("comments", ("comments",), "comments", "comments.log", "comments_error.log"),
    LogDomain("home", ("home",), "home", "home.log", "home_error.log"),
    LogDomain("portfolio", ("portfolio",), "portfolio", "portfolio.log", "portfolio_error.log"),
    LogDomain("search", ("search",), "search", "search.log", "search_error.log"),
    LogDomain(
        "mongo",
        ("wagtailblog3.mongo", "wagtailblog3.mongodb"),
        "mongo",
        "mongo.log",
        "mongo_error.log",
    ),
    LogDomain(
        "ai",
        ("wagtailblog3.ai_backends",),
        "ai",
        "ai.log",
        "ai_error.log",
    ),
    LogDomain(
        "storage",
        ("wagtailblog3.storage_backends",),
        "storage",
        "storage.log",
        "storage_error.log",
    ),
)

DOMAIN_BY_KEY = {domain.key: domain for domain in LOG_DOMAINS}
LOG_DIRECTORIES = tuple(
    dict.fromkeys(
        [domain.directory for domain in LOG_DOMAINS]
        + ["celery", "email", "runtime", "system"]
    )
)


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


LOG_FILE_CATALOG = {
    domain.key: {
        "activity": f"{domain.directory}/{domain.activity_file}",
        "error": f"{domain.directory}/{domain.error_file}",
    }
    for domain in LOG_DOMAINS
}
LOG_FILE_CATALOG.update(
    {
        "celery": {
            "activity": "celery/celery_worker.log",
            "error": "celery/celery_error.log",
        },
        "django": {
            "activity": "system/django_warning.log",
            "error": "system/django_error.log",
        },
        "email": {
            "activity": "email/email_operations.log",
            "error": "email/email_error.log",
        },
        "project": {
            "activity": "system/application.log",
            "error": "system/application_error.log",
        },
        "runtime": {"activity": "runtime/runserver.log", "error": None},
        "system": {"activity": None, "error": "system/error.log"},
        "wagtail": {
            "activity": "system/wagtail_warning.log",
            "error": "system/wagtail_error.log",
        },
    }
)
