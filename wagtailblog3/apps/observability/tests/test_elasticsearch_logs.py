import tempfile
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from observability.elasticsearch_logs import (
    build_cleanup_plan,
    LogSearchUnavailable,
    build_filebeat_config,
    build_ingest_pipeline,
    delete_logs_by_plan,
    get_log_summary,
    prepare_log_index,
    record_document,
    reset_client_cache,
    search_logs,
)
from observability.cleanup import CleanupResult
from observability.parser import LogRecord
from observability.registry import LOG_FILE_BY_KEY, LOG_FILE_SPECS


ES_SETTINGS = {
    "ENABLED": True,
    "URLS": ["http://es.test:9200"],
    "READ_INDEX": "wagtailblog-test-logs-read",
    "WRITE_INDEX": "wagtailblog-test-logs-write",
    "TIMEOUT": 1,
    "VERIFY_CERTS": False,
    "NUMBER_OF_SHARDS": 1,
    "NUMBER_OF_REPLICAS": 0,
}

TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "observability-elasticsearch-tests",
    }
}


class ElasticsearchLogTests(SimpleTestCase):
    def setUp(self):
        reset_client_cache()

    def tearDown(self):
        reset_client_cache()

    def test_record_document_uses_stable_offset_id_and_redacts_content(self):
        record = LogRecord(
            timestamp=datetime(2026, 8, 5, 8, 0, 0),
            level="ERROR",
            logger="blog.views",
            relative_path="views.py",
            function="save",
            line=10,
            pid=1,
            thread="main",
            message="password=[REDACTED]",
            traceback="",
            raw="password=[REDACTED]",
            source_key="blog_error|8|42",
            source_label="blog error",
            source_path="blog/blog_error.log",
            rotation=0,
            start_offset=10,
            end_offset=30,
        )
        document_id, document = record_document(record, domain="blog")

        self.assertEqual(len(document_id), 64)
        self.assertEqual(document["kind"], "error")
        self.assertEqual(document["document_id"], document_id)
        self.assertEqual(document["source_key"], "blog_error")
        self.assertEqual(document["source_identity"], "blog_error|8|42")
        self.assertEqual(document["source_device"], "8")
        self.assertEqual(document["source_inode"], "42")
        self.assertEqual(document["observed_at"], document["@timestamp"])
        self.assertEqual(document["domain"], "blog")
        self.assertEqual(document["@timestamp"], "2026-08-05T00:00:00+00:00")
        self.assertNotIn("password=secret", document.values())
        rotated_id, _ = record_document(replace(record, rotation=1), domain="blog")
        rewritten_id, _ = record_document(
            replace(record, raw="new record"), domain="blog"
        )
        self.assertEqual(rotated_id, document_id)
        self.assertNotEqual(rewritten_id, document_id)

    def test_disabled_backend_is_explicitly_unavailable(self):
        with override_settings(ELASTICSEARCH_LOGGING={"ENABLED": False}):
            with self.assertRaises(LogSearchUnavailable):
                search_logs()

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS, LOG_DIR="/srv/wagtail/logs")
    def test_generated_shipper_config_is_catalog_based_and_secret_free(self):
        config = build_filebeat_config()
        self.assertEqual(len(config["filebeat.inputs"]), len(LOG_FILE_SPECS))
        first = config["filebeat.inputs"][0]
        self.assertTrue(first["paths"][0].startswith("/srv/wagtail/logs/"))
        self.assertEqual(first["fields_under_root"], True)
        self.assertEqual(config["output.elasticsearch"]["hosts"], ["${ELASTICSEARCH_LOG_URL}"])
        self.assertEqual(
            config["output.elasticsearch"]["parameters"]["pipeline"],
            "wagtailblog-test-logs-normalize-v2",
        )
        self.assertNotIn("output.elasticsearch.pipeline", str(config))
        fingerprint_targets = {
            processor["fingerprint"]["target_field"]
            for processor in config["processors"]
            if "fingerprint" in processor
        }
        self.assertEqual(fingerprint_targets, {"document_id", "@metadata._id"})
        self.assertNotIn("password", str(config).lower())

    def test_generated_shipper_config_can_reference_an_api_key_without_reading_it(self):
        config = build_filebeat_config({**ES_SETTINGS, "AUTH_MODE": "api_key"})
        output = config["output.elasticsearch"]

        self.assertEqual(output["api_key"], "${ELASTICSEARCH_LOG_API_KEY}")
        self.assertNotIn("ELASTICSEARCH_LOG_USERNAME", str(config))

    def test_generated_shipper_config_preserves_tls_verification_choice(self):
        config = build_filebeat_config({**ES_SETTINGS, "VERIFY_CERTS": False})
        self.assertEqual(
            config["output.elasticsearch"]["ssl.verification_mode"], "none"
        )

    def test_ingest_pipeline_has_multiline_safe_parser_and_redaction(self):
        pipeline = build_ingest_pipeline({"TIMEZONE": "UTC"})
        processors = pipeline["processors"]
        self.assertEqual(processors[0]["rename"]["target_field"], "raw")
        self.assertTrue(any("gsub" in processor for processor in processors))
        self.assertTrue(any("grok" in processor for processor in processors))
        self.assertEqual(
            next(processor for processor in processors if "date" in processor)["date"]["timezone"],
            "UTC",
        )
        copied_fields = {
            processor["set"]["field"]
            for processor in processors
            if "set" in processor and "copy_from" in processor["set"]
        }
        self.assertTrue(
            {"observed_at", "source_device", "source_inode", "start_offset"}.issubset(copied_fields)
        )

    def test_ingest_pipeline_keeps_processor_options_inside_processor(self):
        pipeline = build_ingest_pipeline({"TIMEZONE": "UTC"})
        for processor in pipeline["processors"]:
            self.assertEqual(len(processor), 1)
            processor_name, options = next(iter(processor.items()))
            self.assertIsInstance(options, dict, processor_name)

    def test_render_filebeat_command_emits_parseable_yaml(self):
        output = StringIO()
        call_command("render_filebeat_config", stdout=output)
        rendered = yaml.safe_load(output.getvalue())
        self.assertIn("filebeat.inputs", rendered)
        self.assertIn("output.elasticsearch", rendered)

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS)
    def test_search_uses_bounded_query_and_signed_search_after(self):
        client = Mock()
        client.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "one",
                        "sort": ["2026-08-05T00:00:00", "one"],
                        "_source": {
                            "@timestamp": "2026-08-05T00:00:00Z",
                            "level": "ERROR",
                            "kind": "error",
                            "domain": "blog",
                            "source_key": "blog_error",
                            "message": "password=hidden",
                            "raw": "password=hidden",
                            "relative_path": "/home/source/project/views.py",
                        },
                    },
                    {
                        "_id": "two",
                        "sort": ["2026-08-04T00:00:00", "two"],
                        "_source": {
                            "@timestamp": "2026-08-04T00:00:00Z",
                            "level": "ERROR",
                            "kind": "error",
                            "domain": "blog",
                            "source_key": "blog_error",
                            "message": "older",
                        },
                    },
                ]
            }
        }
        with patch("observability.elasticsearch_logs._client", return_value=client):
            result = search_logs(domain="blog", keyword="older", page_size=1)

        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.records[0].timestamp, datetime(2026, 8, 5, 8, 0, 0))
        self.assertEqual(result.records[0].source_path, "blog/blog_error.log")
        self.assertNotIn("hidden", result.records[0].raw)
        self.assertTrue(result.has_more)
        body = client.search.call_args.kwargs
        self.assertEqual(body["index"], "wagtailblog-test-logs-read")
        self.assertEqual(body["size"], 2)
        self.assertFalse(body["track_total_hits"])
        self.assertIn("simple_query_string", body["query"]["bool"]["must"][0])

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS)
    def test_search_normalizes_local_filter_ranges_to_utc(self):
        client = Mock()
        client.search.return_value = {"hits": {"hits": []}}
        with patch("observability.elasticsearch_logs._client", return_value=client):
            search_logs(
                since=datetime(2026, 8, 5, 8, 0, 0),
                until=datetime(2026, 8, 5, 9, 0, 0),
            )

        date_range = next(
            item["range"]["@timestamp"]
            for item in client.search.call_args.kwargs["query"]["bool"]["filter"]
            if "range" in item
        )
        self.assertEqual(date_range["gte"], "2026-08-05T00:00:00+00:00")
        self.assertEqual(date_range["lte"], "2026-08-05T01:00:00+00:00")

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS)
    def test_prepare_creates_only_missing_concrete_index_and_aliases(self):
        client = Mock()
        client.indices.exists.return_value = False
        with patch("observability.elasticsearch_logs._client", return_value=client):
            index = prepare_log_index()

        self.assertEqual(index, "wagtailblog-test-logs-000001")
        kwargs = client.indices.create.call_args.kwargs
        self.assertEqual(kwargs["settings"]["number_of_replicas"], 0)
        self.assertIn("wagtailblog-test-logs-read", kwargs["aliases"])
        self.assertTrue(
            kwargs["aliases"]["wagtailblog-test-logs-write"]["is_write_index"]
        )

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS)
    def test_prepare_repairs_aliases_when_concrete_index_already_exists(self):
        class MissingAlias(Exception):
            status_code = 404

        client = Mock()
        client.indices.exists.return_value = True
        client.indices.get_alias.side_effect = MissingAlias("missing")
        with patch("observability.elasticsearch_logs._client", return_value=client):
            index = prepare_log_index()

        self.assertEqual(index, "wagtailblog-test-logs-000001")
        actions = client.indices.update_aliases.call_args.kwargs["actions"]
        self.assertEqual(
            {item["add"]["alias"] for item in actions},
            {"wagtailblog-test-logs-read", "wagtailblog-test-logs-write"},
        )
        mapping = client.indices.put_mapping.call_args.kwargs
        self.assertIn("source_inode", mapping["properties"])

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS)
    def test_prepare_creates_missing_ingest_pipeline(self):
        class MissingPipeline(Exception):
            status_code = 404

        client = Mock()
        client.indices.exists.return_value = True
        client.ingest.get_pipeline.side_effect = MissingPipeline("missing")
        with patch("observability.elasticsearch_logs._client", return_value=client):
            prepare_log_index()

        kwargs = client.ingest.put_pipeline.call_args.kwargs
        self.assertEqual(kwargs["id"], "wagtailblog-test-logs-normalize-v2")
        self.assertIn("processors", kwargs)

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS)
    def test_cleanup_plan_is_registry_bounded_and_uses_identity_cutoff(self):
        spec = LOG_FILE_BY_KEY["blog_error"]
        result = CleanupResult(
            target=spec.key,
            scope="current",
            file_results=[
                {
                    "source_key": spec.key,
                    "source_path": spec.relative_path,
                    "rotation": 0,
                    "succeeded": True,
                    "bytes_before": 4096,
                    "pre_device": 8,
                    "pre_inode": 42,
                },
                {
                    "source_key": "/etc/passwd",
                    "source_path": "/etc/passwd",
                    "rotation": 0,
                    "succeeded": True,
                    "bytes_before": 1,
                    "pre_device": 1,
                    "pre_inode": 1,
                },
            ],
        )
        plan = build_cleanup_plan(
            (spec,), result, cutoff=datetime(2026, 8, 5, 8, 0, 0)
        )

        self.assertEqual(len(plan.selectors), 1)
        self.assertEqual(plan.spec_keys, (spec.key,))
        self.assertEqual(plan.estimated_bytes, 4096)
        serialized = str(plan.selectors[0])
        self.assertIn("source_device", serialized)
        self.assertIn("source_inode", serialized)
        self.assertIn("start_offset", serialized)
        self.assertIn("observed_at", serialized)
        self.assertIn("@timestamp", serialized)
        self.assertNotIn("/etc/passwd", serialized)

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS)
    def test_delete_by_query_uses_read_alias_and_never_deletes_index(self):
        spec = LOG_FILE_BY_KEY["blog_error"]
        result = CleanupResult(
            target=spec.key,
            scope="rotated",
            file_results=[
                {
                    "source_key": spec.key,
                    "source_path": spec.relative_path,
                    "rotation": 1,
                    "succeeded": True,
                    "bytes_before": 100,
                    "pre_device": 8,
                    "pre_inode": 43,
                }
            ],
        )
        plan = build_cleanup_plan(
            (spec,), result, cutoff=datetime(2026, 8, 5, 8, 0, 0)
        )
        client = Mock()
        client.delete_by_query.return_value = {
            "deleted": 3,
            "version_conflicts": 0,
            "timed_out": False,
        }
        with patch("observability.elasticsearch_logs._client", return_value=client):
            deleted = delete_logs_by_plan(plan)

        self.assertEqual(deleted.deleted, 3)
        kwargs = client.delete_by_query.call_args.kwargs
        self.assertEqual(kwargs["index"], ES_SETTINGS["READ_INDEX"])
        self.assertTrue(kwargs["refresh"])
        client.indices.delete.assert_not_called()

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS)
    def test_prepare_rejects_write_alias_owned_by_another_index(self):
        client = Mock()
        client.indices.exists.return_value = True

        def aliases(name):
            if name == "wagtailblog-test-logs-write":
                return {"other-logs-000001": {"aliases": {name: {}}}}
            return {}

        client.indices.get_alias.side_effect = aliases
        with patch("observability.elasticsearch_logs._client", return_value=client):
            with self.assertRaises(LogSearchUnavailable):
                prepare_log_index()
        client.indices.update_aliases.assert_not_called()

    @override_settings(
        ELASTICSEARCH_LOGGING={**ES_SETTINGS, "FAILURE_COOLDOWN": 60}
    )
    def test_failed_backend_is_short_circuited_for_cooldown_window(self):
        client = Mock()
        client.search.side_effect = TimeoutError("offline")
        with patch("observability.elasticsearch_logs._client", return_value=client), patch(
            "observability.elasticsearch_logs.logger"
        ) as logger:
            with self.assertRaises(LogSearchUnavailable):
                get_log_summary()
            with self.assertRaises(LogSearchUnavailable):
                get_log_summary()
        self.assertEqual(client.search.call_count, 1)
        logger.warning.assert_called_once()

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS)
    def test_summary_uses_single_aggregation_query(self):
        client = Mock()
        client.search.return_value = {
            "aggregations": {
                "error_count": {"doc_count": 4},
                "warning_count": {"doc_count": 3},
                "domains": {
                    "buckets": [
                        {"key": "blog", "errors": {"doc_count": 2}},
                        {"key": "mongo", "errors": {"doc_count": 1}},
                    ]
                },
            }
        }
        with patch("observability.elasticsearch_logs._client", return_value=client):
            summary = get_log_summary()

        self.assertEqual(summary["error_count"], 4)
        self.assertEqual(summary["warning_count"], 3)
        self.assertEqual(summary["errors_by_domain"]["blog"], 2)
        self.assertEqual(client.search.call_count, 1)
        self.assertEqual(client.search.call_args.kwargs["size"], 0)

    @override_settings(ELASTICSEARCH_LOGGING=ES_SETTINGS, CACHES=TEST_CACHES)
    def test_overview_uses_es_summary_without_reading_log_bodies(self):
        from observability.services import get_overview

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with override_settings(LOG_DIR=root), patch(
                "observability.services.get_log_summary",
                return_value={
                    "error_count": 5,
                    "warning_count": 2,
                    "errors_by_domain": {"blog": 3},
                },
            ), patch("observability.services.read_logs") as read_logs:
                overview = get_overview(refresh=True)

        self.assertEqual(overview["error_count"], 5)
        self.assertEqual(overview["warning_count"], 2)
        self.assertEqual(
            next(item for item in overview["modules"] if item["key"] == "blog")[
                "recent_error_count"
            ],
            3,
        )
        read_logs.assert_not_called()
