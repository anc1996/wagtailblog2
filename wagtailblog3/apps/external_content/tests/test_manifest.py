"""
Wagtail Page 正文外置通用协议 - Manifest 协议定向测试。

验证 ExternalContentManifest 的协议版本锁定、严格字段指针校验、键白名单防冗余与页面/Revision身份交叉核验。
"""

from types import SimpleNamespace
from django.test import TestCase
from wagtail.models import Page

from external_content.exceptions import (
    ManifestIdentityMismatchError,
    ManifestProtocolError,
    ManifestValidationError,
)
from external_content.manifest import ExternalContentManifest, FieldManifestEntry

class ManifestProtocolTests(TestCase):
    """Manifest 规范解析与安全防御测试。"""

    def setUp(self) -> None:
        self.valid_field_entry = FieldManifestEntry(
            codec="streamfield.v1",
            schema_version=1,
            version_id="ver-1234567890abcdef",
            payload_sha256="a" * 64,
        )
        self.valid_manifest_data = {
            "protocol_version": 1,
            "content_storage_schema": 1,
            "model_label": "guides.guidepage",
            "page_id": 42,
            "fields": {
                "body": {
                    "codec": "streamfield.v1",
                    "schema_version": 1,
                    "version_id": "ver-1234567890abcdef",
                    "payload_sha256": "a" * 64,
                }
            },
        }

    def test_valid_manifest_roundtrip(self) -> None:
        """合法 Manifest 字典与实例序列化往返等价。"""
        manifest = ExternalContentManifest.from_dict(self.valid_manifest_data)
        self.assertEqual(manifest.protocol_version, 1)
        self.assertEqual(manifest.content_storage_schema, 1)
        self.assertEqual(manifest.model_label, "guides.guidepage")
        self.assertEqual(manifest.page_id, 42)
        self.assertIn("body", manifest.fields)
        self.assertEqual(manifest.fields["body"].version_id, "ver-1234567890abcdef")

        serialized = manifest.to_dict()
        self.assertEqual(serialized["protocol_version"], 1)
        self.assertEqual(serialized["fields"]["body"]["codec"], "streamfield.v1")

    def test_wrapped_namespace_support(self) -> None:
        """支持解析外层包装为 _external_content_manifest 的字典。"""
        wrapped = {"_external_content_manifest": self.valid_manifest_data}
        manifest = ExternalContentManifest.from_dict(wrapped)
        self.assertEqual(manifest.page_id, 42)

    def test_unsupported_protocol_version_rejected(self) -> None:
        """protocol_version 不为 1 时必须拒绝并抛出 ManifestProtocolError。"""
        bad_data = dict(self.valid_manifest_data, protocol_version=2)
        with self.assertRaises(ManifestProtocolError):
            ExternalContentManifest.from_dict(bad_data)

    def test_boolean_protocol_version_rejected(self) -> None:
        """protocol_version 为 True 时必须拒绝（Python 中 bool 是 int 子类）。"""
        bad_data = dict(self.valid_manifest_data, protocol_version=True)
        with self.assertRaises(ManifestProtocolError):
            ExternalContentManifest.from_dict(bad_data)

    def test_float_protocol_version_rejected(self) -> None:
        """protocol_version 为浮点数 1.0 时必须拒绝，契约严格锁定为整数 1。"""
        bad_data = dict(self.valid_manifest_data, protocol_version=1.0)
        with self.assertRaises(ManifestProtocolError):
            ExternalContentManifest.from_dict(bad_data)

    def test_missing_root_attribute_rejected(self) -> None:
        """缺少 model_label 或 page_id 等关键属性时失败关闭。"""
        bad_data = dict(self.valid_manifest_data)
        del bad_data["page_id"]
        with self.assertRaises(ManifestValidationError):
            ExternalContentManifest.from_dict(bad_data)

    def test_redundant_root_attribute_rejected(self) -> None:
        """Manifest 包含未知的根节点冗余属性时严格拒绝。"""
        bad_data = dict(self.valid_manifest_data, injected_extra_prop="malicious")
        with self.assertRaises(ManifestValidationError):
            ExternalContentManifest.from_dict(bad_data)

    def test_redundant_field_entry_attribute_rejected(self) -> None:
        """字段指针条目中包含未知的冗余属性时严格拒绝。"""
        bad_fields = {
            "body": {
                "codec": "streamfield.v1",
                "schema_version": 1,
                "version_id": "ver-123",
                "payload_sha256": "a" * 64,
                "extra_metadata": "unexpected",
            }
        }
        bad_data = dict(self.valid_manifest_data, fields=bad_fields)
        with self.assertRaises(ManifestValidationError):
            ExternalContentManifest.from_dict(bad_data)

    def test_empty_fields_dict_rejected(self) -> None:
        """fields 为空字典时拒绝。"""
        bad_data = dict(self.valid_manifest_data, fields={})
        with self.assertRaises(ManifestValidationError):
            ExternalContentManifest.from_dict(bad_data)

    def test_corrupted_field_sha256_rejected(self) -> None:
        """payload_sha256 格式非法（非 64 位小写十六进制）时拒绝。"""
        bad_data = {
            "protocol_version": 1,
            "content_storage_schema": 1,
            "model_label": "guides.guidepage",
            "page_id": 42,
            "fields": {
                "body": {
                    "codec": "streamfield.v1",
                    "schema_version": 1,
                    "version_id": "ver-123",
                    "payload_sha256": "not-a-valid-sha256",
                }
            },
        }
        with self.assertRaises(ManifestValidationError):
            ExternalContentManifest.from_dict(bad_data)

    def test_validate_against_page_success_and_identity_mismatches(self) -> None:
        """验证与实际 Page 实例交叉核验：匹配通过，跨页或模型不符阻断。"""
        manifest_data = dict(self.valid_manifest_data, model_label="guides.guidepage")
        manifest = ExternalContentManifest.from_dict(manifest_data)

        page = SimpleNamespace(_meta=SimpleNamespace(label_lower="guides.guidepage"), pk=42)
        manifest.validate_against_page(page)

        # 跨页 page_id 不符
        page_other = SimpleNamespace(_meta=SimpleNamespace(label_lower="guides.guidepage"), pk=99)
        with self.assertRaises(ManifestIdentityMismatchError):
            manifest.validate_against_page(page_other)

        # 模型 model_label 不符
        page_wrong_model = SimpleNamespace(_meta=SimpleNamespace(label_lower="blog.blogpage"), pk=42)
        page_wrong_model.pk = 42
        with self.assertRaises(ManifestIdentityMismatchError):
            manifest.validate_against_page(page_wrong_model)

    def test_validate_against_revision(self) -> None:
        """验证与 Wagtail Revision 对象的 object_id 与 content_type 交叉核验（严格失败关闭）。"""
        manifest = ExternalContentManifest.from_dict(self.valid_manifest_data)

        # 1. 合法 Revision 匹配
        ct_mock = SimpleNamespace(app_label="guides", model="guidepage")
        valid_rev = SimpleNamespace(object_id=42, content_type=ct_mock)
        manifest.validate_against_revision(valid_rev)

        # 2. None Revision 拒绝
        with self.assertRaises(ManifestIdentityMismatchError):
            manifest.validate_against_revision(None)

        # 3. 缺少 object_id 拒绝
        missing_id_rev = SimpleNamespace(content_type=ct_mock)
        with self.assertRaises(ManifestIdentityMismatchError):
            manifest.validate_against_revision(missing_id_rev)

        # 4. 缺少 content_type 拒绝
        missing_ct_rev = SimpleNamespace(object_id=42)
        with self.assertRaises(ManifestIdentityMismatchError):
            manifest.validate_against_revision(missing_ct_rev)

        # 5. object_id 不符拒绝
        bad_id_rev = SimpleNamespace(object_id=999, content_type=ct_mock)
        with self.assertRaises(ManifestIdentityMismatchError):
            manifest.validate_against_revision(bad_id_rev)

        # 6. content_type 模型不符拒绝
        bad_ct_mock = SimpleNamespace(app_label="blog", model="blogpage")
        bad_ct_rev = SimpleNamespace(object_id=42, content_type=bad_ct_mock)
        with self.assertRaises(ManifestIdentityMismatchError):
            manifest.validate_against_revision(bad_ct_rev)
    def test_invalid_model_label_formats_rejected(self) -> None:
        """测试非标准小写 app_label.model_name 格式在 Manifest 校验时被严格拦截。"""
        invalid_labels = [
            "GuidePage",
            "guides",
            "guides.guide.page",
            "guides/guidepage",
            "guides-guidepage",
            "guides guidepage",
            "GUIDES.GUIDEPAGE",
            "",
        ]
        for bad_label in invalid_labels:
            bad_data = dict(self.valid_manifest_data)
            bad_data["model_label"] = bad_label
            with self.assertRaises(ManifestValidationError, msg=f"应拒绝非法 model_label: {bad_label}"):
                ExternalContentManifest.from_dict(bad_data)
