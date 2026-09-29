"""
Wagtail Page 正文外置通用协议 - Manifest 协议规范与解析。

契约规定：
1. Revision 中唯一顶层命名空间为 _external_content_manifest；
2. protocol_version 严格锁定为整数 1（强类型拒绝布尔值及浮点数 1.0）；
3. 严格白名单键校验，拒绝任何根节点或字段节点多余冗余属性；
4. 严格绑定 model_label、page_id、Revision 及每个外置字段的 codec、schema_version、version_id 与 payload_sha256；
5. Revision 身份核验严格失败关闭：object_id 与 content_type 缺一不可；
6. 任何结构缺失、类型错误或身份不匹配必须失败关闭，严禁静默回退。
"""

from dataclasses import asdict, dataclass
import re
from typing import Any, Dict, Optional

from wagtail.models import Page

from external_content.exceptions import (
    ManifestIdentityMismatchError,
    ManifestProtocolError,
    ManifestValidationError,
)

PROTOCOL_VERSION: int = 1
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MODEL_LABEL_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
ALLOWED_ROOT_KEYS = frozenset({"protocol_version", "content_storage_schema", "model_label", "page_id", "fields"})
ALLOWED_ENTRY_KEYS = frozenset({"codec", "schema_version", "version_id", "payload_sha256"})


@dataclass(frozen=True)
class FieldManifestEntry:
    """单个外置字段的不可变指针身份。"""
    codec: str
    schema_version: int
    version_id: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.codec, str) or not self.codec.strip():
            raise ManifestValidationError("FieldManifestEntry.codec 必须为非空字符串")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version < 1
        ):
            raise ManifestValidationError("FieldManifestEntry.schema_version 必须为正整数 (>=1)")
        if not isinstance(self.version_id, str) or not self.version_id.strip():
            raise ManifestValidationError("FieldManifestEntry.version_id 必须为非空字符串")
        if not isinstance(self.payload_sha256, str) or not SHA256_HEX_PATTERN.match(self.payload_sha256):
            raise ManifestValidationError(
                f"FieldManifestEntry.payload_sha256 必须为 64 位小写十六进制字符串，当前为: {self.payload_sha256}"
            )


