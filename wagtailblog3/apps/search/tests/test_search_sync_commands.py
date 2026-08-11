from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from search.models import (
    ContentSearchDelivery,
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchStatus,
    ContentSearchTarget,
)


class ContentSearchReplayDeliveryCommandTests(TestCase):
    """手工重放必须精确、显式确认且不会复活已成功或过期的投递。"""

    def setUp(self):
        event = ContentSearchOutbox.objects.create(
            page_id=90001,
            content_version=1,
            operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
        )
        target = ContentSearchTarget.objects.create(
            target_id="replay-command-target",
            connection_name="default",
            index_name="replay-command-index",
            enabled=True,
        )
        self.delivery = ContentSearchDelivery.objects.create(
            event=event,
            target=target,
            status=ContentSearchStatus.DEAD,
        )

    @override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=True)
    def test_confirmation_requeues_one_dead_delivery_and_wakes_maintenance(self):
        output = StringIO()
        with patch("search.tasks.wake_content_search_delivery.apply_async") as apply_async:
            with self.captureOnCommitCallbacks(execute=True):
                call_command(
                    "search_replay_delivery",
                    str(self.delivery.event.event_id),
                    self.delivery.target.target_id,
                    "--reason",
                    "mapping corrected",
                    "--confirm",
                    stdout=output,
                )

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, ContentSearchStatus.RETRY)
        self.assertEqual(self.delivery.last_error_code, "manual_replay")
        apply_async.assert_called_once()
        self.assertIn("target_id=replay-command-target", output.getvalue())

    @override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=True)
    def test_confirmation_is_required_before_any_delivery_write(self):
        with self.assertRaises(CommandError):
            call_command(
                "search_replay_delivery",
                str(self.delivery.event.event_id),
                self.delivery.target.target_id,
                "--reason",
                "operator review",
            )

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, ContentSearchStatus.DEAD)

    @override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=True)
    def test_completed_delivery_cannot_be_replayed(self):
        self.delivery.status = ContentSearchStatus.SUCCEEDED
        self.delivery.save(update_fields=("status",))

        with self.assertRaises(CommandError):
            call_command(
                "search_replay_delivery",
                str(self.delivery.event.event_id),
                self.delivery.target.target_id,
                "--reason",
                "operator review",
                "--confirm",
            )

    @override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=True)
    def test_event_and_target_must_identify_the_same_delivery(self):
        other_target = ContentSearchTarget.objects.create(
            target_id="other-replay-target",
            connection_name="default",
            index_name="other-replay-index",
            enabled=True,
        )

        with self.assertRaises(CommandError):
            call_command(
                "search_replay_delivery",
                str(self.delivery.event.event_id),
                other_target.target_id,
                "--reason",
                "operator review",
                "--confirm",
            )

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, ContentSearchStatus.DEAD)
