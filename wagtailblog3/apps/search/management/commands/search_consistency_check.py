"""按游标执行独立内容索引的一致性只读检查。"""

import json
import os

from django.core.management.base import BaseCommand, CommandError

from search.models import ContentSearchTarget
from search.services.consistency import check_content_search_consistency
from search.services.elasticsearch import ContentSearchElasticsearchError


class Command(BaseCommand):
    """必须指定一个物理目标，拒绝通配或跨索引检查。"""

    help = "只读比较一个内容搜索目标的 State 与 Elasticsearch 文档"

    def add_arguments(self, parser):
        parser.add_argument("--target", required=True, help="精确的 ContentSearchTarget.target_id。")
        parser.add_argument("--after-page-id", type=int, default=0)
        parser.add_argument("--limit", type=int, default=1000)
        parser.add_argument("--sample-limit", type=int, default=20)
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Elasticsearch 不可读或响应无效时返回失败状态。",
        )

    def handle(self, *args, **options):
        target = ContentSearchTarget.objects.filter(target_id=options["target"]).first()
        if target is None:
            raise CommandError("未找到指定的内容搜索目标")
        report = {
            "environment": os.environ.get("WAGTAILBLOG_ENV", "unset"),
            "read_only": True,
            "target_id": target.target_id,
            "connection_name": target.connection_name,
            "index_name": target.index_name,
        }
        try:
            report["result"] = check_content_search_consistency(
                target,
                after_page_id=options["after_page_id"],
                limit=options["limit"],
                sample_limit=options["sample_limit"],
            )
        except ContentSearchElasticsearchError as error:
            report["error"] = {"code": error.code, "retryable": error.retryable}
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            if options["strict"]:
                raise CommandError("内容搜索一致性检查无法读取 Elasticsearch")
            return
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
