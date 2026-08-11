"""在明确授权后重新排队单个内容搜索 Delivery。"""

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from search.models import ContentSearchDelivery, ContentSearchStatus
from search.tasks import wake_content_search_delivery


class Command(BaseCommand):
    """只允许精确重放一个非终态成功的 Delivery，避免批量覆盖索引状态。"""

    help = "在 --confirm 后重新排队一个指定的内容搜索 Delivery"

    def add_arguments(self, parser):
        parser.add_argument("event_id", help="精确的 ContentSearchOutbox.event_id UUID。")
        parser.add_argument("target_id", help="精确的 ContentSearchTarget.target_id。")
        parser.add_argument("--reason", required=True, help="记录本次人工重放的简短原因。")
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="确认修改该 Delivery 的持久化状态。",
        )

    def handle(self, *args, **options):
        event_id = options["event_id"]
        target_id = options["target_id"]
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        delivery = ContentSearchDelivery.objects.select_related("event", "target").filter(
            event__event_id=event_id,
            target__target_id=target_id,
        ).first()
        if delivery is None:
            raise CommandError("未找到指定事件和目标对应的内容搜索 Delivery")

        self.stdout.write(
            "environment={environment} delivery_id={delivery_id} event_id={event_id} "
            "target_id={target_id} index_name={index_name} status={status}".format(
                environment=environment,
                delivery_id=delivery.pk,
                event_id=event_id,
                target_id=delivery.target.target_id,
                index_name=delivery.target.index_name,
                status=delivery.status,
            )
        )
        if not options["confirm"]:
            raise CommandError("拒绝写入：必须显式提供 --confirm")
        if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
            raise CommandError("拒绝重放：CONTENT_SEARCH_CONSUMER_ENABLED 当前为 false")
        if delivery.status in {
            ContentSearchStatus.SUCCEEDED,
            ContentSearchStatus.SUPERSEDED,
        }:
            raise CommandError("拒绝重放：已完成或已过期的 Delivery 不能人工复活")

        with transaction.atomic():
            delivery = (
                ContentSearchDelivery.objects.select_for_update()
                .select_related("event", "target")
                .get(pk=delivery.pk)
            )
            if (
                delivery.status == ContentSearchStatus.PROCESSING
                and delivery.lock_expires_at
                and delivery.lock_expires_at > timezone.now()
            ):
                raise CommandError("拒绝重放：Delivery 仍由有效租约处理")

            delivery.status = ContentSearchStatus.RETRY
            delivery.available_at = timezone.now()
            delivery.locked_by = ""
            delivery.lock_expires_at = None
            delivery.last_error_code = "manual_replay"
            # 原因只作为命令审计输入，不落库，避免自由文本意外携带正文或凭据。
            delivery.last_error_message = ""
            delivery.completed_at = None
            delivery.save(
                update_fields=(
                    "status",
                    "available_at",
                    "locked_by",
                    "lock_expires_at",
                    "last_error_code",
                    "last_error_message",
                    "completed_at",
                    "updated_at",
                )
            )
            transaction.on_commit(
                lambda: wake_content_search_delivery.apply_async(
                    kwargs={"event_id": str(delivery.event.event_id)},
                    queue="maintenance",
                )
            )

        self.stdout.write(self.style.SUCCESS("已重新排队指定 Delivery"))
