"""P-03 正式快照与目标编码契约；不需要数据库的定向测试。"""

from copy import deepcopy
from datetime import datetime, timezone
import importlib
import importlib.util
import unittest


def snapshot_data() -> dict:
    """手工固定的 Blog 正式快照，不含后来草稿或用户属性。"""
    return {
        "snapshot_schema_version": 1,
        "snapshot_kind": "blog_v1",
        "page_id": 7,
        "model_label": "blog.blogpage",
        "search_type": "blog",
        "publication_generation": 3,
        "publication_ref": {
            "published_revision_id": 19,
            "source_page_id": None,
            "source_revision_id": None,
        },
        "published_metadata": {
            "title": "正式标题",
            "summary": "正式摘要",
            "first_published_at": "2026-10-08T01:00:00+00:00",
            "intro": "正式摘要",
            "date": "2026-10-01",
            "tag_ids": [2, 9],
            "category_ids": [4],
        },
        "body_ref": {
            "mongo_content_id": "legacy-id",
            "body_version_id": "body-v7",
            "body_sha256": "a" * 64,
            "body_schema_version": 1,
        },
        "projection_schema_version": 2,
    }


class PublishedSnapshotTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(
            importlib.util.find_spec("search.services.published_snapshot"),
            "P-03 缺少严格 PublishedSnapshotV1 值对象",
        )
        return importlib.import_module("search.services.published_snapshot")

    def test_snapshot_is_deeply_immutable_and_digest_covers_every_component(self):
        module = self.module()
        data = snapshot_data()
        snapshot = module.PublishedSnapshotV1.from_dict(data)
        data["published_metadata"]["title"] = "草稿"
        exported = snapshot.to_dict()
        exported["published_metadata"]["tag_ids"].append(100)
        self.assertEqual(snapshot.to_dict()["published_metadata"]["title"], "正式标题")
        self.assertEqual(snapshot.to_dict()["published_metadata"]["tag_ids"], [2, 9])
        for section, key, value in [
            (None, "page_id", 8),
            (None, "publication_generation", 4),
            (None, "search_type", "archive"),
            ("publication_ref", "published_revision_id", 20),
            ("published_metadata", "title", "新正式标题"),
            ("published_metadata", "first_published_at", "2026-10-08T02:00:00+00:00"),
            ("published_metadata", "tag_ids", [3]),
            ("body_ref", "body_version_id", "body-v8"),
        ]:
            changed = snapshot_data()
            (changed if section is None else changed[section])[key] = value
            self.assertNotEqual(snapshot.digest, module.PublishedSnapshotV1.from_dict(changed).digest)

    def test_unknown_schema_partial_identity_and_bad_digest_fail_closed(self):
        module = self.module()
        for key, value in [
            ("snapshot_schema_version", True), ("snapshot_schema_version", 2),
            ("projection_schema_version", 1), ("snapshot_kind", "future_v3"),
            ("page_id", False),
        ]:
            data = snapshot_data()
            data[key] = value
            with self.assertRaises(module.PublishedSnapshotError):
                module.PublishedSnapshotV1.from_dict(data)
        for section, key in [
            ("publication_ref", "published_revision_id"),
            ("published_metadata", "first_published_at"),
            ("body_ref", "body_sha256"),
        ]:
            data = snapshot_data()
            del data[section][key]
            with self.assertRaises(module.PublishedSnapshotError):
                module.PublishedSnapshotV1.from_dict(data)
        with self.assertRaises(module.PublishedSnapshotError):
            module.PublishedSnapshotV1.from_dict(snapshot_data(), expected_digest="0" * 64)

    def test_no_revision_without_complete_alias_source(self):
        module = self.module()
        data = snapshot_data()
        data["publication_ref"]["published_revision_id"] = None
        with self.assertRaises(module.PublishedSnapshotError):
            module.PublishedSnapshotV1.from_dict(data)
        data["publication_ref"].update(source_page_id=12, source_revision_id=51)
        snapshot = module.PublishedSnapshotV1.from_dict(data)
        self.assertEqual(snapshot.to_dict()["publication_ref"]["source_revision_id"], 51)

    def test_schema_null_remains_legacy_and_unknown_versions_rejected(self):
        module = self.module()
        self.assertEqual(module.effective_projection_schema_version(None), 1)
        self.assertEqual(module.effective_projection_schema_version(2), 2)
        for bad in (True, 0, 3, "2"):
            with self.assertRaises(module.PublishedSnapshotError):
                module.effective_projection_schema_version(bad)

    def test_state_payload_cannot_use_numeric_equality_to_bypass_digest(self):
        from types import SimpleNamespace
        module = self.module()
        snapshot = module.PublishedSnapshotV1.from_dict(snapshot_data())
        data = snapshot.to_dict()
        state = SimpleNamespace(
            **{key: data[key] for key in (
                "page_id", "model_label", "search_type", "snapshot_kind",
                "publication_generation", "projection_schema_version",
            )},
            snapshot_digest=snapshot.digest, published_snapshot=deepcopy(data),
        )
        snapshot.validate_state(state)
        state.published_snapshot["publication_generation"] = 3.0
        with self.assertRaises(module.PublishedSnapshotError):
            snapshot.validate_state(state)


class PublishedAdapterTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from wagtail import blocks
        from wagtail.fields import StreamField
        from external_content.registry import ContentStoragePolicy, FieldStorageConfig
        from external_content.codecs.streamfield import StreamFieldV1Codec
        from external_content.store import ContentVersionStore, compute_canonical_hash

        nested = blocks.StructBlock([
            ("question", blocks.CharBlock()),
            ("answers", blocks.ListBlock(blocks.TextBlock())),
        ])
        self.field = StreamField([
            ("markdown_block", blocks.TextBlock()),
            ("faq", nested),
            ("rich", blocks.RichTextBlock()),
        ], use_json_field=True)
        self.page = SimpleNamespace(
            pk=7, alias_of_id=None,
            _meta=SimpleNamespace(label_lower="sample.dualpage", get_field=lambda name: self.field),
            title="后来草稿", summary="草稿摘要",
        )
        self.policy = ContentStoragePolicy(
            "sample.dualpage", 1,
            {name: FieldStorageConfig(name, "mongo", StreamFieldV1Codec()) for name in ("article", "appendix")},
        )
        self.payloads = {
            "article": [
                {"type": "markdown_block", "id": "a", "value": "# 原始 Markdown\n" + "长文本 " * 20000 + "尾部独有中文"},
                {"type": "faq", "id": "q", "value": {
                    "question": "问题中文", "answers": [{"type": "item", "id": "l", "value": "答案中文"}],
                }},
            ],
            "appendix": [{"type": "rich", "id": "b", "value": "<p>附录中文 needle-end</p>"}],
        }
        self.docs = {}
        self.manifest = {
            "protocol_version": 1, "content_storage_schema": 1,
            "model_label": "sample.dualpage", "page_id": 7, "fields": {},
        }
        for name, payload in self.payloads.items():
            identity = {
                "model_label": "sample.dualpage", "page_id": 7, "field_name": name,
                "codec": "streamfield.v1", "schema_version": 1,
                "payload_sha256": compute_canonical_hash(payload),
            }
            self.docs[name] = {"version_id": name, **identity, "payload": payload}
            self.manifest["fields"][name] = {
                key: self.docs[name][key] for key in ("codec", "schema_version", "version_id", "payload_sha256")
            }
        docs = self.docs
        class ReadOnlyCollection:
            def with_options(self, **options):
                return self

            def find_one(self, query):
                return deepcopy(docs.get(query["version_id"]))
        self.store = ContentVersionStore(collection=ReadOnlyCollection())
        self.revision = SimpleNamespace(
            pk=19, object_id="7",
            content_type=SimpleNamespace(app_label="sample", model="dualpage"),
            content={
                "title": "正式标题", "summary": "正式摘要",
                "_external_content_manifest": deepcopy(self.manifest),
            },
        )
        self.published_at = datetime(2026, 10, 8, 1, tzinfo=timezone.utc)

    def adapter(self):
        from unittest.mock import patch
        self.assertIsNotNone(importlib.util.find_spec("search.services.published_adapters"))
        from search.services.published_adapters import ExternalPublishedAdapter
        patcher = patch("search.services.published_adapters.get_policy_for_version", return_value=self.policy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ExternalPublishedAdapter(model=type(self.page), store=self.store)

    def capture(self, adapter=None):
        return (adapter or self.adapter()).capture(
            self.page, revision=self.revision, manifest=self.manifest,
            publication_generation=3, first_published_at=self.published_at,
            search_type="pages",
        )

    def test_full_dual_field_nested_chinese_and_raw_markdown_preserved(self):
        original = deepcopy(self.payloads)
        adapter = self.adapter()
        snapshot = self.capture(adapter)
        text = adapter.extract_text(snapshot)
        self.assertGreater(len(text), 80000)
        for word in ("尾部独有中文", "问题中文", "答案中文", "附录中文 needle-end"):
            self.assertIn(word, text)
        self.assertIn("答案中文\n\n附录中文", text)
        self.assertEqual(self.payloads, original)
        self.assertEqual(snapshot.to_dict()["published_metadata"]["title"], "正式标题")
        self.assertEqual(set(snapshot.to_dict()["body_ref"]["fields"]), {"article", "appendix"})

    def test_body_corruption_missing_version_identity_and_unknown_block_fail_closed(self):
        from search.services.published_snapshot import PublishedSnapshotError
        adapter = self.adapter()
        snapshot = self.capture(adapter)
        original = deepcopy(self.docs)
        mutations = [
            lambda: self.docs.pop("appendix"),
            lambda: self.docs["article"].update(page_id=8),
            lambda: self.docs["article"]["payload"][0].update(value="篡改"),
            lambda: self.docs["article"]["payload"][0].update(type="unknown"),
        ]
        for mutation in mutations:
            self.docs.clear()
            self.docs.update(deepcopy(original))
            mutation()
            with self.assertRaises(PublishedSnapshotError):
                adapter.extract_text(snapshot)

    def test_revision_pointer_mismatch_and_partial_manifest_rejected(self):
        from search.services.published_snapshot import PublishedSnapshotError
        adapter = self.adapter()
        for action in (
            lambda: setattr(self.revision, "object_id", "8"),
            lambda: self.revision.content["_external_content_manifest"]["fields"]["article"].update(version_id="other"),
            lambda: self.manifest["fields"].pop("appendix"),
        ):
            with self.subTest(action=action):
                saved_revision, saved_manifest = deepcopy(self.revision), deepcopy(self.manifest)
                action()
                with self.assertRaises(PublishedSnapshotError):
                    self.capture(adapter)
                self.revision, self.manifest = saved_revision, saved_manifest

    def test_external_target_v1_refused_and_hash_target_independent(self):
        from search.services.document import build_published_content_document
        from search.services.published_snapshot import PublishedSnapshotError
        adapter = self.adapter()
        snapshot = self.capture(adapter)
        document = build_published_content_document(
            snapshot, 11, scope={"locale_id": 2, "site_ids": [1], "ancestor_ids": [1, 3]},
            adapter=adapter,
        )
        self.assertEqual(document.document["title"], "正式标题")
        self.assertEqual(document.document["sort_date"], "2026-10-08T01:00:00+00:00")
        self.assertEqual(document.document["content_version"], 11)
        self.assertNotIn("article", document.document)
        with self.assertRaises(PublishedSnapshotError):
            build_published_content_document(snapshot, 11, target_schema_version=1, adapter=adapter)

    def test_unknown_nested_block_with_valid_hash_is_rejected_and_empty_is_legal(self):
        from external_content.store import compute_canonical_hash
        from search.services.published_snapshot import PublishedSnapshotError
        adapter = self.adapter()
        self.docs["article"]["payload"] = [{
            "type": "faq", "id": "q", "value": {
                "question": "问题", "answers": [], "unknown_child": "不能丢弃",
            },
        }]
        digest = compute_canonical_hash(self.docs["article"]["payload"])
        self.docs["article"]["payload_sha256"] = digest
        self.manifest["fields"]["article"]["payload_sha256"] = digest
        self.revision.content["_external_content_manifest"] = deepcopy(self.manifest)
        with self.assertRaises(PublishedSnapshotError):
            adapter.extract_text(self.capture(adapter))
        for name in self.docs:
            self.docs[name]["payload"] = []
            digest = compute_canonical_hash([])
            self.docs[name]["payload_sha256"] = digest
            self.manifest["fields"][name]["payload_sha256"] = digest
        self.revision.content["_external_content_manifest"] = deepcopy(self.manifest)
        self.assertEqual(adapter.extract_text(self.capture(adapter)), "")

    def test_single_field_clear_retains_other_field_and_unknown_codec_refused(self):
        from external_content.store import compute_canonical_hash
        from search.services.published_snapshot import PublishedSnapshotError
        self.docs["article"]["payload"] = []
        digest = compute_canonical_hash([])
        self.docs["article"]["payload_sha256"] = digest
        self.manifest["fields"]["article"]["payload_sha256"] = digest
        self.revision.content["_external_content_manifest"] = deepcopy(self.manifest)
        adapter = self.adapter()
        self.assertIn("附录中文 needle-end", adapter.extract_text(self.capture(adapter)))
        self.assertNotIn("尾部独有中文", adapter.extract_text(self.capture(adapter)))
        self.manifest["fields"]["article"]["codec"] = "unknown.v1"
        self.revision.content["_external_content_manifest"] = deepcopy(self.manifest)
        with self.assertRaises(PublishedSnapshotError):
            self.capture(adapter)

    def test_adapter_checks_returned_store_identity_hash_and_version_itself(self):
        from search.services.published_snapshot import PublishedSnapshotError
        adapter = self.adapter()
        snapshot = self.capture(adapter)
        docs = self.docs
        class UncheckedStore:
            def get_version(self, version_id, expected_identity):
                return deepcopy(docs[version_id])
        adapter.store = UncheckedStore()
        self.assertIn("尾部独有中文", adapter.extract_text(snapshot))
        original = deepcopy(self.docs["article"])
        for mutate in (
            lambda: self.docs["article"].update(version_id="wrong"),
            lambda: self.docs["article"].update(page_id=8),
            lambda: self.docs["article"].update(schema_version=True),
            lambda: self.docs["article"]["payload"][0].update(value="未经校验正文"),
        ):
            self.docs["article"] = deepcopy(original)
            mutate()
            with self.assertRaises(PublishedSnapshotError):
                adapter.extract_text(snapshot)

    def test_revision_serialized_page_identity_cannot_disagree(self):
        from search.services.published_snapshot import PublishedSnapshotError
        self.revision.content["pk"] = 8
        with self.assertRaises(PublishedSnapshotError):
            self.capture()

    def test_declared_custom_unknown_text_block_is_not_silently_ignored(self):
        from wagtail import blocks
        from wagtail.fields import StreamField
        from external_content.store import compute_canonical_hash
        from search.services.published_snapshot import PublishedSnapshotError
        self.field = StreamField([("custom", blocks.Block())], use_json_field=True)
        for name in self.docs:
            payload = [{"type": "custom", "id": "c", "value": "未知自定义正文"}]
            self.docs[name]["payload"] = payload
            digest = compute_canonical_hash(payload)
            self.docs[name]["payload_sha256"] = digest
            self.manifest["fields"][name]["payload_sha256"] = digest
        self.revision.content["_external_content_manifest"] = deepcopy(self.manifest)
        adapter = self.adapter()
        with self.assertRaises(PublishedSnapshotError):
            adapter.extract_text(self.capture(adapter))

    def test_nested_stream_and_list_text_is_complete(self):
        from wagtail import blocks
        from wagtail.fields import StreamField
        from external_content.store import compute_canonical_hash
        self.field = StreamField([
            ("sections", blocks.ListBlock(blocks.StreamBlock([
                ("text", blocks.TextBlock()), ("markdown_block", blocks.TextBlock()),
            ]))),
        ], use_json_field=True)
        payload = [{
            "type": "sections", "id": "s", "value": [{
                "type": "item", "id": "i", "value": [
                    {"type": "text", "id": "t", "value": "嵌套正文"},
                    {"type": "markdown_block", "id": "m", "value": "**嵌套尾词**"},
                ],
            }],
        }]
        for name in self.docs:
            self.docs[name]["payload"] = deepcopy(payload)
            digest = compute_canonical_hash(payload)
            self.docs[name]["payload_sha256"] = digest
            self.manifest["fields"][name]["payload_sha256"] = digest
        self.revision.content["_external_content_manifest"] = deepcopy(self.manifest)
        adapter = self.adapter()
        text = adapter.extract_text(self.capture(adapter))
        self.assertEqual(text.count("嵌套正文"), 2)
        self.assertEqual(text.count("嵌套尾词"), 2)

    def test_real_model_revision_and_policy_identity_must_agree(self):
        from types import SimpleNamespace
        from search.services.published_snapshot import PublishedSnapshotError
        adapter = self.adapter()
        self.revision.content_type.model = "otherpage"
        with self.assertRaises(PublishedSnapshotError):
            self.capture(adapter)
        self.revision.content_type.model = "dualpage"
        self.policy.external_fields.pop("appendix")
        with self.assertRaises(PublishedSnapshotError):
            self.capture(adapter)
        wrong_model = type("OtherModel", (), {
            "_meta": SimpleNamespace(label_lower="sample.otherpage"),
        })
        from search.services.published_adapters import ExternalPublishedAdapter
        with self.assertRaises(PublishedSnapshotError):
            self.capture(ExternalPublishedAdapter(wrong_model, store=self.store))

    def test_alias_requires_matching_source_revision_and_owner(self):
        from search.services.published_snapshot import PublishedSnapshotError
        adapter = self.adapter()
        self.page.pk = 8
        self.page.alias_of_id = 7
        kwargs = dict(
            revision=self.revision, manifest=self.manifest, publication_generation=3,
            first_published_at=self.published_at, search_type="pages",
            source_page_id=7, source_revision_id=19,
        )
        snapshot = adapter.capture(self.page, **kwargs)
        self.assertEqual(snapshot.page_id, 8)
        self.assertIsNone(snapshot.to_dict()["publication_ref"]["published_revision_id"])
        self.assertIn("尾部独有中文", adapter.extract_text(snapshot))
        kwargs["source_revision_id"] = 20
        with self.assertRaises(PublishedSnapshotError):
            adapter.capture(self.page, **kwargs)


class GuidePublishedAdapterTests(unittest.TestCase):
    def test_real_guide_schema_projects_all_seven_blocks_and_text_choices(self):
        """捕获跳过正文选择文本或任一指南块的回归；仅 Mongo collection 使用替身。"""
        from types import SimpleNamespace
        from guides.models import GuidePage
        from external_content.store import ContentVersionStore, compute_canonical_hash
        from search.services.published_adapters import ExternalPublishedAdapter

        payload = [
            {"type": "lead", "id": "l", "value": "<p>导读独有词</p>"},
            {"type": "step", "id": "s", "value": {
                "title": "步骤独有词", "description": "<p>说明独有词</p>", "image": None,
            }},
            {"type": "code", "id": "c", "value": {
                "language": "python", "code": "print('代码独有词')", "caption": "文件独有词",
            }},
            {"type": "tip", "id": "t", "value": {
                "tip_type": "warning", "content": "<p>提示独有词</p>",
            }},
            {"type": "faq", "id": "q", "value": {
                "question": "问题独有词", "answer": "<p>回答独有词</p>",
            }},
            {"type": "reference", "id": "r", "value": {
                "title": "资料独有词", "url": "https://example.invalid/reference-needle",
                "source": "来源独有词",
            }},
            {"type": "markdown_block", "id": "m", "value": "**末尾独有词**"},
        ]
        original = deepcopy(payload)
        digest = compute_canonical_hash(payload)
        entry = {
            "codec": "streamfield.v1", "schema_version": 1,
            "version_id": "guide-body", "payload_sha256": digest,
        }
        manifest = {
            "protocol_version": 1, "content_storage_schema": 1,
            "model_label": "guides.guidepage", "page_id": 7, "fields": {"body": entry},
        }
        stored = {
            **entry, "model_label": "guides.guidepage", "page_id": 7,
            "field_name": "body", "payload": payload,
        }

        class ReadOnlyCollection:
            def with_options(self, **options):
                return self

            def find_one(self, query):
                if query["version_id"] != "guide-body":
                    raise AssertionError("wrong_guide_version")
                return deepcopy(stored)

        page = SimpleNamespace(
            pk=7, alias_of_id=None, _meta=GuidePage._meta, title="草稿标题", summary="草稿摘要",
        )
        revision = SimpleNamespace(
            pk=19, object_id="7",
            content_type=SimpleNamespace(app_label="guides", model="guidepage"),
            content={
                "pk": 7, "title": "正式指南", "summary": "正式摘要",
                "_external_content_manifest": deepcopy(manifest),
            },
        )
        adapter = ExternalPublishedAdapter(
            GuidePage, store=ContentVersionStore(collection=ReadOnlyCollection()),
        )
        snapshot = adapter.capture(
            page, revision, manifest, 3, datetime(2026, 10, 8, 1, tzinfo=timezone.utc), "guides",
        )
        text = adapter.extract_text(snapshot)
        for word in (
            "导读独有词", "步骤独有词", "说明独有词", "代码独有词", "文件独有词",
            "提示独有词", "问题独有词", "回答独有词", "资料独有词",
            "reference-needle", "来源独有词", "末尾独有词", "python", "warning",
        ):
            self.assertIn(word, text)
        self.assertNotIn("<p>", text)
        self.assertNotIn("草稿", text)
        self.assertEqual(payload, original)


class BlogPublishedAdapterTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        from blog.models import BlogPage
        from search.services.published_snapshot import canonical_digest
        self.model = BlogPage
        self.page = SimpleNamespace(pk=7, _meta=BlogPage._meta, alias_of_id=None,
                                    title="后来草稿", intro="草稿", mongo_content_id="draft-id")
        self.body = [{"type": "markdown_block", "id": "m", "value": "# 正式正文 中文尾词"}]
        self.body_hash = canonical_digest(self.body)
        self.document = {
            "aggregate_type": "blog_page", "aggregate_id": "7",
            "body_version_id": "body-v7", "body_sha256": self.body_hash,
            "body_schema_version": 1, "body": self.body,
        }
        self.revision = SimpleNamespace(
            pk=19, object_id="7",
            content_type=SimpleNamespace(app_label="blog", model="blogpage"),
            content={
                "title": "正式标题", "intro": "<p>正式摘要</p>", "date": "2026-10-01",
                "categories": [4], "tagged_items": [{"pk": 88, "tag": 2}, {"pk": 89, "tag": 9}],
                "mongo_content_id": "legacy-id", "mongo_body_version_id": "body-v7",
                "body_sha256": self.body_hash, "body_schema_version": 1,
            },
        )

    def adapter(self):
        from search.services.published_adapters import BlogPublishedAdapter
        document = self.document
        class ReadOnlyBlogStore:
            def get_content_body_version(self, aggregate_type, aggregate_id, version, digest, schema):
                if (aggregate_type, aggregate_id, version, digest, schema) != (
                    "blog_page", 7, "body-v7", self_hash, 1,
                ):
                    raise AssertionError("wrong_blog_read_identity")
                return deepcopy(document)
        self_hash = self.body_hash
        return BlogPublishedAdapter(self.model, store=ReadOnlyBlogStore())

    def capture(self, adapter):
        return adapter.capture(
            self.page, revision=self.revision, publication_generation=3,
            first_published_at=datetime(2026, 10, 8, 1, tzinfo=timezone.utc), search_type="blog",
        )

    def test_blog_uses_revision_metadata_and_immutable_body_not_current_draft(self):
        adapter = self.adapter()
        snapshot = self.capture(adapter)
        self.assertEqual(snapshot.to_dict()["published_metadata"], {
            "title": "正式标题", "summary": "正式摘要", "intro": "正式摘要",
            "date": "2026-10-01", "tag_ids": [2, 9], "category_ids": [4],
            "first_published_at": "2026-10-08T01:00:00+00:00",
        })
        self.assertEqual(snapshot.to_dict()["body_ref"]["mongo_content_id"], "legacy-id")
        self.assertIn("中文尾词", adapter.extract_text(snapshot))
        self.assertNotIn("草稿", adapter.extract_text(snapshot))

    def test_blog_missing_pointer_wrong_revision_and_corrupted_body_fail_closed(self):
        from search.services.published_snapshot import PublishedSnapshotError
        adapter = self.adapter()
        snapshot = self.capture(adapter)
        self.document["body"][0]["value"] = "损坏"
        with self.assertRaises(PublishedSnapshotError):
            adapter.extract_text(snapshot)
        self.document["body"] = self.body = []
        self.document["body_sha256"] = "0" * 64
        with self.assertRaises(PublishedSnapshotError):
            adapter.extract_text(snapshot)
        del self.revision.content["mongo_body_version_id"]
        with self.assertRaises(PublishedSnapshotError):
            self.capture(adapter)
        self.revision.object_id = "8"
        with self.assertRaises(PublishedSnapshotError):
            self.capture(adapter)

    def test_factory_routes_snapshot_and_model_but_rejects_native_page(self):
        from search.services.published_adapters import (
            BlogPublishedAdapter, ExternalPublishedAdapter, get_published_adapter,
        )
        from search.services.published_snapshot import PublishedSnapshotError, PublishedSnapshotV1
        from guides.models import GuidePage, GuideIndexPage
        self.assertIsInstance(get_published_adapter(self.model), BlogPublishedAdapter)
        self.assertIsInstance(get_published_adapter(PublishedSnapshotV1.from_dict(snapshot_data())), BlogPublishedAdapter)
        self.assertIsInstance(get_published_adapter(GuidePage), ExternalPublishedAdapter)
        with self.assertRaises(PublishedSnapshotError):
            get_published_adapter(GuideIndexPage)

    def test_blog_legal_empty_legacy_ids_and_unknown_block_keep_separate_contract(self):
        from search.services.published_snapshot import PublishedSnapshotError, canonical_digest
        # Blog 历史块不强制要求通用 codec 的稳定 id。
        self.body[0].pop("id")
        self.body_hash = canonical_digest(self.body)
        self.document["body_sha256"] = self.body_hash
        self.revision.content["body_sha256"] = self.body_hash
        adapter = self.adapter()
        self.assertIn("中文尾词", adapter.extract_text(self.capture(adapter)))
        self.body[:] = []
        self.body_hash = canonical_digest(self.body)
        self.document["body_sha256"] = self.body_hash
        self.revision.content["body_sha256"] = self.body_hash
        adapter = self.adapter()
        self.assertEqual(adapter.extract_text(self.capture(adapter)), "")
        self.body.append({"type": "unknown", "value": "不能漏掉"})
        self.body_hash = canonical_digest(self.body)
        self.document["body_sha256"] = self.body_hash
        self.revision.content["body_sha256"] = self.body_hash
        adapter = self.adapter()
        with self.assertRaises(PublishedSnapshotError):
            adapter.extract_text(self.capture(adapter))

    def test_blog_missing_business_metadata_does_not_fill_from_draft_or_empty(self):
        from search.services.published_snapshot import PublishedSnapshotError
        original = deepcopy(self.revision.content)
        for name in ("intro", "date", "categories", "tagged_items"):
            self.revision.content = deepcopy(original)
            del self.revision.content[name]
            with self.assertRaises(PublishedSnapshotError):
                self.capture(self.adapter())

    def test_default_blog_read_does_not_initialize_or_write_mongo_manager(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from search.services.published_adapters import BlogPublishedAdapter
        document = deepcopy(self.document)
        class ReadOnlyCollection:
            def find_one(self, query):
                if query != {key: document[key] for key in (
                    "aggregate_type", "aggregate_id", "body_version_id",
                    "body_sha256", "body_schema_version",
                )}:
                    raise AssertionError("wrong_blog_identity")
                return deepcopy(document)
        class ReadOnlyClient:
            def __getitem__(self, name):
                return SimpleNamespace(get_collection=lambda *args, **kwargs: ReadOnlyCollection())
        adapter = BlogPublishedAdapter(self.model)
        with (
            patch("external_content.store.get_mongo_client", return_value=ReadOnlyClient()),
            patch("wagtailblog3.mongo.MongoManager._connect", side_effect=AssertionError("forbidden_init")),
            patch("wagtailblog3.mongo.MongoManager._ensure_indexes", side_effect=AssertionError("forbidden_write")),
        ):
            self.assertIn("中文尾词", adapter.extract_text(self.capture(adapter)))


class ProjectionSchemaTests(unittest.TestCase):
    def test_blog_v1_v2_share_logical_hash_and_fixed_business_metadata(self):
        from search.services.document import build_published_content_document
        from search.services.published_snapshot import PublishedSnapshotV1
        snapshot = PublishedSnapshotV1.from_dict(snapshot_data())
        class FixedAdapter:
            def extract_text(self, value):
                return "正式正文 中文"
        kwargs = {"scope": {"locale_id": 1, "site_ids": [4], "ancestor_ids": [1]}, "adapter": FixedAdapter()}
        v1 = build_published_content_document(snapshot, 23, target_schema_version=1, **kwargs)
        v2 = build_published_content_document(snapshot, 23, target_schema_version=2, **kwargs)
        self.assertEqual(v1.content_hash, v2.content_hash)
        self.assertEqual(v1.document["intro"], "正式摘要")
        self.assertEqual(v1.document["date"], "2026-10-01")
        self.assertEqual(v1.document["tag_ids"], [2, 9])
        self.assertEqual(v1.document["body_version_id"], "body-v7")
        self.assertEqual(v2.document["summary"], "正式摘要")
        self.assertEqual(v1.document["page_id"], v2.document["page_id"])
        self.assertEqual(v1.document["content_version"], v2.document["content_version"])
        self.assertNotIn("snapshot_digest", v1.document)
        self.assertNotIn("intro", v2.document)
        self.assertNotIn("body_ref", v2.document)

    def test_exact_target_whitelists_and_scope_hash_are_stable(self):
        from search.services.document import build_published_content_document
        from search.services.published_snapshot import PublishedSnapshotV1
        snapshot = PublishedSnapshotV1.from_dict(snapshot_data())
        class FixedAdapter:
            def extract_text(self, value):
                return ""
        scope = {"locale_id": 1, "site_ids": [4, 4], "ancestor_ids": [3, 1]}
        original = deepcopy(scope)
        v2 = build_published_content_document(snapshot, 23, scope=scope, adapter=FixedAdapter())
        self.assertEqual(set(v2.document), {
            "page_id", "locale_id", "site_ids", "ancestor_ids", "model_label", "search_type",
            "snapshot_kind", "title", "summary", "body_text", "first_published_at", "sort_date",
            "content_version", "publication_generation", "projection_schema_version",
            "snapshot_digest", "content_hash", "searchable", "is_deleted",
        })
        v1 = build_published_content_document(snapshot, 23, scope=scope,
                                              target_schema_version=1, adapter=FixedAdapter())
        self.assertEqual(set(v1.document), {
            "page_id", "content_version", "content_hash", "searchable", "operation",
            "title", "intro", "body_text", "date", "first_published_at", "locale_id",
            "tag_ids", "category_ids", "body_version_id", "publication_generation",
        })
        self.assertEqual(scope, original)
        reordered = build_published_content_document(
            snapshot, 24, scope={"locale_id": 1, "site_ids": [4], "ancestor_ids": [1, 3]},
            adapter=FixedAdapter(),
        )
        self.assertEqual(v2.content_hash, reordered.content_hash)
        changed = build_published_content_document(
            snapshot, 24, scope={"locale_id": 2, "site_ids": [4], "ancestor_ids": [1, 3]},
            adapter=FixedAdapter(),
        )
        self.assertNotEqual(v2.content_hash, changed.content_hash)

    def test_invalid_document_inputs_refused_before_body_read(self):
        from search.services.document import build_published_content_document
        from search.services.published_snapshot import PublishedSnapshotV1, PublishedSnapshotError
        snapshot = PublishedSnapshotV1.from_dict(snapshot_data())
        class NoBodyRead:
            def extract_text(self, value):
                raise AssertionError("invalid_input_must_not_read_body")
        cases = [
            {"content_version": True}, {"content_version": 0},
            {"target_schema_version": True}, {"target_schema_version": 3},
            {"target_schema_version": None}, {"scope": {"locale_id": True}},
            {"scope": {"site_ids": [True]}}, {"scope": {"ancestor_ids": [0]}},
            {"scope": {"searchable": 1}}, {"scope": {"is_deleted": "false"}},
            {"scope": {"private_attribute": "not_indexed"}},
        ]
        for changes in cases:
            kwargs = {"content_version": 1, "adapter": NoBodyRead(), **changes}
            with self.subTest(changes=changes), self.assertRaises(PublishedSnapshotError):
                build_published_content_document(snapshot, **kwargs)

    def test_additive_model_defaults_keep_legacy_rows_null(self):
        from search.models import ContentSearchState, ContentSearchOutbox, ContentSearchTarget
        for model in (ContentSearchState, ContentSearchOutbox):
            value = model()
            for name in ("model_label", "search_type", "snapshot_kind", "published_snapshot", "snapshot_digest", "projection_schema_version"):
                self.assertIsNone(getattr(value, name))
                self.assertTrue(model._meta.get_field(name).null)
            self.assertIsNotNone(model._meta.get_field("mongo_content_id"))
            self.assertIsNotNone(model._meta.get_field("body_version_id"))
        self.assertEqual(ContentSearchTarget().projection_schema_version, 1)
