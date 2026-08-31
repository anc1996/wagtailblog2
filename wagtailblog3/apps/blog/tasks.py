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
from bson import ObjectId

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
from blog.models import (
    MongoCleanupIntent,
    MongoCleanupIntentStatus,
    PageDeletionIntent,
    PageDeletionIntentStatus,
    BlogPublicationState,
)
from wagtail.models import Revision
from wagtailblog3.mongo import MongoManager


logger = logging.getLogger(__name__)


def _page_deletion_queue() -> str:
	"""返回当前环境配置的维护队列，测试与生产不得混用固定队列名。"""
	queue = getattr(settings, "CELERY_MAINTENANCE_QUEUE", "maintenance")
	return queue.strip() if isinstance(queue, str) and queue.strip() else "maintenance"


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


_PAGE_DELETION_TERMINAL = {
	PageDeletionIntentStatus.SUCCEEDED,
	PageDeletionIntentStatus.DEAD,
	PageDeletionIntentStatus.BLOCKED_REFERENCE,
}


def _page_deletion_owner() -> str:
	"""生成页面删除 Worker 的租约 owner，避免崩溃前后的任务互相覆盖。"""
	return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def _page_deletion_lease_seconds() -> int:
	"""读取页面删除租约，配置异常时退回两分钟以限制重叠 Mongo 操作。"""
	try:
		value = int(getattr(settings, "PAGE_DELETION_LEASE_SECONDS", 120))
	except (TypeError, ValueError, OverflowError):
		value = 120
	return max(1, min(value, 3600))


def _claim_page_deletion(intent_id: object) -> tuple[PageDeletionIntent | None, str | None]:
	"""在事务内认领删除意图；Mongo I/O 永远发生在租约提交之后。"""
	now = timezone.now()
	owner = _page_deletion_owner()
	with transaction.atomic():
		intent = PageDeletionIntent.objects.select_for_update(skip_locked=True).filter(intent_id=intent_id).first()
		if intent is None or intent.status in _PAGE_DELETION_TERMINAL or intent.available_at > now:
			return None, None
		if intent.lease_expires_at and intent.lease_expires_at > now:
			return None, None
		intent.attempts += 1
		intent.status = PageDeletionIntentStatus.PROCESSING
		intent.lease_owner = owner
		intent.lease_expires_at = now + timedelta(seconds=_page_deletion_lease_seconds())
		intent.save(update_fields=("attempts", "status", "lease_owner", "lease_expires_at", "updated_at"))
	return intent, owner


def _save_page_deletion_progress(
	intent: PageDeletionIntent,
	owner: str,
	**updates: Any,
) -> bool:
	"""只有当前租约 owner 能推进步骤，防止过期任务覆盖较新的重试结果。"""
	updates.setdefault("updated_at", timezone.now())
	return bool(PageDeletionIntent.objects.filter(pk=intent.pk, lease_owner=owner).update(**updates))


def _release_page_deletion(
	intent: PageDeletionIntent,
	owner: str,
	*,
	status: str,
	error_code: str = "",
	available_at: Any = None,
) -> bool:
	"""释放租约并保留失败分类；已经删除的集合不回滚。"""
	return _save_page_deletion_progress(
		intent,
		owner,
		status=status,
		last_error_code=error_code[:64],
		available_at=available_at or timezone.now(),
		lease_owner="",
		lease_expires_at=None,
	)


def _manifest_strings(intent: PageDeletionIntent, key: str) -> list[str]:
	"""读取不可变清单中的非空字符串，拒绝临时查询扩大删除范围。"""
	value = intent.manifest.get(key) if isinstance(intent.manifest, dict) else None
	return sorted({str(item) for item in value if item}) if isinstance(value, list) else []


