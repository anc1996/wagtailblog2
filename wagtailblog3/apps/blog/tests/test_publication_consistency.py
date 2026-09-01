"""BlogPage 发布状态对账与 publication generation 的回归测试。"""

import json
from io import StringIO
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from wagtail.actions.publish_page_revision import PublishPageRevisionAction

from blog.models import (
    BlogPage,
    BlogPublicationConsistencyCheckpoint,
    BlogPublicationState,
)
from blog.services.publication_consistency import check_blog_publication_consistency
from blog.tasks import check_publication_consistency
from search.models import ContentSearchOutbox, ContentSearchState
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


class BlogPublicationConsistencyTests(BlogLifecycleFixtureMixin, TestCase):
    """验证对账覆盖页面全集，并且命令保持只读。"""

    def _publish_page(self, text: str) -> tuple[BlogPage, object]:
        page = self._create_draft_page(text)
        revision = page.save_revision()
        revision.publish()
        page.refresh_from_db()
        return page, revision

    def test_existing_blog_page_without_state_is_reported(self):
        """对账不能只扫描状态表，否则完全缺失状态行的页面会被漏报。"""
        page, _revision = self._publish_page("缺失状态的正式正文")
        BlogPublicationState.objects.filter(page_id=page.pk).delete()

        report = check_blog_publication_consistency(
            after_page_id=0,
            limit=100,
            check_mongo=False,
        )

        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["counts"].get("state_missing"), 1)
        self.assertEqual(report["samples"]["state_missing"], [page.pk])

    def test_wagtail8_edit_publish_action_promotes_revision_state(self):
        """Wagtail 8.0 编辑发布动作绕过 BlogPage.publish 时仍切换正式正文指针。"""
        page = self._create_draft_page("Wagtail 8.0 编辑发布动作")
        revision = page.save_revision()

        with patch("search.services.outbox.schedule_content_search_wakeup"):
            PublishPageRevisionAction(revision).execute(skip_permission_checks=True)

        page.refresh_from_db()
        state = BlogPublicationState.objects.get(page_id=page.pk)
        content = json.loads(revision.content) if isinstance(revision.content, str) else revision.content
        self.assertTrue(page.live)
        self.assertEqual(page.live_revision_id, revision.pk)
        self.assertEqual(state.published_body_version_id, content["mongo_body_version_id"])
        self.assertEqual(state.published_body_sha256, content["body_sha256"])

    def test_consistency_cursor_advances_across_page_batches(self):
        """连续批次必须使用上一批最后一个 BlogPage 主键，避免重复扫描前一批。"""
        pages = [self._create_draft_page(f"批次游标文章 {index}") for index in range(3)]
        for page in pages:
            page.save_revision()

        first = check_blog_publication_consistency(
            after_page_id=0,
            limit=2,
            check_mongo=False,
        )
        second = check_blog_publication_consistency(
            after_page_id=first["next_after_page_id"],
            limit=2,
            check_mongo=False,
        )

        self.assertEqual(first["scanned"], 2)
        self.assertEqual(first["next_after_page_id"], pages[1].pk)
        self.assertEqual(second["scanned"], 1)
        self.assertEqual(second["after_page_id"], pages[1].pk)
        self.assertEqual(second["next_after_page_id"], pages[2].pk)
        self.assertNotIn(pages[0].pk, second["samples"]["state_missing"])

    def test_repeated_cursor_call_is_stateless_and_does_not_overwrite_data(self):
        """相同游标的重复调用必须得到相同批次，且不得写回游标或业务数据。"""
        pages = [self._create_draft_page(f"重复游标文章 {index}") for index in range(2)]
        for page in pages:
            page.save_revision()
        before_pages = list(
            BlogPage.objects.filter(pk__in=[page.pk for page in pages])
            .order_by("pk")
            .values_list("pk", "live", "live_revision_id", "body")
        )
        before_states = list(
            BlogPublicationState.objects.order_by("page_id").values_list(
                "page_id", "publication_generation", "published_body_version_id", "updated_at"
            )
        )

        first = check_blog_publication_consistency(after_page_id=0, limit=1, check_mongo=False)
        second = check_blog_publication_consistency(after_page_id=0, limit=1, check_mongo=False)

        self.assertEqual(first, second)
        self.assertEqual(
            before_pages,
            list(
                BlogPage.objects.filter(pk__in=[page.pk for page in pages])
                .order_by("pk")
                .values_list("pk", "live", "live_revision_id", "body")
            ),
        )
        self.assertEqual(
            before_states,
            list(
                BlogPublicationState.objects.order_by("page_id").values_list(
                    "page_id", "publication_generation", "published_body_version_id", "updated_at"
                )
            ),
        )

    def test_live_page_without_published_pointer_is_reported(self):
        """live 页面必须有正式 Mongo 正文指针，状态行缺指针应明确告警。"""
        page, _revision = self._publish_page("缺失正式正文指针")
        state = BlogPublicationState.objects.get(page_id=page.pk)
        state.published_body_version_id = None
        state.published_body_sha256 = None
        state.published_body_schema_version = None
        state.save(
            update_fields=(
                "published_body_version_id",
                "published_body_sha256",
                "published_body_schema_version",
                "updated_at",
            )
        )

        report = check_blog_publication_consistency(
            after_page_id=page.pk - 1,
            limit=1,
            check_mongo=False,
        )

        self.assertEqual(report["counts"].get("live_pointer_missing"), 1)
        self.assertEqual(report["samples"]["live_pointer_missing"], [page.pk])

    def test_repeated_publish_advances_generation_monotonically(self):
        """重复发布同一 Revision 也必须获得新的代次，迟到事件可按代次淘汰。"""
        page, revision = self._publish_page("第一次正式正文")
        first_state = BlogPublicationState.objects.get(page_id=page.pk)
        first_generation = first_state.publication_generation
        first_body_version = first_state.published_body_version_id

        revision.publish()
        second_state = BlogPublicationState.objects.get(page_id=page.pk)

        self.assertGreater(second_state.publication_generation, first_generation)
        self.assertEqual(second_state.published_body_version_id, first_body_version)
        self.assertEqual(
            ContentSearchOutbox.objects.filter(page_id=page.pk)
            .order_by("-publication_generation")
            .values_list("publication_generation", flat=True)[:2].count(),
            2,
        )

    def test_consistency_command_is_read_only(self):
        """命令只输出诊断结果，不创建或更新 State、搜索状态和 Outbox。"""
        page, _revision = self._publish_page("只读对账正文")
        before = {
            "states": list(
                BlogPublicationState.objects.order_by("page_id").values_list(
                    "page_id", "publication_generation", "updated_at"
                )
            ),
            "search_states": list(
                ContentSearchState.objects.order_by("page_id").values_list(
                    "page_id", "content_version", "updated_at"
                )
            ),
            "outbox": list(
                ContentSearchOutbox.objects.filter(page_id=page.pk)
                .order_by("pk")
                .values_list("pk", "publication_generation", "created_at", "updated_at")
            ),
        }
        output = StringIO()

        call_command(
            "blog_publication_consistency_check",
            "--after-page-id",
            str(page.pk - 1),
            "--limit",
            "1",
            "--skip-mongo",
            stdout=output,
        )

        report = json.loads(output.getvalue())
        after = {
            "states": list(
                BlogPublicationState.objects.order_by("page_id").values_list(
                    "page_id", "publication_generation", "updated_at"
                )
            ),
            "search_states": list(
                ContentSearchState.objects.order_by("page_id").values_list(
                    "page_id", "content_version", "updated_at"
                )
            ),
            "outbox": list(
                ContentSearchOutbox.objects.filter(page_id=page.pk)
                .order_by("pk")
                .values_list("pk", "publication_generation", "created_at", "updated_at")
            ),
        }

        self.assertTrue(report["read_only"])
        self.assertEqual(report["mongo_checked"], False)
        self.assertEqual(before, after)


