import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from blog.services.markdown_import_paths import (
    LocalMediaPathError,
    resolve_local_media_path,
)


class MarkdownImportPathTests(SimpleTestCase):
    def test_resolves_existing_file_inside_source_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "assets" / "diagram.png"
            media.parent.mkdir()
            media.write_bytes(b"image")

            resolved = resolve_local_media_path(root, "assets/diagram.png")

            self.assertEqual(resolved.path, media.resolve())
            self.assertEqual(resolved.normalized_source, "assets/diagram.png")
            self.assertEqual(resolved.safe_filename, "diagram.png")

            repeated = resolve_local_media_path(
                root, "assets/../assets/diagram.png"
            )
            self.assertEqual(
                repeated.normalized_source, resolved.normalized_source
            )
            self.assertEqual(repeated.path, resolved.path)

    def test_rejects_parent_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            root.mkdir()
            outside = base / "secret.png"
            outside.write_bytes(b"secret")

            with self.assertRaisesMessage(LocalMediaPathError, "path_outside_source_root"):
                resolve_local_media_path(root, "../secret.png")

            link = root / "linked.png"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("当前文件系统不允许创建符号链接")
            with self.assertRaisesMessage(LocalMediaPathError, "path_outside_source_root"):
                resolve_local_media_path(root, "linked.png")

    def test_resolves_percent_encoded_unicode_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "第八章.assets" / "图片 01.png"
            media.parent.mkdir()
            media.write_bytes(b"image")

            resolved = resolve_local_media_path(
                root,
                "%E7%AC%AC%E5%85%AB%E7%AB%A0.assets/%E5%9B%BE%E7%89%87%2001.png",
            )

            self.assertEqual(resolved.path, media.resolve())
            self.assertEqual(resolved.normalized_source, "第八章.assets/图片 01.png")

    def test_rejects_percent_encoded_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = Path(directory).parent / "encoded-secret.png"
            outside.write_bytes(b"secret")
            try:
                with self.assertRaisesMessage(LocalMediaPathError, "path_outside_source_root"):
                    resolve_local_media_path(root, "%2e%2e/encoded-secret.png")
            finally:
                outside.unlink(missing_ok=True)

    def test_rejects_absolute_unc_and_non_file_schemes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in (
                "C:\\notes\\image.png",
                "\\\\server\\share\\image.png",
                "/etc/passwd",
                "file:///etc/passwd",
                "javascript:alert(1)",
                "data:image/png;base64,AA==",
            ):
                with self.subTest(source=source):
                    with self.assertRaises(LocalMediaPathError):
                        resolve_local_media_path(root, source)

    def test_rejects_missing_directory_and_unreadable_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "folder"
            folder.mkdir()

            with self.assertRaisesMessage(LocalMediaPathError, "file_missing"):
                resolve_local_media_path(root, "missing.png")
            with self.assertRaisesMessage(LocalMediaPathError, "not_a_file"):
                resolve_local_media_path(root, "folder")

            media = root / "image.png"
            media.write_bytes(b"image")
            with mock.patch.object(os, "access", return_value=False):
                with self.assertRaisesMessage(LocalMediaPathError, "file_unreadable"):
                    resolve_local_media_path(root, "image.png")
