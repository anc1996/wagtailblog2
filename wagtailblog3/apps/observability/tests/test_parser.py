"""验证日志头、相对路径和多行异常信息的解析。"""

from django.test import SimpleTestCase

from observability.parser import parse_bytes


class LogParserTests(SimpleTestCase):
    """验证标准日志头、相对路径和多行回溯的解析结果。"""
    def test_parses_standard_record_and_multiline_traceback(self):
        data = (
            b"[2026-07-29 15:00:00] ERROR [blog.views:save:42] "
            b"[pid=10 thread=MainThread] save failed\n"
            b"Traceback (most recent call last):\n"
            b"  File \"views.py\", line 42\n"
            b"ValueError: bad value\n"
        )
        records = parse_bytes(data)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.logger, "blog.views")
        self.assertEqual(record.function, "save")
        self.assertEqual(record.line, 42)
        self.assertEqual(record.pid, 10)
        self.assertIn("ValueError: bad value", record.traceback)

    def test_parses_new_relative_path_format(self):
        records = parse_bytes(
            b"[2026-07-29 15:00:00] ERROR "
            b"[blog.views|wagtailblog3/apps/blog/views.py:save:42] "
            b"[pid=10 thread=MainThread] save failed\n"
        )
        record = records[0]
        self.assertEqual(record.logger, "blog.views")
        self.assertEqual(record.relative_path, "wagtailblog3/apps/blog/views.py")
        self.assertEqual(record.function, "save")
        self.assertEqual(record.line, 42)

    def test_parses_email_format_without_function(self):
        records = parse_bytes(
            b"[2026-07-29 15:01:00] INFO [django.core.mail] "
            b"[pid=11 thread=worker] sent\n"
        )
        self.assertEqual(records[0].logger, "django.core.mail")
        self.assertEqual(records[0].function, "")
        self.assertIsNone(records[0].line)

    def test_preserves_unstructured_content(self):
        records = parse_bytes(b"uWSGI runtime output\nsecond line\n")
        self.assertEqual(records[0].level, "UNKNOWN")
        self.assertIn("second line", records[0].raw)

    def test_preserves_exact_record_byte_boundaries(self):
        first = (
            b"[2026-07-29 15:00:00] ERROR [blog.views:run:10] "
            b"[pid=1 thread=MainThread] first\r\n"
        )
        second = (
            b"[2026-07-29 15:00:01] WARNING [blog.views:run:11] "
            b"[pid=1 thread=MainThread] second"
        )

        records = parse_bytes(first + second, base_offset=100)

        self.assertEqual(records[0].start_offset, 100)
        self.assertEqual(records[0].end_offset, 100 + len(first))
        self.assertEqual(records[1].start_offset, 100 + len(first))
        self.assertEqual(records[1].end_offset, 100 + len(first) + len(second))

    def test_display_layer_redacts_secrets_and_absolute_paths(self):
        data = (
            b"[2026-07-29 15:00:00] ERROR [blog.views:save:42] "
            b"[pid=10 thread=MainThread] password=hunter2 "
            b"Authorization: Bearer raw-token\n"
            b"Traceback (most recent call last):\n"
            b"  File \"/home/source/Django/wagtail/wagtailblog2/"
            b"wagtailblog3/apps/blog/views.py\", line 42\n"
            b"RuntimeError: Cookie=sessionid=raw-cookie api_key=raw-key\n"
        )

        record = parse_bytes(data)[0]

        for secret in ("hunter2", "raw-token", "raw-cookie", "raw-key"):
            self.assertNotIn(secret, record.raw)
            self.assertNotIn(secret, record.traceback)
        self.assertNotIn("/home/source/", record.raw)
        self.assertIn("wagtailblog3/apps/blog/views.py", record.traceback)
        self.assertIn("[REDACTED]", record.raw)
