"""以旧搜索为基准，读取独立内容索引并输出脱敏差异报告。"""

from __future__ import annotations

import hashlib
import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from search.core import SearchUnavailableError, perform_search
from blog.models import BlogPage
from search.models import ContentSearchTarget
from search.services.content_index import validate_content_index_name
from search.services.content_query import query_content_search_page
from search.services.elasticsearch import ContentSearchElasticsearchError
from search.services.shadow import classify_shadow_difference


class Command(BaseCommand):
    help = "只读比较旧博客搜索和独立内容索引，不输出查询原文或正文"

    def add_arguments(self, parser):
        parser.add_argument("--target", required=True, help="精确 ContentSearchTarget.target_id")
        parser.add_argument("--query", required=True, help="待比较的查询，仅用于本次进程")
        parser.add_argument("--size", type=int, default=10, help="比较的最大结果数，范围 1-100")
        parser.add_argument(
            "--field-presence",
            action="store_true",
            help="仅报告标题、简介和正式正文是否包含查询文本，不输出受保护内容。",
        )

    @staticmethod
    def _field_presence(page_ids, query_string):
        query_text = query_string.casefold()
        pages = BlogPage.objects.live().public().in_bulk(page_ids)
        result = {}
        for page_id in page_ids:
            page = pages.get(page_id)
            if page is None:
                continue
            formal_content = page.get_content_from_mongodb()
            body_text = page.get_full_text_for_search(content=formal_content) if formal_content is not None else ""
            fields = {
                "title": query_text in str(page.title or "").casefold(),
                "intro": query_text in str(page.intro or "").casefold(),
                "body_text": query_text in body_text.casefold(),
            }
            result[str(page_id)] = fields
        return result

    def handle(self, *args, **options):
        target = ContentSearchTarget.objects.filter(
            target_id=options["target"],
            connection_name=settings.CONTENT_SEARCH_CONNECTION_NAME,
            enabled=True,
        ).first()
        if target is None:
            raise CommandError("content_target_not_found_or_disabled")
        try:
            validate_content_index_name(target.index_name, settings.CONTENT_SEARCH_INDEX_PREFIX)
        except ValueError as error:
            raise CommandError("content_target_index_invalid") from error
        size = min(max(options["size"], 1), 100)
        query_string = options["query"]
        query_hash = hashlib.sha256(query_string.encode("utf-8")).hexdigest()
        started = time.perf_counter()
        try:
            old_results = perform_search(query_string, "blog")
            old_page_ids = tuple(item.pk for item in old_results[:size])
        except SearchUnavailableError as error:
            raise CommandError("legacy_search_unavailable") from error
        legacy_latency_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        try:
            content_page = query_content_search_page(
                target,
                query_string,
                size=size,
                index_name=target.index_name,
            )
        except ContentSearchElasticsearchError as error:
            raise CommandError(error.code) from error
        content_latency_ms = (time.perf_counter() - started) * 1000
        report = {
            "read_only": True,
            "target_id": target.target_id,
            "index_name": target.index_name,
            "query_hash": query_hash,
            "size": size,
            "legacy_page_ids": list(old_page_ids),
            "content_page_ids": list(content_page.page_ids),
            "difference": classify_shadow_difference(old_page_ids, content_page.page_ids),
            "legacy_latency_ms": round(legacy_latency_ms, 2),
            "content_latency_ms": round(content_latency_ms, 2),
            "content_es_took_ms": content_page.took_ms,
        }
        if options["field_presence"]:
            compared_page_ids = tuple(dict.fromkeys(old_page_ids + content_page.page_ids))
            report["field_presence"] = self._field_presence(compared_page_ids, query_string)
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))
