import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from django.db import IntegrityError
from django.db.models import UniqueConstraint
from django.test import SimpleTestCase

from blog.models import (
    MarkdownImportArtifact,
    MarkdownImportArtifactCleanupStatus,
    MarkdownImportArtifactStatus,
    MarkdownImportBatch,
    MarkdownImportBatchStatus,
    MarkdownImportSession,
    MarkdownImportSessionStatus,
)
from blog.services.markdown_import_idempotency import (
    IdempotencyConflictError,
    IdempotencyKeyError,
    build_request_fingerprint,
    claim_import_batch,
    validate_idempotency_key,
)


class MarkdownImportIdempotencyTests(SimpleTestCase):
    def test_request_fingerprint_is_stable_for_equivalent_json(self):
        first = {
            "target_parent_id": 42,
            "options": {"allow_external_images": True, "strict": False},
            "manifest": [{"artifact_id": "a-1", "size": 20}],
        }
        second = {
            "manifest": [{"size": 20, "artifact_id": "a-1"}],
            "options": {"strict": False, "allow_external_images": True},
            "target_parent_id": 42,
        }

        self.assertEqual(
            build_request_fingerprint(first), build_request_fingerprint(second)
        )
        self.assertEqual(len(build_request_fingerprint(first)), 64)

    def test_request_fingerprint_changes_with_import_contract(self):
        base = {"target_parent_id": 42, "manifest": []}

        self.assertNotEqual(
            build_request_fingerprint(base),
            build_request_fingerprint({**base, "target_parent_id": 43}),
        )

    def test_only_uuid4_idempotency_keys_are_accepted(self):
        key = uuid.uuid4()

        self.assertEqual(validate_idempotency_key(str(key)), key)
        for invalid in ("not-a-uuid", str(uuid.uuid1()), uuid.UUID(int=0)):
            with self.subTest(invalid=invalid):
                with self.assertRaises(IdempotencyKeyError):
                    validate_idempotency_key(invalid)

    def test_claim_creates_batch_for_new_user_key(self):
        key = uuid.uuid4()
        batch = SimpleNamespace(request_fingerprint="a" * 64)
        manager = mock.Mock()
        manager.filter.return_value.first.return_value = None
        manager.create.return_value = batch

        with mock.patch(
            "blog.services.markdown_import_idempotency.transaction.atomic",
            return_value=nullcontext(),
        ):
            claim = claim_import_batch(
                user_id=7,
                idempotency_key=key,
                request_fingerprint="a" * 64,
                target_parent_id=42,
                manager=manager,
            )

        self.assertTrue(claim.created)
        self.assertIs(claim.batch, batch)
        manager.create.assert_called_once_with(
            user_id=7,
            idempotency_key=key,
            request_fingerprint="a" * 64,
            target_parent_id=42,
        )

    def test_claim_reuses_same_key_and_fingerprint(self):
        batch = SimpleNamespace(request_fingerprint="b" * 64)
        manager = mock.Mock()
        manager.filter.return_value.first.return_value = batch

        claim = claim_import_batch(
            user_id=7,
            idempotency_key=uuid.uuid4(),
            request_fingerprint="b" * 64,
            target_parent_id=42,
            manager=manager,
        )

        self.assertFalse(claim.created)
        self.assertIs(claim.batch, batch)
        manager.create.assert_not_called()

    def test_claim_rejects_same_key_with_different_fingerprint(self):
        batch = SimpleNamespace(request_fingerprint="c" * 64)
        manager = mock.Mock()
        manager.filter.return_value.first.return_value = batch

        with self.assertRaisesMessage(
            IdempotencyConflictError, "idempotency_conflict"
        ):
            claim_import_batch(
                user_id=7,
                idempotency_key=uuid.uuid4(),
                request_fingerprint="d" * 64,
                target_parent_id=42,
                manager=manager,
            )

    def test_claim_recovers_concurrent_unique_constraint_race(self):
        key = uuid.uuid4()
        batch = SimpleNamespace(request_fingerprint="e" * 64)
        manager = mock.Mock()
        manager.filter.return_value.first.return_value = None
        manager.create.side_effect = IntegrityError("duplicate")
        manager.get.return_value = batch

        with mock.patch(
            "blog.services.markdown_import_idempotency.transaction.atomic",
            return_value=nullcontext(),
        ):
            claim = claim_import_batch(
                user_id=7,
                idempotency_key=key,
                request_fingerprint="e" * 64,
                target_parent_id=42,
                manager=manager,
            )

        self.assertFalse(claim.created)
        self.assertIs(claim.batch, batch)
        manager.get.assert_called_once_with(user_id=7, idempotency_key=key)

    def test_claim_reraises_integrity_error_when_no_competing_batch_exists(self):
        key = uuid.uuid4()
        database_error = IntegrityError("unrelated constraint")
        manager = mock.Mock()
        manager.filter.return_value.first.return_value = None
        manager.create.side_effect = database_error
        manager.get.side_effect = MarkdownImportBatch.DoesNotExist

        with mock.patch(
            "blog.services.markdown_import_idempotency.transaction.atomic",
            return_value=nullcontext(),
        ):
            with self.assertRaises(IntegrityError) as raised:
                claim_import_batch(
                    user_id=7,
                    idempotency_key=key,
                    request_fingerprint="f" * 64,
                    target_parent_id=42,
                    manager=manager,
                )

        self.assertIs(raised.exception, database_error)


