import json
import os
from datetime import date as date_type
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bson import ObjectId
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from search.models import (
    ContentSearchTarget,
    ContentSearchTargetRole,
    SearchIndexBuild,
    SearchIndexBuildStatus,
)
from search.services.content_index import (
    CONTENT_INDEX_REQUIRED_FIELDS,
    build_content_index_template,
    content_index_template_matches,
)
from search.services.document import (
    build_formal_content_document,
    build_formal_content_documents,
)
from search.services.elasticsearch import (
    ContentSearchIndexCreateResult,
    create_content_search_index,
)
from search.services.mongo import read_formal_contents_by_id
from wagtailblog3.mongo import MongoManager


PRODUCTION_INDEX_SETTINGS = {
    "CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME": "content_production",
    "CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX": "wagtailblog-prod-content",
    "CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED": True,
    "CONTENT_SEARCH_PRODUCTION_BACKUP_ROOT": "/backups",
    "CONTENT_SEARCH_PRODUCTION_INDEX_CREATE_ENABLED": True,
    "CONTENT_SEARCH_PRODUCER_ENABLED": False,
    "CONTENT_SEARCH_CONSUMER_ENABLED": False,
    "CONTENT_SEARCH_CURSOR_ENABLED": False,
    "CONTENT_SEARCH_PIT_ENABLED": False,
    "SEARCH_SUGGESTIONS_V2_ENABLED": False,
    "SEARCH_POPULAR_SUGGESTIONS_ENABLED": False,
    "SEARCH_TITLE_SUGGESTIONS_ENABLED": False,
    "CONTENT_SEARCH_RECONCILE_ENABLED": False,
    "CONTENT_SEARCH_INDEX_SHARDS": 1,
    "CONTENT_SEARCH_INDEX_REPLICAS": 0,
    "CONTENT_SEARCH_INDEX_REFRESH_INTERVAL": "30s",
    "WAGTAILSEARCH_BACKENDS": {"content_production": {"BACKEND": "test"}},
}

PRODUCTION_ALIAS_SWITCH_SETTINGS = {
    "CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME": "content_production",
    "CONTENT_SEARCH_PRODUCTION_INDEX_PREFIX": "wagtailblog-prod-content",
    "CONTENT_SEARCH_PRODUCTION_BACKUP_ROOT": "/backups",
    "CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED": True,
    "CONTENT_SEARCH_PRODUCTION_QUERY_SWITCH_ENABLED": True,
    "CONTENT_SEARCH_CONNECTION_NAME": "content_production",
    "CONTENT_SEARCH_INDEX_PREFIX": "wagtailblog-prod-content",
    "CONTENT_SEARCH_READ_ALIAS": "wagtailblog-prod-content-read",
}


class _Relation:
    def __init__(self, values):
        self.values = values

    def values_list(self, field_name, flat):
        return self.values

    def all(self):
        return [SimpleNamespace(pk=value) for value in self.values]


class _FormalContentPage:
    """以最小页面替身验证索引投影不会重复读取 Mongo 正式正文。"""

    pk = 73001
    mongo_content_id = "64e7b6ef1d86d3f4b6e4e001"
    title = "独立内容索引"
    intro = "<p>精简索引简介</p>"
    date = date_type(2026, 8, 10)
    first_published_at = date_type(2026, 8, 11)
    locale_id = 3
    tags = _Relation([11, 12])
    categories = _Relation([21])

    def __init__(self):
        self.formal_content = {"body": [{"type": "markdown_block", "value": "正式正文"}]}
        self.mongo_read_count = 0
        self.received_content = None

    def get_content_from_mongodb(self):
        self.mongo_read_count += 1
        return self.formal_content

    def get_full_text_for_search(self, content=None):
        self.received_content = content
        return "正式正文"


