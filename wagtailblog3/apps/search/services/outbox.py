import logging
import re
from typing import Any

from django.conf import settings
from django.db import transaction
from wagtail.models import Page

from search.models import (
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchScopeJob,
    ContentSearchScopeJobStatus,
    ContentSearchState,
    ContentSearchStatus,
)
from search.services.document import build_formal_content_snapshot
from search.services.published_snapshot import (
    PublishedSnapshotError, PublishedSnapshotV1, effective_projection_schema_version,
)
from search.tasks import _maintenance_queue, wake_content_search_delivery


logger = logging.getLogger(__name__)


def published_projection_scope(page: Any) -> dict[str, Any]:
    """范围只读当前 SQL 拓扑；标题、摘要、正文仍只能来自固定快照。"""
    from wagtail.models import Site

    ancestors = list(page.get_ancestors(inclusive=True).values_list("pk", flat=True))
    return {
        "locale_id": page.locale_id,
        "ancestor_ids": ancestors,
        "site_ids": list(Site.objects.filter(root_page_id__in=ancestors).values_list("pk", flat=True)),
    }


def capture_final_publication(
    page: Any, *, publication_state: Any = None, revision: Any = None, adapter: Any = None,
) -> PublishedSnapshotV1:
    """在调用方持有 A 域锁时读取最终正式指针，不锁 SearchState、不恢复 Revision。

    Blog 的正式 Revision 来自 Page.live_revision_id，外置来自 PublicationState；
    二者都必须与原生保存结果一致。Alias 的来源核验仍由唯一 P03 适配器负责。
    """
    from wagtail.models import Revision
    from search.content_registry import get_content_registry
    from search.services.published_adapters import get_published_adapter

    entry = get_content_registry().for_model(page.specific_class if hasattr(page, "specific_class") else type(page))
    if entry is None or not entry.enabled:
        raise PublishedSnapshotError("publication_model_not_managed")
    if not page.live:
        raise PublishedSnapshotError("formal_page_not_live")
    if publication_state is None:
        if entry.snapshot_kind == "blog_v1":
            from blog.models import BlogPublicationState
            publication_state = BlogPublicationState.objects.get(page_id=page.pk)
        else:
            from external_content.models import ExternalContentPublicationState
            publication_state = ExternalContentPublicationState.objects.get(page_id=page.pk)
    generation = publication_state.publication_generation
    source_id = getattr(publication_state, "source_page_id", None)
    source_revision_id = getattr(publication_state, "source_revision_id", None)
    if entry.snapshot_kind == "blog_v1":
        revision_id = page.live_revision_id
    else:
        if publication_state.model_label != entry.model_label:
            raise PublishedSnapshotError("formal_model_mismatch")
        revision_id = source_revision_id if source_id else publication_state.published_revision_id
        if not source_id and page.live_revision_id != revision_id:
            raise PublishedSnapshotError("formal_revision_mismatch")
    if not revision_id or (revision is not None and revision.pk != revision_id):
        raise PublishedSnapshotError("formal_revision_mismatch")
    if revision is None:
        revision = Revision.objects.get(pk=revision_id)
    adapter = adapter or get_published_adapter(entry.model_class)
    kwargs = dict(
        revision=revision, publication_generation=generation,
        first_published_at=page.first_published_at, search_type=entry.search_type,
    )
    if source_id is not None:
        kwargs.update(source_page_id=source_id, source_revision_id=source_revision_id)
    if entry.snapshot_kind == "external_v1":
        kwargs["manifest"] = publication_state.published_manifest
    snapshot = adapter.capture(page, **kwargs)
    if entry.snapshot_kind == "blog_v1":
        body = snapshot.to_dict()["body_ref"]
        if any(body[key] != getattr(publication_state, field) for key, field in (
            ("body_version_id", "published_body_version_id"),
            ("body_sha256", "published_body_sha256"),
            ("body_schema_version", "published_body_schema_version"),
        )):
            raise PublishedSnapshotError("formal_body_pointer_mismatch")
    return snapshot


