"""日志清理后的 Elasticsearch 可靠同步任务。

职责：把本地文件清理产生的 ``LogIndexSyncJob`` outbox 记录异步投递为
``delete_by_query``，并把执行状态回写 ``LogClearAudit``。数据流为：后台清理
文件 -> 事务内创建 outbox -> Celery worker 首次删除/轮询 -> 延迟补偿删除 -> 审计完成。
关键依赖：Django 事务与 MySQL outbox、Celery maintenance 队列、Elasticsearch 日志读别名。
MongoDB、页面正文和业务数据不参与本模块。
"""

from __future__ import annotations

from datetime import timedelta
import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .elasticsearch_logs import (
    LogIndexCleanupPlan,
    LogIndexSyncError,
    delete_logs_by_plan,
    get_delete_task_result,
)
from .models import LogClearAudit, LogIndexSyncJob


logger = logging.getLogger(__name__)
RETRY_DELAYS = (30, 120, 600, 1800, 7200, 21600)
COMPENSATION_DELAY = 10
POLL_DELAY = 10
TASK_LEASE = 10 * 60


def _sync_config() -> dict:
    """读取并规范化 ES 日志同步配置。

    参数：无。
    返回：字典配置；配置值不是字典时返回空字典。
    异常：无，配置异常在具体阈值函数中使用安全默认值处理。
    """
    value = getattr(settings, "ELASTICSEARCH_LOGGING", {})
    return value if isinstance(value, dict) else {}


def _sync_threshold() -> int:
    """返回可同步等待 ES 删除完成的最大字节数。

    参数：无。
    返回：非负字节阈值。
    异常：配置转换错误不向上抛出，使用 10 MiB 默认值以保证任务可执行。
    """
    try:
        value = int(_sync_config().get("DELETE_SYNC_MAX_BYTES", 10 * 1024 * 1024))
    except (TypeError, ValueError, OverflowError):
        value = 10 * 1024 * 1024
    return max(0, value)


def _sync_selector_threshold() -> int:
    """返回可同步等待的最大删除选择器数量。

    参数：无。
    返回：限制在 1 到 512 之间的整数。
    异常：无效配置使用默认值 12，防止超大布尔查询阻塞 worker。
    """
    try:
        value = int(_sync_config().get("DELETE_SYNC_MAX_SELECTORS", 12))
    except (TypeError, ValueError, OverflowError):
        value = 12
    return max(1, min(value, 512))