class ContentIndexDefinitionTests(SimpleTestCase):
    """mapping 只允许设计字段，并保留 analyzer 对比所需的三个配置。"""

    def test_balanced_mapping_is_strict_and_excludes_protected_content(self):
        definition = build_content_index_template(
            "wagtailblog-test-content-v001",
            analyzer_profile="balanced",
        )
        mappings = definition["template"]["mappings"]
        properties = mappings["properties"]

        self.assertEqual(mappings["dynamic"], "strict")
        self.assertTrue(CONTENT_INDEX_REQUIRED_FIELDS.issubset(properties))
        self.assertNotIn("mongo_content_id", properties)
        self.assertNotIn("mongo_draft_pointer", properties)
        self.assertEqual(properties["title"]["analyzer"], "content_ik_max_word")
        self.assertEqual(properties["body_text"]["analyzer"], "content_ik_smart")
        self.assertEqual(definition["template"]["settings"]["codec"], "best_compression")

    def test_legacy_standard_profile_has_no_ik_analyzer(self):
        definition = build_content_index_template(
            "wagtailblog-test-content-v002",
            analyzer_profile="legacy_standard",
            version="v002",
        )
        properties = definition["template"]["mappings"]["properties"]

        self.assertEqual(properties["body_text"], {"type": "text"})
        self.assertNotIn("search_analyzer", properties["title"])
        self.assertNotIn("analysis", definition["template"]["settings"])

    def test_template_reuse_requires_same_meta_and_index_name(self):
        definition = build_content_index_template("wagtailblog-test-content-v001")
        existing = {
            "index_templates": [
                {
                    "name": "wagtailblog-test-content-v001-template",
                    "index_template": {
                        "index_patterns": definition["index_patterns"],
                        "template": definition["template"],
                        "_meta": definition["_meta"],
                    },
                }
            ]
        }

        self.assertTrue(content_index_template_matches(existing, definition))
        existing["index_templates"][0]["index_template"]["_meta"] = {}
        self.assertFalse(content_index_template_matches(existing, definition))

    def test_template_reuse_rejects_same_meta_when_mapping_is_missing_fields(self):
        definition = build_content_index_template("wagtailblog-test-content-v005")
        existing = {
            "index_templates": [
                {
                    "index_template": {
                        "index_patterns": definition["index_patterns"],
                        "template": {
                            **definition["template"],
                            "mappings": {
                                **definition["template"]["mappings"],
                                "properties": {
                                    key: value
                                    for key, value in definition["template"]["mappings"]["properties"].items()
                                    if key != "publication_generation"
                                },
                            },
                        },
                        "_meta": definition["_meta"],
                    }
                }
            ]
        }

        self.assertFalse(content_index_template_matches(existing, definition))

    def test_document_uses_one_formal_read_and_projects_filter_fields(self):
        page = _FormalContentPage()

        document = build_formal_content_document(page, content_version=7)

        self.assertEqual(page.mongo_read_count, 1)
        self.assertIs(page.received_content, page.formal_content)
        self.assertEqual(document.document["locale_id"], 3)
        self.assertEqual(document.document["tag_ids"], [11, 12])
        self.assertEqual(document.document["category_ids"], [21])
        self.assertEqual(document.document["date"], "2026-08-10")
        self.assertEqual(document.document["first_published_at"], "2026-08-11")
        self.assertNotIn("mongo_content_id", document.document)

    def test_document_prefers_published_body_version_pointer(self):
        page = _FormalContentPage()
        page._meta = SimpleNamespace()
        state_values = Mock()
        state_values.first.return_value = {
            "published_body_version_id": "body-v2",
            "published_body_sha256": "a" * 64,
            "published_body_schema_version": 1,
        }
        state_filter = Mock()
        state_filter.values.return_value = state_values
        manager = Mock()
        manager.get_content_body_version.return_value = {
            "body": [{"type": "markdown_block", "value": "发布版本"}],
        }

        with (
            patch("blog.models.BlogPublicationState.objects.filter", return_value=state_filter),
            patch("blog.models.MongoManager", return_value=manager),
        ):
            document = build_formal_content_document(page, content_version=8)

        self.assertEqual(page.mongo_read_count, 0)
        manager.get_content_body_version.assert_called_once_with(
            "blog_page", page.pk, "body-v2", "a" * 64, 1
        )
        self.assertEqual(document.document["body_text"], "正式正文")

    def test_document_reads_published_version_without_legacy_content_id(self):
        page = _FormalContentPage()
        page._meta = SimpleNamespace()
        page.mongo_content_id = None
        state_values = Mock()
        state_values.first.return_value = {
            "published_body_version_id": "body-v2",
            "published_body_sha256": "a" * 64,
            "published_body_schema_version": 1,
        }
        state_filter = Mock()
        state_filter.values.return_value = state_values
        manager = Mock()
        manager.get_content_body_version.return_value = {
            "body": [{"type": "markdown_block", "value": "正式正文"}],
        }

        with (
            patch("blog.models.BlogPublicationState.objects.filter", return_value=state_filter),
            patch("blog.models.MongoManager", return_value=manager),
        ):
            document = build_formal_content_document(page, content_version=8)

        self.assertIsNotNone(document)
        self.assertIsNone(document.mongo_content_id)
        self.assertEqual(page.mongo_read_count, 0)

    def test_document_falls_back_to_legacy_content_without_published_pointer(self):
        page = _FormalContentPage()
        page._meta = SimpleNamespace()
        state_values = Mock()
        state_values.first.return_value = None
        state_filter = Mock()
        state_filter.values.return_value = state_values

        with patch("blog.models.BlogPublicationState.objects.filter", return_value=state_filter):
            document = build_formal_content_document(page, content_version=9)

        self.assertEqual(page.mongo_read_count, 1)
        self.assertEqual(document.document["body_text"], "正式正文")

    def test_batch_projection_never_falls_back_to_per_page_mongo_reads(self):
        first_page = _FormalContentPage()
        second_page = _FormalContentPage()
        second_page.pk = 73002
        second_page.mongo_content_id = "64e7b6ef1d86d3f4b6e4e002"

        documents, missing_page_ids = build_formal_content_documents(
            [first_page, second_page],
            {first_page.pk: 1, second_page.pk: 1},
            {str(first_page.mongo_content_id): first_page.formal_content},
        )

        self.assertEqual(len(documents), 1)
        self.assertEqual(missing_page_ids, [second_page.pk])
        self.assertEqual(first_page.mongo_read_count, 0)
        self.assertEqual(second_page.mongo_read_count, 0)

    def test_batch_projection_uses_state_body_when_legacy_content_id_is_empty(self):
        page = _FormalContentPage()
        page.mongo_content_id = None

        documents, missing_page_ids = build_formal_content_documents(
            [page],
            {page.pk: 1},
            {f"page:{page.pk}": page.formal_content},
            body_version_ids={page.pk: "published-v1"},
            publication_generations={page.pk: 1},
        )

        self.assertEqual(missing_page_ids, [])
        self.assertEqual(len(documents), 1)
        self.assertIsNone(documents[0].mongo_content_id)
        self.assertEqual(documents[0].document["body_version_id"], "published-v1")
        self.assertEqual(page.mongo_read_count, 0)

    def test_batch_reader_uses_one_in_query_and_excludes_invalid_ids(self):
        first_id = "64e7b6ef1d86d3f4b6e4e001"
        second_id = "64e7b6ef1d86d3f4b6e4e002"
        collection = Mock()
        collection.find.return_value = [
            {"_id": ObjectId(first_id), "body": [{"type": "markdown_block"}]},
        ]
        manager = SimpleNamespace(blog_content=collection)

        contents = read_formal_contents_by_id(
            [first_id, second_id, first_id, "not-an-object-id"],
            mongo_manager=manager,
        )

        self.assertEqual(set(contents), {first_id})
        collection.find.assert_called_once_with(
            {"_id": {"$in": [ObjectId(first_id), ObjectId(second_id)]}},
            {"_id": 1, "body": 1},
        )

    def test_batch_reader_falls_back_to_page_id_for_legacy_pointer(self):
        collection = Mock()
        collection.find.side_effect = [
            [],
            [{"_id": ObjectId("64e7b6ef1d86d3f4b6e4e009"), "page_id": 73009,
              "body": [{"type": "markdown_block"}]}],
        ]
        manager = SimpleNamespace(blog_content=collection)

        contents = read_formal_contents_by_id(
            ["64e7b6ef1d86d3f4b6e4e099"],
            mongo_manager=manager,
            page_ids=[73009],
        )

        self.assertIn("page:73009", contents)
        self.assertIn("64e7b6ef1d86d3f4b6e4e009", contents)
        self.assertEqual(collection.find.call_count, 2)
        self.assertEqual(
            collection.find.call_args_list[1].args[0],
            {"page_id": {"$in": [73009]}},
        )

    def test_single_reader_falls_back_to_page_id(self):
        manager = MongoManager.__new__(MongoManager)
        manager.blog_content = Mock()
        manager.blog_content.find_one.side_effect = [
            None,
            {"_id": ObjectId("64e7b6ef1d86d3f4b6e4e010"), "page_id": 73010,
             "body": [{"type": "markdown_block"}]},
        ]

        content = manager.get_blog_content_compatible("64e7b6ef1d86d3f4b6e4e099", page_id=73010)

        self.assertEqual(content["page_id"], 73010)
        self.assertEqual(manager.blog_content.find_one.call_count, 2)
        self.assertEqual(
            manager.blog_content.find_one.call_args_list[1].args[0],
            {"page_id": 73010},
        )


