import json
import os
import runpy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from search.models import ContentSearchTarget, ContentSearchTargetRole
from search.services.content_query import get_content_search_shadow_target
from search.services.content_query import (
    ContentSearchHitHighlight,
    ContentSearchQueryPage,
    ContentSearchResults,
    query_content_search_page,
)
from search.services.highlights import HIGHLIGHT_END_TAG, HIGHLIGHT_START_TAG


class SearchClusterPreflightTests(SimpleTestCase):
    @override_settings(
        CONTENT_SEARCH_CONNECTION_NAME="default",
        WAGTAILSEARCH_BACKENDS={"default": {"BACKEND": "test"}},
    )
    def test_preflight_is_read_only_and_redacts_connection_details(self):
        client = Mock()
        client.info.return_value = {
            "cluster_name": "test-cluster",
            "version": {"number": "8.19.3"},
        }
        client.cluster.health.return_value = {
            "status": "green",
            "number_of_nodes": 1,
            "active_primary_shards": 1,
            "active_shards": 1,
            "unassigned_shards": 0,
        }
        client.indices.stats.return_value = {
            "indices": {
                "test-index": {
                    "primaries": {
                        "docs": {"count": 117},
                        "store": {"size_in_bytes": 15974},
                    }
                }
            }
        }
        client.nodes.stats.return_value = {
            "nodes": {
                "node-1": {
                    "jvm": {
                        "mem": {
                            "heap_used_percent": 42,
                            "heap_max_in_bytes": 2147483648,
                        }
                    },
                    "fs": {"total": {"available_in_bytes": 10737418240}},
                }
            }
        }
        client.snapshot.get_repository.return_value = {"test-repository": {"type": "fs"}}
        output = StringIO()
        with patch(
            "search.management.commands.search_cluster_preflight.get_content_search_client_for_connection",
            return_value=client,
        ):
            call_command(
                "search_cluster_preflight",
                "--index",
                "test-index",
                "--snapshot-repository",
                "test-repository",
                stdout=output,
            )

        report = json.loads(output.getvalue())
        self.assertTrue(report["read_only"])
        self.assertEqual(report["health"]["status"], "green")
        self.assertEqual(report["index"]["docs_count"], 117)
        self.assertEqual(report["capacity"]["max_heap_used_percent"], 42)
        self.assertEqual(report["capacity"]["min_fs_available_in_bytes"], 10737418240)
        self.assertTrue(report["snapshot"]["exists"])
        client.info.assert_called_once_with()
        client.cluster.health.assert_called_once_with(index="test-index", level="cluster")
        client.indices.stats.assert_called_once_with(index="test-index", metric="docs,store")
        client.nodes.stats.assert_called_once_with(metric="jvm,fs")
        client.snapshot.get_repository.assert_called_once_with(name="test-repository")


class SearchSecondaryConnectionSettingsTests(SimpleTestCase):
    settings_path = (
        Path(__file__).resolve().parents[4] / "wagtailblog3" / "settings" / "database.py"
    )

    def _load_database_settings(self, **environment):
        with patch.dict(os.environ, environment, clear=False):
            return runpy.run_path(str(self.settings_path), run_name="wp6_database_settings")

    def test_secondary_connection_isolated_and_tls_verified(self):
        settings_namespace = self._load_database_settings(
            CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED="true",
            CONTENT_SEARCH_SECONDARY_CONNECTION_NAME="content_secondary_test",
            CONTENT_SEARCH_SECONDARY_URL="https://secondary.test.invalid",
            CONTENT_SEARCH_SECONDARY_AUTH_MODE="api_key",
            CONTENT_SEARCH_SECONDARY_API_KEY="test-only-placeholder",
            CONTENT_SEARCH_SECONDARY_VERIFY_CERTS="true",
        )

        backend = settings_namespace["WAGTAILSEARCH_BACKENDS"]["content_secondary_test"]
        self.assertEqual(backend["URLS"], ["https://secondary.test.invalid"])
        self.assertTrue(backend["OPTIONS"]["verify_certs"])
        self.assertFalse(backend["AUTO_UPDATE"])
        self.assertIn("api_key", backend["OPTIONS"])
        self.assertIn("default", settings_namespace["WAGTAILSEARCH_BACKENDS"])

    def test_production_content_connection_disables_wagtail_auto_update(self):
        settings_namespace = self._load_database_settings(
            CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED="false",
            CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_ENABLED="true",
            CONTENT_SEARCH_PRODUCTION_CONNECTION_NAME="content_production_test",
            CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_URL="http://production.test.invalid",
            CONTENT_SEARCH_PRODUCTION_EXISTING_CLUSTER_AUTH_MODE="none",
        )

        backend = settings_namespace["WAGTAILSEARCH_BACKENDS"]["content_production_test"]
        self.assertEqual(backend["URLS"], ["http://production.test.invalid"])
        self.assertFalse(backend["AUTO_UPDATE"])
        self.assertNotIn("AUTO_UPDATE", settings_namespace["WAGTAILSEARCH_BACKENDS"]["default"])

    def test_secondary_connection_rejects_http(self):
        with self.assertRaisesRegex(ValueError, "不含凭据的 HTTPS URL"):
            self._load_database_settings(
                CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED="true",
                CONTENT_SEARCH_SECONDARY_URL="http://secondary.test.invalid",
                CONTENT_SEARCH_SECONDARY_AUTH_MODE="api_key",
                CONTENT_SEARCH_SECONDARY_API_KEY="test-only-placeholder",
                CONTENT_SEARCH_SECONDARY_VERIFY_CERTS="true",
            )

    def test_secondary_connection_rejects_credentials_in_url(self):
        with self.assertRaisesRegex(ValueError, "不含凭据的 HTTPS URL"):
            self._load_database_settings(
                CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED="true",
                CONTENT_SEARCH_SECONDARY_URL="https://user:password@secondary.test.invalid",
                CONTENT_SEARCH_SECONDARY_AUTH_MODE="api_key",
                CONTENT_SEARCH_SECONDARY_API_KEY="test-only-placeholder",
                CONTENT_SEARCH_SECONDARY_VERIFY_CERTS="true",
            )

    def test_secondary_connection_requires_authentication(self):
        with self.assertRaisesRegex(ValueError, "api_key 或 basic"):
            self._load_database_settings(
                CONTENT_SEARCH_SECONDARY_CONNECTION_ENABLED="true",
                CONTENT_SEARCH_SECONDARY_URL="https://secondary.test.invalid",
                CONTENT_SEARCH_SECONDARY_AUTH_MODE="",
                CONTENT_SEARCH_SECONDARY_VERIFY_CERTS="true",
            )


