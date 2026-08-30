"""内容搜索 Outbox 的 Delivery 调度、租约和幂等消费。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
import os
import random
import socket
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from blog.models import BlogPage
from search.models import (
    ContentSearchDelivery,
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchState,
    ContentSearchStatus,
    ContentSearchTarget,
    ContentSearchTargetRole,
)
from search.services.document import build_formal_content_document
from search.services.elasticsearch import (
    ContentSearchElasticsearchError,
    write_content_search_document,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryLease:
    """一次成功领取的 Delivery 租约；owner 防止迟到 worker 覆盖状态。"""
    delivery_id: int
    event_id: int
    target_id: int
    owner: str


def _batch_size(limit: object | None = None) -> int:
    configured_limit = getattr(settings, "CONTENT_SEARCH_DELIVERY_BATCH_SIZE", 100)
    try:
        value = int(configured_limit if limit is None else limit)
    except (TypeError, ValueError, OverflowError):
        value = 100
    return max(1, min(value, 500))


def _lease_seconds() -> int:
    try:
        value = int(getattr(settings, "CONTENT_SEARCH_LEASE_SECONDS", 120))
    except (TypeError, ValueError, OverflowError):
        value = 120
    return max(1, value)


def _max_attempts() -> int:
    try:
        value = int(getattr(settings, "CONTENT_SEARCH_MAX_ATTEMPTS", 10))
    except (TypeError, ValueError, OverflowError):
        value = 10
    return max(1, value)


def _retry_delay(attempts: int) -> float:
    """计算带随机抖动的指数退避，避免多个 worker 同时重试。"""
    try:
        maximum = int(getattr(settings, "CONTENT_SEARCH_RETRY_MAX_SECONDS", 3600))
    except (TypeError, ValueError, OverflowError):
        maximum = 3600
    try:
        base = int(getattr(settings, "CONTENT_SEARCH_RETRY_BASE_SECONDS", 5))
    except (TypeError, ValueError, OverflowError):
        base = 5
    base = max(1, base)
    maximum = max(base, maximum)
    exponential_delay = min(maximum, base * (2 ** max(0, attempts - 1)))
    return min(maximum, exponential_delay + random.uniform(0, max(1, exponential_delay / 4)))


def _worker_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def _terminal_statuses() -> set[str]:
    return {
        ContentSearchStatus.SUCCEEDED,
        ContentSearchStatus.SUPERSEDED,
        ContentSearchStatus.DEAD,
    }


def materialize_content_search_deliveries(limit: object | None = None) -> int:
    """为已启用目标补齐 Delivery；没有目标时保留 Outbox 等待建索引。"""
    """为当前已启用目标补齐 Delivery；没有目标时保留 Outbox 以待后续索引建立。"""

    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        return 0

    targets = list(
        ContentSearchTarget.objects.filter(enabled=True).only("pk", "created_at", "role")
    )
    if not targets:
        return 0

    now = timezone.now()
    created_count = 0
    for target in targets:
        eligible_events = Q(
            status__in=(
                ContentSearchStatus.PENDING,
                ContentSearchStatus.PROCESSING,
                ContentSearchStatus.RETRY,
            ),
            available_at__lte=now,
        )
        if target.role == ContentSearchTargetRole.BUILDING:
            # 构建目标注册后产生的事件即使已被旧目标完成，也必须补建新目标 Delivery。
            eligible_events |= Q(created_at__gte=target.created_at)
        event_ids = list(
            ContentSearchOutbox.objects.filter(eligible_events)
            .order_by("available_at", "pk")
            .values_list("pk", flat=True)[: _batch_size(limit)]
        )
        for event_id in event_ids:
            _delivery, created = ContentSearchDelivery.objects.get_or_create(
                event_id=event_id,
                target_id=target.pk,
                defaults={"available_at": now},
            )
            if created:
                created_count += 1
    return created_count


def reclaim_expired_content_search_deliveries(limit: object | None = None) -> int:
    """回收崩溃 worker 遗留的过期租约，使事件安全重试。"""
    """回收 Worker 崩溃遗留的租约，允许相同外部版本安全重投。"""

    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        return 0

    now = timezone.now()
    delivery_ids = list(
        ContentSearchDelivery.objects.filter(
            status=ContentSearchStatus.PROCESSING,
            lock_expires_at__lte=now,
        )
        .order_by("lock_expires_at", "pk")
        .values_list("pk", flat=True)[: _batch_size(limit)]
    )
    reclaimed_count = 0
    event_ids = set()
    for delivery_id in delivery_ids:
        with transaction.atomic():
            delivery = ContentSearchDelivery.objects.select_for_update().get(pk=delivery_id)
            if (
                delivery.status != ContentSearchStatus.PROCESSING
                or delivery.lock_expires_at is None
                or delivery.lock_expires_at > now
            ):
                continue
            delivery.status = ContentSearchStatus.RETRY
            delivery.lease_reclaims += 1
            delivery.available_at = now
            delivery.locked_by = ""
            delivery.lock_expires_at = None
            delivery.save(
                update_fields=(
                    "status",
                    "lease_reclaims",
                    "available_at",
                    "locked_by",
                    "lock_expires_at",
                    "updated_at",
                )
            )
            event_ids.add(delivery.event_id)
            reclaimed_count += 1
    for event_id in event_ids:
        refresh_content_search_outbox_status(event_id)
    return reclaimed_count


def due_content_search_delivery_ids(limit: object | None = None) -> list[int]:
    """返回到期 Delivery 主键；实际领取必须由 worker 使用行锁完成。"""
    """只返回已到期 Delivery 主键，实际领取必须由 Worker 使用行锁完成。"""

    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        return []

    materialize_content_search_deliveries(limit=limit)
    reclaim_expired_content_search_deliveries(limit=limit)
    now = timezone.now()
    return list(
        ContentSearchDelivery.objects.filter(
            status__in=(ContentSearchStatus.PENDING, ContentSearchStatus.RETRY),
            available_at__lte=now,
            target__enabled=True,
        )
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[: _batch_size(limit)]
    )


def _claim_content_search_delivery(delivery_id: int):
    """使用 skip_locked 原子领取 Delivery，并创建带过期时间的 owner 租约。"""
    now = timezone.now()
    owner = _worker_owner()
    with transaction.atomic():
        delivery = (
            ContentSearchDelivery.objects.select_for_update(skip_locked=True)
            .select_related("event", "target")
            .filter(pk=delivery_id)
            .first()
        )
        if delivery is None:
            return None, "locked"
        if delivery.status in _terminal_statuses():
            return None, delivery.status
        if not delivery.target.enabled:
            return None, _finish_content_search_delivery(
                delivery,
                owner=None,
                status=ContentSearchStatus.SUPERSEDED,
            )
        if delivery.status == ContentSearchStatus.PROCESSING:
            if delivery.lock_expires_at and delivery.lock_expires_at > now:
                return None, ContentSearchStatus.PROCESSING
        elif delivery.available_at > now:
            return None, delivery.status

        delivery.status = ContentSearchStatus.PROCESSING
        delivery.attempts += 1
        delivery.locked_by = owner
        delivery.lock_expires_at = now + timedelta(seconds=_lease_seconds())
        delivery.save(
            update_fields=(
                "status",
                "attempts",
                "locked_by",
                "lock_expires_at",
                "updated_at",
            )
        )
        return (
            DeliveryLease(
                delivery_id=delivery.pk,
                event_id=delivery.event_id,
                target_id=delivery.target_id,
                owner=owner,
            ),
            ContentSearchStatus.PROCESSING,
        )


def classify_content_search_event(
    event: ContentSearchOutbox, state: ContentSearchState | None
) -> str:
    """按消费者的版本围栏分类一个 Outbox 事件。

    参数：``event`` 是待投递事件，``state`` 是同一页面的当前搜索状态。
    返回：仅返回 ``ready``、``retry``、``superseded`` 或 ``dead``，供消费者与
    受控运维命令共享，避免 dry-run 预判与实际消费采用不同版本规则。
    副作用：无；不读取正文、不领取租约，也不修改 Outbox、Delivery 或 State。
    """
    if state is None:
        return "dead"
    if event.content_version < state.content_version:
        return "superseded"
    if event.content_version > state.content_version:
        return "retry"
    if event.operation != state.desired_operation:
        return "retry"
    if bool(event.searchable) != bool(state.searchable):
        return "retry"
    if event.content_hash and state.content_hash and event.content_hash != state.content_hash:
        return "retry"
    if (
        not event.body_version_id
        and not state.body_version_id
        and event.mongo_content_id
        and state.mongo_content_id
        and event.mongo_content_id != state.mongo_content_id
    ):
        return "retry"
    if event.body_version_id and event.body_version_id != state.body_version_id:
        return "superseded"
    if state.body_version_id and not event.body_version_id:
        # 新状态已有不可变正文身份时，旧格式事件不得再索引未知正文。
        return "superseded"
    if event.publication_generation is not None:
        if state.publication_generation is None:
            return "retry"
        if event.publication_generation < state.publication_generation:
            return "superseded"
        if event.publication_generation > state.publication_generation:
            return "retry"
    if state.publication_generation is not None and event.publication_generation is None:
        return "superseded"
    return "ready"


def _load_current_event_and_state(
    lease: DeliveryLease,
) -> tuple[ContentSearchOutbox, ContentSearchState | None, str]:
    """读取租约事件及当前 State，并返回与实际消费一致的版本分类。"""

    event = ContentSearchOutbox.objects.get(pk=lease.event_id)
    state = ContentSearchState.objects.filter(page_id=event.page_id).first()
    return event, state, classify_content_search_event(event, state)


def _confirm_formal_content(lease: DeliveryLease, formal_document: object) -> str:
    """ES 写入前再次锁定 State，避免 Mongo 新正文被旧事件索引。"""
    """在 ES 写入前再次锁定 State，避免 Mongo 新正文被旧版本事件索引。"""

    with transaction.atomic():
        event = ContentSearchOutbox.objects.select_for_update().get(pk=lease.event_id)
        state = ContentSearchState.objects.select_for_update().filter(page_id=event.page_id).first()
        if state is None:
            return "dead"
        if event.content_version < state.content_version:
            return "superseded"
        if event.content_version > state.content_version:
            return "retry"
        if (
            event.operation != ContentSearchOperation.UPSERT
            or state.desired_operation != ContentSearchOperation.UPSERT
            or not event.searchable
            or not state.searchable
        ):
            return "retry"
        document_body_version_id = getattr(formal_document, "body_version_id", None)
        if state.body_version_id and document_body_version_id != state.body_version_id:
            return "retry"
        if state.content_hash and state.content_hash != formal_document.content_hash:
            return "retry"
        if event.content_hash and event.content_hash != formal_document.content_hash:
            return "retry"
        if event.body_version_id and document_body_version_id != event.body_version_id:
            return "retry"
        if not document_body_version_id:
            # 只有旧 blog_content 路径使用 mongo_content_id；现代正文由版本 ID/hash/schema 定位。
            if state.mongo_content_id != formal_document.mongo_content_id:
                return "retry"
            if event.mongo_content_id and event.mongo_content_id != formal_document.mongo_content_id:
                return "retry"
        if (
            event.publication_generation is not None
            and getattr(formal_document, "publication_generation", None)
            != event.publication_generation
        ):
            return "retry"

        update_state = not state.content_hash
        update_event = not event.content_hash
        if update_state:
            state.content_hash = formal_document.content_hash
            state.save(update_fields=("content_hash", "updated_at"))
        if update_event:
            event.content_hash = formal_document.content_hash
            update_fields = ["content_hash", "updated_at"]
            if not document_body_version_id:
                event.mongo_content_id = formal_document.mongo_content_id
                update_fields.append("mongo_content_id")
            event.save(update_fields=tuple(update_fields))
    return "ready"


def _tombstone_document(event: object) -> dict[str, object]:
    """生成仅保留版本和公开状态的 tombstone，防止 ES 残留正文。"""
    """墓碑只保留版本和公开状态，防止取消发布后 ES 留存任何可展示正文。"""

    return {
        "page_id": event.page_id,
        "content_version": event.content_version,
        "searchable": False,
        "operation": ContentSearchOperation.TOMBSTONE,
        **(
            {"body_version_id": event.body_version_id}
            if event.body_version_id
            else {}
        ),
        **(
            {"publication_generation": event.publication_generation}
            if event.publication_generation is not None
            else {}
        ),
    }


def _finish_content_search_delivery(delivery, owner, status, error_code=""):
    now = timezone.now()
    if owner is not None and delivery.locked_by != owner:
        return delivery.status

    delivery.status = status
    delivery.locked_by = ""
    delivery.lock_expires_at = None
    delivery.last_error_code = error_code
    delivery.last_error_message = ""
    if status == ContentSearchStatus.RETRY:
        delivery.available_at = now + timedelta(seconds=_retry_delay(delivery.attempts))
        delivery.completed_at = None
    else:
        delivery.available_at = now
        delivery.completed_at = now
    delivery.save(
        update_fields=(
            "status",
            "available_at",
            "locked_by",
            "lock_expires_at",
            "last_error_code",
            "last_error_message",
            "completed_at",
            "updated_at",
        )
    )
    return delivery.status


def _complete_delivery(lease, status, error_code=""):
    with transaction.atomic():
        delivery = ContentSearchDelivery.objects.select_for_update().get(pk=lease.delivery_id)
        result = _finish_content_search_delivery(
            delivery,
            owner=lease.owner,
            status=status,
            error_code=error_code,
        )
    refresh_content_search_outbox_status(lease.event_id)
    return result


def _retry_or_dead_delivery(lease, error_code):
    with transaction.atomic():
        delivery = ContentSearchDelivery.objects.select_for_update().get(pk=lease.delivery_id)
        if delivery.locked_by != lease.owner:
            result = delivery.status
        else:
            status = (
                ContentSearchStatus.DEAD
                if delivery.attempts >= _max_attempts()
                else ContentSearchStatus.RETRY
            )
            result = _finish_content_search_delivery(
                delivery,
                owner=lease.owner,
                status=status,
                error_code=error_code,
            )
    refresh_content_search_outbox_status(lease.event_id)
    return result


def refresh_content_search_outbox_status(event_id: int):
    """由各目标 Delivery 聚合 Outbox 状态；required 目标优先决定结果。"""
    """由各目标 Delivery 聚合 Outbox 状态，optional 目标失败不会阻断 required 目标收敛。"""

    with transaction.atomic():
        event = ContentSearchOutbox.objects.select_for_update().get(pk=event_id)
        deliveries = list(event.deliveries.select_related("target").all())
        active_deliveries = [delivery for delivery in deliveries if delivery.target.enabled]
        if not active_deliveries:
            return event.status

        required_deliveries = [
            delivery for delivery in active_deliveries if delivery.target.required
        ]
        tracked_deliveries = required_deliveries or active_deliveries
        statuses = {delivery.status for delivery in tracked_deliveries}
        event.attempts = sum(delivery.attempts for delivery in tracked_deliveries)
        event.completed_at = None
        if ContentSearchStatus.DEAD in statuses:
            event.status = ContentSearchStatus.DEAD
            event.available_at = timezone.now()
        elif statuses & {ContentSearchStatus.PROCESSING}:
            event.status = ContentSearchStatus.PROCESSING
            event.available_at = timezone.now()
        elif statuses & {ContentSearchStatus.RETRY}:
            event.status = ContentSearchStatus.RETRY
            event.available_at = min(
                delivery.available_at
                for delivery in tracked_deliveries
                if delivery.status == ContentSearchStatus.RETRY
            )
        elif statuses & {ContentSearchStatus.PENDING}:
            event.status = ContentSearchStatus.PENDING
            event.available_at = min(
                delivery.available_at
                for delivery in tracked_deliveries
                if delivery.status == ContentSearchStatus.PENDING
            )
        elif statuses == {ContentSearchStatus.SUPERSEDED}:
            event.status = ContentSearchStatus.SUPERSEDED
            event.completed_at = timezone.now()
        else:
            event.status = ContentSearchStatus.SUCCEEDED
            event.completed_at = timezone.now()
        event.save(
            update_fields=(
                "status",
                "attempts",
                "available_at",
                "completed_at",
                "updated_at",
            )
        )
    return event.status


def process_content_search_delivery(delivery_id: int) -> str:
    """消费一个 Delivery；ES 写入在事务外执行，最终状态由租约 owner 保护。"""
    """消费一个 Delivery；外部 ES 写入始终在事务外，完成状态通过租约 owner 防止迟到覆盖。"""

    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        return "disabled"

    lease, status = _claim_content_search_delivery(delivery_id)
    if lease is None:
        return status

    try:
        event, state, state_status = _load_current_event_and_state(lease)
        if state_status == "superseded":
            return _complete_delivery(lease, ContentSearchStatus.SUPERSEDED)
        if state_status == "dead":
            return _complete_delivery(
                lease,
                ContentSearchStatus.DEAD,
                error_code="content_search_state_missing",
            )
        if state_status != "ready":
            return _retry_or_dead_delivery(lease, "content_search_state_mismatch")

        target = ContentSearchTarget.objects.get(pk=lease.target_id)
        if not target.enabled:
            return _complete_delivery(lease, ContentSearchStatus.SUPERSEDED)

        if event.operation == ContentSearchOperation.TOMBSTONE or not state.searchable:
            tombstone = _tombstone_document(event)
            if event.publication_generation is None:
                write_result = write_content_search_document(
                    target, tombstone, event.content_version
                )
            else:
                write_result = write_content_search_document(
                    target,
                    tombstone,
                    event.content_version,
                    publication_generation=event.publication_generation,
                )
        else:
            page = BlogPage.objects.live().public().filter(pk=event.page_id).first()
            if page is None:
                # 权限范围消费者尚未收敛前不能伪成功，否则旧 ES 正文可能继续公开。
                return _retry_or_dead_delivery(lease, "content_search_page_not_public")
            formal_document = build_formal_content_document(
                page,
                event.content_version,
                body_version_id=event.body_version_id,
                publication_generation=event.publication_generation,
            )
            if formal_document is None:
                return _retry_or_dead_delivery(lease, "mongo_formal_content_unavailable")
            confirmation = _confirm_formal_content(lease, formal_document)
            if confirmation == "superseded":
                return _complete_delivery(lease, ContentSearchStatus.SUPERSEDED)
            if confirmation == "dead":
                return _complete_delivery(
                    lease,
                    ContentSearchStatus.DEAD,
                    error_code="content_search_state_missing",
                )
            if confirmation != "ready":
                return _retry_or_dead_delivery(lease, "content_search_formal_content_changed")
            if event.publication_generation is None:
                write_result = write_content_search_document(
                    target, formal_document.document, event.content_version
                )
            else:
                write_result = write_content_search_document(
                    target,
                    formal_document.document,
                    event.content_version,
                    publication_generation=event.publication_generation,
                )
    except ContentSearchElasticsearchError as error:
        if error.retryable:
            return _retry_or_dead_delivery(lease, error.code)
        return _complete_delivery(lease, ContentSearchStatus.DEAD, error_code=error.code)
    except Exception:
        # 不记录异常文本，避免 Mongo 或 ES 客户端把正文、请求体或凭据带入任务审计。
        logger.error(
            "content_search_delivery_unexpected_failure delivery_id=%s event_id=%s",
            lease.delivery_id,
            lease.event_id,
        )
        return _retry_or_dead_delivery(lease, "content_search_unexpected_error")

    if write_result.status == "superseded":
        return _complete_delivery(lease, ContentSearchStatus.SUPERSEDED)
    return _complete_delivery(lease, ContentSearchStatus.SUCCEEDED)
