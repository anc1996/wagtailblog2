"""日志概览和清理服务。"""

from __future__ import annotations

from datetime import timedelta
from time import monotonic
from uuid import UUID

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from .reader import read_logs, resolve_registered_path
from .registry import LOG_DOMAIN_KEYS, LOG_FILE_BY_KEY, LogFileSpec, iter_log_files
from .cleanup import CleanupResult, execute_cleanup, preview_cleanup


OVERVIEW_CACHE_KEY = "observability:overview:v1"


ClearResult = CleanupResult


def _file_versions(spec: LogFileSpec):
    for rotation in range(spec.backup_count + 1):
        path = resolve_registered_path(spec, rotation)
        if path.exists() and path.is_file():
            yield rotation, path


def get_overview(*, refresh: bool = False) -> dict:
    if refresh:
        cache.delete(OVERVIEW_CACHE_KEY)
    cached = cache.get(OVERVIEW_CACHE_KEY)
    if cached is not None:
        return cached

    # 概览数据短时缓存；刷新请求只清除概览缓存，不影响日志文件和分页游标。
    modules = []
    total_bytes = 0
    active_domains = 0
    since = timezone.localtime(timezone.now() - timedelta(hours=1)).replace(tzinfo=None)
    for domain in LOG_DOMAIN_KEYS:
        activity_bytes = 0
        error_bytes = 0
        rotations = 0
        last_modified = None
        for spec in iter_log_files(domain):
            for rotation, path in _file_versions(spec):
                stat = path.stat()
                total_bytes += stat.st_size
                if spec.kind == "error":
                    error_bytes += stat.st_size
                else:
                    activity_bytes += stat.st_size
                rotations += int(rotation > 0)
                modified = timezone.datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone())
                last_modified = max(filter(None, (last_modified, modified)), default=modified)
        if activity_bytes or error_bytes:
            active_domains += 1
        recent_error_records = read_logs(
            domain=domain,
            kind="error",
            since=since,
            page_size=200,
        ).records
        modules.append(
            {
                "key": domain,
                "files": [
                    {
                        "key": spec.key,
                        "label": spec.label,
                        "kind": spec.kind,
                    }
                    for spec in iter_log_files(domain)
                ],
                "activity_bytes": activity_bytes,
                "error_bytes": error_bytes,
                "rotation_count": rotations,
                "last_modified": last_modified,
                "recent_error_count": len(recent_error_records),
            }
        )

    errors = read_logs(kind="error", since=since, page_size=200).records
    warnings = read_logs(kind="activity", level="WARNING", since=since, page_size=200).records
    result = {
        "error_count": len(errors),
        "warning_count": len(warnings),
        "active_domains": active_domains,
        "domain_count": len(LOG_DOMAIN_KEYS),
        "total_bytes": total_bytes,
        "modules": modules,
        "refreshed_at": timezone.now(),
    }
    cache.set(OVERVIEW_CACHE_KEY, result, 30)
    return result


def select_clear_specs(target_type: str, target: str, kind: str = "") -> tuple[LogFileSpec, ...]:
    """把受控参数映射为注册项，禁止接收任何客户端路径。"""
    if target_type == "file":
        spec = LOG_FILE_BY_KEY.get(target)
        return (spec,) if spec and (not kind or spec.kind == kind) else ()
    if target_type == "domain":
        return iter_log_files(target, kind or None)
    if target_type == "business":
        return tuple(spec for spec in iter_log_files(kind=kind or None) if spec.business)
    if target_type == "all":
        return iter_log_files(kind=kind or None)
    return ()


def describe_clear(
    specs: tuple[LogFileSpec, ...],
    scope: str,
    *,
    target_type: str = "file",
    target: str = "",
    kind: str = "",
) -> dict:
    return preview_cleanup(
        specs,
        target_type=target_type,
        target=target,
        kind=kind,
        scope=scope,
    )


def clear_logs(specs: tuple[LogFileSpec, ...], scope: str) -> ClearResult:
    result = execute_cleanup(specs, scope)
    cache.delete(OVERVIEW_CACHE_KEY)
    return result


