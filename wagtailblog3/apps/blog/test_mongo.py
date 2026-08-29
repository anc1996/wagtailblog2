"""验证 Mongo 历史正文读取器的指针兼容性与失败分类。"""

import json

from bson import ObjectId
from django.test import SimpleTestCase

from wagtailblog3.mongo import (
    MongoManager,
    MongoRevisionBodyError,
    MongoRevisionNotFoundError,
    MongoRevisionPointerError,
    MongoRevisionUnavailableError,
)


class _MemoryRevisionCollection:
    """只读内存替身，避免读取器单元测试连接真实 MongoDB。"""

    def __init__(self, documents=None, error=None):
        self.documents = documents or {}
        self.error = error

    def find_one(self, query):
        if self.error is not None:
            raise self.error
        return self.documents.get(query["_id"])


class MongoRevisionReaderTests(SimpleTestCase):
    """验证 BSON、历史字符串指针及各类快照读取失败。"""

    def setUp(self):
        self.manager = object.__new__(MongoManager)

    def test_reads_object_id_and_legacy_string_pointer(self):
        object_id = ObjectId()
        self.manager.blog_revisions = _MemoryRevisionCollection(
            {
                object_id: {"_id": object_id, "page_id": 38, "body": []},
                "rev_544_legacy": {
                    "_id": "rev_544_legacy",
                    "page_id": 544,
                    "body": [],
                },
                "rev_545_json": {
                    "_id": "rev_545_json",
                    "page_id": 545,
                    "body": json.dumps([]),
                },
            }
        )

        self.assertEqual(
            self.manager.get_blog_revision_body(str(object_id))["_id"],
            str(object_id),
        )
        self.assertEqual(
            self.manager.get_blog_revision_body("rev_544_legacy")["_id"],
            "rev_544_legacy",
        )
        self.assertEqual(
            self.manager.get_blog_revision_body("rev_545_json")["body"],
            [],
        )

    def test_classifies_empty_missing_body_and_missing_snapshot(self):
        self.manager.blog_revisions = _MemoryRevisionCollection(
            {"broken": {"_id": "broken", "page_id": 38}}
        )

        with self.assertRaises(MongoRevisionPointerError):
            self.manager.get_blog_revision_body("")
        with self.assertRaises(MongoRevisionBodyError):
            self.manager.get_blog_revision_body("broken")
        with self.assertRaises(MongoRevisionNotFoundError):
            self.manager.get_blog_revision_body("missing")

    def test_classifies_invalid_json_and_non_list_legacy_body(self):
        self.manager.blog_revisions = _MemoryRevisionCollection(
            {
                "invalid-json": {"_id": "invalid-json", "body": "{"},
                "not-list": {"_id": "not-list", "body": json.dumps({})},
            }
        )

        with self.assertRaises(MongoRevisionBodyError):
            self.manager.get_blog_revision_body("invalid-json")
        with self.assertRaises(MongoRevisionBodyError):
            self.manager.get_blog_revision_body("not-list")

    def test_classifies_mongo_failure_as_unavailable(self):
        self.manager.blog_revisions = _MemoryRevisionCollection(
            error=RuntimeError("simulated read failure")
        )

        with self.assertRaises(MongoRevisionUnavailableError):
            self.manager.get_blog_revision_body("rev_544_legacy")
