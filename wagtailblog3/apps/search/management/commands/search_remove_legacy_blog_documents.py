"""从默认 Wagtail Page 索引移除已迁移的 BlogPage 文档。"""

from __future__ import annotations

import json
import os

from django.core.management.base import BaseCommand, CommandError
from wagtail.models import Page
from wagtail.search.backends import get_search_backend


class Command(BaseCommand):
    help = "从默认 Wagtail Page 索引删除已迁移的 BlogPage 文档"

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="确认只删除默认 Page 索引中的 BlogPage 文档",
        )
        parser.add_argument(
            "--backup-reference",
            help="本次备份目录或备份清单的引用；生产环境确认执行时必填",
        )

    def handle(self, *args, **options):
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        if options["confirm"] and environment not in {"test", "production"}:
            raise CommandError("旧 BlogPage 索引清理只允许 test 或 production 环境")
        if options["confirm"] and environment == "production" and not options["backup_reference"]:
            raise CommandError("生产清理必须提供 --backup-reference")

        backend = get_search_backend("default")
        index = backend.get_index_for_model(Page)
        client = getattr(backend, "es", None) or getattr(backend, "client", None)
        if client is None:
            raise CommandError("默认 Wagtail 搜索后端不提供 Elasticsearch 客户端")

        query = {"term": {"_django_content_type": "blog.BlogPage"}}
        try:
            count_response = client.count(index=index.name, body={"query": query})
            count = int(count_response.get("count", 0))
        except Exception as error:
            raise CommandError("读取旧 BlogPage 文档数量失败") from error

        report = {
            "environment": environment,
            "dry_run": not options["confirm"],
            "index_name": index.name,
            "document_type": "blog.BlogPage",
            "matched_count": count,
            "deleted_count": 0,
            "backup_reference": options["backup_reference"] or "",
        }
        if not options["confirm"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return

        try:
            result = client.delete_by_query(
                index=index.name,
                body={"query": query},
                conflicts="proceed",
                refresh=True,
                wait_for_completion=True,
            )
        except Exception as error:
            raise CommandError("删除旧 BlogPage 文档失败") from error

        report["deleted_count"] = int(result.get("deleted", 0))
        report["version_conflicts"] = int(result.get("version_conflicts", 0))
        report["failures"] = len(result.get("failures") or [])
        if report["failures"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("旧 BlogPage 文档清理存在失败项")
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
