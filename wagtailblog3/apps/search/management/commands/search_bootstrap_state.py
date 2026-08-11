"""按游标初始化缺失的公开内容搜索 State。"""

import json
import os

from django.core.management.base import BaseCommand, CommandError

from search.models import ContentSearchTarget
from search.services.bootstrap import bootstrap_content_search_states


class Command(BaseCommand):
    """默认 dry-run；写入必须确认单一目标和有限批次。"""

    help = "初始化公开 BlogPage 缺失的内容搜索 State，默认只读预演"

    def add_arguments(self, parser):
        parser.add_argument("--target", required=True, help="精确目标 ID，仅用于记录本次初始化范围。")
        parser.add_argument("--after-page-id", type=int, default=0)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="确认写入缺失 State；省略时仅 dry-run。",
        )

    def handle(self, *args, **options):
        target = ContentSearchTarget.objects.filter(target_id=options["target"]).first()
        if target is None:
            raise CommandError("未找到指定的内容搜索目标")
        limit = max(1, min(options["limit"], 500))
        after_page_id = max(0, options["after_page_id"])
        dry_run = not options["confirm"]
        result = bootstrap_content_search_states(
            after_page_id=after_page_id,
            limit=limit,
            dry_run=dry_run,
        )
        report = {
            "environment": os.environ.get("WAGTAILBLOG_ENV", "unset"),
            "dry_run": dry_run,
            "target_id": target.target_id,
            "connection_name": target.connection_name,
            "index_name": target.index_name,
            "result": result,
        }
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
