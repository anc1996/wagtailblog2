"""博客分析的 Celery 维护任务。"""

import uuid
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from django.core.files.storage import storages
from django.db.models import Q
from django.utils import timezone

from blog.models import (
    MarkdownImportArtifact,
    MarkdownImportArtifactCleanupStatus,
    MarkdownImportSession,
    MarkdownImportSessionStatus,
)
from blog.services.markdown_import_media import cleanup_artifact_object


def _cleanup_storage_registry(artifact):
    try:
        return {artifact.storage_alias: storages[artifact.storage_alias]}
    except Exception:
        return {}


def _artifact_is_referenced(artifact):
    if not artifact.media_model or artifact.media_object_id is None:
        return False
    return MarkdownImportArtifact.objects.filter(
        media_model=artifact.media_model,
        media_object_id=artifact.media_object_id,
    ).exclude(pk=artifact.pk).exists()


@shared_task(name="blog.tasks.cleanup_markdown_import_artifact")
def cleanup_markdown_import_artifact(artifact_id):
    """按明确 artifact UUID 清理对象；失败保留审计并采用有限退避。"""

    try:
        artifact_uuid = uuid.UUID(str(artifact_id))
    except (TypeError, ValueError):
        return {"status": "invalid_artifact_id"}
    artifact = MarkdownImportArtifact.objects.filter(artifact_id=artifact_uuid).first()
    if artifact is None:
        return {"status": "not_found"}
    if artifact.cleanup_status not in {
        MarkdownImportArtifactCleanupStatus.PENDING,
        MarkdownImportArtifactCleanupStatus.RETRY,
    }:
        return {"status": "noop", "cleanup_status": artifact.cleanup_status}

    max_attempts = max(1, int(getattr(settings, "MARKDOWN_IMPORT_CLEANUP_MAX_ATTEMPTS", 5)))
    attempts = int(getattr(artifact, "cleanup_attempts", 0) or 0)
    if attempts >= max_attempts:
        return {"status": "exhausted", "attempts": attempts}

    cleaned = cleanup_artifact_object(
        artifact,
        storages=_cleanup_storage_registry(artifact),
        reference_guard=_artifact_is_referenced,
    )
    if cleaned:
        artifact.cleanup_attempts = attempts + 1
        artifact.cleanup_next_attempt_at = None
        artifact.save(update_fields=["cleanup_attempts", "cleanup_next_attempt_at", "updated_at"])
        return {"status": "cleaned", "attempts": artifact.cleanup_attempts}

    attempts += 1
    artifact.cleanup_attempts = attempts
    if attempts >= max_attempts:
        artifact.cleanup_next_attempt_at = None
    else:
        base_delay = max(1, int(getattr(settings, "MARKDOWN_IMPORT_CLEANUP_RETRY_BASE_SECONDS", 60)))
        artifact.cleanup_next_attempt_at = timezone.now() + timedelta(
            seconds=base_delay * (2 ** (attempts - 1))
        )
    artifact.save(update_fields=["cleanup_attempts", "cleanup_next_attempt_at", "updated_at"])
    return {"status": "retry", "attempts": attempts}


@shared_task(name="blog.tasks.dispatch_markdown_import_cleanup_retries")
def dispatch_markdown_import_cleanup_retries():
    """只投递明确到期的 cleanup_retry artifact，不扫描存储桶或批次前缀。"""

    if not getattr(settings, "MARKDOWN_IMPORT_CLEANUP_RETRY_ENABLED", True):
        return {"status": "disabled", "count": 0}
    now = timezone.now()
    max_attempts = max(1, int(getattr(settings, "MARKDOWN_IMPORT_CLEANUP_MAX_ATTEMPTS", 5)))
    limit = max(1, int(getattr(settings, "MARKDOWN_IMPORT_CLEANUP_BATCH_SIZE", 100)))
    artifact_ids = list(
        MarkdownImportArtifact.objects.filter(
            cleanup_status=MarkdownImportArtifactCleanupStatus.RETRY,
            cleanup_attempts__lt=max_attempts,
        )
        .filter(Q(cleanup_next_attempt_at__isnull=True) | Q(cleanup_next_attempt_at__lte=now))
        .order_by("updated_at")
        .values_list("artifact_id", flat=True)[:limit]
    )
    for artifact_id in artifact_ids:
        cleanup_markdown_import_artifact.delay(str(artifact_id))
    return {"status": "dispatched", "count": len(artifact_ids)}


@shared_task(name="blog.tasks.assemble_markdown_import_session")
def assemble_markdown_import_session(session_id):
    """异步组装已上传媒体；任务可安全重复投递。"""

    from blog.services.markdown_import_sessions import assemble_session

    return assemble_session(str(session_id))


@shared_task(name="blog.tasks.expire_markdown_import_sessions")
def expire_markdown_import_sessions():
    """只标记过期会话并投递精确 artifact 清理，不按存储前缀扫描。"""

    now = timezone.now()
    sessions = list(
        MarkdownImportSession.objects.filter(
            expires_at__lte=now,
            status__in=[
                MarkdownImportSessionStatus.CREATED,
                MarkdownImportSessionStatus.UPLOADING,
                MarkdownImportSessionStatus.READY,
            ],
        ).values_list("session_id", flat=True)[:100]
    )
    for session_id in sessions:
        session = MarkdownImportSession.objects.filter(session_id=session_id).first()
        if session is None:
            continue
        from blog.services.markdown_import_sessions import _mark_expired

        _mark_expired(session)
        for artifact_id in session.artifacts.filter(
            cleanup_status__in=[MarkdownImportArtifactCleanupStatus.PENDING, MarkdownImportArtifactCleanupStatus.RETRY]
        ).values_list("artifact_id", flat=True):
            cleanup_markdown_import_artifact.delay(str(artifact_id))
    return {"status": "dispatched", "count": len(sessions)}


@shared_task(name="blog.tasks.cleanup_analytics_details")
def cleanup_analytics_details():
    """按保留期清理短期明细；默认关闭，生产启用前必须取得数据清理授权。"""

    if not getattr(settings, "BLOG_ANALYTICS_CLEANUP_ENABLED", False):
        return {"status": "disabled"}
    call_command(
        "cleanup_pageviews",
        confirm=True,
        days=getattr(settings, "BLOG_PAGEVIEW_RETENTION_DAYS", 30),
        batch_size=getattr(settings, "BLOG_ANALYTICS_CLEANUP_BATCH_SIZE", 500),
    )
    return {"status": "completed"}
