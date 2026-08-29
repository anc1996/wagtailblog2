"""WP0：验证博客正式正文、草稿和 Wagtail 生命周期的当前行为。"""

from copy import deepcopy
from datetime import date
import hashlib
import json
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.signals import pre_delete
from django.test import TestCase, TransactionTestCase
from wagtail.models import Locale, Page, PageViewRestriction
from wagtail.signals import page_published, page_unpublished
from modelsearch.index import get_indexed_models

from blog.models import (
    BlogIndexPage,
    BlogPage,
    BlogPageForm,
    BlogRevisionBodyUnavailableError,
)
from wagtailblog3.mongo import MongoBodyVersionBodyError, MongoRevisionNotFoundError


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
        self.body_versions = {}
        self.revision_reads = []
        self.body_version_reads = []
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

    @classmethod
    def _body_sha256(cls, body_data):
        """按生产仓储相同规则计算正文摘要，保证测试替身的幂等键一致。"""
        canonical = json.dumps(
            cls._copy_for_storage(body_data),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def save_content_body_version(
        self, aggregate_type, aggregate_id, body_data, *, body_schema_version=1
    ):
        """模拟不可变正文版本的 insert-once 语义，不访问真实 MongoDB。

        同一聚合、模式版本和正文哈希只生成一个版本；正文变化则插入新版本，
        既有版本不会被更新。返回值与 ``MongoManager`` 的版本身份契约一致。
        """
        prepared_body = self._copy_for_storage(body_data)
        body_sha256 = self._body_sha256(prepared_body)
        key = (str(aggregate_type), str(aggregate_id), body_sha256, body_schema_version)
        existing = self.body_versions.get(key)
        if existing is not None:
            return deepcopy(existing["identity"])
        identity = {
            "body_version_id": f"body-version-{len(self.body_versions) + 1}",
            "body_sha256": body_sha256,
            "body_schema_version": body_schema_version,
        }
        self.body_versions[key] = {
            "aggregate_type": str(aggregate_type),
            "aggregate_id": str(aggregate_id),
            "body_schema_version": body_schema_version,
            "body": prepared_body,
            "identity": identity,
        }
        return deepcopy(identity)

    def get_content_body_version(
        self, aggregate_type, aggregate_id, body_version_id, body_sha256, body_schema_version
    ):
        """按完整聚合身份读取不可变正文，并校验版本 ID、模式和哈希。"""
        self.body_version_reads.append(body_version_id)
        for document in self.body_versions.values():
            if (
                document["aggregate_type"] == str(aggregate_type)
                and document["aggregate_id"] == str(aggregate_id)
                and document["identity"]["body_version_id"] == body_version_id
            ):
                if (
                    document["identity"]["body_sha256"] != body_sha256
                    or document["identity"]["body_schema_version"] != body_schema_version
                ):
                    raise MongoBodyVersionBodyError("正文版本身份校验失败")
                result = deepcopy(document)
                result.pop("identity", None)
                result["body_version_id"] = body_version_id
                result["body_sha256"] = body_sha256
                result["body_schema_version"] = body_schema_version
                return result
        raise MongoRevisionNotFoundError("MongoDB 中不存在对应不可变正文版本")

    def get_blog_revision_body(self, pointer):
        self.revision_reads.append(pointer)
        document = self.revision_documents.get(pointer)
        if document is None:
            raise MongoRevisionNotFoundError("MongoDB 中不存在对应历史正文快照")
        return deepcopy(document)

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
        body_version_id = revision.content["mongo_body_version_id"]
        self.assertTrue(
            any(
                item["identity"]["body_version_id"] == body_version_id
                for item in self.mongo.body_versions.values()
            )
        )
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

    def test_missing_revision_snapshot_never_uses_current_live_body(self):
        """Wagtail ``Revision.as_object`` 必须因缺失快照失败，不能伪造历史内容。"""
        page = self._create_draft_page("草稿历史正文")
        revision = page.save_revision()
        pointer = revision.content["mongo_draft_pointer"]
        del self.mongo.revision_documents[pointer]
        body_version_id = revision.content["mongo_body_version_id"]
        self.mongo.body_versions = {
            key: value
            for key, value in self.mongo.body_versions.items()
            if value["identity"]["body_version_id"] != body_version_id
        }

        with self.assertRaises(BlogRevisionBodyUnavailableError):
            revision.as_object()

    def test_revision_snapshot_must_belong_to_its_page(self):
        """跨页面指针不能把另一篇文章正文注入当前 Revision。"""
        page = self._create_draft_page("本页历史正文")
        revision = page.save_revision()
        pointer = revision.content["mongo_draft_pointer"]
        self.mongo.revision_documents[pointer]["page_id"] = page.pk + 1
        body_version_id = revision.content["mongo_body_version_id"]
        for value in self.mongo.body_versions.values():
            if value["identity"]["body_version_id"] == body_version_id:
                value["aggregate_id"] = str(page.pk + 1)

        with self.assertRaises(BlogRevisionBodyUnavailableError):
            revision.as_object()

    def test_admin_form_rejects_empty_latest_revision_pointer(self):
        """编辑表单不能把空指针草稿替换为当前正式正文后再保存。"""
        page = self._create_draft_page("待编辑的草稿正文")
        revision = page.save_revision()
        revision_content = dict(revision.content)
        revision_content["mongo_draft_pointer"] = ""
        revision_content.pop("mongo_body_version_id")
        revision_content.pop("body_sha256")
        revision_content.pop("body_schema_version")
        revision.content = revision_content
        revision.save(update_fields=["content"])
        page.refresh_from_db()

        with self.assertRaises(BlogRevisionBodyUnavailableError):
            BlogPageForm(instance=page)

    def test_admin_form_prefers_new_body_version_over_broken_legacy_pointer(self):
        """兼容期旧指针损坏时，后台表单仍必须读取同一 Revision 的新版本。"""
        page = self._create_draft_page("不可变正文版本")
        revision = page.save_revision()
        revision_content = dict(revision.content)
        revision_content["mongo_draft_pointer"] = "missing-legacy-pointer"
        revision.content = revision_content
        revision.save(update_fields=["content"])
        page.refresh_from_db()

        form_class = page.get_edit_handler().get_form_class()
        form = form_class(instance=page)

        self.assertEqual(form.instance.body_text, "不可变正文版本")

    def test_admin_form_rejects_cross_page_latest_revision_pointer(self):
        """编辑表单必须执行与历史预览相同的正文归属校验。"""
        page = self._create_draft_page("本页草稿正文")
        revision = page.save_revision()
        pointer = revision.content["mongo_draft_pointer"]
        self.mongo.revision_documents[pointer]["page_id"] = page.pk + 1
        body_version_id = revision.content["mongo_body_version_id"]
        for value in self.mongo.body_versions.values():
            if value["identity"]["body_version_id"] == body_version_id:
                value["aggregate_id"] = str(page.pk + 1)
        page.refresh_from_db()

        with self.assertRaises(BlogRevisionBodyUnavailableError) as error:
            BlogPageForm(instance=page)

        self.assertEqual(error.exception.code, "revision_snapshot_missing")

    def test_latest_revision_without_pointer_keeps_its_empty_mysql_body(self):
        """无指针的空 Revision 不能被当前正式 Mongo 正文覆盖。"""
        page = self._create_draft_page("当前正式正文")
        revision = page.save_revision()
        revision_content = dict(revision.content)
        revision_content.pop("mongo_draft_pointer")
        revision_content.pop("mongo_body_version_id")
        revision_content.pop("body_sha256")
        revision_content.pop("body_schema_version")
        revision_content["body"] = "[]"
        revision.content = revision_content
        revision.save(update_fields=["content"])
        page.refresh_from_db()

        revision_object = page.get_latest_revision_as_object()

        self.assertEqual(len(revision_object.body), 0)

    def test_admin_form_without_pointer_keeps_latest_empty_mysql_body(self):
        """后台表单不能把无指针空 Revision 替换为当前正式正文。"""
        page = self._create_draft_page("当前正式正文")
        revision = page.save_revision()
        revision_content = dict(revision.content)
        revision_content.pop("mongo_draft_pointer")
        revision_content.pop("mongo_body_version_id")
        revision_content.pop("body_sha256")
        revision_content.pop("body_schema_version")
        revision_content["body"] = "[]"
        revision.content = revision_content
        revision.save(update_fields=["content"])
        page.refresh_from_db()

        form_class = page.get_edit_handler().get_form_class()
        form = form_class(instance=page)

        self.assertEqual(len(form.instance.body), 0)

    def test_authenticated_admin_blocks_preview_compare_and_revert_before_writing(self):
        """真实后台请求遇到缺失快照时返回 409，恢复 POST 不得新增 Revision。"""
        page = self._create_draft_page("第一版历史正文")
        first_revision = page.save_revision()
        page.body = self._markdown_body("第二版历史正文")
        second_revision = page.save_revision()
        first_pointer = first_revision.content["mongo_draft_pointer"]
        del self.mongo.revision_documents[first_pointer]
        first_body_version_id = first_revision.content["mongo_body_version_id"]
        self.mongo.body_versions = {
            key: value
            for key, value in self.mongo.body_versions.items()
            if value["identity"]["body_version_id"] != first_body_version_id
        }

        user = get_user_model().objects.create_superuser(
            username=f"revision-body-admin-{page.pk}",
            email=f"revision-body-admin-{page.pk}@example.test",
            password="test-password",
        )
        self.client.force_login(user)
        preview_url = f"/admin/pages/{page.pk}/revisions/{first_revision.pk}/view/"
        compare_url = (
            f"/admin/pages/{page.pk}/revisions/compare/"
            f"{first_revision.pk}...{second_revision.pk}/"
        )
        revert_url = f"/admin/pages/{page.pk}/revisions/{first_revision.pk}/revert/"
        revision_count = page.revisions.count()

        # 测试环境没有收集后的 manifest；本测试改用开发静态存储，不执行 collectstatic。
        test_storages = {
            **settings.STORAGES,
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
            },
        }
        with self.settings(STORAGES=test_storages):
            preview_response = self.client.get(preview_url)
            compare_response = self.client.get(compare_url)
            revert_response = self.client.post(revert_url)

        self.assertEqual(preview_response.status_code, 409)
        self.assertEqual(compare_response.status_code, 409)
        self.assertEqual(revert_response.status_code, 409)
        self.assertEqual(page.revisions.count(), revision_count)

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

    def test_default_page_rebuild_queryset_includes_blog_pages(self):
        page = self._publish(self._create_draft_page("默认 Page 索引生命周期"))

        self.assertIn(Page, get_indexed_models())
        self.assertIn(BlogPage, get_indexed_models())
        self.assertTrue(BlogPage.get_indexed_objects().filter(pk=page.pk).exists())


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
