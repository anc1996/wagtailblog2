"""MongoDB 孤儿正文数据治理与安全清理测试套件。

覆盖：
1. 孤儿扫描与交叉分类逻辑；
2. 正文 Body 反解析预览（标题提取、字数统计、Markdown重构）；
3. 强阻断 Fencing Token 并发防误删熔断；
4. 物理清理原子执行与审计日志；
5. Web 后台视图与 API 鉴权拦截；
6. CLI 管理命令 --apply --yes 动力对齐。
"""

import json
from unittest.mock import MagicMock, patch

from bson import ObjectId
from django.contrib.auth import get_user_model
from django.core import management
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django.urls import reverse

from blog.services.mongo_orphan import MongoOrphanService

User = get_user_model()


class MongoOrphanServiceTests(TestCase):
    """测试孤儿正文服务层的核心逻辑。"""

    def setUp(self) -> None:
        self.context = {
            "page_ids": {100},
            "modern_refs": {"modern-ver-1"},
            "legacy_refs": set(),
            "revision_refs": {"rev-ver-2"},
            "cleanup_refs": set(),
            "deletion_pending_pages": {200},
            "deletion_refs": set(),
            "tombstone_pages": {300},
        }

    def test_classify_referenced_page(self) -> None:
        """活跃页面引用必须被最高优先级保护。"""
        cat, pid = MongoOrphanService.classify_document(
            "content_body_versions",
            {"_id": "v1", "aggregate_id": "100", "body_version_id": "v1"},
            self.context,
        )
        self.assertEqual(cat, "referenced_page")
        self.assertEqual(pid, 100)

    def test_classify_deletion_pending(self) -> None:
        """正在异步编排删除中的页面正文由 Worker 接管，不能人工直接清理。"""
        cat, pid = MongoOrphanService.classify_document(
            "blog_page_revision_bodies",
            {"_id": "r1", "page_id": 200},
            self.context,
        )
        self.assertEqual(cat, "referenced_pending")
        self.assertEqual(pid, 200)

    def test_classify_historical_missing_page(self) -> None:
        """页面在 MySQL 物理删除，但指针仍在历史 Revision 中保留。"""
        cat, pid = MongoOrphanService.classify_document(
            "content_body_versions",
            {"_id": "v2", "aggregate_id": "999", "body_version_id": "rev-ver-2"},
            self.context,
        )
        self.assertEqual(cat, "referenced_missing_page")
        self.assertEqual(pid, 999)

    def test_classify_orphan_candidate(self) -> None:
        """页面在墓碑清单中且无任何指针引用，方为完全孤儿。"""
        cat, pid = MongoOrphanService.classify_document(
            "blog_content",
            {"_id": "b1", "page_id": 300},
            self.context,
        )
        self.assertEqual(cat, "orphan_candidate")
        self.assertEqual(pid, 300)

    def test_body_preview_parser_markdown_string(self) -> None:
        """测试纯文本 Markdown 反解析与首行标题提取。"""
        raw_body = "# 深度剖析文章标题\n\n这里是正文段落，介绍详细背景知识。"
        text, title, count, types = MongoOrphanService._parse_body_content(raw_body)
        self.assertEqual(title, "深度剖析文章标题")
        self.assertIn("这里是正文段落", text)
        self.assertEqual(count, 1)
        self.assertEqual(types, ["raw_text"])

    def test_body_preview_parser_streamfield_blocks(self) -> None:
        """测试 StreamField 块结构的反解析与富文本标题提纯。"""
        blocks = [
            {"type": "rich_text", "value": "<p><h2>架构设计原则</h2></p>"},
            {"type": "markdown_block", "value": "这是第二段 Markdown 正文。"},
        ]
        text, title, count, types = MongoOrphanService._parse_body_content(blocks)
        self.assertEqual(title, "架构设计原则")
        self.assertIn("这是第二段 Markdown 正文。", text)
        self.assertEqual(count, 2)
        self.assertEqual(types, ["rich_text", "markdown_block"])

    @patch.object(MongoOrphanService, "collect_mysql_context")
    @patch.object(MongoOrphanService, "get_mongo_database")
    def test_fencing_blocks_active_page_deletion(self, mock_get_db, mock_context) -> None:
        """核心防线：若待删文档被活跃页面引用，物理删除必须抛出 PermissionError 熔断。"""
        mock_context.return_value = {"page_ids": {100}, "deletion_pending_pages": set(), "tombstone_pages": set(), "modern_refs": set(), "legacy_refs": set(), "revision_refs": set(), "cleanup_refs": set(), "deletion_refs": set()}
        mock_db = MagicMock()
        mock_db["content_body_versions"].find_one.return_value = {
            "_id": ObjectId("6a9485e1d9c82da1ac2324c8"),
            "aggregate_id": "100",
            "body_version_id": "ver-100",
        }
        mock_get_db.return_value = mock_db

        with self.assertRaises(PermissionError) as cm:
            MongoOrphanService.delete_orphan_document(
                "content_body_versions",
                "6a9485e1d9c82da1ac2324c8",
                actor="test_user",
            )
        self.assertIn("并发防护拦截", str(cm.exception))
        mock_db["content_body_versions"].delete_one.assert_not_called()

    @patch.object(MongoOrphanService, "collect_mysql_context")
    @patch.object(MongoOrphanService, "get_mongo_database")
    def test_delete_historical_missing_page_document_success(self, mock_get_db, mock_context) -> None:
        """主页面已在 MySQL 物理删除的历史快照残留文档，支持受控安全清理。"""
        mock_context.return_value = {
            "page_ids": set(),
            "deletion_pending_pages": set(),
            "tombstone_pages": set(),
            "modern_refs": set(),
            "legacy_refs": set(),
            "revision_refs": {"hist-rev-pointer"},
            "cleanup_refs": set(),
            "deletion_refs": set(),
        }
        mock_db = MagicMock()
        target_oid = ObjectId("6a9485e1d9c82da1ac2324c8")
        mock_db["content_body_versions"].find_one.return_value = {
            "_id": target_oid,
            "aggregate_id": "636",
            "body_version_id": "hist-rev-pointer",
        }
        del_result = MagicMock()
        del_result.deleted_count = 1
        mock_db["content_body_versions"].delete_one.return_value = del_result
        mock_get_db.return_value = mock_db

        res = MongoOrphanService.delete_orphan_document(
            "content_body_versions",
            str(target_oid),
            actor="admin",
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["category"], "referenced_missing_page")
        mock_db["content_body_versions"].delete_one.assert_called_once_with({"_id": target_oid})

    @patch.object(MongoOrphanService, "collect_mysql_context")
    @patch.object(MongoOrphanService, "get_mongo_database")
    def test_delete_orphan_document_success(self, mock_get_db, mock_context) -> None:
        """测试完全孤儿文档的原子删除执行。"""
        mock_context.return_value = {
            "page_ids": set(),
            "deletion_pending_pages": set(),
            "tombstone_pages": {300},
            "modern_refs": set(),
            "legacy_refs": set(),
            "revision_refs": set(),
            "cleanup_refs": set(),
            "deletion_refs": set(),
        }
        mock_db = MagicMock()
        target_oid = ObjectId("6a95008e139b3b8fc0730b1c")
        mock_db["content_body_versions"].find_one.return_value = {
            "_id": target_oid,
            "aggregate_id": "300",
            "body_version_id": "orphan-ver",
        }
        del_result = MagicMock()
        del_result.deleted_count = 1
        mock_db["content_body_versions"].delete_one.return_value = del_result
        mock_get_db.return_value = mock_db

        res = MongoOrphanService.delete_orphan_document(
            "content_body_versions",
            str(target_oid),
            actor="admin",
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["mongo_id"], str(target_oid))
        mock_db["content_body_versions"].delete_one.assert_called_once_with({"_id": target_oid})