class ContentIndexElasticsearchTests(SimpleTestCase):
    """创建路径只能新增精确模板和物理索引，不能覆盖已有资源。"""

    def _client_with_mapping(self):
        properties = {field_name: {} for field_name in CONTENT_INDEX_REQUIRED_FIELDS}
        client = Mock()
        client.indices.exists_index_template.return_value = False
        client.indices.exists.return_value = False
        client.indices.get_mapping.return_value = {
            "wagtailblog-test-content-v001": {
                "mappings": {"dynamic": "strict", "properties": properties}
            }
        }
        return client

    def test_create_adds_template_then_new_physical_index(self):
        client = self._client_with_mapping()
        backend = SimpleNamespace(es=client)
        definition = build_content_index_template("wagtailblog-test-content-v001")

        with patch("search.services.elasticsearch.get_search_backend", return_value=backend):
            result = create_content_search_index(
                "default",
                "wagtailblog-test-content-v001",
                "wagtailblog-test-content-v001-template",
                definition,
            )

        self.assertEqual(result, ContentSearchIndexCreateResult(True, True))
        client.indices.put_index_template.assert_called_once_with(
            name="wagtailblog-test-content-v001-template",
            index_patterns=["wagtailblog-test-content-v001"],
            template=definition["template"],
            meta=definition["_meta"],
            priority=200,
            create=True,
        )
        client.indices.create.assert_called_once_with(index="wagtailblog-test-content-v001")


