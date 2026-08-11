"""计算已标注搜索评测结果的 WP0 指标。"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from search.evaluation import calculate_metrics


class Command(BaseCommand):
    """只读取人工标注和测试结果，不连接或修改搜索服务。"""

    help = "读取已标注的搜索评测 JSON 并输出 Recall、MRR、NDCG 和延迟分位数"

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="已标注评测 JSON 文件路径。")

    def handle(self, *args, **options):
        input_path = Path(options["input"])
        try:
            fixture = json.loads(input_path.read_text(encoding="utf-8"))
            result = calculate_metrics(fixture["cases"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CommandError(f"无法计算搜索评测指标: {type(error).__name__}") from error

        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