class MongoOrphanWebAdminTests(TestCase):
    """测试后台报表面板、预览 API 与清理 API 的路由与权限。"""

    def setUp(self) -> None:
        self.client = Client()
        self.superuser = User.objects.create_superuser(
            username="super_orphan_admin",
            password="adminpassword123",
            email="admin@example.com",
        )
        self.normal_user = User.objects.create_user(
            username="normal_editor",
            password="userpassword123",
            email="editor@example.com",
            is_staff=True,
        )

    def test_anonymous_redirected(self) -> None:
        """匿名用户访问主面板应重定向到登录页。"""
        resp = self.client.get(reverse("mongo_orphans_report"))
        self.assertEqual(resp.status_code, 302)

    def test_normal_editor_forbidden(self) -> None:
        """非超级管理员即使是后台编辑，也无权访问孤儿正文治理面板。"""
        self.client.force_login(self.normal_user)
        resp = self.client.get(reverse("mongo_orphans_report"))
        self.assertEqual(resp.status_code, 302)

    @patch.object(MongoOrphanService, "scan_orphans")
    def test_superuser_access_report_view(self, mock_scan) -> None:
        """超级管理员可以顺利访问报表面板并获取 200。"""
        mock_scan.return_value = {
            "collections": {"content_body_versions": 10},
            "category_counts": {"orphan_candidate": 1},
            "candidate_count": 1,
            "candidates": [{
                "collection": "content_body_versions",
                "mongo_id": "6a95008e139b3b8fc0730b1c",
                "page_id": 300,
                "category": "orphan_candidate",
                "category_label": "完全孤儿",
                "created_at": "2026-08-31T04:18:22+00:00",
                "can_delete": True,
            }],
            "mongo_error": None,
        }
        self.client.force_login(self.superuser)
        resp = self.client.get(reverse("mongo_orphans_report"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Mongo 孤儿正文数据治理")
        self.assertContains(resp, "6a95008e139b3b8fc0730b1c")

    @patch.object(MongoOrphanService, "get_orphan_body_preview")
    def test_preview_api_success(self, mock_preview) -> None:
        """测试正文预览 API 成功返回解析数据。"""
        mock_preview.return_value = {
            "collection": "content_body_versions",
            "mongo_id": "6a95008e139b3b8fc0730b1c",
            "page_id": 300,
            "title_hint": "测试文章标题",
            "markdown_content": "这是反解析出的 Markdown 正文",
            "char_count": 20,
            "block_count": 1,
            "block_types": ["markdown_block"],
            "can_delete": True,
            "orphan_reason": "完全孤儿判定通过",
        }
        self.client.force_login(self.superuser)
        url = reverse("mongo_orphan_preview_api") + "?collection=content_body_versions&mongo_id=6a95008e139b3b8fc0730b1c"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["title_hint"], "测试文章标题")
        self.assertTrue(data["can_delete"])

    @patch.object(MongoOrphanService, "delete_orphan_document")
    def test_cleanup_api_fencing_denied(self, mock_delete) -> None:
        """测试当服务层触发 Fencing 并发拦截时，API 准确返回 403 阻断。"""
        mock_delete.side_effect = PermissionError("并发防护拦截：正文被活跃页面引用")
        self.client.force_login(self.superuser)
        resp = self.client.post(reverse("mongo_orphan_cleanup_api"), {
            "collection": "content_body_versions",
            "mongo_id": "active-doc-id",
        })
        self.assertEqual(resp.status_code, 403)
        self.assertIn("并发防护拦截", resp.json()["error"])

    @patch.object(MongoOrphanService, "delete_orphan_document")
    def test_cleanup_api_success(self, mock_delete) -> None:
        """测试超级管理员正常执行单条清理 API 成功返回。"""
        mock_delete.return_value = {
            "success": True,
            "collection": "content_body_versions",
            "mongo_id": "6a95008e139b3b8fc0730b1c",
            "page_id": 300,
            "deleted_count": 1,
        }
        self.client.force_login(self.superuser)
        resp = self.client.post(reverse("mongo_orphan_cleanup_api"), {
            "collection": "content_body_versions",
            "mongo_id": "6a95008e139b3b8fc0730b1c",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])

    @patch.object(MongoOrphanService, "delete_orphan_document")
    def test_cleanup_api_batch_success(self, mock_delete) -> None:
        """测试超级管理员批量执行清理 API。"""
        mock_delete.side_effect = lambda collection_name, mongo_id, actor: {
            "success": True,
            "collection": collection_name,
            "mongo_id": mongo_id,
        }
        self.client.force_login(self.superuser)
        payload = {
            "items": [
                {"collection": "content_body_versions", "mongo_id": "id-1"},
                {"collection": "content_body_versions", "mongo_id": "id-2"},
            ]
        }
        resp = self.client.post(
            reverse("mongo_orphan_cleanup_api"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["deleted_count"], 2)
        self.assertEqual(mock_delete.call_count, 2)


class MongoOrphanCommandTests(TestCase):
    """测试 orphan_report 命令行命令的动力与受控保护。"""

    def test_apply_without_yes_raises_error(self) -> None:
        """执行 --apply 未带 --yes 时直接阻断。"""
        with self.assertRaises(CommandError) as cm:
            management.call_command("orphan_report", apply=True)
        self.assertIn("必须同时指定 --yes", str(cm.exception))

    @patch.object(MongoOrphanService, "scan_orphans")
    @patch.object(MongoOrphanService, "delete_orphan_document")
    def test_apply_with_yes_executes_orphan_candidate_only(self, mock_delete, mock_scan) -> None:
        """验证 --apply --yes 仅清理完全孤儿 candidate，不误碰其他快照。"""
        mock_scan.return_value = {
            "candidates": [
                {"collection": "content_body_versions", "mongo_id": "orphan-1", "category": "orphan_candidate"},
                {"collection": "content_body_versions", "mongo_id": "ref-1", "category": "referenced_missing_page"},
            ],
            "collections": {},
            "category_counts": {},
            "candidate_count": 2,
            "emitted_count": 2,
            "truncated": False,
            "mongo_error": None,
        }
        mock_delete.return_value = {"success": True, "mongo_id": "orphan-1"}

        management.call_command("orphan_report", apply=True, yes=True)
        mock_delete.assert_called_once_with(
            "content_body_versions",
            "orphan-1",
            actor="CLI:orphan_report",
        )
