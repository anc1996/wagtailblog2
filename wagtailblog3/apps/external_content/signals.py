"""
Wagtail Page 正文外置通用协议 - 生命周期信号接收器。

监听 Wagtail 原生 page_published 与 page_unpublished 信号，
为后台 PublishPageRevisionAction 与 UnpublishAction 提供统一的保底与幂等接入。
独立于搜索生产开关执行，确保正式指针与公开代次的绝对权威。
"""

import logging
from typing import Any

from django.dispatch import receiver
from wagtail.signals import page_published, page_unpublished

from external_content.mixins import ExternalContentPageMixin
from external_content.publication import ExternalContentPublicationService

logger = logging.getLogger(__name__)


@receiver(page_published, dispatch_uid="external_content.record_page_published")
def handle_external_content_page_published(sender: Any, instance: Any, **kwargs: Any) -> None:
    """
    页面发布信号响应。

    当后台 PublishPageRevisionAction 绕过 page.publish() 时，
    在信号层补齐正式指针推进与公开代次递增（具备严格幂等性）。
    仅在页面真正处于 live 状态时推进，排除未来排期尚未上线的场景。
    """
    if not isinstance(instance, ExternalContentPageMixin):
        return

    # 别名 (alias) 派生由专用 G4 流程编排，不在此处作为源页直接覆盖
    if kwargs.get("alias"):
        return

    # 若页面尚未处于上线状态（如排期至未来），不推进正式指针
    if not getattr(instance, "live", False):
        logger.info(
            "external_content_publish_signal_skipped_not_live page_id=%s",
            getattr(instance, "pk", None),
        )
        return

    revision = kwargs.get("revision")
    if revision is None or getattr(revision, "pk", None) is None:
        return

    try:
        ExternalContentPublicationService.ensure_published_revision(
            instance.pk,
            revision.pk,
        )
        logger.info(
            "external_content_publish_signal_ensured page_id=%s revision_id=%s",
            instance.pk,
            revision.pk,
        )
    except Exception as exc:
        logger.error(
            "external_content_publish_signal_failed page_id=%s revision_id=%s error=%s",
            instance.pk,
            getattr(revision, "pk", None),
            exc,
            exc_info=True,
        )
        raise


@receiver(page_unpublished, dispatch_uid="external_content.record_page_unpublished")
def handle_external_content_page_unpublished(sender: Any, instance: Any, **kwargs: Any) -> None:
    """
    页面下架信号响应。

    递增公开代次并清空正式正文指针。具备严格幂等性。
    """
    if not isinstance(instance, ExternalContentPageMixin):
        return

    # 若数据库中页面依然是 live=True（如 unpublish commit=False 场景），禁止推进代次与清空指针
    db_live = type(instance).objects.filter(pk=instance.pk).values_list("live", flat=True).first()
    if db_live:
        logger.info("external_content_unpublish_signal_skipped_db_still_live page_id=%s", instance.pk)
        return

    try:
        ExternalContentPublicationService.advance_unpublish_generation(instance.pk)
        logger.info("external_content_unpublish_signal_processed page_id=%s", instance.pk)
    except Exception as exc:
        logger.error(
            "external_content_unpublish_signal_failed page_id=%s error=%s",
            instance.pk,
            exc,
            exc_info=True,
        )
        raise
