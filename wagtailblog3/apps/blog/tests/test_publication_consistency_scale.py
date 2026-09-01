"""对账服务的可控规模边界测试，不创建百万级持久测试数据。"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from blog.models import BlogPublicationState
from blog.services.publication_consistency import check_blog_publication_consistency
from search.models import (
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchState,
    ContentSearchStatus,
)
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


class BlogPublicationConsistencyScaleTests(BlogLifecycleFixtureMixin, TestCase):
    """验证百万级设计所依赖的批次边界，而不是伪造百万条数据库记录。"""

    def _create_search_projection(self, page, event_count: int = 1) -> None:
        """为页面建立最小 MySQL 投影和有限历史事件，避免访问 Mongo。"""
        BlogPublicationState.objects.update_or_create(
            page_id=page.pk,
            defaults={
                "publication_generation": event_count,
                "published_body_version_id": None,
            },
        )
        ContentSearchState.objects.update_or_create(
            page_id=page.pk,
            defaults={
                "content_version": event_count,
                "desired_operation": ContentSearchOperation.UPSERT,
                "searchable": True,
                "publication_generation": event_count,
            },
        )
        ContentSearchOutbox.objects.bulk_create(
            [
                ContentSearchOutbox(
                    page_id=page.pk,
                    content_version=version,
                    operation=ContentSearchOperation.UPSERT,
                    searchable=True,
                    publication_generation=version,
                    status=ContentSearchStatus.PENDING,
                )
                for version in range(1, event_count + 1)
            ]
        )

    def test_daily_small_batch_has_a_hard_row_limit(self):
        """每天少量新增文章时，对账只读取本批页面，游标交给下一轮继续。"""
        pages = [
            self._create_draft_page(f"daily article {index}")
            for index in range(15)
        ]

        report = check_blog_publication_consistency(
            after_page_id=pages[0].pk - 1,
            limit=10,
            check_mongo=False,
        )

        self.assertEqual(report["scanned"], 10)
        self.assertEqual(report["next_after_page_id"], pages[9].pk)

    def test_high_water_bound_excludes_pages_created_after_cycle_start(self):
        """周期 high-water mark 固定后，新页面应留给下一轮，不能改变当前批次边界。"""
        pages = [self._create_draft_page(f"high water article {index}") for index in range(2)]

        report = check_blog_publication_consistency(
            after_page_id=pages[0].pk - 1,
            upper_bound_page_id=pages[0].pk,
            limit=100,
            check_mongo=False,
        )

        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["next_after_page_id"], pages[0].pk)

    def test_outbox_history_is_loaded_with_bounded_query_count(self):
        """多个历史事件只应使用批量查询，不能退化成每页一次 Outbox 查询。"""
        pages = [self._create_draft_page(f"history article {index}") for index in range(10)]
        for page in pages:
            self._create_search_projection(page, event_count=8)

        with CaptureQueriesContext(connection) as queries:
            report = check_blog_publication_consistency(
                after_page_id=pages[0].pk - 1,
                limit=len(pages),
                check_mongo=False,
            )

        self.assertEqual(report["scanned"], len(pages))
        self.assertNotIn("outbox_missing", report["counts"])
        # 页面数量和每页历史事件数增加时，Outbox 仍由一个批量查询加载。
        self.assertLessEqual(len(queries), 7)
