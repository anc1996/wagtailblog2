"""为测试数据库生成可重复查看的内容分析演示聚合。"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone
from wagtail.models import Site

from blog.models import (
    BlogPage,
    FeedRequestDaily,
    PageTrafficSourceDaily,
    PageViewCount,
)


class Command(BaseCommand):
    help = "为现有公开文章补充测试用分析聚合；默认只预览，且拒绝非测试数据库。"

    def add_arguments(self, parser):
        parser.add_argument("--confirm", action="store_true", help="确认写入测试数据库")
        parser.add_argument("--days", type=int, default=30, help="生成最近多少天，默认30天")
        parser.add_argument("--pages", type=int, default=30, help="最多使用多少篇公开文章，默认30篇")

    def handle(self, *args, **options):
        database_name = str(connection.settings_dict.get("NAME", ""))
        if "test" not in database_name.lower():
            raise CommandError("该命令只允许写入名称包含 test 的测试数据库。")

        days = options["days"]
        page_limit = options["pages"]
        if not 1 <= days <= 90 or not 1 <= page_limit <= 100:
            raise CommandError("--days 必须为1到90，--pages 必须为1到100。")

        pages = list(
            BlogPage.objects.live()
            .public()
            .prefetch_related("authors", "tags")
            .order_by("pk")[:page_limit]
        )
        if not pages:
            self.stdout.write(self.style.WARNING("测试库没有公开 BlogPage，未生成数据。"))
            return

        projected = len(pages) * days
        if not options["confirm"]:
            self.stdout.write(
                f"预览：将使用 {len(pages)} 篇现有公开文章，检查最近 {days} 天，"
                f"最多补充 {projected} 条文章日聚合。"
            )
            self.stdout.write("未写入数据；确认后请增加 --confirm。")
            return

        created_counts = 0
        created_sources = 0
        created_feeds = 0
        today = timezone.localdate()
        source_categories = ("direct", "internal", "search", "social", "referral")

        with transaction.atomic():
            for day_offset in range(days):
                date = today - timedelta(days=day_offset)
                for page_index, page in enumerate(pages):
                    seed = page_index * 17 + day_offset * 11
                    views = 35 + seed % 260
                    visitors = max(1, int(views * (0.48 + (seed % 15) / 100)))
                    engaged = int(visitors * (0.38 + (seed % 18) / 100))
                    reached_50 = int(visitors * (0.52 + (seed % 16) / 100))
                    reached_90 = int(visitors * (0.24 + (seed % 14) / 100))
                    aggregate, created = PageViewCount.objects.get_or_create(
                        page=page,
                        date=date,
                        defaults={
                            "view_count_v2": views,
                            "unique_visitor_count_v2": visitors,
                            "engaged_visitor_count": min(engaged, visitors),
                            "scroll_50_visitor_count": min(reached_50, visitors),
                            "scroll_90_visitor_count": min(reached_90, visitors),
                            "active_reading_seconds": engaged * (48 + seed % 90),
                            "v2_started_at": timezone.now(),
                        },
                    )
                    created_counts += int(created)

                    # 已有来源聚合时保持原数据；缺失时用单一分类补齐，确保来源总数不超过文章聚合。
                    if not PageTrafficSourceDaily.objects.filter(page=page, date=date).exists():
                        PageTrafficSourceDaily.objects.create(
                            page=page,
                            date=date,
                            source_category=source_categories[seed % len(source_categories)],
                            view_count=aggregate.view_count_v2,
                            unique_visitor_count=aggregate.unique_visitor_count_v2,
                        )
                        created_sources += 1

            created_feeds = self._create_feed_demo(pages, days, today)

        self.stdout.write(
            self.style.SUCCESS(
                f"演示数据已补充：文章日聚合 {created_counts} 条，来源聚合 "
                f"{created_sources} 条，Feed聚合 {created_feeds} 条。"
            )
        )

    @staticmethod
    def _create_feed_demo(pages, days, today):
        """按全站、作者和标签范围补充RSS/Atom兴趣数据。"""

        site = Site.objects.filter(is_default_site=True).first() or pages[0].get_site()
        if site is None:
            return 0
        locale = pages[0].locale
        authors = {}
        tags = {}
        for page in pages:
            for author in page.authors.all():
                authors.setdefault(author.pk, author)
            for tag in page.tags.all():
                tags.setdefault(tag.pk, tag)

        scopes = [("global", 0, "", "")]
        scopes.extend(
            ("author", author.pk, author.slug, author.name)
            for author in list(authors.values())[:5]
        )
        scopes.extend(
            ("tag", tag.pk, tag.slug, tag.name)
            for tag in list(tags.values())[:5]
        )

        created_count = 0
        for day_offset in range(days):
            date = today - timedelta(days=day_offset)
            for scope_index, (scope_type, scope_id, slug, label) in enumerate(scopes):
                for format_index, feed_format in enumerate(("rss", "atom")):
                    seed = day_offset * 13 + scope_index * 7 + format_index * 3
                    responses_200 = 8 + seed % 70
                    responses_304 = 3 + seed % 42
                    _, created = FeedRequestDaily.objects.get_or_create(
                        site=site,
                        locale=locale,
                        date=date,
                        scope_type=scope_type,
                        scope_id=scope_id,
                        feed_format=feed_format,
                        defaults={
                            "scope_slug": slug,
                            "scope_label": label,
                            "response_200_count": responses_200,
                            "response_304_count": responses_304,
                            "estimated_client_count": max(1, int((responses_200 + responses_304) * 0.42)),
                        },
                    )
                    created_count += int(created)
        return created_count