class ContentIndexCreateCommandTests(TestCase):
    """命令默认不写入；确认后仍只登记禁用中的 building 目标。"""

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_CONNECTION_NAME="default",
        CONTENT_SEARCH_INDEX_SHARDS=1,
        CONTENT_SEARCH_INDEX_REPLICAS=0,
        CONTENT_SEARCH_INDEX_REFRESH_INTERVAL="30s",
    )
    def test_dry_run_has_no_es_or_mysql_writes(self):
        output = StringIO()
        with patch(
            "search.management.commands.search_create_content_index.create_content_search_index"
        ) as create_index:
            call_command("search_create_content_index", "--target", "content-v001", stdout=output)

        report = json.loads(output.getvalue())
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["target_enabled"])
        create_index.assert_not_called()
        self.assertFalse(ContentSearchTarget.objects.filter(target_id="content-v001").exists())

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_CONNECTION_NAME="default",
        CONTENT_SEARCH_INDEX_SHARDS=1,
        CONTENT_SEARCH_INDEX_REPLICAS=0,
        CONTENT_SEARCH_INDEX_REFRESH_INTERVAL="30s",
    )
    def test_confirm_registers_disabled_building_target_after_es_creation(self):
        output = StringIO()
        with patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"}), patch(
            "search.management.commands.search_create_content_index.create_content_search_index",
            return_value=ContentSearchIndexCreateResult(True, True),
        ) as create_index:
            call_command(
                "search_create_content_index",
                "--target",
                "content-v001",
                "--confirm",
                stdout=output,
            )

        target = ContentSearchTarget.objects.get(target_id="content-v001")
        build = SearchIndexBuild.objects.get(target=target)
        self.assertFalse(target.enabled)
        self.assertFalse(target.required)
        self.assertEqual(target.role, ContentSearchTargetRole.BUILDING)
        self.assertEqual(build.status, SearchIndexBuildStatus.CREATED)
        self.assertIn('"index_created": true', output.getvalue())
        create_index.assert_called_once()

    @override_settings(CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content")
    def test_confirm_refuses_non_test_environment_before_es_write(self):
        output = StringIO()
        with patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}), patch(
            "search.management.commands.search_create_content_index.create_content_search_index"
        ) as create_index, self.assertRaises(CommandError):
            call_command(
                "search_create_content_index",
                "--target",
                "content-v001",
                "--confirm",
                stdout=output,
            )

        create_index.assert_not_called()
        self.assertIn("test_environment_required", output.getvalue())


