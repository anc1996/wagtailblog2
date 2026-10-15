"""P04 L0：隔离 ORM/ES 边界，不把 mock 结果当作双库验收。"""

from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from django.test import override_settings
from search.models import ContentSearchOperation, ContentSearchStatus
from search.services import content_index, delivery, elasticsearch, outbox, scope, rebuild, title_index, bootstrap, document, consistency


def snapshot_data() -> dict:
    return {
        "snapshot_schema_version": 1, "snapshot_kind": "blog_v1",
        "page_id": 7, "model_label": "blog.blogpage", "search_type": "blog",
        "publication_generation": 3,
        "publication_ref": {
            "published_revision_id": 19, "source_page_id": None,
            "source_revision_id": None,
        },
        "published_metadata": {
            "title": "正式标题", "summary": "正式摘要",
            "first_published_at": "2026-10-08T01:00:00+00:00",
            "intro": "正式摘要", "date": "2026-10-01",
            "tag_ids": [2], "category_ids": [4],
        },
        "body_ref": {
            "mongo_content_id": "legacy-id", "body_version_id": "body-v7",
            "body_sha256": "a" * 64, "body_schema_version": 1,
        },
        "projection_schema_version": 2,
    }


def event_state() -> tuple:
    from search.services.published_snapshot import PublishedSnapshotV1
    data = snapshot_data()
    snapshot = PublishedSnapshotV1.from_dict(data)
    common = dict(
        page_id=7, content_version=4, searchable=True, content_hash="b" * 64,
        mongo_content_id="legacy-id", body_version_id="body-v7",
        publication_generation=3, projection_schema_version=2,
        model_label="blog.blogpage", search_type="blog", snapshot_kind="blog_v1",
        snapshot_digest=snapshot.digest, published_snapshot=snapshot.to_dict(),
    )
    return (
        SimpleNamespace(operation=ContentSearchOperation.UPSERT, **deepcopy(common)),
        SimpleNamespace(desired_operation=ContentSearchOperation.UPSERT, **deepcopy(common)),
    )


def target(schema=1, *, required=True, role="serving", enabled=True):
    return SimpleNamespace(
        pk=schema, target_id=f"schema-{schema}",
        projection_schema_version=schema, required=required, role=role,
        enabled=enabled, index_name="test-content-v006", connection_name="content",
    )


class SchemaRoutingTests(unittest.TestCase):
    def test_blog_v2_continues_enabled_rollback_v1_but_external_is_v2_only(self):
        blog, _state = event_state()
        external = SimpleNamespace(**vars(blog))
        external.snapshot_kind = "external_v1"
        external.model_label = "guides.guidepage"
        legacy = SimpleNamespace(projection_schema_version=None, snapshot_kind=None)
        cases = [
            (blog, target(1, required=False), True),
            (blog, target(2), True),
            (external, target(1), False),
            (external, target(2), True),
            (legacy, target(1), True),
            (legacy, target(2), False),
            (legacy, target(1, required=False), False),
            (blog, target(1, enabled=False), False),
            (blog, target(2, role="retired"), False),
            (blog, target(True), False),
        ]
        for event, destination, expected in cases:
            with self.subTest(event=event.snapshot_kind, target=vars(destination)):
                self.assertEqual(delivery.event_applies_to_target(event, destination), expected)

    def test_same_version_snapshot_tampering_is_never_ready(self):
        event, state = event_state()
        self.assertEqual(delivery.classify_content_search_event(event, state), "ready")
        for field, value in [
            ("snapshot_digest", "0" * 64),
            ("model_label", "guides.guidepage"),
            ("projection_schema_version", True),
            ("published_snapshot", {}),
        ]:
            damaged = SimpleNamespace(**deepcopy(vars(event)))
            setattr(damaged, field, value)
            with self.subTest(field=field):
                self.assertNotEqual(delivery.classify_content_search_event(damaged, state), "ready")

    def test_old_version_remains_superseded_even_when_payload_is_damaged(self):
        event, state = event_state()
        event.content_version -= 1
        event.published_snapshot = {}
        self.assertEqual(delivery.classify_content_search_event(event, state), "superseded")


