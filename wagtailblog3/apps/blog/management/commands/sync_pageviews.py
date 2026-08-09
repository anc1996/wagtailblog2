"""已退役的 Redis 页面计数命令。"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "已退役：访问统计现在由成功响应后的数据库 V2 聚合直接写入。"

    def handle(self, *args, **options):
        raise CommandError(
            "sync_pageviews 已退役，不会读取或删除 Redis 键；请使用页面访问统计 V2。"
        )
