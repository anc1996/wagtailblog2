"""独立内容索引的在线回填、双投递启动和追平门禁。"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from blog.models import BlogPage
from search.models import (
    ContentSearchOperation,
    ContentSearchState,
    ContentSearchStatus,
    ContentSearchTarget,
    ContentSearchTargetRole,
    SearchIndexBuild,
    SearchIndexBuildStatus,
)
from search.services.delivery import (
    materialize_content_search_deliveries,
    reclaim_expired_content_search_deliveries,
)
from search.services.document import build_formal_content_documents
from search.services.elasticsearch import (
    ContentSearchElasticsearchError,
    estimate_content_search_bulk_bytes,
    read_content_search_documents,
    scan_content_search_documents,
    verify_content_search_index,
    write_content_search_documents,
)
from search.services.mongo import (
    ContentSearchMongoReadError,
    read_formal_contents_by_id,
)


class ContentSearchRebuildError(Exception):
    """不携带敏感原文的稳定重建错误编码。"""
    """回填失败的脱敏分类，不携带正文、连接信息或底层异常原文。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ContentSearchRebuildBatch:
    """单批回填结果；只有整批写入成功才推进 checkpoint。"""
    done: bool
    checkpoint_page_id: int
    scanned: int
    succeeded: int
    superseded: int
    batch_count: int
    batch_bytes: int


