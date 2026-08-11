import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from search.models import (
    ContentSearchDelivery,
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchState,
    ContentSearchStatus,
    ContentSearchTarget,
)
from search.services.elasticsearch import ContentSearchElasticsearchError
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


class ContentSearchObservabilityFixtureMixin:
    """构造最小同步记录，不依赖真实 Elasticsearch 或 Mongo 服务。"""

    def _target(self, target_id="observability-target"):
        return ContentSearchTarget.objects.create(
            target_id=target_id,
            connection_name="default",
            index_name="observability-content-v001",
            enabled=True,
        )

    def _event(self, page_id, content_version, operation, searchable, content_hash=None):
        return ContentSearchOutbox.objects.create(
            page_id=page_id,
            content_version=content_version,
            operation=operation,
            searchable=searchable,
            content_hash=content_hash,
        )


class ContentSearchStatusCommandTests(ContentSearchObservabilityFixtureMixin, TestCase):
    """状态命令只聚合 MySQL 元数据，不能读取正文或改变 Delivery。"""

    def test_status_command_returns_counts_and_no_writes(self):
        target = self._target()
        retry_event = self._event(91001, 1, ContentSearchOperation.TOMBSTONE, False)
        processing_event = self._event(91002, 1, ContentSearchOperation.TOMBSTONE, False)
        ContentSearchDelivery.objects.create(
            event=retry_event,
            target=target,
            status=ContentSearchStatus.RETRY,
            last_error_code="es_http_429",
            lease_reclaims=2,
        )
        ContentSearchDelivery.objects.create(
            event=processing_event,
            target=target,
            status=ContentSearchStatus.PROCESSING,
            lock_expires_at=timezone.now(),
        )
        before = list(
            ContentSearchDelivery.objects.order_by("pk").values_list("status", "updated_at")
        )
        output = StringIO()

        call_command("search_sync_status", "--target", target.target_id, stdout=output)

        report = json.loads(output.getvalue())
        after = list(
            ContentSearchDelivery.objects.order_by("pk").values_list("status", "updated_at")
        )
        self.assertTrue(report["read_only"])
        self.assertEqual(report["targets"][0]["connection_name"], "default")
        self.assertEqual(report["targets"][0]["status_counts"]["retry"], 1)
        self.assertEqual(report["targets"][0]["lease_reclaims"], 2)
        self.assertEqual(report["targets"][0]["failure_counts"], {"es_http_429": 1})
        self.assertEqual(before, after)


