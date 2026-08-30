import logging
from typing import Any

from celery import shared_task
from django.conf import settings

from search.services.delivery import (
    due_content_search_delivery_ids,
    process_content_search_delivery,
)


logger = logging.getLogger(__name__)


def _maintenance_queue() -> str:
	"""返回当前环境的维护队列名称，拒绝空值回退到生产默认名称。"""

	queue = getattr(settings, "CELERY_MAINTENANCE_QUEUE", "maintenance")
	return queue.strip() if isinstance(queue, str) and queue.strip() else "maintenance"


@shared_task(name="search.tasks.wake_content_search_delivery", ignore_result=True)
def wake_content_search_delivery(event_id: Any = None) -> int:
    """提交后快速唤醒 dispatcher；数据库状态仍是唯一可靠的任务来源。"""

    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        logger.info("content_search_wakeup_deferred event_id=%s", event_id)
        return 0

    if isinstance(event_id, str) and event_id.startswith("scope:"):
        try:
            job_id = int(event_id.split(":", 1)[1])
        except (TypeError, ValueError):
            logger.error("content_search_scope_wakeup_invalid event_id=%s", event_id)
            return 0
        return 1 if consume_content_search_scope_job.apply_async(
            args=(job_id,), queue=_maintenance_queue()
        ) else 0

    return dispatch_pending_content_search_deliveries()


@shared_task(name="search.tasks.consume_content_search_delivery", ignore_result=True)
def consume_content_search_delivery(delivery_id: int) -> Any:
    """maintenance Worker 入口：领取一个租约并以至少一次语义完成索引投递。"""

    return process_content_search_delivery(delivery_id)


@shared_task(name="search.tasks.dispatch_pending_content_search_deliveries", ignore_result=True)
def dispatch_pending_content_search_deliveries(limit: int | None = None) -> int:
    """Beat 补偿待处理和过期租约 Delivery；broker 失败不会改变持久化状态。"""

    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        return 0

    delivery_ids = due_content_search_delivery_ids(limit=limit)
    dispatched_count = 0
    for delivery_id in delivery_ids:
        try:
            consume_content_search_delivery.apply_async(
                args=(delivery_id,),
                queue=_maintenance_queue(),
            )
            dispatched_count += 1
        except Exception:
            logger.error(
                "content_search_delivery_dispatch_failed delivery_id=%s",
                delivery_id,
            )
    return dispatched_count


@shared_task(name="search.tasks.consume_content_search_scope_job", ignore_result=True)
def consume_content_search_scope_job(job_id: int) -> str:
    """maintenance Worker 入口：处理权限范围任务的一个可恢复批次。"""

    # 延迟导入避免 outbox -> tasks -> scope -> outbox 的启动期循环依赖。
    from search.services.scope import process_scope_job

    return process_scope_job(int(job_id))


@shared_task(name="search.tasks.dispatch_pending_content_search_scope_jobs", ignore_result=True)
def dispatch_pending_content_search_scope_jobs(limit: int | None = None) -> int:
    """派发待处理范围任务；租约和检查点由消费者负责最终一致性。"""

    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        return 0
    dispatched_count = 0
    # 延迟导入避免 Django app ready 阶段加载 ScopeJob 服务。
    from search.services.scope import due_scope_job_ids

    for job_id in due_scope_job_ids(limit=limit):
        try:
            consume_content_search_scope_job.apply_async(
                args=(job_id,), queue=_maintenance_queue()
            )
            dispatched_count += 1
        except Exception:
            logger.error("content_search_scope_dispatch_failed job_id=%s", job_id)
    return dispatched_count
