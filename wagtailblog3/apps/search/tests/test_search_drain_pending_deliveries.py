import json
import os
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from search.models import (
    ContentSearchDelivery,
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchState,
    ContentSearchStatus,
    ContentSearchTarget,
)


class SearchDrainPendingDeliveriesCommandTests(TestCase):
    """受控排空命令必须先固定 manifest，再把实际状态推进交给正式消费者。"""

    def setUp(self) -> None:
        self.target = ContentSearchTarget.objects.create(
            target_id="drain-test-target",
            connection_name="default",
            index_name="wagtailblog-test-drain-v005",
            enabled=True,
            required=True,
        )
        self.current_delivery = self._delivery(page_id=91001, version=2)
        self.superseded_delivery = self._delivery(page_id=91002, version=1, state_version=2)

    def _delivery(
        self, page_id: int, version: int, state_version: int | None = None
    ) -> ContentSearchDelivery:
        current_version = state_version or version
        ContentSearchState.objects.create(
            page_id=page_id,
            content_version=current_version,
            desired_operation=ContentSearchOperation.UPSERT,
            searchable=True,
            body_version_id=f"body-{page_id}-{current_version}",
            publication_generation=1,
        )
        event = ContentSearchOutbox.objects.create(
            page_id=page_id,
            content_version=version,
            operation=ContentSearchOperation.UPSERT,
            searchable=True,
            body_version_id=f"body-{page_id}-{version}",
            publication_generation=1,
        )
        return ContentSearchDelivery.objects.create(event=event, target=self.target)

    def _dry_run_report(self) -> dict[str, object]:
        output = StringIO()
        call_command(
            "search_drain_pending_deliveries",
            "--target",
            self.target.target_id,
            "--limit",
            "2",
            stdout=output,
        )
        return json.loads(output.getvalue())

    def test_dry_run_classifies_without_writes(self) -> None:
        before = list(
            ContentSearchDelivery.objects.order_by("pk").values_list("pk", "updated_at", "status")
        )

        report = self._dry_run_report()

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["candidate_count"], 2)
        self.assertEqual(
            report["classification_counts"], {"ready": 1, "superseded": 1}
        )
        self.assertEqual(
            [entry["delivery_id"] for entry in report["manifest"]],
            [self.current_delivery.pk, self.superseded_delivery.pk],
        )
        self.assertEqual(
            before,
            list(ContentSearchDelivery.objects.order_by("pk").values_list("pk", "updated_at", "status")),
        )

    @override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=True)
    @patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"}, clear=False)
    def test_confirm_requires_matching_manifest_and_uses_consumer(self) -> None:
        report = self._dry_run_report()

        with patch(
            "search.management.commands.search_drain_pending_deliveries.process_content_search_delivery",
            side_effect=[ContentSearchStatus.SUCCEEDED, ContentSearchStatus.SUPERSEDED],
        ) as process_delivery:
            output = StringIO()
            call_command(
                "search_drain_pending_deliveries",
                "--target",
                self.target.target_id,
                "--limit",
                "2",
                "--confirm",
                "--expected-manifest-sha256",
                report["manifest_sha256"],
                stdout=output,
            )

        result = json.loads(output.getvalue())
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["result_counts"], {"succeeded": 1, "superseded": 1})
        self.assertEqual(
            [call.args[0] for call in process_delivery.call_args_list],
            [self.current_delivery.pk, self.superseded_delivery.pk],
        )

    @override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=True)
    @patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"}, clear=False)
    def test_confirm_stops_on_first_non_terminal_result(self) -> None:
        report = self._dry_run_report()
        output = StringIO()

        with (
            patch(
                "search.management.commands.search_drain_pending_deliveries.process_content_search_delivery",
                side_effect=[ContentSearchStatus.RETRY, ContentSearchStatus.SUCCEEDED],
            ) as process_delivery,
            self.assertRaisesMessage(CommandError, "delivery_processing_stopped"),
        ):
            call_command(
                "search_drain_pending_deliveries",
                "--target",
                self.target.target_id,
                "--limit",
                "2",
                "--confirm",
                "--expected-manifest-sha256",
                report["manifest_sha256"],
                stdout=output,
            )

        stopped = json.loads(output.getvalue())
        self.assertEqual(stopped["stopped_delivery_id"], self.current_delivery.pk)
        self.assertEqual(stopped["stopped_result"], ContentSearchStatus.RETRY)
        process_delivery.assert_called_once_with(self.current_delivery.pk)

    def test_manifest_hash_changes_with_retry_budget(self) -> None:
        first_hash = self._dry_run_report()["manifest_sha256"]
        ContentSearchDelivery.objects.filter(pk=self.current_delivery.pk).update(attempts=1)

        self.assertNotEqual(first_hash, self._dry_run_report()["manifest_sha256"])

    @override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=True)
    @patch.dict(os.environ, {"WAGTAILBLOG_ENV": "test"}, clear=False)
    def test_confirm_rejects_a_changed_or_missing_manifest_hash(self) -> None:
        with self.assertRaisesMessage(CommandError, "manifest_sha256_mismatch"):
            call_command(
                "search_drain_pending_deliveries",
                "--target",
                self.target.target_id,
                "--confirm",
                "--expected-manifest-sha256",
                "0" * 64,
            )

    @override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=True)
    @patch.dict(os.environ, {"WAGTAILBLOG_ENV": "production"}, clear=False)
    def test_confirm_is_limited_to_the_test_environment(self) -> None:
        report = self._dry_run_report()

        with self.assertRaisesMessage(CommandError, "test_environment_required"):
            call_command(
                "search_drain_pending_deliveries",
                "--target",
                self.target.target_id,
                "--confirm",
                "--expected-manifest-sha256",
                report["manifest_sha256"],
            )
