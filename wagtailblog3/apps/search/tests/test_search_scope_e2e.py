"""WP0/P0：验证 ContentSearchScopeJob 在父子页面权限变更与页面移动下的端到端搜索闭环。"""

from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from wagtail.models import PageViewRestriction

from blog.models import BlogPage
from search.models import (
    ContentSearchDelivery,
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchScopeJob,
    ContentSearchScopeJobStatus,
    ContentSearchState,
    ContentSearchStatus,
    ContentSearchTarget,
)
from search.services.delivery import (
    due_content_search_delivery_ids,
    materialize_content_search_deliveries,
    process_content_search_delivery,
)
from search.services.elasticsearch import ContentSearchWriteResult
from search.services.scope import process_scope_job
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


@override_settings(
    CONTENT_SEARCH_PRODUCER_ENABLED=True,
    CONTENT_SEARCH_CONSUMER_ENABLED=True,
    CONTENT_SEARCH_SCOPE_BATCH_SIZE=10,
)
class ContentSearchScopeE2ETests(BlogLifecycleFixtureMixin, TestCase):
    """端到端验证 Wagtail 8.0 权限树继承、页面移动与 ES 投影同步。"""

    def setUp(self):
        super().setUp()
        self.target = ContentSearchTarget.objects.create(
            target_id="test-scope-target",
            connection_name="default",
            index_name="test-scope-v005",
            required=True,
            enabled=True,
        )

    def _publish_page(self, parent, title, body_text):
        page_number = BlogPage.objects.count() + 1
        page = BlogPage(
            title=title,
            slug=f"scope-page-{page_number}",
            date=date(2026, 9, 3),
            intro=f"<p>{title} 简介</p>",
            body=self._markdown_body(body_text),
            live=False,
            has_unpublished_changes=True,
        )
        parent.add_child(instance=page)
        revision = page.save_revision()
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            revision.publish()
        page.refresh_from_db()
        return page

    def _drain_deliveries(self):
        """排空所有待投递的搜索事件，并捕获写入 ES 的文档。"""
        materialize_content_search_deliveries()
        delivery_ids = due_content_search_delivery_ids()
        captured_docs = []

        with patch(
            "search.services.delivery.write_content_search_document",
            side_effect=lambda target, doc, ver, **kw: (
                captured_docs.append(dict(doc)),
                ContentSearchWriteResult(status="succeeded"),
            )[1],
        ):
            for d_id in delivery_ids:
                process_content_search_delivery(d_id)

        return captured_docs

    def _drain_scope_jobs(self):
        """消费所有待处理的 ScopeJob 直至 SUCCEEDED。"""
        while True:
            jobs = list(
                ContentSearchScopeJob.objects.filter(
                    status__in=(
                        ContentSearchScopeJobStatus.PENDING,
                        ContentSearchScopeJobStatus.PROCESSING,
                        ContentSearchScopeJobStatus.RETRY,
                    )
                )
            )
            if not jobs:
                break
            for job in jobs:
                process_scope_job(job.pk)

    def test_parent_restriction_adds_tombstone_to_all_descendants(self):
        """父页面设置访问密码后，其子页面与孙子页面自动在 ES 投影中被打上墓碑。"""
        # 1. 建立 Parent -> Child -> Grandchild 三级已发布结构
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            parent = self._publish_page(self.index, "父目录页面", "父目录正文")
            child = self._publish_page(parent, "子目录页面", "子文章正文")
            grandchild = self._publish_page(child, "孙子页面", "孙文章正文")

        # 初始投递，确保所有页面均已公开进入 ES
        docs = self._drain_deliveries()
        self.assertTrue(any(d["page_id"] == child.pk and d["searchable"] for d in docs))
        self.assertTrue(any(d["page_id"] == grandchild.pk and d["searchable"] for d in docs))

        # 2. 为父目录加密码锁
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            restriction = PageViewRestriction.objects.create(
                page=parent,
                restriction_type=PageViewRestriction.PASSWORD,
                password="secure-password-123",
            )

        # 3. 驱动 ScopeJob 巡检
        self._drain_scope_jobs()

        # 4. 验证 State 与 Outbox 产生了 TOMBSTONE
        for p in (parent, child, grandchild):
            state = ContentSearchState.objects.get(page_id=p.pk)
            self.assertEqual(state.desired_operation, ContentSearchOperation.TOMBSTONE)
            self.assertFalse(state.searchable)

        # 5. 排空 Delivery，验证写入 ES 的文档 searchable 均为 False
        tombstone_docs = self._drain_deliveries()
        for p in (parent, child, grandchild):
            match = next((d for d in tombstone_docs if d["page_id"] == p.pk), None)
            self.assertIsNotNone(match, f"未找到页面 {p.pk} 的墓碑文档")
            self.assertFalse(match["searchable"])
            self.assertEqual(match["operation"], ContentSearchOperation.TOMBSTONE)

    def test_parent_restriction_removal_restores_all_descendants_to_upsert(self):
        """父页面解除限制后，子孙页面自动重新投递 UPSERT 正式正文恢复搜索。"""
        # 1. 创建受保护父页面及子页面
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            parent = self._publish_page(self.index, "受限父页面", "受限正文")
            child = self._publish_page(parent, "受限子页面", "受限子正文")
            restriction = PageViewRestriction.objects.create(
                page=parent,
                restriction_type=PageViewRestriction.PASSWORD,
                password="pwd",
            )

        # 巡检排空，确保初始为墓碑状态
        self._drain_scope_jobs()
        self._drain_deliveries()
        self.assertFalse(ContentSearchState.objects.get(page_id=child.pk).searchable)

        # 2. 解除父页面访问限制
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            restriction.delete()

        # 3. 驱动 ScopeJob
        self._drain_scope_jobs()

        # 4. 验证 State 恢复为可搜索 UPSERT
        state = ContentSearchState.objects.get(page_id=child.pk)
        self.assertEqual(state.desired_operation, ContentSearchOperation.UPSERT)
        self.assertTrue(state.searchable)

        # 5. 排空 Delivery，验证恢复正式正文和内容哈希
        docs = self._drain_deliveries()
        child_doc = next((d for d in docs if d["page_id"] == child.pk), None)
        self.assertIsNotNone(child_doc)
        self.assertTrue(child_doc["searchable"])
        self.assertEqual(child_doc["body_text"], "受限子正文")
        self.assertEqual(len(child_doc["content_hash"]), 64)

    def test_moving_public_page_under_restricted_parent_tombstones_page(self):
        """原本公开的页面移动到受保护的父页面下后，自动生成墓碑并从 ES 撤出。"""
        # 1. 建立受保护父页面 和 公开独立页面
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            parent = self._publish_page(self.index, "受限目录", "私密目录")
            PageViewRestriction.objects.create(
                page=parent,
                restriction_type=PageViewRestriction.LOGIN,
            )
            public_page = self._publish_page(self.index, "公开文章", "自由阅读")

        self._drain_scope_jobs()
        self._drain_deliveries()
        self.assertTrue(ContentSearchState.objects.get(page_id=public_page.pk).searchable)

        # 2. 将公开文章移动到受保护父目录下
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            public_page.move(parent, pos="last-child")

        # 3. 驱动页面移动产生的 ScopeJob
        self._drain_scope_jobs()

        # 4. 验证公开文章因继承父级限制而被打上墓碑
        state = ContentSearchState.objects.get(page_id=public_page.pk)
        self.assertEqual(state.desired_operation, ContentSearchOperation.TOMBSTONE)
        self.assertFalse(state.searchable)

        docs = self._drain_deliveries()
        moved_doc = next((d for d in docs if d["page_id"] == public_page.pk), None)
        self.assertIsNotNone(moved_doc)
        self.assertFalse(moved_doc["searchable"])

    def test_moving_restricted_page_under_public_parent_restores_page(self):
        """受保护父页面下的文章移出到公开根目录后，自动恢复公开搜索。"""
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            restricted_parent = self._publish_page(self.index, "限制目录", "限制内容")
            PageViewRestriction.objects.create(
                page=restricted_parent,
                restriction_type=PageViewRestriction.LOGIN,
            )
            child_page = self._publish_page(restricted_parent, "移出文章", "即将公开")

        self._drain_scope_jobs()
        self._drain_deliveries()
        self.assertFalse(ContentSearchState.objects.get(page_id=child_page.pk).searchable)

        # 移出到公开 index 下
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            child_page.move(self.index, pos="last-child")

        self._drain_scope_jobs()
        self.assertTrue(ContentSearchState.objects.get(page_id=child_page.pk).searchable)

        docs = self._drain_deliveries()
        restored_doc = next((d for d in docs if d["page_id"] == child_page.pk), None)
        self.assertIsNotNone(restored_doc)
        self.assertTrue(restored_doc["searchable"])
        self.assertEqual(restored_doc["body_text"], "即将公开")

    @override_settings(CONTENT_SEARCH_SCOPE_BATCH_SIZE=1)
    def test_multilevel_subtree_batching_and_checkpoint_advancement(self):
        """设置批次大小为 1，验证多页面子树在跨越批次边界时检查点稳健推进。"""
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            parent = self._publish_page(self.index, "批次父页", "批次父页正文")
            c1 = self._publish_page(parent, "子页1", "正文1")
            c2 = self._publish_page(parent, "子页2", "正文2")
            restriction = PageViewRestriction.objects.create(
                page=parent,
                restriction_type=PageViewRestriction.PASSWORD,
                password="123",
            )

        job = ContentSearchScopeJob.objects.get(root_page_id=parent.pk)
        self.assertEqual(job.checkpoint_page_id, 0)

        # 批次 1 处理 parent
        res1 = process_scope_job(job.pk, limit=1)
        self.assertEqual(res1, ContentSearchScopeJobStatus.PENDING)
        job.refresh_from_db()
        self.assertEqual(job.checkpoint_page_id, parent.pk)

        # 批次 2 处理 c1
        res2 = process_scope_job(job.pk, limit=1)
        self.assertEqual(res2, ContentSearchScopeJobStatus.PENDING)
        job.refresh_from_db()
        self.assertEqual(job.checkpoint_page_id, c1.pk)

        # 批次 3 处理 c2
        res3 = process_scope_job(job.pk, limit=1)
        self.assertEqual(res3, ContentSearchScopeJobStatus.PENDING)
        job.refresh_from_db()
        self.assertEqual(job.checkpoint_page_id, c2.pk)

        # 批次 4 完成无更多页面 -> SUCCEEDED
        res4 = process_scope_job(job.pk, limit=1)
        self.assertEqual(res4, ContentSearchScopeJobStatus.SUCCEEDED)
        job.refresh_from_db()
        self.assertEqual(job.status, ContentSearchScopeJobStatus.SUCCEEDED)
