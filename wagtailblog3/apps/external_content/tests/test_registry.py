"""
Wagtail Page 正文外置通用协议 - 策略注册表与防漂移定向测试。

验证：
1. 精确匹配 Field 类型的 Codec 查找；
2. 自定义子类继承保护：必须显式声明 __external_content_compatible__ = True；
3. 未授权覆盖注册抛出异常；
4. 解析 content_storage 字典，提取字段配置；
5. 策略快照生成与确定性 SHA-256 指纹；
6. 磁盘持久化基线校验防漂移（同版本修改配置失败关闭，升版本允许登记新基线）；
7. 策略历史版本解析 (get_policy_for_version) 与快照指纹重算验签；
8. 缺失/损坏基线文件失败关闭，未登记策略在只读模式下阻断，锁内并发冲突阻断覆盖。
"""

import json
import os
import tempfile
from django.db import models
from django.test import SimpleTestCase
from wagtail.models import Page
from wagtail.fields import StreamField
from wagtail import blocks

from external_content.codecs.base import BaseContentCodec
from external_content.codecs.streamfield import StreamFieldV1Codec
from external_content.exceptions import (
    CodecError,
    CodecNotFoundError,
    FieldStorageConfigError,
)
from external_content.registry import (
    CodecRegistry,
    ContentStoragePolicy,
    get_policy_for_version,
    get_storage_policy_for_model,
    load_policy_baselines,
    record_policy_baseline,
    reset_active_baseline_file,
    set_active_baseline_file,
)


class IncompatibleCustomStreamField(StreamField):
    """未显式声明兼容标记的自定义 StreamField。"""
    pass


class ExplicitlyCompatibleCustomStreamField(StreamField):
    """显式声明兼容标记的自定义 StreamField。"""
    __external_content_compatible__ = True


class SubclassOfCompatibleStreamField(ExplicitlyCompatibleCustomStreamField):
    """继承自已兼容类的孙子类（未在自身类字典显式声明）。"""
    pass


class DummyCodec(BaseContentCodec):
    """测试用虚拟编解码器。"""
    codec_id = "dummy.v1"
    schema_version = 1

    def normalize_payload(self, value):
        return value

    def validate_payload(self, payload):
        pass

    def get_empty_sentinel(self):
        return []

    def encode(self, value):
        return self.normalize_payload(value), "a" * 64

    def decode(self, payload, field=None):
        return payload


class CodecRegistryTests(SimpleTestCase):
    """CodecRegistry 精确匹配与继承保护测试。"""

    def setUp(self) -> None:
        self.registry = CodecRegistry()
        self.codec = StreamFieldV1Codec()
        self.registry.register(StreamField, self.codec)

    def test_exact_type_match_success(self) -> None:
        """精确注册类型成功解析。"""
        field = StreamField([("heading", blocks.CharBlock())])
        resolved = self.registry.get_codec_for_field(field)
        self.assertEqual(resolved.codec_id, "streamfield.v1")

    def test_incompatible_custom_subclass_raises_error(self) -> None:
        """未显式声明 __external_content_compatible__ 的子类抛出 CodecNotFoundError。"""
        field = IncompatibleCustomStreamField([("heading", blocks.CharBlock())])
        with self.assertRaises(CodecNotFoundError) as ctx:
            self.registry.get_codec_for_field(field)
        self.assertIn("__external_content_compatible__", str(ctx.exception))

    def test_explicitly_compatible_subclass_succeeds(self) -> None:
        """自身类显式声明兼容标记的子类成功匹配基类 Codec。"""
        field = ExplicitlyCompatibleCustomStreamField([("heading", blocks.CharBlock())])
        resolved = self.registry.get_codec_for_field(field)
        self.assertEqual(resolved.codec_id, "streamfield.v1")

    def test_inherited_compatible_flag_rejected(self) -> None:
        """隐式从父类继承兼容标记的子类依然被拒绝，必须在自身类显式声明。"""
        field = SubclassOfCompatibleStreamField([("heading", blocks.CharBlock())])
        with self.assertRaises(CodecNotFoundError) as ctx:
            self.registry.get_codec_for_field(field)
        self.assertIn("未在自身类定义中显式声明", str(ctx.exception))

    def test_duplicate_registration_without_override_raises_error(self) -> None:
        """未指定 allow_override 时重复注册相同 Field 类型抛出异常。"""
        dummy = DummyCodec()
        with self.assertRaises(FieldStorageConfigError):
            self.registry.register(StreamField, dummy, allow_override=False)

    def test_duplicate_registration_with_override_succeeds(self) -> None:
        """指定 allow_override=True 时允许覆盖注册。"""
        dummy = DummyCodec()
        self.registry.register(StreamField, dummy, allow_override=True)
        self.assertEqual(self.registry.get_codec("dummy.v1"), dummy)

    def test_non_field_type_registration_rejected(self) -> None:
        """注册非 models.Field 类型抛出 TypeError。"""
        with self.assertRaises(TypeError):
            self.registry.register(str, DummyCodec())  # type: ignore

    def test_get_nonexistent_codec_raises_error(self) -> None:
        """查找不存在的 codec_id 抛出 CodecNotFoundError。"""
        with self.assertRaises(CodecNotFoundError):
            self.registry.get_codec("non_existent_codec")


