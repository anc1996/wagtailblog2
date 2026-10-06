"""
Wagtail Page 正文外置通用协议 - ExternalContentCoordinator 多字段协调器定向测试。

验证：
1. 专用测试 Page (MultiFieldTestPage) 拥有两个外置 StreamField 与一个原生 MySQL 字段；
2. prepare_external_content_for_revision 与 load_external_content_from_manifest 全流程无损往返；
3. 未持久化页面 (pk is None) 阻断外置正文准备；
4. 未配置策略页面阻断外置正文准备；
5. 未在基线中固化的模型准备正文时严格失败关闭（拒绝运行时自动登记）；
6. 不完整 Manifest（遗漏任一已登记外置字段指针）必须失败关闭 (ManifestValidationError)；
7. Manifest 包含未知多余字段指针必须失败关闭 (ManifestValidationError)；
8. 字段 Codec 标识或 Codec schema_version 不匹配必须失败关闭 (ManifestValidationError)；
9. content_storage_schema 不匹配或历史未知版本必须失败关闭；
10. 跨页与跨模型指针读取被严格拦截 (ManifestIdentityMismatchError)；
11. Mongo 存储损坏或篡改时阻断还原 (CorruptedContentPayloadError)；
12. 普通 serializable_data() 调用绝不产生 MongoDB 写入副作用。
"""

import json
import logging
import os
import tempfile
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec, patch
from django.conf import settings
from django.db import models
from django.test import SimpleTestCase, TestCase
from wagtail import blocks
from wagtail.fields import StreamField, StreamValue
from wagtail.models import Page

from external_content.codecs.streamfield import StreamFieldV1Codec
from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import (
    CodecPayloadError,
    ContentStoreError,
    CorruptedContentPayloadError,
    FieldStorageConfigError,
    ManifestIdentityMismatchError,
    ManifestValidationError,
)
from external_content.manifest import ExternalContentManifest, FieldManifestEntry
from external_content.mixins import ExternalContentPageMixin
from external_content.registry import (
    ContentStoragePolicy,
    FieldStorageConfig,
    get_policy_for_version,
    get_storage_policy_for_model,
    reset_active_baseline_file,
    set_active_baseline_file,
)
from external_content.store import ContentVersionStore, ensure_indexes, get_mongo_client


class MultiFieldTestPage(ExternalContentPageMixin, Page):
    """用于验收通用内核多字段与原生字段混合场景的测试 Page。"""
    native_intro = models.CharField(max_length=255, default="")
    primary_body = StreamField(
        [
            ("heading", blocks.CharBlock()),
            ("paragraph", blocks.TextBlock()),
        ],
        use_json_field=True,
    )
    secondary_body = StreamField(
        [
            ("quote", blocks.BlockQuoteBlock()),
        ],
        use_json_field=True,
    )

    content_storage = {
        "primary_body": "mongo",
        "secondary_body": "mongo",
    }
    content_storage_schema = 1

    class Meta:
        app_label = "external_content_test_coord"