def build_event_projection(
    event: Any, target: Any, *, scope: dict[str, Any] | None = None, adapter: Any = None,
) -> Any:
    """从唯一 P03 编码器生成目标文档，摘要/hash 校验先于任何正文访问。"""
    from search.services import document
    from search.services.delivery import event_applies_to_target

    snapshot = PublishedSnapshotV1.from_dict(
        event.published_snapshot, expected_digest=event.snapshot_digest,
    )
    snapshot.validate_state(event)
    if not event_applies_to_target(event, target):
        raise PublishedSnapshotError("projection_target_incompatible")
    if not event.content_hash:
        raise PublishedSnapshotError("snapshot_hash_missing")
    if scope is None:
        page = Page.objects.get(pk=event.page_id)
        scope = published_projection_scope(page)
    result = document.build_published_content_document(
        snapshot, event.content_version, scope=scope,
        target_schema_version=effective_projection_schema_version(target.projection_schema_version),
        adapter=adapter,
    )
    if result is None or result.content_hash != event.content_hash:
        raise PublishedSnapshotError("projection_hash_mismatch")
    return result


def schedule_content_search_wakeup(event_id: Any) -> None:
    """消息发送失败不能回滚已提交的 Outbox，Beat 会在 WP3C 负责补偿扫描。"""

    try:
        wake_content_search_delivery.apply_async(
            kwargs={"event_id": str(event_id)},
            queue=_maintenance_queue(),
        )
    except Exception:
        logger.error("content_search_wakeup_enqueue_failed event_id=%s", event_id)