class ContentStoragePolicyTests(SimpleTestCase):
    """模型级存储策略解析与持久化基线校验测试。"""

    def setUp(self) -> None:
        self.temp_baseline = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.temp_baseline.close()
        # 初始化基线骨架
        with open(self.temp_baseline.name, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "policies": {}}, f)
        set_active_baseline_file(self.temp_baseline.name)

    def tearDown(self) -> None:
        reset_active_baseline_file()
        if os.path.exists(self.temp_baseline.name):
            os.remove(self.temp_baseline.name)
        lock_file = self.temp_baseline.name + ".lock"
        if os.path.exists(lock_file):
            os.remove(lock_file)

    def test_unconfigured_model_returns_none(self) -> None:
        """未声明 content_storage 的普通页面返回 None。"""
        class PlainPage(Page):
            class Meta:
                app_label = "storage_test_plain"

        policy = get_storage_policy_for_model(PlainPage)
        self.assertIsNone(policy)

    def test_single_external_field_policy_resolved(self) -> None:
        """单外置字段策略正确解析并计算指纹。"""
        class ValidSinglePage(Page):
            intro = models.CharField(max_length=255, default="")
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "storage_test_single"

        policy = get_storage_policy_for_model(ValidSinglePage)
        self.assertIsNotNone(policy)
        self.assertEqual(policy.model_label, "storage_test_single.validsinglepage")
        self.assertEqual(policy.schema_version, 1)
        self.assertIn("body", policy.external_fields)
        self.assertNotIn("intro", policy.external_fields)
        self.assertEqual(policy.external_fields["body"].backend, "mongo")
        self.assertEqual(policy.external_fields["body"].codec.codec_id, "streamfield.v1")

        fp = policy.fingerprint()
        self.assertTrue(isinstance(fp, str) and len(fp) == 64)

    def test_multi_external_fields_policy_resolved(self) -> None:
        """多外置字段策略正确解析。"""
        class ValidMultiPage(Page):
            intro = models.CharField(max_length=255, default="")
            body = StreamField([("heading", blocks.CharBlock())])
            sidebar_content = StreamField([("text", blocks.TextBlock())])
            content_storage = {"body": "mongo", "sidebar_content": "mongo"}
            content_storage_schema = 2

            class Meta:
                app_label = "storage_test_multi"

        policy = get_storage_policy_for_model(ValidMultiPage)
        self.assertIsNotNone(policy)
        self.assertEqual(policy.schema_version, 2)
        self.assertEqual(len(policy.external_fields), 2)
        self.assertIn("body", policy.external_fields)
        self.assertIn("sidebar_content", policy.external_fields)
        self.assertNotIn("intro", policy.external_fields)

    def test_persistent_policy_drift_rejected_across_restarts(self) -> None:
        """验证跨进程/重启后同版本策略篡改检测：修改字段或后端抛出 FieldStorageConfigError。"""
        class PersistentEvolvingPage(Page):
            body = StreamField([("heading", blocks.CharBlock())])
            sidebar = StreamField([("text", blocks.TextBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "storage_test_evolving"

        # 1. 首次解析，将基线持久化至临时文件
        policy1 = get_storage_policy_for_model(PersistentEvolvingPage)
        self.assertEqual(policy1.schema_version, 1)

        # 2. 运行时篡改策略但未升级 schema
        PersistentEvolvingPage.content_storage = {"body": "mongo", "sidebar": "mongo"}

        # 3. 再次解析，比对持久化基线发现指纹不符并抛出异常
        with self.assertRaises(FieldStorageConfigError):
            get_storage_policy_for_model(PersistentEvolvingPage)

    def test_bumped_version_policy_modification_allowed(self) -> None:
        """递增 content_storage_schema 后允许登记新版本基线。"""
        class VersionBumpingPage(Page):
            body = StreamField([("heading", blocks.CharBlock())])
            sidebar = StreamField([("text", blocks.TextBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "storage_test_bump"

        policy_v1 = get_storage_policy_for_model(VersionBumpingPage)
        self.assertEqual(policy_v1.schema_version, 1)

        # 递增 schema 版本至 2，此时修改生效并写入新版本基线
        VersionBumpingPage.content_storage = {"body": "mongo", "sidebar": "mongo"}
        VersionBumpingPage.content_storage_schema = 2

        policy_v2 = get_storage_policy_for_model(VersionBumpingPage)
        self.assertEqual(policy_v2.schema_version, 2)
        self.assertEqual(len(policy_v2.external_fields), 2)

    def test_get_policy_for_version_current_and_historical(self) -> None:
        """测试按版本查询策略：当前版本直接解析，历史版本从基线快照无损还原。"""
        class MultiVersionPage(Page):
            body = StreamField([("heading", blocks.CharBlock())])
            sidebar = StreamField([("text", blocks.TextBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "storage_test_multiversion"

        # 1. 注册 v1
        p_v1 = get_storage_policy_for_model(MultiVersionPage, allow_record_new=True)
        self.assertEqual(p_v1.schema_version, 1)

        # 2. 升级至 v2 并添加 sidebar 外置
        MultiVersionPage.content_storage = {"body": "mongo", "sidebar": "mongo"}
        MultiVersionPage.content_storage_schema = 2
        p_v2 = get_storage_policy_for_model(MultiVersionPage, allow_record_new=True)
        self.assertEqual(p_v2.schema_version, 2)

        # 3. 按版本解析：v2 匹配当前模型
        res_v2 = get_policy_for_version(MultiVersionPage, 2)
        self.assertEqual(res_v2.schema_version, 2)
        self.assertEqual(len(res_v2.external_fields), 2)

        # 4. 按版本解析：v1 从历史基线快照还原（只包含 body，不包含 sidebar）
        res_v1 = get_policy_for_version(MultiVersionPage, 1)
        self.assertEqual(res_v1.schema_version, 1)
        self.assertEqual(len(res_v1.external_fields), 1)
        self.assertIn("body", res_v1.external_fields)
        self.assertNotIn("sidebar", res_v1.external_fields)

        # 5. 查询未登记的版本 99 抛出异常
        with self.assertRaises(FieldStorageConfigError):
            get_policy_for_version(MultiVersionPage, 99)

    def test_get_policy_for_version_tampered_snapshot_detected(self) -> None:
        """历史快照指纹或模型身份被篡改时，get_policy_for_version 失败关闭。"""
        class SnapshotModel(Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "storage_test_snap"

        get_storage_policy_for_model(SnapshotModel, allow_record_new=True)

        # 直接篡改文件中的指纹
        with open(self.temp_baseline.name, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = "storage_test_snap.snapshotmodel:v1"
        data["policies"][key]["fingerprint"] = "bad" * 21 + "a"
        with open(self.temp_baseline.name, "w", encoding="utf-8") as f:
            json.dump(data, f)

        # 递增模型当前版本使 v1 变为历史版本走快照还原分支
        SnapshotModel.content_storage_schema = 2
        with self.assertRaises(FieldStorageConfigError) as ctx:
            get_policy_for_version(SnapshotModel, 1)
        self.assertIn("历史基线快照指纹验签失败", str(ctx.exception))

    def test_record_policy_baseline_conflict_different_fingerprint_raises(self) -> None:
        """锁内记录基线时，若已有同版本记录且指纹不同，拒绝覆盖。"""
        snapshot = {
            "model_label": "test.m",
            "schema_version": 1,
            "external_fields": {},
        }
        record_policy_baseline("test.m", 1, "fp1" + "0" * 61, snapshot)

        # 相同指纹允许幂等通过
        record_policy_baseline("test.m", 1, "fp1" + "0" * 61, snapshot)

        # 异构指纹必须拒绝
        with self.assertRaises(FieldStorageConfigError) as ctx:
            record_policy_baseline("test.m", 1, "fp2" + "0" * 61, snapshot)
        self.assertIn("严禁在并发竞争中篡改或覆盖已有版本策略基线", str(ctx.exception))

    def test_record_policy_baseline_detects_corrupted_file_fails_closed(self) -> None:
        """基线文件已存在但内容损坏时，record_policy_baseline 必须失败关闭，绝不重置为清空覆盖。"""
        with open(self.temp_baseline.name, "w", encoding="utf-8") as f:
            f.write("corrupted json content")

        snapshot = {"model_label": "test.m", "schema_version": 1, "external_fields": {}}
        with self.assertRaises(FieldStorageConfigError):
            record_policy_baseline("test.m", 1, "a" * 64, snapshot)

    def test_missing_baseline_file_raises_error(self) -> None:
        """基线文件不存在时必须抛出异常拒绝隐式通过。"""
        set_active_baseline_file("/tmp/non_existent_baseline_file_path.json")
        with self.assertRaises(FieldStorageConfigError):
            load_policy_baselines()

    def test_allow_record_new_false_rejects_unrecorded_model(self) -> None:
        """在只读模式下 (allow_record_new=False)，未备案的模型必须抛出异常阻断。"""
        class UnrecordedPage(Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 1

            class Meta:
                app_label = "storage_test_unrecorded"

        with self.assertRaises(FieldStorageConfigError):
            get_storage_policy_for_model(UnrecordedPage, allow_record_new=False)

    def test_content_storage_non_dict_raises_error(self) -> None:
        """content_storage 非字典类型抛出 FieldStorageConfigError。"""
        class InvalidTypePage(Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = ["body"]

            class Meta:
                app_label = "storage_test_baddict"

        with self.assertRaises(FieldStorageConfigError):
            get_storage_policy_for_model(InvalidTypePage)

    def test_invalid_schema_version_raises_error(self) -> None:
        """content_storage_schema 为非正整数时抛出 FieldStorageConfigError。"""
        class ZeroSchemaPage(Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "mongo"}
            content_storage_schema = 0

            class Meta:
                app_label = "storage_test_zeroschema"

        with self.assertRaises(FieldStorageConfigError):
            get_storage_policy_for_model(ZeroSchemaPage)

    def test_nonexistent_field_raises_error(self) -> None:
        """声明的模型中不存在的字段时抛出 FieldStorageConfigError。"""
        class MissingFieldPage(Page):
            content_storage = {"ghost_field": "mongo"}

            class Meta:
                app_label = "storage_test_missingfield"

        with self.assertRaises(FieldStorageConfigError):
            get_storage_policy_for_model(MissingFieldPage)

    def test_forbidden_field_raises_error(self) -> None:
        """外置保留路由、权限、主键或关系字段配置时立即被阻断。"""
        class ForbiddenPage(Page):
            extra_rel = models.ForeignKey("wagtailcore.Page", on_delete=models.CASCADE, null=True)
            content_storage = {"title": "mongo"}

            class Meta:
                app_label = "storage_test_forbidden"

        with self.assertRaises(FieldStorageConfigError):
            get_storage_policy_for_model(ForbiddenPage)

        class ForbiddenRelPage(Page):
            extra_rel = models.ForeignKey("wagtailcore.Page", on_delete=models.CASCADE, null=True)
            content_storage = {"extra_rel": "mongo"}

            class Meta:
                app_label = "storage_test_forbiddenrel"

        with self.assertRaises(FieldStorageConfigError):
            get_storage_policy_for_model(ForbiddenRelPage)

    def test_unsupported_backend_raises_error(self) -> None:
        """配置非支持的存储后端（如 redis）时立即阻断。"""
        class UnsupportedBackendPage(Page):
            body = StreamField([("heading", blocks.CharBlock())])
            content_storage = {"body": "redis"}

            class Meta:
                app_label = "storage_test_unsupportedbackend"

        with self.assertRaises(FieldStorageConfigError):
            get_storage_policy_for_model(UnsupportedBackendPage)