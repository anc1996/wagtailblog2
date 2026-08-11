"""独立内容索引的正式 Mongo 正文批量读取。"""

from __future__ import annotations

from bson import ObjectId

from wagtailblog3.mongo import MongoManager


class ContentSearchMongoReadError(Exception):
    """只向调用方暴露脱敏错误码，避免 Mongo 异常带出连接信息或正文。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def read_formal_contents_by_id(mongo_content_ids, mongo_manager=None):
    """一次查询返回多个正式正文；无效或缺失 ID 由调用方按页面记录处理。"""

    object_ids = []
    seen_ids = set()
    for content_id in mongo_content_ids:
        content_id = str(content_id or "")
        if not content_id or content_id in seen_ids or not ObjectId.is_valid(content_id):
            continue
        seen_ids.add(content_id)
        object_ids.append(ObjectId(content_id))
    if not object_ids:
        return {}

    try:
        manager = mongo_manager or MongoManager()
        cursor = manager.blog_content.find(
            {"_id": {"$in": object_ids}},
            {"_id": 1, "body": 1},
        )
        contents = {}
        for document in cursor:
            body = document.get("body") if isinstance(document, dict) else None
            if isinstance(body, list) and document.get("_id") is not None:
                contents[str(document["_id"])] = {"body": body}
        return contents
    except Exception as error:
        raise ContentSearchMongoReadError("mongo_formal_content_batch_read_failed") from error
