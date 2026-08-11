from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from search.models import (
    ContentSearchDelivery,
    ContentSearchOperation,
    ContentSearchOutbox,
    ContentSearchState,
    ContentSearchStatus,
    ContentSearchTarget,
    ContentSearchTargetRole,
)
from search.services.delivery import (
    due_content_search_delivery_ids,
    process_content_search_delivery,
    reclaim_expired_content_search_deliveries,
)
from search.services.elasticsearch import (
    ContentSearchElasticsearchError,
    ContentSearchWriteResult,
)
from search.tasks import dispatch_pending_content_search_deliveries
from search.tests.test_lifecycle_baseline import BlogLifecycleFixtureMixin


@override_settings(
    CONTENT_SEARCH_PRODUCER_ENABLED=True,
    CONTENT_SEARCH_CONSUMER_ENABLED=True,
    CONTENT_SEARCH_RETRY_BASE_SECONDS=1,
    CONTENT_SEARCH_RETRY_MAX_SECONDS=4,
)
class ContentSearchDeliveryTests(BlogLifecycleFixtureMixin, TestCase):
    """以内存 Mongo 和 ES 写入替身验证 WP3C 的崩溃窗口与版本边界。"""

    def _target(self, *, required=True):
        target_number = ContentSearchTarget.objects.count() + 1
        return ContentSearchTarget.objects.create(
            target_id=f"content-sync-test-{target_number}",
            connection_name="default",
            index_name=f"content-sync-test-v{target_number:03d}",
            required=required,
            enabled=True,
        )

    def _published_event(self, body_text="正式正文"):
        page = self._create_draft_page(body_text)
        revision = page.save_revision()
        with patch("search.services.outbox.schedule_content_search_wakeup"):
            revision.publish()
        page.refresh_from_db()
        return page, ContentSearchOutbox.objects.get(page_id=page.pk)

    def _tombstone_event(self, page_id, content_version):
        state = ContentSearchState.objects.create(
            page_id=page_id,
            content_version=content_version,
            desired_operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
        )
        event = ContentSearchOutbox.objects.create(
            page_id=page_id,
            content_version=content_version,
            operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
        )
        return state, event

    def _delivery_id_for(self, event):
        delivery_ids = due_content_search_delivery_ids()
        delivery = ContentSearchDelivery.objects.get(event=event)
        self.assertIn(delivery.pk, delivery_ids)
        return delivery.pk

    def test_upsert_reads_formal_mongo_and_completes_required_target(self):
        page, event = self._published_event("正式 Mongo 正文")
        self._target()
        delivery_id = self._delivery_id_for(event)

        with patch(
            "search.services.delivery.write_content_search_document",
            return_value=ContentSearchWriteResult(status="succeeded"),
        ) as write_document:
            result = process_content_search_delivery(delivery_id)

        self.assertEqual(result, ContentSearchStatus.SUCCEEDED)
        delivery = ContentSearchDelivery.objects.get(pk=delivery_id)
        event.refresh_from_db()
        document = write_document.call_args.args[1]
        self.assertEqual(document["page_id"], page.pk)
        self.assertEqual(document["body_text"], "正式 Mongo 正文")
        self.assertEqual(len(document["content_hash"]), 64)
        self.assertTrue(document["searchable"])
        self.assertEqual(delivery.status, ContentSearchStatus.SUCCEEDED)
        self.assertEqual(event.status, ContentSearchStatus.SUCCEEDED)

    def test_expired_lease_is_reclaimed_then_processed(self):
        _state, event = self._tombstone_event(page_id=80001, content_version=1)
        self._target()
        delivery_id = self._delivery_id_for(event)
        ContentSearchDelivery.objects.filter(pk=delivery_id).update(
            status=ContentSearchStatus.PROCESSING,
            locked_by="dead-worker",
            lock_expires_at=timezone.now() - timedelta(seconds=1),
        )

        self.assertEqual(reclaim_expired_content_search_deliveries(), 1)
        delivery = ContentSearchDelivery.objects.get(pk=delivery_id)
        self.assertEqual(delivery.lease_reclaims, 1)
        with patch(
            "search.services.delivery.write_content_search_document",
            return_value=ContentSearchWriteResult(status="succeeded"),
        ):
            result = process_content_search_delivery(delivery_id)

        self.assertEqual(result, ContentSearchStatus.SUCCEEDED)
        self.assertEqual(
            ContentSearchDelivery.objects.get(pk=delivery_id).status,
            ContentSearchStatus.SUCCEEDED,
        )

    def test_worker_crash_after_es_success_retries_same_external_version(self):
        _state, event = self._tombstone_event(page_id=80002, content_version=7)
        self._target()
        delivery_id = self._delivery_id_for(event)
        writer = Mock(return_value=ContentSearchWriteResult(status="succeeded"))

        with patch("search.services.delivery.write_content_search_document", writer):
            with patch(
                "search.services.delivery._complete_delivery",
                side_effect=RuntimeError("simulated worker death"),
            ):
                with self.assertRaises(RuntimeError):
                    process_content_search_delivery(delivery_id)

            ContentSearchDelivery.objects.filter(pk=delivery_id).update(
                lock_expires_at=timezone.now() - timedelta(seconds=1),
            )
            result = process_content_search_delivery(delivery_id)

        self.assertEqual(result, ContentSearchStatus.SUCCEEDED)
        self.assertEqual(writer.call_count, 2)
        self.assertEqual(writer.call_args_list[0].args[2], 7)
        self.assertEqual(writer.call_args_list[1].args[2], 7)

    def test_older_event_is_superseded_without_es_write(self):
        ContentSearchState.objects.create(
            page_id=80003,
            content_version=11,
            desired_operation=ContentSearchOperation.UPSERT,
            searchable=True,
            content_hash="a" * 64,
            mongo_content_id="live-current",
        )
        event = ContentSearchOutbox.objects.create(
            page_id=80003,
            content_version=10,
            operation=ContentSearchOperation.UPSERT,
            searchable=True,
            content_hash="b" * 64,
            mongo_content_id="live-old",
        )
        self._target()
        delivery_id = self._delivery_id_for(event)

        with patch("search.services.delivery.write_content_search_document") as write_document:
            result = process_content_search_delivery(delivery_id)

        self.assertEqual(result, ContentSearchStatus.SUPERSEDED)
        write_document.assert_not_called()

    def test_tombstone_prevents_late_upsert_from_resurrecting_document(self):
        ContentSearchState.objects.create(
            page_id=80004,
            content_version=12,
            desired_operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
        )
        old_event = ContentSearchOutbox.objects.create(
            page_id=80004,
            content_version=11,
            operation=ContentSearchOperation.UPSERT,
            searchable=True,
            content_hash="c" * 64,
            mongo_content_id="live-old",
        )
        tombstone_event = ContentSearchOutbox.objects.create(
            page_id=80004,
            content_version=12,
            operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
        )
        self._target()
        old_delivery_id = self._delivery_id_for(old_event)
        tombstone_delivery_id = ContentSearchDelivery.objects.get(event=tombstone_event).pk

        writer = Mock(return_value=ContentSearchWriteResult(status="succeeded"))
        with patch("search.services.delivery.write_content_search_document", writer):
            self.assertEqual(
                process_content_search_delivery(old_delivery_id),
                ContentSearchStatus.SUPERSEDED,
            )
            self.assertEqual(
                process_content_search_delivery(tombstone_delivery_id),
                ContentSearchStatus.SUCCEEDED,
            )

        self.assertEqual(writer.call_count, 1)
        self.assertEqual(
            writer.call_args.args[1],
            {
                "page_id": 80004,
                "content_version": 12,
                "searchable": False,
                "operation": ContentSearchOperation.TOMBSTONE,
            },
        )

    def test_es_rate_limit_becomes_retry_without_writing_error_body(self):
        _state, event = self._tombstone_event(page_id=80005, content_version=1)
        self._target()
        delivery_id = self._delivery_id_for(event)

        with patch(
            "search.services.delivery.write_content_search_document",
            side_effect=ContentSearchElasticsearchError("es_http_429", retryable=True),
        ):
            result = process_content_search_delivery(delivery_id)

        delivery = ContentSearchDelivery.objects.get(pk=delivery_id)
        self.assertEqual(result, ContentSearchStatus.RETRY)
        self.assertEqual(delivery.status, ContentSearchStatus.RETRY)
        self.assertEqual(delivery.last_error_code, "es_http_429")
        self.assertEqual(delivery.last_error_message, "")
        self.assertGreater(delivery.available_at, timezone.now())

    def test_retrying_target_does_not_repeat_already_succeeded_target(self):
        _state, event = self._tombstone_event(page_id=80007, content_version=1)
        successful_target = self._target()
        retrying_target = self._target()
        self.assertEqual(len(due_content_search_delivery_ids()), 2)
        successful_delivery = ContentSearchDelivery.objects.get(
            event=event,
            target=successful_target,
        )
        retrying_delivery = ContentSearchDelivery.objects.get(
            event=event,
            target=retrying_target,
        )
        write_counts = {successful_target.pk: 0, retrying_target.pk: 0}

        def write_document(target, document, content_version):
            write_counts[target.pk] += 1
            if target.pk == retrying_target.pk and write_counts[target.pk] == 1:
                raise ContentSearchElasticsearchError("es_http_429", retryable=True)
            return ContentSearchWriteResult(status="succeeded")

        with patch(
            "search.services.delivery.write_content_search_document",
            side_effect=write_document,
        ):
            self.assertEqual(
                process_content_search_delivery(successful_delivery.pk),
                ContentSearchStatus.SUCCEEDED,
            )
            self.assertEqual(
                process_content_search_delivery(retrying_delivery.pk),
                ContentSearchStatus.RETRY,
            )
            ContentSearchDelivery.objects.filter(pk=retrying_delivery.pk).update(
                available_at=timezone.now() - timedelta(seconds=1),
            )
            self.assertEqual(
                process_content_search_delivery(retrying_delivery.pk),
                ContentSearchStatus.SUCCEEDED,
            )

        self.assertEqual(write_counts[successful_target.pk], 1)
        self.assertEqual(write_counts[retrying_target.pk], 2)

    def test_dispatcher_only_sends_due_delivery_ids_to_maintenance_worker(self):
        _state, event = self._tombstone_event(page_id=80008, content_version=1)
        self._target()

        with patch("search.tasks.consume_content_search_delivery.apply_async") as apply_async:
            dispatched_count = dispatch_pending_content_search_deliveries()

        delivery = ContentSearchDelivery.objects.get(event=event)
        self.assertEqual(dispatched_count, 1)
        apply_async.assert_called_once_with(args=(delivery.pk,), queue="maintenance")

    def test_permanent_es_mapping_failure_enters_dead_letter(self):
        _state, event = self._tombstone_event(page_id=80006, content_version=1)
        self._target()
        delivery_id = self._delivery_id_for(event)

        with patch(
            "search.services.delivery.write_content_search_document",
            side_effect=ContentSearchElasticsearchError("es_http_400", retryable=False),
        ):
            result = process_content_search_delivery(delivery_id)

        delivery = ContentSearchDelivery.objects.get(pk=delivery_id)
        event.refresh_from_db()
        self.assertEqual(result, ContentSearchStatus.DEAD)
        self.assertEqual(delivery.status, ContentSearchStatus.DEAD)
        self.assertEqual(event.status, ContentSearchStatus.DEAD)

    def test_missing_formal_mongo_content_retries_without_es_write(self):
        _page, event = self._published_event("将被删除的正式正文")
        self._target()
        delivery_id = self._delivery_id_for(event)
        self.mongo.live_documents.clear()

        with patch("search.services.delivery.write_content_search_document") as write_document:
            result = process_content_search_delivery(delivery_id)

        delivery = ContentSearchDelivery.objects.get(pk=delivery_id)
        self.assertEqual(result, ContentSearchStatus.RETRY)
        self.assertEqual(delivery.last_error_code, "mongo_formal_content_unavailable")
        write_document.assert_not_called()

    def test_building_target_receives_event_already_completed_by_old_target(self):
        target = ContentSearchTarget.objects.create(
            target_id="content-building-test",
            connection_name="default",
            index_name="content-building-test-v001",
            role=ContentSearchTargetRole.BUILDING,
            required=False,
            enabled=True,
        )
        _state, event = self._tombstone_event(page_id=80009, content_version=1)
        event.status = ContentSearchStatus.SUCCEEDED
        event.save(update_fields=("status",))

        delivery_ids = due_content_search_delivery_ids()
        delivery = ContentSearchDelivery.objects.get(event=event, target=target)

        self.assertIn(delivery.pk, delivery_ids)
        self.assertEqual(delivery.status, ContentSearchStatus.PENDING)


@override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=False)
class ContentSearchDeliveryDisabledTests(TestCase):
    """关闭 consumer flag 时，调度和消费入口均不得改变持久化同步状态。"""

    def test_delivery_consumer_is_inert_when_disabled(self):
        event = ContentSearchOutbox.objects.create(
            page_id=81001,
            content_version=1,
            operation=ContentSearchOperation.TOMBSTONE,
            searchable=False,
        )

        self.assertEqual(due_content_search_delivery_ids(), [])
        self.assertEqual(process_content_search_delivery(999999), "disabled")
        self.assertFalse(ContentSearchDelivery.objects.filter(event=event).exists())
