import logging

from celery import shared_task
from django.conf import settings

from search.services.delivery import (
    due_content_search_delivery_ids,
    process_content_search_delivery,
)


logger = logging.getLogger(__name__)


@shared_task(name="search.tasks.wake_content_search_delivery", ignore_result=True)
def wake_content_search_delivery(event_id=None):
    """提交后快速唤醒 dispatcher；数据库状态仍是唯一可靠的任务来源。"""

    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        logger.info("content_search_wakeup_deferred event_id=%s", event_id)
        return 0

    return dispatch_pending_content_search_deliveries()


@shared_task(name="search.tasks.consume_content_search_delivery", ignore_result=True)
def consume_content_search_delivery(delivery_id):
    """maintenance Worker 入口：领取一个租约并以至少一次语义完成索引投递。"""

    return process_content_search_delivery(delivery_id)


@shared_task(name="search.tasks.dispatch_pending_content_search_deliveries", ignore_result=True)
def dispatch_pending_content_search_deliveries(limit=None):
    """Beat 补偿待处理和过期租约 Delivery；broker 失败不会改变持久化状态。"""

    if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
        return 0

    delivery_ids = due_content_search_delivery_ids(limit=limit)
    dispatched_count = 0
    for delivery_id in delivery_ids:
        try:
            consume_content_search_delivery.apply_async(
                args=(delivery_id,),
                queue="maintenance",
            )
            dispatched_count += 1
        except Exception:
            logger.error(
                "content_search_delivery_dispatch_failed delivery_id=%s",
                delivery_id,
            )
    return dispatched_count
