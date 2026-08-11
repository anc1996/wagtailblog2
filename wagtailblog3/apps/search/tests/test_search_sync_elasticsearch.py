from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from search.services.elasticsearch import (
    ContentSearchBulkWriteResult,
    ContentSearchElasticsearchError,
    write_content_search_documents,
    read_content_search_documents,
    scan_content_search_documents,
    write_content_search_document,
)


class ContentSearchElasticsearchWriterTests(SimpleTestCase):
    """固定 ES 外部版本写入协议，避免客户端升级后丢失防复活约束。"""

    def setUp(self):
        self.target = SimpleNamespace(
            connection_name="default",
            index_name="content-sync-test-v001",
        )
        self.document = {
            "page_id": 70001,
            "content_version": 9,
            "searchable": False,
            "operation": "tombstone",
        }

    def _backend_with_status(self, status):
        client = Mock()
        client.bulk.return_value = {"items": [{"index": {"status": status}}]}
        return SimpleNamespace(es=client), client

    def test_bulk_write_uses_physical_index_and_external_version(self):
        backend, client = self._backend_with_status(201)
        with patch("search.services.elasticsearch.get_search_backend", return_value=backend):
            result = write_content_search_document(self.target, self.document, 9)

        self.assertEqual(result.status, "succeeded")
        client.bulk.assert_called_once_with(
            operations=[
                {
                    "index": {
                        "_index": "content-sync-test-v001",
                        "_id": "70001",
                        "version": 9,
                        "version_type": "external",
                    }
                },
                self.document,
            ]
        )

    def test_version_conflict_is_superseded_not_retry(self):
        backend, _client = self._backend_with_status(409)
        with patch("search.services.elasticsearch.get_search_backend", return_value=backend):
            result = write_content_search_document(self.target, self.document, 9)

        self.assertEqual(result.status, "superseded")

    def test_rate_limit_is_retryable_and_mapping_error_is_dead(self):
        for status, retryable in ((429, True), (400, False)):
            backend, _client = self._backend_with_status(status)
            with self.subTest(status=status), patch(
                "search.services.elasticsearch.get_search_backend",
                return_value=backend,
            ), self.assertRaises(ContentSearchElasticsearchError) as raised:
                write_content_search_document(self.target, self.document, 9)

            self.assertEqual(raised.exception.retryable, retryable)
            self.assertEqual(raised.exception.code, f"es_http_{status}")

    def test_bulk_writer_keeps_each_document_external_version(self):
        backend = SimpleNamespace(
            es=Mock(
                bulk=Mock(
                    return_value={
                        "items": [
                            {"index": {"status": 201}},
                            {"index": {"status": 409}},
                        ]
                    }
                )
            )
        )
        documents = [
            {"page_id": 70001, "content_version": 9, "searchable": True},
            {"page_id": 70002, "content_version": 11, "searchable": True},
        ]
        with patch("search.services.elasticsearch.get_search_backend", return_value=backend):
            result = write_content_search_documents(self.target, documents)

        self.assertEqual(result, ContentSearchBulkWriteResult(succeeded=1, superseded=1))
        backend.es.bulk.assert_called_once_with(
            operations=[
                {
                    "index": {
                        "_index": "content-sync-test-v001",
                        "_id": "70001",
                        "version": 9,
                        "version_type": "external",
                    }
                },
                documents[0],
                {
                    "index": {
                        "_index": "content-sync-test-v001",
                        "_id": "70002",
                        "version": 11,
                        "version_type": "external",
                    }
                },
                documents[1],
            ]
        )

    def test_bulk_writer_classifies_partial_retry_without_returning_es_body(self):
        backend = SimpleNamespace(
            es=Mock(
                bulk=Mock(
                    return_value={
                        "items": [
                            {"index": {"status": 201}},
                            {"index": {"status": 429, "error": {"reason": "secret"}}},
                        ]
                    }
                )
            )
        )
        documents = [
            {"page_id": 70001, "content_version": 9},
            {"page_id": 70002, "content_version": 11},
        ]
        with patch("search.services.elasticsearch.get_search_backend", return_value=backend):
            with self.assertRaises(ContentSearchElasticsearchError) as raised:
                write_content_search_documents(self.target, documents)

        self.assertEqual(raised.exception.code, "es_bulk_item_http_429")
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("secret", str(raised.exception))


class ContentSearchElasticsearchReaderTests(SimpleTestCase):
    """只读一致性检查只接收有限字段，并把异常转换为脱敏分类。"""

    def setUp(self):
        self.target = SimpleNamespace(
            connection_name="default",
            index_name="content-sync-test-v001",
        )

    def test_mget_returns_page_id_keyed_documents(self):
        backend = SimpleNamespace(
            es=Mock(
                mget=Mock(
                    return_value={
                        "docs": [
                            {
                                "_id": "71001",
                                "found": True,
                                "_source": {
                                    "page_id": 71001,
                                    "content_version": 2,
                                    "content_hash": "a" * 64,
                                },
                            },
                            {"_id": "71002", "found": False},
                        ]
                    }
                )
            )
        )
        with patch("search.services.elasticsearch.get_search_backend", return_value=backend):
            documents = read_content_search_documents(self.target, [71001, 71002])

        self.assertEqual(documents[71001]["content_version"], 2)
        backend.es.mget.assert_called_once_with(
            index="content-sync-test-v001",
            ids=["71001", "71002"],
            source_includes=(
                "page_id",
                "content_version",
                "content_hash",
                "searchable",
                "operation",
            ),
        )

    def test_scan_uses_search_after_and_rejects_unstructured_response(self):
        client = Mock()
        client.search.return_value = {
            "hits": {
                "hits": [
                    {"_source": {"page_id": 71003, "content_version": 4}},
                ]
            }
        }
        backend = SimpleNamespace(es=client)
        with patch("search.services.elasticsearch.get_search_backend", return_value=backend):
            documents = scan_content_search_documents(self.target, 71000, 100)

        self.assertEqual(documents, [(71003, {"page_id": 71003, "content_version": 4})])
        client.search.assert_called_once_with(
            index="content-sync-test-v001",
            query={"match_all": {}},
            sort=[{"page_id": "asc"}],
            search_after=[71000],
            size=100,
            source_includes=(
                "page_id",
                "content_version",
                "content_hash",
                "searchable",
                "operation",
            ),
            track_total_hits=False,
        )

        client.search.return_value = {"unexpected": True}
        with patch("search.services.elasticsearch.get_search_backend", return_value=backend):
            with self.assertRaises(ContentSearchElasticsearchError) as raised:
                scan_content_search_documents(self.target, 71000, 100)
        self.assertEqual(raised.exception.code, "es_invalid_search_response")
