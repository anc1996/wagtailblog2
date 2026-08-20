import tempfile
import unittest
from pathlib import Path

from tools.markdown_import_gui import (
    admin_edit_url,
    ai_template_options,
    apply_ai_suggestion,
    checkpoint_reuse_candidate,
    format_inline_image_location,
    normalize_site_url,
    safe_import_error,
    scan_markdown_files,
)


class MarkdownImportGuiHelpersTests(unittest.TestCase):
    def test_ai_template_options_only_accept_valid_summaries(self):
        labels, mapping = ai_template_options([
            {"id": 3, "name": "技术笔记", "version": 2},
            {"id": "bad", "name": "无效", "version": 1},
        ])

        self.assertEqual(labels, ["技术笔记（v2）"])
        self.assertEqual(mapping, {"技术笔记（v2）": 3})

    def test_ai_suggestion_updates_only_current_file_intro_and_tags(self):
        original = {
            "title": "用户标题",
            "intro": "原简介",
            "date": "2026-08-20",
            "tags": ["原标签"],
            "ai_template_id": 3,
        }

        updated = apply_ai_suggestion(
            original,
            {"intro": "AI 简介", "tags": ["Django", "Django", "Wagtail"]},
        )

        self.assertEqual(updated["title"], "用户标题")
        self.assertEqual(updated["intro"], "AI 简介")
        self.assertEqual(updated["tags"], ["Django", "Wagtail"])
        self.assertEqual(original["intro"], "原简介")

    def test_terminal_or_legacy_checkpoint_requires_new_import(self):
        self.assertTrue(checkpoint_reuse_candidate({"session_status": "uploading"}))
        self.assertFalse(checkpoint_reuse_candidate({"session_status": "success"}))
        self.assertFalse(checkpoint_reuse_candidate({"session_status": "uploading", "legacy": True}))

    def test_normalize_host_port_adds_language_prefix(self):
        self.assertEqual(normalize_site_url("192.168.20.5:8080"), "http://192.168.20.5:8080/zh-hans")

    def test_normalize_keeps_explicit_https_path(self):
        self.assertEqual(normalize_site_url("https://blog.example.test/custom/"), "https://blog.example.test/custom")

    def test_admin_url_does_not_inherit_language_prefix(self):
        self.assertEqual(
            admin_edit_url("http://192.168.20.5:8080/zh-hans", 582),
            "http://192.168.20.5:8080/admin/pages/582/edit/",
        )

    def test_rejects_credentials_and_non_http_schemes(self):
        with self.assertRaises(ValueError):
            normalize_site_url("https://user:pass@example.test")
        with self.assertRaises(ValueError):
            normalize_site_url("file:///tmp/blog")

    def test_scan_only_top_level_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.md").write_text("b", encoding="utf-8")
            (root / "a.MD").write_text("a", encoding="utf-8")
            (root / "notes.txt").write_text("x", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "c.md").write_text("c", encoding="utf-8")
            self.assertEqual([path.name for path in scan_markdown_files(root)], ["a.MD", "b.md"])

    def test_formats_inline_image_location_without_source_path(self):
        item = {
            "table_index": 2,
            "row_index": 4,
            "cell_index": 3,
            "source": r"C:\secret\photo.png",
        }

        rendered = format_inline_image_location(item)

        self.assertEqual(rendered, "表格 2，第 4 行，第 3 列")
        self.assertNotIn("secret", rendered)

    def test_safe_import_error_hides_local_paths(self):
        self.assertEqual(safe_import_error(ValueError("media_failed")), "media_failed")
        self.assertEqual(safe_import_error(ValueError(r"C:\secret\photo.png")), "ValueError")


if __name__ == "__main__":
    unittest.main()