def _has_external_references(intent: PageDeletionIntent) -> bool:
	"""检查其他页面或 Revision 是否共享清单指针；当前删除页面自身不构成阻断。"""
	versions = _manifest_strings(intent, "body_version_ids")
	legacy_pointer = intent.manifest.get("legacy_pointer") if isinstance(intent.manifest, dict) else None
	if versions and BlogPublicationState.objects.exclude(page_id=intent.page_id).filter(
		Q(published_body_version_id__in=versions)
		| Q(draft_body_version_id__in=versions)
		| Q(approved_body_version_id__in=versions)
	).exists():
		return True
	if legacy_pointer and BlogPage.objects.exclude(pk=intent.page_id).filter(mongo_content_id=str(legacy_pointer)).exists():
		return True
	blog_ct = ContentType.objects.get_for_model(BlogPage, for_concrete_model=False)
	pointers = set(_manifest_strings(intent, "revision_pointers"))
	if not pointers:
		return False
	for revision in Revision.objects.filter(content_type=blog_ct).exclude(object_id=str(intent.page_id)).only("content").iterator():
		try:
			payload = json.loads(revision.content) if isinstance(revision.content, str) else revision.content
		except (TypeError, ValueError, json.JSONDecodeError):
			continue
		if isinstance(payload, dict) and str(payload.get("mongo_draft_pointer") or "") in pointers:
			return True
	return False


def _expand_page_manifest(intent: PageDeletionIntent, manager: MongoManager) -> None:
	"""删除前补齐页面在 Mongo 中未被 State/Revision 指针覆盖的历史版本。"""
	manifest = dict(intent.manifest or {})
	versions = set(_manifest_strings(intent, "body_version_ids"))
	for document in manager.content_body_versions.find(
		{"aggregate_type": "blog_page", "aggregate_id": str(intent.page_id)},
		{"body_version_id": 1},
	):
		value = document.get("body_version_id")
		if value:
			versions.add(str(value))
	revision_pointers = set(_manifest_strings(intent, "revision_pointers"))
	for document in manager.blog_revisions.find({"page_id": intent.page_id}, {"_id": 1}):
		value = document.get("_id")
		if value is not None:
			revision_pointers.add(str(value))
	manifest["body_version_ids"] = sorted(versions)
	manifest["revision_pointers"] = sorted(revision_pointers)
	if manifest != (intent.manifest or {}):
		PageDeletionIntent.objects.filter(pk=intent.pk, lease_owner=intent.lease_owner).update(
			manifest=manifest, updated_at=timezone.now()
		)
		intent.manifest = manifest


def _tombstone_succeeded(intent: PageDeletionIntent) -> bool:
	"""按当前删除意图绑定的 event/generation 确认墓碑已完成投递。"""
	if not getattr(settings, "CONTENT_SEARCH_PRODUCER_ENABLED", False):
		return True
	from search.models import ContentSearchOperation, ContentSearchOutbox, ContentSearchStatus

	event_id = intent.manifest.get("tombstone_event_id") if isinstance(intent.manifest, dict) else None
	if not event_id:
		return False
	return ContentSearchOutbox.objects.filter(
		event_id=event_id,
		page_id=intent.page_id,
		operation=ContentSearchOperation.TOMBSTONE,
		status=ContentSearchStatus.SUCCEEDED,
		publication_generation=intent.deletion_generation,
	).exists()


def _mongo_delete_page_versions(intent: PageDeletionIntent, manager: MongoManager) -> int:
	"""按固化版本 ID 与聚合身份删除正式正文，缺失文档视为幂等完成。"""
	versions = _manifest_strings(intent, "body_version_ids")
	if not versions:
		return 0
	result = manager.content_body_versions.delete_many(
		{
			"aggregate_type": "blog_page",
			"aggregate_id": str(intent.page_id),
			"body_version_id": {"$in": versions},
		}
	)
	return int(result.deleted_count)


def _mongo_delete_revision_bodies(intent: PageDeletionIntent, manager: MongoManager) -> int:
	"""逐条删除已快照的 Revision 指针，禁止以 page_id 批量删除替代指针校验。"""
	deleted = 0
	for pointer in _manifest_strings(intent, "revision_pointers"):
		mongo_id: object = ObjectId(pointer) if ObjectId.is_valid(pointer) else pointer
		result = manager.blog_revisions.delete_one({"_id": mongo_id, "page_id": intent.page_id})
		deleted += int(result.deleted_count)
	return deleted