class MarkdownImportModelContractTests(SimpleTestCase):
    def test_session_records_resume_and_expiry_contract(self):
        field_names = {field.name for field in MarkdownImportSession._meta.fields}

        self.assertTrue(
            {
                "session_id",
                "batch",
                "manifest",
                "total_artifacts",
                "total_bytes",
                "completed_artifacts",
                "expires_at",
                "assembly_requested_at",
            }.issubset(field_names)
        )
        self.assertEqual(
            MarkdownImportSession._meta.get_field("status").default,
            MarkdownImportSessionStatus.CREATED,
        )
        indexes = {
            index.name: tuple(index.fields)
            for index in MarkdownImportSession._meta.indexes
        }
        self.assertEqual(
            indexes["blog_md_session_stat_exp_idx"],
            ("status", "expires_at"),
        )

    def test_batch_has_user_scoped_idempotency_constraint(self):
        constraints = {
            constraint.name: constraint
            for constraint in MarkdownImportBatch._meta.constraints
            if isinstance(constraint, UniqueConstraint)
        }

        constraint = constraints["blog_md_import_user_key_uq"]
        self.assertEqual(tuple(constraint.fields), ("user", "idempotency_key"))
        self.assertEqual(
            MarkdownImportBatch._meta.get_field("request_fingerprint").max_length,
            64,
        )
        self.assertEqual(
            MarkdownImportBatch._meta.get_field("status").default,
            MarkdownImportBatchStatus.PENDING,
        )

    def test_artifact_records_exact_cleanup_evidence(self):
        field_names = {field.name for field in MarkdownImportArtifact._meta.fields}

        self.assertTrue(
            {
                "artifact_id",
                "source_kind",
                "normalized_source",
				"normalized_source_hash",
                "storage_alias",
                "object_name",
                "media_model",
                "media_object_id",
                "sha256",
                "cleanup_status",
                "cleanup_error_code",
                "error_code",
            }.issubset(field_names)
        )
        self.assertEqual(
            MarkdownImportArtifact._meta.get_field("status").default,
            MarkdownImportArtifactStatus.PENDING,
        )
        self.assertEqual(
            MarkdownImportArtifact._meta.get_field("sha256").max_length,
            64,
        )
        self.assertEqual(
            MarkdownImportArtifact._meta.get_field("cleanup_status").default,
            MarkdownImportArtifactCleanupStatus.NONE,
        )
        self.assertNotIn(
            "cleanup_retry",
            MarkdownImportArtifactStatus.values,
        )
        self.assertNotIn(
            "cleaned",
            MarkdownImportArtifactStatus.values,
        )
        indexes = {
            index.name: tuple(index.fields)
            for index in MarkdownImportArtifact._meta.indexes
        }
        self.assertEqual(
            indexes["blog_md_art_clean_upd_idx"],
            ("cleanup_status", "updated_at"),
        )
        constraint = next(
            item
            for item in MarkdownImportArtifact._meta.constraints
            if item.name == "blog_md_artifact_source_uq"
        )
        self.assertEqual(
            tuple(constraint.fields),
            ("batch", "source_kind", "normalized_source_hash"),
        )