class StrictMappingTests(unittest.TestCase):
    def test_v2_mapping_exact_fields_and_schema_metadata(self):
        template = content_index.build_content_index_template(
            "test-content-v006", analyzer_profile="legacy_standard",
            version="v006", projection_schema_version=2,
        )
        mapping = template["template"]["mappings"]
        expected = {
            "page_id", "locale_id", "site_ids", "ancestor_ids", "model_label",
            "search_type", "snapshot_kind", "title", "summary", "body_text",
            "first_published_at", "sort_date", "content_version",
            "publication_generation", "projection_schema_version",
            "snapshot_digest", "content_hash", "searchable", "is_deleted",
        }
        self.assertEqual(set(mapping["properties"]), expected)
        self.assertEqual(mapping["dynamic"], "strict")
        self.assertEqual(mapping["_meta"]["projection_schema_version"], 2)
        self.assertEqual(template["_meta"]["projection_schema_version"], 2)

    def test_bad_schema_and_unexpected_source_rejected_before_external_client(self):
        for document, destination in [
            ({"page_id": 7, "content_version": 4, "body_ref": {}}, target(1)),
            ({"page_id": 7, "content_version": 4, "projection_schema_version": 2}, target(1)),
            ({"page_id": 7, "content_version": 4}, target(2)),
            ({"page_id": 7, "content_version": True}, target(1)),
        ]:
            with self.subTest(document=document), patch.object(elasticsearch, "get_search_backend") as backend:
                with self.assertRaises(elasticsearch.ContentSearchElasticsearchError):
                    elasticsearch.write_content_search_documents(destination, [document])
                backend.assert_not_called()

    def test_verify_rejects_mapping_type_drift(self):
        template = content_index.build_content_index_template(
            "test-content-v005", analyzer_profile="legacy_standard",
        )
        mapping = deepcopy(template["template"]["mappings"])
        mapping["properties"]["content_version"] = {"type": "text"}
        client = MagicMock()
        destination = target(1)
        client.indices.get_mapping.return_value = {destination.index_name: {"mappings": mapping}}
        with patch.object(elasticsearch, "get_content_search_client", return_value=client):
            with self.assertRaises(elasticsearch.ContentSearchElasticsearchError):
                elasticsearch.verify_content_search_index(destination)

    def test_v2_identity_read_white_list_has_digest_and_no_legacy_body_pointer(self):
        client = MagicMock()
        client.mget.return_value = {"docs": []}
        with patch.object(elasticsearch, "get_content_search_client", return_value=client):
            elasticsearch.read_content_search_documents(target(2), [7], include_identity=True)
        fields = client.mget.call_args.kwargs["source_includes"]
        self.assertIn("snapshot_digest", fields)
        self.assertIn("is_deleted", fields)
        self.assertNotIn("body_version_id", fields)


