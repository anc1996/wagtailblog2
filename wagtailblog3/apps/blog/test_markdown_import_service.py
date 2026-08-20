from types import SimpleNamespace
from datetime import date
from unittest import mock

from django.test import SimpleTestCase

from blog.services.markdown_import_service import (
    assemble_import_body,
    compensate_draft_failure,
    create_unpublished_blog_draft,
)
from blog.services.markdown_import_media import MediaImportResult
from blog.services.markdown_import_parser import parse_markdown_blocks
from blog.services.markdown_import_types import MarkdownImportBlock


class MarkdownImportBodyAssemblyTests(SimpleTestCase):
    def test_assembles_blocks_in_source_order_and_keeps_mermaid_code_field(self):
        blocks = (
            MarkdownImportBlock("markdown_block", "# 标题\n", 1, 1),
            MarkdownImportBlock(
                "image_block",
                {"source": "assets/photo.png", "title": "照片"},
                2,
                2,
            ),
            MarkdownImportBlock(
                "embed_block",
                {"url": "https://www.youtube.com/watch?v=abc", "title": "视频"},
                3,
                3,
            ),
            MarkdownImportBlock(
                "mermaid_chart",
                {"code": "graph TD; A-->B;", "renderer": "modern-v11.12"},
                4,
                6,
            ),
        )
        media_results = {
            "assets/photo.png": MediaImportResult(
                "image_block", SimpleNamespace(pk=12)
            )
        }

        assembled = assemble_import_body(blocks, media_results=media_results)

        self.assertEqual(
            [item["type"] for item in assembled],
            ["markdown_block", "image_block", "embed_block", "mermaid_chart"],
        )
        self.assertEqual(assembled[1]["value"], 12)
        self.assertEqual(
            assembled[2]["value"],
            {"title": "视频", "embed_url": "https://www.youtube.com/watch?v=abc"},
        )
        self.assertEqual(assembled[3]["value"]["code"], "graph TD; A-->B;")
        self.assertEqual(assembled[3]["value"]["renderer"], "modern-v11.12")

    def test_failed_media_result_is_kept_as_an_independent_markdown_block(self):
        block = MarkdownImportBlock(
            "image_block",
            {"source": "missing.png", "title": "缺图"},
            1,
            1,
        )
        media_results = {
            "missing.png": MediaImportResult(
                "markdown_block", "[导入缺失：图片 原始引用：missing.png 原因：media_form_invalid]"
            )
        }

        assembled = assemble_import_body((block,), media_results=media_results)

        self.assertEqual(assembled, [{"type": "markdown_block", "value": media_results["missing.png"].value}])

    def test_embed_block_rejects_unregistered_or_non_https_urls(self):
        block = MarkdownImportBlock(
            "embed_block",
            {"url": "https://evil.example/embed", "title": "外部"},
            1,
            1,
        )

        with self.assertRaisesMessage(ValueError, "embed_url_invalid"):
            assemble_import_body((block,), media_results={})

    def test_table_images_are_rewritten_inside_markdown_without_zoom_style(self):
        source = (
            '<table><tr><td rowspan="2"><img src="assets/photo.png" '
            'alt="图示" style="zoom:30%;" /></td></tr></table>\n'
        )
        blocks = parse_markdown_blocks(source)
        image = SimpleNamespace(
            pk=12,
            file=SimpleNamespace(url="/media/original/photo.png"),
        )

        assembled = assemble_import_body(
            blocks,
            media_results={"assets/photo.png": MediaImportResult("image_block", image)},
        )

        self.assertEqual([item["type"] for item in assembled], ["markdown_block"])
        rewritten = assembled[0]["value"]
        self.assertIn('rowspan="2"', rewritten)
        self.assertIn('embedtype="image"', rewritten)
        self.assertIn('id="12"', rewritten)
        self.assertIn('format="fullwidth_web"', rewritten)
        self.assertIn('src="/media/original/photo.png"', rewritten)
        self.assertIn('alt="图示"', rewritten)
        self.assertNotIn("zoom", rewritten)
        self.assertNotIn("style=", rewritten)

    def test_failed_table_image_marks_only_its_cell_and_keeps_other_image(self):
        source = (
            "| 一 | 二 |\n"
            "| --- | --- |\n"
            "| ![失败](missing.png) | ![成功](ok.png) |\n"
        )
        blocks = parse_markdown_blocks(source)
        image = SimpleNamespace(pk=13, file=SimpleNamespace(url="/media/ok.png"))
        failed = MediaImportResult(
            "markdown_block",
            "[导入缺失：图片 原始引用：missing.png 原因：media_form_invalid]",
        )

        assembled = assemble_import_body(
            blocks,
            media_results={
                "missing.png": failed,
                "ok.png": MediaImportResult("image_block", image),
            },
        )

        rewritten = assembled[0]["value"]
        self.assertIn(failed.value, rewritten)
        self.assertEqual(rewritten.count('embedtype="image"'), 1)
        self.assertIn("| [导入缺失：图片", rewritten)
        self.assertIn("| <embed", rewritten)


