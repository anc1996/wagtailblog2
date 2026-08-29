"""博客分析的 Celery 维护任务。"""

import uuid
import json
import os
import socket
import logging
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from django.core.files.storage import storages
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType

from blog.models import (
    MarkdownImportArtifact,
    MarkdownImportArtifactCleanupStatus,
    MarkdownImportSession,
    MarkdownImportSessionStatus,
    BlogPage,
    BlogPublicationConsistencyCheckpoint,
)
from blog.services.markdown_import_media import cleanup_artifact_object
from blog.services import publication_consistency as publication_consistency_service
from blog.models import MongoCleanupIntent, MongoCleanupIntentStatus
from wagtail.models import Revision
from wagtailblog3.mongo import MongoManager


logger = logging.getLogger(__name__)


def check_blog_publication_consistency(*args: Any, **kwargs: Any) -> dict[str, Any]:
	"""转发对账服务，保留可测试的任务模块入口。"""
	return publication_consistency_service.check_blog_publication_consistency(*args, **kwargs)


def _publication_consistency_lease_seconds() -> int:
	"""返回对账周期租约秒数，异常配置回退到保守默认值。"""
	try:
		value = int(getattr(settings, "BLOG_PUBLICATION_CONSISTENCY_LEASE_SECONDS", 240))
	except (TypeError, ValueError, OverflowError):
		value = 240
	return max(1, min(value, 3600))


def _publication_consistency_owner() -> str:
	"""生成本次对账周期的唯一租约 owner。"""
	return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


@shared_task(name="blog.tasks.check_publication_consistency", ignore_result=True)
def check_publication_consistency(
	limit: object | None = None,
	after_page_id: object | None = None,
) -> dict[str, Any]:
	"""按有界批次执行发布对账；周期模式只写独立 checkpoint 元数据。"""
	try:
		batch_limit = int(
			getattr(settings, "BLOG_PUBLICATION_CONSISTENCY_BATCH_SIZE", 100)
			if limit is None
			else limit
		)
	except (TypeError, ValueError, OverflowError):
		batch_limit = 100
	batch_limit = max(1, min(batch_limit, 5000))
	try:
		cursor = 0 if after_page_id is None else max(0, int(after_page_id))
	except (TypeError, ValueError, OverflowError):
		cursor = 0
	periodic = limit is None and after_page_id is None
	checkpoint = None
	owner = None
	upper_bound = None
	if periodic:
		now = timezone.now()
		owner = _publication_consistency_owner()
		with transaction.atomic():
			checkpoint, _ = BlogPublicationConsistencyCheckpoint.objects.select_for_update().get_or_create(
				scope="blog_publication_consistency", defaults={"last_counts": {}},
			)
			if checkpoint.lease_expires_at and checkpoint.lease_expires_at > now:
				return {"status": "lease_busy", "scanned": 0, "counts": {}}
			if checkpoint.cursor_page_id == 0 or checkpoint.scan_upper_bound_page_id is None:
				checkpoint.scan_upper_bound_page_id = BlogPage.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
			checkpoint.lease_owner = owner
			checkpoint.lease_expires_at = now + timedelta(seconds=_publication_consistency_lease_seconds())
			checkpoint.last_started_at = now
			checkpoint.last_error = ""
			checkpoint.save(update_fields=("scan_upper_bound_page_id", "lease_owner", "lease_expires_at", "last_started_at", "last_error", "updated_at"))
			cursor = checkpoint.cursor_page_id
			upper_bound = checkpoint.scan_upper_bound_page_id
	try:
		report = check_blog_publication_consistency(
			after_page_id=cursor,
			limit=batch_limit,
			sample_limit=20,
			check_mongo=False,
			upper_bound_page_id=upper_bound,
		)
	except Exception as exc:
		if periodic and checkpoint is not None and owner is not None:
			# 对账只读失败时释放租约并记录异常类型，避免后续周期长期被占用。
			BlogPublicationConsistencyCheckpoint.objects.filter(
				pk=checkpoint.pk, lease_owner=owner,
			).update(
				last_error=type(exc).__name__[:255],
				lease_owner="",
				lease_expires_at=None,
				updated_at=timezone.now(),
			)
		raise
	if periodic and checkpoint is not None and owner is not None:
		now = timezone.now()
		finished = not report["scanned"] or report["next_after_page_id"] >= (upper_bound or 0)
		checkpoint_update = {
			"cursor_page_id": 0 if finished else report["next_after_page_id"],
			"scan_upper_bound_page_id": None if finished else upper_bound,
			"cycle": checkpoint.cycle + (1 if finished else 0),
			"last_scanned": report["scanned"], "last_counts": report["counts"],
			"lease_owner": "", "lease_expires_at": None, "updated_at": now,
		}
		if finished:
			checkpoint_update["last_completed_at"] = now
		# 部分批次保留上次完成时间，监控可区分“正在轮转”和“长期未完成”。
		updated = BlogPublicationConsistencyCheckpoint.objects.filter(
			pk=checkpoint.pk, lease_owner=owner,
		).update(**checkpoint_update)
		if updated != 1:
			# 条件更新未命中表示租约已过期或被其他 worker 接管，不能误报本批已完成。
			logger.warning(
				"blog_publication_consistency_lease_lost scanned=%s next_after_page_id=%s",
				report["scanned"],
				report["next_after_page_id"],
			)
			return {
				"status": "lease_lost",
				"scanned": report["scanned"],
				"counts": report["counts"],
				"next_after_page_id": report["next_after_page_id"],
			}
	logger.info(
		"blog_publication_consistency scanned=%s counts=%s next_after_page_id=%s",
		report["scanned"],
		report["counts"],
		report["next_after_page_id"],
	)
	return {
		"status": "completed",
		"scanned": report["scanned"],
		"counts": report["counts"],
		"next_after_page_id": report["next_after_page_id"],
	}


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


