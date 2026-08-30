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
from wagtailblog3.settings.search_runtime import (
    validate_production_content_search_runtime_namespace,
)


class ContentSearchFlagTests(SimpleTestCase):
    """WP3A 只安装数据骨架，不改变现有发布和搜索行为。"""

    def test_content_search_runtime_is_enabled_in_test_environment(self):
        enabled_flag_names = (
            "CONTENT_SEARCH_PRODUCER_ENABLED",
            "CONTENT_SEARCH_CONSUMER_ENABLED",
        )

        self.assertTrue(all(getattr(settings, name) is True for name in enabled_flag_names))
        self.assertEqual(settings.CONTENT_SEARCH_CONNECTION_NAME, "default")
        self.assertEqual(settings.CONTENT_SEARCH_INDEX_PREFIX, "wagtailblog-test-content")


class ProductionContentSearchRuntimeNamespaceTests(SimpleTestCase):
    def test_disabled_production_features_do_not_require_runtime_namespace(self):
        validate_production_content_search_runtime_namespace(
            environment="production",
            feature_flags=(False,),
            runtime_connection_name="default",
            runtime_index_prefix="wagtailblog-test-content",
            runtime_read_alias="wagtailblog-test-content-read",
            production_connection_name="content_production",
            production_index_prefix="wagtailblog-prod-content",
        )

    def test_enabled_production_feature_rejects_test_runtime_namespace(self):
        with self.assertRaisesMessage(ValueError, "运行时连接名"):
            validate_production_content_search_runtime_namespace(
                environment="production",
                feature_flags=(True,),
                runtime_connection_name="default",
                runtime_index_prefix="wagtailblog-test-content",
                runtime_read_alias="wagtailblog-test-content-read",
                production_connection_name="content_production",
                production_index_prefix="wagtailblog-prod-content",
            )

    def test_enabled_production_feature_accepts_matching_runtime_namespace(self):
        validate_production_content_search_runtime_namespace(
            environment="production",
            feature_flags=(True,),
            runtime_connection_name="content_production",
            runtime_index_prefix="wagtailblog-prod-content",
            runtime_read_alias="wagtailblog-prod-content-read",
            production_connection_name="content_production",
            production_index_prefix="wagtailblog-prod-content",
        )

    def test_enabled_production_feature_requires_explicit_production_prefix(self):
        with self.assertRaisesMessage(ValueError, "生产索引前缀"):
            validate_production_content_search_runtime_namespace(
                environment="production",
                feature_flags=(True,),
                runtime_connection_name="content_production",
                runtime_index_prefix="",
                runtime_read_alias="-read",
                production_connection_name="content_production",
                production_index_prefix="",
            )


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
        try:
            executor.migrate([("search", None)])
            executor.loader.build_graph()
            executor.migrate([("search", "0002_contentsearchoutbox_mongo_content_id_and_more")])
        finally:
            # 迁移回滚测试必须恢复最新 schema，避免污染同一进程中的后续测试。
            executor.loader.build_graph()
            latest_search = [
                node for node in executor.loader.graph.leaf_nodes() if node[0] == "search"
            ]
            executor.migrate(latest_search)
