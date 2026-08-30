"""访问限制变化后的 BlogPage 搜索范围重算。"""

from __future__ import annotations

from datetime import timedelta
import os
import socket
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Case, F, Value, When
from django.utils import timezone
from wagtail.models import Page

from blog.models import BlogPage
from search.models import (
    ContentSearchOperation,
    ContentSearchScopeJob,
    ContentSearchScopeJobStatus,
    ContentSearchState,
)
def _lease_seconds() -> int:
    try:
        return max(1, int(getattr(settings, "CONTENT_SEARCH_SCOPE_LEASE_SECONDS", 120)))
    except (TypeError, ValueError, OverflowError):
        return 120


def _batch_size(limit: int | None = None) -> int:
    value = limit if limit is not None else getattr(settings, "CONTENT_SEARCH_SCOPE_BATCH_SIZE", 100)
    try:
        return max(1, min(int(value), 500))
    except (TypeError, ValueError, OverflowError):
        return 100


def _max_attempts() -> int:
    try:
        return max(1, int(getattr(settings, "CONTENT_SEARCH_SCOPE_MAX_ATTEMPTS", 10)))
    except (TypeError, ValueError, OverflowError):
        return 10


def _owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def claim_scope_job(job_id: int) -> tuple[ContentSearchScopeJob | None, str]:
    """领取一个范围任务租约。

    参数：``job_id`` 为精确任务主键。
    返回：``(job, owner)``；任务已被其他 Worker 持有或已完成时返回 ``(None, "")``。
    副作用：只更新 ScopeJob 的租约和尝试次数，不触碰页面正文或 Elasticsearch。
    """
    now = timezone.now()
    owner = _owner()
    with transaction.atomic():
        job = ContentSearchScopeJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None or job.status in (ContentSearchScopeJobStatus.SUCCEEDED, ContentSearchScopeJobStatus.DEAD):
            return None, ""
        if (
            job.status == ContentSearchScopeJobStatus.PROCESSING
            and job.lock_expires_at is not None
            and job.lock_expires_at > now
        ):
            return None, ""
        # attempts 表示连续失败/租约回收次数，而不是已处理批次数；否则大子树会在正常分页时耗尽重试预算。
        if job.status == ContentSearchScopeJobStatus.PROCESSING:
            job.attempts += 1
        job.status = ContentSearchScopeJobStatus.PROCESSING
        job.locked_by = owner
        job.lock_expires_at = now + timedelta(seconds=_lease_seconds())
        job.last_error_code = ""
        job.last_error_message = ""
        job.save(update_fields=("status", "attempts", "locked_by", "lock_expires_at", "last_error_code", "last_error_message", "updated_at"))
    return job, owner


def _reconcile_page(page_id: int) -> None:
    """使单页 State 的可搜索意图与当前 Wagtail public 状态一致。

    只有两者不一致时才调用 Outbox 服务，保证任务重试幂等；正文版本仍由正式发布流程决定。
    """
    # outbox 模块会导入 tasks，而 tasks 又注册 ScopeJob；延迟导入可避免应用启动时循环依赖。
    from search.services.outbox import ContentSearchOutboxService

    page = BlogPage.objects.filter(pk=page_id).first()
    if page is None:
        return
    is_public = BlogPage.objects.live().public().filter(pk=page_id).exists()
    with transaction.atomic():
        state = ContentSearchState.objects.select_for_update().filter(pk=page_id).first()
        state_public = bool(
            state
            and state.searchable
            and state.desired_operation == ContentSearchOperation.UPSERT
        )
        if state_public == is_public:
            return
        if is_public:
            ContentSearchOutboxService.record_publication(page)
        else:
            ContentSearchOutboxService.record_unpublish(page)


