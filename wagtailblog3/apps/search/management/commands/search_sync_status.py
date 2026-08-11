"""输出内容搜索同步状态的只读摘要。"""

import json
import os

from django.core.management.base import BaseCommand, CommandError

from search.models import ContentSearchTarget
from search.services.consistency import get_content_search_sync_status


class Command(BaseCommand):
    """只读查询 Delivery 状态，不访问正文、Mongo 或 Elasticsearch。"""

    help = "输出内容搜索各目标的同步状态 JSON 摘要"

    def add_arguments(self, parser):
        parser.add_argument("--target", help="精确目标 ID；省略时列出全部已登记目标。")

    def handle(self, *args, **options):
        targets = ContentSearchTarget.objects.order_by("target_id")
        if options["target"]:
            targets = targets.filter(target_id=options["target"])
            if not targets.exists():
                raise CommandError("未找到指定的内容搜索目标")
        report = {
            "environment": os.environ.get("WAGTAILBLOG_ENV", "unset"),
            "read_only": True,
            "targets": get_content_search_sync_status(targets),
        }
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
