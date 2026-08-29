"""不可变 Mongo 正文版本仓储的隔离测试，不写入真实数据库。"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from wagtailblog3.mongo import (
    MongoBodyVersionBodyError,
    MongoBodyVersionNotFoundError,
    MongoManager,
)
from blog.models import BlogPage


class _MemoryVersionCollection:
    """提供 Mongo find/insert 最小契约，并记录插入次数。"""

    def __init__(self):
        self.documents = []
        self.insert_count = 0

    def find_one(self, query):
        for document in self.documents:
            if all(str(document.get(key)) == str(value) for key, value in query.items()):
                return dict(document)
        return None

    def insert_one(self, document):
        self.documents.append(dict(document))
        self.insert_count += 1
        return SimpleNamespace(inserted_id=document["body_version_id"])


class MongoBodyVersionTests(TestCase):
    """校验版本身份、幂等插入和完整性围栏。"""

    def setUp(self):
        self.manager = object.__new__(MongoManager)
        self.manager.content_body_versions = _MemoryVersionCollection()

    def test_same_body_reuses_immutable_version(self):
        first = self.manager.save_content_body_version("blog_page", 38, [{"type": "rich_text", "value": "a"}])
        second = self.manager.save_content_body_version("blog_page", 38, [{"value": "a", "type": "rich_text"}])

        self.assertEqual(first, second)
        self.assertEqual(self.manager.content_body_versions.insert_count, 1)

    def test_different_body_gets_distinct_version(self):
        first = self.manager.save_content_body_version("blog_page", 38, [{"type": "rich_text", "value": "a"}])
        second = self.manager.save_content_body_version("blog_page", 38, [{"type": "rich_text", "value": "b"}])

        self.assertNotEqual(first["body_version_id"], second["body_version_id"])
        self.assertEqual(self.manager.content_body_versions.insert_count, 2)

    def test_schema_version_is_part_of_version_identity(self):
        first = self.manager.save_content_body_version(
            "blog_page", 38, [{"type": "rich_text", "value": "a"}], body_schema_version=1
        )
        second = self.manager.save_content_body_version(
            "blog_page", 38, [{"type": "rich_text", "value": "a"}], body_schema_version=2
        )

        self.assertNotEqual(first["body_version_id"], second["body_version_id"])
        self.assertEqual(self.manager.content_body_versions.insert_count, 2)

    def test_read_rejects_tampered_body_hash(self):
        version = self.manager.save_content_body_version("blog_page", 38, [{"type": "rich_text", "value": "a"}])
        self.manager.content_body_versions.documents[0]["body"] = [{"type": "rich_text", "value": "tampered"}]

        with self.assertRaises(MongoBodyVersionBodyError):
            self.manager.get_content_body_version(
                "blog_page", 38, version["body_version_id"], version["body_sha256"], version["body_schema_version"]
            )

    def test_read_requires_full_aggregate_identity(self):
        version = self.manager.save_content_body_version("blog_page", 38, [])

        with self.assertRaises(MongoBodyVersionNotFoundError):
            self.manager.get_content_body_version(
                "blog_page", 39, version["body_version_id"], version["body_sha256"], version["body_schema_version"]
            )

    @patch("blog.models.MongoManager")
    def test_revision_serialization_carries_new_identity_and_legacy_pointer(self, manager_cls):
        manager = manager_cls.return_value
        manager.save_content_body_version.return_value = {
            "body_version_id": "version-1",
            "body_sha256": "a" * 64,
            "body_schema_version": 1,
        }
        manager.save_blog_revision_body.return_value = "legacy-pointer"
        page = BlogPage(title="版本测试", body=[])

        data = page.serializable_data()

        self.assertEqual(data["mongo_body_version_id"], "version-1")
        self.assertEqual(data["body_sha256"], "a" * 64)
        self.assertEqual(data["body_schema_version"], 1)
        self.assertEqual(data["mongo_draft_pointer"], "legacy-pointer")
