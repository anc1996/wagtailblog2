"""以稳定 manifest 受控消费单一测试搜索目标的待投递记录。"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from search.models import (
    ContentSearchDelivery,
    ContentSearchState,
    ContentSearchStatus,
    ContentSearchTarget,
)
from search.services.delivery import (
    classify_content_search_event,
    process_content_search_delivery,
)


_MAX_LIMIT = 20
_DRAINABLE_STATUSES = (ContentSearchStatus.PENDING, ContentSearchStatus.RETRY)


def _manifest_sha256(entries: list[dict[str, object]]) -> str:
    """计算不含正文的候选清单哈希，防止 dry-run 与确认执行之间范围漂移。"""

    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pending_delivery_manifest(
    target: ContentSearchTarget, limit: int
) -> list[dict[str, object]]:
    """按消费者实际版本规则构造当前可消费 Delivery 的无正文 manifest。

    参数：``target`` 是唯一已启用物理索引目标，``limit`` 已经通过上限校验。
    返回：按 ``(available_at,id)`` 稳定排序的候选；每项含事件身份与版本分类，
    不含 Mongo 正文、正文 hash 或连接信息。
    副作用：仅执行 MySQL 读取；不领取租约，也不改变投递状态。
    """

    deliveries = list(
        ContentSearchDelivery.objects.filter(
            target=target,
            status__in=_DRAINABLE_STATUSES,
            available_at__lte=timezone.now(),
        )
        .select_related("event")
        .order_by("available_at", "pk")[:limit]
    )
    states = ContentSearchState.objects.in_bulk(
        [delivery.event.page_id for delivery in deliveries], field_name="page_id"
    )
    return [
        {
            "delivery_id": delivery.pk,
            "delivery_status": delivery.status,
            "delivery_attempts": delivery.attempts,
            "delivery_available_at": delivery.available_at.isoformat(),
            "event_id": str(delivery.event.event_id),
            "page_id": delivery.event.page_id,
            "content_version": delivery.event.content_version,
            "operation": delivery.event.operation,
            "classification": classify_content_search_event(
                delivery.event, states.get(delivery.event.page_id)
            ),
            "body_version_id": delivery.event.body_version_id,
            "publication_generation": delivery.event.publication_generation,
            "event_content_hash": delivery.event.content_hash,
            "state_content_hash": (
                states[delivery.event.page_id].content_hash
                if delivery.event.page_id in states
                else None
            ),
            "state_searchable": (
                states[delivery.event.page_id].searchable
                if delivery.event.page_id in states
                else None
            ),
            "target_index_name": target.index_name,
            "target_updated_at": target.updated_at.isoformat(),
        }
        for delivery in deliveries
    ]


class Command(BaseCommand):
    """默认只读预演测试目标的有限 Delivery 批次，确认后才逐条交给正式消费者。"""

    help = "默认 dry-run 分类指定测试搜索目标的待投递记录；--confirm 需匹配 manifest 哈希"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--target", required=True, help="精确的 ContentSearchTarget.target_id")
        parser.add_argument(
            "--limit",
            type=int,
            default=_MAX_LIMIT,
            help=f"单批最多 {_MAX_LIMIT} 条，默认 {_MAX_LIMIT}",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="确认由既有消费者处理本次 manifest 中的 Delivery",
        )
        parser.add_argument(
            "--expected-manifest-sha256",
            help="确认执行时必须提供刚刚 dry-run 输出的 manifest_sha256",
        )

    def handle(self, *args: object, **options: object) -> None:
        target_id = options["target"]
        limit = options["limit"]
        confirm = options["confirm"]
        expected_hash = options["expected_manifest_sha256"]
        if not isinstance(target_id, str) or not isinstance(limit, int) or not isinstance(confirm, bool):
            raise CommandError("invalid_arguments")
        if limit <= 0 or limit > _MAX_LIMIT:
            raise CommandError(f"limit_must_be_between_1_and_{_MAX_LIMIT}")

        target = ContentSearchTarget.objects.filter(target_id=target_id).first()
        if target is None:
            raise CommandError("content_search_target_not_found")
        if not target.enabled:
            raise CommandError("content_search_target_not_enabled")

        manifest = _pending_delivery_manifest(target, limit)
        manifest_hash = _manifest_sha256(manifest)
        classifications = Counter(entry["classification"] for entry in manifest)
        report: dict[str, object] = {
            "environment": os.environ.get("WAGTAILBLOG_ENV", "unset"),
            "target_id": target.target_id,
            "index_name": target.index_name,
            "dry_run": not confirm,
            "limit": limit,
            "candidate_count": len(manifest),
            "classification_counts": dict(sorted(classifications.items())),
            "manifest_sha256": manifest_hash,
            "manifest": manifest,
        }
        if not confirm:
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return

        if os.environ.get("WAGTAILBLOG_ENV") != "test":
            raise CommandError("test_environment_required")
        if not isinstance(expected_hash, str) or expected_hash != manifest_hash:
            raise CommandError("manifest_sha256_mismatch")
        if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
            raise CommandError("content_search_consumer_disabled")
        if set(classifications) - {"ready", "superseded"}:
            raise CommandError("manifest_contains_undrainable_classification")

        # 每条记录仍由既有租约消费者领取，避免运维命令越过幂等和版本围栏。
        result_counts: Counter[str] = Counter()
        for entry in manifest:
            delivery_id = entry["delivery_id"]
            if not isinstance(delivery_id, int):
                raise CommandError("invalid_manifest_delivery_id")
            result = process_content_search_delivery(delivery_id)
            result_counts[result] += 1
            if result not in {
                ContentSearchStatus.SUCCEEDED,
                ContentSearchStatus.SUPERSEDED,
            }:
                # 前面已完成的记录保持幂等结果；首个异常状态必须阻止后续 Delivery 和 rebuild。
                report["dry_run"] = False
                report["result_counts"] = dict(sorted(result_counts.items()))
                report["stopped_delivery_id"] = delivery_id
                report["stopped_result"] = result
                self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
                raise CommandError("delivery_processing_stopped")
        report["dry_run"] = False
        report["result_counts"] = dict(sorted(result_counts.items()))
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
