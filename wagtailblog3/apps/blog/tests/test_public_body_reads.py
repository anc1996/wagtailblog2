"""验证公开 BlogPage 读取正式 Mongo 正文版本时的指针和草稿隔离。"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from blog.models import BlogPage, BlogPublicationState


class BlogPublicBodyReadTests(TestCase):
    """固定正式正文版本、旧正文兼容和草稿不可泄露三个公开读取边界。"""

    @staticmethod
    def _body(text: str) -> list[dict[str, str]]:
        """构造最小 Mongo StreamField 正文，避免测试依赖真实 Mongo。"""
        return [{"type": "rich_text", "value": text, "id": "public-body-block"}]

    @staticmethod
    def _page(page_id: int = 38001) -> BlogPage:
        """构造只参与正文读取的页面对象，不保存 Wagtail 页面树。"""
        return BlogPage(pk=page_id, mongo_content_id="legacy-formal-38001")

    def test_public_read_uses_published_state_body_version(self):
        """有正式指针时必须按完整身份读取版本，不得回退到旧正文。"""
        page = self._page()
        BlogPublicationState.objects.create(
            page_id=page.pk,
            published_body_version_id="body-version-38001",
            published_body_sha256="a" * 64,
            published_body_schema_version=1,
            publication_generation=3,
        )
        manager = MagicMock()
        manager.get_content_body_version.return_value = {
            "body": self._body("正式版本正文"),
        }

        with patch("blog.models.MongoManager", return_value=manager):
            content = page.get_content_from_mongodb()

        self.assertEqual(content["body"][0]["value"], "正式版本正文")
        manager.get_content_body_version.assert_called_once_with(
            "blog_page",
            page.pk,
            "body-version-38001",
            "a" * 64,
            1,
        )
        manager.get_blog_content.assert_not_called()

    def test_missing_state_keeps_legacy_formal_content_fallback(self):
        """尚未登记的旧页面继续读取正式正文，避免兼容期公开内容中断。"""
        page = self._page(38002)
        manager = MagicMock()
        manager.get_blog_content.return_value = {
            "body": self._body("旧正式正文"),
        }

        with patch("blog.models.MongoManager", return_value=manager):
            content = page.get_content_from_mongodb()

        self.assertEqual(content["body"][0]["value"], "旧正式正文")
        manager.get_blog_content.assert_called_once_with(page.mongo_content_id)
        manager.get_content_body_version.assert_not_called()

    def test_public_read_does_not_leak_legacy_revision_draft(self):
        """正式指针存在时，即使旧接口含草稿秘密，也不得把草稿带入公开读取。"""
        page = self._page(38003)
        BlogPublicationState.objects.create(
            page_id=page.pk,
            published_body_version_id="body-version-38003",
            published_body_sha256="b" * 64,
            published_body_schema_version=1,
            publication_generation=1,
        )
        manager = MagicMock()
        manager.get_content_body_version.return_value = {
            "body": self._body("正式正文"),
        }
        manager.get_blog_content.return_value = {
            "body": self._body("草稿秘密-不得公开"),
        }

        with patch("blog.models.MongoManager", return_value=manager):
            content = page.get_content_from_mongodb()

        self.assertNotIn("草稿秘密", str(content))
        self.assertEqual(content["body"][0]["value"], "正式正文")
        manager.get_blog_content.assert_not_called()
