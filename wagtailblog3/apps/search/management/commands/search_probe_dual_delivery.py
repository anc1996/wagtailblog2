"""在测试环境验证同一内容事件的多目标投递收敛。"""

from __future__ import annotations

import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from blog.models import BlogPage
from search.models import ContentSearchDelivery, ContentSearchTarget
from search.services.delivery import (
    due_content_search_delivery_ids,
    materialize_content_search_deliveries,
    process_content_search_delivery,
)
from search.services.outbox import ContentSearchOutboxService


class Command(BaseCommand):
    help = "为一个公开测试页面生成同内容事件，验证多目标 Delivery 收敛"

    def add_arguments(self, parser):
        parser.add_argument("--page-id", type=int, required=True, help="精确的公开 BlogPage ID")
        parser.add_argument("--confirm", action="store_true", help="确认创建测试 Outbox/Delivery 并同步消费")

    def handle(self, *args, **options):
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        page = BlogPage.objects.live().public().filter(pk=options["page_id"]).first()
        if page is None:
            raise CommandError("public_blog_page_not_found")
        targets = list(ContentSearchTarget.objects.filter(enabled=True).order_by("target_id"))
        report = {
            "environment": environment,
            "dry_run": not options["confirm"],
            "page_id": page.pk,
            "enabled_target_ids": [target.target_id for target in targets],
            "delivery_count": 0,
            "statuses": {},
        }
        if len(targets) < 2:
            raise CommandError("dual_enabled_targets_required")
        if not options["confirm"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return
        if environment != "test":
            raise CommandError("test_environment_required")
        if not settings.CONTENT_SEARCH_PRODUCER_ENABLED or not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
            raise CommandError("content_producer_and_consumer_required")
        if any("test" not in target.index_name.split("-") for target in targets):
            raise CommandError("test_index_prefix_required")

        event = ContentSearchOutboxService.record_publication(page)
        if event is None:
            raise CommandError("content_event_not_created")
        materialize_content_search_deliveries()
        delivery_ids = list(
            ContentSearchDelivery.objects.filter(event=event, target__enabled=True)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        for delivery_id in due_content_search_delivery_ids(limit=len(delivery_ids)):
            if delivery_id in delivery_ids:
                process_content_search_delivery(delivery_id)
        deliveries = list(
            ContentSearchDelivery.objects.filter(pk__in=delivery_ids).order_by("target__target_id")
        )
        report.update(
            {
                "event_id": str(event.event_id),
                "content_version": event.content_version,
                "delivery_count": len(deliveries),
                "statuses": {delivery.target.target_id: delivery.status for delivery in deliveries},
            }
        )
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
