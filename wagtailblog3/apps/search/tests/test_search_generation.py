from unittest.mock import patch

from django.test import TestCase, override_settings

from search.models import (
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchState,
    ContentSearchStatus,
    ContentSearchTarget,
)
from search.services.delivery import due_content_search_delivery_ids, process_content_search_delivery
from search.services.elasticsearch import ContentSearchWriteResult


@override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=True)
class ContentSearchGenerationTests(TestCase):
    """验证公开代际和 Mongo 正文版本阻止迟到搜索事件覆盖新投影。"""

    def setUp(self):
        self.target = ContentSearchTarget.objects.create(
            target_id="generation-test",
            connection_name="default",
            index_name="generation-test-v001",
            enabled=True,
        )

    def _event(self, *, page_id: int, version: int, generation: int | None, body: str | None):
        state = ContentSearchState.objects.create(
            page_id=page_id,
            content_version=version,
            desired_operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
            publication_generation=generation,
            body_version_id=body,
        )
        event = ContentSearchOutbox.objects.create(
            page_id=page_id,
            content_version=version,
            operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
            publication_generation=generation,
            body_version_id=body,
        )
        return state, event

    def test_tombstone_carries_generation_and_body_identity(self):
        _state, event = self._event(page_id=91001, version=1, generation=7, body="body-7")
        delivery_id = due_content_search_delivery_ids()[0]
        with patch(
            "search.services.delivery.write_content_search_document",
            return_value=ContentSearchWriteResult(status="succeeded"),
        ) as writer:
            self.assertEqual(process_content_search_delivery(delivery_id), ContentSearchStatus.SUCCEEDED)
        document = writer.call_args.args[1]
        self.assertEqual(document["publication_generation"], 7)
        self.assertEqual(document["body_version_id"], "body-7")

    def test_old_format_event_is_superseded_after_state_has_identity(self):
        ContentSearchState.objects.create(
            page_id=91002,
            content_version=1,
            desired_operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
            publication_generation=8,
            body_version_id="body-8",
        )
        event = ContentSearchOutbox.objects.create(
            page_id=91002,
            content_version=1,
            operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
        )
        delivery_id = due_content_search_delivery_ids()[0]
        with patch("search.services.delivery.write_content_search_document") as writer:
            self.assertEqual(process_content_search_delivery(delivery_id), ContentSearchStatus.SUPERSEDED)
        writer.assert_not_called()

    def test_older_generation_is_superseded_without_es_write(self):
        ContentSearchState.objects.create(
            page_id=91003,
            content_version=1,
            desired_operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
            publication_generation=9,
            body_version_id="body-9",
        )
        event = ContentSearchOutbox.objects.create(
            page_id=91003,
            content_version=1,
            operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
            publication_generation=8,
            body_version_id="body-8",
        )
        delivery_id = due_content_search_delivery_ids()[0]
        with patch("search.services.delivery.write_content_search_document") as writer:
            self.assertEqual(process_content_search_delivery(delivery_id), ContentSearchStatus.SUPERSEDED)
        writer.assert_not_called()
