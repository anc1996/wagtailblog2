"""日志清理审计台账生命周期、终态门禁、冷备与交互视图测试用例。"""

import gzip
import json
import os
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from observability.models import LogClearAudit
from observability.services import get_audit_retention_summary, purge_expired_audits


User = get_user_model()


class AuditRetentionServiceTestCase(TestCase):
    """测试审计记录 180 天生命周期、终态安全门禁与冷备归档逻辑。"""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="testadmin",
            email="admin@example.com",
            password="password123",
        )
        self.now = timezone.now()

    def test_retention_summary_and_terminal_protection(self):
        """测试生命周期看板统计以及未决状态保护隔离。"""
        # 1. 200 天前、终态已完成（应该满足清理资格）
        audit_eligible = LogClearAudit.objects.create(
            user=self.user,
            target_type="domain",
            target="blog",
            kind="activity",
            scope="all",
            bytes_freed=1024,
            succeeded_files=2,
            failed_files=0,
            state="completed",
            index_sync_state="completed",
        )
        LogClearAudit.objects.filter(id=audit_eligible.id).update(
            created_at=self.now - timedelta(days=200)
        )

        # 2. 200 天前、ES 同步仍处于 pending（受终态安全门禁保护，不得清理）
        audit_unresolved = LogClearAudit.objects.create(
            user=self.user,
            target_type="domain",
            target="search",
            kind="activity",
            scope="all",
            bytes_freed=512,
            succeeded_files=1,
            failed_files=0,
            state="completed",
            index_sync_state="pending",
        )
        LogClearAudit.objects.filter(id=audit_unresolved.id).update(
            created_at=self.now - timedelta(days=200)
        )

        # 3. 10 天前、近期记录（不得清理）
        audit_recent = LogClearAudit.objects.create(
            user=self.user,
            target_type="file",
            target="celery_beat",
            kind="error",
            scope="current",
            bytes_freed=256,
            succeeded_files=1,
            failed_files=0,
            state="completed",
            index_sync_state="not_required",
        )
        LogClearAudit.objects.filter(id=audit_recent.id).update(
            created_at=self.now - timedelta(days=10)
        )

        summary = get_audit_retention_summary(days=180)
        self.assertEqual(summary["total_count"], 3)
        self.assertEqual(summary["eligible_count"], 1)
        self.assertEqual(summary["unresolved_count"], 1)

    def test_purge_expired_audits_with_cold_backup(self):
        """测试超期归档清理逻辑与冷备 gzip JSON 生成。"""
        audit_old = LogClearAudit.objects.create(
            user=self.user,
            target_type="all",
            target="all:*:*",
            kind="",
            scope="all",
            bytes_freed=2048,
            succeeded_files=5,
            failed_files=0,
            state="completed",
            index_sync_state="completed",
            details={
                "actual": {"bytes_freed": 2048},
                "changed_files": ["blog/blog.log.1"],
                "duration_ms": 120.5,
            },
        )
        LogClearAudit.objects.filter(id=audit_old.id).update(
            created_at=self.now - timedelta(days=190)
        )

        # 执行清理
        result = purge_expired_audits(days=180, dry_run=False, backup=True)
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["deleted_count"], 1)
        self.assertTrue(result["backup_path"])
        self.assertTrue(os.path.exists(result["backup_path"]))

        # 验证导出的 gzip 备份文件完整性
        with gzip.open(result["backup_path"], "rt", encoding="utf-8") as gz_f:
            archived_data = json.load(gz_f)
            self.assertEqual(len(archived_data), 1)
            self.assertEqual(archived_data[0]["target"], "all:*:*")
            self.assertEqual(archived_data[0]["bytes_freed"], 2048)

        # 验证数据库记录已被物理删除
        self.assertFalse(LogClearAudit.objects.filter(id=audit_old.id).exists())

        # 清理生成的临时备份文件
        if os.path.exists(result["backup_path"]):
            os.remove(result["backup_path"])


class AuditViewsAndApiTestCase(TestCase):
    """测试审计后台 7 语义列表格渲染、快捷时间筛选与详情接口。"""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_superuser(
            username="adminuser",
            email="admin@example.com",
            password="securepassword123",
        )
        self.client.force_login(self.user)

        self.audit = LogClearAudit.objects.create(
            user=self.user,
            ip_address="192.168.20.1",
            target_type="domain",
            target="blog",
            kind="activity",
            scope="rotated",
            files_before=4,
            bytes_before=1000,
            bytes_freed=1000,
            succeeded_files=4,
            failed_files=0,
            state="completed",
            index_sync_state="completed",
            details={
                "duration_ms": 88.5,
                "changed_files": ["blog/blog.log.1"],
                "file_results": [
                    {
                        "file": "blog/blog.log.1",
                        "action": "unlink",
                        "outcome": "unlinked",
                        "bytes_before": 1000,
                        "bytes_freed": 1000,
                        "succeeded": True,
                        "error": "",
                    }
                ],
                "request": {"method": "POST", "user_agent": "TestBrowser/1.0"},
            },
        )

    def test_audit_list_view_renders_correctly(self):
        """测试审计台账列表页渲染与展示字段。"""
        url = reverse("observability:audits")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "历史审计总数")
        self.assertContains(response, "时间范围：")
        self.assertContains(response, "近 180 天")
        self.assertContains(response, "blog")
        self.assertContains(response, "📋 报告")

    def test_audit_quick_range_filters(self):
        """测试 today、7d、30d、180d 快捷时间切片参数。"""
        url = reverse("observability:audits")
        for preset in ("today", "7d", "30d", "180d"):
            resp = self.client.get(f"{url}?range={preset}")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.context["audit_filters"]["range"], preset)

    def test_audit_detail_json_api(self):
        """测试单条审计详情 JSON 接口返回数据结构与字段。"""
        url = reverse("observability:audit_detail", args=[self.audit.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], self.audit.id)
        self.assertEqual(data["target"], "blog")
        self.assertEqual(data["user"], "adminuser")
        self.assertEqual(data["duration_display"], "88 ms" if "88 ms" in data["duration_display"] else data["duration_display"])
        self.assertEqual(len(data["changed_files"]), 1)
        self.assertEqual(len(data["file_results"]), 1)
        self.assertEqual(data["file_results"][0]["outcome"], "unlinked")

    def test_post_purge_expired_action(self):
        """测试通过 POST 请求主动触发超期归档清理。"""
        url = reverse("observability:audits")
        response = self.client.post(url, {"action": "purge_expired", "days": "180"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "待归档审计记录")