class LockAndLeaseTests(unittest.TestCase):
    def test_claim_does_not_join_or_read_target_in_delivery_transaction(self):
        row = MagicMock(
            pk=1, event_id=2, target_id=3, status=ContentSearchStatus.PENDING,
            attempts=0,
        )
        row.available_at = delivery.timezone.now()
        manager = MagicMock()
        manager.select_for_update.return_value.filter.return_value.first.return_value = row
        with patch.object(delivery.transaction, "atomic", side_effect=lambda: nullcontext()), patch.object(
            delivery.ContentSearchDelivery, "objects", manager
        ):
            lease, status = delivery._claim_content_search_delivery(1)
        manager.select_for_update.return_value.select_related.assert_not_called()
        self.assertEqual(status, ContentSearchStatus.PROCESSING)
        self.assertEqual(lease.event_id, 2)

    def test_result_is_owner_and_processing_conditional_update_not_object_save(self):
        lease = delivery.DeliveryLease(1, 2, 3, "old-token")
        manager = MagicMock()
        manager.filter.return_value.update.return_value = 0
        manager.filter.return_value.values_list.return_value.first.return_value = ContentSearchStatus.PROCESSING
        with patch.object(delivery.ContentSearchDelivery, "objects", manager), patch.object(
            delivery, "refresh_content_search_outbox_status"
        ), patch.object(delivery.transaction, "atomic", side_effect=lambda: nullcontext()):
            result = delivery._complete_delivery(lease, ContentSearchStatus.SUCCEEDED)
        manager.select_for_update.assert_not_called()
        manager.filter.assert_any_call(
            pk=1, locked_by="old-token", status=ContentSearchStatus.PROCESSING
        )
        self.assertEqual(result, ContentSearchStatus.PROCESSING)

    def test_scope_locks_page_before_state_and_reads_public_inside_transaction(self):
        order = []
        pages = MagicMock()
        pages.select_for_update.return_value.only.return_value.filter.return_value.first.side_effect = (
            lambda: order.append("page") or SimpleNamespace(pk=7)
        )
        blogs = MagicMock()
        blogs.filter.return_value.first.return_value = SimpleNamespace(pk=7)
        blogs.live.return_value.public.return_value.filter.return_value.exists.side_effect = (
            lambda: order.append("public") or False
        )
        states = MagicMock()
        states.select_for_update.return_value.filter.return_value.first.side_effect = (
            lambda: order.append("state") or None
        )
        with patch.object(scope.transaction, "atomic", side_effect=lambda: nullcontext()), patch.object(
            scope.Page, "objects", pages
        ), patch.object(scope.BlogPage, "objects", blogs), patch.object(
            scope.ContentSearchState, "objects", states
        ):
            scope._reconcile_page(7)
        self.assertEqual(order[0], "page")
        self.assertLess(order.index("page"), order.index("state"))
        self.assertLess(order.index("page"), order.index("public"))

    def test_c_domain_locates_without_lock_then_locks_state_before_outbox(self):
        order = []
        events, states = MagicMock(), MagicMock()
        events.filter.return_value.values_list.return_value.get.return_value = 7
        states.select_for_update.return_value.filter.return_value.first.side_effect = (
            lambda: order.append("state") or SimpleNamespace(page_id=7)
        )
        events.select_for_update.return_value.get.side_effect = (
            lambda **kwargs: order.append("outbox") or SimpleNamespace(page_id=7)
        )
        with patch.object(delivery.ContentSearchOutbox, "objects", events), patch.object(
            delivery.ContentSearchState, "objects", states
        ):
            delivery._locked_event_and_state(2)
        self.assertEqual(order, ["state", "outbox"])


class OutboxAtomicTests(unittest.TestCase):
    def test_v2_producer_has_independent_default_off_gate(self):
        from search.services.published_snapshot import PublishedSnapshotV1
        snapshot = PublishedSnapshotV1.from_dict(snapshot_data())
        with override_settings(CONTENT_SEARCH_V2_PRODUCER_ENABLED=False, CONTENT_SEARCH_PRODUCER_ENABLED=True):
            self.assertIsNone(outbox.ContentSearchOutboxService.record_published_snapshot(
                SimpleNamespace(pk=7), snapshot, content_hash="b" * 64,
            ))

    def test_snapshot_write_requires_callers_publication_transaction(self):
        from search.services.published_snapshot import PublishedSnapshotV1, PublishedSnapshotError
        snapshot = PublishedSnapshotV1.from_dict(snapshot_data())
        with override_settings(CONTENT_SEARCH_V2_PRODUCER_ENABLED=True):
            with self.assertRaisesRegex(PublishedSnapshotError, "publication_transaction_required"):
                outbox.ContentSearchOutboxService.record_published_snapshot(
                    SimpleNamespace(pk=7), snapshot, content_hash="b" * 64,
                )

    def test_no_compatible_target_is_pending_and_never_succeeded(self):
        event, state = event_state()
        event.status = ContentSearchStatus.SUCCEEDED
        event.save = MagicMock()
        event.deliveries = MagicMock()
        event.deliveries.select_related.return_value.all.return_value = []
        targets = MagicMock()
        targets.filter.return_value.filter.return_value = []
        with patch.object(delivery, "_locked_event_and_state", return_value=(event, state)), patch.object(
            delivery.transaction, "atomic", side_effect=lambda: nullcontext()
        ), patch.object(delivery.ContentSearchTarget, "objects", targets):
            self.assertEqual(delivery.refresh_content_search_outbox_status(1), ContentSearchStatus.PENDING)
        self.assertEqual(event.last_error_code, "content_search_no_compatible_target")
        self.assertIsNone(event.completed_at)

    def test_scope_request_uses_independent_v2_gate(self):
        pages, jobs = MagicMock(), MagicMock()
        jobs.select_for_update.return_value.filter.return_value.order_by.return_value.first.return_value = None
        with override_settings(CONTENT_SEARCH_PRODUCER_ENABLED=False, CONTENT_SEARCH_V2_PRODUCER_ENABLED=True), patch.object(
            outbox.Page, "objects", pages
        ), patch.object(outbox.ContentSearchScopeJob, "objects", jobs), patch.object(
            outbox.transaction, "atomic", side_effect=lambda: nullcontext()
        ), patch.object(outbox.transaction, "on_commit"):
            outbox.ContentSearchOutboxService.request_scope_recalculation(7)
        jobs.create.assert_called_once_with(root_page_id=7)

    def test_idempotent_reentry_rejects_damaged_current_event(self):
        from search.services.published_snapshot import PublishedSnapshotV1, PublishedSnapshotError
        event, state = event_state()
        event.snapshot_digest = "0" * 64
        states, events = MagicMock(), MagicMock()
        states.select_for_update.return_value.filter.return_value.first.return_value = state
        events.select_for_update.return_value.filter.return_value.first.return_value = event
        with patch.object(outbox.transaction, "atomic", side_effect=lambda: nullcontext()), patch.object(
            outbox.ContentSearchState, "objects", states
        ), patch.object(outbox.ContentSearchOutbox, "objects", events):
            with self.assertRaises(PublishedSnapshotError):
                outbox.ContentSearchOutboxService._record_change(
                    page_id=7, operation="upsert", searchable=True,
                    mongo_content_id="legacy-id", body_version_id="body-v7",
                    publication_generation=3, content_hash="b" * 64,
                    published_snapshot=PublishedSnapshotV1.from_dict(snapshot_data()),
                    page_already_locked=True,
                )
        events.create.assert_not_called()