class FakeRevision:
    def __init__(self, pointer="mongo-pointer"):
        self.content = {"mongo_draft_pointer": pointer}
        self.id = 88


class FakeDraftPage:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.pk = None
        self.live = True
        self.published = False

    def save_revision(self, *, user, log_action):
        self.revision_user = user
        self.revision_log_action = log_action
        return FakeRevision()

    def publish(self):
        self.published = True


class FakeParent:
    def __init__(self):
        self.added_page = None

    def add_child(self, *, instance):
        self.added_page = instance
        instance.pk = 123


class MarkdownImportDraftLifecycleTests(SimpleTestCase):
    def test_creates_unpublished_page_and_revision_without_publish(self):
        parent = FakeParent()

        result = create_unpublished_blog_draft(
            parent,
            title="导入文章",
            date="2026-08-17",
            intro="简介",
            body_values=[{"type": "markdown_block", "value": "正文"}],
            user=SimpleNamespace(pk=7),
            page_factory=FakeDraftPage,
        )

        self.assertIs(result.page, parent.added_page)
        self.assertEqual(result.page.pk, 123)
        self.assertFalse(result.page.live)
        self.assertFalse(result.page.published)
        self.assertEqual(result.revision.id, 88)
        self.assertEqual(result.mongo_draft_pointer, "mongo-pointer")
        self.assertEqual(result.page.revision_log_action, "wagtail.create")

    @mock.patch("wagtail.models.Page.save")
    @mock.patch("blog.models.MongoManager")
    @mock.patch("wagtail.models.pages.ContentType.objects.get_for_model")
    def test_blog_page_draft_only_save_does_not_write_formal_mongo_content(
        self, get_for_model, mongo_manager, page_save
    ):
        from blog.models import BlogPage

        from django.contrib.contenttypes.models import ContentType

        get_for_model.return_value = ContentType(pk=1)
        page = BlogPage(
            title="导入文章",
            date=date(2026, 8, 17),
            intro="简介",
            body=[],
        )
        page.pk = 123
        page._markdown_import_draft_only = True

        with mock.patch("blog.models.BlogPage.objects") as page_manager:
            page.save()

        mongo_manager.assert_not_called()
        page_save.assert_called_once()
        page_manager.filter.assert_not_called()

    def test_compensation_deletes_only_new_page_pointer_and_media_artifacts(self):
        calls = []
        media_artifacts = [SimpleNamespace(artifact_id="a1"), SimpleNamespace(artifact_id="a2")]

        result = compensate_draft_failure(
            page=SimpleNamespace(pk=123),
            mongo_draft_pointer="mongo-pointer",
            media_artifacts=media_artifacts,
            delete_page=lambda page: calls.append(("page", page.pk)),
            delete_mongo_pointer=lambda pointer: calls.append(("mongo", pointer)),
            cleanup_media=lambda artifact: calls.append(("media", artifact.artifact_id)) or True,
        )

        self.assertTrue(result.cleaned)
        self.assertEqual(
            calls,
            [("page", 123), ("mongo", "mongo-pointer"), ("media", "a1"), ("media", "a2")],
        )

    def test_page_delete_failure_blocks_pointer_and_media_cleanup(self):
        calls = []

        result = compensate_draft_failure(
            page=SimpleNamespace(pk=123),
            mongo_draft_pointer="mongo-pointer",
            media_artifacts=[SimpleNamespace(artifact_id="a1")],
            delete_page=lambda page: (_ for _ in ()).throw(RuntimeError("page")),
            delete_mongo_pointer=lambda pointer: calls.append(("mongo", pointer)),
            cleanup_media=lambda artifact: calls.append(("media", artifact.artifact_id)) or True,
        )

        self.assertFalse(result.cleaned)
        self.assertIn("page_delete_failed", result.errors)
        self.assertIn("compensation_dependency_blocked", result.errors)
        self.assertEqual(calls, [])
