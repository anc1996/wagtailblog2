"""为旧 PageView 明细填充不可碰撞的历史访客标识。"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from blog.models import PageView


class Command(BaseCommand):
    help = "预览或分批填充旧 PageView.visitor_key；默认不写入。"

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if batch_size < 1:
            raise CommandError("--batch-size 必须为正整数。")
        if options["dry_run"] and options["confirm"]:
            raise CommandError("--dry-run 与 --confirm 不能同时使用。")
        queryset = PageView.objects.filter(Q(visitor_key__isnull=True) | Q(visitor_key="")).order_by("pk")
        total = queryset.count()
        self.stdout.write(f"待填充旧访问记录：{total} 条。")
        if not options["confirm"]:
            self.stdout.write(self.style.WARNING("未提供 --confirm，没有修改任何数据。"))
            return

        updated = 0
        last_pk = 0
        while True:
            rows = list(queryset.filter(pk__gt=last_pk)[:batch_size])
            if not rows:
                break
            for row in rows:
                # 每条旧记录使用自身主键，保持唯一约束且不伪造历史访客关系。
                row.visitor_key = f"legacy:{row.pk}"
            PageView.objects.bulk_update(rows, ["visitor_key"], batch_size=batch_size)
            updated += len(rows)
            last_pk = rows[-1].pk
        self.stdout.write(self.style.SUCCESS(f"已填充 {updated} 条旧访问记录。"))