class AdditionalSafetyTests(unittest.TestCase):
    def test_v2_source_rejects_body_on_tombstone_and_invalid_scope_before_client(self):
        event, _ = event_state()
        tombstone = delivery._tombstone_document(event, target(2))
        bad_tombstone = {**tombstone, "body_text": "不得留存的旧正文"}
        live = {
            **tombstone, "searchable": True, "is_deleted": False,
            "title": "正式", "summary": "", "body_text": "正文",
            "first_published_at": "2026-10-08T01:00:00+00:00",
            "sort_date": "2026-10-08T01:00:00+00:00",
            "locale_id": 1, "site_ids": [1], "ancestor_ids": [1],
            "content_hash": "b" * 64,
        }
        elasticsearch.validate_content_search_source(target(2), live)
        for bad in (
            bad_tombstone, {**live, "site_ids": [True]},
            {**live, "body_text": {"payload": "原始内容"}},
            {**live, "content_hash": "invalid"},
        ):
            with self.subTest(bad=bad), patch.object(elasticsearch, "get_search_backend") as client:
                with self.assertRaises(elasticsearch.ContentSearchElasticsearchError):
                    elasticsearch.write_content_search_documents(target(2), [bad])
                client.assert_not_called()

    def test_v2_template_bad_mapping_version_fails_before_client(self):
        definition = content_index.build_content_index_template(
            "test-content-v006", analyzer_profile="legacy_standard",
            version="v006", projection_schema_version=2,
        )
        for metadata in ({"mapping_version": "content-v005-legacy_standard"}, {"analyzer_profile": "ik_smart"}):
            damaged = deepcopy(definition)
            damaged["_meta"].update(metadata)
            damaged["template"]["mappings"]["_meta"].update(metadata)
            with self.subTest(metadata=metadata), patch.object(
                elasticsearch, "get_content_search_client_for_connection"
            ) as client:
                with self.assertRaises(elasticsearch.ContentSearchElasticsearchError):
                    elasticsearch.create_content_search_index(
                        "content", "test-content-v006", "test-content-v006-template", damaged,
                    )
                client.assert_not_called()

    def test_empty_trailing_rebuild_batch_advances_to_fixed_scan_upper_bound(self):
        build = SimpleNamespace(
            pk=2, status="backfilling", checkpoint_page_id=7,
            scan_upper_bound_page_id=12, save=MagicMock(),
        )
        blogs, builds = MagicMock(), MagicMock()
        blogs.live.return_value.public.return_value.filter.return_value.prefetch_related.return_value.order_by.return_value.__getitem__.return_value = []
        builds.select_for_update.return_value.get.return_value = build
        with patch.object(rebuild, "_build_for_target", return_value=(target(1), build)), patch.object(
            rebuild.BlogPage, "objects", blogs
        ), patch.object(rebuild.SearchIndexBuild, "objects", builds), patch.object(
            rebuild.transaction, "atomic", side_effect=lambda: nullcontext()
        ), patch.object(rebuild, "materialize_content_search_deliveries"):
            result = rebuild.rebuild_content_search_batch("target", 10, 4096)
        self.assertTrue(result.done)
        self.assertEqual(result.checkpoint_page_id, 12)
        self.assertEqual(build.checkpoint_page_id, 12)

    def test_title_creation_checks_exact_mapping_not_subset(self):
        definition = title_index.build_title_suggestion_index_template("test-content-title-v001")
        mapping = deepcopy(definition["template"]["mappings"])
        mapping["properties"]["unexpected"] = {"type": "text"}
        client = MagicMock()
        client.indices.exists.return_value = False
        client.indices.get_mapping.return_value = {"test-content-title-v001": {"mappings": mapping}}
        with patch.object(title_index, "get_content_search_client_for_connection", return_value=client):
            with self.assertRaises(elasticsearch.ContentSearchElasticsearchError):
                title_index.create_title_suggestion_index("content", "test-content-title-v001")


