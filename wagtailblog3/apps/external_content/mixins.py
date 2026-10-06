"""
Wagtail Page 正文外置协议的通用页面混入。

外置正文仅通过受控 Revision、发布与复制流程写入 MongoDB；MySQL Page 和
Revision 保留空哨兵及 Manifest 指针。读取、发布、别名同步和复制都必须校验
页面身份及不可变正文哈希，失败时拒绝继续，不回退为空正文。
"""
import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone as datetime_timezone
from typing import Any, ClassVar, Dict, Iterator, Optional

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import (
    ContentStoreError,
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
from external_content.locking import (
    check_page_write_not_blocked,
    lock_page_lifecycle_hierarchy,
    preserve_threadlocal_context,
)
from external_content.registry import get_storage_policy_for_model
from external_content.store import ContentVersionStore

logger = logging.getLogger(__name__)

# 受控上下文必须在退出时恢复，避免嵌套生命周期操作向后续请求泄漏权限。
_controlled_serialization_context = threading.local()
_controlled_copy_context = threading.local()
_controlled_alias_context = threading.local()
_external_content_access_context = threading.local()


@contextmanager
def controlled_external_content_save() -> Iterator[None]:
    """
    标记 Wagtail 后台表单首次 Page.save() 为受控初始化。
    该中间写入仅保存页面元数据；正文版本仍由 save_revision() 持久化。
    """
    with preserve_threadlocal_context(_controlled_save_context, "in_controlled_save", True):
        yield


@contextmanager
def _suppress_external_content_autoload() -> Iterator[None]:
    """原生复制与 ORM 重载期间暂缓加载，避免读取中间态正式指针。"""
    with preserve_threadlocal_context(
        _external_content_access_context,
        "suppress_autoload",
        True,
    ):
        yield


class ExternalContentPageMixin:
    """Wagtail Page 正文外置通用能力混入基类。"""

    content_storage: ClassVar[Optional[Dict[str, str]]] = None
    content_storage_schema: ClassVar[int] = 1

    def __getattribute__(self, name: str) -> Any:
        external_fields = getattr(type(self), "content_storage", None) or {}
        instance_state = object.__getattribute__(self, "__dict__")
        if (
            name in external_fields
            and external_fields.get(name) == "mongo"
            and not getattr(_external_content_access_context, "suppress_autoload", False)
            and not getattr(_controlled_copy_context, "in_copy", False)
            and not getattr(_controlled_alias_context, "in_alias", False)
            and instance_state.get("live", False)
            and instance_state.get("live_revision_id")
            and not instance_state.get("_external_content_loading", False)
            and not instance_state.get("_external_content_loaded_hashes")
        ):
            object.__setattr__(self, "_external_content_loading", True)
            try:
                object.__getattribute__(self, "load_published_content")()
            finally:
                object.__setattr__(self, "_external_content_loading", False)
        return super().__getattribute__(name)

    def serializable_data(self) -> Dict[str, Any]:
        """
        普通序列化保持只读；只有受控 Revision 保存才写入空哨兵和 Manifest。
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
        从 Wagtail Revision 恢复页面及其不可变正文。
        缺失 Manifest、身份不匹配或正文损坏时失败关闭，并透传 Wagtail 外键校验参数。
        """
        obj = super().from_serializable_data(data, check_fks=check_fks, strict_fks=strict_fks)  # type: ignore[misc]

        policy = get_storage_policy_for_model(cls, allow_record_new=False)
        if policy and policy.external_fields:
            raw_manifest = data.get("_external_content_manifest")
            if not raw_manifest or not isinstance(raw_manifest, dict):
                raise PublicationRevisionInvalidError(
                    f"External-content revision restore failed for {cls._meta.label_lower}: missing or invalid _external_content_manifest."
                )

            try:
                manifest = ExternalContentManifest.from_dict(raw_manifest)
            except ManifestError as exc:
                raise PublicationRevisionInvalidError(f"Revision Manifest 恢复失败: {exc}") from exc

            # 身份校验
            expected_model_label = obj._meta.label_lower
            if manifest.model_label != expected_model_label:
                raise ManifestIdentityMismatchError(
                    f"Manifest 模型身份 {manifest.model_label} 与目标模型 {expected_model_label} 不匹配"
                )
            expected_page_id = obj.pk or data.get("id") or data.get("pk")
            in_controlled_copy = getattr(_controlled_copy_context, "in_copy", False)
            if expected_page_id and manifest.page_id != expected_page_id and not in_controlled_copy:
                raise ManifestIdentityMismatchError(
                    f"Manifest 页面身份 {manifest.page_id} 与目标页面 {expected_page_id} 不匹配"
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
        """在生命周期锁内创建不可变正文版本，禁止覆盖既有 Revision。"""
        if not self.pk:
            raise RuntimeError("外置正文页面调用 save_revision() 前必须具备持久化主键 ID")
        if overwrite_revision is not None:
            overwrite_revision = None

        check_page_write_not_blocked(self.pk, for_update=False)
        with transaction.atomic():
            lock_page_lifecycle_hierarchy(self.pk, revision_ids=None, check_write=True)
            coordinator = ExternalContentCoordinator(store=getattr(self, "_test_content_store", None))
            manifest = coordinator.prepare_external_content_for_revision(self)
            with preserve_threadlocal_context(_controlled_serialization_context, "manifest", manifest):
                with preserve_threadlocal_context(_controlled_save_context, "in_controlled_save", True):
                    revision = super().save_revision(
                        *args, overwrite_revision=overwrite_revision, **kwargs
                    )
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
                name: entry.payload_sha256 for name, entry in manifest.fields.items()
            }
            return revision

    def save(self, *args: Any, **kwargs: Any) -> Any:
        """
        保存页面元数据前校验发布指针与正文身份。正文修改必须通过 save_revision()；
        外置字段写入 MySQL 时替换为空哨兵，并在 finally 中恢复实例内存值。
        """
        # 别名关系由生命周期入口维护，禁止普通保存绕过源页面身份校验。
        alias_source_id = getattr(self, "alias_of_id", None)
        authorized_alias_save = bool(
            alias_source_id
            and getattr(_controlled_alias_context, "in_alias", False)
            and alias_source_id
            == getattr(_controlled_alias_context, "alias_update_source_page_id", None)
        )
        if alias_source_id and not authorized_alias_save:
            raise ExternalContentOperationBlockedError(
                f"{self._meta.label_lower} (id={self.pk}): 外置正文别名关系只能通过受控生命周期入口保存。"
            )

        policy = get_storage_policy_for_model(type(self), allow_record_new=False)
        if not policy or not policy.external_fields:
            return super().save(*args, **kwargs)  # type: ignore[misc]

        if self.pk:
            check_page_write_not_blocked(self.pk, for_update=False)

        update_fields = kwargs.get("update_fields")
        in_controlled_save = getattr(_controlled_save_context, "in_controlled_save", False)
        in_publication_action = getattr(_controlled_save_context, "in_publication_action", False)
        is_initial_insert = bool(getattr(self, "_state", None) and self._state.adding) or not self.pk
        has_no_revision = False
        if self.pk:
            from wagtail.models import Revision
            has_no_revision = not Revision.objects.filter(
                object_id=self.pk, content_type__model=self._meta.model_name
            ).exists()
        is_initial_insert = is_initial_insert or has_no_revision

        # Alias 暂用源 Revision；仅当前同步源的直属 Alias 交由专用流程校验。
        if (
            self.pk
            and in_publication_action
            and getattr(self, "live", False)
            and getattr(self, "live_revision_id", None)
            and not authorized_alias_save
        ):
            ExternalContentPublicationService.lock_and_validate_revision(
                self.pk,
                self.live_revision_id,
            )

        # ORM 空哨兵不是正文基线；正式内容加载后才能判断是否发生非法直写。
        if (
            not is_initial_insert
            and not in_controlled_save
            and not in_publication_action
            and not update_fields
            and getattr(self, "live", False)
            and getattr(self, "live_revision_id", None)
            and not (getattr(self, "_external_content_loaded_hashes", {}) or {})
        ):
            self.load_published_content()

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
                        # 未加载的草稿字段以 SQL 空哨兵为基线，不推断缺失正文。
                        _, loaded_hashes[field_name] = field_cfg.codec.encode(field_cfg.codec.get_empty_sentinel())
                    if current_hash != loaded_hashes.get(field_name):
                        raise DirectSaveForbiddenError(
                            f"外置正文页面 (page_id={self.pk}) 的字段 {field_name} 被直接修改，禁止调用 save()。"
                            f"正文修改必须通过 save_revision() 创建不可变版本并写入 MongoDB。"
                        )

        # SQL 仅持久化空哨兵，真实正文必须在异常退出时同样恢复到内存。
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
        在同一 MySQL 事务内锁定并校验 Revision 与 Mongo 正文，再推进正式发布指针。
        排期发布不得提前公开；重复发布同一 Revision 保持幂等。
        """
        if revision is None or getattr(revision, "pk", None) is None:
            raise ValueError("发布时必须提供有效的 Revision 实例")

        # 到期发布仍须校验排期凭证，不能跳过正式发布门禁。
        is_scheduled_execution = kwargs.get("log_action") == "wagtail.publish.scheduled"
        if is_scheduled_execution:
            validate_scheduled_revision(revision)

        with transaction.atomic():
            candidate = ExternalContentPublicationService.lock_and_validate_revision(
                self.pk,
                revision.pk,
                store=getattr(self, "_test_content_store", None),
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

            # 排期不提前推进正式指针；重复发布仅在指针或 Manifest 改变时推进。
            if is_future_schedule:
                self.go_live_at = target_go_live
            else:
                state = candidate.state
                if (
                    state.published_revision_id != revision.pk
                    or state.published_manifest != candidate.manifest.to_dict()
                ):
                    ExternalContentPublicationService.promote_published_candidate(candidate)

            with preserve_threadlocal_context(_controlled_save_context, "in_controlled_save", True):
                result = super().publish(revision, *args, **kwargs)  # type: ignore[misc]

            # 仅实际发布同步内存状态，排期页面仍保持原有公开状态。
            if not is_future_schedule:
                self.live = True
                self.live_revision = revision
                self.has_unpublished_changes = not revision.is_latest_revision()

            logger.info("external_content_page_published page_id=%s revision_id=%s scheduled=%s", self.pk, revision.pk, is_future_schedule)
            return result

    def unpublish(self, *args: Any, commit: bool = True, **kwargs: Any) -> Any:
        """
        在事务内取消发布并清除正式正文指针。commit=False 时不推进公开代次，
        避免预览或未提交操作改变读者可见状态。
        """
        if not getattr(self, "live", False) and self.pk and commit:
            db_live = type(self).objects.filter(pk=self.pk).values_list("live", flat=True).first()
            if db_live:
                self.live = True

        previous_state = None
        if not commit and self.pk:
            previous_state = ExternalContentPublicationState.objects.filter(
                page_id=self.pk,
            ).values(
                "published_revision_id",
                "published_manifest",
                "published_at",
                "publication_generation",
            ).first()

        with transaction.atomic():
            with preserve_threadlocal_context(_controlled_save_context, "in_controlled_save", True):
                result = super().unpublish(*args, commit=commit, **kwargs)  # type: ignore[misc]

            if commit:
                self.live = False
                self.live_revision = None
                ExternalContentPublicationService.advance_unpublish_generation(self.pk)
                ExternalContentPublicationService.clear_published_pointer(self.pk)
                logger.info("external_content_page_unpublished page_id=%s", self.pk)
            elif previous_state is not None:
                state = ExternalContentPublicationState.objects.select_for_update().get(page_id=self.pk)
                state.published_revision_id = previous_state["published_revision_id"]
                state.published_manifest = previous_state["published_manifest"]
                state.published_at = previous_state["published_at"]
                state.publication_generation = previous_state["publication_generation"]
                state.save(
                    update_fields=[
                        "published_revision_id",
                        "published_manifest",
                        "published_at",
                        "publication_generation",
                        "updated_at",
                    ]
                )

            return result

    def load_published_content(self) -> None:
        """
        依据正式 Manifest 将不可变正文恢复到页面实例。
        已发布页面缺少状态、版本或身份校验失败时拒绝返回空正文。
        """
        if not self.pk:
            return

        policy = get_storage_policy_for_model(type(self), allow_record_new=False)
        if not policy or not policy.external_fields:
            return

        state = ExternalContentPublicationState.objects.filter(page_id=self.pk).first()
        if not state:
            if not getattr(self, "live_revision_id", None):
                return
            raise PublicationBodyUnavailableError(
                f"已发布的页面 (page_id={self.pk}) 缺少正式发布状态，拒绝展示空白正文"
            )
        if not state.published_manifest:
            if not state.published_revision_id and not getattr(self, "live_revision_id", None):
                return
            raise PublicationBodyUnavailableError(
                f"已发布页面 (page_id={self.pk}) 缺少有效 published_manifest，拒绝展示空白正文。"
            )

        try:
            manifest = ExternalContentManifest.from_dict(state.published_manifest)
        except Exception as exc:
            raise PublicationBodyUnavailableError(
                f"已发布页面的 published_manifest 格式非法或损坏: {exc}"
            ) from exc

        coordinator = ExternalContentCoordinator(store=getattr(self, "_test_content_store", None))
        try:
            coordinator.load_external_content_from_manifest(self, manifest)
        except ContentStoreError as exc:
            raise PublicationBodyUnavailableError(
                f"已发布页面 (page_id={self.pk}) 的外置正文不可用: {exc}"
            ) from exc
        self._external_content_loaded_hashes = {
            field_name: field_ptr.payload_sha256 for field_name, field_ptr in manifest.fields.items()
        }

    def update_aliases(
        self,
        *,
        revision: Any = None,
        _content: Any = None,
        _updated_ids: Any = None,
    ) -> Any:
        """排期不提前公开 Alias；同步身份上下文必须逐层精确恢复。"""
        from external_content.copying import prepare_alias_update, sync_external_content_aliases

        with transaction.atomic():
            if self._defer_first_scheduled_alias_update(revision, _content):
                return None
            canonical_revision, canonical_content = prepare_alias_update(
                self, revision=revision, content=_content
            )
            with preserve_threadlocal_context(_controlled_alias_context, "in_alias", True):
                with preserve_threadlocal_context(
                    _controlled_alias_context,
                    "alias_update_source_page_id",
                    self.pk,
                ):
                    sync_external_content_aliases(self, revision=canonical_revision)
                    return super().update_aliases(
                        revision=canonical_revision,
                        _content=canonical_content,
                        _updated_ids=_updated_ids,
                    )

    update_aliases.alters_data = True

    def _defer_first_scheduled_alias_update(self, revision: Any, content: Any) -> bool:
        """仅真实首次排期可以暂缓同步，不能凭线程开关豁免正式来源校验。"""
        from external_content.copying import (
            _alias_snapshot_key,
            _manifest_from_revision,
            _validate_revision_manifest,
        )

        if (
            not getattr(_controlled_save_context, "in_publication_action", False)
            or getattr(self, "live", False)
            or getattr(self, "live_revision_id", None)
            or revision is None
            or not getattr(revision, "pk", None)
        ):
            return False

        locks = lock_page_lifecycle_hierarchy(
            self.pk, [revision.pk], check_write=True, nowait=True
        )
        source = locks.page
        if source.live or source.live_revision_id or source.alias_of_id:
            return False
        if len(locks.revisions) != 1:
            raise PublicationRevisionInvalidError("首次排期 Alias 同步缺少归属正确的 Revision")
        stored_revision = locks.revisions[0]
        approved_at = stored_revision.approved_go_live_at
        if approved_at is None:
            return False
        if not isinstance(approved_at, datetime) or timezone.is_naive(approved_at):
            raise PublicationRevisionInvalidError("首次排期批准时间缺少有效时区")
        if approved_at <= timezone.now():
            return False

        snapshot = stored_revision.content
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except (TypeError, ValueError) as exc:
                raise PublicationRevisionInvalidError("首次排期 Revision 快照非法") from exc
        if not isinstance(snapshot, dict):
            raise PublicationRevisionInvalidError("首次排期 Revision 快照非法")
        scheduled_at = snapshot.get("go_live_at")
        if scheduled_at is None:
            # Wagtail 允许只设置批准时间；受控 Action 已将锁内批准写入运行时页面，
            # 必须绑定该时间而非更改不可变快照或凭线程开关豁免来源校验。
            scheduled_at = getattr(self, "go_live_at", None)
        if isinstance(scheduled_at, str):
            scheduled_at = parse_datetime(scheduled_at)
        provided_approved_at = getattr(revision, "approved_go_live_at", None)
        if (
            not isinstance(scheduled_at, datetime)
            or timezone.is_naive(scheduled_at)
            or not isinstance(provided_approved_at, datetime)
            or timezone.is_naive(provided_approved_at)
        ):
            raise PublicationRevisionInvalidError("首次排期时间缺少有效时区")
        # Revision JSON 只保留毫秒；等价时区和亚毫秒损失不能被误判为篡改。
        scheduled_key = scheduled_at.astimezone(datetime_timezone.utc).replace(
            microsecond=scheduled_at.microsecond // 1000 * 1000
        )
        approved_key = approved_at.astimezone(datetime_timezone.utc).replace(
            microsecond=approved_at.microsecond // 1000 * 1000
        )
        provided_approved_key = provided_approved_at.astimezone(datetime_timezone.utc).replace(
            microsecond=provided_approved_at.microsecond // 1000 * 1000
        )
        state = locks.state
        if (
            scheduled_key != approved_key
            or provided_approved_key != approved_key
            or stored_revision.content_type_id != source.content_type_id
            or revision.content_type_id != stored_revision.content_type_id
            or revision.base_content_type_id != stored_revision.base_content_type_id
            or str(revision.object_id) != str(stored_revision.object_id)
            or _alias_snapshot_key(revision.content) != _alias_snapshot_key(snapshot)
            or state is None
            or state.model_label != self._meta.label_lower
            or state.published_revision_id is not None
            or state.published_manifest
            or state.source_page_id is not None
            or state.source_revision_id is not None
            or (content is not None and _alias_snapshot_key(content) != _alias_snapshot_key(snapshot))
        ):
            raise PublicationRevisionInvalidError("首次排期状态或快照不一致，拒绝 Alias 同步")

        coordinator = ExternalContentCoordinator(store=getattr(self, "_test_content_store", None))
        _validate_revision_manifest(
            source.specific,
            stored_revision,
            _manifest_from_revision(stored_revision),
            coordinator.store,
            validate_business=True,
        )
        return True

    def refresh_from_db(self, *args: Any, **kwargs: Any) -> Any:
        with _suppress_external_content_autoload():
            result = super().refresh_from_db(*args, **kwargs)
        if getattr(self, "live", False):
            self.load_published_content()
        return result

    def serve(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """
        前台渲染前加载并校验正式发布正文。
        """
        if self.live:
            self.load_published_content()
        return super().serve(request, *args, **kwargs)  # type: ignore[misc]

    def copy(self, *args: Any, **kwargs: Any) -> Any:
        """锁定源页面并加载正文后复制，目标指针由专用协调器重绑定。"""
        from external_content.copying import duplicate_external_content_for_copy

        with transaction.atomic():
            lock_page_lifecycle_hierarchy(self.pk, revision_ids=None, check_write=True)
            if getattr(self, "live", False):
                self.load_published_content()
            with preserve_threadlocal_context(_controlled_copy_context, "in_copy", True):
                with _suppress_external_content_autoload():
                    target_page = super().copy(*args, **kwargs)
            duplicate_external_content_for_copy(
                source_page=self,
                target_page=target_page,
                keep_live=kwargs.get("keep_live", False),
                copy_revisions=kwargs.get("copy_revisions", True),
                user=kwargs.get("user"),
            )
            return target_page

    def create_alias(self, *args: Any, **kwargs: Any) -> Any:
        """Alias Action 统一负责围栏和各递归层初始化，禁止外围宽授权窗口。"""
        with transaction.atomic():
            return super().create_alias(*args, **kwargs)
