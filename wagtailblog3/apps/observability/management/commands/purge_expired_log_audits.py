"""清理超期系统日志清理审计记录的管理命令。"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from observability.services import (
    DEFAULT_PURGE_BATCH_SIZE,
    DEFAULT_RETENTION_DAYS,
    get_audit_retention_summary,
    purge_expired_audits,
)


class Command(BaseCommand):
    """清理超过保留期限的日志清理审计记录与关联 Outbox 任务。"""

    help = "清理超过保留期限（默认 180 天）的日志清理审计记录，支持演练与冷存储归档。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=f"审计记录保留天数（默认: {DEFAULT_RETENTION_DAYS} 天）",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_PURGE_BATCH_SIZE,
            help=f"每次事务删除的批次大小（默认: {DEFAULT_PURGE_BATCH_SIZE}）",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="仅输出待清理记录统计与明细，不执行真实删除",
        )
        parser.add_argument(
            "--no-backup",
            action="store_true",
            help="禁用删除前的冷存储 gzip JSON 导出备份",
        )

    def handle(self, *args, **options):
        days: int = options["days"]
        batch_size: int = options["batch_size"]
        dry_run: bool = options["dry_run"]
        backup: bool = not options["no_backup"]

        summary = get_audit_retention_summary(days=days)
        self.stdout.write(
            f"审计记录生命周期状态：总数 {summary['total_count']} 条，"
            f"最早记录: {summary['oldest_date'] or '无'}，"
            f"截止时间 ({days} 天前): {summary['cutoff_date']:%Y-%m-%d %H:%M:%S}"
        )
        self.stdout.write(
            f"满足终态可清理记录: {summary['eligible_count']} 条，"
            f"未决受保护记录: {summary['unresolved_count']} 条"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("已启用 --dry-run 演练模式，未执行任何物理删除。"))
            return

        result = purge_expired_audits(
            days=days,
            batch_size=batch_size,
            dry_run=False,
            backup=backup,
        )

        if result["backup_path"]:
            self.stdout.write(self.style.SUCCESS(f"冷存储归档已保存至: {result['backup_path']}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"清理完成：匹配 {result['matched_count']} 条，"
                f"成功删除 {result['deleted_count']} 条，"
                f"分 {result['batches']} 个批次执行。"
            )
        )
