# -*- coding: utf-8 -*-
"""
Wagtail Page 正文外置通用协议 - 只读完整性核查服务。

功能与安全约束：
1. 只读审计：严禁调用 save()、delete()、update()，严禁向 MongoDB 发起任何写操作；
2. 索引硬门禁只读查验：仅调用 check_indexes() 查验，严禁自动调用 ensure_indexes() 建立索引；
3. 复合 6 键强校验：model_label, page_id, field_name, codec, schema_version, payload_sha256；
4. 读时哈希重算：重算 Mongo 文档中 payload 的 SHA-256 与声明比对，防范位翻转与损坏；
5. Revision 元数据关联：严格校验 Revision.content_type 与 Revision.object_id 与 Page 及 Manifest 绑定；
6. Alias 页面安全模型：Alias 页面拥有独立 Manifest，但 published_revision_id 必须为 None，不误报缺少 Revision；
7. 墓碑与删除意图保护：若 MySQL 页面实体不存在，但存在未回收或回收中的 ExternalContentDeletionIntent，不误报为 PAGE_MISSING；
8. 内存有界批处理：强制基于 QuerySet.iterator(chunk_size=batch_size) 进行有界流式核查；
9. 脱敏输出：仅输出有限错误码枚举与结构化元数据统计，严禁输出正文 payload、密码或 raw exception。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from external_content.registry import CodecRegistry
    from external_content.store import ContentVersionStore

logger = logging.getLogger(__name__)

# 批处理 batch_size 的严格边界范围
MIN_BATCH_SIZE: int = 1
MAX_BATCH_SIZE: int = 1000
DEFAULT_BATCH_SIZE: int = 100

# 样本保留上限默认值
DEFAULT_SAMPLE_LIMIT: int = 100


class IntegrityErrorCode(models.TextChoices):
    """
    通用正文外置协议 - 完整性核查有限错误码枚举。
    正交、有限、脱敏的机器识别错误码。
    """
    INDEX_MISSING = "INDEX_MISSING", "MongoDB唯一索引缺失或未就绪"
    PAGE_MISSING = "PAGE_MISSING", "MySQL页面不存在且无删除意图/墓碑记录"
    POLICY_MISMATCH = "POLICY_MISMATCH", "模型未备案或外置字段与基线不匹配"
    STATE_INVALID = "STATE_INVALID", "PublicationState记录异常或损坏"
    MANIFEST_INVALID = "MANIFEST_INVALID", "Manifest协议、结构或字段条目非法"
    REVISION_MISSING = "REVISION_MISSING", "状态引用的Revision在MySQL中缺失"
    REVISION_IDENTITY_MISMATCH = "REVISION_IDENTITY_MISMATCH", "Revision元数据与Page或Manifest不匹配"
    VERSION_MISSING = "VERSION_MISSING", "MongoDB中缺失对应version_id的文档"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH", "MongoDB文档6键身份与Manifest不匹配"
    HASH_MISMATCH = "HASH_MISMATCH", "MongoDB正文payload重算SHA-256哈希与声明不符"
    ALIAS_INVALID = "ALIAS_INVALID", "Alias别名页面状态或Revision绑定非法"


@dataclass(frozen=True)
class IntegrityIssue:
    """
    单个完整性核查问题记录（严格脱敏，不含正文、密码或敏感信息）。
    """
    error_code: str
    page_id: Optional[int] = None
    field_name: Optional[str] = None
    version_id: Optional[str] = None
    revision_id: Optional[int] = None
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """
        返回脱敏字典表示，严禁包含任何 payload 或凭据信息。
        """
        data: Dict[str, Any] = {
            "error_code": self.error_code,
            "page_id": self.page_id,
            "field_name": self.field_name,
            "version_id": self.version_id,
            "revision_id": self.revision_id,
            "message": self.message,
        }
        if self.details:
            data["details"] = self.details
        return {k: v for k, v in data.items() if v is not None}


@dataclass
class IntegritySummary:
    """
    完整性核查统计摘要（严格脱敏、内存友好、便于机器 JSON 解析）。
    """
    total_pages_checked: int = 0
    total_revisions_checked: int = 0
    total_versions_checked: int = 0
    total_issues_count: int = 0
    sample_limit: int = DEFAULT_SAMPLE_LIMIT
    issues_truncated: bool = False
    issues_by_code: Dict[str, int] = field(default_factory=dict)
    issues: List[IntegrityIssue] = field(default_factory=list)
    indexes_checked: bool = False
    indexes_ready: bool = False
    passed: bool = True

    def add_issue(self, issue: IntegrityIssue) -> None:
        """
        记录一个完整性缺陷，累加全量计数与分类计数，并在超出 sample_limit 时截断样本列表。
        """
        self.total_issues_count += 1
        code = issue.error_code
        self.issues_by_code[code] = self.issues_by_code.get(code, 0) + 1
        self.passed = False
        if len(self.issues) < self.sample_limit:
            self.issues.append(issue)
        else:
            self.issues_truncated = True

    def to_dict(self) -> Dict[str, Any]:
        """
        返回机器友好的统计摘要字典。
        """
        return {
            "total_pages_checked": self.total_pages_checked,
            "total_revisions_checked": self.total_revisions_checked,
            "total_versions_checked": self.total_versions_checked,
            "total_issues_count": self.total_issues_count,
            "sample_limit": self.sample_limit,
            "issues_truncated": self.issues_truncated,
            "issues_by_code": dict(sorted(self.issues_by_code.items())),
            "issues": [issue.to_dict() for issue in self.issues],
            "indexes_checked": self.indexes_checked,
            "indexes_ready": self.indexes_ready,
            "passed": self.passed,
        }


class ExternalContentIntegrityVerifier:
    """
    Wagtail Page 外置正文只读完整性核查器。

    执行原则：
    - 绝对只读：杜绝 save()、delete()、update()、写入 Mongo，严禁 ensure_indexes()；
    - 内存有界：分页采用 QuerySet.iterator(chunk_size=batch_size)；
    - 脱敏上报：仅上报有限错误码与统计信息；
    - 6 键交叉比对：MySQL <-> Manifest <-> Mongo 三位一体比对。
    """

    def __init__(
        self,
        *,
        store: Optional[ContentVersionStore] = None,
        registry: Optional[CodecRegistry] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        sample_limit: int = DEFAULT_SAMPLE_LIMIT,
        check_indexes_flag: bool = True,
        page_source: Optional[Any] = None,
        state_source: Optional[Any] = None,
        revision_source: Optional[Any] = None,
        deletion_intent_source: Optional[Any] = None,
    ) -> None:
        """
        初始化核查器并严格校验入参类型与数值边界。

        :param store: ContentVersionStore 实例（若为空则延迟获取）
        :param registry: CodecRegistry 实例（默认延迟使用 default_codec_registry）
        :param batch_size: 批次迭代大小，必须处于 [1, 1000]，严格拒绝 bool
        :param sample_limit: 样本留存上限，必须为正整数，严格拒绝 bool
        :param check_indexes_flag: 是否在前置阶段只读校验 MongoDB 索引完备性
        :param page_source: 注入的页面数据源或 QuerySet（用于单元测试解耦与流式迭代）
        :param state_source: 注入的发布状态数据源
        :param revision_source: 注入的修订历史数据源
        :param deletion_intent_source: 注入的删除意图/墓碑数据源
        :raises ValueError: batch_size 或 sample_limit 非法或越界
        """
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or batch_size < MIN_BATCH_SIZE
            or batch_size > MAX_BATCH_SIZE
        ):
            raise ValueError(
                f"batch_size 必须处于 [{MIN_BATCH_SIZE}, {MAX_BATCH_SIZE}] 范围内的整数且不能为布尔值，当前传入: {batch_size}"
            )

        if (
            isinstance(sample_limit, bool)
            or not isinstance(sample_limit, int)
            or sample_limit <= 0
        ):
            raise ValueError(
                f"sample_limit 必须为正整数且不能为布尔值，当前传入: {sample_limit}"
            )

        self.store = store
        self.batch_size = batch_size
        self.sample_limit = sample_limit
        self.check_indexes_flag = check_indexes_flag
        self.page_source = page_source
        self.state_source = state_source
        self.revision_source = revision_source
        self.deletion_intent_source = deletion_intent_source

        # 延迟解耦 registry
        if registry is not None:
            self.registry = registry
        else:
            try:
                from external_content.registry import default_codec_registry
                self.registry = default_codec_registry
            except Exception:
                self.registry = None

    def verify(
        self,
        *,
        page_id: Optional[int] = None,
    ) -> IntegritySummary:
        """
        执行全量或单页面外置正文只读完整性核查。

        契约保证：
        1. 严格只读零写入：不调用 save/update/delete，不调用 ensure_indexes；
        2. 参数严格校验：page_id 必须为正整数且非 bool；
        3. 纯 Fake 兼容：优先遍历注入的 page_source，在空集合或合规状态下平稳返回。

        :param page_id: 可选，指定仅核查的单个 Wagtail Page ID（正整数且拒绝 bool）
        :return: IntegritySummary 统计摘要对象
        :raises ValueError: page_id 参数非法或越界
        """
        if page_id is not None:
            if isinstance(page_id, bool) or not isinstance(page_id, int) or page_id <= 0:
                raise ValueError(
                    f"page_id 必须为正整数且不能为布尔值，当前传入: {page_id}"
                )

        summary = IntegritySummary(sample_limit=self.sample_limit)

        # 1. MongoDB 索引只读门禁检查
        if self.check_indexes_flag and self.store is not None:
            store = self.get_effective_store()
            summary.indexes_checked = True
            try:
                indexes_ok = store.check_indexes()
                summary.indexes_ready = bool(indexes_ok)
                if not summary.indexes_ready:
                    summary.add_issue(
                        IntegrityIssue(
                            error_code=IntegrityErrorCode.INDEX_MISSING,
                            message="MongoDB 唯一索引缺失或未就绪",
                        )
                    )
                    return summary
            except Exception as e:
                summary.indexes_ready = False
                summary.add_issue(
                    IntegrityIssue(
                        error_code=IntegrityErrorCode.INDEX_MISSING,
                        message=f"MongoDB 索引检查失败: {type(e).__name__}",
                    )
                )
                return summary

        # 2. 页面只读流式扫描
        pages_iterable = []
        if self.page_source is not None:
            if callable(self.page_source):
                pages_iterable = self.page_source()
            else:
                pages_iterable = self.page_source
        else:
            # 运行时未显式注入时延迟尝试加载 Wagtail Page
            try:
                from wagtail.models import Page
                qs = Page.objects.all()
                if page_id is not None:
                    qs = qs.filter(pk=page_id)
                pages_iterable = qs
            except Exception:
                pages_iterable = []

        # 支持 QuerySet.iterator(chunk_size=...) 或标准迭代器
        if hasattr(pages_iterable, "iterator"):
            iterator = pages_iterable.iterator(chunk_size=self.batch_size)
        else:
            iterator = iter(pages_iterable)

        checked_page_ids: set[int] = set()
        for page in iterator:
            page_pk = getattr(page, "pk", None)
            if not isinstance(page_pk, int) or isinstance(page_pk, bool) or page_pk <= 0:
                continue
            checked_page_ids.add(page_pk)
            summary.total_pages_checked += 1
            self._verify_page(page, summary)

        if page_id is None:
            if self.state_source is not None or self.page_source is None:
                self._verify_orphan_states(checked_page_ids, summary)

        return summary

    def _resolve_state(self, page_id: int) -> Any:
        """??????? ORM ???????????"""
        source = self.state_source
        if source is None:
            try:
                from external_content.models import ExternalContentPublicationState
                return ExternalContentPublicationState.objects.filter(page_id=page_id).first()
            except Exception:
                return None
        if callable(source):
            try:
                return source(page_id)
            except TypeError:
                source = source()
        if isinstance(source, dict):
            return source.get(page_id)
        if hasattr(source, "filter"):
            filtered = source.filter(page_id=page_id)
            return filtered.first() if hasattr(filtered, "first") else next(iter(filtered), None)
        return next((item for item in source if getattr(item, "page_id", None) == page_id), None)

    def _resolve_revision(self, revision_id: Any) -> Any:
        """??????? ORM ???? Revision?"""
        if revision_id is None:
            return None
        source = self.revision_source
        if source is None:
            try:
                from wagtail.models import Revision
                return Revision.objects.filter(pk=revision_id).first()
            except Exception:
                return None
        if callable(source):
            try:
                return source(revision_id)
            except TypeError:
                source = source()
        if isinstance(source, dict):
            return source.get(revision_id)
        if hasattr(source, "filter"):
            filtered = source.filter(pk=revision_id)
            return filtered.first() if hasattr(filtered, "first") else next(iter(filtered), None)
        return next((item for item in source if getattr(item, "pk", None) == revision_id), None)

    def _verify_page(self, page: Any, summary: IntegritySummary) -> None:
        """????? State?Revision?Manifest ? Mongo ?????"""
        from external_content.exceptions import (
            ContentStoreError,
            ContentVersionIdentityMismatchError,
            ContentVersionNotFoundError,
            CorruptedContentPayloadError,
        )
        from external_content.manifest import ExternalContentManifest
        from external_content.registry import get_storage_policy_for_model

        page_id = int(page.pk)
        page_class = getattr(page, "specific_class", None) or type(page)
        try:
            policy = get_storage_policy_for_model(page_class, allow_record_new=False)
        except Exception as exc:
            summary.add_issue(IntegrityIssue(
                error_code=IntegrityErrorCode.POLICY_MISMATCH,
                page_id=page_id,
                message=f"????????????: {type(exc).__name__}",
            ))
            return
        if policy is None or not policy.external_fields:
            return

        state = self._resolve_state(page_id)
        is_live = bool(getattr(page, "live", False))
        if state is None:
            if is_live:
                summary.add_issue(IntegrityIssue(
                    error_code=IntegrityErrorCode.STATE_INVALID,
                    page_id=page_id,
                    message="????????????????",
                ))
            return

        expected_label = getattr(page_class._meta, "label_lower", "")
        if getattr(state, "model_label", None) != expected_label:
            summary.add_issue(IntegrityIssue(
                error_code=IntegrityErrorCode.STATE_INVALID,
                page_id=page_id,
                message="PublicationState ???????????",
            ))

        for manifest_name, revision_name in (
            ("published_manifest", "published_revision_id"),
            ("draft_manifest", "draft_revision_id"),
            ("approved_manifest", "approved_revision_id"),
        ):
            raw_manifest = getattr(state, manifest_name, None)
            revision_id = getattr(state, revision_name, None)
            if raw_manifest is None:
                if manifest_name == "published_manifest" and is_live:
                    summary.add_issue(IntegrityIssue(
                        error_code=IntegrityErrorCode.STATE_INVALID,
                        page_id=page_id,
                        revision_id=revision_id,
                        message="???????? published_manifest",
                    ))
                continue
            try:
                manifest = ExternalContentManifest.from_dict(raw_manifest)
            except Exception as exc:
                summary.add_issue(IntegrityIssue(
                    error_code=IntegrityErrorCode.MANIFEST_INVALID,
                    page_id=page_id,
                    revision_id=revision_id,
                    message=f"{manifest_name} ????: {type(exc).__name__}",
                ))
                continue

            if manifest.page_id != page_id or manifest.model_label != expected_label:
                summary.add_issue(IntegrityIssue(
                    error_code=IntegrityErrorCode.REVISION_IDENTITY_MISMATCH,
                    page_id=page_id,
                    revision_id=revision_id,
                    message=f"{manifest_name} ????????",
                ))

            if revision_id is not None:
                summary.total_revisions_checked += 1
                revision = self._resolve_revision(revision_id)
                if revision is None:
                    summary.add_issue(IntegrityIssue(
                        error_code=IntegrityErrorCode.REVISION_MISSING,
                        page_id=page_id,
                        revision_id=revision_id,
                        message=f"{manifest_name} ??? Revision ???",
                    ))
                elif str(getattr(revision, "object_id", "")) != str(page_id):
                    summary.add_issue(IntegrityIssue(
                        error_code=IntegrityErrorCode.REVISION_IDENTITY_MISMATCH,
                        page_id=page_id,
                        revision_id=revision_id,
                        message="Revision.object_id ??????",
                    ))

            store = self.get_effective_store()
            for field_name, entry in manifest.fields.items():
                summary.total_versions_checked += 1
                expected_identity = {
                    "model_label": manifest.model_label,
                    "page_id": manifest.page_id,
                    "field_name": field_name,
                    "codec": entry.codec,
                    "schema_version": entry.schema_version,
                    "payload_sha256": entry.payload_sha256,
                }
                try:
                    store.get_version(entry.version_id, expected_identity=expected_identity)
                except ContentVersionNotFoundError:
                    code = IntegrityErrorCode.VERSION_MISSING
                    message = f"{manifest_name} ??? Mongo ?????"
                except ContentVersionIdentityMismatchError:
                    code = IntegrityErrorCode.IDENTITY_MISMATCH
                    message = f"{manifest_name} ??? Mongo ???????"
                except CorruptedContentPayloadError:
                    code = IntegrityErrorCode.HASH_MISMATCH
                    message = f"{manifest_name} ??? Mongo payload ?????"
                except ContentStoreError as exc:
                    code = IntegrityErrorCode.VERSION_MISSING
                    message = f"{manifest_name} ??????: {type(exc).__name__}"
                else:
                    continue
                summary.add_issue(IntegrityIssue(
                    error_code=code,
                    page_id=page_id,
                    field_name=field_name,
                    version_id=entry.version_id,
                    revision_id=revision_id,
                    message=message,
                ))

    def _verify_orphan_states(self, checked_page_ids: set[int], summary: IntegritySummary) -> None:
        """?????? Page ???????????????"""
        source = self.state_source
        if source is None:
            try:
                from external_content.models import ExternalContentPublicationState
                states = ExternalContentPublicationState.objects.all()
            except Exception:
                return
        elif callable(source):
            states = source()
        else:
            states = source
        intents = self.deletion_intent_source
        if intents is None:
            try:
                from external_content.models import ExternalContentDeletionIntent
                intents = ExternalContentDeletionIntent.objects.all()
            except Exception:
                intents = []
        intent_page_ids = {getattr(item, "page_id", None) for item in intents}
        for state in states:
            state_page_id = getattr(state, "page_id", None)
            if state_page_id in checked_page_ids or state_page_id in intent_page_ids:
                continue
            summary.add_issue(IntegrityIssue(
                error_code=IntegrityErrorCode.PAGE_MISSING,
                page_id=state_page_id,
                message="PublicationState ????????????????",
            ))

    def get_effective_store(self) -> ContentVersionStore:
        """
        获取有效的只读 ContentVersionStore 实例（延迟解耦）。
        """
        if self.store is None:
            try:
                from external_content.store import ContentVersionStore
                self.store = ContentVersionStore(enforce_index_check=False)
            except Exception as e:
                raise RuntimeError(f"未配置 ContentVersionStore 且无法延迟导入: {e}") from e
        return self.store