@dataclass(frozen=True)
class ExternalContentManifest:
    """
    通用正文外置协议 Revision Manifest。

    Revision JSON 与发布状态中保存的唯一正文指针凭证。
    """
    protocol_version: int
    content_storage_schema: int
    model_label: str
    page_id: int
    fields: Dict[str, FieldManifestEntry]

    def __post_init__(self) -> None:
        if (
            isinstance(self.protocol_version, bool)
            or not isinstance(self.protocol_version, int)
            or self.protocol_version != PROTOCOL_VERSION
        ):
            raise ManifestProtocolError(
                f"不受支持的 Manifest protocol_version: {self.protocol_version}，当前仅支持协议整数主版本: {PROTOCOL_VERSION}"
            )
        if (
            isinstance(self.content_storage_schema, bool)
            or not isinstance(self.content_storage_schema, int)
            or self.content_storage_schema < 1
        ):
            raise ManifestValidationError("content_storage_schema 必须为正整数 (>=1)")
        if not isinstance(self.model_label, str) or not MODEL_LABEL_PATTERN.match(self.model_label):
            raise ManifestValidationError(f"ExternalContentManifest.model_label 必须为规范小写 'app_label.model_name' 格式，当前为: {self.model_label!r}")
        if (
            isinstance(self.page_id, bool)
            or not isinstance(self.page_id, int)
            or self.page_id < 1
        ):
            raise ManifestValidationError("page_id 必须为已持久化的正整数主键 (>=1)")
        if not isinstance(self.fields, dict) or not self.fields:
            raise ManifestValidationError("fields 必须为包含至少一个外置字段的字典")

    def to_dict(self) -> Dict[str, Any]:
        """将 Manifest 序列化为字典表示。"""
        return {
            "protocol_version": self.protocol_version,
            "content_storage_schema": self.content_storage_schema,
            "model_label": self.model_label,
            "page_id": self.page_id,
            "fields": {
                name: asdict(entry)
                for name, entry in sorted(self.fields.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ExternalContentManifest":
        """
        从字典解析并严格校验 Manifest。

        :param data: 包含 _external_content_manifest 或直接是 manifest 字典的数据
        :return: ExternalContentManifest 实例
        :raises ManifestValidationError: 结构非法、缺少必要字段或存在多余属性
        :raises ManifestProtocolError: 协议版本不受支持
        """
        if not isinstance(data, dict):
            raise ManifestValidationError(f"Manifest 数据必须为字典，当前类型: {type(data).__name__}")

        # 允许外层包裹 _external_content_manifest 键或直接传入内部字典
        manifest_payload = data.get("_external_content_manifest", data)
        if not isinstance(manifest_payload, dict):
            raise ManifestValidationError("Manifest 载荷必须为字典")

        # 严格白名单键校验，拒绝任何多余冗余属性与属性缺失
        actual_root_keys = set(manifest_payload.keys())
        if actual_root_keys != ALLOWED_ROOT_KEYS:
            missing = ALLOWED_ROOT_KEYS - actual_root_keys
            extra = actual_root_keys - ALLOWED_ROOT_KEYS
            if missing:
                raise ManifestValidationError(f"Manifest 缺少必须的根节点属性: {sorted(missing)}")
            if extra:
                raise ManifestValidationError(f"Manifest 包含未知的根节点冗余属性: {sorted(extra)}")

        pv = manifest_payload.get("protocol_version")
        if (
            isinstance(pv, bool)
            or not isinstance(pv, int)
            or pv != PROTOCOL_VERSION
        ):
            raise ManifestProtocolError(
                f"不受支持的 Manifest protocol_version: {pv}，当前仅支持协议整数主版本: {PROTOCOL_VERSION}"
            )

        fields_data = manifest_payload.get("fields")
        if not isinstance(fields_data, dict) or not fields_data:
            raise ManifestValidationError("Manifest.fields 必须为非空字典")

        parsed_fields: Dict[str, FieldManifestEntry] = {}
        for field_name, entry_data in fields_data.items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise ManifestValidationError("字段名称必须为非空字符串")
            if not isinstance(entry_data, dict):
                raise ManifestValidationError(f"字段 '{field_name}' 的指针条目必须为字典")

            # 字段条目严格白名单校验
            actual_entry_keys = set(entry_data.keys())
            if actual_entry_keys != ALLOWED_ENTRY_KEYS:
                entry_missing = ALLOWED_ENTRY_KEYS - actual_entry_keys
                entry_extra = actual_entry_keys - ALLOWED_ENTRY_KEYS
                if entry_missing:
                    raise ManifestValidationError(f"字段 '{field_name}' 指针缺少必要属性: {sorted(entry_missing)}")
                if entry_extra:
                    raise ManifestValidationError(f"字段 '{field_name}' 指针包含未知的冗余属性: {sorted(entry_extra)}")

            parsed_fields[field_name] = FieldManifestEntry(
                codec=entry_data["codec"],
                schema_version=entry_data["schema_version"],
                version_id=entry_data["version_id"],
                payload_sha256=entry_data["payload_sha256"],
            )

        return cls(
            protocol_version=manifest_payload["protocol_version"],
            content_storage_schema=manifest_payload["content_storage_schema"],
            model_label=manifest_payload["model_label"],
            page_id=manifest_payload["page_id"],
            fields=parsed_fields,
        )

    def validate_against_page(self, page: Page) -> None:
        """
        对 Manifest 的身份绑定进行交叉核验。

        验证 model_label 与 page._meta.label_lower 一致；
        验证 page_id 与 page.pk 一致。

        :param page: 待校验的 Wagtail Page 实例
        :raises ManifestIdentityMismatchError: 页面身份与 Manifest 不符
        """
        expected_label = getattr(page._meta, "label_lower", "")
        if self.model_label != expected_label:
            raise ManifestIdentityMismatchError(
                f"Manifest model_label ('{self.model_label}') 与实际页面模型 ('{expected_label}') 不一致，阻断指针解析"
            )

        if self.page_id != page.pk:
            raise ManifestIdentityMismatchError(
                f"Manifest page_id ({self.page_id}) 与实际页面主键 ({page.pk}) 不一致，阻断跨页指针非法读取"
            )

    def validate_against_revision(self, revision: Any) -> None:
        """
        验证 Manifest 与 Wagtail Revision 元数据的绑定一致性（严格失败关闭）。

        必须具备有效的 object_id 与 content_type；缺少任何属性或与 Manifest 不符均抛出 ManifestIdentityMismatchError。

        :param revision: Wagtail Revision 实例或具备 object_id / content_type 的结构
        :raises ManifestIdentityMismatchError: Revision 缺少必要身份属性或与 Manifest 不符
        """
        if revision is None:
            raise ManifestIdentityMismatchError("待校验的 Revision 对象不能为 None")

        object_id = getattr(revision, "object_id", None)
        if object_id is None:
            raise ManifestIdentityMismatchError("Revision 缺少必需的 object_id 属性，阻断指针解析")

        try:
            rev_object_id = int(str(object_id))
        except (ValueError, TypeError):
            raise ManifestIdentityMismatchError(
                f"Revision object_id ({object_id}) 无法转换为整数，阻断指针解析"
            )

        if rev_object_id != self.page_id:
            raise ManifestIdentityMismatchError(
                f"Manifest page_id ({self.page_id}) 与 Revision.object_id ({object_id}) 不一致，阻断跨页指针非法读取"
            )

        content_type = getattr(revision, "content_type", None)
        if not content_type:
            raise ManifestIdentityMismatchError("Revision 缺少必需的 content_type 属性，阻断指针解析")

        app_label = getattr(content_type, "app_label", None)
        model = getattr(content_type, "model", None)
        if not app_label or not model:
            raise ManifestIdentityMismatchError("Revision.content_type 缺少 app_label 或 model 属性")

        expected_ct_label = f"{app_label}.{model}".lower()
        if self.model_label != expected_ct_label:
            raise ManifestIdentityMismatchError(
                f"Manifest model_label ('{self.model_label}') 与 Revision.content_type ('{expected_ct_label}') 不一致，阻断跨模型指针非法读取"
            )