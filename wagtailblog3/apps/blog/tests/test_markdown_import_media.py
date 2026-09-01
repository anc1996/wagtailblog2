import uuid
from types import SimpleNamespace
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.storage import storages
from django.test import SimpleTestCase
from wagtail.images import get_image_model
from wagtailmedia.models import get_media_model

from blog.models import (
    MarkdownImportArtifactCleanupStatus,
    MarkdownImportArtifactStatus,
)
from blog.services.markdown_import_media import (
    MediaImportError,
    MediaImportResult,
    MediaProbeResult,
    _same_storage,
    cleanup_artifact_object,
    import_media_artifact,
    import_media_artifacts,
    validate_media_upload,
    probe_media_content,
)


def _mp4_box(box_type, payload):
    raw_type = box_type.encode("ascii")
    return (len(payload) + 8).to_bytes(4, "big") + raw_type + payload


def _minimal_mp4(handler, codec):
    handler_box = _mp4_box("hdlr", b"\0\0\0\0\0\0\0\0" + handler.encode("ascii") + b"\0\0\0\0")
    sample = (16).to_bytes(4, "big") + codec.encode("ascii") + b"\0" * 8
    stsd = _mp4_box("stsd", b"\0\0\0\0" + (1).to_bytes(4, "big") + sample)
    return _mp4_box(
        "ftyp",
        b"isom" + (0).to_bytes(4, "big") + b"isomiso2",
    ) + _mp4_box(
        "moov",
        _mp4_box("trak", _mp4_box("mdia", handler_box + _mp4_box("minf", _mp4_box("stbl", stsd)))),
    )


class FakeStorage:
    def __init__(
        self,
        *,
        renamed_to=None,
        save_error=None,
        exists_error=None,
        delete_error=None,
        sticky=False,
        preexisting=None,
    ):
        self.renamed_to = renamed_to
        self.save_error = save_error
        self.exists_error = exists_error
        self.delete_error = delete_error
        self.sticky = sticky
        self.objects = dict(preexisting or {})
        self.saved_names = []
        self.deleted_names = []

    def save(self, name, content):
        self.saved_names.append(name)
        if self.save_error:
            raise self.save_error
        actual_name = self.renamed_to or name
        content.seek(0)
        self.objects[actual_name] = content.read()
        return actual_name

    def exists(self, name):
        if self.exists_error:
            raise self.exists_error
        return name in self.objects

    def delete(self, name):
        self.deleted_names.append(name)
        if self.delete_error:
            raise self.delete_error
        if not self.sticky:
            self.objects.pop(name, None)


class FakeArtifact:
    def __init__(
        self,
        position=1,
        media_type="image",
        normalized_source="assets/photo.png",
        safe_filename="photo.png",
    ):
        self.artifact_id = uuid.uuid4()
        self.position = position
        self.media_type = media_type
        self.normalized_source = normalized_source
        self.safe_filename = safe_filename
        self.status = MarkdownImportArtifactStatus.PENDING
        self.storage_alias = ""
        self.object_name = ""
        self.sha256 = ""
        self.media_model = ""
        self.media_object_id = None
        self.error_code = ""
        self.cleanup_status = MarkdownImportArtifactCleanupStatus.NONE
        self.cleanup_error_code = ""
        self.updated_at = None
        self.saved_snapshots = []

    def save(self, *, update_fields):
        self.saved_snapshots.append({field: getattr(self, field) for field in update_fields})


class FakeFileField:
    def __init__(self, storage=None):
        self.name = ""
        self._committed = False
        self.storage = storage


class FakeMediaObject:
    class Meta:
        label = "blog.BlogImage"
        label_lower = "blog.blogimage"

    _meta = Meta()

    def __init__(self, *, save_error=None, pk=31, storage=None, save_after_pk=False):
        self.file = FakeFileField(storage)
        self.pk = pk
        self.save_error = save_error
        self.save_after_pk = save_after_pk
        self.saved = False
        self._state = SimpleNamespace(adding=True)

    def save(self):
        self.saved = True
        if self.save_after_pk:
            self.pk = 91
            self._state.adding = False
        if self.save_error:
            raise self.save_error
        self._state.adding = False


