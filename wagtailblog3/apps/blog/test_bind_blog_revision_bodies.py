"""历史 Revision 正文绑定只读审计的回归测试。"""

import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

from blog.management.commands.bind_blog_revision_bodies import Command, _manifest_sha256


class BindBlogRevisionBodiesCommandTests(SimpleTestCase):
    """验证候选仅来自完整 State/Mongo 三元组，且命令没有写入路径。"""

    def test_manifest_hash_is_stable(self) -> None:
        entries = [{"page_id": 38, "revision_id": 1059, "eligible": True}]
        self.assertEqual(_manifest_sha256(entries), _manifest_sha256(entries))

    def test_complete_unbound_live_revision_is_candidate(self) -> None:
        command = Command()
        page = SimpleNamespace(pk=38, live_revision_id=1059)
        state = SimpleNamespace(
            page_id=38,
            published_body_version_id="v1",
            published_body_sha256="a" * 64,
            published_body_schema_version=1,
        )
        revision = SimpleNamespace(pk=1059, content={"title": "historical", "body": "[]"})
        with patch("blog.management.commands.bind_blog_revision_bodies.BlogPublicationState.objects.filter") as states, patch(
            "blog.management.commands.bind_blog_revision_bodies.Revision.objects.filter"
        ) as revisions, patch.object(command, "_read_versions", return_value={("v1", "a" * 64, 1)}):
            states.return_value.only.return_value = [state]
            revisions.return_value.only.return_value = [revision]
            report = command._build_report([page], 0)
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["manifest"][0]["suggested_pointer"]["mongo_body_version_id"], "v1")

    def test_existing_or_unavailable_pointer_is_not_candidate(self) -> None:
        command = Command()
        page = SimpleNamespace(pk=38, live_revision_id=1059)
        state = SimpleNamespace(
            page_id=38,
            published_body_version_id="v1",
            published_body_sha256="a" * 64,
            published_body_schema_version=1,
        )
        revision = SimpleNamespace(pk=1059, content={"mongo_body_version_id": "other"})
        with patch("blog.management.commands.bind_blog_revision_bodies.BlogPublicationState.objects.filter") as states, patch(
            "blog.management.commands.bind_blog_revision_bodies.Revision.objects.filter"
        ) as revisions, patch.object(command, "_read_versions", return_value=set()):
            states.return_value.only.return_value = [state]
            revisions.return_value.only.return_value = [revision]
            report = command._build_report([page], 0)
        self.assertEqual(report["candidate_count"], 0)
        self.assertIn("revision_pointer_conflict", report["rows"][0]["refusal_reasons"])
        self.assertIn("published_body_version_unavailable", report["rows"][0]["refusal_reasons"])

    def test_handle_reports_read_only(self) -> None:
        output = StringIO()
        with patch("blog.management.commands.bind_blog_revision_bodies.BlogPage.objects.filter") as pages, patch.object(
            Command, "_build_report", return_value={"read_only": True, "dry_run": True}
        ):
            pages.return_value.order_by.return_value.only.return_value.__getitem__.return_value = []
            call_command("bind_blog_revision_bodies", stdout=output)
        self.assertTrue(json.loads(output.getvalue())["read_only"])
