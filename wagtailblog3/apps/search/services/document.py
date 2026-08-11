import hashlib
import json
from dataclasses import dataclass

from django.utils.html import strip_tags


_FORMAL_CONTENT_UNSET = object()


@dataclass(frozen=True)
class FormalContentSnapshot:
    mongo_content_id: str
    content_hash: str


@dataclass(frozen=True)
class FormalContentDocument:
    mongo_content_id: str
    content_hash: str
    document: dict


def _related_ids(page, relation_name):
    """只投影公开筛选所需的关联主键，不复制关系对象或展示字段。"""

    relation = getattr(page, relation_name, None)
    if relation is None:
        return []
    if hasattr(relation, "all"):
        return [item.pk for item in relation.all()]
    return list(relation.values_list("pk", flat=True))


def _build_formal_payload(page, formal_content=_FORMAL_CONTENT_UNSET):
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
    return {
        "title": str(getattr(page, "title", "") or ""),
        "intro": strip_tags(str(getattr(page, "intro", "") or "")),
        "body_text": body_text,
        "date": published_date.isoformat() if published_date else None,
        "locale_id": getattr(page, "locale_id", None),
        "tag_ids": _related_ids(page, "tags"),
        "category_ids": _related_ids(page, "categories"),
        "mongo_content_id": str(mongo_content_id),
    }


def _content_hash(payload):
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_formal_content_snapshot(page, formal_content=_FORMAL_CONTENT_UNSET):
    """只从 Mongo 正式正文构造 hash，草稿 Revision 永远不参与公开搜索版本。"""

    payload = _build_formal_payload(page, formal_content=formal_content)
    if payload is None:
        return None
    return FormalContentSnapshot(
        mongo_content_id=payload["mongo_content_id"],
        content_hash=_content_hash(payload),
    )


def build_formal_content_document(
    page,
    content_version,
    formal_content=_FORMAL_CONTENT_UNSET,
):
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
        "locale_id": payload["locale_id"],
        "tag_ids": payload["tag_ids"],
        "category_ids": payload["category_ids"],
    }
    return FormalContentDocument(
        mongo_content_id=payload["mongo_content_id"],
        content_hash=document["content_hash"],
        document=document,
    )


def build_formal_content_documents(pages, content_versions, formal_contents):
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
