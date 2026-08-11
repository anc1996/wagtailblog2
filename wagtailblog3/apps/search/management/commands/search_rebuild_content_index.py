"""在测试环境在线回填独立内容索引，并检查增量双投递是否追平。"""

from __future__ import annotations

import json
import os
import re

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from search.models import ContentSearchTarget
from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import ContentSearchElasticsearchError
from search.services.rebuild import (
    ContentSearchRebuildError,
    content_search_build_report,
    get_content_search_build_gate,
    rebuild_content_search_index,
    start_content_search_build,
)


_TARGET_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")


class Command(BaseCommand):
    """确认执行只允许测试环境的精确物理目标，默认只输出计划。"""

    help = "在线回填测试环境的独立内容索引，默认仅输出预演计划"

    def add_arguments(self, parser):
        parser.add_argument("--target", required=True, help="精确的 ContentSearchTarget.target_id。")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help="每次从 MySQL 游标读取的公开页面数，默认使用配置值。",
        )
        parser.add_argument(
            "--max-batch-bytes",
            type=int,
            default=None,
            help="每批 Bulk 请求的 UTF-8 字节上限，默认使用配置值。",
        )
        parser.add_argument(
            "--max-batches",
            type=int,
            default=None,
            help="最多处理多少个 MySQL 批次；省略时持续到扫描上界。",
        )
        parser.add_argument(
            "--resume-build",
            action="store_true",
            help="允许从 failed/backfilling 状态的 checkpoint 继续。",
        )
        parser.add_argument(
            "--check-catch-up",
            action="store_true",
            help="只执行一次 Delivery 和公开文档追平检查，不重新扫描页面。",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--confirm",
            action="store_true",
            help="确认写入测试 MySQL、启用 building target 并写入测试 ES。",
        )
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="只读取目标和构建状态，不修改 MySQL、Mongo 或 Elasticsearch。",
        )

    def _load_target(self, target_id):
        if not _TARGET_ID_PATTERN.fullmatch(target_id):
            raise CommandError("目标 ID 必须为精确的 slug，不能包含通配符或空白")
        target = ContentSearchTarget.objects.filter(target_id=target_id).first()
        if target is None:
            raise CommandError("未找到指定的内容搜索目标")
        try:
            validate_content_index_name(target.index_name, settings.CONTENT_SEARCH_INDEX_PREFIX)
        except ValueError as error:
            raise CommandError("目标不是当前环境下的精确物理索引") from error
        return target

    def _base_report(self, target, confirm):
        build = target.builds.order_by("-pk").first()
        report = {
            "environment": os.environ.get("WAGTAILBLOG_ENV", "unset"),
            "dry_run": not confirm,
            "target_id": target.target_id,
            "connection_name": target.connection_name,
            "index_name": target.index_name,
            "target_role": target.role,
            "target_enabled": target.enabled,
            "producer_enabled": settings.CONTENT_SEARCH_PRODUCER_ENABLED,
            "consumer_enabled": settings.CONTENT_SEARCH_CONSUMER_ENABLED,
        }
        if build is not None:
            report["build"] = content_search_build_report(build)
        return report

    def handle(self, *args, **options):
        target = self._load_target(options["target"])
        confirm = bool(options["confirm"])
        report = self._base_report(target, confirm)
        if not confirm:
            if options["check_catch_up"]:
                report["gate"] = get_content_search_build_gate(target.target_id, mutate=False)
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return

        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        if environment != "test":
            report["refused"] = "test_environment_required"
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("WP4B 当前只允许在 WAGTAILBLOG_ENV=test 执行")
        if "test" not in settings.CONTENT_SEARCH_INDEX_PREFIX.split("-"):
            report["refused"] = "test_index_prefix_required"
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("测试环境内容索引前缀必须包含独立 test 标识")
        if not settings.CONTENT_SEARCH_PRODUCER_ENABLED:
            report["refused"] = "content_producer_required"
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("在线双投递要求 CONTENT_SEARCH_PRODUCER_ENABLED=true")
        if not settings.CONTENT_SEARCH_CONSUMER_ENABLED:
            report["refused"] = "content_consumer_required"
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("在线双投递要求 CONTENT_SEARCH_CONSUMER_ENABLED=true")

        try:
            if options["check_catch_up"]:
                report["gate"] = get_content_search_build_gate(target.target_id, mutate=True)
            else:
                start_content_search_build(
                    target.target_id,
                    resume=options["resume_build"],
                )
                _build, batches, last_batch = rebuild_content_search_index(
                    target.target_id,
                    batch_size=options["batch_size"] or settings.CONTENT_SEARCH_REBUILD_BATCH_SIZE,
                    max_batch_bytes=options["max_batch_bytes"]
                    or settings.CONTENT_SEARCH_REBUILD_MAX_BATCH_BYTES,
                    max_batches=options["max_batches"],
                )
                report["batches_processed"] = batches
                report["last_batch"] = {
                    "done": last_batch.done,
                    "checkpoint_page_id": last_batch.checkpoint_page_id,
                    "scanned": last_batch.scanned,
                    "succeeded": last_batch.succeeded,
                    "superseded": last_batch.superseded,
                    "batch_count": last_batch.batch_count,
                    "batch_bytes": last_batch.batch_bytes,
                }
            target.refresh_from_db()
            report.update(self._base_report(target, confirm))
            if options["check_catch_up"]:
                report["gate"] = get_content_search_build_gate(target.target_id, mutate=False)
        except (ContentSearchRebuildError, ContentSearchElasticsearchError) as error:
            report["error"] = {
                "code": getattr(error, "code", "content_search_rebuild_failed"),
                "retryable": getattr(error, "retryable", False),
            }
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("独立内容索引在线回填失败") from error

        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
