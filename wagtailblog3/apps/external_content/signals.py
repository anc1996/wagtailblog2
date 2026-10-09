"""
Wagtail Page 正文外置通用协议 - 生命周期信号接收器。

监听 Wagtail 原生 page_published、page_unpublished 以及 Django pre_delete、post_delete 信号：
1. 页面发布：为后台 PublishPageRevisionAction 与 UnpublishAction 提供统一的保底与幂等接入，并自动同步别名页面；
2. 页面下架：递增公开代次并清空正式指针；
3. 页面删除：全自动捕获任何入口（原生 delete、后台 Action、父页级联、QuerySet批量）的删除动作，
   确保在 MySQL 物理抹除前固化 ExternalContentDeletionIntent，在抹除后标记为 MYSQL_DELETED，
   彻底保障 MongoDB 不可变版本完好保留并进入安全延迟回收期。
"""

import logging
from typing import Any

from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver
from wagtail.signals import page_published, page_unpublished

from external_content.mixins import ExternalContentPageMixin
from external_content.exceptions import ExternalContentOperationBlockedError
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)
from external_content.publication import ExternalContentPublicationService

logger = logging.getLogger(__name__)

# 导入删除上下文，用于在 pre_delete 中标记删除流程，避免 unpublish 自阻断
from external_content.deletion import _deletion_context


@receiver(page_published, dispatch_uid="external_content.record_page_published")
def handle_external_content_page_published(sender: Any, instance: Any, **kwargs: Any) -> None:
    """
    页面发布信号响应。
    当后台 PublishPageRevisionAction 绕过 page.publish() 时，
    在信号层补齐正式指针推进与公开代次递增（具备严格幂等性）。
    仅在页面真正处于 live 状态时推进，排除未来排期尚未上线的场景。
    并在发布完成后自动同步所有关联别名页面。
    """
    if not isinstance(instance, ExternalContentPageMixin):
        return

    # 别名 (alias) 派生由专用 G4 流程编排，不在此处作为源页直接触发
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

        # 同步别名页面
        from external_content.copying import sync_external_content_aliases
        sync_external_content_aliases(instance, revision=revision)
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


@receiver(pre_delete, dispatch_uid="external_content.pre_delete_guard")
def handle_external_content_pre_delete(sender: Any, instance: Any, **kwargs: Any) -> None:
    """
    底层预删除保护：
    在 MySQL 记录与关联 Revision 被级联或直接删除前，
    自动固化外置正文删除意图清单（若尚未登记），
    保证哪怕通过父页面删除、QuerySet.delete() 或管理后台 DeleteAction，
    所有不可变正文版本 ID 均被全量捕获冻结。

    修复：登记意图后设置删除上下文标志，使 Wagtail 触发的 unpublish 跳过写入检查，
    避免本次删除意图被误判为外部并发写入而自阻断。
    """
    if not isinstance(instance, ExternalContentPageMixin):
        return

    if not instance.pk:
        return

    from external_content.deletion import _deletion_failure_stage, record_deletion_intent

    with _deletion_failure_stage("pre-delete", logger):
        # 缺少 Collector 隔离窗口时必须先拒绝，不能独立提交 PENDING 后再报错。
        if not hasattr(_deletion_context, "deleting_page_ids"):
            raise ExternalContentOperationBlockedError("删除缺少隔离的 Collector 授权窗口")
        record_deletion_intent(instance)
        # 意图登记和状态核验成功后才逐页授权，Collector 退出负责整批精确恢复。
        _deletion_context.deleting_page_ids.add(instance.pk)


@receiver(post_delete, dispatch_uid="external_content.post_delete_status_update")
def handle_external_content_post_delete(sender: Any, instance: Any, **kwargs: Any) -> None:
    """
    底层后删除确认：
    在 MySQL 物理抹除后，将删除意图状态置为 MYSQL_DELETED。
    重要架构契约：
    - PublicationState 必须作为审计日志保留，严禁在此处删除；
    - PublicationState 记录了页面的发布历史，即使页面被删除也需要保留用于审计。
    - 删除意图的 manifest 中引用了这些历史信息，GC 时需要读取。

    确保绝不触碰 MongoDB 正文。

    清理删除上下文标志，避免影响后续请求。
    """
    if not isinstance(instance, ExternalContentPageMixin):
        return

    if not instance.pk:
        return

    # 检查是否是受控安全删除流程
    # 受控删除流程由 execute_safe_page_deletion() 管理，不需要这里的自动处理
    if getattr(instance, "_controlled_safe_deletion_in_progress", False):
        return

    from external_content.deletion import _deletion_failure_stage

    # 异常诊断只记固定阶段，不能带入正文、身份、原异常或 traceback。
    with _deletion_failure_stage("post-delete", logger):
        # ❌ 不删除 PublicationState！它是审计日志，必须保留。
        # ExternalContentPublicationState.objects.filter(page_id=instance.pk).delete()

        # 将删除意图标记为 MYSQL_DELETED
        intent = ExternalContentDeletionIntent.objects.filter(
            page_id=instance.pk,
            status=DeletionIntentStatus.PENDING,
        ).first()
        if intent is None:
            raise ExternalContentOperationBlockedError("后删除缺少已核验的 PENDING 意图")
        intent.status = DeletionIntentStatus.MYSQL_DELETED
        with _deletion_failure_stage("intent-status-save", logger):
            intent.save(update_fields=["status", "updated_at"])
        logger.info("external_content_auto_deletion_intent_mysql_deleted intent_id=%s page_id=%s", intent.pk, instance.pk)