def _mongo_delete_legacy_content(intent: PageDeletionIntent, manager: MongoManager) -> int:
	"""仅按页面快照的兼容 ObjectId 删除旧正文，指针无效即视为失败而非按 page_id 扩删。"""
	pointer = intent.manifest.get("legacy_pointer") if isinstance(intent.manifest, dict) else None
	if not pointer:
		return 0
	if not ObjectId.is_valid(str(pointer)):
		raise ValueError("legacy_pointer_invalid")
	result = manager.blog_content.delete_one({"_id": ObjectId(str(pointer)), "page_id": intent.page_id})
	return int(result.deleted_count)


@shared_task(name="blog.tasks.process_page_deletion")
def process_page_deletion(intent_id: object) -> dict[str, Any]:
	"""执行页面删除状态机：先墓碑、后引用检查和 Mongo 清理，最后物理删除 MySQL 页面。

	每个集合完成后立即写入步骤与计数，因此 Worker 崩溃只会重试未完成的精确清单。
	任一外部引用会保留所有 Mongo 数据并进入 ``blocked_reference``，不会部分静默删除。
	"""
	intent, owner = _claim_page_deletion(intent_id)
	if intent is None or owner is None:
		return {"status": "noop"}
	if not _tombstone_succeeded(intent):
		_release_page_deletion(intent, owner, status=PageDeletionIntentStatus.SEARCH_PENDING)
		return {"status": PageDeletionIntentStatus.SEARCH_PENDING}
	try:
		manager = MongoManager()
		_expand_page_manifest(intent, manager)
		if _has_external_references(intent):
			_release_page_deletion(intent, owner, status=PageDeletionIntentStatus.BLOCKED_REFERENCE, error_code="external_reference")
			return {"status": PageDeletionIntentStatus.BLOCKED_REFERENCE}
		counts = dict(intent.deleted_counts or {})
		if intent.step in {"snapshot", "tombstone", "check_references", "delete_content_versions"}:
			counts["content_body_versions"] = _mongo_delete_page_versions(intent, manager)
			if not _save_page_deletion_progress(intent, owner, status=PageDeletionIntentStatus.MONGO_PENDING, step="delete_revision_bodies", deleted_counts=counts):
				return {"status": "lease_lost"}
			intent.step = "delete_revision_bodies"
		if intent.step == "delete_revision_bodies":
			counts["blog_page_revision_bodies"] = _mongo_delete_revision_bodies(intent, manager)
			if not _save_page_deletion_progress(intent, owner, step="delete_legacy_content", deleted_counts=counts):
				return {"status": "lease_lost"}
			intent.step = "delete_legacy_content"
		if intent.step == "delete_legacy_content":
			counts["blog_content"] = _mongo_delete_legacy_content(intent, manager)
			if not _save_page_deletion_progress(intent, owner, status=PageDeletionIntentStatus.MYSQL_FINALIZE_PENDING, step="finalize_mysql", deleted_counts=counts):
				return {"status": "lease_lost"}
			intent.step = "finalize_mysql"
		with transaction.atomic():
			locked = PageDeletionIntent.objects.select_for_update().get(pk=intent.pk)
			if locked.lease_owner != owner:
				return {"status": "lease_lost"}
			# State 没有外键指向 Page，物理删除页面不会自动清理它；先清空
			# 正文指针，避免对账或新页面复用主键时误读已删除正文。
			BlogPublicationState.objects.filter(page_id=locked.page_id).update(
				draft_body_version_id=None,
				draft_body_sha256=None,
				draft_body_schema_version=None,
				published_body_version_id=None,
				published_body_sha256=None,
				published_body_schema_version=None,
				approved_revision_id=None,
				approved_revision_created_at=None,
				approved_body_version_id=None,
				approved_body_sha256=None,
				approved_body_schema_version=None,
				updated_at=timezone.now(),
			)
			page = BlogPage.objects.filter(pk=locked.page_id).first()
			if page is not None:
				# 仅此最终步骤携带令牌；pre_delete 仍会拦截其他绕过受控入口的物理删除。
				page._allow_page_delete_finalize = True
				page._search_delete_recorded = True
				# Wagtail Collector 会直接向所有后代发出 pre_delete，必须一次性传播最终删除令牌。
				from blog.signals import allow_page_delete_for_ids

				descendant_ids = set(page.get_descendants().values_list("pk", flat=True))
				allowed_ids = {int(page.pk), *(int(value) for value in descendant_ids)}
				for descendant in BlogPage.objects.filter(pk__in=descendant_ids):
					descendant._search_delete_recorded = True
				with allow_page_delete_for_ids(allowed_ids):
					page.delete()
			locked.status = PageDeletionIntentStatus.SUCCEEDED
			locked.step = "done"
			locked.completed_at = timezone.now()
			locked.lease_owner = ""
			locked.lease_expires_at = None
			locked.last_error_code = ""
			locked.save(update_fields=("status", "step", "completed_at", "lease_owner", "lease_expires_at", "last_error_code", "updated_at"))
		return {"status": PageDeletionIntentStatus.SUCCEEDED, "deleted_counts": counts}
	except Exception as exc:
		status = PageDeletionIntentStatus.DEAD if intent.attempts >= 5 else PageDeletionIntentStatus.PARTIAL_FAILED
		delay = min(3600, 60 * (2 ** min(intent.attempts, 6)))
		_release_page_deletion(
			intent,
			owner,
			status=status,
			error_code=type(exc).__name__,
			available_at=timezone.now() + timedelta(seconds=delay),
		)
		if status != PageDeletionIntentStatus.DEAD:
			process_page_deletion.apply_async(args=(str(intent.intent_id),), queue=_page_deletion_queue(), countdown=delay)
		return {"status": status, "error_code": type(exc).__name__}