class FullPathSafetyTests(unittest.TestCase):
    def test_event_encoding_uses_fixed_snapshot_and_verifies_hash(self):
        """错误地用当前草稿或接受不同 hash 都必须导致本测试失败。"""
        self.assertTrue(callable(getattr(outbox, "build_event_projection", None)))
        event, _ = event_state()
        seen = []
        def encode(snapshot, version, **kwargs):
            seen.append((snapshot.to_dict()["published_metadata"]["title"], version, kwargs["target_schema_version"]))
            return document.FormalContentDocument(None, "b" * 64, {"title": "正式标题"})
        with patch.object(document, "build_published_content_document", side_effect=encode, create=True):
            for schema in (1, 2):
                result = outbox.build_event_projection(
                    event, target(schema), scope={"locale_id": 1, "site_ids": [1], "ancestor_ids": [1]},
                    adapter=SimpleNamespace(),
                )
                self.assertEqual(result.content_hash, "b" * 64)
        self.assertEqual(seen, [("正式标题", 4, 1), ("正式标题", 4, 2)])
        with patch.object(document, "build_published_content_document", return_value=document.FormalContentDocument(None, "c" * 64, {}), create=True):
            with self.assertRaisesRegex(outbox.PublishedSnapshotError, "projection_hash_mismatch"):
                outbox.build_event_projection(event, target(2), scope={}, adapter=SimpleNamespace())

    def test_damaged_snapshot_is_rejected_before_body_access(self):
        self.assertTrue(callable(getattr(outbox, "build_event_projection", None)))
        event, _ = event_state()
        event.published_snapshot["published_metadata"]["title"] = "篡改"
        with patch.object(document, "build_published_content_document", create=True) as encoder:
            with self.assertRaises(outbox.PublishedSnapshotError):
                outbox.build_event_projection(event, target(2), scope={}, adapter=SimpleNamespace())
            encoder.assert_not_called()

    def test_optional_only_targets_require_delivery_coverage_not_partial_success(self):
        event, state = event_state()
        event.status = "succeeded"
        event.save = MagicMock()
        first, second = target(1, required=False), target(2, required=False)
        event.deliveries = MagicMock()
        event.deliveries.select_related.return_value.all.return_value = [
            SimpleNamespace(target=first, target_id=first.pk, status="succeeded", attempts=1)
        ]
        targets = MagicMock()
        targets.filter.return_value.filter.return_value = [first, second]
        with patch.object(delivery, "_locked_event_and_state", return_value=(event, state)), patch.object(
            delivery.transaction, "atomic", side_effect=lambda: nullcontext()
        ), patch.object(delivery.ContentSearchTarget, "objects", targets):
            self.assertEqual(delivery.refresh_content_search_outbox_status(1), "pending")

    def test_tombstone_rechecks_current_state_before_es_write(self):
        event, state = event_state()
        event.operation = state.desired_operation = "tombstone"
        event.searchable = state.searchable = False
        lease = delivery.DeliveryLease(1, 2, 3, "token")
        targets = MagicMock()
        targets.get.return_value = target(2)
        with override_settings(CONTENT_SEARCH_CONSUMER_ENABLED=True), patch.object(
            delivery, "_claim_content_search_delivery", return_value=(lease, "processing")
        ), patch.object(delivery, "_load_current_event_and_state", return_value=(event, state, "ready")), patch.object(
            delivery, "_locked_event_and_state", return_value=(event, SimpleNamespace(**{**vars(state), "content_version": 5}))
        ), patch.object(delivery.transaction, "atomic", side_effect=lambda: nullcontext()), patch.object(
            delivery.ContentSearchTarget, "objects", targets
        ), patch.object(delivery, "_complete_delivery", side_effect=lambda lease, status, **kw: status), patch.object(
            delivery, "write_content_search_document"
        ) as writer:
            self.assertEqual(delivery.process_content_search_delivery(1), "superseded")
            writer.assert_not_called()

    def test_v2_hash_backfill_never_repairs_missing_fixed_hash(self):
        _, state = event_state()
        state.content_hash = None
        with self.assertRaisesRegex(rebuild.ContentSearchRebuildError, "snapshot_hash_missing"):
            rebuild._backfill_missing_content_hashes(
                {7: state}, [{"page_id": 7, "content_hash": "b" * 64, "body_version_id": "body-v7"}],
            )

    def test_title_uses_snapshot_not_later_draft_and_preserves_version(self):
        self.assertTrue(callable(getattr(title_index, "build_published_title_document", None)))
        _, state = event_state()
        result = title_index.build_published_title_document(state, locale_id=2)
        self.assertEqual(result, {
            "page_id": 7, "title": "正式标题", "locale_id": 2,
            "searchable": True, "popularity": 0,
        })
        state.published_snapshot["published_metadata"]["title"] = "篡改草稿"
        with self.assertRaises(outbox.PublishedSnapshotError):
            title_index.build_published_title_document(state, locale_id=2)

    def test_schema_two_command_dry_run_selects_v006_without_external_io(self):
        from io import StringIO
        import json
        from django.core.management import call_command
        result = StringIO()
        with patch.object(elasticsearch, "get_content_search_client_for_connection") as client:
            call_command("search_create_content_index", "--target", "p04-test", "--projection-schema-version", "2", stdout=result)
            plan = json.loads(result.getvalue())
        self.assertEqual(plan["projection_schema_version"], 2)
        self.assertTrue(plan["index_name"].endswith("-v006"))
        self.assertTrue(plan["dry_run"])
        client.assert_not_called()