class ProductionContentIndexCreateCommandTests(TestCase):
    """生产入口必须在写入前完成独立连接、备份和双重确认检查。"""

    def _call(self, *args, **kwargs):
        return call_command(
            "search_create_production_content_index",
            "--target",
            "prod-content-v001",
            "--index-name",
            "wagtailblog-prod-content-v001",
            "--mapping-version",
            "v001",
            "--backup-reference",
            "wagtailblog3-pre-search-20260811-221511",
            *args,
            **kwargs,
        )

    @override_settings(**PRODUCTION_INDEX_SETTINGS)
    def test_dry_run_never_writes_or_requires_the_write_flag(self):
        output = StringIO()
        with patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}), patch(
            "search.management.commands.search_create_production_content_index.Path"
        ) as path, patch(
            "search.management.commands.search_create_production_content_index.create_content_search_index"
        ) as create_index:
            path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True
            self._call(stdout=output)

        report = json.loads(output.getvalue())
        self.assertTrue(report["dry_run"])
        self.assertTrue(report["ready_for_confirm"])
        create_index.assert_not_called()
        self.assertFalse(ContentSearchTarget.objects.filter(target_id="prod-content-v001").exists())

    @override_settings(**PRODUCTION_INDEX_SETTINGS)
    def test_confirm_requires_second_confirmation_before_es_write(self):
        output = StringIO()
        with patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}), patch(
            "search.management.commands.search_create_production_content_index.Path"
        ) as path, patch(
            "search.management.commands.search_create_production_content_index.create_content_search_index"
        ) as create_index, self.assertRaises(CommandError):
            path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True
            self._call("--confirm", stdout=output)

        create_index.assert_not_called()
        self.assertIn("second_production_confirmation_required", output.getvalue())

    def test_dry_run_refuses_without_explicit_cluster_mode(self):
        output = StringIO()
        production_settings = {
            **PRODUCTION_INDEX_SETTINGS,
            "CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED": False,
            "CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED": False,
        }
        with self.settings(**production_settings), patch.dict(
            os.environ, {"WAGTAILBLOG_ENV": "production"}
        ), patch(
            "search.management.commands.search_create_production_content_index.Path"
        ) as path:
            path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True
            self._call(stdout=output)

        report = json.loads(output.getvalue())
        self.assertFalse(report["ready_for_confirm"])
        self.assertIn("explicit_production_cluster_mode_required", report["refused"])

    @override_settings(**PRODUCTION_INDEX_SETTINGS)
    def test_confirm_creates_only_disabled_building_target(self):
        output = StringIO()
        with patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}), patch(
            "search.management.commands.search_create_production_content_index.Path"
        ) as path, patch(
            "search.management.commands.search_create_production_content_index.create_content_search_index",
            return_value=ContentSearchIndexCreateResult(True, True),
        ) as create_index:
            path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True
            self._call(
                "--confirm",
                "--confirm-production-index-create",
                stdout=output,
            )

        target = ContentSearchTarget.objects.get(target_id="prod-content-v001")
        self.assertEqual(target.connection_name, "content_production")
        self.assertFalse(target.enabled)
        self.assertFalse(target.required)
        self.assertEqual(target.role, ContentSearchTargetRole.BUILDING)
        self.assertEqual(SearchIndexBuild.objects.get(target=target).status, SearchIndexBuildStatus.CREATED)
        create_index.assert_called_once()

    @override_settings(**PRODUCTION_INDEX_SETTINGS)
    def test_confirm_refuses_when_a_mutating_search_feature_is_enabled(self):
        output = StringIO()
        with patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}), patch(
            "search.management.commands.search_create_production_content_index.Path"
        ) as path, patch(
            "search.management.commands.search_create_production_content_index.create_content_search_index"
        ) as create_index, self.settings(CONTENT_SEARCH_PRODUCER_ENABLED=True), self.assertRaises(CommandError):
            path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True
            self._call(
                "--confirm",
                "--confirm-production-index-create",
                stdout=output,
            )

        create_index.assert_not_called()
        self.assertIn("search_feature_flags_must_remain_disabled", output.getvalue())


