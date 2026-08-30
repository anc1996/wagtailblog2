"""独立内容索引的正式 Mongo 正文批量读取。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from bson import ObjectId

from wagtailblog3.mongo import MongoManager


class ContentSearchMongoReadError(Exception):
    """只向调用方暴露脱敏错误码，避免 Mongo 异常带出连接信息或正文。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def read_published_body_versions_by_page(
	page_refs: Mapping[int, Mapping[str, object]],
	mongo_manager: Any = None,
) -> dict[str, Mapping[str, list[Any]]]:
	"""一次查询读取多个页面的已发布不可变正文版本。

	``page_refs`` 由页面主键映射到 ``body_version_id``、``body_sha256`` 和
	``body_schema_version``。查询结果按 ``page:<id>`` 返回，供批量索引直接投影；
	缺失或字段不完整的版本不会回退到其他页面正文。函数只读 MongoDB，单次查询
	失败统一转换为脱敏的搜索读取错误。
	"""
	if not page_refs:
		return {}
	queries: list[dict[str, object]] = []
	for page_id, ref in page_refs.items():
		version_id = ref.get("body_version_id")
		body_sha256 = ref.get("body_sha256")
		schema_version = ref.get("body_schema_version")
		if (
			isinstance(version_id, str)
			and version_id
			and isinstance(body_sha256, str)
			and len(body_sha256) == 64
			and isinstance(schema_version, int)
			and schema_version > 0
		):
			queries.append(
				{
					"aggregate_type": "blog_page",
					"aggregate_id": str(page_id),
					"body_version_id": version_id,
					"body_sha256": body_sha256,
					"body_schema_version": schema_version,
				}
			)
	if not queries:
		return {}
	try:
		manager = mongo_manager or MongoManager()
		cursor = manager.content_body_versions.find(
			{"$or": queries},
			{"_id": 0, "aggregate_id": 1, "body_version_id": 1, "body": 1},
		)
		by_page: dict[str, Mapping[str, list[Any]]] = {}
		for document in cursor:
			if not isinstance(document, dict) or not isinstance(document.get("body"), list):
				continue
			aggregate_id = document.get("aggregate_id")
			if aggregate_id is None:
				continue
			by_page[f"page:{aggregate_id}"] = {"body": document["body"]}
		return by_page
	except Exception as error:
		raise ContentSearchMongoReadError("mongo_published_body_version_batch_read_failed") from error


def read_formal_contents_by_id(
    mongo_content_ids: Iterable[Any],
    mongo_manager: Any = None,
    page_ids: Iterable[Any] | None = None,
) -> dict[str, Mapping[str, list[Any]]]:
    """一次查询返回多个正式正文；无效或缺失 ID 由调用方按页面记录处理。"""

    object_ids = []
    seen_ids = set()
    for content_id in mongo_content_ids:
        content_id = str(content_id or "")
        if not content_id or content_id in seen_ids or not ObjectId.is_valid(content_id):
            continue
        seen_ids.add(content_id)
        object_ids.append(ObjectId(content_id))
    if not object_ids and not page_ids:
        return {}

    try:
        manager = mongo_manager or MongoManager()
        projection = {"_id": 1, "body": 1}
        if page_ids is not None:
            projection["page_id"] = 1
        cursor = manager.blog_content.find(
            {"_id": {"$in": object_ids}} if object_ids else {"_id": {"$exists": False}},
            projection,
        )
        contents = {}
        for document in cursor:
            body = document.get("body") if isinstance(document, dict) else None
            if isinstance(body, list) and document.get("_id") is not None:
                value = {"body": body}
                contents[str(document["_id"])] = value
                if page_ids is not None and document.get("page_id") is not None:
                    contents[f"page:{document['page_id']}"] = value
        missing_page_ids = [page_id for page_id in (page_ids or []) if f"page:{page_id}" not in contents]
        if missing_page_ids:
            for document in manager.blog_content.find(
                {"page_id": {"$in": missing_page_ids}},
                {"_id": 1, "page_id": 1, "body": 1},
            ):
                body = document.get("body") if isinstance(document, dict) else None
                page_id = document.get("page_id") if isinstance(document, dict) else None
                if isinstance(body, list) and page_id is not None:
                    value = {"body": body}
                    contents[f"page:{page_id}"] = value
                    if document.get("_id") is not None:
                        contents[str(document["_id"])] = value
        return contents
    except Exception as error:
        raise ContentSearchMongoReadError("mongo_formal_content_batch_read_failed") from error
