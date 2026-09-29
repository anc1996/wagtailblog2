"""
Wagtail Page 正文外置通用协议 - 异常层级定义。

定义 Codec、Manifest、Store、策略配置及生命周期的强类型异常体系，
所有异常均继承自 ExternalContentError，禁止在下游吞咽异常或静默降级为空正文。
"""

from typing import Any, Optional


class ExternalContentError(Exception):
    """正文外置通用协议根异常。"""
    code: str = "external_content_error"

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} (details={self.details})"
        return self.message


class CodecError(ExternalContentError):
    """编解码器异常基类。"""
    code: str = "codec_error"


class CodecNotFoundError(CodecError):
    """指定字段类型未在注册表中找到匹配的 Codec。"""
    code: str = "codec_not_found"


class CodecPayloadError(CodecError):
    """字段正文 payload 结构、类型或规范化失败。"""
    code: str = "codec_payload_invalid"


class CorruptedContentPayloadError(CodecError):
    """读取正文时哈希校验失败或数据内容损坏。"""
    code: str = "corrupted_content_payload"


class FieldStorageConfigError(ExternalContentError):
    """模型级 content_storage 或 content_storage_schema 配置非法。"""
    code: str = "field_storage_config_invalid"


class ManifestError(ExternalContentError):
    """Manifest 解析与校验异常基类。"""
    code: str = "manifest_error"


class ManifestValidationError(ManifestError):
    """Manifest 格式不完整、缺少必须键或类型错误。"""
    code: str = "manifest_validation_failed"


class ManifestProtocolError(ManifestError):
    """Manifest 协议版本不受支持。"""
    code: str = "manifest_protocol_unsupported"


class ManifestIdentityMismatchError(ManifestError):
    """Manifest 所属模型或 Page ID 与当前上下文不符。"""
    code: str = "manifest_identity_mismatch"


class ContentStoreError(ExternalContentError):
    """底层不可变正文仓储操作异常。"""
    code: str = "content_store_error"


class ContentVersionNotFoundError(ContentStoreError):
    """在 MongoDB 中未找到对应 version_id 的不可变正文文档。"""
    code: str = "content_version_missing"


class ContentVersionIdentityMismatchError(ContentStoreError):
    """从 MongoDB 读取的文档身份（page_id/field/sha256）与请求指针不匹配。"""
    code: str = "content_version_identity_mismatch"


class PublicationError(ExternalContentError):
    """发布与生命周期协调异常基类。"""
    code: str = "publication_error"


class PublicationRevisionInvalidError(PublicationError):
    """指定 Revision 缺少可发布正文版本 Manifest 或 Manifest 校验失败。"""
    code: str = "publication_revision_invalid"


class PublicationBodyUnavailableError(PublicationError):
    """指定 Revision 的 Mongo 正文版本不可读取、哈希校验失败或网络中断。"""
    code: str = "publication_body_unavailable"


class PublicationWorkflowRevisionDriftError(PublicationError):
    """Workflow 审批通过后页面产生新 Revision 时阻止隐式发布新正文。"""
    code: str = "publication_workflow_revision_drift"


class RevisionOverwriteForbiddenError(ExternalContentError):
    """指定覆盖的 Revision 已被审批、发布、排期或工作流持久引用，严格禁止覆盖。"""
    code: str = "revision_overwrite_forbidden"

class DirectSaveForbiddenError(PublicationError):
    """外置正文页面禁止直接调用 save() 持久化正文变更。"""
    code: str = "direct_save_forbidden"

class ExternalContentOperationBlockedError(ExternalContentError):
    """正文外置通用协议前置阶段（如 G4 落地前）阻断未就绪操作（如复制、别名、删除）。"""
    code: str = "external_content_operation_blocked"
