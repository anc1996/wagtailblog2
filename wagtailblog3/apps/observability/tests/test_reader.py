"""验证受限日志读取、轮转跟随和游标失效处理。"""

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from observability.reader import _decode_cursor, read_logs, resolve_registered_path
from observability.registry import LOG_FILE_BY_KEY


def _line(second, level="ERROR", message="failure"):
    return (
        f"[2026-07-29 15:00:{second:02d}] {level} [blog.views:run:10] "
        f"[pid=1 thread=MainThread] {message} {second}\n"
    )


class LogReaderTests(SimpleTestCase):
    """验证日志读取、轮转跟随、游标失效和结果筛选。"""
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "blog").mkdir()

    def test_signed_cursor_reads_older_records_without_duplicates(self):
        path = self.root / "blog/blog_error.log"
        path.write_text("".join(_line(i) for i in range(6)), encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            first = read_logs(domain="blog", kind="error", page_size=2)
            second = read_logs(domain="blog", kind="error", page_size=2, cursor=first.next_cursor)
        self.assertEqual([item.message for item in first.records], ["failure 5", "failure 4"])
        self.assertEqual([item.message for item in second.records], ["failure 3", "failure 2"])

    def test_cursor_follows_same_inode_after_rotation(self):
        current = self.root / "blog/blog_error.log"
        current.write_text("".join(_line(i) for i in range(6)), encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            first = read_logs(domain="blog", kind="error", page_size=2)
            current.rename(f"{current}.1")
            current.write_text(_line(10, message="new file"), encoding="utf-8")
            second = read_logs(
                domain="blog",
                kind="error",
                page_size=2,
                cursor=first.next_cursor,
            )
        self.assertEqual([item.message for item in second.records], ["failure 3", "failure 2"])
        self.assertTrue(all(item.rotation == 1 for item in second.records))

    def test_cursor_continues_snapshot_when_current_file_only_appends(self):
        current = self.root / "blog/blog_error.log"
        current.write_text("".join(_line(i) for i in range(6)), encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            first = read_logs(domain="blog", kind="error", page_size=2)
            with current.open("a", encoding="utf-8") as stream:
                stream.write(_line(10, message="appended after snapshot"))
            second = read_logs(
                domain="blog",
                kind="error",
                page_size=2,
                cursor=first.next_cursor,
            )

        self.assertEqual([item.message for item in first.records], ["failure 5", "failure 4"])
        self.assertEqual([item.message for item in second.records], ["failure 3", "failure 2"])

    def test_cursor_does_not_treat_truncated_content_as_older_page(self):
        current = self.root / "blog/blog_error.log"
        current.write_text("".join(_line(i) for i in range(6)), encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            first = read_logs(domain="blog", kind="error", page_size=2)
            current.write_text(_line(9, message="replacement"), encoding="utf-8")
            second = read_logs(
                domain="blog",
                kind="error",
                page_size=2,
                cursor=first.next_cursor,
            )
        self.assertEqual(second.records, [])

    def test_rotated_files_are_opt_in(self):
        current = self.root / "blog/blog_error.log"
        current.write_text(_line(5), encoding="utf-8")
        Path(f"{current}.1").write_text(_line(1), encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            current_only = read_logs(domain="blog", kind="error", page_size=10)
            all_versions = read_logs(domain="blog", kind="error", include_rotated=True, page_size=10)
        self.assertEqual(len(current_only.records), 1)
        self.assertEqual(len(all_versions.records), 2)

    def test_keyword_and_level_filters(self):
        path = self.root / "blog/blog_error.log"
        path.write_text(_line(1, "ERROR", "needle") + _line(2, "CRITICAL", "other"), encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            result = read_logs(domain="blog", kind="error", level="ERROR", keyword="NEEDLE", page_size=10)
        self.assertEqual(len(result.records), 1)
        self.assertIn("needle", result.records[0].message)
        self.assertEqual(result.records[0].source_path, "blog/blog_error.log")

    def test_custom_end_time_filters_newer_records(self):
        path = self.root / "blog/blog_error.log"
        path.write_text(_line(1) + _line(2), encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            result = read_logs(
                domain="blog",
                kind="error",
                until=datetime(2026, 7, 29, 15, 0, 1),
                page_size=10,
            )
        self.assertEqual([record.message for record in result.records], ["failure 1"])

    def test_deep_level_and_keyword_filters_scan_past_newer_non_matches(self):
        path = self.root / "blog/blog_error.log"
        deep = _line(0, "WARNING", "deep needle")
        filler = "".join(
            _line(i % 60, "ERROR", f"tail filler {i}") for i in range(6000)
        )
        path.write_text(deep + filler, encoding="utf-8")
        self.assertGreater(path.stat().st_size, 500 * 1024)

        with override_settings(LOG_DIR=self.root):
            result = read_logs(
                domain="blog",
                kind="error",
                level="WARNING",
                keyword="DEEP NEEDLE",
                page_size=50,
            )

        self.assertEqual([record.message for record in result.records], ["deep needle 0"])
        self.assertGreater(result.bytes_read, 64 * 1024)

    def test_dense_tail_stops_after_the_initial_window(self):
        path = self.root / "blog/blog_error.log"
        path.write_text(
            "".join(_line(i % 60, "ERROR", f"dense tail {i}") for i in range(5000)),
            encoding="utf-8",
        )
        self.assertGreater(path.stat().st_size, 2 * 64 * 1024)

        with override_settings(LOG_DIR=self.root):
            result = read_logs(domain="blog", kind="error", page_size=10)

        self.assertEqual(len(result.records), 10)
        self.assertEqual(result.bytes_read, 64 * 1024)

    def test_time_filter_scans_past_tail_without_matches(self):
        path = self.root / "blog/blog_error.log"
        older = _line(0, "ERROR", "inside time window")
        newer = "".join(
            _line(i % 60, "ERROR", f"newer filler {i}") for i in range(1200)
        ).replace("2026-07-29 15:", "2026-07-29 16:")
        path.write_text(older + newer, encoding="utf-8")

        with override_settings(LOG_DIR=self.root):
            result = read_logs(
                domain="blog",
                kind="error",
                until=datetime(2026, 7, 29, 15, 0, 30),
                page_size=50,
            )

        self.assertEqual(
            [record.message for record in result.records], ["inside time window 0"]
        )

    def test_empty_page_keeps_cursor_and_advances_scanned_offset(self):
        path = self.root / "blog/blog_error.log"
        path.write_text(
            _line(0, "WARNING", "eventual match")
            + "".join(
                _line(i % 60, "ERROR", f"non matching {i}") for i in range(5000)
            ),
            encoding="utf-8",
        )

        results = []
        cursor = ""
        with override_settings(LOG_DIR=self.root), patch(
            "observability.reader.MAX_TOTAL_BYTES", 128 * 1024
        ):
            for _ in range(8):
                result = read_logs(
                    domain="blog",
                    kind="error",
                    level="WARNING",
                    page_size=50,
                    cursor=cursor,
                )
                results.append(result)
                cursor = result.next_cursor
                if result.records:
                    break

        self.assertEqual(results[0].records, [])
        self.assertTrue(results[0].has_more)
        self.assertTrue(all(item.bytes_read <= 128 * 1024 for item in results))
        self.assertEqual(
            [record.message for record in results[-1].records], ["eventual match 0"]
        )

    def test_rejects_symlink(self):
        outside = self.root / "outside.log"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "blog/blog_error.log"
        link.symlink_to(outside)
        with override_settings(LOG_DIR=self.root):
            with self.assertRaisesRegex(ValueError, "符号链接"):
                resolve_registered_path(LOG_FILE_BY_KEY["blog_error"])

    def test_request_result_limit_is_enforced(self):
        path = self.root / "blog/blog_error.log"
        path.write_text("".join(_line(i) for i in range(6)), encoding="utf-8")
        with override_settings(LOG_DIR=self.root), patch(
            "observability.reader.MAX_RESULTS", 2
        ):
            result = read_logs(domain="blog", kind="error", page_size=10)
        self.assertEqual(len(result.records), 2)

    def test_multiple_files_merge_and_paginate_without_duplicates(self):
        (self.root / "base").mkdir()
        (self.root / "blog/blog_error.log").write_text(
            _line(1) + _line(3) + _line(5), encoding="utf-8"
        )
        (self.root / "base/base_error.log").write_text(
            _line(0) + _line(2) + _line(4), encoding="utf-8"
        )
        with override_settings(LOG_DIR=self.root):
            first = read_logs(kind="error", page_size=3)
            second = read_logs(kind="error", page_size=3, cursor=first.next_cursor)
        first_messages = [item.message for item in first.records]
        second_messages = [item.message for item in second.records]
        self.assertEqual(first_messages, ["failure 5", "failure 4", "failure 3"])
        self.assertEqual(second_messages, ["failure 2", "failure 1", "failure 0"])
        self.assertFalse(set(first_messages) & set(second_messages))

    def test_multiple_sources_continue_without_duplicates_or_gaps(self):
        (self.root / "base").mkdir()
        blog_lines = "".join(_line(i, message=f"blog-{i}") for i in range(10))
        base_lines = "".join(_line(i, message=f"base-{i}") for i in range(10))
        (self.root / "blog/blog_error.log").write_text(blog_lines, encoding="utf-8")
        (self.root / "base/base_error.log").write_text(base_lines, encoding="utf-8")

        messages = []
        cursor = ""
        with override_settings(LOG_DIR=self.root):
            while True:
                result = read_logs(kind="error", page_size=3, cursor=cursor)
                messages.extend(record.message for record in result.records)
                if not result.has_more:
                    break
                cursor = result.next_cursor

        expected = {f"blog-{i} {i}" for i in range(10)} | {
            f"base-{i} {i}" for i in range(10)
        }
        self.assertEqual(len(messages), 20)
        self.assertEqual(len(set(messages)), 20)
        self.assertEqual(set(messages), expected)

    def test_large_unstructured_log_returns_unknown_records_and_paginates(self):
        path = self.root / "blog/blog_error.log"
        path.write_bytes(b"x" * (9 * 1024 * 1024))

        all_offsets = []
        cursor = ""
        with override_settings(LOG_DIR=self.root):
            while True:
                result = read_logs(
                    domain="blog", kind="error", page_size=50, cursor=cursor
                )
                self.assertTrue(result.records)
                self.assertTrue(all(record.level == "UNKNOWN" for record in result.records))
                all_offsets.extend(record.start_offset for record in result.records)
                if not result.has_more:
                    break
                cursor = result.next_cursor

        self.assertEqual(len(all_offsets), 144)
        self.assertEqual(len(set(all_offsets)), 144)
        self.assertEqual(min(all_offsets), 0)
        self.assertEqual(max(all_offsets), 9 * 1024 * 1024 - 64 * 1024)

    def test_unstructured_read_respects_single_request_byte_limit(self):
        path = self.root / "blog/blog_error.log"
        path.write_bytes(b"x" * (3 * 1024 * 1024))

        with override_settings(LOG_DIR=self.root), patch(
            "observability.reader.MAX_TOTAL_BYTES", 1024 * 1024
        ):
            result = read_logs(domain="blog", kind="error", page_size=200)

        self.assertEqual(result.bytes_read, 1024 * 1024)
        self.assertEqual(len(result.records), 16)
        self.assertTrue(result.has_more)

    def test_cursor_rejects_copytruncate_that_regrows_past_offset(self):
        current = self.root / "blog/blog_error.log"
        original = "".join(_line(i, message="original") for i in range(20))
        current.write_text(original, encoding="utf-8")
        with override_settings(LOG_DIR=self.root):
            first = read_logs(domain="blog", kind="error", page_size=2)
            replacement = "".join(
                _line(i, message="replacement") for i in range(30)
            )
            current.write_text(replacement, encoding="utf-8")
            second = read_logs(
                domain="blog",
                kind="error",
                page_size=2,
                cursor=first.next_cursor,
            )

        self.assertTrue(first.has_more)
        self.assertEqual(second.records, [])
        self.assertFalse(second.has_more)

    def test_copytruncate_is_rejected_even_when_boundary_anchor_is_unchanged(self):
        current = self.root / "blog/blog_error.log"
        original = "".join(
            _line(i % 60, message=f"original {i}") for i in range(3000)
        ).encode()
        current.write_bytes(original)
        with override_settings(LOG_DIR=self.root):
            first = read_logs(domain="blog", kind="error", page_size=2)
            state = next(iter(_decode_cursor(first.next_cursor).values()))
            replacement = bytearray(b"x" * len(original))
            anchor_start = max(0, state.offset - 128)
            replacement[anchor_start : state.offset] = original[
                anchor_start : state.offset
            ]
            current.write_bytes(replacement)
            second = read_logs(
                domain="blog",
                kind="error",
                page_size=2,
                cursor=first.next_cursor,
            )

        self.assertTrue(first.has_more)
        self.assertEqual(second.records, [])
        self.assertFalse(second.has_more)

    def test_multi_source_non_matches_advance_while_other_source_returns(self):
        (self.root / "base").mkdir()
        (self.root / "base/base_error.log").write_text(
            _line(59, "WARNING", "base visible"), encoding="utf-8"
        )
        blog = self.root / "blog/blog_error.log"
        blog.write_text(
            _line(0, "WARNING", "blog deep")
            + "".join(
                _line(i % 60, "ERROR", f"blog filler {i}") for i in range(4000)
            ),
            encoding="utf-8",
        )

        messages = []
        pages = []
        cursor = ""
        with override_settings(LOG_DIR=self.root), patch(
            "observability.reader.MAX_TOTAL_BYTES", 128 * 1024
        ):
            for _ in range(8):
                result = read_logs(
                    kind="error",
                    level="WARNING",
                    page_size=1,
                    cursor=cursor,
                )
                pages.append(result)
                messages.extend(record.message for record in result.records)
                if not result.has_more:
                    break
                cursor = result.next_cursor

        self.assertEqual(messages, ["base visible 59", "blog deep 0"])
        self.assertTrue(any(not page.records and page.has_more for page in pages))
        self.assertTrue(all(page.bytes_read <= 128 * 1024 for page in pages))

    def test_global_byte_budget_defers_unvisited_source_without_losing_it(self):
        (self.root / "base").mkdir()
        (self.root / "base/base_error.log").write_text(
            "".join(
                _line(i % 60, "ERROR", f"base filler {i}") for i in range(2500)
            ),
            encoding="utf-8",
        )
        (self.root / "blog/blog_error.log").write_text(
            _line(1, "WARNING", "deferred blog"), encoding="utf-8"
        )

        messages = []
        cursor = ""
        with override_settings(LOG_DIR=self.root), patch(
            "observability.reader.MAX_TOTAL_BYTES", 64 * 1024
        ):
            for _ in range(8):
                result = read_logs(
                    kind="error",
                    level="WARNING",
                    page_size=1,
                    cursor=cursor,
                )
                messages.extend(record.message for record in result.records)
                if not result.has_more:
                    break
                cursor = result.next_cursor

        self.assertEqual(messages, ["deferred blog 1"])
