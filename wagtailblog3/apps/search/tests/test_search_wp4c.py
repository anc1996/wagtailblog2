import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections
from django.test import SimpleTestCase, TestCase, override_settings

from search.models import (
    ContentSearchTarget,
    ContentSearchTargetRole,
    SearchIndexBuild,
    SearchIndexBuildStatus,
)
from search.services.alias import (
	ContentSearchAliasSwitchInProgress,
	_content_search_alias_switch_lock_name,
	clear_content_search_read_alias,
	content_search_alias_switch_lock,
	get_content_search_read_alias_indices,
	switch_content_search_read_alias,
)
from search.services.content_query import (
    ContentSearchQueryUnavailable,
    ContentSearchQueryPage,
    build_content_search_query,
    build_content_search_sort,
    get_content_search_serving_target,
    query_content_search_page,
)
from search.services.elasticsearch import ContentSearchElasticsearchError
from search.core import perform_search


class ContentSearchQueryTests(SimpleTestCase):
    def test_blog_search_always_routes_to_independent_results(self):
        independent_results = object()
        with (
            patch("search.core.build_content_search_results", return_value=independent_results) as build_results,
            patch("search.core.Query.get") as get_query,
            patch("search.core._build_base_qs") as build_base_qs,
        ):
            result = perform_search("Django", "blog")

        self.assertIs(result, independent_results)
        build_results.assert_called_once_with("Django", start_date=None, end_date=None, order_by=None)
        get_query.return_value.add_hit.assert_called_once_with()
        build_base_qs.assert_not_called()

    def test_all_search_always_routes_to_the_federated_builder(self):
        federated_results = object()
        with (
            patch("search.core.build_federated_search_results", return_value=federated_results) as builder,
            patch("search.core.Query.get"),
        ):
            result = perform_search("Django", "all")

        self.assertIs(result, federated_results)
        builder.assert_called_once_with("Django", start_date=None, end_date=None, order_by=None)

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_READ_ALIAS="wagtailblog-test-content-read",
    )
    def test_query_requires_searchable_document_and_public_guard(self):
        backend = SimpleNamespace(
            es=Mock(
                search=Mock(
                    return_value={
                        "took": 4,
                        "hits": {
                            "total": {"value": 2, "relation": "eq"},
                            "hits": [
                                {"_id": "101", "_source": {"page_id": 101}},
                                {"_id": "102", "_source": {"page_id": 102}},
                            ],
                        },
                    }
                )
            )
        )
        target = SimpleNamespace(connection_name="default", index_name="wagtailblog-test-content-v001")
        with (
            patch("search.services.content_query.get_content_search_client", return_value=backend.es),
            patch("search.services.content_query._public_page_ids_in_order", return_value=(101,)),
        ):
            result = query_content_search_page(target, "Django", size=20)

        self.assertEqual(result, ContentSearchQueryPage((101,), 2, 4))
        backend.es.search.assert_called_once()
        request = backend.es.search.call_args.kwargs
        self.assertEqual(request["index"], "wagtailblog-test-content-read")
        self.assertEqual(request["query"]["bool"]["filter"], [{"term": {"searchable": True}}])
        self.assertEqual(request["source_includes"], ("page_id",))

    def test_query_uses_stable_tie_breaker_for_date_sort(self):
        query = build_content_search_query("Django", start_date=SimpleNamespace(isoformat=lambda: "2026-01-01"))
        self.assertEqual(query["bool"]["filter"][0], {"term": {"searchable": True}})
        self.assertEqual(query["bool"]["filter"][1], {"range": {"date": {"gte": "2026-01-01"}}})
        self.assertEqual(build_content_search_sort("-date")[-1], {"page_id": "asc"})

    @override_settings(CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content")
    def test_invalid_content_response_is_classified_without_body(self):
        target = SimpleNamespace(connection_name="default", index_name="wagtailblog-test-content-v001")
        client = Mock()
        client.search.return_value = {"hits": {"unexpected": True}}
        with (
            patch("search.services.content_query.get_content_search_client", return_value=client),
            self.assertRaises(ContentSearchElasticsearchError) as raised,
        ):
            query_content_search_page(target, "Django")
        self.assertEqual(raised.exception.code, "es_invalid_content_search_response")


class ContentSearchAliasTests(SimpleTestCase):
    @override_settings(CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content")
    def test_alias_switch_is_one_atomic_update(self):
        target = SimpleNamespace(connection_name="default")
        indices = Mock()
        indices.get_alias.return_value = {"wagtailblog-test-content-v000": {"aliases": {}}}
        client = SimpleNamespace(indices=indices)
        with patch("search.services.alias.get_content_search_client", return_value=client):
            result = switch_content_search_read_alias(
                target,
                "wagtailblog-test-content-v001",
                expected_indices=("wagtailblog-test-content-v000",),
            )

        self.assertEqual(result.previous_indices, ("wagtailblog-test-content-v000",))
        indices.update_aliases.assert_called_once_with(
            actions=[
                {
                    "remove": {
                        "index": "wagtailblog-test-content-v000",
                        "alias": "wagtailblog-test-content-read",
                    }
                },
                {
                    "add": {
                        "index": "wagtailblog-test-content-v001",
                        "alias": "wagtailblog-test-content-read",
                    }
                },
            ]
        )

    @override_settings(CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content")
    def test_alias_read_rejects_index_outside_environment_prefix(self):
        target = SimpleNamespace(connection_name="default")
        indices = Mock()
        indices.get_alias.return_value = {"wagtailblog-production-v001": {"aliases": {}}}
        client = SimpleNamespace(indices=indices)
        with (
            patch("search.services.alias.get_content_search_client", return_value=client),
            self.assertRaises(ContentSearchElasticsearchError) as raised,
        ):
            get_content_search_read_alias_indices(target)
        self.assertEqual(raised.exception.code, "content_alias_points_outside_prefix")

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_READ_ALIAS="wagtailblog-test-content-read",
    )
    def test_alias_switch_can_explicitly_use_production_namespace(self):
        target = SimpleNamespace(connection_name="content_production")
        indices = Mock()
        indices.get_alias.return_value = {"wagtailblog-prod-content-v000": {"aliases": {}}}
        client = SimpleNamespace(indices=indices)
        with patch("search.services.alias.get_content_search_client", return_value=client):
            result = switch_content_search_read_alias(
                target,
                "wagtailblog-prod-content-v001",
                alias="wagtailblog-prod-content-read",
                expected_indices=("wagtailblog-prod-content-v000",),
                index_prefix="wagtailblog-prod-content",
            )

        self.assertEqual(result.alias, "wagtailblog-prod-content-read")
        indices.update_aliases.assert_called_once_with(
            actions=[
                {
                    "remove": {
                        "index": "wagtailblog-prod-content-v000",
                        "alias": "wagtailblog-prod-content-read",
                    }
                },
                {
                    "add": {
                        "index": "wagtailblog-prod-content-v001",
                        "alias": "wagtailblog-prod-content-read",
                    }
                },
            ]
        )

    @override_settings(CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content")
    def test_alias_clear_is_one_atomic_update(self):
        target = SimpleNamespace(connection_name="default")
        indices = Mock()
        indices.get_alias.return_value = {"wagtailblog-test-content-v001": {"aliases": {}}}
        client = SimpleNamespace(indices=indices)
        with patch("search.services.alias.get_content_search_client", return_value=client):
            removed = clear_content_search_read_alias(
                target,
                expected_indices=("wagtailblog-test-content-v001",),
            )

        self.assertEqual(removed, ("wagtailblog-test-content-v001",))
        indices.update_aliases.assert_called_once_with(
            actions=[
                {
                    "remove": {
                        "index": "wagtailblog-test-content-v001",
                        "alias": "wagtailblog-test-content-read",
                    }
                }
            ]
        )


class ContentSearchAliasCommandTests(TestCase):
    def setUp(self):
        self.target = ContentSearchTarget.objects.create(
            target_id="content-switch-test",
            connection_name="default",
            index_name="wagtailblog-test-content-v001",
            role=ContentSearchTargetRole.BUILDING,
            enabled=True,
        )
        self.build = SearchIndexBuild.objects.create(
            target=self.target,
            mapping_version="content-v001-balanced",
            status=SearchIndexBuildStatus.READY,
        )

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_CONNECTION_NAME="default",
    )
    def test_default_is_read_only_plan(self):
        output = StringIO()
        with (
            patch.dict("os.environ", {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_switch_content_alias.get_content_search_read_alias_indices",
                return_value=("wagtailblog-test-content-v000",),
            ),
            patch("search.management.commands.search_switch_content_alias.switch_content_search_read_alias") as switch,
        ):
            call_command("search_switch_content_alias", "--target", self.target.target_id, stdout=output)

        report = json.loads(output.getvalue())
        self.assertTrue(report["dry_run"])
        switch.assert_not_called()
        self.target.refresh_from_db()
        self.assertEqual(self.target.role, ContentSearchTargetRole.BUILDING)

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_CONNECTION_NAME="default",
    )
    def test_confirm_switches_alias_and_marks_target_serving(self):
        old_target = ContentSearchTarget.objects.create(
            target_id="content-switch-old-serving",
            connection_name="default",
            index_name="wagtailblog-test-content-v000",
            role=ContentSearchTargetRole.SERVING,
            required=True,
            enabled=True,
        )
        SearchIndexBuild.objects.create(
            target=old_target,
            mapping_version="content-v000-balanced",
            status=SearchIndexBuildStatus.SERVING,
        )
        output = StringIO()
        switched = SimpleNamespace(new_index=self.target.index_name)
        with (
            patch.dict("os.environ", {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_switch_content_alias.get_content_search_read_alias_indices",
                return_value=(),
            ),
            patch("search.management.commands.search_switch_content_alias.verify_content_search_index"),
            patch(
                "search.management.commands.search_switch_content_alias.get_content_search_build_gate",
                return_value={"clean": True},
            ),
            patch(
                "search.management.commands.search_switch_content_alias.switch_content_search_read_alias",
                return_value=switched,
            ) as switch,
        ):
            call_command(
                "search_switch_content_alias",
                "--target",
                self.target.target_id,
                "--confirm",
                stdout=output,
            )

        self.assertTrue(json.loads(output.getvalue())["alias_changed"])
        switch.assert_called_once()
        self.target.refresh_from_db()
        self.build.refresh_from_db()
        self.assertEqual(self.target.role, ContentSearchTargetRole.SERVING)
        self.assertTrue(self.target.required)
        self.assertEqual(self.build.status, SearchIndexBuildStatus.SERVING)
        old_target.refresh_from_db()
        self.assertFalse(old_target.enabled)
        self.assertFalse(old_target.required)
        self.assertEqual(old_target.role, ContentSearchTargetRole.RETIRED)

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_CONNECTION_NAME="default",
    )
    def test_confirm_refuses_dirty_fresh_gate_before_alias_write(self):
        with (
            patch.dict("os.environ", {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_switch_content_alias.get_content_search_read_alias_indices",
                return_value=("wagtailblog-test-content-v000",),
            ),
            patch(
                "search.management.commands.search_switch_content_alias.get_content_search_build_gate",
                return_value={"clean": False},
            ),
            patch(
                "search.management.commands.search_switch_content_alias.switch_content_search_read_alias"
            ) as switch,
            self.assertRaisesMessage(CommandError, "content_build_gate_not_clean"),
        ):
            call_command(
                "search_switch_content_alias",
                "--target",
                self.target.target_id,
                "--confirm",
            )

        switch.assert_not_called()

    @override_settings(CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content")
    def test_confirm_refuses_production_before_alias_write(self):
        with (
            patch.dict("os.environ", {"WAGTAILBLOG_ENV": "production"}),
            patch(
                "search.management.commands.search_switch_content_alias.get_content_search_read_alias_indices",
                return_value=(),
            ),
            patch("search.management.commands.search_switch_content_alias.switch_content_search_read_alias") as switch,
            self.assertRaises(CommandError),
        ):
            call_command(
                "search_switch_content_alias",
                "--target",
                self.target.target_id,
                "--confirm",
            )
        switch.assert_not_called()

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_CONNECTION_NAME="default",
    )
    def test_confirm_rolls_alias_back_to_old_search(self):
        self.target.role = ContentSearchTargetRole.SERVING
        self.target.save(update_fields=("role",))
        self.build.status = SearchIndexBuildStatus.SERVING
        self.build.save(update_fields=("status",))
        previous_target = ContentSearchTarget.objects.create(
            target_id="content-switch-previous",
            connection_name="default",
            index_name="wagtailblog-test-content-v000",
            role=ContentSearchTargetRole.BUILDING,
            required=False,
            enabled=True,
        )
        previous_build = SearchIndexBuild.objects.create(
            target=previous_target,
            mapping_version="content-v000-balanced",
            status=SearchIndexBuildStatus.READY,
        )
        output = StringIO()
        with (
            patch.dict("os.environ", {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_rollback_content_alias.get_content_search_read_alias_indices",
                return_value=(self.target.index_name,),
            ),
            patch(
                "search.management.commands.search_rollback_content_alias.switch_content_search_read_alias",
                return_value=SimpleNamespace(new_index="wagtailblog-test-content-v000"),
            ) as switch_alias,
            patch(
                "search.management.commands.search_rollback_content_alias.get_content_search_build_gate",
                return_value={"clean": True},
            ),
        ):
            call_command(
                "search_rollback_content_alias",
                "--target",
                self.target.target_id,
                "--previous-index",
                "wagtailblog-test-content-v000",
                "--confirm",
                stdout=output,
            )

        report = json.loads(output.getvalue())
        self.assertTrue(report["alias_changed"])
        self.assertEqual(report["new_indices"], ["wagtailblog-test-content-v000"])
        switch_alias.assert_called_once()
        self.target.refresh_from_db()
        self.build.refresh_from_db()
        self.assertEqual(self.target.role, ContentSearchTargetRole.RETIRED)
        self.assertFalse(self.target.enabled)
        self.assertFalse(self.target.required)
        self.assertEqual(self.build.status, SearchIndexBuildStatus.READY)
        previous_target.refresh_from_db()
        previous_build.refresh_from_db()
        self.assertEqual(previous_target.role, ContentSearchTargetRole.SERVING)
        self.assertTrue(previous_target.enabled)
        self.assertTrue(previous_target.required)
        self.assertEqual(previous_build.status, SearchIndexBuildStatus.SERVING)
        self.assertEqual(get_content_search_serving_target().pk, previous_target.pk)

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_CONNECTION_NAME="default",
    )
    def test_confirm_rollback_refuses_retired_target_before_alias_write(self):
        self.target.role = ContentSearchTargetRole.SERVING
        self.target.required = True
        self.target.save(update_fields=("role", "required"))
        self.build.status = SearchIndexBuildStatus.SERVING
        self.build.save(update_fields=("status",))
        previous_target = ContentSearchTarget.objects.create(
            target_id="content-switch-retired",
            connection_name="default",
            index_name="wagtailblog-test-content-v000",
            role=ContentSearchTargetRole.RETIRED,
            required=False,
            enabled=False,
        )
        SearchIndexBuild.objects.create(
            target=previous_target,
            mapping_version="content-v000-balanced",
            status=SearchIndexBuildStatus.READY,
        )
        with (
            patch.dict("os.environ", {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_rollback_content_alias.get_content_search_read_alias_indices",
                return_value=(self.target.index_name,),
            ),
            patch(
                "search.management.commands.search_rollback_content_alias.switch_content_search_read_alias"
            ) as switch_alias,
            self.assertRaisesMessage(CommandError, "previous_target_requires_rebuild"),
        ):
            call_command(
                "search_rollback_content_alias",
                "--target",
                self.target.target_id,
                "--previous-index",
                previous_target.index_name,
                "--confirm",
            )

        switch_alias.assert_not_called()

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_CONNECTION_NAME="default",
    )
    def test_confirm_rollback_requires_clean_rebuilt_target_before_alias_write(self):
        self.target.role = ContentSearchTargetRole.SERVING
        self.target.required = True
        self.target.save(update_fields=("role", "required"))
        self.build.status = SearchIndexBuildStatus.SERVING
        self.build.save(update_fields=("status",))
        previous_target = ContentSearchTarget.objects.create(
            target_id="content-switch-rebuilding",
            connection_name="default",
            index_name="wagtailblog-test-content-v000",
            role=ContentSearchTargetRole.BUILDING,
            required=False,
            enabled=True,
        )
        SearchIndexBuild.objects.create(
            target=previous_target,
            mapping_version="content-v000-balanced",
            status=SearchIndexBuildStatus.READY,
        )
        with (
            patch.dict("os.environ", {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_rollback_content_alias.get_content_search_read_alias_indices",
                return_value=(self.target.index_name,),
            ),
            patch(
                "search.management.commands.search_rollback_content_alias.get_content_search_build_gate",
                return_value={"clean": False},
            ),
            patch(
                "search.management.commands.search_rollback_content_alias.switch_content_search_read_alias"
            ) as switch_alias,
            self.assertRaisesMessage(CommandError, "previous_build_gate_not_clean"),
        ):
            call_command(
                "search_rollback_content_alias",
                "--target",
                self.target.target_id,
                "--previous-index",
                previous_target.index_name,
                "--confirm",
            )

        switch_alias.assert_not_called()

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_CONNECTION_NAME="default",
    )
    def test_confirm_rollback_requires_previous_index_before_alias_write(self):
        self.target.role = ContentSearchTargetRole.SERVING
        self.target.save(update_fields=("role",))
        self.build.status = SearchIndexBuildStatus.SERVING
        self.build.save(update_fields=("status",))
        with (
            patch.dict("os.environ", {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_rollback_content_alias.get_content_search_read_alias_indices",
                return_value=(self.target.index_name,),
            ),
            patch(
                "search.management.commands.search_rollback_content_alias.switch_content_search_read_alias"
            ) as switch_alias,
            self.assertRaisesMessage(CommandError, "previous_index_required"),
        ):
            call_command(
                "search_rollback_content_alias",
                "--target",
                self.target.target_id,
                "--confirm",
            )

        switch_alias.assert_not_called()


class ContentSearchTargetSelectionTests(TestCase):
    @override_settings(CONTENT_SEARCH_CONNECTION_NAME="default")
    def test_query_rejects_optional_serving_target(self):
        ContentSearchTarget.objects.create(
            target_id="content-optional-serving",
            connection_name="default",
            index_name="wagtailblog-test-content-v099",
            role=ContentSearchTargetRole.SERVING,
            required=False,
            enabled=True,
        )

        with self.assertRaisesMessage(ContentSearchQueryUnavailable, "content_serving_target_missing"):
            get_content_search_serving_target()


class ContentSearchAliasLockTests(TestCase):
    def test_mysql_lock_refuses_parallel_alias_switch(self):
        if connection.vendor != "mysql":
            self.skipTest("仅 MySQL 支持跨连接 advisory lock")

        other_connection = connections["default"].copy()
        lock_name = _content_search_alias_switch_lock_name("default")
        try:
            with other_connection.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 0)", [lock_name])
                self.assertEqual(cursor.fetchone()[0], 1)
            with self.assertRaises(ContentSearchAliasSwitchInProgress):
                with content_search_alias_switch_lock("default"):
                    self.fail("第二个切换不应取得同一连接的 alias 锁")
        finally:
            with other_connection.cursor() as cursor:
                cursor.execute("SELECT RELEASE_LOCK(%s)", [lock_name])
            other_connection.close()