class FormalProducerPathTests(unittest.TestCase):
    """真实 P03 capture/全文编码；只替换 SQL、broker、ES 的 I/O 边界。"""

    def fixture(self, external=False):
        from search.tests.test_external_projection import BlogPublishedAdapterTests, PublishedAdapterTests
        fixture = (PublishedAdapterTests if external else BlogPublishedAdapterTests)()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_formal_capture_rejects_draft_and_binds_final_state(self):
        self.assertTrue(callable(getattr(outbox, "capture_final_publication", None)))
        f = self.fixture()
        from datetime import datetime, timezone
        f.page.live = True
        f.page.live_revision_id = 19
        f.page.first_published_at = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)
        entry = SimpleNamespace(model_class=f.model, model_label="blog.blogpage",
                                enabled=True, snapshot_kind="blog_v1", search_type="blog")
        state = SimpleNamespace(publication_generation=3, published_body_version_id="body-v7",
                                published_body_sha256=f.body_hash, published_body_schema_version=1)
        with patch("search.content_registry.get_content_registry", return_value=SimpleNamespace(for_model=lambda model: entry)):
            snapshot = outbox.capture_final_publication(
                f.page, publication_state=state, revision=f.revision, adapter=f.adapter(),
            )
            self.assertEqual(snapshot.to_dict()["published_metadata"]["title"], "正式标题")
            f.page.live_revision_id = 20
            with self.assertRaisesRegex(outbox.PublishedSnapshotError, "formal_revision_mismatch"):
                outbox.capture_final_publication(
                    f.page, publication_state=state, revision=f.revision, adapter=f.adapter(),
                )

    def test_real_blog_snapshot_encodes_both_targets_with_same_hash_and_no_draft(self):
        f = self.fixture()
        adapter = f.adapter()
        snapshot = f.capture(adapter)
        topology = {"locale_id": 1, "site_ids": [1], "ancestor_ids": [1, 7]}
        encoded = document.build_published_content_document(snapshot, 4, scope=topology, adapter=adapter)
        event, _ = event_state()
        event.published_snapshot, event.snapshot_digest = snapshot.to_dict(), snapshot.digest
        event.content_hash = encoded.content_hash
        v1 = outbox.build_event_projection(event, target(1), scope=topology, adapter=adapter)
        v2 = outbox.build_event_projection(event, target(2), scope=topology, adapter=adapter)
        self.assertEqual(v1.content_hash, v2.content_hash)
        self.assertEqual(v1.document["title"], "正式标题")
        self.assertEqual(v1.document["tag_ids"], [2, 9])
        self.assertIn("中文尾词", v2.document["body_text"])
        self.assertNotIn("后来草稿", str(v2.document))
        elasticsearch.validate_content_search_source(target(2), v2.document)

    def test_real_external_projection_missing_fixed_body_never_falls_back(self):
        f = self.fixture(external=True)
        adapter, topology = f.adapter(), {"locale_id": 1, "site_ids": [1], "ancestor_ids": [1, 7]}
        snapshot = f.capture(adapter)
        encoded = document.build_published_content_document(snapshot, 4, scope=topology, adapter=adapter)
        event = SimpleNamespace(**snapshot.to_dict(), published_snapshot=snapshot.to_dict(),
                                snapshot_digest=snapshot.digest, content_version=4,
                                content_hash=encoded.content_hash, operation="upsert", searchable=True)
        projected = outbox.build_event_projection(event, target(2), scope=topology, adapter=adapter)
        self.assertIn("尾部独有中文", projected.document["body_text"])
        self.assertIn("附录中文 needle-end", projected.document["body_text"])
        elasticsearch.validate_content_search_source(target(2), projected.document)
        f.docs.pop("appendix")
        with self.assertRaises(outbox.PublishedSnapshotError):
            outbox.build_event_projection(event, target(2), scope=topology, adapter=adapter)

    def test_tombstone_reuses_snapshot_without_body_read_and_advances_generation(self):
        self.assertTrue(callable(getattr(outbox.ContentSearchOutboxService, "record_final_tombstone", None)))
        event, state = event_state()
        states = MagicMock()
        states.select_for_update.return_value.filter.return_value.first.return_value = state
        with override_settings(CONTENT_SEARCH_V2_PRODUCER_ENABLED=True), patch.object(
            outbox.transaction, "get_connection", return_value=SimpleNamespace(in_atomic_block=True)
        ), patch.object(outbox.ContentSearchState, "objects", states), patch.object(
            outbox.ContentSearchOutboxService, "_record_change", side_effect=lambda **kw: kw
        ), patch.object(document, "build_published_content_document") as body:
            result = outbox.ContentSearchOutboxService.record_final_tombstone(
                SimpleNamespace(pk=7), publication_generation=4,
            )
            self.assertEqual(result["operation"], "tombstone")
            self.assertEqual(result["publication_generation"], 4)
            self.assertEqual(result["published_snapshot"].to_dict()["published_metadata"]["title"], "正式标题")
            self.assertIsNone(result["content_hash"])
            body.assert_not_called()

    def test_new_formal_producer_requires_outer_transaction_not_late_autocommit(self):
        self.assertTrue(callable(getattr(outbox.ContentSearchOutboxService, "record_final_publication", None)))
        with override_settings(CONTENT_SEARCH_V2_PRODUCER_ENABLED=True):
            with self.assertRaisesRegex(outbox.PublishedSnapshotError, "publication_transaction_required"):
                outbox.ContentSearchOutboxService.record_final_publication(SimpleNamespace(pk=7))