class FakeModelRow:
    def __init__(self, *, delete_error=None):
        self.delete_error = delete_error
        self.deleted = False

    def delete(self):
        if self.delete_error:
            raise self.delete_error
        self.deleted = True


class FakeValidForm:
    def __init__(self, instance, *, valid=True):
        self.instance = instance
        self.valid = valid
        self.errors = {"file": ["rejected"]}
        self.save_calls = []

    def is_valid(self):
        return self.valid

    def save(self, commit=True):
        self.save_calls.append(commit)
        if commit:
            self.instance.save()
        return self.instance


class MarkdownImportMediaValidationTests(SimpleTestCase):
    def test_builtin_probe_accepts_mp3_frame_structure(self):
        frame = bytes.fromhex("fffb9064") + b"\0" * 413
        uploaded = SimpleUploadedFile("voice.mp3", frame + frame)

        result = probe_media_content(uploaded)

        self.assertEqual(result, MediaProbeResult(True, "audio/mpeg", "mp3", "mpeg-layer3"))

    def test_builtin_probe_accepts_mp4_track_structure(self):
        uploaded = SimpleUploadedFile("clip.mp4", _minimal_mp4("vide", "avc1"))

        result = probe_media_content(uploaded)

        self.assertEqual(result, MediaProbeResult(True, "video/mp4", "mp4", "h264"))

    def test_builtin_probe_rejects_renamed_bytes(self):
        uploaded = SimpleUploadedFile("clip.mp4", b"not a media container")

        result = probe_media_content(uploaded)

        self.assertFalse(result.valid)

    def test_image_uses_wagtail_form_for_final_validation(self):
        uploaded = SimpleUploadedFile("photo.png", b"real image bytes")
        instance = FakeMediaObject()
        form = FakeValidForm(instance)
        factory = mock.Mock(return_value=form)

        returned = validate_media_upload(
            "image", uploaded, form_factory=factory, model_instance=instance,
            form_data={"title": "photo", "collection": "1"}, user=SimpleNamespace(pk=7),
        )

        self.assertIs(returned, form)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs["files"], {"file": uploaded})

    def test_form_rejection_has_stable_code_without_exposing_errors(self):
        uploaded = SimpleUploadedFile("photo.png", b"bad")
        form = FakeValidForm(FakeMediaObject(), valid=False)

        with self.assertRaisesMessage(MediaImportError, "media_form_invalid") as raised:
            validate_media_upload("image", uploaded, form_factory=mock.Mock(return_value=form), model_instance=form.instance, form_data={})

        self.assertEqual(raised.exception.code, "media_form_invalid")
        self.assertNotIn("rejected", str(raised.exception))

    def test_form_construction_failure_has_stable_code_without_exposing_error(self):
        form_factory = mock.Mock(side_effect=PermissionError("collection denied"))

        with self.assertRaisesMessage(MediaImportError, "media_form_invalid") as raised:
            validate_media_upload(
                "image",
                SimpleUploadedFile("photo.png", b"bad"),
                form_factory=form_factory,
                model_instance=FakeMediaObject(),
                form_data={},
            )

        self.assertEqual(raised.exception.code, "media_form_invalid")
        self.assertNotIn("collection denied", str(raised.exception))

    def test_audio_and_video_require_probed_mime_family(self):
        probes = (
            ("audio", MediaProbeResult(True, "audio/mpeg", "mp3", "mp3")),
            ("video", MediaProbeResult(True, "video/mp4", "mp4", "h264")),
        )
        for media_type, probe_result in probes:
            with self.subTest(media_type=media_type):
                form = FakeValidForm(FakeMediaObject())
                returned = validate_media_upload(
                    media_type, SimpleUploadedFile("asset.bin", b"content"),
                    content_probe=mock.Mock(return_value=probe_result),
                    form_factory=mock.Mock(return_value=form), model_instance=form.instance, form_data={},
                )
                self.assertIs(returned, form)

    def test_spoofed_audio_mime_is_rejected_before_media_form(self):
        form_factory = mock.Mock()

        with self.assertRaisesMessage(MediaImportError, "media_content_type_mismatch"):
            validate_media_upload(
                "audio", SimpleUploadedFile("voice.mp3", b"not audio"),
                content_probe=mock.Mock(
                    return_value=MediaProbeResult(
                        False, "application/octet-stream", "", ""
                    )
                ),
                form_factory=form_factory, model_instance=FakeMediaObject(), form_data={},
            )

        form_factory.assert_not_called()

    def test_damaged_container_and_codec_mismatch_are_rejected(self):
        cases = (
            ("audio", MediaProbeResult(False, "audio/mpeg", "mp3", "")),
            ("video", MediaProbeResult(False, "video/mp4", "mp4", "aac")),
        )
        for media_type, probe_result in cases:
            with self.subTest(media_type=media_type):
                with self.assertRaisesMessage(
                    MediaImportError, "media_deep_probe_invalid"
                ):
                    validate_media_upload(
                        media_type,
                        SimpleUploadedFile("asset.bin", b"damaged"),
                        content_probe=mock.Mock(return_value=probe_result),
                        form_factory=mock.Mock(),
                        model_instance=FakeMediaObject(),
                        form_data={},
                    )

    def test_default_probe_fails_closed_without_codec_parser(self):
        with self.assertRaisesMessage(
            MediaImportError, "media_deep_probe_unavailable"
        ):
            validate_media_upload(
                "audio",
                SimpleUploadedFile("voice.mp3", b"ID3" + b"0" * 32),
                form_factory=mock.Mock(),
                model_instance=FakeMediaObject(),
                form_data={},
            )

    def test_probe_failure_has_stable_code_without_exposing_exception(self):
        form_factory = mock.Mock()

        with self.assertRaisesMessage(MediaImportError, "media_probe_failed") as raised:
            validate_media_upload(
                "video",
                SimpleUploadedFile("clip.mp4", b"content"),
                content_probe=mock.Mock(side_effect=OSError("secret path")),
                form_factory=form_factory,
                model_instance=FakeMediaObject(),
                form_data={},
            )

        self.assertEqual(raised.exception.code, "media_probe_failed")
        self.assertNotIn("secret path", str(raised.exception))
        form_factory.assert_not_called()


