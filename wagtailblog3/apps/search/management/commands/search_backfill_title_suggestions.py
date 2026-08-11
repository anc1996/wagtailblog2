"""把测试环境公开 BlogPage 标题回填到独立标题建议索引。"""

from __future__ import annotations

import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from blog.models import BlogPage
from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import _response_body, get_content_search_client_for_connection
from search.services.title_index import default_title_suggestion_index_name


class Command(BaseCommand):
    help = "回填测试环境公开 BlogPage 标题到独立建议索引，默认仅输出 dry-run"

    def add_arguments(self, parser):
        parser.add_argument("--index-name")
        parser.add_argument("--batch-size", type=int, default=200)
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        environment = os.environ.get("WAGTAILBLOG_ENV", "unset")
        index_name = options["index_name"] or default_title_suggestion_index_name(
            settings.CONTENT_SEARCH_INDEX_PREFIX
        )
        try:
            validate_content_index_name(index_name, settings.CONTENT_SEARCH_INDEX_PREFIX)
        except ValueError as error:
            raise CommandError(str(error)) from error
        batch_size = max(1, min(int(options["batch_size"]), 1000))
        pages = BlogPage.objects.live().public().order_by("pk")
        total = pages.count()
        report = {
            "environment": environment,
            "dry_run": not options["confirm"],
            "index_name": index_name,
            "alias": settings.CONTENT_SEARCH_TITLE_SUGGESTIONS_READ_ALIAS,
            "public_pages": total,
            "written": 0,
            "failed": 0,
            "alias_changed": False,
        }
        if not options["confirm"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return
        if environment != "test":
            report["refused"] = "test_environment_required"
            self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
            raise CommandError("标题建议索引回填仅允许测试环境")
        if "test" not in settings.CONTENT_SEARCH_INDEX_PREFIX.split("-"):
            raise CommandError("测试环境内容索引前缀必须包含独立 test 标识")

        client = get_content_search_client_for_connection(settings.CONTENT_SEARCH_CONNECTION_NAME)
        for start in range(0, total, batch_size):
            batch = list(pages[start:start + batch_size])
            operations = []
            for page in batch:
                operations.extend(
                    [
                        {"index": {"_index": index_name, "_id": str(page.pk)}},
                        {
                            "page_id": page.pk,
                            "title": page.title,
                            "locale_id": page.locale_id,
                            "searchable": True,
                            "popularity": 0,
                        },
                    ]
                )
            try:
                response = client.bulk(operations=operations, refresh=False)
                body = _response_body(response)
            except Exception as error:
                raise CommandError("标题建议索引 Bulk 回填失败") from error
            items = body.get("items", []) if isinstance(body, dict) else []
            for item in items:
                result = item.get("index", {}) if isinstance(item, dict) else {}
                if 200 <= int(result.get("status", 500)) < 300:
                    report["written"] += 1
                else:
                    report["failed"] += 1
            if report["failed"]:
                raise CommandError("标题建议索引存在 Bulk 失败项，已停止回填")
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