def _positive_int(value: object, default: int, maximum: int | None = None) -> int:
    """将外部批量参数归一化到正整数范围。"""
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    value = max(1, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _build_for_target(target_id: str, lock: bool = False):
    """读取目标及最新 build；写状态时通过行锁保证并发安全。"""
    target_query = ContentSearchTarget.objects
    if lock:
        target_query = target_query.select_for_update()
    target = target_query.filter(target_id=target_id).first()
    if target is None:
        raise ContentSearchRebuildError("content_target_not_found")
    build_query = SearchIndexBuild.objects
    if lock:
        build_query = build_query.select_for_update()
    build = build_query.filter(target=target).order_by("-pk").first()
    if build is None:
        raise ContentSearchRebuildError("content_build_not_found")
    return target, build


def _public_page_upper_bound() -> int:
    """返回启动回填时公开页面的最大主键，固定扫描上界。"""
    return (
        BlogPage.objects.live()
        .public()
        .aggregate(max_page_id=Max("pk"))
        .get("max_page_id")
        or 0
    )


def start_content_search_build(target_id: str, resume: bool = False):
    """注册双投递起点并固定扫描上界，然后才允许开始回填。"""

    if not settings.CONTENT_SEARCH_PRODUCER_ENABLED:
        raise ContentSearchRebuildError("content_producer_required")
    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        raise ContentSearchRebuildError("content_consumer_required")
    target_preview = ContentSearchTarget.objects.filter(target_id=target_id).first()
    if target_preview is None:
        raise ContentSearchRebuildError("content_target_not_found")
    try:
        verify_content_search_index(target_preview)
    except ContentSearchElasticsearchError as error:
        raise ContentSearchRebuildError(error.code) from error

    with transaction.atomic():
        target, build = _build_for_target(target_id, lock=True)
        if target.role != ContentSearchTargetRole.BUILDING:
            raise ContentSearchRebuildError("content_target_not_building")
        if build.status in {
            SearchIndexBuildStatus.READY,
            SearchIndexBuildStatus.SERVING,
            SearchIndexBuildStatus.RETIRED,
        }:
            raise ContentSearchRebuildError("content_build_not_resumable")
        if build.status in {
            SearchIndexBuildStatus.BACKFILLING,
            SearchIndexBuildStatus.CATCHING_UP,
        } and not resume:
            raise ContentSearchRebuildError("content_build_resume_required")
        if build.status == SearchIndexBuildStatus.FAILED and not resume:
            raise ContentSearchRebuildError("content_build_resume_required")

        if not build.scan_upper_bound_page_id:
            build.scan_upper_bound_page_id = _public_page_upper_bound()
        target.enabled = True
        target.save(update_fields=("enabled", "updated_at"))
        build.status = SearchIndexBuildStatus.BACKFILLING
        build.started_at = build.started_at or timezone.now()
        build.ready_at = None
        build.catch_up_clean_streak = 0
        build.last_error_code = ""
        build.last_error_message = ""
        build.save(
            update_fields=(
                "status",
                "scan_upper_bound_page_id",
                "started_at",
                "ready_at",
                "catch_up_clean_streak",
                "last_error_code",
                "last_error_message",
                "updated_at",
            )
        )
    # 目标启用后立即补齐注册以来的事件，避免等待下一次 Beat 才建立双投递。
    materialize_content_search_deliveries()
    return _build_for_target(target_id)


def _split_documents_by_bytes(target: object, documents: list[dict[str, object]], max_batch_bytes: int):
    """按 ES bulk 估算字节数切分文档，单文档超限时立即失败。"""
    current = []
    current_bytes = 0
    for document in documents:
        document_bytes = estimate_content_search_bulk_bytes(target, [document])
        if document_bytes > max_batch_bytes:
            raise ContentSearchRebuildError("content_search_document_too_large")
        if current and current_bytes + document_bytes > max_batch_bytes:
            yield current, current_bytes
            current = []
            current_bytes = 0
        current.append(document)
        current_bytes += document_bytes
    if current:
        yield current, current_bytes


def _mark_build_failed(build_id: int, error_code: str, *, missing: int = 0, failed: int = 0) -> None:
    """原子记录失败状态和计数，保留已有 checkpoint 供恢复。"""
    with transaction.atomic():
        build = SearchIndexBuild.objects.select_for_update().get(pk=build_id)
        build.status = SearchIndexBuildStatus.FAILED
        build.failed += failed
        build.missing += missing
        build.last_error_code = error_code
        build.last_error_message = ""
        build.catch_up_clean_streak = 0
        build.save(
            update_fields=(
                "status",
                "failed",
                "missing",
                "last_error_code",
                "last_error_message",
                "catch_up_clean_streak",
                "updated_at",
            )
        )


def _checkpoint_build(
    build_id: int,
    page_id: int,
    scanned: int,
    succeeded: int,
    superseded: int,
    batch_count: int,
    batch_bytes: int,
):
    """在事务中推进 checkpoint；较旧 checkpoint 永远不能覆盖新值。"""
    with transaction.atomic():
        build = SearchIndexBuild.objects.select_for_update().get(pk=build_id)
        if build.checkpoint_page_id >= page_id:
            return build
        build.checkpoint_page_id = page_id
        build.scanned += scanned
        build.succeeded += succeeded
        build.superseded += superseded
        build.last_batch_count = batch_count
        build.last_batch_bytes = batch_bytes
        build.last_checkpoint_at = timezone.now()
        build.last_error_code = ""
        build.last_error_message = ""
        build.save(
            update_fields=(
                "checkpoint_page_id",
                "scanned",
                "succeeded",
                "superseded",
                "last_batch_count",
                "last_batch_bytes",
                "last_checkpoint_at",
                "last_error_code",
                "last_error_message",
                "updated_at",
            )
        )
        return build


def rebuild_content_search_batch(target_id: str, batch_size: int, max_batch_bytes: int) -> ContentSearchRebuildBatch:
    """处理一个 MySQL 游标批次；只有整批成功才推进 checkpoint。"""

    batch_size = _positive_int(batch_size, 200, maximum=1000)
    max_batch_bytes = _positive_int(max_batch_bytes, 4 * 1024 * 1024)
    target, build = _build_for_target(target_id)
    if build.status != SearchIndexBuildStatus.BACKFILLING:
        raise ContentSearchRebuildError("content_build_not_backfilling")

    pages = list(
        BlogPage.objects.live()
        .public()
        .filter(
            pk__gt=build.checkpoint_page_id,
            pk__lte=build.scan_upper_bound_page_id,
        )
        .prefetch_related("tags", "categories")
        .order_by("pk")[:batch_size]
    )
    if not pages:
        with transaction.atomic():
            locked_build = SearchIndexBuild.objects.select_for_update().get(pk=build.pk)
            locked_build.status = SearchIndexBuildStatus.CATCHING_UP
            locked_build.save(
                update_fields=("status", "last_error_code", "last_error_message", "updated_at")
            )
        materialize_content_search_deliveries()
        return ContentSearchRebuildBatch(
            done=True,
            checkpoint_page_id=build.checkpoint_page_id,
            scanned=0,
            succeeded=0,
            superseded=0,
            batch_count=0,
            batch_bytes=0,
        )

    page_ids = [page.pk for page in pages]
    states = ContentSearchState.objects.in_bulk(page_ids, field_name="page_id")
    missing_state_ids = [page_id for page_id in page_ids if page_id not in states]
    if missing_state_ids or any(states[page_id].content_version <= 0 for page_id in page_ids):
        _mark_build_failed(build.pk, "content_search_state_missing", failed=len(pages))
        raise ContentSearchRebuildError("content_search_state_missing")

    mongo_ids = [getattr(page, "mongo_content_id", None) for page in pages]
    try:
        formal_contents = read_formal_contents_by_id(mongo_ids, page_ids=page_ids)
        versions = {page_id: states[page_id].content_version for page_id in page_ids}
        body_version_ids = {
            page_id: states[page_id].body_version_id for page_id in page_ids
        }
        publication_generations = {
            page_id: states[page_id].publication_generation for page_id in page_ids
        }
        formal_documents, missing_page_ids = build_formal_content_documents(
            pages,
            versions,
            formal_contents,
            body_version_ids=body_version_ids,
            publication_generations=publication_generations,
        )
    except ContentSearchMongoReadError:
        _mark_build_failed(build.pk, "mongo_formal_content_batch_read_failed", failed=len(pages))
        raise ContentSearchRebuildError("mongo_formal_content_batch_read_failed")

    if missing_page_ids:
        _mark_build_failed(
            build.pk,
            "mongo_formal_content_unavailable",
            missing=len(missing_page_ids),
            failed=len(pages),
        )
        raise ContentSearchRebuildError("mongo_formal_content_unavailable")

    documents = [formal_document.document for formal_document in formal_documents]
    pages_by_id = {page.pk: page for page in pages}
    for document in documents:
        state = states[document["page_id"]]
        page = pages_by_id[document["page_id"]]
        if state.mongo_content_id != getattr(page, "mongo_content_id", None):
            _mark_build_failed(build.pk, "content_search_state_content_id_mismatch", failed=len(pages))
            raise ContentSearchRebuildError("content_search_state_content_id_mismatch")
        if state.content_hash != document["content_hash"]:
            _mark_build_failed(build.pk, "content_search_state_hash_mismatch", failed=len(pages))
            raise ContentSearchRebuildError("content_search_state_hash_mismatch")
        if state.body_version_id != document.get("body_version_id"):
            _mark_build_failed(build.pk, "content_search_state_body_version_mismatch", failed=len(pages))
            raise ContentSearchRebuildError("content_search_state_body_version_mismatch")
        if state.publication_generation != document.get("publication_generation"):
            _mark_build_failed(build.pk, "content_search_state_generation_mismatch", failed=len(pages))
            raise ContentSearchRebuildError("content_search_state_generation_mismatch")

    succeeded = 0
    superseded = 0
    batch_bytes = 0
    try:
        for chunk, chunk_bytes in _split_documents_by_bytes(
            target,
            documents,
            max_batch_bytes,
        ):
            result = write_content_search_documents(target, chunk)
            succeeded += result.succeeded
            superseded += result.superseded
            batch_bytes += chunk_bytes
    except (ContentSearchElasticsearchError, ContentSearchRebuildError) as error:
        error_code = getattr(error, "code", "content_search_bulk_write_failed")
        _mark_build_failed(build.pk, error_code, failed=len(pages))
        raise ContentSearchRebuildError(error_code) from error

    checkpoint = _checkpoint_build(
        build.pk,
        pages[-1].pk,
        len(pages),
        succeeded,
        superseded,
        len(documents),
        batch_bytes,
    )
    materialize_content_search_deliveries()
    return ContentSearchRebuildBatch(
        done=False,
        checkpoint_page_id=checkpoint.checkpoint_page_id,
        scanned=len(pages),
        succeeded=succeeded,
        superseded=superseded,
        batch_count=len(documents),
        batch_bytes=batch_bytes,
    )


def rebuild_content_search_index(
    target_id: str,
    batch_size: int,
    max_batch_bytes: int,
    max_batches: int | None = None,
):
    """循环回填直到扫描上界完成，或按运维指定的批次数安全暂停。"""

    if max_batches is not None:
        max_batches = _positive_int(max_batches, 1, maximum=100000)
    batches = 0
    while max_batches is None or batches < max_batches:
        result = rebuild_content_search_batch(target_id, batch_size, max_batch_bytes)
        batches += 1
        if result.done:
            break
    _target, build = _build_for_target(target_id)
    return build, batches, result


def _append_sample(samples: dict[str, list[object]], key: str, value: object, limit: int) -> None:
    """只保留有限样本，避免一致性报告输出大规模页面 ID。"""
    if len(samples[key]) < limit:
        samples[key].append(value)


def _check_build_index_consistency(target: object, sample_limit: int = 20, batch_size: int = 1000) -> dict[str, object]:
    """只检查公开页面和 ES 中可搜索文档，避免把历史墓碑误判为缺失。"""

    sample_limit = _positive_int(sample_limit, 20, maximum=100)
    batch_size = _positive_int(batch_size, 1000, maximum=5000)
    counts = {
        "missing_state": 0,
        "missing_document": 0,
        "stale": 0,
        "ahead": 0,
        "hash_mismatch": 0,
        "wrong_operation": 0,
        "extra_searchable": 0,
    }
    samples = {key: [] for key in counts}

    public_queryset = BlogPage.objects.live().public().order_by("pk")
    after_page_id = 0
    while True:
        pages = list(public_queryset.filter(pk__gt=after_page_id)[:batch_size])
        if not pages:
            break
        page_ids = [page.pk for page in pages]
        states = ContentSearchState.objects.in_bulk(page_ids, field_name="page_id")
        documents = read_content_search_documents(target, page_ids)
        for page_id in page_ids:
            state = states.get(page_id)
            if state is None:
                counts["missing_state"] += 1
                _append_sample(samples, "missing_state", page_id, sample_limit)
                continue
            document = documents.get(page_id)
            if document is None:
                counts["missing_document"] += 1
                _append_sample(samples, "missing_document", page_id, sample_limit)
                continue
            try:
                document_version = int(document.get("content_version"))
            except (TypeError, ValueError):
                document_version = None
            if document_version is None or document_version < state.content_version:
                counts["stale"] += 1
                _append_sample(samples, "stale", page_id, sample_limit)
            elif document_version > state.content_version:
                counts["ahead"] += 1
                _append_sample(samples, "ahead", page_id, sample_limit)
            if document.get("operation") != ContentSearchOperation.UPSERT:
                counts["wrong_operation"] += 1
                _append_sample(samples, "wrong_operation", page_id, sample_limit)
            if state.content_hash and document.get("content_hash") != state.content_hash:
                counts["hash_mismatch"] += 1
                _append_sample(samples, "hash_mismatch", page_id, sample_limit)
        after_page_id = pages[-1].pk

    after_page_id = 0
    while True:
        scanned = scan_content_search_documents(target, after_page_id, batch_size)
        if not scanned:
            break
        scanned_ids = [page_id for page_id, _document in scanned]
        states = ContentSearchState.objects.in_bulk(scanned_ids, field_name="page_id")
        public_ids = set(
            BlogPage.objects.live().public().filter(pk__in=scanned_ids).values_list("pk", flat=True)
        )
        for page_id, document in scanned:
            if bool(document.get("searchable")) and page_id not in public_ids:
                counts["extra_searchable"] += 1
                _append_sample(samples, "extra_searchable", page_id, sample_limit)
            state = states.get(page_id)
            if state is None:
                continue
            try:
                document_version = int(document.get("content_version"))
            except (TypeError, ValueError):
                document_version = None
            if document_version is not None and document_version != state.content_version:
                key = "stale" if document_version < state.content_version else "ahead"
                counts[key] += 1
                _append_sample(samples, key, page_id, sample_limit)
        after_page_id = scanned[-1][0]

    return {"counts": counts, "samples": samples}


def get_content_search_build_gate(target_id: str, mutate: bool = False) -> dict[str, object]:
    """检查回填、Delivery 和公开文档的一致性；连续两次干净检查才就绪。"""

    if mutate:
        reclaim_expired_content_search_deliveries()
        materialize_content_search_deliveries()
    target, build = _build_for_target(target_id, lock=False)
    deliveries = target.deliveries.all()
    status_counts = {
        status: deliveries.filter(status=status).count() for status in ContentSearchStatus.values
    }
    unsettled = sum(
        status_counts[status]
        for status in (
            ContentSearchStatus.PENDING,
            ContentSearchStatus.PROCESSING,
            ContentSearchStatus.RETRY,
            ContentSearchStatus.DEAD,
        )
    )
    consistency = {
        "counts": {},
        "samples": {},
    }
    if unsettled == 0 and build.checkpoint_page_id >= build.scan_upper_bound_page_id:
        consistency = _check_build_index_consistency(target)
    consistency_clean = not any(consistency["counts"].values()) if consistency["counts"] else unsettled == 0
    checkpoint_clean = build.checkpoint_page_id >= build.scan_upper_bound_page_id
    clean = checkpoint_clean and unsettled == 0 and consistency_clean

    if mutate:
        with transaction.atomic():
            locked_build = SearchIndexBuild.objects.select_for_update().get(pk=build.pk)
            if clean:
                locked_build.catch_up_clean_streak += 1
                if locked_build.catch_up_clean_streak >= 2:
                    locked_build.status = SearchIndexBuildStatus.READY
                    locked_build.ready_at = timezone.now()
                else:
                    locked_build.status = SearchIndexBuildStatus.CATCHING_UP
            else:
                locked_build.catch_up_clean_streak = 0
                locked_build.status = SearchIndexBuildStatus.CATCHING_UP
                locked_build.ready_at = None
            locked_build.save(
                update_fields=(
                    "status",
                    "catch_up_clean_streak",
                    "ready_at",
                    "updated_at",
                )
            )
            build = locked_build

    return {
        "target_id": target.target_id,
        "index_name": target.index_name,
        "build_id": str(build.build_id),
        "status": build.status,
        "checkpoint_page_id": build.checkpoint_page_id,
        "scan_upper_bound_page_id": build.scan_upper_bound_page_id,
        "checkpoint_clean": checkpoint_clean,
        "delivery_status_counts": status_counts,
        "unsettled_delivery_count": unsettled,
        "consistency": consistency,
        "clean": clean,
        "catch_up_clean_streak": build.catch_up_clean_streak,
    }


def content_search_build_report(build: object) -> dict[str, object]:
    """返回不含正文和错误原文的构建摘要。"""

    return {
        "build_id": str(build.build_id),
        "target_id": build.target.target_id,
        "index_name": build.target.index_name,
        "status": build.status,
        "scan_upper_bound_page_id": build.scan_upper_bound_page_id,
        "checkpoint_page_id": build.checkpoint_page_id,
        "scanned": build.scanned,
        "succeeded": build.succeeded,
        "superseded": build.superseded,
        "failed": build.failed,
        "missing": build.missing,
        "last_batch_count": build.last_batch_count,
        "last_batch_bytes": build.last_batch_bytes,
        "catch_up_clean_streak": build.catch_up_clean_streak,
        "last_error_code": build.last_error_code,
    }