class ContentSearchConsistencyCommandTests(ContentSearchObservabilityFixtureMixin, TestCase):
    """一致性检查必须使用有界游标和有限 ID 样本，不能回传索引正文。"""

    def test_consistency_command_reports_expected_categories_without_writes(self):
        target = self._target()
        states = [
            ContentSearchState.objects.create(
                page_id=92001,
                content_version=1,
                desired_operation=ContentSearchOperation.UPSERT,
                searchable=True,
                content_hash="a" * 64,
            ),
            ContentSearchState.objects.create(
                page_id=92002,
                content_version=2,
                desired_operation=ContentSearchOperation.UPSERT,
                searchable=True,
                content_hash="b" * 64,
            ),
            ContentSearchState.objects.create(
                page_id=92003,
                content_version=3,
                desired_operation=ContentSearchOperation.TOMBSTONE,
                searchable=False,
            ),
            ContentSearchState.objects.create(
                page_id=92004,
                content_version=4,
                desired_operation=ContentSearchOperation.UPSERT,
                searchable=True,
                content_hash="d" * 64,
            ),
            ContentSearchState.objects.create(
                page_id=92005,
                content_version=5,
                desired_operation=ContentSearchOperation.UPSERT,
                searchable=True,
                content_hash="e" * 64,
            ),
            ContentSearchState.objects.create(
                page_id=92006,
                content_version=6,
                desired_operation=ContentSearchOperation.UPSERT,
                searchable=True,
                content_hash="f" * 64,
            ),
        ]
        index_documents = {
            92003: {
                "page_id": 92003,
                "content_version": 3,
                "searchable": True,
                "operation": "upsert",
            },
            92004: {
                "page_id": 92004,
                "content_version": 3,
                "searchable": True,
                "operation": "upsert",
                "content_hash": "d" * 64,
            },
            92005: {
                "page_id": 92005,
                "content_version": 6,
                "searchable": True,
                "operation": "upsert",
                "content_hash": "e" * 64,
            },
            92006: {
                "page_id": 92006,
                "content_version": 6,
                "searchable": True,
                "operation": "upsert",
                "content_hash": "wrong-hash",
            },
        }
        before = list(ContentSearchState.objects.order_by("page_id").values_list("updated_at", flat=True))
        output = StringIO()

        with (
            patch(
                "search.services.consistency.read_content_search_documents",
                return_value=index_documents,
            ),
            patch(
                "search.services.consistency.scan_content_search_documents",
                return_value=[
                    (92003, index_documents[92003]),
                    (92999, {"page_id": 92999, "content_version": 1}),
                ],
            ),
        ):
            call_command(
                "search_consistency_check",
                "--target",
                target.target_id,
                "--limit",
                len(states),
                stdout=output,
            )

        report = json.loads(output.getvalue())["result"]
        after = list(ContentSearchState.objects.order_by("page_id").values_list("updated_at", flat=True))
        self.assertEqual(
            report["counts"],
            {
                "missing": 2,
                "stale": 1,
                "ahead": 1,
                "hash_mismatch": 1,
                "wrong_tombstone": 1,
                "extra": 1,
            },
        )
        self.assertEqual(report["samples"]["extra"], [92999])
        self.assertEqual(before, after)

    def test_non_strict_es_error_returns_sanitized_report(self):
        target = self._target()
        output = StringIO()

        with patch(
            "search.management.commands.search_consistency_check.check_content_search_consistency",
            side_effect=ContentSearchElasticsearchError("es_read_http_503", retryable=True),
        ):
            call_command("search_consistency_check", "--target", target.target_id, stdout=output)

        report = json.loads(output.getvalue())
        self.assertEqual(report["error"], {"code": "es_read_http_503", "retryable": True})
        self.assertNotIn("body", output.getvalue())


class ContentSearchBootstrapStateCommandTests(BlogLifecycleFixtureMixin, TestCase):
    """State 初始化默认只预演，确认后只创建缺失 State 而不写 Outbox 或 ES。"""

    def setUp(self):
        super().setUp()
        self.target = ContentSearchTarget.objects.create(
            target_id="bootstrap-target",
            connection_name="default",
            index_name="bootstrap-content-v001",
            enabled=False,
        )
        self.page = self._create_draft_page("bootstrap 正式正文")
        self.page.save_revision().publish()
        self.page.refresh_from_db()

    def test_bootstrap_defaults_to_dry_run_then_requires_confirm_to_write(self):
        output = StringIO()
        call_command("search_bootstrap_state", "--target", self.target.target_id, stdout=output)

        dry_run_report = json.loads(output.getvalue())
        self.assertTrue(dry_run_report["dry_run"])
        self.assertEqual(dry_run_report["result"]["created"], 1)
        self.assertFalse(ContentSearchState.objects.filter(page_id=self.page.pk).exists())

        output = StringIO()
        call_command(
            "search_bootstrap_state",
            "--target",
            self.target.target_id,
            "--confirm",
            stdout=output,
        )

        state = ContentSearchState.objects.get(page_id=self.page.pk)
        self.assertEqual(state.content_version, 1)
        self.assertTrue(state.searchable)
        self.assertEqual(len(state.content_hash), 64)
        self.assertFalse(ContentSearchOutbox.objects.filter(page_id=self.page.pk).exists())
