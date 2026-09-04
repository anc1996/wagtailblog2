"""日志概览和清理服务。"""

from __future__ import annotations

from datetime import timedelta
import gzip
import json
import logging
from time import monotonic
from uuid import UUID

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .elasticsearch_logs import (
    LogSearchUnavailable,
    build_cleanup_plan,
    get_log_summary,
    is_enabled,
)
from pathlib import Path
from django.conf import settings
from .models import LogClearAudit
from .reader import read_logs, resolve_registered_path
from .registry import LOG_DOMAIN_KEYS, LOG_FILE_BY_KEY, LogFileSpec, iter_log_files
from .cleanup import (
    CleanupResult,
    execute_cleanup,
    preview_cleanup,
    discover_orphan_rotations,
    SAFE_ORPHAN_AGE_SECONDS,
)


OVERVIEW_CACHE_KEY = "observability:overview:v1"


ClearResult = CleanupResult


def _detect_celery_protected_files() -> dict:
    """检测 Celery 目录下受系统保护的调度数据库文件。

    这些文件包括 ``celerybeat-schedule`` 主库及其 WAL/SHM 事务文件，
    属于 Celery Beat 调度器的持久化状态数据库，受系统严格保护，绝不属于日志文件，
    也不得被任何日志清理逻辑触碰。
    """
    root = Path(settings.LOG_DIR).resolve()
    celery_dir = root / "celery"
    if not celery_dir.exists() or not celery_dir.is_dir():
        return {"total_bytes": 0, "files": []}

    protected_files = []
    total_bytes = 0
    try:
        for entry in sorted(celery_dir.iterdir()):
            if entry.name.startswith("celerybeat-schedule") and entry.is_file():
                st = entry.stat()
                total_bytes += st.st_size
                protected_files.append({
                    "name": entry.name,
                    "relative_path": f"celery/{entry.name}",
                    "size": st.st_size,
                })
    except OSError:
        pass
    return {"total_bytes": total_bytes, "files": protected_files}


def _file_versions(spec: LogFileSpec):
    """枚举一个注册日志当前文件及允许的轮转版本。

    参数：``spec`` 为日志注册项。
    返回：``(rotation, path)`` 生成器，仅包含实际存在的普通文件。
    异常：路径不安全时由 ``resolve_registered_path`` 抛出 ``ValueError``。
    """
    for rotation in range(spec.backup_count + 1):
        path = resolve_registered_path(spec, rotation)
        if path.exists() and path.is_file():
            yield rotation, path


