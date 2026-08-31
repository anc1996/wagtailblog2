"""BlogPage 不可恢复删除的 MySQL 编排与精确清单生成。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from django.conf import settings
from django.db import transaction

from blog.models import BlogPage, BlogPublicationState, PageDeletionIntent, PageDeletionIntentStatus


_ACTIVE_STATUSES = {
	PageDeletionIntentStatus.DELETING,
	PageDeletionIntentStatus.PROCESSING,
    PageDeletionIntentStatus.SEARCH_PENDING,
    PageDeletionIntentStatus.MONGO_PENDING,
    PageDeletionIntentStatus.PARTIAL_FAILED,
    PageDeletionIntentStatus.BLOCKED_REFERENCE,
    PageDeletionIntentStatus.MYSQL_FINALIZE_PENDING,
}


def _maintenance_queue() -> str:
	"""使用当前环境维护队列，避免测试任务误投递到生产名称。"""
	queue = getattr(settings, "CELERY_MAINTENANCE_QUEUE", "maintenance")
	return queue.strip() if isinstance(queue, str) and queue.strip() else "maintenance"


def _revision_payload(revision: Any) -> dict[str, Any]:
    """将 Wagtail Revision.content 解析为最小指针字典，不输出正文。"""
    payload = revision.content
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return payload if isinstance(payload, dict) else {}


def _build_manifest(page: BlogPage, state: BlogPublicationState | None) -> dict[str, Any]:
    """固化页面全部正文版本、历史快照和兼容指针，供后续任务断点执行。"""
    versions: set[str] = set()
    if state is not None:
        for field in ("draft_body_version_id", "published_body_version_id", "approved_body_version_id"):
            value = getattr(state, field, None)
            if value:
                versions.add(str(value))
    revisions: set[str] = set()
    for revision in page.revisions.only("content").iterator():
        payload = _revision_payload(revision)
        for field in ("mongo_body_version_id",):
            value = payload.get(field)
            if value:
                versions.add(str(value))
        pointer = payload.get("mongo_draft_pointer")
        if pointer:
            revisions.add(str(pointer))
    return {
        "body_version_ids": sorted(versions),
        "revision_pointers": sorted(revisions),
        "legacy_pointer": str(page.mongo_content_id) if page.mongo_content_id else None,
    }


def request_page_deletion(
    page: BlogPage,
    *,
    actor_id: str | None = None,
    request_id: str | None = None,
) -> PageDeletionIntent:
    """创建或复用页面删除意图，并在事务提交后唤醒 maintenance Worker。

    参数：``page`` 必须是已持久化的 BlogPage；可选操作者和请求 ID 只记录审计元数据。
    返回：当前页面唯一活动意图。同代重复请求不会创建第二条清单。
    异常：未持久化页面抛出 ``ValueError``；数据库错误回滚意图和搜索墓碑。
    """
    if not page.pk:
        raise ValueError("page_required")
    from blog.tasks import process_page_deletion
    from search.services.outbox import ContentSearchOutboxService

    with transaction.atomic():
        locked_page = BlogPage.objects.select_for_update().get(pk=page.pk)
        active = (
            PageDeletionIntent.objects.select_for_update()
            .filter(page_id=locked_page.pk, status__in=_ACTIVE_STATUSES)
            .order_by("-deletion_generation")
            .first()
        )
        if active is not None:
            intent = active
        else:
            state = BlogPublicationState.objects.filter(page_id=locked_page.pk).first()
            generation = (state.publication_generation if state else 0) + 1
            intent = PageDeletionIntent.objects.create(
                dedupe_key=f"page:{locked_page.pk}:delete:{generation}",
                page_id=locked_page.pk,
                deletion_generation=generation,
                request_id=(request_id or str(uuid.uuid4()))[:128],
                actor_id=(actor_id or "")[:128],
                manifest=_build_manifest(locked_page, state),
                status=PageDeletionIntentStatus.DELETING,
                step="snapshot",
            )
            # Tombstone 与删除清单位于同一 MySQL 事务，避免页面消失后搜索仍可见。
            tombstone_event_id = None
            if getattr(settings, "CONTENT_SEARCH_PRODUCER_ENABLED", False):
                from blog.services.publication import BlogPublicationService

                BlogPublicationService.advance_unpublish_generation(locked_page.pk)
                event = ContentSearchOutboxService.record_delete(locked_page)
                tombstone_event_id = str(getattr(event, "event_id", "")) or None
            if tombstone_event_id:
                manifest = dict(intent.manifest or {})
                manifest["tombstone_event_id"] = tombstone_event_id
                intent.manifest = manifest
                intent.save(update_fields=("manifest", "updated_at"))
        transaction.on_commit(
            lambda: process_page_deletion.apply_async(
                args=(str(intent.intent_id),), queue=_maintenance_queue()
            )
        )
    return intent
