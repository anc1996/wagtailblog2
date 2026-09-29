"""
Wagtail Page 正文外置通用协议 - Page Mixin 定义。

页面模型通过继承此类并声明模型级 content_storage = {"body": "mongo"} 显式接入外置正文通用协议。
未继承本 Mixin 的普通页面（如 HomePage、PortfolioPage）严格保持原生存储行为。

生命周期能力：
1. 受控保存：显式 prepare_revision 将不可变正文持久化至 MongoDB (majority确认)；
   Revision 中外置字段保存为空哨兵，并注入 _external_content_manifest；
2. 普通序列化纯只读且零 MongoDB 写入副作用，保留原生内存值；
3. 反序列化 (from_serializable_data) 严格保留 check_fks/strict_fks，且失败关闭 (Fail-Closed)，
   任何 Manifest 缺失、版本损坏或身份不匹配严格阻断；
4. 彻底杜绝 overwrite 覆盖竞态，对外置页面一律安全新建 Revision；
5. 原生与后台 PublishPageRevisionAction 发布时原子核对正文，损坏时拦截保存并回滚，live 绝不提前推进；
6. 严格拦截非受控上下文直接调用 save() 持久化外置正文修改，区分受控初始化、元数据更新与非法正文直存；
7. 取消发布 (unpublish) 递增公开代次并清空正式指针；
8. 前台渲染 (serve) 自动从正式 Manifest 加载并还原可渲染正文，缺失时失败关闭。
"""

import json
import logging
import threading
from typing import Any, ClassVar, Dict, Optional

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import (
    ExternalContentOperationBlockedError,
    DirectSaveForbiddenError,
    ManifestError,
    ManifestIdentityMismatchError,
    PublicationBodyUnavailableError,
    PublicationRevisionInvalidError,
    RevisionOverwriteForbiddenError,
)
from external_content.manifest import ExternalContentManifest
from external_content.models import ExternalContentPublicationState
from external_content.publication import (
    ExternalContentPublicationService,
    _controlled_save_context,
    validate_scheduled_revision,
)
from external_content.registry import get_storage_policy_for_model
from external_content.store import ContentVersionStore

logger = logging.getLogger(__name__)

# 线程局部受控序列化上下文与受控保存上下文
_controlled_serialization_context = threading.local()
# _controlled_save_context imported from publication


