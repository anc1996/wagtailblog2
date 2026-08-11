"""独立内容索引的只读状态汇总和有界一致性检查。"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from search.models import (
    ContentSearchDelivery,
    ContentSearchOperation,
    ContentSearchState,
    ContentSearchStatus,
)
from search.services.elasticsearch import (
    read_content_search_documents,
    scan_content_search_documents,
)


def _bounded_limit(value, default, maximum):
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError):
        converted = default
    return max(1, min(converted, maximum))


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _append_sample(samples, key, page_id, sample_limit):
    if len(samples[key]) < sample_limit:
        samples[key].append(page_id)


def get_content_search_sync_status(targets):
    """汇总每个目标的 Delivery 状态；只返回计数、年龄和脱敏失败分类。"""

    now = timezone.now()
    report_targets = []
    for target in targets:
        deliveries = ContentSearchDelivery.objects.filter(target=target)
        status_counts = {status: 0 for status in ContentSearchStatus.values}
        for item in deliveries.values("status").annotate(total=Count("pk")):
            status_counts[item["status"]] = item["total"]
        oldest = (
            deliveries.filter(
                status__in=(ContentSearchStatus.PENDING, ContentSearchStatus.RETRY)
            )
            .order_by("available_at", "pk")
            .values_list("available_at", flat=True)
            .first()
        )
        failure_counts = {
            item["last_error_code"] or "unspecified": item["total"]
            for item in deliveries.filter(
                status__in=(ContentSearchStatus.RETRY, ContentSearchStatus.DEAD)
            )
            .values("last_error_code")
            .annotate(total=Count("pk"))
        }
        report_targets.append(
            {
                "target_id": target.target_id,
                "connection_name": target.connection_name,
                "index_name": target.index_name,
                "enabled": target.enabled,
                "required": target.required,
                "status_counts": status_counts,
                "oldest_pending_retry_age_seconds": (
                    max(0, int((now - oldest).total_seconds())) if oldest else None
                ),
                "expired_processing_count": deliveries.filter(
                    status=ContentSearchStatus.PROCESSING,
                    lock_expires_at__lte=now,
                ).count(),
                "lease_reclaims": deliveries.aggregate(total=Sum("lease_reclaims"))["total"]
                or 0,
                "succeeded_last_minute": deliveries.filter(
                    status=ContentSearchStatus.SUCCEEDED,
                    completed_at__gte=now - timedelta(minutes=1),
                ).count(),
                "failure_counts": failure_counts,
            }
        )
    return report_targets


def check_content_search_consistency(target, after_page_id=0, limit=1000, sample_limit=20):
    """比较一批 State 与 ES 文档，并独立扫描同一游标后的 ES 额外文档。"""

    limit = _bounded_limit(limit, default=1000, maximum=5000)
    sample_limit = _bounded_limit(sample_limit, default=20, maximum=100)
    after_page_id = max(0, _safe_int(after_page_id) or 0)
    states = list(
        ContentSearchState.objects.filter(page_id__gt=after_page_id)
        .order_by("page_id")[:limit]
    )
    state_ids = [state.page_id for state in states]
    index_documents = read_content_search_documents(target, state_ids)
    counts = Counter()
    samples = {
        "missing": [],
        "stale": [],
        "ahead": [],
        "hash_mismatch": [],
        "wrong_tombstone": [],
        "extra": [],
    }

    for state in states:
        document = index_documents.get(state.page_id)
        if document is None:
            counts["missing"] += 1
            _append_sample(samples, "missing", state.page_id, sample_limit)
            continue
        document_version = _safe_int(document.get("content_version"))
        if document_version is None or document_version < state.content_version:
            counts["stale"] += 1
            _append_sample(samples, "stale", state.page_id, sample_limit)
            continue
        if document_version > state.content_version:
            counts["ahead"] += 1
            _append_sample(samples, "ahead", state.page_id, sample_limit)
            continue

        expected_tombstone = (
            state.desired_operation == ContentSearchOperation.TOMBSTONE
            or not state.searchable
        )
        actual_tombstone = (
            not bool(document.get("searchable"))
            and document.get("operation") == ContentSearchOperation.TOMBSTONE
        )
        if expected_tombstone != actual_tombstone:
            counts["wrong_tombstone"] += 1
            _append_sample(samples, "wrong_tombstone", state.page_id, sample_limit)
            continue
        if (
            not expected_tombstone
            and state.content_hash
            and document.get("content_hash") != state.content_hash
        ):
            counts["hash_mismatch"] += 1
            _append_sample(samples, "hash_mismatch", state.page_id, sample_limit)

    scanned_documents = scan_content_search_documents(target, after_page_id, limit)
    scanned_ids = [page_id for page_id, _document in scanned_documents]
    known_state_ids = set(
        ContentSearchState.objects.filter(page_id__in=scanned_ids).values_list(
            "page_id", flat=True
        )
    )
    for page_id, _document in scanned_documents:
        if page_id not in known_state_ids:
            counts["extra"] += 1
            _append_sample(samples, "extra", page_id, sample_limit)

    return {
        "target_id": target.target_id,
        "connection_name": target.connection_name,
        "index_name": target.index_name,
        "after_page_id": after_page_id,
        "next_state_after_page_id": state_ids[-1] if state_ids else after_page_id,
        "next_index_after_page_id": scanned_ids[-1] if scanned_ids else after_page_id,
        "state_scanned": len(states),
        "index_scanned": len(scanned_documents),
        "counts": {
            key: counts[key]
            for key in (
                "missing",
                "stale",
                "ahead",
                "hash_mismatch",
                "wrong_tombstone",
                "extra",
            )
        },
        "samples": samples,
    }
