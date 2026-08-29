import logging
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
from search.tasks import wake_content_search_delivery


logger = logging.getLogger(__name__)


def schedule_content_search_wakeup(event_id: Any) -> None:
    """消息发送失败不能回滚已提交的 Outbox，Beat 会在 WP3C 负责补偿扫描。"""

    try:
        wake_content_search_delivery.apply_async(
            kwargs={"event_id": str(event_id)},
            queue="maintenance",
        )
    except Exception as error:
        logger.error("content_search_wakeup_enqueue_failed event_id=%s error=%s", event_id, error)


class ContentSearchOutboxService:
    """把公开页面状态变更和 Outbox 事件写入同一 MySQL 事务。"""

    @classmethod
    def record_publication(cls, page: Any) -> Any:
        if not settings.CONTENT_SEARCH_PRODUCER_ENABLED:
            return None
        if not cls._is_public(page.pk):
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

        return cls._record_change(
            page_id=page.pk,
            operation=ContentSearchOperation.UPSERT,
            searchable=True,
            mongo_content_id=snapshot.mongo_content_id,
            content_hash=snapshot.content_hash,
            body_version_id=body_version_id,
            publication_generation=publication_generation,
        )

    @classmethod
    def record_unpublish(cls, page: Any) -> Any:
        if not settings.CONTENT_SEARCH_PRODUCER_ENABLED:
            return None
        return cls._record_tombstone(page)

    @classmethod
    def record_delete(cls, page: Any) -> Any:
        if not settings.CONTENT_SEARCH_PRODUCER_ENABLED:
            return None
        return cls._record_tombstone(page)

    @classmethod
    def request_scope_recalculation(cls, root_page_id: int) -> Any:
        if not settings.CONTENT_SEARCH_PRODUCER_ENABLED:
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
    ) -> ContentSearchOutbox:
        with transaction.atomic():
            # 首次发布时 State 尚不存在，先锁定 Page 行可避免两个发布请求并发创建不同初始版本。
            Page.objects.select_for_update().only("pk").get(pk=page_id)
            state = (
                ContentSearchState.objects.select_for_update()
                .filter(page_id=page_id)
                .first()
            )
            if state is None:
                state = ContentSearchState(page_id=page_id, content_version=0)

            state.content_version += 1
            state.desired_operation = operation
            state.searchable = searchable
            state.mongo_content_id = mongo_content_id
            state.content_hash = content_hash
            state.body_version_id = body_version_id
            state.publication_generation = publication_generation
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
            )
            transaction.on_commit(lambda: schedule_content_search_wakeup(event.event_id))
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