class ContentSearchOutboxService:
    """把公开页面状态变更和 Outbox 事件写入同一 MySQL 事务。"""

    @classmethod
    def record_final_publication(
        cls, page: Any, *, publication_state: Any = None, revision: Any = None,
    ) -> ContentSearchOutbox | None:
        """发布协调器的最终挂点：同事务固定正式快照、全文 hash 和事件。

        不自行重进 A 域锁闭包；调用方须先完成原生页面保存及所有 alias 生命周期锁。
        gate 默认关闭，旧 Blog 路径继续由旧生产者处理。
        """
        if not getattr(settings, "CONTENT_SEARCH_V2_PRODUCER_ENABLED", False):
            return None
        if not transaction.get_connection().in_atomic_block:
            raise PublishedSnapshotError("publication_transaction_required")
        from search.content_registry import get_content_registry
        from search.services.published_adapters import get_published_adapter
        from search.services.document import build_published_content_document

        entry = get_content_registry().for_model(type(page))
        if entry is None or not entry.enabled:
            return None
        # 重新读原生 SQL 结果，不能接受 commit=False/未来排期的内存 live 标记。
        formal_page = entry.model_class.objects.get(pk=page.pk)
        if not formal_page.live:
            return None
        adapter = get_published_adapter(entry.model_class)
        snapshot = capture_final_publication(
            formal_page, publication_state=publication_state, revision=revision, adapter=adapter,
        )
        is_public = cls._is_public(page.pk)
        content_hash = None
        if is_public:
            encoded = build_published_content_document(
                snapshot, 1, scope=published_projection_scope(formal_page), adapter=adapter,
            )
            content_hash = encoded.content_hash
        return cls.record_published_snapshot(
            formal_page, snapshot, content_hash=content_hash,
            operation=ContentSearchOperation.UPSERT if is_public else ContentSearchOperation.TOMBSTONE,
        )

    @classmethod
    def record_final_tombstone(
        cls, page: Any, *, publication_generation: int | None = None,
    ) -> ContentSearchOutbox | None:
        """下架/删除保留固定正式审计快照，不访问 Mongo，也不重新读取草稿。

        公开代次由生命周期服务传入；范围限制使用原代次。无 SearchState 表示从未
        纳入本投影，不伪造 Revision；已有 legacy 状态必须先显式 bootstrap。
        """
        if not getattr(settings, "CONTENT_SEARCH_V2_PRODUCER_ENABLED", False):
            return None
        if not transaction.get_connection().in_atomic_block:
            raise PublishedSnapshotError("publication_transaction_required")
        state = ContentSearchState.objects.select_for_update().filter(page_id=page.pk).first()
        if state is None:
            return None
        snapshot = PublishedSnapshotV1.from_dict(
            state.published_snapshot, expected_digest=state.snapshot_digest,
        )
        snapshot.validate_state(state)
        data = snapshot.to_dict()
        if publication_generation is not None:
            if type(publication_generation) is not int or publication_generation < data["publication_generation"]:
                raise PublishedSnapshotError("publication_generation_regressed")
            data["publication_generation"] = publication_generation
            snapshot = PublishedSnapshotV1.from_dict(data)
        return cls.record_published_snapshot(
            page, snapshot, content_hash=None, operation=ContentSearchOperation.TOMBSTONE,
        )

    @classmethod
    def record_published_snapshot(
        cls, page: Any, snapshot: PublishedSnapshotV1, *, content_hash: str | None,
        operation: str = ContentSearchOperation.UPSERT,
    ) -> ContentSearchOutbox | None:
        """P02 原发布事务内的唯一 v2 生产入口，不替发布协调器推进 generation。

        调用方必须已按 A 域锁住 Page/PublicationState/Revision/Intent，完成原生
        发布结果并捕获 P03 快照；此处只锁 SearchState→Outbox。禁止 on_commit
        再来补写事件，否则无法与正式指针一起回滚。
        """
        if not getattr(settings, "CONTENT_SEARCH_V2_PRODUCER_ENABLED", False):
            return None
        if not transaction.get_connection().in_atomic_block:
            raise PublishedSnapshotError("publication_transaction_required")
        if not isinstance(snapshot, PublishedSnapshotV1):
            raise PublishedSnapshotError("published_snapshot_required")
        data = snapshot.to_dict()
        if type(page.pk) is not int or data["page_id"] != page.pk:
            raise PublishedSnapshotError("snapshot_page_mismatch")
        if operation not in (ContentSearchOperation.UPSERT, ContentSearchOperation.TOMBSTONE):
            raise PublishedSnapshotError("projection_operation_invalid")
        if operation == ContentSearchOperation.UPSERT and (
            not isinstance(content_hash, str) or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
        ):
            raise PublishedSnapshotError("projection_hash_required")
        body = data["body_ref"] if data["snapshot_kind"] == "blog_v1" else {}
        return cls._record_change(
            page_id=page.pk, operation=operation,
            searchable=operation == ContentSearchOperation.UPSERT,
            mongo_content_id=body.get("mongo_content_id"),
            body_version_id=body.get("body_version_id"),
            publication_generation=data["publication_generation"],
            content_hash=content_hash if operation == ContentSearchOperation.UPSERT else None,
            published_snapshot=snapshot, page_already_locked=True,
        )

    @classmethod
    def record_publication(cls, page: Any) -> Any:
        if getattr(settings, "CONTENT_SEARCH_V2_PRODUCER_ENABLED", False):
            # 新发布协调器承担唯一版本推进，旧信号不可偷偷写第二个事件。
            return None
        if not settings.CONTENT_SEARCH_PRODUCER_ENABLED:
            return None
        if not cls._is_public(page.pk):
            logger.info("content_search_publication_tombstone page_id=%s reason=not_public", page.pk)
            return cls._record_tombstone(page)

        body_version_id, publication_generation = cls._publication_metadata(page.pk)
        snapshot = build_formal_content_snapshot(page)
        if snapshot is None:
            # Mongo 暂时不可读时保留 pending 事件，后续消费者只能重试，绝不能用空正文覆盖旧文档。
            logger.warning("content_search_formal_body_unavailable page_id=%s", page.pk)
            return cls._record_change(
                page_id=page.pk,
                operation=ContentSearchOperation.UPSERT,
                searchable=True,
                mongo_content_id=getattr(page, "mongo_content_id", None),
                content_hash=None,
                body_version_id=body_version_id,
                publication_generation=publication_generation,
            )

        event = cls._record_change(
            page_id=page.pk,
            operation=ContentSearchOperation.UPSERT,
            searchable=True,
            mongo_content_id=snapshot.mongo_content_id,
            content_hash=snapshot.content_hash,
            body_version_id=body_version_id,
            publication_generation=publication_generation,
        )
        logger.info(
            "content_search_publication_recorded page_id=%s event_uuid=%s content_version=%s body_version_id=%s generation=%s",
            page.pk,
            event.event_id,
            event.content_version,
            event.body_version_id,
            event.publication_generation,
        )
        return event

    @classmethod
    def record_unpublish(cls, page: Any) -> Any:
        if getattr(settings, "CONTENT_SEARCH_V2_PRODUCER_ENABLED", False):
            return None
        if not settings.CONTENT_SEARCH_PRODUCER_ENABLED:
            return None
        event = cls._record_tombstone(page)
        logger.info("content_search_unpublish_recorded page_id=%s event_uuid=%s generation=%s", page.pk, event.event_id, event.publication_generation)
        return event

    @classmethod
    def record_delete(cls, page: Any) -> Any:
        if getattr(settings, "CONTENT_SEARCH_V2_PRODUCER_ENABLED", False):
            return None
        if not settings.CONTENT_SEARCH_PRODUCER_ENABLED:
            return None
        event = cls._record_tombstone(page)
        logger.info("content_search_delete_recorded page_id=%s event_uuid=%s generation=%s", page.pk, event.event_id, event.publication_generation)
        return event

    @classmethod
    def request_scope_recalculation(cls, root_page_id: int) -> Any:
        if not (
            settings.CONTENT_SEARCH_PRODUCER_ENABLED
            or getattr(settings, "CONTENT_SEARCH_V2_PRODUCER_ENABLED", False)
        ):
            return None

        with transaction.atomic():
            Page.objects.select_for_update().only("pk").get(pk=root_page_id)
            active_job = (
                ContentSearchScopeJob.objects.select_for_update()
                .filter(
                    root_page_id=root_page_id,
                    status__in=(
                        ContentSearchScopeJobStatus.PENDING,
                        ContentSearchScopeJobStatus.PROCESSING,
                        ContentSearchScopeJobStatus.RETRY,
                    ),
                )
                .order_by("pk")
                .first()
            )
            if active_job is not None:
                if active_job.status == ContentSearchScopeJobStatus.PROCESSING:
                    active_job.rescan_requested = True
                    active_job.save(update_fields=("rescan_requested", "updated_at"))
                return active_job

            job = ContentSearchScopeJob.objects.create(root_page_id=root_page_id)
            transaction.on_commit(lambda: schedule_content_search_wakeup(f"scope:{job.pk}"))
            return job

    @classmethod
    def _record_tombstone(cls, page: Any) -> Any:
        body_version_id, publication_generation = cls._publication_metadata(page.pk)
        return cls._record_change(
            page_id=page.pk,
            operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
            mongo_content_id=None,
            content_hash=None,
            body_version_id=body_version_id,
            publication_generation=publication_generation,
        )

    @classmethod
    def _record_change(
        cls,
        page_id: int,
        operation: str,
        searchable: bool,
        mongo_content_id: str | None,
        content_hash: str | None,
        body_version_id: str | None = None,
        publication_generation: int | None = None,
        published_snapshot: PublishedSnapshotV1 | None = None,
        page_already_locked: bool = False,
    ) -> ContentSearchOutbox:
        with transaction.atomic():
            # 首次发布时 State 尚不存在，先锁定 Page 行可避免两个发布请求并发创建不同初始版本。
            if not page_already_locked:
                Page.objects.select_for_update().only("pk").get(pk=page_id)
            state = (
                ContentSearchState.objects.select_for_update()
                .filter(page_id=page_id)
                .first()
            )
            if state is None:
                state = ContentSearchState(page_id=page_id, content_version=0)
            elif (
                published_snapshot is None
                and effective_projection_schema_version(getattr(state, "projection_schema_version", None)) == 2
            ):
                raise PublishedSnapshotError("legacy_producer_cannot_downgrade_state")

            snapshot_fields = {}
            if published_snapshot is not None:
                data = published_snapshot.to_dict()
                snapshot_fields = {
                    name: data[name]
                    for name in ("model_label", "search_type", "snapshot_kind", "projection_schema_version")
                }
                snapshot_fields.update(
                    published_snapshot=data, snapshot_digest=published_snapshot.digest,
                )
                # 同一正式快照重入（原生信号/协调器）不再递增版本；范围 hash 变更仍生成新版本。
                if (
                    state.content_version > 0
                    and getattr(state, "snapshot_digest", None) == published_snapshot.digest
                    and state.desired_operation == operation and state.searchable == searchable
                    and state.content_hash == content_hash
                ):
                    existing = ContentSearchOutbox.objects.select_for_update().filter(
                        page_id=page_id, content_version=state.content_version,
                    ).first()
                    if existing is None:
                        raise PublishedSnapshotError("current_snapshot_event_missing")
                    published_snapshot.validate_state(state)
                    published_snapshot.validate_state(existing)
                    if (
                        existing.operation != operation or existing.searchable != searchable
                        or existing.content_hash != content_hash
                        or existing.content_version != state.content_version
                    ):
                        raise PublishedSnapshotError("current_snapshot_event_mismatch")
                    return existing

            state.content_version += 1
            state.desired_operation = operation
            state.searchable = searchable
            state.mongo_content_id = mongo_content_id
            state.content_hash = content_hash
            state.body_version_id = body_version_id
            state.publication_generation = publication_generation
            for name, value in snapshot_fields.items():
                setattr(state, name, value)
            state.save()

            event = ContentSearchOutbox.objects.create(
                page_id=page_id,
                content_version=state.content_version,
                operation=operation,
                content_hash=content_hash,
                mongo_content_id=mongo_content_id,
                searchable=searchable,
                body_version_id=body_version_id,
                publication_generation=publication_generation,
                status=ContentSearchStatus.PENDING,
                **snapshot_fields,
            )
            transaction.on_commit(lambda: schedule_content_search_wakeup(event.event_id))
            logger.info(
                "content_search_outbox_created page_id=%s event_id=%s operation=%s content_version=%s body_version_id=%s generation=%s searchable=%s",
                page_id,
                event.event_id,
                event.operation,
                event.content_version,
                event.body_version_id,
                event.publication_generation,
                event.searchable,
            )
            return event

    @staticmethod
    def _is_public(page_id: int) -> bool:
        return Page.objects.live().public().filter(pk=page_id).exists()

    @staticmethod
    def _publication_metadata(page_id: int) -> tuple[str | None, int | None]:
        """读取已提交的 BlogPage 正式正文身份；兼容尚未建立发布状态的旧页面。"""
        try:
            from blog.models import BlogPublicationState

            state = BlogPublicationState.objects.filter(page_id=page_id).values(
                "published_body_version_id", "publication_generation"
            ).first()
        except Exception:
            # 搜索事件不能因兼容状态表不可用而阻断页面发布，旧字段仍可驱动投影。
            return None, None
        if not state:
            return None, None
        body_version_id = state.get("published_body_version_id")
        generation = state.get("publication_generation")
        return (
            body_version_id if isinstance(body_version_id, str) and body_version_id else None,
            generation if isinstance(generation, int) and generation > 0 else None,
        )
