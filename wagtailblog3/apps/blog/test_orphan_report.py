"""Mongo 孤儿扫描命令的只读分类回归测试。"""

from django.core import management
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from blog.management.commands.orphan_report import Command


class OrphanReportClassificationTests(SimpleTestCase):
    """验证分类只依赖页面编号和指针集合，不读取正文。"""

    def setUp(self) -> None:
        self.command = Command()
        self.context = {
            "page_ids": {10},
            "modern_refs": {"body-live"},
            "legacy_refs": set(),
            "revision_refs": set(),
            "cleanup_refs": set(),
            "deletion_pending_pages": {20},
            "deletion_refs": set(),
            "search_refs": set(),
            "tombstone_pages": {30},
        }

    def test_existing_page_is_not_candidate(self) -> None:
        category, page_id = self.command._classify_document(
            "content_body_versions",
            {"_id": "mongo-1", "aggregate_id": "10", "body_version_id": "body-live", "body": "secret"},
            self.context,
        )
        self.assertEqual((category, page_id), ("referenced_page", 10))

    def test_pending_deletion_page_is_retained_for_worker(self) -> None:
        category, page_id = self.command._classify_document(
            "blog_page_revision_bodies", {"_id": "rev-1", "page_id": 20}, self.context
        )
        self.assertEqual((category, page_id), ("referenced_pending", 20))

    def test_tombstoned_missing_page_is_orphan_candidate(self) -> None:
        category, page_id = self.command._classify_document(
            "blog_content", {"_id": "legacy-1", "page_id": 30}, self.context
        )
        self.assertEqual((category, page_id), ("orphan_candidate", 30))

    def test_unknown_page_is_blocked(self) -> None:
        category, page_id = self.command._classify_document(
            "blog_content", {"_id": "legacy-2", "page_id": 40}, self.context
        )
        self.assertEqual((category, page_id), ("blocked_unknown", 40))

    def test_apply_is_rejected_before_any_database_access(self) -> None:
        with self.assertRaises(CommandError):
            management.call_command("orphan_report", apply=True)
