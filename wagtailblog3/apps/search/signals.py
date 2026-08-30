"""搜索投影的 Wagtail 生命周期信号适配。"""

import logging
from typing import Any

from django.conf import settings
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from wagtail.models import PageViewRestriction
from wagtail.signals import page_published, page_unpublished

from blog.models import BlogPage
from blog.services.publication import BlogPublicationService
from search.services.outbox import ContentSearchOutboxService


logger = logging.getLogger(__name__)


@receiver(
    page_published,
    sender=BlogPage,
    dispatch_uid="search.record_blog_page_published",
)
def record_blog_page_published(sender: Any, instance: BlogPage, **kwargs: Any) -> None:
    """页面发布后补齐正式指针，并记录待投递的搜索事件。"""

    if settings.CONTENT_SEARCH_PRODUCER_ENABLED:
        revision = kwargs.get("revision")
        if (
            instance.live
            and revision is not None
            and getattr(revision, "pk", None) is not None
        ):
            # Wagtail 8.0 编辑发布绕过 BlogPage.publish；信号收到的 Revision 是唯一可信版本。
            BlogPublicationService.ensure_published_revision(instance.pk, revision.pk)
        event = ContentSearchOutboxService.record_publication(instance)
        logger.info(
            "content_search_publish_signal page_id=%s event_id=%s",
            instance.pk,
            getattr(event, "event_id", None),
        )


@receiver(
    page_unpublished,
    sender=BlogPage,
    dispatch_uid="search.record_blog_page_unpublished",
)
def record_blog_page_unpublished(sender: Any, instance: BlogPage, **kwargs: Any) -> None:
    """取消发布后记录不可搜索的墓碑事件。"""

    if settings.CONTENT_SEARCH_PRODUCER_ENABLED:
        event = ContentSearchOutboxService.record_unpublish(instance)
        logger.info(
            "content_search_unpublish_signal page_id=%s event_id=%s",
            instance.pk,
            getattr(event, "event_id", None),
        )


@receiver(
    post_save,
    sender=PageViewRestriction,
    dispatch_uid="search.request_scope_recalculation_on_restriction_saved",
)
@receiver(
    pre_delete,
    sender=PageViewRestriction,
    dispatch_uid="search.request_scope_recalculation_on_restriction_deleted",
)
def request_scope_recalculation_on_restriction_change(
    sender: Any,
    instance: PageViewRestriction,
    **kwargs: Any,
) -> None:
    """权限范围变化后创建重算任务，避免在信号中递归扫描整棵页面树。"""

    if settings.CONTENT_SEARCH_PRODUCER_ENABLED:
        ContentSearchOutboxService.request_scope_recalculation(instance.page_id)
