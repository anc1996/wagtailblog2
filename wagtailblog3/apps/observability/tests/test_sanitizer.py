import logging
from pathlib import Path

from django.test import SimpleTestCase

from observability.filters import ProjectRelativePathFilter
from observability.sanitizer import REDACTED, SensitiveDataFilter, sanitize_log_text


class LogSanitizerTests(SimpleTestCase):
    def test_redacts_all_required_sensitive_names_and_bearer(self):
        text = " ".join(
            (
                "password=one",
                "passwd=two",
                "token=three",
                "Authorization: Bearer four",
                "Bearer five",
                "Cookie=sessionid=six; csrftoken=seven",
                "sessionid=eight",
                "secret=nine",
                "api_key=ten",
                "access_key=eleven",
                "private_key=twelve",
            )
        )

        cleaned = sanitize_log_text(text)

        for secret in (
            "one", "two", "three", "four", "five", "six", "seven",
            "eight", "nine", "ten", "eleven", "twelve",
        ):
            self.assertNotIn(secret, cleaned)
        self.assertIn(REDACTED, cleaned)

    def test_redacts_json_values_and_private_key_blocks(self):
        text = (
            '{"password": "quoted secret", "api_key": "abc"}\n'
            "-----BEGIN PRIVATE KEY-----\nraw-material\n-----END PRIVATE KEY-----"
        )

        cleaned = sanitize_log_text(text)

        self.assertNotIn("quoted secret", cleaned)
        self.assertNotIn("raw-material", cleaned)
        self.assertGreaterEqual(cleaned.count(REDACTED), 2)

    def test_collapses_project_and_third_party_traceback_paths(self):
        root = Path("/home/source/Django/wagtail/wagtailblog2")
        text = (
            'File "/home/source/Django/wagtail/wagtailblog2/'
            'wagtailblog3/apps/blog/views.py", line 8\n'
            'File "/root/anaconda3/envs/test/lib/python3.13/site-packages/vendor.py", line 2'
        )

        cleaned = sanitize_log_text(text, root)

        self.assertIn('File "wagtailblog3/apps/blog/views.py"', cleaned)
        self.assertIn('File "vendor.py"', cleaned)
        self.assertNotIn("/home/", cleaned)
        self.assertNotIn("/root/", cleaned)

    def test_sensitive_filter_formats_arguments_before_redaction(self):
        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "token=%s", ("raw-token",), None
        )

        self.assertTrue(SensitiveDataFilter().filter(record))

        self.assertEqual(record.args, ())
        self.assertNotIn("raw-token", record.getMessage())
        self.assertIn(REDACTED, record.getMessage())

    def test_relative_path_filter_uses_filename_outside_project(self):
        root = Path("/home/source/Django/wagtail/wagtailblog2")
        filter_instance = ProjectRelativePathFilter(root)
        project = logging.LogRecord(
            "test", logging.INFO, str(root / "wagtailblog3/apps/blog/views.py"), 1, "", (), None
        )
        external = logging.LogRecord(
            "test", logging.INFO, "/root/vendor/package/client.py", 1, "", (), None
        )

        filter_instance.filter(project)
        filter_instance.filter(external)

        self.assertEqual(project.relative_path, "wagtailblog3/apps/blog/views.py")
        self.assertEqual(external.relative_path, "client.py")
        self.assertNotIn("..", project.relative_path)
