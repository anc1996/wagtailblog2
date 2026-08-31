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
from search.models import ContentSearchOperation, ContentSearchState
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
            not kwargs.get("alias")
            and revision is not None
            and getattr(revision, "pk", None) is not None
        ):
            # Wagtail 8.0 的发布信号在不同动作中 live 状态更新时机不同；Revision 才是唯一可信正文版本。
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
        # Wagtail 8.0 后台 UnpublishAction 绕过 BlogPage.unpublish；在统一信号中推进代次并清理正式指针。
        BlogPublicationService.advance_unpublish_generation(instance.pk)
        BlogPublicationService.clear_published_pointer(instance.pk)
        event = ContentSearchOutboxService.record_unpublish(instance)
        logger.info(
            "content_search_unpublish_signal page_id=%s event_id=%s",
            instance.pk,
            getattr(event, "event_id", None),
        )


@receiver(
    pre_delete,
    sender=BlogPage,
    dispatch_uid="search.record_blog_page_deleted",
)
def record_blog_page_deleted(sender: Any, instance: BlogPage, **kwargs: Any) -> None:
    """后台 DeletePageAction 绕过页面子类方法时，仍登记递增代次的删除墓碑。"""

    if settings.CONTENT_SEARCH_PRODUCER_ENABLED:
        # Django Collector 可能对同一实例发出多次 pre_delete；实例标记保证墓碑只写一次。
        if getattr(instance, "_search_delete_recorded", False):
            return
        instance._search_delete_recorded = True
        state = ContentSearchState.objects.filter(page_id=instance.pk).first()
        if state is None or state.desired_operation != ContentSearchOperation.TOMBSTONE:
            BlogPublicationService.advance_unpublish_generation(instance.pk)
        ContentSearchOutboxService.record_delete(instance)
        logger.info("content_search_delete_signal page_id=%s", instance.pk)


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
