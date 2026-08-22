"""从 MongoDB 正式正文构造内容搜索文档和内容哈希。"""

from collections.abc import Mapping, Sequence
from typing import Any

import hashlib
import json
from dataclasses import dataclass

from django.utils.html import strip_tags


_FORMAL_CONTENT_UNSET = object()


@dataclass(frozen=True)
class FormalContentSnapshot:
    """正式正文的稳定标识和哈希；草稿 revision 不参与计算。"""
    mongo_content_id: str
    content_hash: str


@dataclass(frozen=True)
class FormalContentDocument:
    """待写入搜索索引的最小公开文档及其版本校验信息。"""
    mongo_content_id: str
    content_hash: str
    document: dict


def _related_ids(page: object, relation_name: str) -> list[int]:
    """只投影公开筛选需要的关联主键，不复制关系对象或展示字段。"""
    """只投影公开筛选所需的关联主键，不复制关系对象或展示字段。"""

    relation = getattr(page, relation_name, None)
    if relation is None:
        return []
    if hasattr(relation, "all"):
        return [item.pk for item in relation.all()]
    return list(relation.values_list("pk", flat=True))


def _build_formal_payload(
    page: object, formal_content: object = _FORMAL_CONTENT_UNSET
) -> dict[str, Any] | None:
    """从正式 Mongo 正文生成搜索投影；空正文是合法值，缺失正文才返回 None。"""
    """从正式 Mongo 正文构造索引输入，草稿 Revision 永远不参与公开搜索版本。"""

    mongo_content_id = getattr(page, "mongo_content_id", None)
    if not mongo_content_id:
        return None

    # 回填可传入同批次已读取的正式正文，避免每篇文章再次访问 MongoDB。
    if formal_content is _FORMAL_CONTENT_UNSET:
        formal_content = page.get_content_from_mongodb()
    # 空文章正文是合法内容，只有正式文档不存在才应判定为 Mongo 缺失。
    if formal_content is None:
        return None

    body_text = page.get_full_text_for_search(content=formal_content)

    published_date = getattr(page, "date", None)
    first_published_at = getattr(page, "first_published_at", None)
    return {
        "title": str(getattr(page, "title", "") or ""),
        "intro": strip_tags(str(getattr(page, "intro", "") or "")),
        "body_text": body_text,
        "date": published_date.isoformat() if published_date else None,
        "first_published_at": first_published_at.isoformat() if first_published_at else None,
        "locale_id": getattr(page, "locale_id", None),
        "tag_ids": _related_ids(page, "tags"),
        "category_ids": _related_ids(page, "categories"),
        "mongo_content_id": str(mongo_content_id),
    }


def _content_hash(payload: Mapping[str, Any]) -> str:
    """按稳定 JSON 字段计算哈希，并排除只影响排序的首次发布时间。"""
    # 首次发布时间只服务于普通页面排序，不属于 Mongo 正文；否则新增索引字段会误判正文版本变化。
    hash_payload = dict(payload)
    hash_payload.pop("first_published_at", None)
    serialized = json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_formal_content_snapshot(
    page: object, formal_content: object = _FORMAL_CONTENT_UNSET
) -> FormalContentSnapshot | None:
    """只从正式正文构造 hash，草稿 revision 永远不进入公开搜索版本。"""
    """只从 Mongo 正式正文构造 hash，草稿 Revision 永远不参与公开搜索版本。"""

    payload = _build_formal_payload(page, formal_content=formal_content)
    if payload is None:
        return None
    return FormalContentSnapshot(
        mongo_content_id=payload["mongo_content_id"],
        content_hash=_content_hash(payload),
    )


def build_formal_content_document(
    page: object,
    content_version: int,
    formal_content: object = _FORMAL_CONTENT_UNSET,
) -> FormalContentDocument | None:
    """生成不含 HTML、草稿指针或 Mongo 原始块的最小公开索引文档。"""
    """生成最小公开索引文档，不包含 HTML、草稿指针或 Mongo 原始块。"""

    payload = _build_formal_payload(page, formal_content=formal_content)
    if payload is None:
        return None

    document = {
        "page_id": page.pk,
        "content_version": content_version,
        "content_hash": _content_hash(payload),
        "searchable": True,
        "operation": "upsert",
        "title": payload["title"],
        "intro": payload["intro"],
        "body_text": payload["body_text"],
        "date": payload["date"],
        "first_published_at": payload["first_published_at"],
        "locale_id": payload["locale_id"],
        "tag_ids": payload["tag_ids"],
        "category_ids": payload["category_ids"],
    }
    return FormalContentDocument(
        mongo_content_id=payload["mongo_content_id"],
        content_hash=document["content_hash"],
        document=document,
    )


def build_formal_content_documents(
    pages: Sequence[object],
    content_versions: Mapping[int, int],
    formal_contents: Mapping[str, object],
) -> tuple[list[FormalContentDocument], list[int]]:
    """基于批量 Mongo 读取结果投影文档，禁止循环内回退单篇查询。"""
    """基于批量读取结果投影文档，禁止在循环中回退到单篇 Mongo 查询。"""

    documents = []
    missing_page_ids = []
    for page in pages:
        mongo_content_id = getattr(page, "mongo_content_id", None)
        formal_content = formal_contents.get(str(mongo_content_id))
        document = build_formal_content_document(
            page,
            content_versions[page.pk],
            formal_content=formal_content,
        )
        if document is None:
            missing_page_ids.append(page.pk)
            continue
        documents.append(document)
    return documents, missing_page_ids
