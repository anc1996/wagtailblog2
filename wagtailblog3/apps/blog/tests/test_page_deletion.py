"""页面级不可恢复删除状态机的引用、幂等和精确 Mongo 清理测试。"""

from types import SimpleNamespace
from io import StringIO
from unittest.mock import MagicMock, call, patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from blog.models import BlogPage, BlogPublicationState, PageDeletionIntent, PageDeletionIntentStatus
from blog.signals import (
    allow_page_delete_for_ids,
    cascade_delete_single_mongo_revision,
    delete_blog_content_from_mongodb,
)
from blog.tasks import _tombstone_succeeded, process_page_deletion
from blog.wagtail_hooks import intercept_blog_page_bulk_delete
from search.models import ContentSearchOperation, ContentSearchOutbox, ContentSearchStatus


class PageDeletionTaskTests(TestCase):
    """验证页面清理只使用固化指针，且共享引用会完整阻断。"""

    def _intent(self) -> PageDeletionIntent:
        return PageDeletionIntent.objects.create(
            dedupe_key="page:901:delete:1",
            page_id=901,
            deletion_generation=1,
            manifest={
                "body_version_ids": ["body-v1"],
                "revision_pointers": ["rev_901_1"],
                "legacy_pointer": None,
            },
        )

    @override_settings(CONTENT_SEARCH_PRODUCER_ENABLED=False)
    @patch("blog.tasks.MongoManager")
    @patch("blog.tasks.BlogPage.objects.filter")
    @patch("blog.tasks.Revision.objects.filter")
    def test_worker_clears_publication_state_before_page_finalize(
        self,
        revision_filter: MagicMock,
        page_filter: MagicMock,
        manager_factory: MagicMock,
    ) -> None:
        """页面无外键级联时，物理删除前必须清空 State 的正文指针。"""
        intent = self._intent()
        BlogPublicationState.objects.create(
            page_id=901,
            draft_body_version_id="body-v1",
            draft_body_sha256="a" * 64,
            draft_body_schema_version=1,
            published_body_version_id="body-v1",
            published_body_sha256="a" * 64,
            published_body_schema_version=1,
            approved_revision_id=1,
            approved_body_version_id="body-v1",
            approved_body_sha256="a" * 64,
            approved_body_schema_version=1,
        )
        revision_filter.return_value.exclude.return_value.only.return_value.iterator.return_value = []
        page_filter.return_value.exists.return_value = False
        page_filter.return_value.first.return_value = None
        manager = manager_factory.return_value
        manager.content_body_versions.delete_many.return_value = SimpleNamespace(deleted_count=1)
        manager.blog_revisions.delete_one.return_value = SimpleNamespace(deleted_count=1)

        result = process_page_deletion.run(str(intent.intent_id))

        self.assertEqual(result["status"], PageDeletionIntentStatus.SUCCEEDED)
        state = BlogPublicationState.objects.get(page_id=901)
        self.assertIsNone(state.draft_body_version_id)
        self.assertIsNone(state.published_body_version_id)
        self.assertIsNone(state.approved_body_version_id)
        self.assertIsNone(state.approved_revision_id)

    @override_settings(CONTENT_SEARCH_PRODUCER_ENABLED=False)
    @patch("blog.models.MongoManager")
    def test_save_rejected_when_page_deletion_intent_is_active(self, manager_factory: MagicMock) -> None:
        """删除意图创建后，旧页面对象不能再次写入 Mongo 或 MySQL。"""
        self._intent()
        page = BlogPage(pk=901, title="竞态测试", body=[])

        with self.assertRaisesRegex(RuntimeError, "page_deletion_in_progress"):
            page.save()

        manager_factory.assert_not_called()

    @patch("blog.tasks.process_page_deletion.apply_async")
    def test_retry_command_is_read_only_without_apply(self, dispatch: MagicMock) -> None:
        """人工处理命令默认只展示状态，不改变死信或投递队列。"""
        intent = self._intent()
        intent.status = PageDeletionIntentStatus.DEAD
        intent.save(update_fields=("status", "updated_at"))
        output = StringIO()

        call_command("retry_page_deletion", str(intent.intent_id), stdout=output)

        intent.refresh_from_db()
        self.assertEqual(intent.status, PageDeletionIntentStatus.DEAD)
        dispatch.assert_not_called()

    @patch("blog.tasks.process_page_deletion.apply_async")
    def test_retry_command_apply_unlocks_and_dispatches_maintenance(self, dispatch: MagicMock) -> None:
        """显式 --apply 才能解锁 dead 意图，并固定投递 maintenance 队列。"""
        intent = self._intent()
        intent.status = PageDeletionIntentStatus.DEAD
        intent.attempts = 5
        intent.save(update_fields=("status", "attempts", "updated_at"))

        with self.captureOnCommitCallbacks(execute=True):
            call_command("retry_page_deletion", str(intent.intent_id), "--apply")

        intent.refresh_from_db()
        self.assertEqual(intent.status, PageDeletionIntentStatus.PARTIAL_FAILED)
        self.assertEqual(intent.attempts, 0)
        dispatch.assert_called_once_with(
            args=(str(intent.intent_id),), queue="maintenance"
        )

    @override_settings(CONTENT_SEARCH_PRODUCER_ENABLED=False)
    @patch("blog.tasks.MongoManager")
    @patch("blog.tasks.BlogPage.objects.filter")
    @patch("blog.tasks.Revision.objects.filter")
    def test_worker_deletes_all_three_targets_by_exact_keys(
        self,
        revision_filter: MagicMock,
        page_filter: MagicMock,
        manager_factory: MagicMock,
    ) -> None:
        """正文版本、Revision 快照和旧正文均按清单调用幂等删除。"""
        intent = self._intent()
        revision_filter.return_value.exclude.return_value.only.return_value.iterator.return_value = []
        page_filter.return_value.exists.return_value = False
        page_filter.return_value.first.return_value = None
        manager = manager_factory.return_value
        manager.content_body_versions.delete_many.return_value = SimpleNamespace(deleted_count=1)
        manager.blog_revisions.delete_one.return_value = SimpleNamespace(deleted_count=1)

        result = process_page_deletion.run(str(intent.intent_id))

        self.assertEqual(result["status"], PageDeletionIntentStatus.SUCCEEDED)
        manager.content_body_versions.delete_many.assert_called_once_with(
            {
                "aggregate_type": "blog_page",
                "aggregate_id": "901",
                "body_version_id": {"$in": ["body-v1"]},
            }
        )
        manager.blog_revisions.delete_one.assert_called_once_with(
            {"_id": "rev_901_1", "page_id": 901}
        )

    @override_settings(CONTENT_SEARCH_PRODUCER_ENABLED=False)
    @patch("blog.tasks.MongoManager")
    @patch("blog.tasks.BlogPage.objects.filter")
    @patch("blog.tasks.Revision.objects.filter")
    def test_manifest_expansion_drives_reference_check_and_delete(
        self,
        revision_filter: MagicMock,
        page_filter: MagicMock,
        manager_factory: MagicMock,
    ) -> None:
        """Mongo 中未被 State/Revision 指针覆盖的历史版本也进入同一删除清单。"""
        intent = self._intent()
        revision_filter.return_value.exclude.return_value.only.return_value.iterator.return_value = []
        page_filter.return_value.exists.return_value = False
        page_filter.return_value.first.return_value = None
        manager = manager_factory.return_value
        manager.content_body_versions.find.return_value = [
            {"body_version_id": "body-v1"},
            {"body_version_id": "body-v2"},
        ]
        manager.blog_revisions.find.return_value = [
            {"_id": "rev_901_1"},
            {"_id": "rev_901_2"},
        ]
        manager.content_body_versions.delete_many.return_value = SimpleNamespace(deleted_count=2)
        manager.blog_revisions.delete_one.return_value = SimpleNamespace(deleted_count=1)

        result = process_page_deletion.run(str(intent.intent_id))

        self.assertEqual(result["status"], PageDeletionIntentStatus.SUCCEEDED)
        self.assertEqual(
            manager.content_body_versions.delete_many.call_args.args[0]["body_version_id"]["$in"],
            ["body-v1", "body-v2"],
        )
        self.assertEqual(manager.blog_revisions.delete_one.call_count, 2)

    @override_settings(CONTENT_SEARCH_PRODUCER_ENABLED=False)
    @patch("blog.tasks.Revision.objects.filter")
    def test_worker_blocks_shared_body_version(self, revision_filter: MagicMock) -> None:
        """其他页面引用正式版本时不删除任何集合并进入阻断状态。"""
        intent = self._intent()
        from blog.models import BlogPublicationState

        BlogPublicationState.objects.create(page_id=902, published_body_version_id="body-v1")
        revision_filter.return_value.exclude.return_value.only.return_value.iterator.return_value = []

        result = process_page_deletion.run(str(intent.intent_id))

        self.assertEqual(result["status"], PageDeletionIntentStatus.BLOCKED_REFERENCE)
        intent.refresh_from_db()
        self.assertEqual(intent.status, PageDeletionIntentStatus.BLOCKED_REFERENCE)

    @override_settings(CONTENT_SEARCH_PRODUCER_ENABLED=True)
    def test_tombstone_must_match_bound_event_and_deletion_generation(self) -> None:
        """旧代 tombstone 即使成功也不能放行当前删除意图。"""
        intent = self._intent()
        intent.deletion_generation = 2
        intent.dedupe_key = "page:901:delete:2"
        intent.save(update_fields=("deletion_generation", "dedupe_key"))
        event = ContentSearchOutbox.objects.create(
            page_id=901,
            content_version=3,
            operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
            status=ContentSearchStatus.SUCCEEDED,
            publication_generation=1,
        )
        intent.manifest = {**intent.manifest, "tombstone_event_id": str(event.event_id)}
        intent.save(update_fields=("manifest",))

        self.assertFalse(_tombstone_succeeded(intent))
        event.publication_generation = 2
        event.save(update_fields=("publication_generation",))
        self.assertTrue(_tombstone_succeeded(intent))

    @patch("blog.signals._record_revision_cleanup_intent")
    def test_finalize_context_allows_descendant_and_skips_revision_intent(
        self,
        record_revision: MagicMock,
    ) -> None:
        """Collector 级联子页面和 Revision 时继承根页面最终删除令牌。"""
        page = SimpleNamespace(pk=902)
        revision = SimpleNamespace(object_id="902")

        with allow_page_delete_for_ids({901, 902}):
            delete_blog_content_from_mongodb(None, page)
            cascade_delete_single_mongo_revision(None, revision)

        record_revision.assert_not_called()

    @patch("blog.signals._record_revision_cleanup_intent")
    def test_mysql_finalize_does_not_create_duplicate_revision_intent(
        self,
        record_revision: MagicMock,
    ) -> None:
        """页面自身最终级联已有全量清单，不再创建旧单指针意图。"""
        intent = self._intent()
        intent.status = PageDeletionIntentStatus.MYSQL_FINALIZE_PENDING
        intent.save(update_fields=("status",))

        cascade_delete_single_mongo_revision(None, SimpleNamespace(object_id="901"))

        record_revision.assert_not_called()

    @patch("blog.wagtail_hooks.messages.success")
    @patch("blog.wagtail_hooks.request_page_deletion")
    @patch(
        "blog.wagtail_hooks.BlogPage.get_descendants",
        return_value=SimpleNamespace(specific=lambda: []),
    )
    def test_bulk_delete_hook_registers_each_blog_page_before_native_action(
        self,
        get_descendants_mock: MagicMock,
        request_page_deletion_mock: MagicMock,
        messages_success_mock: MagicMock,
    ) -> None:
        """Wagtail 8.0 批量入口必须逐页登记意图，不能继续执行原生 Collector。"""
        request = SimpleNamespace(method="POST", META={}, user=SimpleNamespace(pk=7))
        action = SimpleNamespace(next_url="/admin/pages/36/")
        pages = [
            SimpleNamespace(specific=BlogPage(pk=642)),
            SimpleNamespace(specific=BlogPage(pk=643)),
        ]


        response = intercept_blog_page_bulk_delete(request, "delete", pages, action)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/admin/pages/36/")
        self.assertEqual(request_page_deletion_mock.call_count, 2)
        self.assertEqual(
            [call.args[0].pk for call in request_page_deletion_mock.call_args_list],
            [642, 643],
        )
        messages_success_mock.assert_called_once()