class RegistryScopeAndHookTests(unittest.TestCase):
    def test_registry_enumeration_includes_enabled_external_and_v1_blog_only(self):
        self.assertTrue(callable(getattr(bootstrap, "registered_pages_queryset", None)))
        entries = [
            SimpleNamespace(enabled=True, model_label="blog.blogpage", snapshot_kind="blog_v1"),
            SimpleNamespace(enabled=True, model_label="guides.guidepage", snapshot_kind="external_v1"),
            SimpleNamespace(enabled=False, model_label="sample.disabledpage", snapshot_kind="external_v1"),
        ]
        with patch("search.content_registry.get_content_registry", return_value=SimpleNamespace(entries=entries)):
            v2 = str(bootstrap.registered_pages_queryset(target=target(2)).query)
            v1 = str(bootstrap.registered_pages_queryset(target=target(1)).query)
        self.assertIn("guidepage", v2)
        self.assertNotIn("disabledpage", v2)
        self.assertIn("blogpage", v1)
        self.assertNotIn("guidepage", v1)

    def test_scope_hash_changes_for_move_even_when_public_bit_unchanged(self):
        self.assertTrue(callable(getattr(scope, "reconcile_published_scope", None)))
        from search.tests.test_external_projection import BlogPublishedAdapterTests
        f = BlogPublishedAdapterTests()
        f.setUp()
        adapter, snapshot = f.adapter(), f.capture(f.adapter())
        event, state = event_state()
        state.published_snapshot, state.snapshot_digest = snapshot.to_dict(), snapshot.digest
        old = {"locale_id": 1, "site_ids": [1], "ancestor_ids": [1, 7]}
        state.content_hash = document.build_published_content_document(snapshot, 4, scope=old, adapter=adapter).content_hash
        new = {"locale_id": 1, "site_ids": [2], "ancestor_ids": [1, 9, 7]}
        with override_settings(CONTENT_SEARCH_V2_PRODUCER_ENABLED=True), patch.object(
            outbox.transaction, "get_connection", return_value=SimpleNamespace(in_atomic_block=True)
        ), patch.object(outbox, "published_projection_scope", return_value=new), patch.object(
            outbox.ContentSearchOutboxService, "_record_change", side_effect=lambda **kw: kw
        ):
            result = scope.reconcile_published_scope(SimpleNamespace(pk=7), state, is_public=True, adapter=adapter)
        self.assertNotEqual(result["content_hash"], state.content_hash)
        self.assertEqual(result["publication_generation"], 3)
        self.assertEqual(result["published_snapshot"].digest, state.snapshot_digest)

    def test_external_signal_produces_after_alias_sync_in_original_transaction(self):
        from external_content import signals as external_signals
        order = []
        page = SimpleNamespace(pk=7, live=True)
        with patch.object(external_signals, "ExternalContentPageMixin", SimpleNamespace), patch.object(
            external_signals.ExternalContentPublicationService, "ensure_published_revision",
            side_effect=lambda *a: order.append("ensure")
        ), patch("external_content.copying.sync_external_content_aliases", side_effect=lambda *a, **kw: order.append("aliases")), patch.object(
            outbox.ContentSearchOutboxService, "record_final_publication", side_effect=lambda *a, **kw: order.append("event")
        ):
            external_signals.handle_external_content_page_published(None, page, revision=SimpleNamespace(pk=19))
        self.assertEqual(order, ["ensure", "aliases", "event"])

    def test_blog_v2_signal_replaces_legacy_producer_and_rejects_autocommit(self):
        from search import signals
        with override_settings(CONTENT_SEARCH_V2_PRODUCER_ENABLED=True, CONTENT_SEARCH_PRODUCER_ENABLED=True):
            with self.assertRaisesRegex(outbox.PublishedSnapshotError, "publication_transaction_required"):
                signals.record_blog_page_published(None, SimpleNamespace(pk=7), revision=SimpleNamespace(pk=19))

    def test_consistency_v2_tombstone_is_deleted_and_digest_identity_not_legacy_pointer(self):
        event, state = event_state()
        state.desired_operation, state.searchable = "tombstone", False
        states = MagicMock()
        states.filter.return_value.order_by.return_value.__getitem__.return_value = [state]
        with patch.object(consistency.ContentSearchState, "objects", states), patch.object(
            consistency, "read_content_search_documents", return_value={
                7: {**delivery._tombstone_document(event, target(2))}
            }
        ), patch.object(consistency, "scan_content_search_documents", return_value=[]):
            report = consistency.check_content_search_consistency(target(2))
        self.assertEqual(report["counts"]["wrong_tombstone"], 0)
        self.assertEqual(report["counts"].get("body_version_mismatch", 0), 0)

    def test_consistency_skips_incompatible_but_advances_input_cursor(self):
        _, state = event_state()
        state.snapshot_kind, state.model_label, state.search_type = "external_v1", "guides.guidepage", "guides"
        states = MagicMock()
        states.filter.return_value.order_by.return_value.__getitem__.return_value = [state]
        with patch.object(consistency.ContentSearchState, "objects", states), patch.object(
            consistency, "read_content_search_documents", return_value={}
        ), patch.object(consistency, "scan_content_search_documents", return_value=[]):
            report = consistency.check_content_search_consistency(target(1))
        self.assertEqual(report["counts"]["missing"], 0)
        self.assertEqual(report["next_state_after_page_id"], 7)