class ProductionContentAliasSwitchCommandTests(TestCase):
    """生产读切换必须在同一个生产命名空间内完成预检和实际写入。"""

    def setUp(self):
        self.target = ContentSearchTarget.objects.create(
            target_id="prod-content-v001",
            connection_name="content_production",
            index_name="wagtailblog-prod-content-v001",
            role=ContentSearchTargetRole.BUILDING,
            enabled=True,
        )
        self.build = SearchIndexBuild.objects.create(
            target=self.target,
            mapping_version="content-v001-balanced",
            status=SearchIndexBuildStatus.READY,
        )

    def _call(self, *args, **kwargs):
        return call_command(
            "search_switch_production_content_alias",
            "--target",
            self.target.target_id,
            "--backup-reference",
            "wagtailblog3-pre-search-20260811-221511",
            *args,
            **kwargs,
        )

    @override_settings(**PRODUCTION_ALIAS_SWITCH_SETTINGS)
    def test_dry_run_uses_production_alias_and_prefix(self):
        output = StringIO()
        with (
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}),
            patch("search.management.commands.search_switch_production_content_alias.Path") as path,
            patch(
                "search.management.commands.search_switch_production_content_alias.get_content_search_read_alias_indices",
                return_value=(),
            ) as get_alias_indices,
            patch("search.management.commands.search_switch_production_content_alias.switch_content_search_read_alias") as switch,
        ):
            path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True
            self._call(stdout=output)

        report = json.loads(output.getvalue())
        self.assertTrue(report["ready_for_confirm"])
        self.assertEqual(report["alias"], "wagtailblog-prod-content-read")
        self.assertEqual(report["current_indices"], [])
        get_alias_indices.assert_called_once_with(
            self.target,
            "wagtailblog-prod-content-read",
            index_prefix="wagtailblog-prod-content",
        )
        switch.assert_not_called()

    @override_settings(
        **{
            **PRODUCTION_ALIAS_SWITCH_SETTINGS,
            "CONTENT_SEARCH_CONNECTION_NAME": "default",
            "CONTENT_SEARCH_INDEX_PREFIX": "wagtailblog-test-content",
            "CONTENT_SEARCH_READ_ALIAS": "wagtailblog-test-content-read",
        }
    )
    def test_dry_run_refuses_test_runtime_namespace_before_alias_write(self):
        output = StringIO()
        with (
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}),
            patch("search.management.commands.search_switch_production_content_alias.Path") as path,
            patch(
                "search.management.commands.search_switch_production_content_alias.get_content_search_read_alias_indices",
                return_value=(),
            ),
            patch("search.management.commands.search_switch_production_content_alias.switch_content_search_read_alias") as switch,
        ):
            path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True
            self._call(stdout=output)

        report = json.loads(output.getvalue())
        self.assertFalse(report["ready_for_confirm"])
        self.assertIn("runtime_connection_must_match_production_connection", report["refused"])
        self.assertIn("runtime_index_prefix_must_match_production_prefix", report["refused"])
        self.assertIn("runtime_read_alias_must_match_production_prefix", report["refused"])
        self.assertEqual(report["alias"], "wagtailblog-prod-content-read")
        switch.assert_not_called()

    @override_settings(
        **{
            **PRODUCTION_ALIAS_SWITCH_SETTINGS,
            "CONTENT_SEARCH_CONNECTION_NAME": "default",
            "CONTENT_SEARCH_INDEX_PREFIX": "wagtailblog-test-content",
            "CONTENT_SEARCH_READ_ALIAS": "wagtailblog-test-content-read",
        }
    )
    def test_confirm_refuses_test_runtime_namespace_before_alias_write(self):
        output = StringIO()
        with (
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}),
            patch("search.management.commands.search_switch_production_content_alias.Path") as path,
            patch(
                "search.management.commands.search_switch_production_content_alias.get_content_search_read_alias_indices",
                return_value=(),
            ),
            patch("search.management.commands.search_switch_production_content_alias.switch_content_search_read_alias") as switch,
            self.assertRaises(CommandError),
        ):
            path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True
            self._call(
                "--confirm",
                "--confirm-production-query-switch",
                stdout=output,
            )

        self.assertIn("runtime_index_prefix_must_match_production_prefix", output.getvalue())
        switch.assert_not_called()
        self.target.refresh_from_db()
        self.build.refresh_from_db()
        self.assertEqual(self.target.role, ContentSearchTargetRole.BUILDING)
        self.assertEqual(self.build.status, SearchIndexBuildStatus.READY)

    @override_settings(**PRODUCTION_ALIAS_SWITCH_SETTINGS)
    def test_confirm_switches_only_production_alias_and_marks_serving(self):
        old_target = ContentSearchTarget.objects.create(
            target_id="prod-content-v000",
            connection_name="content_production",
            index_name="wagtailblog-prod-content-v000",
            role=ContentSearchTargetRole.SERVING,
            required=False,
            enabled=True,
        )
        output = StringIO()
        switched = SimpleNamespace(new_index=self.target.index_name)
        with (
            patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}),
            patch("search.management.commands.search_switch_production_content_alias.Path") as path,
            patch(
                "search.management.commands.search_switch_production_content_alias.get_content_search_read_alias_indices",
                return_value=(),
            ),
            patch("search.management.commands.search_switch_production_content_alias.verify_content_search_index"),
            patch(
                "search.management.commands.search_switch_production_content_alias.switch_content_search_read_alias",
                return_value=switched,
            ) as switch,
        ):
            path.return_value.__truediv__.return_value.__truediv__.return_value.is_file.return_value = True
            self._call(
                "--confirm",
                "--confirm-production-query-switch",
                stdout=output,
            )

        report = json.loads(output.getvalue())
        self.assertTrue(report["alias_changed"])
        switch.assert_called_once_with(
            self.target,
            "wagtailblog-prod-content-v001",
            alias="wagtailblog-prod-content-read",
            expected_indices=(),
            index_prefix="wagtailblog-prod-content",
        )
        self.target.refresh_from_db()
        self.build.refresh_from_db()
        self.assertEqual(self.target.role, ContentSearchTargetRole.SERVING)
        self.assertTrue(self.target.required)
        self.assertEqual(self.build.status, SearchIndexBuildStatus.SERVING)
        old_target.refresh_from_db()
        self.assertFalse(old_target.enabled)
        self.assertFalse(old_target.required)
        self.assertEqual(old_target.role, ContentSearchTargetRole.RETIRED)