class MarkdownImportMediaPersistenceTests(SimpleTestCase):
    def test_real_wagtail_field_storages_match_default_registry_backend(self):
        registry_storage = storages["default"]
        field_storages = (
            get_image_model()._meta.get_field("file").storage,
            get_media_model()._meta.get_field("file").storage,
        )

        for field_storage in field_storages:
            with self.subTest(model_storage=field_storage):
                self.assertTrue(_same_storage(field_storage, registry_storage))
        self.assertFalse(_same_storage(field_storages[0], FakeStorage()))

    def _import(self, artifact, storage, *, model=None):
        form = FakeValidForm(model or FakeMediaObject(storage=storage))
        form.instance.file.storage = storage
        return import_media_artifact(
            artifact, SimpleUploadedFile("photo.png", b"payload"),
            validated_form=form, storage=storage, storage_alias="default",
            storage_registry={"default": storage},
        )

    def test_import_accepts_django_storage_handler_registry(self):
        artifact = FakeArtifact()
        storage = FakeStorage()

        class HandlerLike:
            def __getitem__(self, alias):
                if alias != "default":
                    raise KeyError(alias)
                return storage

        result = import_media_artifact(
            artifact,
            SimpleUploadedFile("photo.png", b"payload"),
            validated_form=FakeValidForm(FakeMediaObject(storage=storage)),
            storage=storage,
            storage_alias="default",
            storage_registry=HandlerLike(),
        )

        self.assertEqual(result.block_type, "image_block")

    def test_plan_is_persisted_before_object_write(self):
        artifact = FakeArtifact()
        storage = FakeStorage()
        original_save = storage.save

        def assert_plan_then_save(name, content):
            self.assertEqual(artifact.storage_alias, "default")
            self.assertEqual(artifact.object_name, name)
            self.assertEqual(len(artifact.sha256), 64)
            self.assertTrue(artifact.saved_snapshots)
            return original_save(name, content)

        storage.save = assert_plan_then_save
        result = self._import(artifact, storage)

        self.assertEqual(result.block_type, "image_block")
        self.assertIn(artifact.artifact_id.hex, artifact.object_name)
        self.assertEqual(artifact.status, MarkdownImportArtifactStatus.SUCCEEDED)
        self.assertEqual(artifact.media_model, "blog.blogimage")
        self.assertEqual(artifact.media_object_id, 31)

    def test_long_filename_is_truncated_to_the_media_field_limit(self):
        artifact = FakeArtifact()
        storage = FakeStorage()
        form = FakeValidForm(FakeMediaObject(storage=storage))
        upload = SimpleUploadedFile(f"{'a' * 90}.jpeg", b"payload")

        result = import_media_artifact(
            artifact,
            upload,
            validated_form=form,
            storage=storage,
            storage_alias="default",
            storage_registry={"default": storage},
        )

        self.assertEqual(result.block_type, "image_block")
        self.assertLessEqual(len(artifact.object_name), 100)
        self.assertTrue(artifact.object_name.endswith(".jpeg"))

    def test_import_rejects_unvalidated_storage_alias_before_writing(self):
        artifact = FakeArtifact()
        storage = FakeStorage()

        with self.assertRaisesMessage(MediaImportError, "storage_alias_invalid"):
            import_media_artifact(
                artifact,
                SimpleUploadedFile("photo.png", b"payload"),
                validated_form=FakeValidForm(FakeMediaObject()),
                storage=storage,
                storage_alias="other",
                storage_registry={"default": storage},
            )

        self.assertEqual(storage.saved_names, [])
        self.assertEqual(artifact.saved_snapshots, [])

    def test_success_uses_form_save_for_final_wagtail_processing(self):
        artifact = FakeArtifact()
        storage = FakeStorage()
        form = FakeValidForm(FakeMediaObject(storage=storage))

        import_media_artifact(
            artifact,
            SimpleUploadedFile("photo.png", b"payload"),
            validated_form=form,
            storage=storage,
            storage_alias="default",
            storage_registry={"default": storage},
        )

        self.assertEqual(form.save_calls, [False, True])
        self.assertEqual(form.instance.file.name, artifact.object_name)
        self.assertTrue(form.instance.file._committed)

    def test_storage_save_failure_keeps_planned_name_for_safe_retry(self):
        artifact = FakeArtifact()
        storage = FakeStorage(save_error=OSError("write failed"))

        result = self._import(artifact, storage)

        self.assertEqual(result.block_type, "markdown_block")
        self.assertEqual(artifact.error_code, "storage_save_failed")
        self.assertEqual(storage.deleted_names, [])
        self.assertEqual(
            artifact.cleanup_status,
            MarkdownImportArtifactCleanupStatus.RETRY,
        )

    def test_storage_rename_is_rejected_and_both_exact_names_are_cleaned(self):
        artifact = FakeArtifact()
        storage = FakeStorage(renamed_to="renamed/object.png")

        result = self._import(artifact, storage)

        self.assertEqual(result.block_type, "markdown_block")
        self.assertEqual(artifact.status, MarkdownImportArtifactStatus.FAILED_MISSING)
        self.assertEqual(artifact.error_code, "storage_name_mismatch")
        self.assertEqual(storage.deleted_names, ["renamed/object.png"])
        self.assertEqual(artifact.cleanup_status, MarkdownImportArtifactCleanupStatus.CLEANED)

    def test_preexisting_planned_name_is_never_written_or_deleted(self):
        artifact = FakeArtifact()
        planned = f"markdown-import/{artifact.artifact_id.hex}/photo.png"
        storage = FakeStorage(preexisting={planned: b"existing"})

        result = self._import(artifact, storage)

        self.assertEqual(result.block_type, "markdown_block")
        self.assertEqual(artifact.error_code, "storage_name_collision")
        self.assertEqual(storage.saved_names, [])
        self.assertEqual(storage.deleted_names, [])
        self.assertEqual(storage.objects[planned], b"existing")

    def test_exists_failure_before_write_is_stable_and_does_not_write(self):
        artifact = FakeArtifact()
        storage = FakeStorage(exists_error=OSError("lookup unavailable"))

        result = self._import(artifact, storage)

        self.assertEqual(result.block_type, "markdown_block")
        self.assertEqual(artifact.error_code, "storage_exists_failed")
        self.assertEqual(storage.saved_names, [])
        self.assertEqual(storage.deleted_names, [])

    def test_renamed_save_does_not_delete_preexisting_planned_object(self):
        artifact = FakeArtifact()
        planned = f"markdown-import/{artifact.artifact_id.hex}/photo.png"

        class CollisionStorage(FakeStorage):
            def save(self, name, content):
                self.saved_names.append(name)
                self.objects[name] = b"existing"
                actual = "renamed/new-object.png"
                self.objects[actual] = b"new"
                return actual

            def exists(self, name):
                # 调用前不存在，save 内模拟并发碰撞后 planned 出现。
                if not self.saved_names and name == planned:
                    return False
                return super().exists(name)

        storage = CollisionStorage()
        result = self._import(artifact, storage)

        self.assertEqual(result.block_type, "markdown_block")
        self.assertEqual(storage.deleted_names, ["renamed/new-object.png"])
        self.assertEqual(storage.objects[planned], b"existing")

    def test_model_save_failure_deletes_only_the_planned_object(self):
        artifact = FakeArtifact()
        storage = FakeStorage()

        result = self._import(artifact, storage, model=FakeMediaObject(save_error=RuntimeError("db")))

        self.assertEqual(result.block_type, "markdown_block")
        self.assertEqual(storage.deleted_names, [artifact.object_name])
        self.assertEqual(artifact.status, MarkdownImportArtifactStatus.FAILED_MISSING)
        self.assertEqual(artifact.error_code, "media_model_save_failed")
        self.assertEqual(artifact.media_model, "")
        self.assertIsNone(artifact.media_object_id)

    def test_form_save_failure_keeps_model_evidence_for_exact_later_cleanup(self):
        artifact = FakeArtifact()
        storage = FakeStorage()
        model = FakeMediaObject(
            storage=storage,
            save_error=RuntimeError("index failed"),
            save_after_pk=True,
        )

        result = self._import(artifact, storage, model=model)

        self.assertEqual(result.block_type, "markdown_block")
        self.assertEqual(artifact.status, MarkdownImportArtifactStatus.FAILED_MISSING)
        self.assertEqual(artifact.media_model, "blog.blogimage")
        self.assertEqual(artifact.media_object_id, 91)
        row = FakeModelRow()
        self.assertTrue(
            cleanup_artifact_object(
                artifact,
                storages={"default": storage},
                reference_guard=lambda _artifact: False,
                model_resolver=lambda label, pk: row,
            )
        )
        self.assertTrue(row.deleted)
        self.assertEqual(artifact.cleanup_status, MarkdownImportArtifactCleanupStatus.CLEANED)

    def test_storage_instance_mismatch_is_rejected_before_plan_or_write(self):
        artifact = FakeArtifact()
        storage = FakeStorage()
        form = FakeValidForm(FakeMediaObject(storage=FakeStorage()))

        with self.assertRaisesMessage(MediaImportError, "storage_alias_mismatch"):
            import_media_artifact(
                artifact,
                SimpleUploadedFile("photo.png", b"payload"),
                validated_form=form,
                storage=storage,
                storage_alias="default",
                storage_registry={"default": storage},
            )

        self.assertEqual(storage.saved_names, [])
        self.assertEqual(artifact.saved_snapshots, [])

    def test_storage_alias_requires_explicit_registry(self):
        artifact = FakeArtifact()
        storage = FakeStorage()
        form = FakeValidForm(FakeMediaObject(storage=storage))

        with self.assertRaisesMessage(MediaImportError, "storage_registry_required"):
            import_media_artifact(
                artifact,
                SimpleUploadedFile("photo.png", b"payload"),
                validated_form=form,
                storage=storage,
                storage_alias="default",
            )

        self.assertEqual(storage.saved_names, [])
        self.assertEqual(artifact.saved_snapshots, [])

    def test_seventh_failure_keeps_all_other_successful_objects(self):
        artifacts = [FakeArtifact(position=index) for index in range(1, 11)]
        storages = [FakeStorage() for _item in artifacts]

        def importer(artifact):
            model = FakeMediaObject(save_error=RuntimeError("db"), pk=artifact.position) if artifact.position == 7 else FakeMediaObject(pk=artifact.position)
            return self._import(artifact, storages[artifact.position - 1], model=model)

        results = import_media_artifacts(artifacts, importer=importer)

        self.assertEqual(results[6].block_type, "markdown_block")
        self.assertTrue(all(result.block_type == "image_block" for result in results[:6]))
        self.assertTrue(all(result.block_type == "image_block" for result in results[7:]))
        self.assertEqual(storages[6].deleted_names, [artifacts[6].object_name])
        for index, storage in enumerate(storages):
            if index != 6:
                self.assertEqual(storage.deleted_names, [])
                self.assertTrue(storage.exists(artifacts[index].object_name))


