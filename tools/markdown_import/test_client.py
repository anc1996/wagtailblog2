import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from tools.markdown_import.client import (
    _checkpoint_can_resume,
    build_ai_context,
    build_import_manifest,
    build_request_fingerprint,
    generate_ai_metadata,
    import_markdown,
    inspect_markdown,
)
from blog.services.markdown_import_remote import RemoteImageDownloadError


class MarkdownImportClientTests(SimpleTestCase):
    def test_ai_context_excludes_media_urls_paths_and_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "article.md"
            markdown.write_text(
                "# Django 导入\n\n"
                "正文包含[官方文档](https://example.com/docs)和说明。\n\n"
                "![远程图](https://example.com/image.png)\n\n"
                '<img src="C:\\secret\\photo.png" alt="本地图">\n\n'
                "附件路径 assets/private/photo.png 和 /mnt/f/private/audio.mp3。\n\n"
                "[参考资料]: https://example.com/reference\n\n"
                "```python\nprint('secret')\n```\n",
                encoding="utf-8",
            )

            context = build_ai_context(markdown)

            self.assertIn("Django 导入", context)
            self.assertIn("官方文档", context)
            self.assertNotIn("远程图", context)
            self.assertNotIn("本地图", context)
            self.assertNotIn("https://", context)
            self.assertNotIn("C:\\secret", context)
            self.assertNotIn("assets/private", context)
            self.assertNotIn("/mnt/f/private", context)
            self.assertNotIn("example.com/reference", context)
            self.assertNotIn("print", context)

    @mock.patch("tools.markdown_import.client.requests.post")
    def test_generate_ai_metadata_sends_only_sanitized_context(self, post):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "article.md"
            markdown.write_text("正文 [链接](https://example.com/private)", encoding="utf-8")
            post.return_value.status_code = 200
            post.return_value.json.return_value = {
                "status": "ok",
                "suggestion": {"intro": "简介", "tags": ["Django", "导入", "测试"]},
            }

            result = generate_ai_metadata(
                markdown,
                url="https://blog.example.test/zh-hans",
                token="secret-token",
                target_parent_id=42,
                template_id=3,
            )

            self.assertEqual(result["intro"], "简介")
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["template_id"], 3)
            self.assertNotIn("https://example.com", payload["context"])
            self.assertNotIn("secret-token", str(payload))
    def test_request_fingerprint_changes_with_per_file_metadata(self):
        manifest = {
            "title": "同名文章",
            "intro": "简介",
            "date": "2026-08-19",
            "tags": ["python"],
            "options": {"allow_external_images": False},
            "blocks": [{"block_type": "markdown_block", "value": "正文"}],
            "artifacts": [{
                "artifact_id": "one",
                "upload_field": "artifact_one",
                "media_type": "image",
                "source_kind": "local",
                "normalized_source": "photo.png",
                "sha256": "abc",
            }],
        }

        original = build_request_fingerprint(manifest, 42)
        changed = dict(manifest, intro="另一段简介")

        self.assertNotEqual(original, build_request_fingerprint(changed, 42))
        self.assertEqual(original, build_request_fingerprint(
            dict(manifest, artifacts=[dict(manifest["artifacts"][0], artifact_id="two", upload_field="artifact_two")]),
            42,
        ))

    def test_only_matching_active_v2_checkpoint_can_resume(self):
        fingerprint = "a" * 64
        self.assertTrue(_checkpoint_can_resume({
            "version": 2,
            "legacy": False,
            "request_fingerprint": fingerprint,
            "session_status": "uploading",
        }, fingerprint))
        self.assertFalse(_checkpoint_can_resume({
            "version": 1,
            "legacy": True,
            "request_fingerprint": fingerprint,
            "session_status": "uploading",
        }, fingerprint))
        self.assertFalse(_checkpoint_can_resume({
            "version": 2,
            "legacy": False,
            "request_fingerprint": fingerprint,
            "session_status": "success",
        }, fingerprint))

    def test_import_uses_session_protocol_with_one_file_per_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "photo.png").write_bytes(b"image")
            markdown = root / "article.md"
            markdown.write_text("![图片](photo.png)\n", encoding="utf-8")

            def response(status_code, payload):
                item = mock.Mock(status_code=status_code)
                item.json.return_value = payload
                return item

            artifact_id = "00000000-0000-4000-8000-000000000001"
            session_id = "00000000-0000-4000-8000-000000000002"
            responses = [
                response(201, {"session_id": session_id, "completed_artifacts": 0}),
                response(200, {"session_id": session_id, "completed_artifacts": 1}),
                response(202, {"session_id": session_id, "completed_artifacts": 1}),
            ]
            with mock.patch(
                "tools.markdown_import.client.uuid.uuid4",
                return_value=uuid.UUID(artifact_id),
            ), mock.patch(
                "tools.markdown_import.client.requests.post",
                side_effect=responses,
            ) as post, mock.patch(
                "tools.markdown_import.client.requests.get",
                return_value=response(
                    200,
                    {
                        "status": "success",
                        "session_id": session_id,
                        "batch_id": "batch",
                        "page_id": 5,
                        "revision_id": 9,
                        "completed_artifacts": 1,
                    },
                ),
            ):
                result = import_markdown(
                    markdown,
                    root,
                    url="http://test/zh-hans",
                    token="test-token",
                    target_parent_id=42,
                    idempotency_key="00000000-0000-4000-8000-000000000003",
                    allow_external_images=False,
                )

            self.assertEqual(result["page_id"], 5)
            self.assertEqual(post.call_count, 3)
            self.assertTrue(post.call_args_list[0].args[0].endswith("/sessions/"))
            self.assertIn("/artifacts/", post.call_args_list[1].args[0])
            self.assertIn("files", post.call_args_list[1].kwargs)

    def test_inspect_is_local_only_and_reports_safe_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / "assets" / "photo.png").write_bytes(b"image")
            markdown = root / "article.md"
            markdown.write_text("# 标题\n\n![图](assets/photo.png)\n", encoding="utf-8")

            result = inspect_markdown(markdown, root, allow_external_images=False)

            self.assertEqual(result["status"], "preview")
            self.assertEqual(result["media_count"], 1)
            self.assertEqual(result["local_files"][0]["source"], "assets/photo.png")
            self.assertNotIn(str(root), str(result))

    def test_manifest_uses_unique_artifact_key_and_no_absolute_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "photo.png").write_bytes(b"image")
            markdown = root / "article.md"
            markdown.write_text("![图](photo.png)\n", encoding="utf-8")

            manifest, files, _ = build_import_manifest(
                markdown, root, allow_external_images=False
            )

            self.assertEqual(len(manifest["artifacts"]), 1)
            artifact = manifest["artifacts"][0]
            self.assertTrue(artifact["upload_field"].startswith("artifact_"))
            self.assertNotIn(str(root), str(manifest))
            self.assertEqual(files[0][1], root / "photo.png")
            self.assertEqual(manifest["artifacts"][0]["size_bytes"], 5)
            self.assertEqual(len(manifest["artifacts"][0]["sha256"]), 64)

    def test_manifest_accepts_gui_metadata_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "article.md"
            markdown.write_text("# Body\n", encoding="utf-8")

            manifest, _, _ = build_import_manifest(
                markdown,
                root,
                allow_external_images=False,
                metadata_overrides={
                    "title": "GUI title",
                    "intro": "GUI intro",
                    "date": "2026-08-18",
                    "tags": ["gui"],
                },
            )

            self.assertEqual(manifest["title"], "GUI title")
            self.assertEqual(manifest["intro"], "GUI intro")
            self.assertEqual(manifest["date"], "2026-08-18")
            self.assertEqual(manifest["tags"], ["gui"])

    def test_remote_download_failure_becomes_single_missing_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "article.md"
            markdown.write_text(
                "![remote](https://example.com/photo.png)\n", encoding="utf-8"
            )
            with mock.patch(
                "tools.markdown_import.client.download_remote_image",
                side_effect=RemoteImageDownloadError("remote_fetch_failed"),
            ):
                manifest, files, _ = build_import_manifest(
                    markdown, root, allow_external_images=True
                )
            self.assertEqual(files, [])
            self.assertEqual(
                manifest["artifacts"][0]["preflight_error_code"],
                "client_download_failed",
            )
            self.assertEqual(manifest["artifacts"][0]["size_bytes"], 0)
            self.assertFalse((root / ".markdown-import-tmp").exists())

    def test_manifest_rejects_same_source_with_different_media_types(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "clip.mp4").write_bytes(b"media")
            markdown = root / "article.md"
            markdown.write_text(
                "![same](clip.mp4)\n\n<audio src=\"clip.mp4\"></audio>\n",
                encoding="utf-8",
            )
            with self.assertRaisesMessage(ValueError, "media_source_type_conflict"):
                build_import_manifest(
                    markdown, root, allow_external_images=False
                )

    def test_front_matter_is_metadata_and_not_a_markdown_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "article.md").write_text(
                "---\n"
                "title: 自定义标题\n"
                "date: 2026-08-18\n"
                "intro: 一段简介\n"
                "tags: [标签一, 标签二]\n"
                "unknown: ignored\n"
                "---\n"
                "# 正文\n",
                encoding="utf-8",
            )

            manifest, _, _ = build_import_manifest(
                root / "article.md", root, allow_external_images=False
            )

            self.assertEqual(manifest["title"], "自定义标题")
            self.assertEqual(manifest["date"], "2026-08-18")
            self.assertEqual(manifest["tags"], ["标签一", "标签二"])
            self.assertEqual(manifest["blocks"][0]["value"], "# 正文\n")

    def test_table_image_and_standalone_image_share_one_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "assets").mkdir()
            (root / "assets" / "photo.png").write_bytes(b"image")
            markdown = root / "article.md"
            markdown.write_text(
                "| 表格 | 图片 |\n"
                "| --- | --- |\n"
                "| 内容 | ![表格图](assets/photo.png) |\n\n"
                "![独占图](assets/photo.png)\n",
                encoding="utf-8",
            )

            manifest, files, _ = build_import_manifest(
                markdown, root, allow_external_images=False
            )

            self.assertEqual(len(manifest["artifacts"]), 1)
            self.assertEqual(len(files), 1)
            artifact = manifest["artifacts"][0]
            self.assertEqual(artifact["reference_scope"], "mixed")
            self.assertEqual(artifact["reference_sources"], ["assets/photo.png"])
            self.assertEqual(len(artifact["occurrence_ids"]), 1)
            markdown_blocks = [
                block for block in manifest["blocks"]
                if block["block_type"] == "markdown_block"
            ]
            self.assertEqual(len(markdown_blocks[0]["inline_images"]), 1)

    def test_inspect_reports_table_image_counts_and_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.png").write_bytes(b"one")
            markdown = root / "article.md"
            markdown.write_text(
                "| 图片 |\n| --- |\n| ![本地](one.png) |\n| ![远程](https://example.com/two.png) |\n",
                encoding="utf-8",
            )

            result = inspect_markdown(markdown, root, allow_external_images=False)

            self.assertEqual(result["inline_image_count"], 2)
            self.assertEqual(result["inline_local_image_count"], 1)
            self.assertEqual(result["inline_remote_image_count"], 1)
            self.assertEqual(len(result["inline_images"]), 2)
            self.assertEqual(result["inline_images"][0]["table_index"], 1)