class ExternalContentCoordinatorTests(TestCase):
    """ExternalContentCoordinator 多字段生命周期协调器定向测试。"""

    def setUp(self) -> None:
        self.temp_baseline = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.temp_baseline.close()
        with open(self.temp_baseline.name, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "policies": {}}, f)
        set_active_baseline_file(self.temp_baseline.name)

        # 显式备案 MultiFieldTestPage 策略基线
        get_storage_policy_for_model(MultiFieldTestPage, allow_record_new=True)

        self.mongo_client = get_mongo_client()
        db_name = getattr(settings, "MONGO_DB", {}).get("NAME", "wagtailblog_test")
        self.coll_name = f"test_coord_{uuid.uuid4().hex[:12]}"
        self.collection = self.mongo_client[db_name][self.coll_name]
        ensure_indexes(self.collection)
        self.store = ContentVersionStore(collection=self.collection)
        self.coordinator = ExternalContentCoordinator(store=self.store)

    def tearDown(self) -> None:
        reset_active_baseline_file()
        if os.path.exists(self.temp_baseline.name):
            os.remove(self.temp_baseline.name)
        lock_file = self.temp_baseline.name + ".lock"
        if os.path.exists(lock_file):
            os.remove(lock_file)
        self.collection.drop()

    def test_multi_field_roundtrip_prepare_and_load(self) -> None:
        """测试双外置 StreamField 与一原生 MySQL 字段的准备与加载无损往返。"""
        page = MultiFieldTestPage(
            title="多字段测试指南",
            native_intro="原生 MySQL 存储的导读摘要",
        )
        page.pk = 101
        page.primary_body = [
            ("heading", "主要核心主题"),
            ("paragraph", "详细的展开段落内容"),
        ]
        page.secondary_body = [
            ("quote", "重要的参考名言引语"),
        ]

        # 1. 显式准备不可变 Revision 正文版本
        manifest = self.coordinator.prepare_external_content_for_revision(page)

        # 核对 Manifest 属性
        self.assertEqual(manifest.protocol_version, 1)
        self.assertEqual(manifest.content_storage_schema, 1)
        self.assertEqual(manifest.model_label, "external_content_test_coord.multifieldtestpage")
        self.assertEqual(manifest.page_id, 101)
        self.assertIn("primary_body", manifest.fields)
        self.assertIn("secondary_body", manifest.fields)
        self.assertNotIn("native_intro", manifest.fields)

        # 检查 MongoDB 中已持久化 2 条字段版本
        self.assertEqual(self.collection.count_documents({}), 2)

        # 2. 从 Manifest 解码还原至全新的 Page 实例
        restored_page = MultiFieldTestPage(
            title="恢复后的目标页面",
            native_intro="未被修改的原生导读",
        )
        restored_page.pk = 101
        self.coordinator.load_external_content_from_manifest(restored_page, manifest)

        # 验证 StreamValue 运行时类型与正文无损还原
        self.assertIsInstance(restored_page.primary_body, StreamValue)
        self.assertEqual(len(restored_page.primary_body), 2)
        self.assertEqual(restored_page.primary_body[0].block_type, "heading")
        self.assertEqual(restored_page.primary_body[0].value, "主要核心主题")
        self.assertEqual(restored_page.primary_body[1].block_type, "paragraph")
        self.assertEqual(restored_page.primary_body[1].value, "详细的展开段落内容")

        self.assertIsInstance(restored_page.secondary_body, StreamValue)
        self.assertEqual(len(restored_page.secondary_body), 1)
        self.assertEqual(restored_page.secondary_body[0].block_type, "quote")
        self.assertEqual(restored_page.secondary_body[0].value, "重要的参考名言引语")

        # 原生 MySQL 字段完全不受外置流程干扰
        self.assertEqual(restored_page.native_intro, "未被修改的原生导读")

    def test_unrecorded_model_prepare_external_content_rejected(self) -> None:
        """未在策略基线中固化的模型准备外置正文时被严格阻断（只读门禁）。"""
        class UnrecordedCoordPage(ExternalContentPageMixin, Page):
            body = StreamField([("heading", blocks.CharBlock())], use_json_field=True)
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "test_coord_unrec"

        unrec = UnrecordedCoordPage(title="未备案页面")
        unrec.pk = 777
        with self.assertRaises(FieldStorageConfigError) as ctx:
            self.coordinator.prepare_external_content_for_revision(unrec)
        self.assertIn("尚未在策略基线中固化", str(ctx.exception))

    def test_incomplete_manifest_missing_field_rejected(self) -> None:
        """测试不完整 Manifest（遗漏任一策略登记的外置字段指针）时必须失败关闭。"""
        page = MultiFieldTestPage(title="测试页")
        page.pk = 101
        page.primary_body = [("heading", "主标题")]
        page.secondary_body = [("quote", "引语")]

        full_manifest = self.coordinator.prepare_external_content_for_revision(page)

        # 构造遗漏 secondary_body 的不完整 Manifest
        incomplete_fields = {
            "primary_body": full_manifest.fields["primary_body"],
        }
        incomplete_manifest = ExternalContentManifest(
            protocol_version=1,
            content_storage_schema=1,
            model_label=full_manifest.model_label,
            page_id=full_manifest.page_id,
            fields=incomplete_fields,
        )

        with self.assertRaises(ManifestValidationError) as ctx:
            self.coordinator.load_external_content_from_manifest(page, incomplete_manifest)
        self.assertIn("不完整指针拒绝加载", str(ctx.exception))

    def test_manifest_mismatched_codec_rejected(self) -> None:
        """Manifest 中字段指定的 codec 与模型策略声明不一致时失败关闭。"""
        page = MultiFieldTestPage(title="测试页")
        page.pk = 101
        page.primary_body = [("heading", "主标题")]
        page.secondary_body = [("quote", "引语")]
        full_manifest = self.coordinator.prepare_external_content_for_revision(page)

        bad_fields = dict(full_manifest.fields)
        orig = bad_fields["primary_body"]
        bad_fields["primary_body"] = FieldManifestEntry(
            codec="raw.v1",
            schema_version=orig.schema_version,
            version_id=orig.version_id,
            payload_sha256=orig.payload_sha256,
        )
        bad_manifest = ExternalContentManifest(
            protocol_version=1,
            content_storage_schema=1,
            model_label=full_manifest.model_label,
            page_id=full_manifest.page_id,
            fields=bad_fields,
        )

        with self.assertRaises(ManifestValidationError) as ctx:
            self.coordinator.load_external_content_from_manifest(page, bad_manifest)
        self.assertIn("与策略声明 ('streamfield.v1') 不一致", str(ctx.exception))

    def test_manifest_mismatched_codec_schema_version_rejected(self) -> None:
        """Manifest 中字段指定的 schema_version 与策略声明不一致时失败关闭。"""
        page = MultiFieldTestPage(title="测试页")
        page.pk = 101
        page.primary_body = [("heading", "主标题")]
        page.secondary_body = [("quote", "引语")]
        full_manifest = self.coordinator.prepare_external_content_for_revision(page)

        bad_fields = dict(full_manifest.fields)
        orig = bad_fields["primary_body"]
        bad_fields["primary_body"] = FieldManifestEntry(
            codec=orig.codec,
            schema_version=99,
            version_id=orig.version_id,
            payload_sha256=orig.payload_sha256,
        )
        bad_manifest = ExternalContentManifest(
            protocol_version=1,
            content_storage_schema=1,
            model_label=full_manifest.model_label,
            page_id=full_manifest.page_id,
            fields=bad_fields,
        )

        with self.assertRaises(ManifestValidationError) as ctx:
            self.coordinator.load_external_content_from_manifest(page, bad_manifest)
        self.assertIn("schema_version (99) 与策略声明", str(ctx.exception))

    def test_unpersisted_page_raises_error(self) -> None:
        """未持久化获取主键 ID 的页面调用准备正文抛出 FieldStorageConfigError。"""
        page = MultiFieldTestPage(title="未保存页面")
        page.pk = None
        with self.assertRaises(FieldStorageConfigError):
            self.coordinator.prepare_external_content_for_revision(page)

    def test_unconfigured_model_raises_error(self) -> None:
        """未配置外置策略的模型调用抛出 FieldStorageConfigError。"""
        class PlainPage(Page):
            class Meta:
                app_label = "external_content_test_plain"

        plain = PlainPage(title="普通原生页面")
        plain.pk = 202
        with self.assertRaises(FieldStorageConfigError):
            self.coordinator.prepare_external_content_for_revision(plain)

    def test_cross_page_pointer_load_rejected(self) -> None:
        """尝试使用其他页面的 Manifest 加载正文时抛出 ManifestIdentityMismatchError。"""
        page = MultiFieldTestPage(title="源页面")
        page.pk = 101
        page.primary_body = [("heading", "源页面正文")]
        page.secondary_body = [("quote", "源页面引用")]

        manifest = self.coordinator.prepare_external_content_for_revision(page)

        # 尝试跨页读取至页面 999
        other_page = MultiFieldTestPage(title="被攻击的目标页面")
        other_page.pk = 999
        other_page.primary_body = [("heading", "目标页面原正文")]
        other_page.secondary_body = [("quote", "目标页面原引用")]

        with self.assertNumQueries(0), \
             patch(
                 "external_content.copying.validate_alias_manifest_source",
                 side_effect=AssertionError("普通跨页不得尝试解析 Alias 来源"),
             ), \
             patch.object(
                 self.store, "get_version",
                 side_effect=AssertionError("身份拒绝前不得读取正文"),
             ), \
             self.assertRaises(ManifestIdentityMismatchError):
            self.coordinator.load_external_content_from_manifest(other_page, manifest)
        self.assertEqual(other_page.primary_body[0].value, "目标页面原正文")
        self.assertEqual(other_page.secondary_body[0].value, "目标页面原引用")

    def test_cross_model_pointer_load_rejected(self) -> None:
        """尝试使用其他模型类型的 Manifest 加载正文时抛出 ManifestIdentityMismatchError。"""
        page = MultiFieldTestPage(title="源页面")
        page.pk = 101
        page.primary_body = [("heading", "源页面正文")]
        page.secondary_body = [("quote", "源页面引用")]

        manifest = self.coordinator.prepare_external_content_for_revision(page)

        class OtherModelPage(ExternalContentPageMixin, Page):
            primary_body = StreamField([("heading", blocks.CharBlock())], use_json_field=True)
            content_storage = {"primary_body": "mongo"}
            class Meta:
                app_label = "external_content_other_model"

        get_storage_policy_for_model(OtherModelPage, allow_record_new=True)

        other_model_page = OtherModelPage(title="不同模型页面")
        other_model_page.pk = 101

        with self.assertRaises(ManifestIdentityMismatchError):
            self.coordinator.load_external_content_from_manifest(other_model_page, manifest)

    def test_manifest_with_unknown_field_rejected(self) -> None:
        """Manifest 中包含页面策略未声明的字段时抛出 ManifestValidationError。"""
        page = MultiFieldTestPage(title="测试页")
        page.pk = 101
        page.primary_body = [("heading", "主标题")]
        page.secondary_body = [("quote", "引用")]
        manifest = self.coordinator.prepare_external_content_for_revision(page)

        # 注入未声明字段
        tampered_fields = dict(manifest.fields)
        tampered_fields["unregistered_field"] = FieldManifestEntry(
            codec="streamfield.v1",
            schema_version=1,
            version_id="ver-fake",
            payload_sha256="0" * 64,
        )
        tampered_manifest = ExternalContentManifest(
            protocol_version=1,
            content_storage_schema=1,
            model_label=manifest.model_label,
            page_id=manifest.page_id,
            fields=tampered_fields,
        )

        with self.assertRaises(ManifestValidationError):
            self.coordinator.load_external_content_from_manifest(page, tampered_manifest)

    def test_corrupted_payload_in_mongo_blocks_load(self) -> None:
        """MongoDB 中存储正文损坏或被外部恶意篡改时阻断加载。"""
        page = MultiFieldTestPage(title="测试页")
        page.pk = 101
        page.primary_body = [("heading", "正版标题")]
        page.secondary_body = [("quote", "正版引用")]
        manifest = self.coordinator.prepare_external_content_for_revision(page)

        primary_vid = manifest.fields["primary_body"].version_id
        self.collection.update_one(
            {"version_id": primary_vid},
            {"$set": {"payload": [{"id": "b1", "type": "heading", "value": "被篡改的标题"}]}},
        )

        with self.assertRaises(CorruptedContentPayloadError):
            self.coordinator.load_external_content_from_manifest(page, manifest)

    def test_serializable_data_has_no_mongo_write_side_effect(self) -> None:
        """验证普通 serializable_data() 纯只读且零 Mongo 写入，保留原生字段与属性。"""
        page = MultiFieldTestPage(
            title="普通序列化页面",
            native_intro="原生导读内容",
        )
        page.pk = 505
        page.primary_body = [("heading", "未显式准备版本")]
        page.secondary_body = [("quote", "普通引用")]

        initial_count = self.collection.count_documents({})

        data = page.serializable_data()
        self.assertIsInstance(data, dict)

        final_count = self.collection.count_documents({})
        self.assertEqual(initial_count, final_count)

        # 原生 MySQL 字段与基本属性完整保留
        self.assertEqual(data["native_intro"], "原生导读内容")
        self.assertEqual(data["title"], "普通序列化页面")

    def test_uncoordinated_save_revision_is_blocked_in_g1(self) -> None:
        """验证外置页面未具备持久化主键时，save_revision 被严格阻断，防止生成孤儿版本。"""
        page = MultiFieldTestPage(
            title="阻断保存页",
            native_intro="原生导读",
        )
        with self.assertRaises(RuntimeError) as ctx:
            page.save_revision()
        self.assertIn("主键", str(ctx.exception))
    def test_multi_field_load_atomic_failure_leaves_page_intact(self) -> None:
        """测试多字段加载原子性：若第二字段正文损坏，第一字段与整个页面属性绝不被部分修改。"""
        page = MultiFieldTestPage(
            title="原子性测试页",
            native_intro="初始原生导读",
        )
        page.pk = 101
        page.primary_body = [("heading", "初始主要正文")]
        page.secondary_body = [("quote", "初始引用正文")]

        # 生成初始 Manifest 并写入 Mongo
        manifest = self.coordinator.prepare_external_content_for_revision(page)

        # 恶意篡改 MongoDB 中 secondary_body 的正文
        sec_vid = manifest.fields["secondary_body"].version_id
        self.collection.update_one(
            {"version_id": sec_vid},
            {"$set": {"payload": [{"id": "bad-block", "type": "quote", "value": "被篡改的正文"}]}},
        )

        target_page = MultiFieldTestPage(
            title="目标待恢复页面",
            native_intro="未被修改的导读",
        )
        target_page.pk = 101
        # 设置 target_page 的初始值
        initial_primary = [("heading", "目标页原始主内容")]
        initial_sec = [("quote", "目标页原始引用")]
        target_page.primary_body = initial_primary
        target_page.secondary_body = initial_sec

        # 尝试还原，必须抛出 CorruptedContentPayloadError
        with self.assertRaises(CorruptedContentPayloadError):
            self.coordinator.load_external_content_from_manifest(target_page, manifest)

        # 核心断言：原子性失败关闭，target_page 的 primary_body 绝不能被部分修改为 manifest 中的内容
        self.assertEqual(len(target_page.primary_body), 1)
        self.assertEqual(target_page.primary_body[0].value, "目标页原始主内容")
        self.assertEqual(len(target_page.secondary_body), 1)
        self.assertEqual(target_page.secondary_body[0].value, "目标页原始引用")
        self.assertEqual(target_page.native_intro, "未被修改的导读")

    def test_prepare_with_unknown_block_type_fails_before_mongo_write(self) -> None:
        """测试 prepare_external_content_for_revision 在写入 Mongo 前拦截未知块类型，且零 Mongo 副作用。"""
        page = MultiFieldTestPage(
            title="未知块测试页",
            native_intro="原生导读",
        )
        page.pk = 101
        page.__dict__["primary_body"] = [
            {"id": "bad-1", "type": "unregistered_illegal_block", "value": "非法块内容"}
        ]
        page.secondary_body = [("quote", "正常引用")]

        initial_count = self.collection.count_documents({})

        with self.assertRaises(CodecPayloadError) as ctx:
            self.coordinator.prepare_external_content_for_revision(page)
        self.assertIn("未在目标字段中定义", str(ctx.exception))

        # 核心断言：事前防御，零 Mongo 写入
        final_count = self.collection.count_documents({})
        self.assertEqual(initial_count, final_count)


