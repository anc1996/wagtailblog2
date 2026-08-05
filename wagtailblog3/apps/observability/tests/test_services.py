"""验证日志概览、清理执行和幂等审计服务。"""

import tempfile
import uuid
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from wagtail.models import ModelLogEntry

from observability.models import LogClearAudit
from observability.registry import LOG_FILE_BY_KEY
from observability.services import (
    OVERVIEW_CACHE_KEY,
    clear_and_audit,
    clear_logs,
    describe_clear,
    get_overview,
    select_clear_specs,
)


@override_settings(ELASTICSEARCH_LOGGING={"ENABLED": False})
class LogClearServiceTests(TestCase):
    """验证日志清理服务、预览结果和幂等审计记录。"""
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "blog").mkdir()
        self.spec = LOG_FILE_BY_KEY["blog_error"]
        cache.delete(OVERVIEW_CACHE_KEY)

    def test_clears_current_file_and_removes_rotations(self):
        current = self.root / self.spec.relative_path
        current.write_text("current", encoding="utf-8")
        Path(f"{current}.1").write_text("old", encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            result = clear_logs((self.spec,), "all")
        self.assertTrue(current.exists())
        self.assertEqual(current.stat().st_size, 0)
        self.assertFalse(Path(f"{current}.1").exists())
        self.assertEqual(result.files_before, 2)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.file_results[0]["action"], "truncate")
        self.assertTrue(result.file_results[0]["inode_preserved"])
        self.assertEqual(result.file_results[1]["outcome"], "unlinked")

    def test_preview_separates_current_rotated_and_each_rotation(self):
        current = self.root / self.spec.relative_path
        current.write_bytes(b"current")
        Path(f"{current}.1").write_bytes(b"one")
        Path(f"{current}.2").write_bytes(b"two-two")
        with override_settings(LOG_DIR=self.root):
            preview = describe_clear(
                (self.spec,),
                "all",
                target_type="file",
                target=self.spec.key,
                kind="error",
            )
        self.assertEqual(preview["current"], {"file_count": 1, "total_bytes": 7})
        self.assertEqual(preview["rotated"], {"file_count": 2, "total_bytes": 10})
        self.assertEqual(preview["total"], {"file_count": 3, "total_bytes": 17})
        self.assertEqual(preview["rotations"][0]["total_bytes"], 3)
        self.assertEqual(preview["rotations"][1]["total_bytes"], 7)

    def test_rotation_between_stat_and_open_never_truncates_new_current(self):
        current = self.root / self.spec.relative_path
        current.write_text("old-current", encoding="utf-8")
        rotated = Path(f"{current}.1")
        real_open = os.open

        def rotate_then_open(path, flags):
            current.rename(rotated)
            current.write_text("new-current", encoding="utf-8")
            return real_open(path, flags)

        with override_settings(LOG_DIR=self.root), patch(
            "observability.cleanup.os.open", side_effect=rotate_then_open
        ):
            result = clear_logs((self.spec,), "current")

        self.assertFalse(result.succeeded)
        self.assertEqual(current.read_text(encoding="utf-8"), "new-current")
        self.assertEqual(rotated.read_text(encoding="utf-8"), "old-current")
        self.assertIn("已被替换", result.failed_files[0]["error"])

    def test_rotation_replacement_before_unlink_is_not_deleted(self):
        current = self.root / self.spec.relative_path
        current.write_text("current", encoding="utf-8")
        rotated = Path(f"{current}.1")
        rotated.write_text("old-rotation", encoding="utf-8")
        from observability import cleanup

        real_lstat = cleanup._safe_lstat
        calls = 0

        def replace_on_second_lstat(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.unlink()
                path.write_text("new-rotation", encoding="utf-8")
            return real_lstat(path)

        with override_settings(LOG_DIR=self.root), patch(
            "observability.cleanup._safe_lstat", side_effect=replace_on_second_lstat
        ):
            result = clear_logs((self.spec,), "rotated")

        self.assertFalse(result.succeeded)
        self.assertEqual(rotated.read_text(encoding="utf-8"), "new-rotation")
        self.assertIn("已被替换", result.failed_files[0]["error"])

    def test_rotation_replacement_at_atomic_isolation_is_restored_not_deleted(self):
        current = self.root / self.spec.relative_path
        current.write_text("current", encoding="utf-8")
        rotated = Path(f"{current}.1")
        rotated.write_text("old-rotation", encoding="utf-8")
        displaced = self.root / "displaced-old-rotation.log"
        real_rename = os.rename

        def replace_then_isolate(source, destination):
            real_rename(source, displaced)
            Path(source).write_text("new-rotation", encoding="utf-8")
            real_rename(source, destination)

        with override_settings(LOG_DIR=self.root), patch(
            "observability.cleanup.os.rename", side_effect=replace_then_isolate
        ):
            result = clear_logs((self.spec,), "rotated")

        self.assertFalse(result.succeeded)
        self.assertEqual(rotated.read_text(encoding="utf-8"), "new-rotation")
        self.assertEqual(displaced.read_text(encoding="utf-8"), "old-rotation")
        self.assertIn("已拒绝删除", result.failed_files[0]["error"])

    def test_cleanup_rejects_symlink_without_touching_target(self):
        current = self.root / self.spec.relative_path
        outside = self.root / "outside.log"
        outside.write_text("do not clear", encoding="utf-8")
        current.symlink_to(outside)

        with override_settings(LOG_DIR=self.root):
            result = clear_logs((self.spec,), "current")

        self.assertFalse(result.succeeded)
        self.assertTrue(current.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "do not clear")
        self.assertIn("符号链接", result.failed_files[0]["error"])

    def test_cleanup_never_deletes_rotation_lock_files(self):
        current = self.root / self.spec.relative_path
        current.write_text("current", encoding="utf-8")
        Path(f"{current}.1").write_text("rotation", encoding="utf-8")
        lock_file = Path(f"{current}.lock")
        rotation_lock = Path(f"{current}.1.lock")
        lock_file.write_text("lock", encoding="utf-8")
        rotation_lock.write_text("rotation lock", encoding="utf-8")

        with override_settings(LOG_DIR=self.root):
            result = clear_logs((self.spec,), "all")

        self.assertTrue(result.succeeded)
        self.assertTrue(lock_file.exists())
        self.assertTrue(rotation_lock.exists())
        self.assertEqual(lock_file.read_text(encoding="utf-8"), "lock")
        self.assertEqual(rotation_lock.read_text(encoding="utf-8"), "rotation lock")

    def test_open_writer_continues_after_in_place_truncate(self):
        current = self.root / self.spec.relative_path
        current.write_text("before\n", encoding="utf-8")
        inode_before = current.stat().st_ino
        with current.open("a", encoding="utf-8") as writer:
            with override_settings(LOG_DIR=self.root):
                clear_logs((self.spec,), "current")
            writer.write("after\n")
            writer.flush()
        self.assertEqual(current.stat().st_ino, inode_before)
        self.assertEqual(current.read_text(encoding="utf-8"), "after\n")

    def test_subprocess_writer_continues_after_in_place_truncate(self):
        current = self.root / self.spec.relative_path
        writer_code = (
            "import sys; "
            "stream=open(sys.argv[1], 'a', encoding='utf-8'); "
            "stream.write('before\\n'); stream.flush(); "
            "print('ready', flush=True); sys.stdin.readline(); "
            "stream.write('after\\n'); stream.flush(); stream.close()"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", writer_code, str(current)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        inode_before = current.stat().st_ino
        with override_settings(LOG_DIR=self.root):
            clear_logs((self.spec,), "current")
        process.communicate("continue\n", timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(current.stat().st_ino, inode_before)
        self.assertEqual(current.read_text(encoding="utf-8"), "after\n")

    def test_rotated_scope_keeps_current_file(self):
        current = self.root / self.spec.relative_path
        current.write_text("current", encoding="utf-8")
        Path(f"{current}.1").write_text("rotated", encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            clear_logs((self.spec,), "rotated")
        self.assertEqual(current.read_text(encoding="utf-8"), "current")
        self.assertFalse(Path(f"{current}.1").exists())

    def test_all_controlled_target_granularities_resolve_registry_entries(self):
        self.assertEqual(select_clear_specs("file", "blog_error"), (self.spec,))
        self.assertTrue(select_clear_specs("domain", "blog"))
        self.assertTrue(all(spec.kind == "error" for spec in select_clear_specs("domain", "blog", "error")))
        self.assertTrue(all(spec.business for spec in select_clear_specs("business", "")))
        self.assertGreater(len(select_clear_specs("all", "")), len(select_clear_specs("business", "")))
        self.assertEqual(select_clear_specs("file", "/etc/passwd"), ())

    def test_audit_is_idempotent(self):
        user = get_user_model().objects.create_user(username="operator", password="test")
        current = self.root / self.spec.relative_path
        current.write_text("current", encoding="utf-8")
        key = uuid.uuid4()
        with override_settings(LOG_DIR=self.root):
            first, first_executed = clear_and_audit(
                user=user,
                ip_address="127.0.0.1",
                idempotency_key=key,
                target="file:blog_error:*",
                scope="current",
                specs=(self.spec,),
            )
            second, second_executed = clear_and_audit(
                user=user,
                ip_address="127.0.0.1",
                idempotency_key=key,
                target="file:blog_error:*",
                scope="current",
                specs=(self.spec,),
            )
        self.assertTrue(first_executed)
        self.assertFalse(second_executed)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(LogClearAudit.objects.count(), 1)
        self.assertEqual(first.details["spec_keys"], [self.spec.key])
        self.assertEqual(first.details["file_results"][0]["outcome"], "truncated")
        self.assertIn("duration_ms", first.details)
        entry = ModelLogEntry.objects.get(action="observability.clear_logs")
        self.assertEqual(entry.user, user)
        self.assertEqual(entry.data["audit_id"], first.pk)

    def test_unexpected_failure_finishes_audit_and_keeps_idempotency_claim(self):
        user = get_user_model().objects.create_user(username="claim-operator", password="test")
        key = uuid.uuid4()
        with override_settings(LOG_DIR=self.root), patch(
            "observability.services.clear_logs"
        ) as mocked_clear:
            mocked_clear.side_effect = RuntimeError("模拟进程中断")
            first, first_executed = clear_and_audit(
                user=user,
                ip_address="127.0.0.1",
                idempotency_key=key,
                target="file:blog_error:*",
                scope="current",
                specs=(self.spec,),
            )
            second, second_executed = clear_and_audit(
                user=user,
                ip_address="127.0.0.1",
                idempotency_key=key,
                target="file:blog_error:*",
                scope="current",
                specs=(self.spec,),
            )
        audit = LogClearAudit.objects.get(idempotency_key=key)
        self.assertTrue(first_executed)
        self.assertFalse(second_executed)
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(audit.succeeded)
        self.assertEqual(audit.details["state"], "failed")
        self.assertEqual(audit.details["error_type"], "RuntimeError")

    def test_file_cleanup_does_not_delete_database_audit(self):
        audit = LogClearAudit.objects.create(
            target="all:*:*", scope="all", succeeded=True
        )
        current = self.root / self.spec.relative_path
        current.write_text("runtime log", encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            clear_logs((self.spec,), "all")
        self.assertTrue(LogClearAudit.objects.filter(pk=audit.pk).exists())

    def test_overview_counts_recent_error_warning_and_uses_cache(self):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        (self.root / "blog/blog_error.log").write_text(
            f"[{timestamp}] ERROR [blog.views:run:10] [pid=1 thread=MainThread] recent error\n",
            encoding="utf-8",
        )
        (self.root / "blog/blog.log").write_text(
            f"[{timestamp}] WARNING [blog.views:run:11] [pid=1 thread=MainThread] recent warning\n",
            encoding="utf-8",
        )
        with override_settings(LOG_DIR=self.root):
            first = get_overview()
            (self.root / "blog/blog_error.log").write_text("", encoding="utf-8")
            cached = get_overview()
        self.assertEqual(first["error_count"], 1)
        self.assertEqual(first["warning_count"], 1)
        self.assertEqual(cached["error_count"], 1)

    def test_clear_invalidates_overview_cache(self):
        cache.set(OVERVIEW_CACHE_KEY, {"stale": True}, timeout=30)
        current = self.root / self.spec.relative_path
        current.write_text("runtime log", encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            clear_logs((self.spec,), "current")
        self.assertIsNone(cache.get(OVERVIEW_CACHE_KEY))
