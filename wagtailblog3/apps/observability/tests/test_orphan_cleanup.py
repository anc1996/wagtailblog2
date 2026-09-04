"""孤儿轮转临时文件与受保护调度数据库的清理与治理测试。"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse

from observability.cleanup import (
    SAFE_ORPHAN_AGE_SECONDS,
    discover_orphan_rotations,
    execute_cleanup,
    preview_cleanup,
)
from observability.registry import LOG_FILE_BY_KEY
from observability.services import _detect_celery_protected_files, get_overview


@override_settings(ELASTICSEARCH_LOGGING={"ENABLED": False})
class OrphanCleanupTests(TestCase):
    """测试孤儿轮转文件发现、时间窗拦截、调度库保护及清理闭环。"""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

        # 构建受控测试日志目录
        (self.root / "celery").mkdir(parents=True, exist_ok=True)
        (self.root / "blog").mkdir(parents=True, exist_ok=True)
        self.spec = LOG_FILE_BY_KEY["celery_beat"]

        # 创建用于视图测试的超级用户
        self.user = get_user_model().objects.create_superuser(
            username="admin_orphan_test", email="admin@example.com", password="test", is_staff=True
        )
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="wagtailadmin", codename="access_admin")
        )
        self.client.force_login(self.user)

    def test_discover_orphan_rotations_filters_locks_and_foreign_files(self) -> None:
        """验证孤儿轮转发现引擎：精确匹配关联 spec 的轮转与隔离临时文件，严格排除并发锁与其他日志。"""
        current_file = self.root / self.spec.relative_path
        current_file.write_text("current beat log", encoding="utf-8")

        # 标准轮转文件
        rotation_1 = self.root / f"{self.spec.relative_path}.1"
        rotation_1.write_text("rotation 1 content", encoding="utf-8")

        # 孤儿轮转临时文件（多进程竞争或异常退出残留）
        orphan_rotate = self.root / "celery/celery_beat.log.rotate.123456789"
        orphan_rotate.write_text("orphan rotate content", encoding="utf-8")

        # 隔离清理临时文件残留
        orphan_cleanup = self.root / "celery/.celery_beat.log.cleanup-uuid-tmp"
        orphan_cleanup.write_text("orphan cleanup content", encoding="utf-8")

        # 并发锁文件（受系统最高级别保护，绝不得当作孤儿文件）
        lock_file = self.root / "celery/.__celery_beat.lock"
        lock_file.write_text("", encoding="utf-8")

        # 其他无关日志规格的文件
        foreign_file = self.root / "celery/celery_worker.log.rotate.999"
        foreign_file.write_text("other worker orphan", encoding="utf-8")

        with override_settings(LOG_DIR=self.root):
            discovered = discover_orphan_rotations(self.spec, min_age_seconds=0)

        discovered_names = {item["name"] for item in discovered}
        self.assertIn("celery_beat.log.rotate.123456789", discovered_names)
        self.assertIn(".celery_beat.log.cleanup-uuid-tmp", discovered_names)
        self.assertNotIn(".__celery_beat.lock", discovered_names)
        self.assertNotIn("celery_worker.log.rotate.999", discovered_names)
        self.assertNotIn("celery_beat.log.1", discovered_names)
        self.assertNotIn("celery_beat.log", discovered_names)

    def test_orphan_age_protection_window(self) -> None:
        """验证 60 秒写保护时间窗：最近 60 秒内产生的文件标记为不安全并跳过，超过时间窗的安全孤儿标记为可清理。"""
        orphan_recent = self.root / "celery/celery_beat.log.rotate.recent"
        orphan_recent.write_text("recent orphan", encoding="utf-8")

        orphan_aged = self.root / "celery/celery_beat.log.rotate.aged"
        orphan_aged.write_text("aged orphan", encoding="utf-8")
        # 修改 aged 文件的 mtime 为 120 秒前
        past_time = time.time() - 120
        os.utime(orphan_aged, (past_time, past_time))

        with override_settings(LOG_DIR=self.root):
            discovered = discover_orphan_rotations(self.spec, min_age_seconds=SAFE_ORPHAN_AGE_SECONDS)

        info_map = {item["name"]: item for item in discovered}
        self.assertFalse(info_map["celery_beat.log.rotate.recent"]["is_safe"])
        self.assertIn("60 秒", info_map["celery_beat.log.rotate.recent"]["skip_reason"])
        self.assertTrue(info_map["celery_beat.log.rotate.aged"]["is_safe"])
        self.assertEqual(info_map["celery_beat.log.rotate.aged"]["skip_reason"], "")

    def test_execute_cleanup_unlinks_safe_orphans_and_preserves_locks_and_schedules(self) -> None:
        """验证清理执行闭环：安全孤儿文件被隔离并删除，锁文件和 Celery 调度数据库完好无损。"""
        current_file = self.root / self.spec.relative_path
        current_file.write_text("current active beat log\n", encoding="utf-8")
        current_size = current_file.stat().st_size

        rotation_1 = self.root / f"{self.spec.relative_path}.1"
        rotation_1.write_text("rotation 1\n", encoding="utf-8")

        # 孤儿文件，设置 mtime 为 120 秒前
        orphan_file = self.root / "celery/celery_beat.log.rotate.safe_to_clean"
        orphan_file.write_text("safe orphan content to delete\n", encoding="utf-8")
        past_time = time.time() - 120
        os.utime(orphan_file, (past_time, past_time))
        orphan_size = orphan_file.stat().st_size

        # 锁文件
        lock_file = self.root / "celery/.__celery_beat.lock"
        lock_file.write_text("", encoding="utf-8")

        # Celery 调度数据库（模拟生产环境）
        schedule_db = self.root / "celery/celerybeat-schedule"
        schedule_db.write_text("sqlite database header binary simulation", encoding="utf-8")
        schedule_wal = self.root / "celery/celerybeat-schedule-wal"
        schedule_wal.write_text("wal log binary", encoding="utf-8")

        with override_settings(LOG_DIR=self.root):
            result = execute_cleanup((self.spec,), scope="all")

        # 1. 验证主文件原地截断为 0
        self.assertTrue(current_file.exists())
        self.assertEqual(current_file.stat().st_size, 0)

        # 2. 验证轮转与孤儿文件均已彻底删除
        self.assertFalse(rotation_1.exists())
        self.assertFalse(orphan_file.exists())

        # 3. 核心底线：锁文件与调度数据库受保护，绝不被删除
        self.assertTrue(lock_file.exists())
        self.assertTrue(schedule_db.exists())
        self.assertTrue(schedule_wal.exists())

        # 4. 验证结果跟踪
        self.assertIn("celery/celery_beat.log.rotate.safe_to_clean", result.changed_files)
        self.assertTrue(result.succeeded)
        self.assertGreaterEqual(result.bytes_freed, current_size + orphan_size)

    def test_celery_protected_detection_and_overview_integration(self) -> None:
        """验证概览接口集成：调度数据库准确识别、孤儿文件纳入统计与总大小核对。"""
        # 写入调度数据库文件
        schedule_db = self.root / "celery/celerybeat-schedule"
        schedule_db.write_text("schedule db 100 bytes simulation" + "x" * 68, encoding="utf-8")
        schedule_wal = self.root / "celery/celerybeat-schedule-wal"
        schedule_wal.write_text("wal file 50 bytes" + "y" * 33, encoding="utf-8")

        # 写入主日志与孤儿轮转
        current_file = self.root / self.spec.relative_path
        current_file.write_text("active beat", encoding="utf-8")
        orphan_file = self.root / "celery/celery_beat.log.rotate.998877"
        orphan_file.write_text("orphan beat file 20 bytes", encoding="utf-8")

        with override_settings(LOG_DIR=self.root):
            protected = _detect_celery_protected_files()
            overview = get_overview(refresh=True)

        # 1. 验证调度数据库受保护检测
        self.assertEqual(len(protected["files"]), 2)
        protected_names = {item["name"] for item in protected["files"]}
        self.assertEqual(protected_names, {"celerybeat-schedule", "celerybeat-schedule-wal"})
        self.assertEqual(protected["total_bytes"], 100 + 50)

        # 2. 验证概览中的孤儿汇总
        self.assertEqual(overview["orphan_summary"]["count"], 1)
        self.assertEqual(overview["orphan_summary"]["total_bytes"], orphan_file.stat().st_size)
        self.assertEqual(overview["celery_protected"]["total_bytes"], 100 + 50)

        # 3. 验证 Celery 模块在概览中包含调度库保护信息
        celery_module = next(m for m in overview["modules"] if m["key"] == "celery")
        self.assertIn("celery_protected", celery_module)
        self.assertEqual(celery_module["celery_protected"]["total_bytes"], 100 + 50)
        self.assertEqual(celery_module["orphan_count"], 1)

    def test_preview_cleanup_aggregates_orphan_files(self) -> None:
        """验证 preview_cleanup 在 rotated 与 all 模式下汇总孤儿文件并返回明细。"""
        orphan_file = self.root / "celery/celery_beat.log.rotate.preview_test"
        orphan_file.write_text("orphan content for preview", encoding="utf-8")
        orphan_size = orphan_file.stat().st_size

        with override_settings(LOG_DIR=self.root):
            preview_rot = preview_cleanup(
                (self.spec,),
                target_type="file",
                target=self.spec.key,
                kind=self.spec.kind,
                scope="rotated",
            )
            preview_cur = preview_cleanup(
                (self.spec,),
                target_type="file",
                target=self.spec.key,
                kind=self.spec.kind,
                scope="current",
            )

        # 仅当前文件模式不应包含孤儿轮转
        self.assertEqual(preview_cur["orphan"]["file_count"], 0)

        # 轮转模式应包含孤儿文件
        self.assertEqual(preview_rot["orphan"]["file_count"], 1)
        self.assertEqual(preview_rot["orphan"]["total_bytes"], orphan_size)
        self.assertIn(orphan_size, [f["size"] for f in preview_rot["orphan"]["files"]])

    def test_overview_renders_orphan_banner_and_drawer(self) -> None:
        """验证概览页面渲染：当存在孤儿文件时显示醒目黄色横幅、调度库保护徽章与文件清单折叠抽屉。"""
        current_file = self.root / self.spec.relative_path
        current_file.write_text("current active beat log", encoding="utf-8")
        orphan_file = self.root / "celery/celery_beat.log.rotate.112233"
        orphan_file.write_text("orphan beat log content", encoding="utf-8")

        schedule_db = self.root / "celery/celerybeat-schedule"
        schedule_db.write_text("db simulation", encoding="utf-8")

        from observability.services import OVERVIEW_CACHE_KEY
        from django.core.cache import cache
        cache.delete(OVERVIEW_CACHE_KEY)

        with override_settings(LOG_DIR=self.root):
            response = self.client.get(reverse("observability:overview"))

        self.assertEqual(response.status_code, 200)
        # 1. 验证告警横幅
        self.assertContains(response, "检测到 1 个孤儿轮转临时文件")
        self.assertContains(response, "一键清理全部轮转与孤儿文件")

        # 2. 验证 Celery 调度数据库保护徽章
        self.assertContains(response, "调度库保护")

        # 3. 验证文件清单折叠抽屉
        self.assertContains(response, "展开「celery」文件清单")
        self.assertContains(response, "celery_beat.log.rotate.112233")

    def test_clear_confirm_page_renders_orphan_breakdown(self) -> None:
        """验证降级确认页渲染：选择 rotated 范围时，展示孤儿轮转文件列表与安全操作文案。"""
        orphan_file = self.root / "celery/celery_beat.log.rotate.confirm_test"
        orphan_file.write_text("orphan content for confirm", encoding="utf-8")

        with override_settings(LOG_DIR=self.root):
            response = self.client.get(
                reverse("observability:clear"),
                {
                    "target_type": "file",
                    "target": self.spec.key,
                    "kind": self.spec.kind,
                    "scope": "rotated",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "包含 1 个孤儿轮转临时文件")
        self.assertContains(response, "celery/celery_beat.log.rotate.confirm_test")
        self.assertContains(response, "当前主文件将安全原地截断至 0 字节")