class MarkdownImportMediaCleanupTests(SimpleTestCase):
    def test_reference_guard_failure_enters_retry_without_deletion(self):
        artifact = FakeArtifact()
        artifact.storage_alias = "default"
        artifact.object_name = "imports/exact/file.png"
        storage = FakeStorage(preexisting={artifact.object_name: b"data"})

        cleaned = cleanup_artifact_object(
            artifact,
            storages={"default": storage},
            reference_guard=mock.Mock(side_effect=RuntimeError("db unavailable")),
        )

        self.assertFalse(cleaned)
        self.assertEqual(artifact.cleanup_status, MarkdownImportArtifactCleanupStatus.RETRY)
        self.assertEqual(
            artifact.cleanup_error_code,
            "cleanup_reference_check_failed",
        )
        self.assertEqual(storage.deleted_names, [])

    def test_missing_marker_prefers_normalized_source_without_absolute_path(self):
        artifact = FakeArtifact(
            normalized_source="images/safe-name.png",
            safe_filename=r"C:\secret\unsafe.png",
        )

        results = import_media_artifacts(
            [artifact],
            importer=mock.Mock(side_effect=MediaImportError("media_form_invalid")),
        )

        self.assertIn("images/safe-name.png", results[0].value)
        self.assertNotIn(r"C:\secret", results[0].value)

    def test_batch_converts_validation_failures_and_continues_in_order(self):
        artifacts = [FakeArtifact(position=index) for index in range(1, 11)]
        errors = {
            7: "media_form_invalid",
            8: "media_probe_failed",
            9: "media_deep_probe_invalid",
        }

        def importer(artifact):
            if artifact.position in errors:
                raise MediaImportError(errors[artifact.position])
            return MediaImportResult("image_block", artifact.position)

        results = import_media_artifacts(artifacts, importer=importer)

        self.assertEqual([result.value for result in results[:6]], list(range(1, 7)))
        self.assertEqual(
            [result.block_type for result in results[6:9]],
            ["markdown_block", "markdown_block", "markdown_block"],
        )
        self.assertEqual(results[9].value, 10)
        self.assertEqual(
            [artifact.status for artifact in artifacts[6:9]],
            [MarkdownImportArtifactStatus.FAILED_MISSING] * 3,
        )
    def test_delete_failure_enters_retry_and_keeps_exact_evidence(self):
        artifact = FakeArtifact()
        artifact.storage_alias = "default"
        artifact.object_name = "imports/exact/file.png"
        artifact.media_model = "blog.BlogImage"
        artifact.media_object_id = 44
        storage = FakeStorage(delete_error=OSError("unavailable"))
        storage.objects[artifact.object_name] = b"data"

        cleaned = cleanup_artifact_object(
            artifact,
            storages={"default": storage},
            reference_guard=lambda _artifact: False,
            model_resolver=lambda label, pk: None,
        )

        self.assertFalse(cleaned)
        self.assertEqual(artifact.cleanup_status, MarkdownImportArtifactCleanupStatus.RETRY)
        self.assertEqual(artifact.cleanup_error_code, "storage_delete_failed")
        self.assertEqual(artifact.object_name, "imports/exact/file.png")
        self.assertEqual(artifact.media_model, "blog.BlogImage")
        self.assertEqual(artifact.media_object_id, 44)
        self.assertEqual(storage.deleted_names, [artifact.object_name])

    def test_missing_object_and_repeated_cleanup_converge(self):
        artifact = FakeArtifact()
        artifact.storage_alias = "default"
        artifact.object_name = "imports/already-gone.png"
        storage = FakeStorage()

        first = cleanup_artifact_object(artifact, storages={"default": storage}, reference_guard=lambda _artifact: False)
        second = cleanup_artifact_object(artifact, storages={"default": storage}, reference_guard=lambda _artifact: False)

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(artifact.cleanup_status, MarkdownImportArtifactCleanupStatus.CLEANED)
        self.assertEqual(storage.deleted_names, [])

    def test_exists_error_enters_retry_without_attempting_delete(self):
        artifact = FakeArtifact()
        artifact.storage_alias = "default"
        artifact.object_name = "imports/exact/file.png"
        storage = FakeStorage(exists_error=OSError("lookup failed"))

        cleaned = cleanup_artifact_object(
            artifact,
            storages={"default": storage},
            reference_guard=lambda _artifact: False,
            model_resolver=lambda label, pk: None,
        )

        self.assertFalse(cleaned)
        self.assertEqual(
            artifact.cleanup_status,
            MarkdownImportArtifactCleanupStatus.RETRY,
        )
        self.assertEqual(artifact.cleanup_error_code, "storage_exists_failed")
        self.assertEqual(storage.deleted_names, [])

    def test_delete_that_leaves_object_enters_retry(self):
        artifact = FakeArtifact()
        artifact.storage_alias = "default"
        artifact.object_name = "imports/exact/file.png"
        storage = FakeStorage(sticky=True)
        storage.objects[artifact.object_name] = b"data"

        cleaned = cleanup_artifact_object(
            artifact,
            storages={"default": storage},
            reference_guard=lambda _artifact: False,
        )

        self.assertFalse(cleaned)
        self.assertEqual(
            artifact.cleanup_error_code,
            "storage_delete_incomplete",
        )
        self.assertEqual(storage.deleted_names, [artifact.object_name])

    def test_model_delete_failure_enters_retry_and_preserves_all_evidence(self):
        artifact = FakeArtifact()
        artifact.storage_alias = "default"
        artifact.object_name = "imports/exact/file.png"
        artifact.media_model = "blog.blogimage"
        artifact.media_object_id = 55
        storage = FakeStorage()
        storage.objects[artifact.object_name] = b"data"
        row = FakeModelRow(delete_error=OSError("db unavailable"))

        cleaned = cleanup_artifact_object(
            artifact,
            storages={"default": storage},
            reference_guard=lambda _artifact: False,
            model_resolver=lambda label, pk: row,
        )

        self.assertFalse(cleaned)
        self.assertEqual(artifact.cleanup_status, MarkdownImportArtifactCleanupStatus.RETRY)
        self.assertEqual(artifact.cleanup_error_code, "media_model_delete_failed")
        self.assertEqual(artifact.object_name, "imports/exact/file.png")
        self.assertEqual(artifact.media_model, "blog.blogimage")
        self.assertEqual(artifact.media_object_id, 55)
        self.assertEqual(storage.deleted_names, [])

    def test_empty_unknown_or_referenced_evidence_refuses_delete(self):
        scenarios = (("", "exact.png", False, "cleanup_invalid_evidence"), ("unknown", "exact.png", False, "cleanup_unknown_storage"), ("default", "exact.png", True, "cleanup_referenced"))
        for alias, name, referenced, expected_code in scenarios:
            with self.subTest(expected_code=expected_code):
                artifact = FakeArtifact()
                artifact.storage_alias = alias
                artifact.object_name = name
                storage = FakeStorage()
                storage.objects[name] = b"data"

                cleaned = cleanup_artifact_object(artifact, storages={"default": storage}, reference_guard=lambda _artifact: referenced)

                self.assertFalse(cleaned)
                self.assertEqual(artifact.cleanup_status, MarkdownImportArtifactCleanupStatus.RETRY)
                self.assertEqual(artifact.cleanup_error_code, expected_code)
                self.assertEqual(storage.deleted_names, [])