def _mongo_cleanup_lease_seconds() -> int:
	"""返回清理任务租约秒数；异常配置使用保守的两分钟默认值。"""
	try:
		value = int(getattr(settings, "MONGO_CLEANUP_LEASE_SECONDS", 120))
	except (TypeError, ValueError, OverflowError):
		value = 120
	return max(1, value)


def _mongo_cleanup_batch_size(limit: object | None = None) -> int:
	"""限制 Beat 每轮扫描的意图数量，避免一次性向 broker 投递过多任务。"""
	try:
		value = int(getattr(settings, "MONGO_CLEANUP_BATCH_SIZE", 100) if limit is None else limit)
	except (TypeError, ValueError, OverflowError):
		value = 100
	return max(1, min(value, 500))


def _mongo_cleanup_owner() -> str:
	"""生成本次任务唯一 owner，防止过期 worker 覆盖新 worker 的状态。"""
	return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def _claim_mongo_cleanup_intent(intent_id: object) -> tuple[MongoCleanupIntent | None, str | None]:
	"""以行锁认领一个清理意图，并在 Mongo I/O 前提交租约状态。"""
	now = timezone.now()
	owner = _mongo_cleanup_owner()
	with transaction.atomic():
		intent = (
			MongoCleanupIntent.objects.select_for_update(skip_locked=True)
			.filter(intent_id=intent_id)
			.first()
		)
		if intent is None:
			return None, None
		if intent.status in {
			MongoCleanupIntentStatus.SUCCEEDED,
			MongoCleanupIntentStatus.SKIPPED,
			MongoCleanupIntentStatus.DEAD,
		}:
			return None, None
		if intent.status == MongoCleanupIntentStatus.PROCESSING:
			if intent.lock_expires_at and intent.lock_expires_at > now:
				return None, None
		elif intent.available_at > now:
			return None, None
		intent.status = MongoCleanupIntentStatus.PROCESSING
		intent.attempts += 1
		intent.locked_by = owner
		intent.lock_expires_at = now + timedelta(seconds=_mongo_cleanup_lease_seconds())
		intent.save(update_fields=("status", "attempts", "locked_by", "lock_expires_at", "updated_at"))
	return intent, owner


def _finish_claimed_intent(intent: MongoCleanupIntent, owner: str, **updates: Any) -> bool:
	"""仅允许当前租约 owner 写回状态，避免旧 worker 覆盖新认领结果。"""
	updates.update(locked_by="", lock_expires_at=None)
	return bool(
		MongoCleanupIntent.objects.filter(
			pk=intent.pk,
			status=MongoCleanupIntentStatus.PROCESSING,
			locked_by=owner,
		).update(**updates)
	)


