"""生成 WP0 使用的脱敏中文搜索评测集。"""

import json

from django.core.management.base import BaseCommand, CommandError


QUERY_TEMPLATES = (
    ("标题主题", "title"),
    ("正文概念", "body_text"),
    ("分类筛选", "categories"),
    ("标签术语", "tags"),
    ("发布日期", "date"),
)


class Command(BaseCommand):
    """只输出合成评测数据，人工标注后才能用于真实效果计算。"""

    help = "输出 100 至 500 条脱敏中文搜索评测样本 JSON"

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=100,
            help="样本数量，范围为 100 至 500。",
        )

    def handle(self, *args, **options):
        count = options["count"]
        if not 100 <= count <= 500:
            raise CommandError("--count 必须位于 100 至 500 之间")

        cases = []
        for position in range(count):
            topic, expected_field = QUERY_TEMPLATES[position % len(QUERY_TEMPLATES)]
            case_id = f"synthetic-{position + 1:03d}"
            cases.append(
                {
                    "id": case_id,
                    "query": f"{topic}{position // len(QUERY_TEMPLATES) + 1}",
                    "expected_fields": [expected_field],
                    "expected_top_10": [],
                    "judgement_status": "pending",
                }
            )

        self.stdout.write(
            json.dumps(
                {
                    "schema": "search-evaluation-fixture-v1",
                    "synthetic": True,
                    "case_count": count,
                    "cases": cases,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
