"""只读扫描 Mongo 正文孤儿数据。

命令把 Mongo 三个正文集合与 MySQL 页面、正文指针、删除意图及搜索事件交叉核对，
仅输出不含正文的诊断 JSON。默认及 ``--dry-run`` 均不会创建索引、写入数据库或删除文档。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pymongo
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from wagtail.models import Revision

from blog.models import (
    BlogPage,
    BlogPublicationState,
    MongoCleanupIntent,
    MongoCleanupIntentStatus,
    PageDeletionIntent,
    PageDeletionIntentStatus,
)


def _as_page_id(value: object) -> int | None:
    """将 Mongo/MySQL 中可能为字符串的页面编号规范化；无法解析时返回 ``None``。"""

    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _manifest_values(manifest: object, key: str) -> set[str]:
    """提取删除清单中的字符串指针，避免把任意 JSON 值当作引用。"""

    if not isinstance(manifest, dict):
        return set()
    values = manifest.get(key)
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if value}


class Command(BaseCommand):
    """生成 Mongo 正文孤儿候选清单；命令始终只读。"""

    help = "只读扫描 Mongo 正文孤儿数据（必须使用 --dry-run）"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="显式只读模式（默认行为）")
        parser.add_argument("--page-id", type=int, help="只报告指定页面编号（用于定向核验）")
        parser.add_argument("--limit", type=int, default=1000, help="最多输出候选行数，范围 1--5000")
        parser.add_argument("--batch-size", type=int, default=500, help="Mongo 游标批大小，范围 50--5000")
        parser.add_argument("--apply", action="store_true", help="已禁用；孤儿清理须另行设计并授权")

    def handle(self, *args: object, **options: object) -> None:
        if options.get("apply"):
            raise CommandError("orphan_report 仅支持只读扫描，不提供 --apply")
        limit = max(1, min(int(options.get("limit") or 1000), 5000))
        batch_size = max(50, min(int(options.get("batch_size") or 500), 5000))
        page_filter = options.get("page_id")
        context = self._collect_mysql_context()
        report = self._scan_mongo(context, limit=limit, batch_size=batch_size, page_filter=page_filter)
        report.update({"read_only": True, "dry_run": True, "page_filter": page_filter})
        self.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True))

    def _collect_mysql_context(self) -> dict[str, Any]:
        """读取所有保护性引用并构建集合；只查询 ID/状态，不读取页面正文。"""

        page_ids = {int(value) for value in BlogPage.objects.values_list("pk", flat=True)}
        modern_refs: set[str] = set()
        for row in BlogPublicationState.objects.values_list(
            "page_id", "draft_body_version_id", "published_body_version_id", "approved_body_version_id"
        ):
            modern_refs.update(str(value) for value in row[1:] if value)

        legacy_refs = {str(value) for value in BlogPage.objects.exclude(mongo_content_id__isnull=True).exclude(mongo_content_id="").values_list("mongo_content_id", flat=True)}
        revision_refs: set[str] = set()
        try:
            blog_ct = ContentType.objects.get_for_model(BlogPage, for_concrete_model=False)
            revisions = Revision.objects.filter(content_type=blog_ct).only("content")
            for revision in revisions.iterator():
                content = revision.content
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                if isinstance(content, dict):
                    for key in ("mongo_draft_pointer", "mongo_body_version_id", "body_version_id"):
                        value = content.get(key)
                        if value:
                            revision_refs.add(str(value))
        except Exception:
            # Revision 表不可读时，报告仍可输出，但所有相关候选必须人工复核。
            revision_refs.add("__revision_scan_error__")

        cleanup_refs: set[str] = set()
        for pointer, status in MongoCleanupIntent.objects.values_list("pointer", "status"):
            # 已完成/跳过的意图不应掩盖残留文档；未完成意图必须保护其精确指针。
            if pointer and status not in (MongoCleanupIntentStatus.SUCCEEDED, MongoCleanupIntentStatus.SKIPPED):
                cleanup_refs.add(str(pointer))
        deletion_pending_pages: set[int] = set()
        deletion_refs: set[str] = set()
        for page_id, status, manifest in PageDeletionIntent.objects.values_list("page_id", "status", "manifest"):
            parsed_page_id = _as_page_id(page_id)
            if parsed_page_id is not None:
                if status not in (PageDeletionIntentStatus.SUCCEEDED, PageDeletionIntentStatus.DEAD):
                    deletion_pending_pages.add(parsed_page_id)
            if status not in (PageDeletionIntentStatus.SUCCEEDED, PageDeletionIntentStatus.DEAD):
                deletion_refs.update(_manifest_values(manifest, "body_version_ids"))
                deletion_refs.update(_manifest_values(manifest, "revision_pointers"))
            legacy_pointer = manifest.get("legacy_pointer") if isinstance(manifest, dict) else None
            if legacy_pointer and status not in (PageDeletionIntentStatus.SUCCEEDED, PageDeletionIntentStatus.DEAD):
                deletion_refs.add(str(legacy_pointer))

        search_refs: set[str] = set()
        tombstone_pages: set[int] = set()
        try:
            from search.models import ContentSearchOutbox, ContentSearchOperation, ContentSearchState

            for page_id, operation, body_version_id, mongo_content_id in ContentSearchState.objects.values_list(
                "page_id", "desired_operation", "body_version_id", "mongo_content_id"
            ):
                if operation == ContentSearchOperation.TOMBSTONE:
                    parsed_page_id = _as_page_id(page_id)
                    if parsed_page_id is not None:
                        tombstone_pages.add(parsed_page_id)
                elif operation == ContentSearchOperation.UPSERT:
                    search_refs.update(str(value) for value in (body_version_id, mongo_content_id) if value)
            for operation, status, body_version_id, mongo_content_id in ContentSearchOutbox.objects.values_list(
                "operation", "status", "body_version_id", "mongo_content_id"
            ):
                # tombstone 事件及已完成事件不再是正文存活依据；仅保护仍待投递的 upsert。
                if operation == ContentSearchOperation.UPSERT and status != "succeeded":
                    search_refs.update(str(value) for value in (body_version_id, mongo_content_id) if value)
        except Exception:
            search_refs.add("__search_scan_error__")

        return {
            "page_ids": page_ids,
            "modern_refs": modern_refs,
            "legacy_refs": legacy_refs,
            "revision_refs": revision_refs,
            "cleanup_refs": cleanup_refs,
            "deletion_pending_pages": deletion_pending_pages,
            "deletion_refs": deletion_refs,
            "search_refs": search_refs,
            "tombstone_pages": tombstone_pages,
        }

    def _scan_mongo(
        self,
        context: dict[str, Any],
        *,
        limit: int,
        batch_size: int,
        page_filter: object,
    ) -> dict[str, Any]:
        """按集合扫描元数据并分类；投影明确排除 ``body`` 字段。"""

        mongo_settings = settings.MONGO_DB
        client = pymongo.MongoClient(
            host=mongo_settings["HOST"],
            port=mongo_settings["PORT"],
            serverSelectionTimeoutMS=5000,
        )
        try:
            database = client[mongo_settings["NAME"]]
            collections = {
                "content_body_versions": ({"aggregate_type": "blog_page"}, {"_id": 1, "aggregate_id": 1, "body_version_id": 1}),
                "blog_page_revision_bodies": ({}, {"_id": 1, "page_id": 1}),
                "blog_content": ({}, {"_id": 1, "page_id": 1}),
            }
            rows: list[dict[str, object]] = []
            category_counts: Counter[str] = Counter()
            collection_counts: Counter[str] = Counter()
            candidate_total = 0
            mongo_error: str | None = None
            for name, (query, projection) in collections.items():
                if page_filter is not None:
                    if name == "content_body_versions":
                        query = {**query, "aggregate_id": str(page_filter)}
                    else:
                        query = {**query, "page_id": page_filter}
                try:
                    cursor = database[name].find(query, projection, batch_size=batch_size)
                    for document in cursor:
                        collection_counts[name] += 1
                        category, page_id = self._classify_document(name, document, context)
                        category_counts[category] += 1
                        if category in {"referenced_page", "referenced_pending"}:
                            continue
                        candidate_total += 1
                        if len(rows) >= limit:
                            continue
                        row: dict[str, object] = {
                            "collection": name,
                            "mongo_id": str(document.get("_id")),
                            "page_id": page_id,
                            "category": category,
                        }
                        if name == "content_body_versions":
                            row["body_version_id"] = str(document.get("body_version_id")) if document.get("body_version_id") else None
                        rows.append(row)
                except Exception as exc:
                    mongo_error = type(exc).__name__
                    break
            return {
                "report": "mongo-orphan-v1",
                "collections": dict(collection_counts),
                "category_counts": dict(sorted(category_counts.items())),
                "candidate_count": candidate_total,
                "emitted_count": len(rows),
                "truncated": candidate_total > len(rows),
                "candidates": rows,
                "mongo_error": mongo_error,
            }
        finally:
            client.close()

    @staticmethod
    def _classify_document(name: str, document: dict[str, Any], context: dict[str, Any]) -> tuple[str, int | None]:
        """按页面存在性和所有已知指针分类，不以正文内容推断归属。"""

        page_id = _as_page_id(document.get("aggregate_id") if name == "content_body_versions" else document.get("page_id"))
        pointer = document.get("body_version_id") if name == "content_body_versions" else document.get("_id")
        pointer_text = str(pointer) if pointer is not None else ""
        if page_id is not None and page_id in context["page_ids"]:
            return "referenced_page", page_id
        if page_id is not None and page_id in context["deletion_pending_pages"]:
            return "referenced_pending", page_id
        known_refs = (
            context["modern_refs"]
            | context["legacy_refs"]
            | context["revision_refs"]
            | context["cleanup_refs"]
            | context["deletion_refs"]
            | context["search_refs"]
        )
        if pointer_text and pointer_text in known_refs:
            return "referenced_missing_page", page_id
        if page_id is None:
            return "blocked_unknown", None
        if page_id in context["tombstone_pages"]:
            return "orphan_candidate", page_id
        return "blocked_unknown", page_id
