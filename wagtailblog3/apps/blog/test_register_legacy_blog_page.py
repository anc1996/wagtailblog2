"""旧 BlogPage 登记命令的只读边界测试。"""

import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core import management
from django.core.management import CommandError
from django.test import SimpleTestCase

from blog.management.commands.register_legacy_blog_page import _body_sha256, _revision_content


class RegisterLegacyBlogPageCommandTests(SimpleTestCase):
	"""验证命令默认 dry-run，且不会误启用写入路径。"""

	def test_apply_is_explicitly_rejected(self):
		with self.assertRaises(CommandError):
			management.call_command("register_legacy_blog_page", apply=True)

	def test_revision_content_and_hash_are_deterministic(self):
		revision = SimpleNamespace(content=json.dumps({"body": [], "mongo_body_version_id": "v1"}))
		self.assertEqual(_revision_content(revision)["mongo_body_version_id"], "v1")
		self.assertEqual(_revision_content(SimpleNamespace(content="not-json")), None)
		self.assertEqual(_body_sha256([{"type": "rich_text", "value": "测试"}]), _body_sha256([{"type": "rich_text", "value": "测试"}]))

	def test_default_handle_marks_report_read_only_without_database_write(self):
		output = StringIO()
		objects = MagicMock()
		objects.filter.return_value.order_by.return_value.only.return_value.__getitem__.return_value = []
		with patch(
			"blog.management.commands.register_legacy_blog_page.Command._build_report",
			return_value={"scanned": 0, "category_counts": {}, "pages": [], "mongo_error": None},
		), patch("blog.management.commands.register_legacy_blog_page.BlogPage.objects", objects):
			management.call_command("register_legacy_blog_page", stdout=output)
		payload = json.loads(output.getvalue())
		self.assertTrue(payload["read_only"])
		self.assertTrue(payload["dry_run"])

	def test_apply_rejects_hash_mismatch_before_mongo_write(self):
		command = __import__(
			"blog.management.commands.register_legacy_blog_page",
			fromlist=["Command"],
		).Command()
		page = SimpleNamespace(pk=38, live=True)
		with patch(
			"blog.management.commands.register_legacy_blog_page.BlogPage.objects"
		) as objects, patch.object(command, "_read_mongo", return_value=({38: {"body": []}}, {})), patch(
			"wagtailblog3.mongo.MongoManager.save_content_body_version"
		) as save_version:
			objects.filter.return_value.only.return_value.first.return_value = page
			with self.assertRaises(CommandError):
				command._handle_apply({"page_id": 38, "expected_hash": "0" * 64})
			save_version.assert_not_called()
