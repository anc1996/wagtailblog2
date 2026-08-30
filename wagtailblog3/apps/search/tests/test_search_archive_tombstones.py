import json
from io import StringIO
from datetime import timedelta

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
    SearchIndexBuild,
    SearchIndexBuildStatus,
)


class TombstoneArchiveCommandTests(TestCase):
    def setUp(self) -> None:
        self.target = ContentSearchTarget.objects.create(
            target_id="archive-target",
            connection_name="default",
            index_name="archive-content-v001",
            enabled=True,
            required=True,
        )

    def _event(self, page_id: int, version: int, completed_at):
        event = ContentSearchOutbox.objects.create(
            page_id=page_id,
            content_version=version,
            operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
            status=ContentSearchStatus.SUCCEEDED,
            completed_at=completed_at,
        )
        ContentSearchState.objects.create(
            page_id=page_id,
            content_version=version,
            desired_operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
        )
        ContentSearchDelivery.objects.create(
            event=event, target=self.target, status=ContentSearchStatus.SUCCEEDED, completed_at=completed_at
        )
        return event

    def test_reports_eligible_manifest_without_writes(self) -> None:
        completed_at = timezone.now() - timedelta(days=200)
        event = self._event(70001, 3, completed_at)
        before = list(ContentSearchOutbox.objects.values_list("pk", "updated_at"))
        output = StringIO()

        call_command("search_archive_tombstones", stdout=output)

        report = json.loads(output.getvalue())
        self.assertTrue(report["dry_run"])
        self.assertTrue(report["read_only"])
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["manifest"][0]["event_id"], str(event.event_id))
        self.assertNotIn("body", output.getvalue())
        self.assertEqual(before, list(ContentSearchOutbox.objects.values_list("pk", "updated_at")))

    def test_refuses_missing_delivery_and_active_build(self) -> None:
        event = self._event(70002, 1, timezone.now() - timedelta(days=200))
        ContentSearchDelivery.objects.filter(event=event).delete()
        SearchIndexBuild.objects.create(
            target=self.target, mapping_version="v005", status=SearchIndexBuildStatus.BACKFILLING
        )
        output = StringIO()

        call_command("search_archive_tombstones", stdout=output)

        report = json.loads(output.getvalue())
        self.assertEqual(report["candidate_count"], 0)
        self.assertGreaterEqual(report["refusal_counts"]["delivery_missing:archive-target"], 1)
        self.assertGreaterEqual(report["refusal_counts"]["index_build_running"], 1)

    def test_retention_cutoff_excludes_recent_events(self) -> None:
        self._event(70003, 1, timezone.now() - timedelta(days=2))
        output = StringIO()

        call_command("search_archive_tombstones", "--retention-days", "30", stdout=output)

        report = json.loads(output.getvalue())
        self.assertEqual(report["scanned"], 0)
        self.assertEqual(report["candidate_count"], 0)