class BlogPublicationConsistencyCheckpointTests(
    BlogLifecycleFixtureMixin,
    TransactionTestCase,
):
    """验证周期对账 checkpoint 的高水位、租约和失败释放行为。"""

    reset_sequences = True

    def setUp(self):
        super().setUp()
        BlogPublicationConsistencyCheckpoint.objects.all().delete()

    @override_settings(BLOG_PUBLICATION_CONSISTENCY_BATCH_SIZE=1)
    def test_periodic_task_advances_and_resets_checkpoint_after_cycle(self):
        """周期任务应固定本轮高水位，完成后将游标和高水位重置。"""
        pages = [
            self._create_draft_page(f"checkpoint 文章 {index}")
            for index in range(2)
        ]
        for page in pages:
            page.save_revision()

        first = check_publication_consistency()
        checkpoint = BlogPublicationConsistencyCheckpoint.objects.get(
            scope="blog_publication_consistency"
        )
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["scanned"], 1)
        self.assertEqual(checkpoint.cursor_page_id, pages[0].pk)
        self.assertEqual(checkpoint.scan_upper_bound_page_id, pages[1].pk)
        first_cycle = checkpoint.cycle

        second = check_publication_consistency()
        checkpoint.refresh_from_db()
        self.assertEqual(second["scanned"], 1)
        self.assertEqual(checkpoint.cursor_page_id, 0)
        self.assertIsNone(checkpoint.scan_upper_bound_page_id)
        self.assertEqual(checkpoint.cycle, first_cycle + 1)
        self.assertEqual(checkpoint.last_scanned, 1)

    def test_periodic_task_does_not_scan_when_lease_is_busy(self):
        """已有有效租约时必须立即返回，避免并发 worker 重复扫描。"""
        BlogPublicationConsistencyCheckpoint.objects.create(
            scope="blog_publication_consistency",
            lease_owner="another-worker",
            lease_expires_at=timezone.now() + timedelta(minutes=5),
        )

        with patch("blog.services.publication_consistency.check_blog_publication_consistency") as checker:
            result = check_publication_consistency()

        self.assertEqual(result, {"status": "lease_busy", "scanned": 0, "counts": {}})
        checker.assert_not_called()

    def test_periodic_task_releases_lease_and_records_error(self):
        """对账读取失败时必须释放租约并保留脱敏错误类型，便于下轮重试。"""
        with patch(
            "blog.services.publication_consistency.check_blog_publication_consistency",
            side_effect=RuntimeError("test-only failure"),
        ):
            try:
                check_publication_consistency()
            except RuntimeError:
                # 兼容任务选择向 Celery 重抛异常或转换为失败结果；两者都必须释放租约。
                pass

        checkpoint = BlogPublicationConsistencyCheckpoint.objects.get(
            scope="blog_publication_consistency"
        )
        self.assertEqual(checkpoint.lease_owner, "")
        self.assertIsNone(checkpoint.lease_expires_at)
        self.assertEqual(checkpoint.last_error, "RuntimeError")

    @override_settings(BLOG_PUBLICATION_CONSISTENCY_BATCH_SIZE=1)
    def test_periodic_task_reports_lease_lost_when_completion_update_matches_no_rows(self):
        """完成写回未命中租约时必须报告 lease_lost，不能伪造 completed 结果。"""
        page = self._create_draft_page("租约丢失测试")
        page.save_revision()

        with patch.object(BlogPublicationConsistencyCheckpoint.objects, "filter") as filter_mock:
            filter_mock.return_value.update.return_value = 0
            result = check_publication_consistency()

        self.assertEqual(result["status"], "lease_lost")
        self.assertEqual(result["scanned"], 1)
        filter_mock.assert_called_once()