def _audit_details(job: LogIndexSyncJob) -> dict:
    """把 outbox 的可展示状态投影为审计详情。

    参数：``job`` 为持久化的索引同步任务。
    返回：可安全写入 JSONField 的基本类型字典。
    异常：无。
    """
    return {
        "state": job.state,
        "attempts": job.attempts,
        "deleted_documents": job.deleted_documents,
        "es_task_id": job.es_task_id,
        "last_error": job.last_error,
        "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _update_audit(job: LogIndexSyncJob) -> None:
    """把 outbox 当前状态同步到其关联清理审计。

    参数：``job`` 必须已关联 ``LogClearAudit``。
    返回：无。
    异常：数据库保存错误向上抛出，避免任务表与审计表静默分叉。

    审计页只读取 ``LogClearAudit``，因此每次状态迁移都要同步这份投影。
    """
    audit = job.audit
    audit.index_sync_state = job.state
    audit.index_sync_attempts = job.attempts
    audit.index_sync_deleted = job.deleted_documents
    audit.index_sync_task_id = job.es_task_id
    audit.index_sync_last_error = job.last_error
    audit.index_sync_completed_at = job.completed_at
    details = dict(audit.details or {})
    details["index_sync"] = _audit_details(job)
    audit.details = details
    audit.save(
        update_fields=(
            "index_sync_state",
            "index_sync_attempts",
            "index_sync_deleted",
            "index_sync_task_id",
            "index_sync_last_error",
            "index_sync_completed_at",
            "details",
        )
    )


def enqueue_log_index_sync(
    audit: LogClearAudit,
    plan: LogIndexCleanupPlan,
) -> LogIndexSyncJob:
    """Create one idempotent outbox job for a completed file cleanup."""
    job, _created = LogIndexSyncJob.objects.get_or_create(
        audit=audit,
        defaults={
            "selector": plan.as_payload(),
            "state": "pending",
            "next_retry_at": timezone.now(),
        },
    )
    _update_audit(job)

    def dispatch() -> None:
        """在外层事务提交后投递任务。

        参数：无，闭包捕获已持久化任务主键。
        返回：无。
        异常：投递异常被记录并保留 pending outbox，后续 Beat 会补偿调度。
        """
        try:
            sync_log_index.apply_async(args=(job.pk,), queue="maintenance")
        except Exception:
            # The durable row is the source of truth; Beat will recover it.
            logger.warning(
                "Unable to dispatch Elasticsearch log cleanup; outbox remains pending",
                extra={
                    "event": "observability.elasticsearch.cleanup_dispatch_failed",
                    "audit_id": job.audit_id,
                },
            )

    transaction.on_commit(dispatch)
    return job


def _schedule(job: LogIndexSyncJob, delay: int, *, state: str = "pending") -> None:
    """为任务写入下一次可执行时间与状态。

    参数：``job`` 为 outbox，``delay`` 为秒数，``state`` 为 pending 或 running。
    返回：无。
    异常：数据库错误向上抛出。

    不直接递归投递 Celery，避免网络短暂故障造成重复消息；Beat 以数据库状态为准补偿。
    """
    job.state = state
    job.next_retry_at = timezone.now() + timedelta(seconds=delay)
    job.save(update_fields=("state", "next_retry_at", "updated_at"))
    _update_audit(job)


def _complete(job: LogIndexSyncJob) -> None:
    """标记两阶段 ES 删除成功完成。

    参数：``job`` 为已完成补偿阶段的 outbox。
    返回：无。
    异常：数据库错误向上抛出。
    """
    job.state = "completed"
    job.next_retry_at = None
    job.es_task_id = ""
    job.last_error = ""
    job.completed_at = timezone.now()
    job.save(
        update_fields=(
            "state",
            "next_retry_at",
            "es_task_id",
            "last_error",
            "completed_at",
            "updated_at",
        )
    )
    _update_audit(job)


def _fail(job: LogIndexSyncJob, exc: LogIndexSyncError) -> None:
    """记录 ES 同步失败并决定重试或死信。

    参数：``job`` 为当前 outbox，``exc`` 包含瞬态性与失败原因。
    返回：无。
    异常：数据库错误向上抛出。

    失败计数保存在 selector payload 中，保证 worker 重启后退避策略不丢失。
    """
    job.last_error = str(exc)[:4000]
    payload = dict(job.selector or {})
    failure_attempts = max(0, int(payload.get("failure_attempts", 0) or 0)) + 1
    payload["failure_attempts"] = failure_attempts
    job.selector = payload
    if exc.transient and failure_attempts <= len(RETRY_DELAYS):
        delay = RETRY_DELAYS[failure_attempts - 1]
        job.save(update_fields=("selector", "last_error", "updated_at"))
        _schedule(job, delay)
        return
    job.state = "dead_letter"
    job.next_retry_at = None
    job.dead_letter_at = timezone.now()
    job.save(
        update_fields=(
            "state",
            "next_retry_at",
            "selector",
            "last_error",
            "dead_letter_at",
            "updated_at",
        )
    )
    _update_audit(job)
    logger.error(
        "Elasticsearch log cleanup entered dead letter",
        extra={
            "event": "observability.elasticsearch.cleanup_dead_letter",
            "audit_id": job.audit_id,
            "attempts": job.attempts,
        },
    )


def run_log_index_sync(job_id: int) -> str:
    """Execute or poll one outbox job; safe to call more than once."""
    with transaction.atomic():
        job = (
            LogIndexSyncJob.objects.select_for_update()
            .select_related("audit")
            .get(pk=job_id)
        )
        now = timezone.now()
        if job.state not in {"pending", "running"}:
            return job.state
        if job.next_retry_at and job.next_retry_at > now:
            return job.state
        job.state = "running"
        job.started_at = job.started_at or now
        job.next_retry_at = now + timedelta(seconds=TASK_LEASE)
        job.attempts += 1
        job.save(
            update_fields=(
                "state",
                "started_at",
                "next_retry_at",
                "attempts",
                "updated_at",
            )
        )
        _update_audit(job)

    plan = LogIndexCleanupPlan.from_payload(job.selector)
    try:
        if job.es_task_id:
            completed, result = get_delete_task_result(job.es_task_id)
            if not completed:
                _schedule(job, POLL_DELAY, state="running")
                return job.state
        else:
            result = delete_logs_by_plan(
                plan,
                wait_for_completion=(
                    plan.estimated_bytes <= _sync_threshold()
                    and len(plan.selectors) <= _sync_selector_threshold()
                ),
            )
            if result.task_id:
                job.es_task_id = result.task_id
                job.save(update_fields=("es_task_id", "updated_at"))
                _schedule(job, POLL_DELAY, state="running")
                return job.state
        if result.timed_out:
            raise LogIndexSyncError("Elasticsearch log cleanup timed out")
    except LogIndexSyncError as exc:
        _fail(job, exc)
        return job.state

    job.deleted_documents += result.deleted
    payload = dict(job.selector or {})
    if payload.get("phase") != "compensation":
        payload["phase"] = "compensation"
        job.selector = payload
        job.es_task_id = ""
        job.save(
            update_fields=(
                "selector",
                "es_task_id",
                "deleted_documents",
                "updated_at",
            )
        )
        _schedule(job, COMPENSATION_DELAY)
        return job.state

    job.save(update_fields=("deleted_documents", "updated_at"))
    _complete(job)
    return job.state


@shared_task(name="observability.tasks.sync_log_index")
def sync_log_index(job_id: int) -> str:
    """Celery 入口：执行或轮询一个日志索引同步任务。

    参数：``job_id`` 为 ``LogIndexSyncJob`` 主键。
    返回：任务最终或当前状态字符串。
    异常：未找到任务、数据库故障等非 ES 业务错误向 Celery 传播。
    """
    return run_log_index_sync(job_id)


@shared_task(name="observability.tasks.dispatch_pending_log_index_sync_jobs")
def dispatch_pending_log_index_sync_jobs(limit: int = 100) -> int:
    """Recover due outbox jobs after broker or worker interruptions."""
    now = timezone.now()
    job_ids = list(
        LogIndexSyncJob.objects.filter(
            state__in=("pending", "running"),
        )
        .filter(Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now))
        .order_by("next_retry_at", "pk")
        .values_list("pk", flat=True)[: max(1, min(int(limit), 500))]
    )
    for job_id in job_ids:
        sync_log_index.apply_async(args=(job_id,), queue="maintenance")
    return len(job_ids)



@shared_task(name="observability.tasks.cleanup_expired_log_audits")
def cleanup_expired_log_audits(retention_days: int = 180, batch_size: int = 500) -> dict:
    """定时任务：自动清理超期（默认180天/6个月）的日志清理审计记录与Outbox数据。

    遵循严格的终态安全门禁，并在删除前将审计数据导出压缩备份至冷存储。

    参数:
        retention_days: 保留天数，默认 180 天。
        batch_size: 每批次删除记录数，默认 500。
    返回:
        执行统计结果字典。
    """
    from .services import purge_expired_audits

    logger.info(
        "Celery 周期任务启动：开始执行超期日志审计生命周期清理 (retention_days=%d)...",
        retention_days,
    )
    result = purge_expired_audits(days=retention_days, batch_size=batch_size, dry_run=False, backup=True)
    logger.info(
        "Celery 周期任务完成：超期日志审计清理结果: 匹配 %d 条，已删除 %d 条，共 %d 批次",
        result.get("matched_count", 0),
        result.get("deleted_count", 0),
        result.get("batches", 0),
    )
    return result
