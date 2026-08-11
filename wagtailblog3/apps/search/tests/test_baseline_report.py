"""WP0：验证只读搜索基线报告的输出边界。"""

import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase


class _FakeCollection:
    def __init__(self, count, indexes):
        self.count = count
        self.indexes = indexes

    def count_documents(self, query):
        return self.count

    def list_indexes(self):
        return self.indexes


class _FakeMongoClient:
    def __init__(self, *args, **kwargs):
        self.admin = self
        self.database = _FakeMongoDatabase({
            "blog_content": _FakeCollection(
                3, [{"name": "page_id_1", "key": {"page_id": 1}}]
            ),
            "blog_page_revision_bodies": _FakeCollection(
                5, [{"name": "_id_", "key": {"_id": 1}}]
            ),
        })
        self.closed = False

    def __getitem__(self, name):
        return self.database

    def command(self, name):
        return {"version": "8.0-test"}

    def close(self):
        self.closed = True


class _FakeMongoDatabase:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections[name]


class _FakeElasticsearchClient:
    def __init__(self):
        self.indices = self
        self.cluster = self

    def get_alias(self, **kwargs):
        return {"wagtail-test-page-v001": {"aliases": {"wagtail-test-page": {}}}}

    def get_settings(self, **kwargs):
        return {
            "wagtail-test-page-v001": {
                "settings": {
                    "index": {"number_of_shards": "1", "number_of_replicas": "0"}
                }
            }
        }

    def get_mapping(self, **kwargs):
        return {
            "wagtail-test-page-v001": {
                "mappings": {"properties": {"title": {"type": "text"}}}
            }
        }

    def count(self, **kwargs):
        return {"count": 7}

    def stats(self, **kwargs):
        return {
            "wagtail-test-page-v001": {
                "primaries": {"store": {"size_in_bytes": 2048}}
            }
        }

    def info(self):
        return {"version": {"number": "8.17.0"}}

    def health(self, **kwargs):
        return {"status": "green"}


class _FakeIndex:
    name = "wagtail-test-page"


class _FakeBackend:
    def __init__(self):
        self.es = _FakeElasticsearchClient()

    def get_index_for_model(self, model):
        return _FakeIndex()


class SearchBaselineReportTests(TestCase):
    """报告仅输出版本、数量、索引摘要和脱敏错误分类。"""

    def test_report_collects_external_metadata_without_document_bodies(self):
        output = StringIO()
        with (
            patch(
                "search.management.commands.search_baseline_report.pymongo.MongoClient",
                _FakeMongoClient,
            ),
            patch(
                "search.management.commands.search_baseline_report.get_search_backend",
                return_value=_FakeBackend(),
            ),
        ):
            call_command("search_baseline_report", stdout=output)

        report = json.loads(output.getvalue())
        self.assertTrue(report["read_only"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["mysql"]["engine"], "mysql")
        self.assertEqual(report["mongodb"]["collections"]["blog_content"]["document_count"], 3)
        self.assertEqual(report["elasticsearch"]["indices"]["wagtail-test-page-v001"]["document_count"], 7)
        self.assertEqual(
            report["elasticsearch"]["indices"]["wagtail-test-page-v001"]["primary_store_size_bytes"],
            2048,
        )
        self.assertNotIn('"body":', json.dumps(report, ensure_ascii=False).lower())

    def test_strict_mode_fails_after_returning_a_sanitised_error_type(self):
        output = StringIO()
        with (
            patch(
                "search.management.commands.search_baseline_report.pymongo.MongoClient",
                side_effect=OSError("connection failed"),
            ),
            patch(
                "search.management.commands.search_baseline_report.get_search_backend",
                return_value=_FakeBackend(),
            ),
            self.assertRaises(CommandError),
        ):
            call_command("search_baseline_report", "--strict", stdout=output)

        report = json.loads(output.getvalue())
        self.assertEqual(report["errors"], [{"component": "mongodb", "error_type": "OSError"}])
        self.assertNotIn("connection failed", output.getvalue())