def clear_and_audit(
    *,
    user,
    ip_address: str | None,
    idempotency_key: UUID,
    target: str,
    target_type: str = "legacy",
    kind: str = "",
    scope: str,
    specs,
    request_metadata: dict | None = None,
):
    """先抢占幂等键再清理，避免并发重复提交执行两次文件操作。"""
    from .models import LogClearAudit

    # 先写入“运行中”审计记录并依赖唯一幂等键抢占执行权，重复请求直接返回旧记录。
    try:
        with transaction.atomic():
            audit = LogClearAudit.objects.create(
                idempotency_key=idempotency_key,
                user=user,
                ip_address=ip_address,
                target=target,
                target_type=target_type,
                kind=kind,
                scope=scope,
                succeeded=False,
                state="running",
                details={"state": "running", "request": request_metadata or {}},
            )
    except IntegrityError:
        audit = LogClearAudit.objects.get(idempotency_key=idempotency_key)
        return audit, False

    started_at = monotonic()
    try:
        result = clear_logs(specs, scope)
    except Exception as exc:
        # 非预期异常也必须结束审计状态；相同幂等键仍不重复执行，重新确认会生成新键。
        audit.state = "failed"
        audit.completed_at = timezone.now()
        audit.failed_files = len(specs)
        audit.details = {
            "state": "failed",
            "target_type": audit.target_type,
            "target": audit.target,
            "kind": audit.kind,
            "scope": audit.scope,
            "request": request_metadata or {},
            "spec_keys": [spec.key for spec in specs],
            "duration_ms": round((monotonic() - started_at) * 1000, 3),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "actual": {
                "matched_file_count": len(specs),
                "succeeded_file_count": 0,
                "failed_file_count": len(specs),
            },
        }
        audit.save(update_fields=("state", "completed_at", "failed_files", "details"))
        _write_wagtail_audit(audit, user, request_metadata)
        return audit, True
    audit.files_before = result.files_before
    audit.bytes_before = result.bytes_before
    audit.bytes_freed = result.bytes_freed
    audit.succeeded_files = sum(1 for item in result.file_results if item["succeeded"])
    audit.failed_files = sum(1 for item in result.file_results if not item["succeeded"])
    audit.succeeded = result.succeeded
    if result.succeeded:
        audit.state = "completed"
    elif audit.succeeded_files:
        audit.state = "partial"
    else:
        audit.state = "failed"
    audit.completed_at = timezone.now()
    audit.details = {
        "state": audit.state,
        "target_type": audit.target_type,
        "target": audit.target,
        "kind": audit.kind,
        "scope": audit.scope,
        "request": request_metadata or {},
        "spec_keys": [spec.key for spec in specs],
        "duration_ms": round((monotonic() - started_at) * 1000, 3),
        "file_results": result.file_results,
        "changed_files": result.changed_files,
        "failed_files": result.failed_files,
        "preview": {
            "matched_file_count": len(result.file_results),
            "current_file_count": sum(1 for item in result.file_results if item["rotation"] == 0 and item["pre_exists"]),
            "rotated_file_count": sum(1 for item in result.file_results if item["rotation"] > 0 and item["pre_exists"]),
            "total_bytes": result.bytes_before,
        },
        "actual": {
            "matched_file_count": len(result.file_results),
            "succeeded_file_count": audit.succeeded_files,
            "failed_file_count": audit.failed_files,
            "bytes_freed": audit.bytes_freed,
        },
    }
    audit.save(
        update_fields=(
            "files_before",
            "bytes_before",
            "bytes_freed",
            "succeeded_files",
            "failed_files",
            "succeeded",
            "state",
            "completed_at",
            "details",
        )
    )
    _write_wagtail_audit(audit, user, request_metadata)
    return audit, True


def _write_wagtail_audit(audit, user, request_metadata):
    """只写入精简的 ModelLogEntry，文件明细保留在本应用的审计记录中。"""
    try:
        from wagtail.models import ModelLogEntry

        ModelLogEntry.objects.log_action(
            instance=audit,
            action="observability.clear_logs",
            user=user,
            data={
                "audit_id": audit.pk,
                "target": audit.target,
                "target_type": audit.target_type,
                "kind": audit.kind,
                "scope": audit.scope,
                "state": audit.state,
                "files": audit.files_before,
                "bytes_freed": audit.bytes_freed,
                "request_id": (request_metadata or {}).get("request_id", ""),
            },
        )
    except Exception as exc:
        details = dict(audit.details or {})
        details["wagtail_audit_error"] = f"{type(exc).__name__}: {exc}"
        audit.details = details
        audit.save(update_fields=("details",))
