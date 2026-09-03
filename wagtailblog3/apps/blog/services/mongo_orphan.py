"""MongoDB 孤儿正文数据治理与安全清理服务。

负责扫描 MongoDB 中未被 MySQL 活跃页面、草稿、Revision 引用或删除任务引用的残留正文快照，
提供正文内容智能反解析预览（支持提取标题、字符数、Markdown 正文及 StreamField 块结构），
并在执行物理删除前通过强阻断 Fencing Token 机制复核当前活跃状态，杜绝误删风险。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
import pymongo
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from wagtail.models import Revision

from blog.models import (
    BlogPage,
    BlogPublicationState,
    MongoCleanupIntent,
    MongoCleanupIntentStatus,
    PageDeletionIntent,
    PageDeletionIntentStatus,
)

logger = logging.getLogger(__name__)

TARGET_COLLECTIONS: tuple[str, ...] = (
    "content_body_versions",
    "blog_page_revision_bodies",
    "blog_content",
)

CATEGORY_LABELS: dict[str, str] = {
    "referenced_page": "存活页面引用 (严禁删除)",
    "referenced_pending": "待删除任务引用 (Worker处理中)",
    "referenced_missing_page": "历史快照残留 (缺主页面)",
    "orphan_candidate": "完全孤儿正文 (可安全清理)",
    "blocked_unknown": "未知受阻正文 (暂不支持清理)",
}


def _as_page_id(value: object) -> int | None:
    """将 Mongo/MySQL 中可能为字符串或 None 的页面编号规范化为 int。"""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_id(mongo_id: str | ObjectId) -> ObjectId | str:
    """尝试将 ID 字符串解析为 ObjectId；若不是合法 ObjectId 则退回为原始字符串。"""
    if isinstance(mongo_id, ObjectId):
        return mongo_id
    id_str = str(mongo_id).strip()
    if ObjectId.is_valid(id_str):
        return ObjectId(id_str)
    return id_str


def _extract_created_time(document: dict[str, Any], doc_id: ObjectId | str) -> str | None:
    """优先从文档自身提取创建时间，若无则尝试从 ObjectId 的 generation_time 获取。"""
    created = document.get("created_at")
    if created:
        if isinstance(created, datetime):
            return created.astimezone(timezone.utc).isoformat()
        return str(created)
    if isinstance(doc_id, ObjectId):
        try:
            return doc_id.generation_time.astimezone(timezone.utc).isoformat()
        except Exception:
            return None
    return None


class MongoOrphanService:
    """Mongo 孤儿正文扫描、解析与受控安全清理核心服务。"""

    @classmethod
    def get_mongo_database(cls) -> pymongo.database.Database:
        """获取直连的 MongoDB 数据库实例；避免实例化创建全局索引的类产生非预期副作用。"""
        mongo_settings = settings.MONGO_DB
        client = pymongo.MongoClient(
            host=mongo_settings["HOST"],
            port=mongo_settings["PORT"],
            serverSelectionTimeoutMS=5000,
        )
        return client[mongo_settings["NAME"]]

    @classmethod
    def collect_mysql_context(cls) -> dict[str, Any]:
        """扫描 MySQL 中的活跃引用集合，构建内存白名单；不读取大字段正文。"""
        page_ids = {int(value) for value in BlogPage.objects.values_list("pk", flat=True)}

        modern_refs: set[str] = set()
        for row in BlogPublicationState.objects.values_list(
            "page_id", "draft_body_version_id", "published_body_version_id", "approved_body_version_id"
        ):
            modern_refs.update(str(value) for value in row[1:] if value)

        legacy_refs = {
            str(value)
            for value in BlogPage.objects.exclude(mongo_content_id__isnull=True)
            .exclude(mongo_content_id="")
            .values_list("mongo_content_id", flat=True)
        }

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
        except Exception as exc:
            logger.warning("收集 Revision 指针异常 (降级跳过): %s", exc)

        cleanup_refs = {
            str(value)
            for value in MongoCleanupIntent.objects.filter(
                status__in=[
                    MongoCleanupIntentStatus.PENDING,
                    MongoCleanupIntentStatus.RETRY,
                    MongoCleanupIntentStatus.PROCESSING,
                ]
            ).values_list("pointer", flat=True)
            if value
        }

        active_deletion_statuses = [
            PageDeletionIntentStatus.DELETING,
            PageDeletionIntentStatus.PROCESSING,
            PageDeletionIntentStatus.SEARCH_PENDING,
            PageDeletionIntentStatus.MONGO_PENDING,
            PageDeletionIntentStatus.PARTIAL_FAILED,
            PageDeletionIntentStatus.BLOCKED_REFERENCE,
            PageDeletionIntentStatus.MYSQL_FINALIZE_PENDING,
        ]

        deletion_pending_pages = {
            int(value)
            for value in PageDeletionIntent.objects.filter(
                status__in=active_deletion_statuses
            ).values_list("page_id", flat=True)
            if value is not None
        }

        deletion_refs: set[str] = set()
        for manifest in PageDeletionIntent.objects.filter(
            status__in=active_deletion_statuses
        ).values_list("manifest", flat=True):
            if isinstance(manifest, dict):
                for k in ("body_version_ids", "revision_ids", "legacy_content_ids"):
                    items = manifest.get(k)
                    if isinstance(items, list):
                        deletion_refs.update(str(v) for v in items if v)

        tombstone_pages = {
            int(value)
            for value in PageDeletionIntent.objects.filter(
                status=PageDeletionIntentStatus.SUCCEEDED
            ).values_list("page_id", flat=True)
            if value is not None
        }

        # 动态联动搜索同步体系中的墓碑记录 (ContentSearchState.desired_operation == "tombstone")
        try:
            from search.models import ContentSearchState
            tombstone_pages.update(
                int(value)
                for value in ContentSearchState.objects.filter(
                    desired_operation="tombstone"
                ).values_list("page_id", flat=True)
                if value is not None
            )
        except Exception as exc:
            logger.warning("收集 ContentSearchState 墓碑页面异常 (降级跳过): %s", exc)

        # 动态联动搜索出件箱中的墓碑事件 (ContentSearchOutbox.operation == "tombstone")
        try:
            from search.models import ContentSearchOutbox
            tombstone_pages.update(
                int(value)
                for value in ContentSearchOutbox.objects.filter(
                    operation="tombstone"
                ).values_list("page_id", flat=True)
                if value is not None
            )
        except Exception as exc:
            logger.warning("收集 ContentSearchOutbox 墓碑页面异常 (降级跳过): %s", exc)

        return {
            "page_ids": page_ids,
            "modern_refs": modern_refs,
            "legacy_refs": legacy_refs,
            "revision_refs": revision_refs,
            "cleanup_refs": cleanup_refs,
            "deletion_pending_pages": deletion_pending_pages,
            "deletion_refs": deletion_refs,
            "tombstone_pages": tombstone_pages,
        }

    @classmethod
    def classify_document(
        cls, collection_name: str, document: dict[str, Any], context: dict[str, Any]
    ) -> tuple[str, int | None]:
        """根据页面存在性和已知引用指针对文档分类。"""
        page_id = _as_page_id(
            document.get("aggregate_id")
            if collection_name == "content_body_versions"
            else document.get("page_id")
        )
        pointer = (
            document.get("body_version_id")
            if collection_name == "content_body_versions"
            else document.get("_id")
        )
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
        )
        if pointer_text and pointer_text in known_refs:
            return "referenced_missing_page", page_id

        if page_id is None:
            return "blocked_unknown", None

        if page_id in context.get("tombstone_pages", set()):
            return "orphan_candidate", page_id

        return "blocked_unknown", page_id

    @classmethod
    def scan_orphans(
        cls,
        *,
        limit: int = 1000,
        batch_size: int = 500,
        page_filter: int | None = None,
        collection_filter: str | None = None,
        include_referenced: bool = False,
    ) -> dict[str, Any]:
        """全量或定向扫描 MongoDB 孤儿正文候选列表。"""
        limit = max(1, min(limit, 5000))
        batch_size = max(50, min(batch_size, 5000))

        context = cls.collect_mysql_context()
        database = cls.get_mongo_database()

        collections_to_scan: dict[str, tuple[dict[str, Any], dict[str, int]]] = {
            "content_body_versions": (
                {"aggregate_type": "blog_page"},
                {"_id": 1, "aggregate_id": 1, "body_version_id": 1, "created_at": 1},
            ),
            "blog_page_revision_bodies": ({}, {"_id": 1, "page_id": 1, "created_at": 1}),
            "blog_content": ({}, {"_id": 1, "page_id": 1, "created_at": 1}),
        }

        if collection_filter and collection_filter in collections_to_scan:
            collections_to_scan = {collection_filter: collections_to_scan[collection_filter]}

        rows: list[dict[str, Any]] = []
        category_counts: Counter[str] = Counter()
        collection_counts: Counter[str] = Counter()
        candidate_total = 0
        mongo_error: str | None = None

        try:
            for coll_name, (base_query, projection) in collections_to_scan.items():
                query = dict(base_query)
                if page_filter is not None:
                    if coll_name == "content_body_versions":
                        query["aggregate_id"] = str(page_filter)
                    else:
                        query["page_id"] = page_filter

                try:
                    cursor = database[coll_name].find(query, projection, batch_size=batch_size)
                    for document in cursor:
                        collection_counts[coll_name] += 1
                        category, page_id = cls.classify_document(coll_name, document, context)
                        category_counts[category] += 1

                        if not include_referenced and category in {"referenced_page", "referenced_pending"}:
                            continue

                        candidate_total += 1
                        if len(rows) >= limit:
                            continue

                        doc_id = document.get("_id")
                        mongo_id_str = str(doc_id)
                        created_at_hint = _extract_created_time(document, doc_id)

                        row: dict[str, Any] = {
                            "collection": coll_name,
                            "mongo_id": mongo_id_str,
                            "page_id": page_id,
                            "category": category,
                            "category_label": CATEGORY_LABELS.get(category, category),
                            "created_at": created_at_hint,
                            "can_delete": category in {"orphan_candidate", "referenced_missing_page"},
                        }
                        if coll_name == "content_body_versions":
                            row["body_version_id"] = str(document.get("body_version_id") or "")
                        rows.append(row)
                except Exception as exc:
                    mongo_error = f"{type(exc).__name__}: {exc}"
                    logger.error("扫描集合 %s 发生错误: %s", coll_name, exc, exc_info=True)
                    break
        finally:
            database.client.close()

        return {
            "collections": dict(collection_counts),
            "category_counts": dict(sorted(category_counts.items())),
            "candidate_count": candidate_total,
            "emitted_count": len(rows),
            "truncated": candidate_total > len(rows),
            "candidates": rows,
            "mongo_error": mongo_error,
        }

    @classmethod
    def get_orphan_body_preview(cls, collection_name: str, mongo_id: str) -> dict[str, Any]:
        """读取指定 Mongo 正文文档并进行反解析，生成富文本预览。"""
        if collection_name not in TARGET_COLLECTIONS:
            raise ValueError(f"不支持的 Mongo 集合: {collection_name}")

        database = cls.get_mongo_database()
        doc_id = _extract_id(mongo_id)

        try:
            document = database[collection_name].find_one({"_id": doc_id})
            if document is None and isinstance(doc_id, ObjectId):
                document = database[collection_name].find_one({"_id": str(mongo_id)})
            if document is None and isinstance(doc_id, str):
                if ObjectId.is_valid(doc_id):
                    document = database[collection_name].find_one({"_id": ObjectId(doc_id)})

            if document is None:
                raise FileNotFoundError(f"在集合 {collection_name} 中未找到 ID 为 {mongo_id} 的文档")

            context = cls.collect_mysql_context()
            category, page_id = cls.classify_document(collection_name, document, context)

            body = document.get("body")
            created_at = _extract_created_time(document, doc_id)
            markdown_content, title_hint, block_count, block_types = cls._parse_body_content(body)
            reason_desc = cls._build_reason_description(category, page_id, collection_name)

            return {
                "collection": collection_name,
                "mongo_id": str(mongo_id),
                "page_id": page_id,
                "body_version_id": str(document.get("body_version_id") or ""),
                "created_at": created_at,
                "category": category,
                "category_label": CATEGORY_LABELS.get(category, category),
                "title_hint": title_hint or "(未识别到明确标题)",
                "markdown_content": markdown_content,
                "char_count": len(markdown_content),
                "block_count": block_count,
                "block_types": block_types,
                "raw_body_snippet": str(body)[:1000] if body is not None else "",
                "can_delete": category in {"orphan_candidate", "referenced_missing_page"},
                "orphan_reason": reason_desc,
            }
        finally:
            database.client.close()

    @classmethod
    def _parse_body_content(cls, body: object) -> tuple[str, str | None, int, list[str]]:
        """从各种格式的 body 数据中提取 Markdown 文本、标题与块元信息。"""
        if body is None:
            return "", None, 0, []

        markdown_chunks: list[str] = []
        title_hint: str | None = None
        block_count = 0
        block_types: list[str] = []

        if isinstance(body, str):
            try:
                parsed_json = json.loads(body)
                if isinstance(parsed_json, list):
                    return cls._parse_body_content(parsed_json)
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
            raw_text = body.strip()
            title_hint = cls._extract_title_from_text(raw_text)
            return raw_text, title_hint, 1, ["raw_text"]

        if isinstance(body, list):
            block_count = len(body)
            for idx, block in enumerate(body):
                if not isinstance(block, dict):
                    continue
                b_type = str(block.get("type") or f"block_{idx}")
                block_types.append(b_type)
                b_val = block.get("value")

                if isinstance(b_val, str):
                    clean_str = b_val.strip()
                    if clean_str:
                        markdown_chunks.append(clean_str)
                        if not title_hint:
                            title_hint = cls._extract_title_from_text(clean_str)
                elif isinstance(b_val, dict):
                    if "heading" in b_val and isinstance(b_val["heading"], str):
                        heading_text = b_val["heading"].strip()
                        markdown_chunks.append(f"## {heading_text}")
                        if not title_hint:
                            title_hint = heading_text
                    if "text" in b_val and isinstance(b_val["text"], str):
                        markdown_chunks.append(b_val["text"].strip())
                    if "markdown" in b_val and isinstance(b_val["markdown"], str):
                        markdown_chunks.append(b_val["markdown"].strip())
                    if "code" in b_val and isinstance(b_val["code"], str):
                        lang = b_val.get("language", "")
                        markdown_chunks.append(f"```{lang}\n{b_val['code']}\n```")

            combined_markdown = "\n\n".join(markdown_chunks)
            if not title_hint:
                title_hint = cls._extract_title_from_text(combined_markdown)
            return combined_markdown, title_hint, block_count, block_types

        if isinstance(body, dict):
            raw_serialized = json.dumps(body, ensure_ascii=False, indent=2)
            title = body.get("title") if isinstance(body.get("title"), str) else None
            return raw_serialized, title, 1, ["dict_body"]

        return str(body), None, 1, ["unknown"]

    @staticmethod
    def _extract_title_from_text(text: str) -> str | None:
        """从 Markdown 或 HTML 文本中提取首行 # 标题或首句纯文本作为线索。"""
        if not text:
            return None
        # 清理 HTML 标签以获得纯净文本标题
        clean_text = re.sub(r"<[^>]+>", "", text).strip()
        lines = [line.strip() for line in clean_text.splitlines() if line.strip()]
        if not lines:
            return None

        for line in lines[:5]:
            match = re.match(r"^#{1,3}\s+(.+)$", line)
            if match:
                return match.group(1).strip()

        first_line = lines[0]
        return first_line[:40] + ("..." if len(first_line) > 40 else "")

    @staticmethod
    def _build_reason_description(category: str, page_id: int | None, collection_name: str) -> str:
        """生成详细的孤儿判定依据描述，便于管理员审查。"""
        if category == "orphan_candidate":
            return (
                f"【完全孤儿】该文档属于集合 {collection_name}，对应页面编号 page_id={page_id}。"
                "在 MySQL 中已确认物理删除且处于墓碑清单，且没有任何活跃 BlogPage、BlogPublicationState"
                "或活动任务指针引用，判定为可安全物理清理的数据。"
            )
        if category == "referenced_missing_page":
            return (
                f"【历史残留】对应页面 page_id={page_id} 在 MySQL 中已不存在，但此正文版本指针仍在"
                "历史某次 Revision 历史快照或清理意图清单中保留。暂建议保留以防历史回滚。"
            )
        if category == "referenced_page":
            return f"【活跃保护】对应页面 page_id={page_id} 仍在 MySQL 中正常运行，绝对禁止删除！"
        if category == "referenced_pending":
            return f"【待删除任务保护】页面 page_id={page_id} 正在由后台 Worker 进行事务性级联删除，禁止人工干预删除！"
        return "【未知受阻】缺少有效的 aggregate_id 或 page_id，存在结构异常，受系统安全边界保护禁止清理。"

    @classmethod
    def delete_orphan_document(
        cls,
        collection_name: str,
        mongo_id: str,
        *,
        actor: object = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """安全删除单个 Mongo 孤儿文档（包含瞬时 Fencing 并发防误删校验）。"""
        if collection_name not in TARGET_COLLECTIONS:
            raise ValueError(f"非法集合名称: {collection_name}")

        database = cls.get_mongo_database()
        doc_id = _extract_id(mongo_id)

        try:
            document = database[collection_name].find_one({"_id": doc_id})
            if document is None and isinstance(doc_id, ObjectId):
                document = database[collection_name].find_one({"_id": str(mongo_id)})
            if document is None and isinstance(doc_id, str) and ObjectId.is_valid(doc_id):
                document = database[collection_name].find_one({"_id": ObjectId(doc_id)})

            if document is None:
                raise FileNotFoundError(f"目标文档在集合 {collection_name} 中不存在或已被删除")

            context = cls.collect_mysql_context()
            category, page_id = cls.classify_document(collection_name, document, context)

            if category in {"referenced_page", "referenced_pending"}:
                logger.critical(
                    "安全阻断：尝试清理正被引用的 Mongo 正文！actor=%s coll=%s id=%s cat=%s page_id=%s",
                    actor,
                    collection_name,
                    mongo_id,
                    category,
                    page_id,
                )
                raise PermissionError(
                    f"并发防护拦截：文档 {mongo_id} 正在被现有活跃页面 (page_id={page_id}) "
                    f"或待处理删除任务引用，系统严禁删除！"
                )

            # 允许完全无主孤儿以及主页面已在 MySQL 物理删除的历史快照残留文档受控清理
            # 存活页面引用 (referenced_page) 与 Worker 删除中任务 (referenced_pending) 仍被上方 Fencing 绝对阻断
            if category not in {"orphan_candidate", "referenced_missing_page"} and not force:
                raise ValueError(
                    f"文档 {mongo_id} 当前分类为【{CATEGORY_LABELS.get(category, category)}】，"
                    "未达到受控清理孤儿标准，拒绝物理删除！"
                )

            actual_id = document["_id"]
            result = database[collection_name].delete_one({"_id": actual_id})

            if result.deleted_count < 1:
                raise RuntimeError(f"MongoDB delete_one 执行未删除任何文档: {actual_id}")

            actor_name = getattr(actor, "username", str(actor or "anonymous"))
            logger.warning(
                "【Mongo孤儿安全清理成功】操作人=%s 集合=%s MongoID=%s PageID=%s 分类=%s",
                actor_name,
                collection_name,
                str(actual_id),
                page_id,
                category,
            )

            return {
                "success": True,
                "collection": collection_name,
                "mongo_id": str(actual_id),
                "page_id": page_id,
                "category": category,
                "deleted_count": result.deleted_count,
            }
        finally:
            database.client.close()