def reclaim_expired_page_deletions(limit: object | None = None) -> int:
	"""回收崩溃 Worker 的页面删除租约，使意图从最后步骤重新可执行。"""
	try:
		batch = max(1, min(int(limit if limit is not None else getattr(settings, "PAGE_DELETION_BATCH_SIZE", 100)), 500))
	except (TypeError, ValueError, OverflowError):
		batch = 100
	now = timezone.now()
	ids = list(PageDeletionIntent.objects.filter(lease_expires_at__lte=now).exclude(lease_owner="").values_list("pk", flat=True)[:batch])
	reclaimed = 0
	for pk in ids:
		with transaction.atomic():
			intent = PageDeletionIntent.objects.select_for_update().filter(pk=pk).first()
			if intent is None or not intent.lease_owner or not intent.lease_expires_at or intent.lease_expires_at > now:
				continue
			intent.lease_owner = ""
			intent.lease_expires_at = None
			intent.lease_reclaims += 1
			intent.status = PageDeletionIntentStatus.PARTIAL_FAILED
			intent.available_at = now
			intent.save(update_fields=("lease_owner", "lease_expires_at", "lease_reclaims", "status", "available_at", "updated_at"))
			reclaimed += 1
	return reclaimed


@shared_task(name="blog.tasks.dispatch_page_deletion_retries", ignore_result=True)
def dispatch_page_deletion_retries(limit: object | None = None) -> int:
	"""Beat 只投递到期的页面删除意图，并固定使用 maintenance 队列。"""
	reclaim_expired_page_deletions(limit)
	now = timezone.now()
	try:
		batch = max(1, min(int(limit if limit is not None else getattr(settings, "PAGE_DELETION_BATCH_SIZE", 100)), 500))
	except (TypeError, ValueError, OverflowError):
		batch = 100
	ids = PageDeletionIntent.objects.filter(
		status__in=(
			PageDeletionIntentStatus.DELETING,
			PageDeletionIntentStatus.PROCESSING,
			PageDeletionIntentStatus.SEARCH_PENDING,
			PageDeletionIntentStatus.MONGO_PENDING,
			PageDeletionIntentStatus.PARTIAL_FAILED,
			PageDeletionIntentStatus.MYSQL_FINALIZE_PENDING,
		),
		available_at__lte=now,
		lease_owner="",
	).values_list("intent_id", flat=True)[:batch]
	for intent_id in ids:
		process_page_deletion.apply_async(args=(str(intent_id),), queue=_page_deletion_queue())
	return len(ids)