class ExternalContentPageMixin:
    """
    Wagtail Page 正文外置通用能力混入基类。
    """

    content_storage: ClassVar[Optional[Dict[str, str]]] = None
    content_storage_schema: ClassVar[int] = 1

    def serializable_data(self) -> Dict[str, Any]:
        """
        获取序列化字典。

        严格契约：
        1. 普通序列化（比对、导出等）纯只读，完整保留原生值，绝不触发 MongoDB 写入副作用；
        2. 占位值与完整 Manifest 快照仅在受控保存上下文 (_controlled_serialization_context) 内生成，
           确保外置字段替换为空哨兵，同时注入 _external_content_manifest。
        """
        data = super().serializable_data()  # type: ignore[misc]

        manifest = getattr(_controlled_serialization_context, "manifest", None)
        if manifest is not None:
            policy = get_storage_policy_for_model(type(self), allow_record_new=False)
            if policy:
                for field_name, field_cfg in policy.external_fields.items():
                    data[field_name] = field_cfg.codec.get_empty_sentinel()

            data["_external_content_manifest"] = manifest.to_dict()

        return data

    @classmethod
    def from_serializable_data(
        cls: type[Any],
        data: Dict[str, Any],
        check_fks: bool = True,
        strict_fks: bool = False,
    ) -> Any:
        """
        从 Wagtail Revision 序列化数据恢复页面实例，并原子还原 MongoDB 历史正文版本。

        严格契约（失败关闭 Fail-Closed）：
        1. 完整透传 Wagtail 原生 check_fks 与 strict_fks 参数；
        2. 外置协议页面若缺少 _external_content_manifest 或格式非法，严禁静默回退，直接抛出异常；
        3. 逐字段核验 6 项身份键与 SHA-256，任何身份不匹配或正文损坏直接报错阻断；
        4. 保证历史预览、恢复历史与页面比对获得精确的历史版本正文。
        """
        obj = super().from_serializable_data(data, check_fks=check_fks, strict_fks=strict_fks)  # type: ignore[misc]

        policy = get_storage_policy_for_model(cls, allow_record_new=False)
        if policy and policy.external_fields:
            raw_manifest = data.get("_external_content_manifest")
            if not raw_manifest or not isinstance(raw_manifest, dict):
                raise PublicationRevisionInvalidError(
                    f"外置正文页面 ({cls._meta.label_lower}) 历史还原失败：Revision 序列化数据缺少 _external_content_manifest 或格式非法，拒绝未受控恢复"
                )

            try:
                manifest = ExternalContentManifest.from_dict(raw_manifest)
            except ManifestError as exc:
                raise PublicationRevisionInvalidError(f"从 Revision 还原 Manifest 失败: {exc}") from exc

            # 身份校验
            expected_model_label = obj._meta.label_lower
            if manifest.model_label != expected_model_label:
                raise ManifestIdentityMismatchError(
                    f"Manifest 所属模型 ({manifest.model_label}) 与页面模型 ({expected_model_label}) 不匹配"
                )
            expected_page_id = obj.pk or data.get("id") or data.get("pk")
            if expected_page_id and manifest.page_id != expected_page_id:
                raise ManifestIdentityMismatchError(
                    f"Manifest 所属页面 ID ({manifest.page_id}) 与目标页面 ID ({expected_page_id}) 不匹配"
                )

            coordinator = ExternalContentCoordinator()
            coordinator.load_external_content_from_manifest(obj, manifest)

            obj._external_content_loaded_hashes = {
                field_name: field_ptr.payload_sha256 for field_name, field_ptr in manifest.fields.items()
            }

        return obj

    def save_revision(
        self,
        *args: Any,
        overwrite_revision: Any = None,
        **kwargs: Any,
    ) -> Any:
        """
        受控准备并保存 Wagtail Revision。

        步骤：
        1. 检查持久化主键 pk，阻断无主键页面的版本生成；
        2. 对外置页面一律安全新建 Revision（强制 overwrite_revision = None），彻底杜绝覆盖竞态与不可变冲突；
        3. 显式调用 coordinator 准备各外置字段不可变版本并持久化写入 MongoDB (majority写关注)；
        4. 进入受控序列化上下文，调用 super().save_revision()，注入 _external_content_manifest；
        5. 在同一 MySQL 事务内更新 ExternalContentPublicationState 的草稿凭证。
        """
        if not self.pk:
            raise RuntimeError("外置正文页面在调用 save_revision 前必须具备持久化主键 ID (pk)，以防生成无主键孤儿版本")

        if overwrite_revision is not None:
            logger.info(
                "external_content_overwrite_revision_disabled page_id=%s rev_id=%s creating_new_revision",
                self.pk,
                getattr(overwrite_revision, "pk", None),
            )
            overwrite_revision = None

        coordinator = ExternalContentCoordinator()

        # 准备不可变正文并写入 MongoDB
        manifest = coordinator.prepare_external_content_for_revision(self)

        with transaction.atomic():
            _controlled_serialization_context.manifest = manifest
            _controlled_save_context.in_controlled_save = True
            try:
                revision = super().save_revision(
                    *args,
                    overwrite_revision=overwrite_revision,
                    **kwargs,
                )  # type: ignore[misc]
            finally:
                _controlled_serialization_context.manifest = None
                _controlled_save_context.in_controlled_save = False

            # 同步更新草稿状态（仅当尚未记录草稿或目标 Revision 大于等于已记录草稿版本时推进，杜绝并发乱序倒退草稿）
            state, _ = ExternalContentPublicationState.objects.select_for_update().get_or_create(
                page_id=self.pk,
                defaults={"model_label": self._meta.label_lower, "publication_generation": 0},
            )
            update_fields = ["updated_at"]
            if state.draft_revision_id is None or revision.pk >= state.draft_revision_id:
                state.draft_revision_id = revision.pk
                state.draft_manifest = manifest.to_dict()
                update_fields.extend(["draft_revision_id", "draft_manifest"])
            state.save(update_fields=update_fields)

            self._external_content_loaded_hashes = {
                field_name: field_ptr.payload_sha256 for field_name, field_ptr in manifest.fields.items()
            }

            logger.info(
                "external_content_revision_saved page_id=%s revision_id=%s",
                self.pk,
                revision.pk,
            )
            return revision

    def save(self, *args: Any, **kwargs: Any) -> Any:
        """
        保存页面到 MySQL。

        规范与安全保护：
        1. 针对直接调用 PublishPageRevisionAction 导致 live 推进的底层拦截：
           若检测到即将被保存为 live=True 且存在 live_revision_id，
           在写入数据库前必须先校验 Mongo 不可变版本的哈希与完整性；
        2. 严格拦截非受控的直接 save()：
           若非初始建行且非受控保存/发布，直接修改外置正文并 save() 将严格抛出 DirectSaveForbiddenError；
        3. 占位保护：在调用 super().save() 写入数据库前，将外置字段替换为空哨兵，
           并在 finally 中恢复内存真实属性，避免将大段正文持久化到关系数据库中。
        """
        # G4 前置保护：阻断别名页面与受控外置页面的别名关系保存
        if getattr(self, "alias_of_id", None) or getattr(self, "alias_of", None):
            raise ExternalContentOperationBlockedError(
                f"{self._meta.label_lower} (id={self.pk}): 正文外置通用协议 G4 阶段尚未就绪，别名创建操作已被安全阻断。"
            )

        policy = get_storage_policy_for_model(type(self), allow_record_new=False)
        if not policy or not policy.external_fields:
            return super().save(*args, **kwargs)  # type: ignore[misc]

        update_fields = kwargs.get("update_fields")
        in_controlled_save = getattr(_controlled_save_context, "in_controlled_save", False)
        in_publication_action = getattr(_controlled_save_context, "in_publication_action", False)
        is_initial_insert = bool(getattr(self, "_state", None) and self._state.adding) or not self.pk

        # 针对由 PublishPageRevisionAction 驱动的 live 推进进行底层校验
        if self.pk and in_publication_action and getattr(self, "live", False) and getattr(self, "live_revision_id", None):
            ExternalContentPublicationService.lock_and_validate_revision(
                self.pk,
                self.live_revision_id,
            )

        # 非受控上下文中禁止直接通过 save() 篡改持久化外置正文字段（已发布页面直接修改正文或清空为 [] 均会被拦截）
        if not is_initial_insert and not in_controlled_save and not in_publication_action:
            loaded_hashes = getattr(self, "_external_content_loaded_hashes", {}) or {}
            for field_name, field_cfg in policy.external_fields.items():
                if update_fields is None or field_name in update_fields:
                    current_val = getattr(self, field_name, None)
                    try:
                        _, current_hash = field_cfg.codec.encode(current_val)
                    except Exception:
                        current_hash = None
                    if field_name not in loaded_hashes:
                        # ORM 重载的外置字段仅含 SQL 空哨兵，以该哨兵哈希作为未加载正文的保存基准。
                        _, loaded_hashes[field_name] = field_cfg.codec.encode(field_cfg.codec.get_empty_sentinel())
                    if current_hash != loaded_hashes.get(field_name):
                        raise DirectSaveForbiddenError(
                            f"外置正文页面 (page_id={self.pk}) 检测到正文字段 '{field_name}' 被直接修改，禁止调用 save()。"
                            f"正文修改必须通过 save_revision() 生成不可变修订版本与 MongoDB 记录。"
                        )

        # 检查是否需要对已登记外置字段执行占位替换写入 SQL
        staged_backup: Dict[str, Any] = {}
        try:
            for field_name, field_cfg in policy.external_fields.items():
                if update_fields is None or field_name in update_fields:
                    current_val = getattr(self, field_name, None)
                    staged_backup[field_name] = current_val
                    setattr(self, field_name, field_cfg.codec.get_empty_sentinel())

            return super().save(*args, **kwargs)  # type: ignore[misc]
        finally:
            for field_name, original_val in staged_backup.items():
                setattr(self, field_name, original_val)
            if is_initial_insert:
                initial_hashes = getattr(self, "_external_content_loaded_hashes", {}) or {}
                for field_name, field_cfg in policy.external_fields.items():
                    val = getattr(self, field_name, None)
                    try:
                        _, val_hash = field_cfg.codec.encode(val)
                        initial_hashes[field_name] = val_hash
                    except Exception:
                        pass
                self._external_content_loaded_hashes = initial_hashes

    def publish(self, revision: Any, *args: Any, **kwargs: Any) -> Any:
        """
        显式发布入口拦截。

        在同一 MySQL 事务内锁定 Revision 并核验 Mongo 正文；非排期发布原子推进正式指针。
        发布成功后主动同步当前实例的 live 与 live_revision 内存状态。
        具备严格幂等性：重复发布同一 Revision 不会重复递增公开代次。
        """
        if revision is None or getattr(revision, "pk", None) is None:
            raise ValueError("发布时必须提供有效的 Revision 实例")

        # 若为排期到期执行，执行排期校验
        is_scheduled_execution = kwargs.get("log_action") == "wagtail.publish.scheduled"
        if is_scheduled_execution:
            validate_scheduled_revision(revision)

        with transaction.atomic():
            candidate = ExternalContentPublicationService.lock_and_validate_revision(
                self.pk,
                revision.pk,
            )

            # 严格解析排期时间
            raw_content = revision.content if isinstance(revision.content, dict) else json.loads(revision.content)
            approved_go_live_at = getattr(revision, "approved_go_live_at", None)
            page_go_live_at = getattr(self, "go_live_at", None) or raw_content.get("go_live_at")
            target_go_live = approved_go_live_at or page_go_live_at

            is_future_schedule = False

            if target_go_live is not None and not is_scheduled_execution:
                if isinstance(target_go_live, str):
                    parsed_dt = parse_datetime(target_go_live)
                    if parsed_dt and parsed_dt > timezone.now():
                        is_future_schedule = True
                elif hasattr(target_go_live, "year") and target_go_live > timezone.now():
                    is_future_schedule = True

            # 幂等推进正式指针：仅当当前正式指针尚未指向该 Revision 或 Manifest 发生变动时才推进
            if is_future_schedule:
                self.go_live_at = target_go_live
            else:
                state = candidate.state
                if (
                    state.published_revision_id != revision.pk
                    or state.published_manifest != candidate.manifest.to_dict()
                ):
                    ExternalContentPublicationService.promote_published_candidate(candidate)

            _controlled_save_context.in_controlled_save = True
            try:
                result = super().publish(revision, *args, **kwargs)  # type: ignore[misc]
            finally:
                _controlled_save_context.in_controlled_save = False

            # 内存状态同步
            if not is_future_schedule:
                self.live = True
                self.live_revision = revision
                self.has_unpublished_changes = not revision.is_latest_revision()

            logger.info("external_content_page_published page_id=%s revision_id=%s scheduled=%s", self.pk, revision.pk, is_future_schedule)
            return result

    def unpublish(self, *args: Any, commit: bool = True, **kwargs: Any) -> Any:
        """
        取消发布入口。在同一 MySQL 事务内递增公开代次并清空正式指针。
        若 commit=False（如预览或未持久化流程），禁止提前推进公开代次与清空指针。
        """
        if not getattr(self, "live", False) and self.pk and commit:
            db_live = type(self).objects.filter(pk=self.pk).values_list("live", flat=True).first()
            if db_live:
                self.live = True

        with transaction.atomic():
            _controlled_save_context.in_controlled_save = True
            try:
                result = super().unpublish(*args, commit=commit, **kwargs)  # type: ignore[misc]
            finally:
                _controlled_save_context.in_controlled_save = False

            if commit:
                self.live = False
                self.live_revision = None
                ExternalContentPublicationService.advance_unpublish_generation(self.pk)
                ExternalContentPublicationService.clear_published_pointer(self.pk)
                logger.info("external_content_page_unpublished page_id=%s", self.pk)

            return result

    def load_published_content(self) -> None:
        """
        从正式发布状态 (published_manifest) 加载外置正文字段至当前实例内存中。
        严格失败关闭：若已发布页面缺失有效状态，禁止静默展示空白正文。
        """
        if not self.pk:
            return

        policy = get_storage_policy_for_model(type(self), allow_record_new=False)
        if not policy or not policy.external_fields:
            return

        state = ExternalContentPublicationState.objects.filter(page_id=self.pk).first()
        if not state or not state.published_manifest:
            raise PublicationBodyUnavailableError(
                f"已发布的页面 (page_id={self.pk}) 缺少有效的正式发布状态或 published_manifest，拒绝展示空白正文"
            )

        try:
            manifest = ExternalContentManifest.from_dict(state.published_manifest)
        except Exception as exc:
            raise PublicationBodyUnavailableError(
                f"已发布页面的 published_manifest 格式非法或损坏: {exc}"
            ) from exc

        coordinator = ExternalContentCoordinator()
        coordinator.load_external_content_from_manifest(self, manifest)
        self._external_content_loaded_hashes = {
            field_name: field_ptr.payload_sha256 for field_name, field_ptr in manifest.fields.items()
        }

    def serve(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """
        前台页面请求渲染。在渲染前自动从 Mongo 恢复正式已发布正文。
        """
        if self.live:
            self.load_published_content()
        return super().serve(request, *args, **kwargs)  # type: ignore[misc]

    def copy(self, *args, **kwargs):
        """G4 前置保护：阻断正文外置页面的直接复制操作。"""
        raise ExternalContentOperationBlockedError(
            f"{self._meta.label_lower} (id={self.pk}): 正文外置通用协议 G4 阶段尚未就绪，此操作已被安全阻断。"
        )

    def create_alias(self, *args, **kwargs):
        """G4 前置保护：阻断正文外置页面的别名创建操作。"""
        raise ExternalContentOperationBlockedError(
            f"{self._meta.label_lower} (id={self.pk}): 正文外置通用协议 G4 阶段尚未就绪，此操作已被安全阻断。"
        )

    def delete(self, *args, **kwargs):
        """G4 前置保护：阻断正文外置页面的物理删除操作以防孤儿数据。"""
        raise ExternalContentOperationBlockedError(
            f"{self._meta.label_lower} (id={self.pk}): 正文外置通用协议 G4 阶段尚未就绪，此操作已被安全阻断。"
        )

    def permissions_for_user(self, user: Any) -> Any:
        """
        重写页面用户权限检测：G4 前置保护期内，硬性关闭 copy 权限，阻断原生 CopyPageAction。
        """
        perms = super().permissions_for_user(user)  # type: ignore[misc]
        perms.can_copy_to = lambda parent_page=None, recursive=False: False
        return perms