class CoordinatorPartialWriteLogSafetyTests(SimpleTestCase):
    """第二字段失败时保留首字段新版本，但不得通过协调器日志泄露正文或异常。"""

    def setUp(self) -> None:
        self.sensitive_marker = "COORD_SYNTHETIC_PRIVATE_MARKER_20261006"
        self.created_version_id = "coord-log-safety-first-version"
        self.codec = StreamFieldV1Codec()
        fields = {
            "primary_body": StreamField([("heading", blocks.CharBlock())]),
            "secondary_body": StreamField([("quote", blocks.BlockQuoteBlock())]),
        }
        self.page = SimpleNamespace(
            pk=101,
            _meta=SimpleNamespace(
                label_lower="external_content_test_coord.multifieldtestpage",
                get_field=fields.__getitem__,
            ),
            primary_body=[
                {"id": "primary-log-safe", "type": "heading", "value": "首字段正文"},
            ],
            secondary_body=[
                {"id": "secondary-log-safe", "type": "quote", "value": "次字段正文"},
            ],
        )
        self.policy = ContentStoragePolicy(
            model_label=self.page._meta.label_lower,
            schema_version=1,
            external_fields={
                field_name: FieldStorageConfig(
                    field_name=field_name,
                    backend="mongo",
                    codec=self.codec,
                )
                for field_name in fields
            },
        )
        self.store = create_autospec(ContentVersionStore, instance=True)
        self.coordinator = ExternalContentCoordinator(store=self.store)

    def _assert_partial_write_retained(self, expected_fields: list[str]) -> None:
        """失败只保留已成功创建的版本账本，禁止同步补偿删除或继续访问仓储。"""
        self.assertEqual(
            self.coordinator.last_created_version_ids,
            [self.created_version_id],
        )
        self.assertEqual(
            [store_call[0] for store_call in self.store.mock_calls],
            ["save_version_with_status"] * len(expected_fields),
        )
        self.assertEqual(
            [
                store_call.kwargs["field_name"]
                for store_call in self.store.save_version_with_status.call_args_list
            ],
            expected_fields,
        )
        for store_call in self.store.save_version_with_status.call_args_list:
            self.assertEqual(store_call.kwargs["page_id"], 101)
            self.assertEqual(store_call.kwargs["model_label"], self.policy.model_label)
            self.assertEqual(store_call.kwargs["codec"], "streamfield.v1")
            self.assertEqual(store_call.kwargs["schema_version"], 1)
            self.assertRegex(store_call.kwargs["payload_sha256"], r"^[0-9a-f]{64}$")
        self.store.cleanup_unreferenced_version.assert_not_called()
        self.store.delete_version.assert_not_called()

    def _assert_warning_is_safe(self, captured: Any) -> None:
        """检查未格式化参数、最终输出及异常渠道，不让正文藏在延迟格式化日志中。"""
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.WARNING)
        formatter = logging.Formatter()
        surfaces = {
            "message_template": str(record.msg),
            "message_args": repr(record.args),
            "message": record.getMessage(),
            "formatted": formatter.format(record),
            "captured_output": "\n".join(captured.output),
            "exception_info": repr(record.exc_info),
            "exception_text": record.exc_text or "",
            "stack_info": record.stack_info or "",
        }
        if record.exc_info:
            surfaces["formatted_exception"] = formatter.formatException(record.exc_info)
        for surface_name, surface in surfaces.items():
            with self.subTest(log_surface=surface_name):
                self.assertFalse(
                    self.sensitive_marker in surface,
                    "协调器警告不得包含合成敏感输入",
                )
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.exc_text)
        self.assertIsNone(record.stack_info)
        arguments = record.args.values() if isinstance(record.args, dict) else record.args
        self.assertFalse(
            any(isinstance(argument, BaseException) for argument in arguments),
            "协调器警告不得携带原始异常对象，即使上游已脱敏",
        )

    def test_second_field_codec_failure_retains_version_without_sensitive_warning(self) -> None:
        """真实 Codec 拒绝未知块类型后，上抛错误且不返回部分 Manifest、不泄漏输入。"""
        self.page.secondary_body[0]["type"] = self.sensitive_marker
        self.store.save_version_with_status.return_value = (
            self.created_version_id,
            True,
        )
        returned_manifest = None

        with self.assertLogs("external_content.coordinator", level="WARNING") as captured:
            with self.assertRaises(CodecPayloadError):
                returned_manifest = self.coordinator.prepare_external_content_for_revision(
                    self.page,
                    policy=self.policy,
                )

        self.assertIsNone(returned_manifest)
        self._assert_partial_write_retained(["primary_body"])
        self.assertEqual(
            self.store.save_version_with_status.call_args.kwargs["payload"],
            [{"id": "primary-log-safe", "type": "heading", "value": "首字段正文"}],
        )
        self._assert_warning_is_safe(captured)

    def test_second_field_store_failure_retains_version_without_sensitive_warning(self) -> None:
        """存储底层错误即使包含敏感文本，仍原样传播而不进入协调器警告或同步清理。"""
        store_error = ContentStoreError(
            f"synthetic store failure: {self.sensitive_marker}",
        )
        self.store.save_version_with_status.side_effect = [
            (self.created_version_id, True),
            store_error,
        ]
        returned_manifest = None

        with self.assertLogs("external_content.coordinator", level="WARNING") as captured:
            with self.assertRaises(ContentStoreError) as raised:
                returned_manifest = self.coordinator.prepare_external_content_for_revision(
                    self.page,
                    policy=self.policy,
                )

        self.assertIs(raised.exception, store_error)
        self.assertIsNone(returned_manifest)
        self._assert_partial_write_retained(["primary_body", "secondary_body"])
        self.assertEqual(
            self.store.save_version_with_status.call_args.kwargs["payload"],
            [{"id": "secondary-log-safe", "type": "quote", "value": "次字段正文"}],
        )
        self._assert_warning_is_safe(captured)