def process_scope_job(job_id: int, limit: int | None = None) -> str:
    """处理一个稳定主键批次并推进 ScopeJob 检查点。

    页面按 ``pk`` 升序限制在根页面子树内；每页事件提交后才推进检查点。
    异常只记录脱敏错误编码并将任务置为 retry/dead，下一次可从原检查点继续。
    """
    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        return "disabled"
    job, owner = claim_scope_job(job_id)
    if job is None:
        return "skipped"
    try:
        # 限制可挂在任意 Wagtail Page（如 BlogIndexPage）上，根节点不能限定为 BlogPage。
        root = Page.objects.filter(pk=job.root_page_id).only("pk", "path").first()
        if root is None:
            return _finish_scope_job(job_id, owner, ContentSearchScopeJobStatus.DEAD, "scope_root_missing")
        page_ids = list(
            BlogPage.objects.filter(path__startswith=root.path, pk__gt=job.checkpoint_page_id)
            .order_by("pk")
            .values_list("pk", flat=True)[: _batch_size(limit)]
        )
        if page_ids:
            for page_id in page_ids:
                _reconcile_page(page_id)
            return _advance_scope_job(job_id, owner, page_ids[-1])
        return _finish_scope_job(job_id, owner, ContentSearchScopeJobStatus.SUCCEEDED, "")
    except Exception:
        # 仅在当前 Worker 持有租约时递增失败计数，避免并发 Worker 覆盖彼此状态。
        with transaction.atomic():
            failed_job = ContentSearchScopeJob.objects.select_for_update().get(pk=job_id)
            if failed_job.locked_by != owner:
                return failed_job.status
            failed_job.attempts += 1
            failed_attempts = failed_job.attempts
            failed_job.save(update_fields=("attempts", "updated_at"))
        status = ContentSearchScopeJobStatus.DEAD if failed_attempts >= _max_attempts() else ContentSearchScopeJobStatus.RETRY
        return _finish_scope_job(job_id, owner, status, "scope_unexpected_error")


def _advance_scope_job(job_id: int, owner: str, checkpoint: int) -> str:
    with transaction.atomic():
        job = ContentSearchScopeJob.objects.select_for_update().get(pk=job_id)
        if job.locked_by != owner:
            return job.status
        job.checkpoint_page_id = checkpoint
        if job.rescan_requested:
            job.checkpoint_page_id = 0
            job.rescan_requested = False
            job.status = ContentSearchScopeJobStatus.PENDING
        else:
            job.status = ContentSearchScopeJobStatus.PENDING
        # 批次已成功提交，后续失败不应继承本次之前的重试次数。
        job.attempts = 0
        job.locked_by = ""
        job.lock_expires_at = None
        job.save(update_fields=("checkpoint_page_id", "rescan_requested", "status", "attempts", "locked_by", "lock_expires_at", "updated_at"))
        return job.status


def _finish_scope_job(job_id: int, owner: str, status: str, error_code: str) -> str:
    with transaction.atomic():
        job = ContentSearchScopeJob.objects.select_for_update().get(pk=job_id)
        if job.locked_by != owner:
            return job.status
        job.status = status
        if status == ContentSearchScopeJobStatus.SUCCEEDED:
            job.attempts = 0
        job.locked_by = ""
        job.lock_expires_at = None
        job.last_error_code = error_code
        job.last_error_message = ""
        update_fields = [
            "status",
            "locked_by",
            "lock_expires_at",
            "last_error_code",
            "last_error_message",
            "updated_at",
        ]
        if status == ContentSearchScopeJobStatus.SUCCEEDED:
            update_fields.append("attempts")
        job.save(update_fields=update_fields)
        return job.status


def due_scope_job_ids(limit: int | None = None) -> list[int]:
    """返回可领取的范围任务主键，并回收已过期租约。"""
    now = timezone.now()
    # 调度器回收过期租约时记一次失败；达到上限直接进入死信，避免无主任务无限重投。
    max_attempts = _max_attempts()
    ContentSearchScopeJob.objects.filter(
        status=ContentSearchScopeJobStatus.PROCESSING,
        lock_expires_at__lte=now,
    ).update(
        status=Case(
            When(
                attempts__gte=max_attempts - 1,
                then=Value(ContentSearchScopeJobStatus.DEAD),
            ),
            default=Value(ContentSearchScopeJobStatus.RETRY),
        ),
        attempts=F("attempts") + 1,
        locked_by="",
        lock_expires_at=None,
    )
    return list(
        ContentSearchScopeJob.objects.filter(
            status__in=(ContentSearchScopeJobStatus.PENDING, ContentSearchScopeJobStatus.RETRY)
        ).order_by("created_at", "pk").values_list("pk", flat=True)[: _batch_size(limit)]
    )
