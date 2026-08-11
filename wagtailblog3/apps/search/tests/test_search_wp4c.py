import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from search.models import (
    ContentSearchTarget,
    ContentSearchTargetRole,
    SearchIndexBuild,
    SearchIndexBuildStatus,
)
from search.services.alias import (
	clear_content_search_read_alias,
	get_content_search_read_alias_indices,
	switch_content_search_read_alias,
)
from search.services.content_query import (
    ContentSearchQueryPage,
    build_content_search_query,
    build_content_search_sort,
    query_content_search_page,
)
from search.services.elasticsearch import ContentSearchElasticsearchError
from search.services.shadow import (
    ContentSearchShadowObserver,
    ShadowSearchRequest,
    ShadowObservedResults,
    classify_shadow_difference,
)
from search.core import perform_search


class ContentSearchQueryTests(SimpleTestCase):
    @override_settings(CONTENT_SEARCH_QUERY_ENABLED=True)
    def test_query_flag_routes_only_blog_type_to_independent_results(self):
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

    @override_settings(CONTENT_SEARCH_QUERY_ENABLED=True, SEARCH_HIGHLIGHTS_ENABLED=False)
    def test_query_flag_does_not_change_all_type_semantics(self):
        query_set = Mock()
        query_set.search.return_value = ["old-result"]
        with (
            patch("search.core._build_base_qs", return_value=query_set),
            patch("search.core.Query.get"),
        ):
            result = perform_search("Django", "all")

        self.assertEqual(result, ["old-result"])
        query_set.search.assert_called_once_with("Django", operator="or", order_by_relevance=True)

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


class ContentSearchShadowTests(SimpleTestCase):
    def _request(self, expected=(101, 102)):
        return ShadowSearchRequest(
            query_string="不应出现在日志中的查询",
            search_type="blog",
            start_date=None,
            end_date=None,
            order_by=None,
            start=0,
            size=20,
            expected_page_ids=expected,
            target=SimpleNamespace(index_name="wagtailblog-test-content-v001"),
        )

    def test_query_hash_does_not_equal_or_log_original_query(self):
        observer = ContentSearchShadowObserver()
        request = self._request()
        self.assertNotIn(request.query_string, observer.query_hash(request))

        with (
            patch(
                "search.services.shadow.query_content_search_page",
                return_value=ContentSearchQueryPage((101, 102), 2, 3),
            ),
            patch("search.services.shadow.logger.info") as log_info,
        ):
            observer._run(request, observer.query_hash(request))

        logged = " ".join(str(value) for call in log_info.call_args_list for value in call.args)
        self.assertNotIn(request.query_string, logged)
        self.assertIn("same", logged)

    @override_settings(
        CONTENT_SEARCH_SHADOW_SAMPLE_RATE=1.0,
        CONTENT_SEARCH_SHADOW_FAILURE_THRESHOLD=2,
        CONTENT_SEARCH_SHADOW_COOLDOWN_SECONDS=30,
    )
    def test_failures_open_breaker_and_sampling_can_submit(self):
        observer = ContentSearchShadowObserver()
        request = self._request()
        observer._executor = Mock()
        observer._semaphore = Mock()
        observer._semaphore.acquire.return_value = True
        future = Mock()
        observer._executor.submit.return_value = future
        self.assertTrue(observer.submit(request))
        observer._executor.submit.assert_called_once()
        observer._record_failure()
        observer._record_failure()
        self.assertTrue(observer._breaker_open())
        observer.reset_for_tests()
        self.assertFalse(observer._breaker_open())

    def test_observed_results_submit_once_per_slice_with_public_ids(self):
        wrapped = Mock()
        wrapped.__getitem__ = Mock(return_value=[SimpleNamespace(pk=101), SimpleNamespace(pk=102)])
        submit = Mock()
        with patch("search.services.shadow.shadow_observer.submit", submit):
            observed = ShadowObservedResults(
                wrapped,
                lambda start, size, expected: self._request(expected),
            )
            self.assertEqual(observed[0:2][0].pk, 101)
            observed[0:2]

        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[0].expected_page_ids, (101, 102))

    def test_difference_classification_is_stable(self):
        self.assertEqual(classify_shadow_difference((1, 2), (1, 2)), "same")
        self.assertEqual(classify_shadow_difference((1, 2), (2, 1)), "order_changed")
        self.assertEqual(classify_shadow_difference((1, 2), (1, 3)), "missing_and_extra")


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
        self.assertEqual(self.build.status, SearchIndexBuildStatus.SERVING)

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
        output = StringIO()
        with (
            patch.dict("os.environ", {"WAGTAILBLOG_ENV": "test"}),
            patch(
                "search.management.commands.search_rollback_content_alias.get_content_search_read_alias_indices",
                return_value=(self.target.index_name,),
            ),
            patch(
                "search.management.commands.search_rollback_content_alias.clear_content_search_read_alias",
                return_value=(self.target.index_name,),
            ) as clear_alias,
        ):
            call_command(
                "search_rollback_content_alias",
                "--target",
                self.target.target_id,
                "--confirm",
                stdout=output,
            )

        report = json.loads(output.getvalue())
        self.assertTrue(report["alias_changed"])
        clear_alias.assert_called_once()
        self.target.refresh_from_db()
        self.build.refresh_from_db()
        self.assertEqual(self.target.role, ContentSearchTargetRole.BUILDING)
        self.assertEqual(self.build.status, SearchIndexBuildStatus.READY)
