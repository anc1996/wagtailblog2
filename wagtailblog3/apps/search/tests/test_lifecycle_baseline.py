"""WP0：验证博客正式正文、草稿和 Wagtail 生命周期的当前行为。"""

from copy import deepcopy
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.db import connection
from django.db.models.signals import pre_delete
from django.test import TestCase, TransactionTestCase
from wagtail.models import Locale, Page, PageViewRestriction
from wagtail.signals import page_published, page_unpublished

from blog.models import BlogIndexPage, BlogPage
from search.core import ESLazyResults


class _LiveCollection:
    """模拟新页面保存后回填 Mongo page_id 的最小集合接口。"""

    def __init__(self, manager):
        self.manager = manager

    def update_one(self, selector, update):
        document = self.manager.live_documents.get(selector.get("_id"))
        if document is not None:
            document.update(update.get("$set", {}))


class InMemoryMongoManager:
    """测试用 Mongo 适配器，分离正式正文与 Revision 草稿且不访问外部数据库。"""

    def __init__(self):
        self.live_documents = {}
        self.revision_documents = {}
        self.revision_reads = []
        self.live_sequence = 0
        self.revision_sequence = 0
        self.blog_content = _LiveCollection(self)

    @classmethod
    def _copy_for_storage(cls, value):
        """模拟 Mongo 网关把 RawDataView 归一化为可持久化的普通值。"""
        if isinstance(value, dict):
            return {key: cls._copy_for_storage(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._copy_for_storage(item) for item in value]
        if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
            return [cls._copy_for_storage(item) for item in value]
        return deepcopy(value)

    def save_blog_content(self, content_data, content_id=None):
        if content_id is None:
            self.live_sequence += 1
            content_id = f"live-{self.live_sequence}"
        self.live_documents[content_id] = self._copy_for_storage(content_data)
        return content_id

    def get_blog_content(self, content_id):
        document = self.live_documents.get(content_id)
        return deepcopy(document) if document is not None else None

    def save_blog_revision_body(self, page_id, body_data):
        self.revision_sequence += 1
        pointer = f"revision-{self.revision_sequence}"
        self.revision_documents[pointer] = {
            "page_id": page_id,
            "body": self._copy_for_storage(body_data),
        }
        return pointer

    def get_blog_revision_body(self, pointer):
        self.revision_reads.append(pointer)
        document = self.revision_documents.get(pointer)
        return deepcopy(document) if document is not None else None

    def delete_blog_content(self, content_id):
        return self.live_documents.pop(content_id, None) is not None

    def delete_page_revisions(self, page_id):
        matching = [
            pointer
            for pointer, document in self.revision_documents.items()
            if document["page_id"] == page_id
        ]
        for pointer in matching:
            del self.revision_documents[pointer]
        return len(matching)

    def delete_single_revision(self, pointer):
        return self.revision_documents.pop(pointer, None) is not None


class BlogLifecycleFixtureMixin:
    """使用 Wagtail Revision API 验证公开正文不会读取 Mongo 草稿集合。"""

    def setUp(self):
        super().setUp()
        self.mongo = InMemoryMongoManager()
        self.mongo_patches = [
            patch("blog.models.MongoManager", return_value=self.mongo),
            patch("blog.signals.MongoManager", return_value=self.mongo),
        ]
        for mongo_patch in self.mongo_patches:
            mongo_patch.start()
            self.addCleanup(mongo_patch.stop)

        root = Page.get_first_root_node()
        if root is None:
            Locale.objects.get_or_create(language_code=settings.LANGUAGE_CODE)
            root = Page(title="WP0 测试根节点", slug="wp0-test-root")
            Page.add_root(instance=root)
        self.index = BlogIndexPage(title="WP0 测试索引", slug="wp0-test-index")
        root.add_child(instance=self.index)

    def _create_draft_page(self, body_text):
        page_number = BlogPage.objects.count() + 1
        page = BlogPage(
            title="WP0 生命周期文章",
            slug=f"wp0-lifecycle-article-{page_number}",
            date=date(2026, 8, 10),
            intro="<p>用于 WP0 的合成简介</p>",
            body=self._markdown_body(body_text),
            live=False,
            has_unpublished_changes=True,
        )
        self.index.add_child(instance=page)
        return page

    @staticmethod
    def _markdown_body(body_text):
        """构造与 Mongo 正文和 Wagtail Revision 一致的 StreamField 原始块。"""
        return [
            {
                "type": "markdown_block",
                "value": body_text,
                "id": str(uuid4()),
            }
        ]

    def _publish(self, page):
        revision = page.save_revision()
        revision_pointer = revision.content["mongo_draft_pointer"]
        revision_body = self.mongo.revision_documents[revision_pointer]["body"]
        self.assertEqual(
            len(page._hydrate_streamfield_from_mongo(revision_body)),
            1,
        )
        revision_object = revision.as_object()
        self.assertIsInstance(revision_object, BlogPage)
        self.assertIn(revision_pointer, self.mongo.revision_reads)
        self.assertEqual(
            len(revision_object.body),
            1,
            {
                "revision_content": revision.content,
                "revision_documents": self.mongo.revision_documents,
            },
        )
        self.assertEqual(
            revision_object.body_text,
            page.body_text,
            {
                "revision_content": revision.content,
                "revision_documents": self.mongo.revision_documents,
            },
        )
        revision.publish()
        page.refresh_from_db()
        return page


class BlogLifecycleBaselineTests(BlogLifecycleFixtureMixin, TestCase):
    """验证正文、草稿和公开 QuerySet 的当前行为。"""

    def test_draft_revision_does_not_replace_published_mongo_body_until_publish(self):
        page = self._publish(self._create_draft_page("正式正文关键字"))
        live_content_id = page.mongo_content_id
        self.assertEqual(
            page.body_text,
            "正式正文关键字",
            {"mongo_content_id": live_content_id, "live_documents": self.mongo.live_documents},
        )

        page.body = self._markdown_body("草稿专属关键字")
        draft_revision = page.save_revision()

        self.assertEqual(page.mongo_content_id, live_content_id)
        self.assertEqual(page.body_text, "正式正文关键字")
        self.assertNotIn("草稿专属关键字", str(draft_revision.content))
        draft_pointer = draft_revision.content["mongo_draft_pointer"]
        self.assertIn("草稿专属关键字", str(self.mongo.revision_documents[draft_pointer]))
        self.assertNotIn("草稿专属关键字", page.body_text)
        self.assertEqual(len(self.mongo.revision_documents), 2)

    def test_republishing_revision_replaces_the_formal_mongo_body(self):
        page = self._publish(self._create_draft_page("第一版正式正文"))
        page.body = self._markdown_body("第二版正式正文")
        revision = page.save_revision()

        revision.publish()
        page.refresh_from_db()

        self.assertTrue(page.live)
        self.assertIn(
            "第二版正式正文",
            page.body_text,
            {"mongo_content_id": page.mongo_content_id, "live_documents": self.mongo.live_documents},
        )
        self.assertNotIn("第一版正式正文", page.body_text)

    def test_unpublished_and_restricted_pages_are_excluded_by_wagtail_public_queryset(self):
        unpublished_page = self._create_draft_page("未发布正文")
        self.assertFalse(unpublished_page.live)
        self.assertFalse(BlogPage.objects.live().public().filter(pk=unpublished_page.pk).exists())

        published_page = self._publish(self._create_draft_page("公开正文"))
        published_page.unpublish()
        published_page.refresh_from_db()

        self.assertFalse(published_page.live)
        self.assertFalse(BlogPage.objects.live().public().filter(pk=published_page.pk).exists())

        restricted_page = self._publish(
            self._create_draft_page("受限正文")
        )
        PageViewRestriction.objects.create(
            page=restricted_page,
            restriction_type=PageViewRestriction.PASSWORD,
            password="test-only-password",
        )

        self.assertTrue(BlogPage.objects.live().filter(pk=restricted_page.pk).exists())
        self.assertFalse(BlogPage.objects.live().public().filter(pk=restricted_page.pk).exists())

    def test_raw_es_result_adapter_currently_fetches_pages_without_public_queryset_guard(self):
        class FakeElasticsearch:
            def search(self, **kwargs):
                return {"hits": {"hits": [{"_id": "wagtailcore_page:123"}]}}

        results = ESLazyResults(
            FakeElasticsearch(), "test-index", {"match_all": {}}, []
        )

        with patch("search.core.Page.objects.live") as live_pages:
            public_pages = live_pages.return_value.public.return_value
            public_pages.filter.return_value.specific.return_value.order_by.return_value = []
            self.assertEqual(results._fetch_slice(0, 1), [])

        live_pages.assert_called_once_with()
        public_pages.filter.assert_called_once_with(pk__in=[123])


class BlogLifecycleSignalBaselineTests(BlogLifecycleFixtureMixin, TransactionTestCase):
    """记录 Wagtail 生命周期信号的顺序和事务状态。"""

    def test_publish_unpublish_and_delete_signals_capture_current_transaction_boundaries(self):
        events = []

        def record_event(name):
            def receiver(sender, instance, **kwargs):
                events.append(
                    {
                        "name": name,
                        "page_id": instance.pk,
                        "live": instance.live,
                        "in_atomic_block": connection.in_atomic_block,
                    }
                )

            return receiver

        published_receiver = record_event("published")
        unpublished_receiver = record_event("unpublished")
        deleted_receiver = record_event("deleted")
        page_published.connect(published_receiver, sender=BlogPage, weak=False)
        page_unpublished.connect(unpublished_receiver, sender=BlogPage, weak=False)
        pre_delete.connect(deleted_receiver, sender=BlogPage, weak=False)
        self.addCleanup(page_published.disconnect, published_receiver, BlogPage)
        self.addCleanup(page_unpublished.disconnect, unpublished_receiver, BlogPage)
        self.addCleanup(pre_delete.disconnect, deleted_receiver, BlogPage)

        page = self._create_draft_page("信号正文")
        revision = page.save_revision()
        revision.publish()
        page.refresh_from_db()
        page.unpublish()
        page.delete()

        self.assertEqual([event["name"] for event in events], ["published", "unpublished", "deleted"])
        self.assertEqual([event["live"] for event in events], [True, False, False])
        # WP3B 将发布、取消发布和删除统一置于外层事务，确保 State/Outbox 与页面状态原子提交。
        self.assertEqual([event["in_atomic_block"] for event in events], [True, True, True])
