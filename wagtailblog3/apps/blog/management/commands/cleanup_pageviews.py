"""安全清理过期的访问与订阅短期明细。"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from blog.models import ArticleEngagementSession, FeedClientDaily, PageView


class Command(BaseCommand):
    help = "预览或分批删除过期访问明细；每日聚合统计永不删除。"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="PageView 保留天数，默认30。")
        parser.add_argument("--batch-size", type=int, default=500, help="每批删除行数，默认500。")
        parser.add_argument("--dry-run", action="store_true", help="只输出目标数量，不删除。")
        parser.add_argument("--confirm", action="store_true", help="确认执行删除；未提供时默认预览。")

    def handle(self, *args, **options):
        days = options["days"]
        batch_size = options["batch_size"]
        if days < 1 or batch_size < 1:
            raise CommandError("--days 和 --batch-size 必须为正整数。")
        execute = options["confirm"] and not options["dry_run"]
        today = timezone.localdate()
        targets = (
            ("PageView", PageView.objects.filter(date__lt=today - timedelta(days=days))),
            ("ArticleEngagementSession", ArticleEngagementSession.objects.filter(date__lt=today - timedelta(days=7))),
            ("FeedClientDaily", FeedClientDaily.objects.filter(date__lt=today - timedelta(days=35))),
        )
        for label, queryset in targets:
            count = queryset.count()
            self.stdout.write(f"{label}: {count} 条符合清理条件。")
            if not execute:
                continue
            deleted = 0
            while True:
                ids = list(queryset.values_list("pk", flat=True)[:batch_size])
                if not ids:
                    break
                deleted += queryset.model.objects.filter(pk__in=ids).delete()[0]
            self.stdout.write(self.style.SUCCESS(f"{label}: 已删除 {deleted} 条短期明细。"))
        if not execute:
            self.stdout.write(self.style.WARNING("未提供 --confirm，本次仅预览，不会删除任何数据。"))
