from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from search.models import (
    ContentSearchDelivery,
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchState,
    ContentSearchStatus,
    ContentSearchTarget,
    SearchIndexBuild,
)


class ContentSearchFlagTests(SimpleTestCase):
    """WP3A 只安装数据骨架，不改变现有发布和搜索行为。"""

    def test_content_search_flags_are_disabled_in_test_environment(self):
        flag_names = (
            "CONTENT_SEARCH_PRODUCER_ENABLED",
            "CONTENT_SEARCH_CONSUMER_ENABLED",
            "CONTENT_SEARCH_SHADOW_READ_ENABLED",
            "CONTENT_SEARCH_QUERY_ENABLED",
            "CONTENT_SEARCH_RECONCILE_ENABLED",
        )

        self.assertTrue(all(getattr(settings, name) is False for name in flag_names))
        self.assertEqual(settings.CONTENT_SEARCH_CONNECTION_NAME, "default")
        self.assertEqual(settings.CONTENT_SEARCH_INDEX_PREFIX, "wagtailblog-test-content")


class ContentSearchModelConstraintTests(TestCase):
    """固定页面版本和事件目标组合，给后续至少一次投递提供数据库幂等基础。"""

    def setUp(self):
        self.target = ContentSearchTarget.objects.create(
            target_id="test-serving",
            connection_name="default",
            index_name="wagtailblog-test-content-v001",
        )
        self.event = ContentSearchOutbox.objects.create(
            page_id=1001,
            content_version=1,
            operation=ContentSearchOperation.UPSERT,
        )

    def test_state_keeps_deleted_page_identity_without_page_foreign_key(self):
        state = ContentSearchState.objects.create(page_id=1001)

        self.assertEqual(state.content_version, 0)
        self.assertFalse(state.searchable)
        self.assertIsNone(state.content_hash)
        self.assertIsNone(state.mongo_content_id)

    def test_outbox_rejects_duplicate_page_version(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ContentSearchOutbox.objects.create(
                    page_id=self.event.page_id,
                    content_version=self.event.content_version,
                    operation=ContentSearchOperation.TOMBSTONE,
                )

    def test_delivery_rejects_duplicate_event_target(self):
        ContentSearchDelivery.objects.create(event=self.event, target=self.target)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ContentSearchDelivery.objects.create(
                    event=self.event,
                    target=self.target,
                )

    def test_target_delete_is_protected_by_delivery_and_build(self):
        delivery = ContentSearchDelivery.objects.create(event=self.event, target=self.target)
        SearchIndexBuild.objects.create(target=self.target, mapping_version="v1")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.target.delete()

        delivery.delete()


class ContentSearchMigrationTests(TransactionTestCase):
    """验证 WP3A 首个迁移可以在测试库反向和正向执行。"""

    reset_sequences = True

    def test_initial_migration_can_reverse_and_reapply(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("search", None)])
        executor.loader.build_graph()
        executor.migrate([("search", "0002_contentsearchoutbox_mongo_content_id_and_more")])