def get_overview(*, refresh: bool = False) -> dict:
    """汇总日志中心概览数据。

    参数：``refresh`` 为真时先失效短期概览缓存。
    返回：域级字节数、近期错误/警告计数与刷新时间。
    异常：ES 读取模型不可用时不向上抛出，而退回受限文件读取；文件路径异常
    仍由底层安全校验抛出。

    ES 只是读模型，概览不能因其暂时故障而让后台日志页面不可用。
    """
    if refresh:
        cache.delete(OVERVIEW_CACHE_KEY)
    cached = cache.get(OVERVIEW_CACHE_KEY)
    if cached is not None:
        return cached

    # 概览数据短时缓存；刷新请求只清除概览缓存，不影响日志文件和分页游标。
    modules = []
    total_bytes = 0
    active_domains = 0
    all_orphan_files = []
    orphan_total_bytes = 0
    celery_protected = _detect_celery_protected_files()

    since = timezone.localtime(timezone.now() - timedelta(hours=1)).replace(tzinfo=None)
    search_summary = None
    if is_enabled():
        try:
            search_summary = get_log_summary(since=since)
        except LogSearchUnavailable:
            # The ES index is an optional read model; file statistics remain
            # available when the cluster or index is unavailable.
            search_summary = None
    for domain in LOG_DOMAIN_KEYS:
        activity_bytes = 0
        error_bytes = 0
        rotations = 0
        domain_orphan_count = 0
        domain_orphan_bytes = 0
        last_modified = None
        module_file_details = []

        for spec in iter_log_files(domain):
            spec_versions = list(_file_versions(spec))
            spec_current_bytes = 0
            spec_rotations_count = 0
            spec_rotations_bytes = 0
            for rotation, path in spec_versions:
                stat = path.stat()
                total_bytes += stat.st_size
                if rotation == 0:
                    spec_current_bytes += stat.st_size
                else:
                    spec_rotations_count += 1
                    spec_rotations_bytes += stat.st_size

                if spec.kind == "error":
                    error_bytes += stat.st_size
                else:
                    activity_bytes += stat.st_size
                rotations += int(rotation > 0)
                modified = timezone.datetime.fromtimestamp(stat.st_mtime, tz=timezone.get_current_timezone())
                last_modified = max(filter(None, (last_modified, modified)), default=modified)

            # 发现与当前注册项关联的孤儿轮转临时文件与隔离残留
            orphans = discover_orphan_rotations(spec, min_age_seconds=0)
            spec_orphan_list = []
            for item in orphans:
                total_bytes += item["size"]
                domain_orphan_count += 1
                domain_orphan_bytes += item["size"]
                orphan_total_bytes += item["size"]
                if spec.kind == "error":
                    error_bytes += item["size"]
                else:
                    activity_bytes += item["size"]
                orphan_info = {
                    "source_key": spec.key,
                    "file": item["relative_path"],
                    "name": item["name"],
                    "size": item["size"],
                    "mtime": item["mtime"],
                    "is_safe": item["is_safe"],
                    "skip_reason": item["skip_reason"],
                }
                all_orphan_files.append(orphan_info)
                spec_orphan_list.append(orphan_info)

            module_file_details.append({
                "key": spec.key,
                "label": spec.label,
                "kind": spec.kind,
                "current_bytes": spec_current_bytes,
                "rotations_count": spec_rotations_count,
                "rotations_bytes": spec_rotations_bytes,
                "orphans": spec_orphan_list,
                "orphan_count": len(spec_orphan_list),
                "orphan_bytes": sum(o["size"] for o in spec_orphan_list),
            })

        if activity_bytes or error_bytes:
            active_domains += 1
        if search_summary is not None:
            recent_error_count = search_summary["errors_by_domain"].get(domain, 0)
        else:
            recent_error_count = len(
                read_logs(
                    domain=domain,
                    kind="error",
                    since=since,
                    page_size=200,
                ).records
            )

        module_data = {
            "key": domain,
            "files": module_file_details,
            "activity_bytes": activity_bytes,
            "error_bytes": error_bytes,
            "rotation_count": rotations + domain_orphan_count,
            "standard_rotation_count": rotations,
            "orphan_count": domain_orphan_count,
            "orphan_bytes": domain_orphan_bytes,
            "last_modified": last_modified,
            "recent_error_count": recent_error_count,
        }
        if domain == "celery":
            module_data["celery_protected"] = celery_protected
        modules.append(module_data)

    if search_summary is not None:
        error_count = search_summary["error_count"]
        warning_count = search_summary["warning_count"]
    else:
        error_count = len(read_logs(kind="error", since=since, page_size=200).records)
        warning_count = len(
            read_logs(kind="activity", level="WARNING", since=since, page_size=200).records
        )
    result = {
        "error_count": error_count,
        "warning_count": warning_count,
        "active_domains": active_domains,
        "domain_count": len(LOG_DOMAIN_KEYS),
        "total_bytes": total_bytes,
        "orphan_summary": {
            "count": len(all_orphan_files),
            "total_bytes": orphan_total_bytes,
            "files": all_orphan_files,
        },
        "celery_protected": celery_protected,
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
    """为确认页构造不修改文件的清理预览。

    参数：``specs`` 为注册目标，其他字段用于描述用户选择，``scope`` 为范围。
    返回：与确认对话框对应的当前/轮转文件数量和大小。
    异常：范围或注册路径不安全时由 ``preview_cleanup`` 抛出。
    """
    return preview_cleanup(
        specs,
        target_type=target_type,
        target=target,
        kind=kind,
        scope=scope,
    )


def clear_logs(specs: tuple[LogFileSpec, ...], scope: str) -> ClearResult:
    """执行文件清理并失效概览缓存。

    参数：``specs`` 必须为注册表项，``scope`` 为合法清理范围。
    返回：包含逐文件结果的 ``CleanupResult``。
    异常：目标或范围非法时由 ``execute_cleanup`` 抛出。

    缓存只保存展示统计；在文件变化后立即失效，避免管理员看到旧容量。
    """
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
    cleanup_cutoff = timezone.now()
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
            "orphan_file_count": sum(1 for item in result.file_results if item["rotation"] == -1 and item["pre_exists"]),
            "total_bytes": result.bytes_before,
        },
        "actual": {
            "matched_file_count": len(result.file_results),
            "succeeded_file_count": audit.succeeded_files,
            "failed_file_count": audit.failed_files,
            "orphan_file_count": sum(1 for item in result.file_results if item.get("action") == "unlink_orphan" and item["succeeded"]),
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
    if is_enabled():
        plan = build_cleanup_plan(specs, result, cutoff=cleanup_cutoff)
        if plan.selectors:
            try:
                from .tasks import enqueue_log_index_sync

                enqueue_log_index_sync(audit, plan)
            except Exception as exc:
                audit.index_sync_state = "failed"
                audit.index_sync_last_error = f"{type(exc).__name__}: {exc}"[:4000]
                details = dict(audit.details or {})
                details["index_sync"] = {
                    "state": "failed",
                    "last_error": audit.index_sync_last_error,
                }
                audit.details = details
                audit.save(
                    update_fields=(
                        "index_sync_state",
                        "index_sync_last_error",
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
                "index_sync_state": audit.index_sync_state,
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


logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS: int = 180
DEFAULT_PURGE_BATCH_SIZE: int = 500


def get_audit_retention_summary(days: int = DEFAULT_RETENTION_DAYS) -> dict:
    """获取日志清理审计台账的生命周期保留状态摘要。

    用于在后台管理界面展示审计总数、最早记录时间、超期可清理记录数及未决保护记录数。

    参数:
        days: 审计记录保留天数，默认 180 天。
    返回:
        包含 retention_days, total_count, oldest_date, cutoff_date,
        eligible_count, unresolved_count 的字典。
    """
    cutoff = timezone.now() - timedelta(days=days)
    total_count = LogClearAudit.objects.count()
    oldest_audit = LogClearAudit.objects.order_by("created_at").first()
    oldest_date = oldest_audit.created_at if oldest_audit else None

    # 终态安全门禁：只有文件清理已完成/失败，且 ES 同步任务也已完成或无需同步的，才允许删除
    terminal_q = Q(created_at__lt=cutoff) & Q(state__in=["completed", "failed"]) & Q(
        index_sync_state__in=["completed", "not_required"]
    )
    eligible_count = LogClearAudit.objects.filter(terminal_q).count()

    # 未决状态（可能仍处于重试或人工排障中，受安全保护豁免删除）
    unresolved_q = Q(created_at__lt=cutoff) & ~terminal_q
    unresolved_count = LogClearAudit.objects.filter(unresolved_q).count()

    return {
        "retention_days": days,
        "total_count": total_count,
        "oldest_date": oldest_date,
        "cutoff_date": cutoff,
        "eligible_count": eligible_count,
        "unresolved_count": unresolved_count,
    }


def purge_expired_audits(
    days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = DEFAULT_PURGE_BATCH_SIZE,
    dry_run: bool = False,
    backup: bool = True,
) -> dict:
    """清理超期（超过指定保留天数）的日志清理审计记录与关联任务。

    执行严格的终态安全门禁与分批不锁表删除策略；清理前将待删除记录压缩归档至冷存储。

    参数:
        days: 保留天数，默认 180 天。
        batch_size: 每次事务删除批次大小，防止长事务锁表。
        dry_run: 是否仅演练而不实际删除。
        backup: 删除前是否先备份至 JSON 压缩归档文件。
    返回:
        包含 matched_count, deleted_count, batches, backup_path 等信息的字典。
    """
    cutoff = timezone.now() - timedelta(days=days)
    terminal_q = Q(created_at__lt=cutoff) & Q(state__in=["completed", "failed"]) & Q(
        index_sync_state__in=["completed", "not_required"]
    )

    matching_qs = LogClearAudit.objects.filter(terminal_q).order_by("created_at")
    matched_count = matching_qs.count()

    if matched_count == 0 or dry_run:
        return {
            "matched_count": matched_count,
            "deleted_count": 0,
            "batches": 0,
            "dry_run": dry_run,
            "backup_path": "",
        }

    backup_path_str = ""
    if backup:
        archive_dir = Path(settings.BASE_DIR) / "backups" / "observability"
        archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = timezone.now().strftime("%Y%m%d_%H%M%S")
        backup_file = archive_dir / f"audit_archive_{timestamp_str}.json.gz"

        archive_records = []
        for audit in matching_qs:
            archive_records.append({
                "id": audit.id,
                "idempotency_key": str(audit.idempotency_key),
                "created_at": audit.created_at.isoformat() if audit.created_at else None,
                "completed_at": audit.completed_at.isoformat() if audit.completed_at else None,
                "user": audit.user.username if audit.user else None,
                "ip_address": audit.ip_address,
                "target_type": audit.target_type,
                "target": audit.target,
                "kind": audit.kind,
                "scope": audit.scope,
                "bytes_before": audit.bytes_before,
                "bytes_freed": audit.bytes_freed,
                "succeeded_files": audit.succeeded_files,
                "failed_files": audit.failed_files,
                "state": audit.state,
                "index_sync_state": audit.index_sync_state,
                "details": audit.details,
            })

        with gzip.open(backup_file, "wt", encoding="utf-8") as gz_f:
            json.dump(archive_records, gz_f, ensure_ascii=False, indent=2)

        backup_path_str = str(backup_file)
        logger.info(
            "日志审计清理：已将 %d 条待清理审计记录导出备份至 %s",
            matched_count,
            backup_path_str,
        )

    deleted_total = 0
    batches = 0

    while True:
        batch_ids = list(
            LogClearAudit.objects.filter(terminal_q)
            .values_list("id", flat=True)[:batch_size]
        )
        if not batch_ids:
            break

        with transaction.atomic():
            deleted_count, _ = LogClearAudit.objects.filter(id__in=batch_ids).delete()
            deleted_total += deleted_count
            batches += 1

    logger.info(
        "日志审计生命周期清理完成：共删除 %d 条超期审计记录，执行 %d 个批次，备份文件: %s",
        deleted_total,
        batches,
        backup_path_str,
    )

    return {
        "matched_count": matched_count,
        "deleted_count": deleted_total,
        "batches": batches,
        "dry_run": False,
        "backup_path": backup_path_str,
    }
