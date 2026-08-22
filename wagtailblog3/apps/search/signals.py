from typing import Any

from django.conf import settings
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from wagtail.models import PageViewRestriction
from wagtail.signals import page_published, page_unpublished

from blog.models import BlogPage
from search.services.outbox import ContentSearchOutboxService


@receiver(
    page_published,
    sender=BlogPage,
    dispatch_uid="search.record_blog_page_published",
)
def record_blog_page_published(sender: Any, instance: BlogPage, **kwargs: Any) -> None:
    """Wagtail 已写入 live 状态后仅记录事务内事件，不直接访问 Elasticsearch。"""

    if settings.CONTENT_SEARCH_PRODUCER_ENABLED:
        ContentSearchOutboxService.record_publication(instance)


@receiver(
    page_unpublished,
    sender=BlogPage,
    dispatch_uid="search.record_blog_page_unpublished",
)
def record_blog_page_unpublished(sender: Any, instance: BlogPage, **kwargs: Any) -> None:
    """取消发布必须先产生墓碑事件，读取层仍由 public() 防御性过滤。"""

    if settings.CONTENT_SEARCH_PRODUCER_ENABLED:
        ContentSearchOutboxService.record_unpublish(instance)


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
    """限制变更只创建范围任务，后续 Worker 按游标处理，避免在 signal 中枚举子树。"""

    if settings.CONTENT_SEARCH_PRODUCER_ENABLED:
        ContentSearchOutboxService.request_scope_recalculation(instance.page_id)
from typing import Any
