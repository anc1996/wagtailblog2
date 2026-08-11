"""创建测试环境独立标题建议索引，不自动回填或切换 alias。"""

from __future__ import annotations

import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from search.services.title_index import (
    create_title_suggestion_index,
    default_title_suggestion_index_name,
)
from search.services.elasticsearch import ContentSearchElasticsearchError


class Command(BaseCommand):
    help = "创建测试环境独立标题建议索引，默认仅输出 dry-run"

    def add_arguments(self, parser):
        parser.add_argument("--index-name")
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        index_name = options["index_name"] or default_title_suggestion_index_name(
            settings.CONTENT_SEARCH_INDEX_PREFIX
        )
        report = {
            "environment": environment,
            "dry_run": not options["confirm"],
            "index_name": index_name,
            "alias": settings.CONTENT_SEARCH_TITLE_SUGGESTIONS_READ_ALIAS,
            "alias_changed": False,
            "backfill": False,
        }
        if not options["confirm"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return
        if environment != "test":
            report["refused"] = "test_environment_required"
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("标题建议索引创建仅允许测试环境")
        if "test" not in settings.CONTENT_SEARCH_INDEX_PREFIX.split("-"):
            raise CommandError("测试环境内容索引前缀必须包含独立 test 标识")
        try:
            result = create_title_suggestion_index(
                settings.CONTENT_SEARCH_CONNECTION_NAME,
                index_name,
            )
        except ContentSearchElasticsearchError as error:
            report["error"] = {"code": error.code, "retryable": error.retryable}
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("标题建议索引创建失败") from error
        report["index_created"] = result.index_created
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
