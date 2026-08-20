import hashlib
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from rest_framework.test import APIRequestFactory, force_authenticate

from django.test import SimpleTestCase

from blog.markdown_import_api import MarkdownImportPreviewView, MarkdownImportDuplicateTitlesView
from blog.markdown_import_api import (
    MarkdownImportMetadataSuggestionView,
    MarkdownImportMetadataTemplatesView,
    MarkdownImportView,
    MarkdownImportSessionCreateView,
    _artifact_manifest,
    _blocks,
    _fingerprint_payload,
)
from content_ai.services.blog_metadata import PromptTemplateError
from blog.services.markdown_import_idempotency import BatchClaim
from blog.services.markdown_import_media import MediaImportResult
from blog.services.markdown_import_service import DraftCompensationResult


class MarkdownImportApiTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(
            is_authenticated=True,
            is_active=True,
            pk=7,
            has_perm=lambda permission: permission == "blog.add_blogpage",
        )
        self.parent = mock.Mock(pk=42)
        self.parent.permissions_for_user.return_value.can_add_subpage.return_value = True

    @mock.patch("blog.markdown_import_api.BlogPage.objects")
    @mock.patch("blog.markdown_import_api.BlogIndexPage.objects")
    def test_duplicate_titles_are_a_warning_and_return_existing_pages(self, index_manager, page_manager):
        self.parent.path = "00010002"
        self.parent.depth = 2
        index_manager.filter.return_value.first.return_value = self.parent
        page_manager.filter.return_value.exclude.return_value.values.return_value.order_by.return_value = [
            {
                "pk": 99,
                "title": "第七章",
                "slug": "-2",
                "live": False,
                "has_unpublished_changes": True,
            }
        ]
        request = self.factory.post(
            "/blog/api/markdown-import/duplicate-titles/",
            {"target_parent_id": 42, "titles": ["第七章"]},
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = MarkdownImportDuplicateTitlesView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["duplicates"][0]["page_id"], 99)
        page_manager.filter.assert_called_once_with(
            path__startswith="00010002",
            title__in={"第七章"},
        )

    @mock.patch("blog.markdown_import_api.list_active_blog_metadata_templates")
    @mock.patch("blog.markdown_import_api.BlogIndexPage.objects")
    def test_ai_templates_only_return_server_enabled_summaries(self, manager, list_templates):
        manager.filter.return_value.first.return_value = self.parent
        list_templates.return_value = [
            SimpleNamespace(as_dict=lambda: {"id": 3, "name": "技术笔记", "description": "技术文章", "version": 2})
        ]
        request = self.factory.get(
            "/blog/api/markdown-import/ai/templates/",
            {"target_parent_id": 42},
        )
        force_authenticate(request, user=self.user)

        response = MarkdownImportMetadataTemplatesView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["templates"][0]["id"], 3)
        self.assertNotIn("intro_prompt", response.data["templates"][0])

    @mock.patch("blog.markdown_import_api.generate_template_metadata")
    @mock.patch("blog.markdown_import_api.BlogIndexPage.objects")
    def test_ai_suggestion_returns_intro_and_tags_without_overwriting_title(self, manager, generate):
        manager.filter.return_value.first.return_value = self.parent
        generate.return_value = SimpleNamespace(
            title="模型标题",
            intro="生成的简介",
            tags=["Django", "Wagtail", "导入"],
        )
        request = self.factory.post(
            "/blog/api/markdown-import/ai/suggest/",
            {
                "target_parent_id": 42,
                "template_id": 3,
                "language": "zh-hans",
                "context": "只包含正文语义的纯文本",
            },
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = MarkdownImportMetadataSuggestionView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["suggestion"]["intro"], "生成的简介")
        self.assertNotIn("title", response.data["suggestion"])
        body = generate.call_args.args[0]
        self.assertEqual(body, [{"type": "markdown_block", "value": "只包含正文语义的纯文本"}])
        self.assertEqual(generate.call_args.kwargs["template_id"], 3)

    @mock.patch("blog.markdown_import_api.generate_template_metadata")
    @mock.patch("blog.markdown_import_api.BlogIndexPage.objects")
    def test_ai_suggestion_rejects_template_disabled_after_client_cache(self, manager, generate):
        manager.filter.return_value.first.return_value = self.parent
        generate.side_effect = PromptTemplateError("模板已停用")
        request = self.factory.post(
            "/blog/api/markdown-import/ai/suggest/",
            {"target_parent_id": 42, "template_id": 3, "context": "正文"},
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = MarkdownImportMetadataSuggestionView.as_view()(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "ai_template_invalid")

    @mock.patch("blog.markdown_import_api.generate_template_metadata")
    @mock.patch("blog.markdown_import_api.BlogIndexPage.objects")
    def test_ai_suggestion_rejects_url_or_local_path_from_modified_client(self, manager, generate):
        manager.filter.return_value.first.return_value = self.parent
        for context in (
            "正文 https://private.example.test/image.png",
            r"正文 C:\\private\\photo.png",
            "正文 /mnt/f/private/photo.png",
        ):
            request = self.factory.post(
                "/blog/api/markdown-import/ai/suggest/",
                {"target_parent_id": 42, "template_id": 3, "context": context},
                format="json",
            )
            force_authenticate(request, user=self.user)

            response = MarkdownImportMetadataSuggestionView.as_view()(request)

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["code"], "ai_context_contains_forbidden_reference")
        generate.assert_not_called()

    @mock.patch("blog.markdown_import_api.BlogIndexPage.objects")
    def test_preview_checks_parent_permission_and_does_not_create_rows(self, manager):
        manager.filter.return_value.first.return_value = self.parent
        request = self.factory.post(
            "/blog/api/markdown-import/preview/",
            {
                "target_parent_id": 42,
                "title": "导入文章",
                "blocks": [
                    {"block_type": "markdown_block", "value": "# 标题", "source_start_line": 1, "source_end_line": 1}
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = MarkdownImportPreviewView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "preview")
        self.assertEqual(response.data["block_count"], 1)
        manager.create.assert_not_called()

    @mock.patch("blog.markdown_import_api.BlogIndexPage.objects")
    def test_preview_rejects_parent_without_add_permission(self, manager):
        self.parent.permissions_for_user.return_value.can_add_subpage.return_value = False
        manager.filter.return_value.first.return_value = self.parent
        request = self.factory.post(
            "/blog/api/markdown-import/preview/",
            {"target_parent_id": 42, "blocks": []},
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = MarkdownImportPreviewView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "target_parent_forbidden")

    @mock.patch("blog.markdown_import_api.BlogIndexPage.objects")
    def test_preview_reports_server_derived_table_image_locations(self, manager):
        manager.filter.return_value.first.return_value = self.parent
        request = self.factory.post(
            "/blog/api/markdown-import/preview/",
            {
                "target_parent_id": 42,
                "blocks": [
                    {
                        "block_type": "markdown_block",
                        "value": "| 图片 |\n| --- |\n| ![图](photo.png) |\n",
                    }
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = MarkdownImportPreviewView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["media_count"], 1)
        self.assertEqual(response.data["block_media_count"], 0)
        self.assertEqual(response.data["inline_image_count"], 1)
        self.assertEqual(response.data["inline_images"][0]["table_index"], 1)
        self.assertEqual(response.data["inline_images"][0]["cell_index"], 1)

    @mock.patch("blog.markdown_import_api.BlogIndexPage.objects")
    def test_request_error_response_is_renderer_finalized(self, manager):
        self.user.has_perm = lambda permission: False
        request = self.factory.post(
            "/blog/api/markdown-import/preview/",
            {"target_parent_id": 42, "blocks": []},
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = MarkdownImportPreviewView.as_view()(request)

        self.assertEqual(response.status_code, 403)
        response.render()
        self.assertIn("import_permission_denied", response.content.decode())

    def test_manifest_rejects_artifact_media_type_mismatch(self):
        blocks = _blocks(
            {
                "blocks": [
                    {
                        "block_type": "image_block",
                        "value": {"source": "photo.png"},
                    }
                ]
            }
        )

        with self.assertRaisesMessage(ValueError, "artifact_media_type_mismatch"):
            _artifact_manifest(
                {
                    "artifacts": [
                        {
                            "artifact_id": "00000000-0000-4000-8000-000000000001",
                            "media_type": "audio",
                            "normalized_source": "photo.png",
                            "upload_field": "artifact_00000000-0000-4000-8000-000000000001",
                        }
                    ]
                },
                blocks,
            )

    def test_manifest_rejects_non_https_remote_scheme(self):
        blocks = _blocks(
            {
                "blocks": [
                    {
                        "block_type": "image_block",
                        "value": {"source": "http://example.com/photo.png"},
                    }
                ]
            }
        )

        with self.assertRaisesMessage(ValueError, "source_scheme_invalid"):
            _artifact_manifest(
                {
                    "artifacts": [
                        {
                            "artifact_id": "00000000-0000-4000-8000-000000000001",
                            "media_type": "image",
                            "normalized_source": "http://example.com/photo.png",
                            "source_kind": "local",
                        }
                    ]
                },
                blocks,
            )

    def test_manifest_keeps_client_remote_failure_marker(self):
        blocks = _blocks(
            {
                "blocks": [
                    {
                        "block_type": "image_block",
                        "value": {"source": "https://example.com/photo.png"},
                    }
                ]
            }
        )

        manifests = _artifact_manifest(
            {
                "artifacts": [
                    {
                        "artifact_id": "00000000-0000-4000-8000-000000000001",
                        "media_type": "image",
                        "source_kind": "remote_https",
                        "normalized_source": "https://example.com/photo.png",
                        "safe_filename": "remote-image.png",
                        "size_bytes": 0,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                        "preflight_error_code": "client_download_failed",
                    }
                ]
            },
            blocks,
        )

        self.assertEqual(manifests[0]["preflight_error_code"], "client_download_failed")

    def test_manifest_accepts_server_derived_table_image_reference(self):
        blocks = _blocks(
            {
                "blocks": [
                    {
                        "block_type": "markdown_block",
                        "value": "| 图片 |\n| --- |\n| ![图](assets/photo.png) |\n",
                    }
                ]
            }
        )

        manifests = _artifact_manifest(
            {
                "artifacts": [
                    {
                        "artifact_id": "00000000-0000-4000-8000-000000000001",
                        "media_type": "image",
                        "source_kind": "local",
                        "normalized_source": "assets/photo.png",
                        "reference_sources": ["assets/photo.png"],
                        "reference_scope": "inline_image",
                        "occurrence_ids": [blocks[0].inline_images[0].occurrence_id],
                        "safe_filename": "photo.png",
                        "size_bytes": 5,
                        "sha256": hashlib.sha256(b"photo").hexdigest(),
                    }
                ]
            },
            blocks,
        )

        self.assertEqual(manifests[0]["reference_sources"], ("assets/photo.png",))
        self.assertEqual(manifests[0]["reference_scope"], "inline_image")

    def test_fingerprint_ignores_transport_artifact_identifiers(self):
        base = {
            "target_parent_id": 42,
            "title": "导入文章",
            "date": "2026-08-18",
            "intro": "简介",
            "tags": [],
            "blocks": [{"block_type": "markdown_block", "value": "正文"}],
        }
        first = _fingerprint_payload(
            base,
            [{
                "artifact_id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
                "upload_field": "artifact_first",
                "media_type": "image",
                "normalized_source": "photo.png",
            }],
        )
        second = _fingerprint_payload(
            base,
            [{
                "artifact_id": uuid.UUID("00000000-0000-4000-8000-000000000002"),
                "upload_field": "artifact_second",
                "media_type": "image",
                "normalized_source": "photo.png",
            }],
        )

        self.assertEqual(first, second)

    @mock.patch("blog.markdown_import_api.session_payload")
    @mock.patch("blog.markdown_import_api.MarkdownImportArtifact.objects.create")
    @mock.patch("blog.markdown_import_api.MarkdownImportSession.objects.create")
    @mock.patch("blog.markdown_import_api.claim_import_batch")
    @mock.patch("blog.markdown_import_api._artifact_manifest")
    @mock.patch("blog.markdown_import_api._intro", return_value="简介")
    @mock.patch("blog.markdown_import_api._tags", return_value=())
    @mock.patch("blog.markdown_import_api._blocks", return_value=())
    @mock.patch("blog.markdown_import_api._target_parent")
    def test_session_create_only_persists_manifest_before_media_upload(
        self,
        target_parent,
        blocks,
        tags,
        intro,
        artifact_manifest,
        claim_batch,
        session_create,
        artifact_create,
        payload_response,
    ):
        artifact_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
        parent = SimpleNamespace(pk=42)
        batch = SimpleNamespace(status="pending", save=mock.Mock())
        session = SimpleNamespace(session_id=uuid.uuid4())
        target_parent.return_value = parent
        artifact_manifest.return_value = [
            {
                "artifact_id": artifact_id,
                "position": 0,
                "media_type": "image",
                "source_kind": "local",
                "normalized_source": "photo.png",
                "safe_filename": "photo.png",
                "size_bytes": 5,
                "sha256": hashlib.sha256(b"photo").hexdigest(),
                "preflight_error_code": "",
            }
        ]
        claim_batch.return_value = BatchClaim(batch=batch, created=True)
        session_create.return_value = session
        payload_response.return_value = {"status": "created", "session_id": "session"}
        request = SimpleNamespace(
            data={
                "target_parent_id": 42,
                "title": "导入文章",
                "intro": "简介",
                "blocks": [],
                "artifacts": [],
                "idempotency_key": "00000000-0000-4000-8000-000000000002",
            },
            user=self.user,
        )

        with mock.patch(
            "blog.markdown_import_api.transaction.atomic",
            return_value=nullcontext(),
        ):
            response = MarkdownImportSessionCreateView().post(request)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["session_id"], "session")
        session_create.assert_called_once()
        artifact_create.assert_called_once()
        self.assertEqual(artifact_create.call_args.kwargs["session"], session)

    @mock.patch("blog.markdown_import_api.compensate_draft_failure")
    @mock.patch("blog.markdown_import_api.create_unpublished_blog_draft")
    @mock.patch("blog.markdown_import_api.assemble_import_body")
    @mock.patch("blog.markdown_import_api.import_media_artifacts")
    @mock.patch("blog.markdown_import_api.MarkdownImportArtifact.objects.create")
    @mock.patch("blog.markdown_import_api.claim_import_batch")
    @mock.patch("blog.markdown_import_api._target_parent")
    def test_import_page_failure_runs_exact_compensation_and_keeps_audit_batch(
        self,
        target_parent,
        claim_batch,
        artifact_create,
        import_media,
        assemble,
        create_draft,
        compensate,
    ):
        artifact_id = uuid.UUID("00000000-0000-4000-8000-000000000001")
        payload_sha256 = hashlib.sha256(b"photo").hexdigest()
        batch = SimpleNamespace(
            batch_id=uuid.UUID("00000000-0000-4000-8000-000000000002"),
            status="pending",
            save=mock.Mock(),
        )
        artifact = SimpleNamespace(
            artifact_id=artifact_id,
            batch=batch,
            position=0,
            media_type="image",
            source_kind="local",
            normalized_source="photo.png",
            safe_filename="photo.png",
            storage_alias="default",
            object_name="markdown-import/object/photo.png",
            status="succeeded",
            save=mock.Mock(),
        )
        target_parent.return_value = self.parent
        claim_batch.return_value = BatchClaim(batch=batch, created=True)
        artifact_create.return_value = artifact
        import_media.return_value = [MediaImportResult("image_block", SimpleNamespace(pk=9))]
        assemble.return_value = [{"type": "image_block", "value": 9}]
        create_draft.side_effect = RuntimeError("draft failure")
        compensate.return_value = DraftCompensationResult(True, ())
        payload = {
            "target_parent_id": 42,
            "title": "导入文章",
            "intro": "用于测试的简介",
            "blocks": [
                {"block_type": "image_block", "value": {"source": "photo.png"}}
            ],
            "artifacts": [
                {
                    "artifact_id": str(artifact_id),
                    "media_type": "image",
                    "normalized_source": "photo.png",
                    "safe_filename": "photo.png",
                    "upload_field": f"artifact_{artifact_id}",
                    "size_bytes": 5,
                    "sha256": payload_sha256,
                }
            ],
            "idempotency_key": "00000000-0000-4000-8000-000000000003",
        }
        request = SimpleNamespace(
            data=payload,
            FILES={f"artifact_{artifact_id}": object()},
            user=self.user,
        )

        with mock.patch(
            "blog.markdown_import_api.transaction.atomic",
            return_value=nullcontext(),
        ):
            response = MarkdownImportView().post(request)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.data["code"], "import_failed")
        self.assertEqual(batch.status, "failed")
        compensate.assert_called_once()
        self.assertEqual(artifact_create.call_count, 1)
        self.assertIsNone(compensate.call_args.kwargs["page"])
        self.assertEqual(
            compensate.call_args.kwargs["media_artifacts"], [artifact]
        )