class CrossClusterShadowTargetTests(TestCase):
    def setUp(self):
        self.target = ContentSearchTarget.objects.create(
            target_id="wp6-secondary-shadow",
            connection_name="content_secondary",
            index_name="wagtailblog-test-secondary-content-v001",
            role=ContentSearchTargetRole.BUILDING,
            enabled=True,
        )

    @override_settings(
        CONTENT_SEARCH_CONNECTION_NAME="default",
        CONTENT_SEARCH_SHADOW_TARGET_ID="wp6-secondary-shadow",
    )
    def test_explicit_shadow_target_can_use_secondary_connection(self):
        target = get_content_search_shadow_target()

        self.assertEqual(target.pk, self.target.pk)
        self.assertEqual(target.connection_name, "content_secondary")


class IndependentSearchHighlightTests(SimpleTestCase):
    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_READ_ALIAS="wagtailblog-test-content-read",
        SEARCH_HIGHLIGHTS_ENABLED=True,
    )
    def test_query_escapes_highlights_and_discards_non_public_hits(self):
        client = Mock()
        client.search.return_value = {
            "hits": {
                "total": {"value": 2, "relation": "eq"},
                "hits": [
                    {
                        "_source": {"page_id": 101},
                        "highlight": {
                            "title": [
                                f"{HIGHLIGHT_START_TAG}Django{HIGHLIGHT_END_TAG} 教程"
                            ],
                            "body_text": [
                                f'<script>alert("x")</script>正文 {HIGHLIGHT_START_TAG}命中{HIGHLIGHT_END_TAG}'
                            ],
                        },
                    },
                    {
                        "_source": {"page_id": 102},
                        "highlight": {
                            "body_text": [
                                f"草稿 {HIGHLIGHT_START_TAG}泄漏{HIGHLIGHT_END_TAG}"
                            ]
                        },
                    },
                ],
            }
        }
        target = SimpleNamespace(connection_name="default")
        with (
            patch("search.services.content_query.get_content_search_client", return_value=client),
            patch(
                "search.services.content_query._public_page_ids_in_order",
                return_value=(101,),
            ),
        ):
            result = query_content_search_page(target, "Django", size=20)

        self.assertEqual(result.page_ids, (101,))
        self.assertEqual(len(result.highlights), 1)
        highlight = result.highlights[0]
        self.assertEqual(highlight.page_id, 101)
        self.assertEqual(str(highlight.title_fragment), "<mark>Django</mark> 教程")
        self.assertNotIn("<script", str(highlight.fragments[0]))
        self.assertIn("<mark>命中</mark>", str(highlight.fragments[0]))
        request = client.search.call_args.kwargs
        self.assertEqual(request["highlight"]["fields"]["body_text"]["max_analyzed_offset"], 100000)

    @override_settings(
        CONTENT_SEARCH_INDEX_PREFIX="wagtailblog-test-content",
        CONTENT_SEARCH_READ_ALIAS="wagtailblog-test-content-read",
        SEARCH_HIGHLIGHTS_ENABLED=False,
    )
    def test_disabled_highlights_keep_independent_query_without_highlight_request(self):
        client = Mock()
        client.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        target = SimpleNamespace(connection_name="default")
        with patch("search.services.content_query.get_content_search_client", return_value=client):
            result = query_content_search_page(target, "Django", size=20)

        self.assertEqual(result.highlights, ())
        self.assertNotIn("highlight", client.search.call_args.kwargs)

    def test_cursor_page_attaches_highlights_after_public_page_lookup(self):
        page = SimpleNamespace(pk=101)
        highlight = ContentSearchHitHighlight(
            page_id=101,
            matched_field="body_text",
            fragments=("正文 <mark>命中</mark>",),
        )
        query_page = ContentSearchQueryPage(
            page_ids=(101,),
            total=1,
            took_ms=2,
            sort_values=((8.5, 101),),
            highlights=(highlight,),
        )
        results = ContentSearchResults(SimpleNamespace(), "Django")
        with (
            patch("search.services.content_query.query_content_search_page", return_value=query_page),
            patch.object(results, "_pages_for_ids", return_value=[page]) as pages_for_ids,
        ):
            cursor_page = results.cursor_page("", 20)

        pages_for_ids.assert_called_once_with([101])
        self.assertEqual(list(cursor_page), [page])
        self.assertEqual(page.search_matched_field, "body_text")
        self.assertEqual(page.search_highlight_fragments, ["正文 <mark>命中</mark>"])