def reclaim_expired_mongo_cleanup_intents(limit: object | None = None) -> int:
	"""回收崩溃 worker 遗留的租约，使意图重新进入可投递状态。"""
	now = timezone.now()
	intent_ids = list(
		MongoCleanupIntent.objects.filter(
			status=MongoCleanupIntentStatus.PROCESSING,
			lock_expires_at__lte=now,
		)
		.order_by("lock_expires_at", "pk")
		.values_list("pk", flat=True)[: _mongo_cleanup_batch_size(limit)]
	)
	reclaimed = 0
	for intent_id in intent_ids:
		with transaction.atomic():
			intent = MongoCleanupIntent.objects.select_for_update().filter(pk=intent_id).first()
			if (
				intent is None
				or intent.status != MongoCleanupIntentStatus.PROCESSING
				or intent.lock_expires_at is None
				or intent.lock_expires_at > now
			):
				continue
			intent.status = MongoCleanupIntentStatus.RETRY
			intent.available_at = now
			intent.lease_reclaims += 1
			intent.locked_by = ""
			intent.lock_expires_at = None
			intent.save(update_fields=("status", "available_at", "lease_reclaims", "locked_by", "lock_expires_at", "updated_at"))
			reclaimed += 1
	return reclaimed


@shared_task(name="blog.tasks.cleanup_mongo_intent")
def cleanup_mongo_intent(intent_id: object) -> dict[str, Any]:
	"""消费 Mongo 清理意图；先租约认领，再核验引用并执行物理删除。"""
	intent, owner = _claim_mongo_cleanup_intent(intent_id)
	if intent is None or owner is None:
		return {"status": "noop"}

	blog_ct = ContentType.objects.get_for_model(BlogPage, for_concrete_model=False)
	if intent.kind == "formal":
		referenced = BlogPage.objects.filter(mongo_content_id=intent.pointer).exists()
	else:
		referenced = False
		# Wagtail 8.0 的 Revision.content 是 TextField，不能使用 JSONField lookup；
		# 先按 ContentType 限定范围，再解析最小元数据避免误判正文字符串。
		for revision in Revision.objects.filter(content_type=blog_ct).only("content").iterator():
			try:
				payload = json.loads(revision.content) if isinstance(revision.content, str) else revision.content
			except (TypeError, ValueError, json.JSONDecodeError):
				continue
			if isinstance(payload, dict) and str(payload.get("mongo_draft_pointer")) == str(intent.pointer):
				referenced = True
				break
	if referenced:
		# 共享指针仍有引用时延迟重试，不能永久标记为已跳过，否则最后一个引用删除后会泄漏。
		next_at = timezone.now() + timedelta(minutes=10)
		_finished = _finish_claimed_intent(intent, owner, status=MongoCleanupIntentStatus.RETRY, available_at=next_at)
		if _finished:
			cleanup_mongo_intent.apply_async(args=(str(intent.intent_id),), queue="maintenance", countdown=600)
		return {"status": "referenced", "retry_at": next_at.isoformat()}

	try:
		manager = MongoManager()
		if intent.kind == "formal":
			manager.delete_blog_content(intent.pointer, raise_on_error=True)
		else:
			manager.delete_single_revision(intent.pointer, raise_on_error=True)
	except Exception as exc:
		attempts = intent.attempts
		status = MongoCleanupIntentStatus.DEAD if attempts >= 5 else MongoCleanupIntentStatus.RETRY
		delay = min(3600, 2 ** attempts * 60)
		_finished = _finish_claimed_intent(
			intent,
			owner,
			status=status,
			last_error=type(exc).__name__,
			available_at=timezone.now() + timedelta(seconds=delay),
		)
		if _finished and status == MongoCleanupIntentStatus.RETRY:
			cleanup_mongo_intent.apply_async(args=(str(intent.intent_id),), queue="maintenance", countdown=delay)
		return {"status": status, "attempts": attempts}
	if not _finish_claimed_intent(intent, owner, status=MongoCleanupIntentStatus.SUCCEEDED):
		return {"status": "lease_lost"}
	return {"status": "succeeded"}


@shared_task(name="blog.tasks.dispatch_pending_mongo_cleanup_retries", ignore_result=True)
def dispatch_pending_mongo_cleanup_retries(limit: object | None = None) -> int:
	"""Beat 补偿投递到期意图，并回收已过期租约；仅使用 maintenance 队列。"""
	reclaim_expired_mongo_cleanup_intents(limit=limit)
	now = timezone.now()
	intent_ids = list(
		MongoCleanupIntent.objects.filter(
			status__in=(MongoCleanupIntentStatus.PENDING, MongoCleanupIntentStatus.RETRY),
			available_at__lte=now,
		)
		.order_by("available_at", "pk")
		.values_list("intent_id", flat=True)[: _mongo_cleanup_batch_size(limit)]
	)
	dispatched = 0
	for intent_id in intent_ids:
		try:
			cleanup_mongo_intent.apply_async(args=(str(intent_id),), queue="maintenance")
			dispatched += 1
		except Exception:
			logger.exception("blog_mongo_cleanup_dispatch_failed intent_id=%s", intent_id)
	return dispatched
