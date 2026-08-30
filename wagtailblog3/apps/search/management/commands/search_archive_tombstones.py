"""只读生成已完成 tombstone 审计归档候选及脱敏 manifest。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from search.models import (
    ContentSearchDelivery,
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchState,
    ContentSearchStatus,
    ContentSearchTarget,
    SearchIndexBuild,
    SearchIndexBuildStatus,
)


_TERMINAL_DELIVERY_STATUSES = {
    ContentSearchStatus.SUCCEEDED,
    ContentSearchStatus.SUPERSEDED,
}
_ACTIVE_BUILD_STATUSES = {
    SearchIndexBuildStatus.CREATED,
    SearchIndexBuildStatus.BACKFILLING,
    SearchIndexBuildStatus.CATCHING_UP,
}


def _sha256_manifest(entries: list[dict[str, Any]]) -> str:
    """对排序后的脱敏候选计算稳定哈希，不包含正文或凭据。"""

    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Command(BaseCommand):
    """扫描可归档 tombstone；本命令永远只读，不提供删除或归档写入操作。"""

    help = "只读输出超过保留期且投递已收敛的 tombstone 归档候选"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="显式确认只读预演（命令始终不会写入）")
        parser.add_argument(
            "--retention-days",
            type=int,
            default=180,
            help="完成时间距今至少保留的天数（默认 180）",
        )
        parser.add_argument("--limit", type=int, default=1000, help="最多扫描的事件数")

    def handle(self, *args: Any, **options: Any) -> None:
        retention_days = options["retention_days"]
        limit = options["limit"]
        if retention_days < 0 or limit <= 0:
            self.stdout.write(json.dumps({"error": "invalid_arguments"}, ensure_ascii=False))
            return

        now = timezone.now()
        cutoff = now - timedelta(days=retention_days)
        targets = list(
            ContentSearchTarget.objects.filter(Q(enabled=True) | Q(required=True)).order_by("target_id")
        )
        active_build_count = SearchIndexBuild.objects.filter(status__in=_ACTIVE_BUILD_STATUSES).count()
        entries: list[dict[str, Any]] = []
        refusals: dict[str, int] = {}
        scanned = 0

        events = (
            ContentSearchOutbox.objects.filter(
                operation=ContentSearchOperation.TOMBSTONE,
                completed_at__isnull=False,
                completed_at__lte=cutoff,
            )
            .order_by("completed_at", "id")[:limit]
        )
        for event in events:
            scanned += 1
            reasons: list[str] = []
            if event.status not in _TERMINAL_DELIVERY_STATUSES:
                reasons.append("outbox_not_terminal")
            state = ContentSearchState.objects.filter(page_id=event.page_id).first()
            if state is None:
                reasons.append("state_missing")
            elif state.desired_operation != ContentSearchOperation.TOMBSTONE:
                reasons.append("state_not_tombstone")
            elif state.content_version < event.content_version:
                reasons.append("state_version_behind")

            delivery_rows = list(
                ContentSearchDelivery.objects.filter(event=event).select_related("target").order_by("target__target_id")
            )
            delivery_by_target = {row.target_id: row for row in delivery_rows}
            if not targets:
                reasons.append("no_required_targets")
            for target in targets:
                delivery = delivery_by_target.get(target.pk)
                if delivery is None:
                    reasons.append(f"delivery_missing:{target.target_id}")
                    continue
                if delivery.status not in _TERMINAL_DELIVERY_STATUSES:
                    reasons.append(f"delivery_not_terminal:{target.target_id}")
                if (
                    delivery.status == ContentSearchStatus.PROCESSING
                    and delivery.lock_expires_at is not None
                    and delivery.lock_expires_at <= now
                ):
                    reasons.append(f"delivery_lease_expired:{target.target_id}")

            if active_build_count:
                reasons.append("index_build_running")

            entry = {
                "event_id": str(event.event_id),
                "page_id": event.page_id,
                "content_version": event.content_version,
                "completed_at": event.completed_at.isoformat() if event.completed_at else None,
                "state_content_version": state.content_version if state else None,
                "delivery_statuses": {
                    target.target_id: delivery_by_target[target.pk].status
                    for target in targets
                    if target.pk in delivery_by_target
                },
                "eligible": not reasons,
                "refusal_reasons": reasons,
            }
            entries.append(entry)
            if reasons:
                for reason in reasons:
                    refusals[reason] = refusals.get(reason, 0) + 1

        manifest = [entry for entry in entries if entry["eligible"]]
        report = {
            "environment": os.environ.get("WAGTAILBLOG_ENV", "unset"),
            "dry_run": True,
            "read_only": True,
            "retention_days": retention_days,
            "cutoff": cutoff.isoformat(),
            "scanned": scanned,
            "candidate_count": len(manifest),
            "refusal_counts": dict(sorted(refusals.items())),
            "manifest_sha256": _sha256_manifest(manifest),
            "manifest": manifest,
            "active_build_count": active_build_count,
            "target_ids": [target.target_id for target in targets],
        }
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
