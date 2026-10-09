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
from dataclasses import replace
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
    _restore_failed_publication_memory,
    validate_scheduled_revision,
)
from external_content.locking import (
    check_page_write_not_blocked,
    lock_page_lifecycle_hierarchy,
    preserve_threadlocal_context,
)
from external_content.registry import ExternalFieldState, get_storage_policy_for_model
from external_content.forms import ExternalContentPageForm, install_external_content_create_bridge
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
    base_form_class = ExternalContentPageForm

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # CreateView.dispatch 在 POST 前构造 page，确保首次创建也在外层 SQL 事务内。
        install_external_content_create_bridge()
        # Django 初始化/ORM 构造不是用户清空；from_db 随后建立来源。
        object.__setattr__(self, "_external_content_internal_assignment", True)
        try:
            super().__init__(*args, **kwargs)
        finally:
            object.__setattr__(self, "_external_content_internal_assignment", False)
        states = {}
        for name, backend in (self.content_storage or {}).items():
            if backend == "mongo":
                value = self.__dict__.get(name)
                explicit = name in kwargs
                states[name] = ExternalFieldState(
                    status=self._external_value_status(value) if explicit else "LOADED",
                    source="NEW",
                )
        object.__setattr__(self, "_external_content_field_states", states)

    @staticmethod
    def _external_value_status(value: Any) -> str:
        """None 是非法/未知正文，不作为显式空的替代品。"""
        return "CLEARED" if value is not None and hasattr(value, "__len__") and len(value) == 0 else "DIRTY"

    @classmethod
    def from_db(cls, db: str, field_names: Any, values: Any) -> Any:
        obj = super().from_db(db, field_names, values)
        source = "LIVE" if obj.__dict__.get("live", False) else "REVISION"
        revision_id = obj.__dict__.get(
            "live_revision_id" if source == "LIVE" else "latest_revision_id"
        )
        obj._external_content_field_states = {
            name: ExternalFieldState("UNLOADED", source, revision_id=revision_id)
            for name, backend in (cls.content_storage or {}).items() if backend == "mongo"
        }
        return obj

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        data = object.__getattribute__(self, "__dict__")
        if (
            (getattr(type(self), "content_storage", None) or {}).get(name) != "mongo"
            or data.get("_external_content_internal_assignment", False)
            or getattr(_external_content_access_context, "suppress_autoload", False)
        ):
            return
        states = data.setdefault("_external_content_field_states", {})
        previous = states.get(name, ExternalFieldState("UNLOADED", "NEW"))
        states[name] = replace(
            previous, status=self._external_value_status(value),
            source="NEW" if previous.source == "NEW" else "EDIT",
        )

    def get_external_field_state(self, name: str) -> ExternalFieldState:
        """只查看状态，不触发 SQL deferred 读取或加载正文。"""
        if (self.content_storage or {}).get(name) != "mongo":
            raise KeyError(name)
        states = self.__dict__.setdefault("_external_content_field_states", {})
        if name not in states:
            states[name] = ExternalFieldState("UNLOADED", "LIVE" if self.__dict__.get("live") else "REVISION")
        return states[name]

    @contextmanager
    def _external_internal_assignment(self) -> Iterator[None]:
        """仅本实例加载/占位赋值免于标记业务编辑；异常与嵌套退出均恢复。"""
        previous = self.__dict__.get("_external_content_internal_assignment", False)
        object.__setattr__(self, "_external_content_internal_assignment", True)
        try:
            yield
        finally:
            object.__setattr__(self, "_external_content_internal_assignment", previous)

    def _set_external_field_loaded(
        self, name: str, value: Any, manifest: ExternalContentManifest,
        *, source: str, revision_id: Optional[int] = None,
    ) -> None:
        """协调器全部验证成功后调用；不得用于未提交 NEW 正文的伪基线。"""
        with self._external_internal_assignment():
            setattr(self, name, value)
        entry = manifest.fields[name]
        self.__dict__.setdefault("_external_content_field_states", {})[name] = ExternalFieldState(
            "LOADED", source, revision_id, entry.payload_sha256, manifest,
        )
        self.__dict__.setdefault("_external_content_loaded_hashes", {})[name] = entry.payload_sha256

    def _bind_external_field_source(
        self, name: str, manifest: ExternalContentManifest,
        *, source: str, revision_id: Optional[int] = None,
    ) -> None:
        """完整校验后固定同源 UNLOADED 指针；不物化、不改已编辑/历史字段。"""
        previous = self.get_external_field_state(name)
        if previous.status == "UNLOADED" and previous.source == source and previous.manifest is None:
            self._external_content_field_states[name] = replace(
                previous, manifest=manifest, revision_id=revision_id,
            )

    def _load_external_field_source(self, name: str, *, materialize: bool = True) -> None:
        """从已固定 Manifest 读取，或第一次解析来源后固定同源所有字段。"""
        field_state = self.get_external_field_state(name)
        if field_state.manifest is None and (
            "live" not in self.__dict__
            or (
                field_state.revision_id is None
                and ("live_revision_id" if field_state.source == "LIVE" else "latest_revision_id")
                not in self.__dict__
            )
        ):
            # only()/defer() 可能连来源元数据也延迟；只读取原生身份/来源列，
            # 绝不读取外置 SQL 占位，也不能凭缺失 live 默认为草稿。
            source = "LIVE" if self.live else "REVISION"
            revision_id = getattr(
                self, "live_revision_id" if source == "LIVE" else "latest_revision_id"
            )
            for field_name, previous in self._external_content_field_states.items():
                if previous.status == "UNLOADED" and previous.manifest is None:
                    self._external_content_field_states[field_name] = replace(
                        previous, source=source, revision_id=revision_id,
                    )
            field_state = self.get_external_field_state(name)
        selected = {name} if materialize else set()
        if field_state.manifest is not None:
            ExternalContentCoordinator(store=getattr(self, "_test_content_store", None)).load_external_content_from_manifest(
                self, field_state.manifest, source=field_state.source,
                revision_id=field_state.revision_id, preserve_edits=True, fields=selected,
            )
            return
        if field_state.source == "LIVE":
            self.load_published_content(preserve_edits=True, fields=selected)
            if self.get_external_field_state(name).manifest is None:
                raise PublicationBodyUnavailableError("外置字段缺少有效的正式 Manifest 来源")
            return
        state = ExternalContentPublicationState.objects.filter(page_id=self.pk).first()
        revision_id = field_state.revision_id
        raw_manifest = None
        if state and state.draft_manifest and (
            revision_id is None or state.draft_revision_id == revision_id
        ):
            raw_manifest = state.draft_manifest
            revision_id = state.draft_revision_id
        elif revision_id:
            # 有明确 Revision 来源时不能改读较新的 draft/live。
            from wagtail.models import Revision
            revision = Revision.objects.filter(
                pk=revision_id, object_id=str(self.pk), content_type_id=self.content_type_id,
            ).first()
            if revision is not None:
                content = revision.content
                if isinstance(content, str):
                    content = json.loads(content)
                raw_manifest = content.get("_external_content_manifest")
        if not raw_manifest:
            raise PublicationRevisionInvalidError("外置字段缺少明确的 Revision Manifest 来源")
        manifest = ExternalContentManifest.from_dict(raw_manifest)
        ExternalContentCoordinator(store=getattr(self, "_test_content_store", None)).load_external_content_from_manifest(
            self, manifest, source="REVISION", revision_id=revision_id,
            preserve_edits=True, fields=selected,
        )

    def __getattribute__(self, name: str) -> Any:
        external_fields = getattr(type(self), "content_storage", None) or {}
        instance_state = object.__getattribute__(self, "__dict__")
        if (
            name in external_fields
            and external_fields.get(name) == "mongo"
            and not getattr(_external_content_access_context, "suppress_autoload", False)
            and not getattr(_controlled_alias_context, "in_alias", False)
            and not instance_state.get("_external_content_loading", False)
            and not instance_state.get("_external_content_internal_assignment", False)
            and object.__getattribute__(self, "get_external_field_state")(name).status == "UNLOADED"
        ):
            object.__setattr__(self, "_external_content_loading", True)
            try:
                object.__getattribute__(self, "_load_external_field_source")(name)
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
            if expected_page_id and manifest.page_id != expected_page_id:
                raise ManifestIdentityMismatchError(
                    f"Manifest 页面身份 {manifest.page_id} 与目标页面 {expected_page_id} 不匹配"
                )

            coordinator = ExternalContentCoordinator()
            coordinator.load_external_content_from_manifest(obj, manifest)

            obj._external_content_loaded_hashes = {
                field_name: field_ptr.payload_sha256 for field_name, field_ptr in manifest.fields.items()
            }

        return obj

    def with_content_json(self, content: Dict[str, Any]) -> Any:
        """原生回绑身份之前校验目标归属；历史来源不受 live/latest_revision 覆盖。

        原生 API 仅传快照而未传 Revision ID，不能把当前 latest_revision_id
        伪装成所选历史版本。跨页 Alias 仍需既有数据库来源证明，不新增豁免。
        """
        policy = get_storage_policy_for_model(type(self), allow_record_new=False)
        if policy and policy.external_fields:
            raw_manifest = content.get("_external_content_manifest")
            if not isinstance(raw_manifest, dict) or not raw_manifest:
                raise PublicationRevisionInvalidError("Revision 快照缺少有效 Manifest")
            manifest = ExternalContentManifest.from_dict(raw_manifest)
            if manifest.page_id == self.pk or not getattr(self, "alias_of_id", None):
                manifest.validate_against_page(self)
            else:
                from external_content.copying import validate_alias_manifest_source

                validate_alias_manifest_source(self, manifest)
        return super().with_content_json(content)

    @contextmanager
    def _restore_failed_revision_memory(self) -> Iterator[None]:
        """仅恢复保存体异常污染的绑定；不推断 atomic 退出时的提交结果。

        SQL 回滚不会恢复 Python。快照只取原始内存，避免 deferred/正文读取；
        外置值保留用户输入对象，状态字典浅拷贝（字段状态为不可变对象）。
        外层事务稍后回滚、COMMIT 未知或提交后回调失败时仍须丢弃并核对实例。
        """
        data = self.__dict__
        missing = object()
        names = (
            "latest_revision_id", "latest_revision_created_at",
            "draft_title", "has_unpublished_changes",
            "_external_content_field_states", "_external_content_loaded_hashes",
        )
        external_names = tuple(
            name for name, backend in (self.content_storage or {}).items()
            if backend == "mongo"
        )
        previous = {name: data.get(name, missing) for name in names + external_names}
        for name in ("_external_content_field_states", "_external_content_loaded_hashes"):
            if isinstance(previous[name], dict):
                previous[name] = previous[name].copy()
        relation_cache = self._state.fields_cache
        previous_revision = relation_cache.get("latest_revision", missing)
        try:
            yield
        except BaseException:
            # 直接恢复原始值/缺省状态，不触发赋值协议、SQL 回写或旧 live 回填。
            for name, value in previous.items():
                if value is missing:
                    data.pop(name, None)
                else:
                    data[name] = value
            if previous_revision is missing:
                relation_cache.pop("latest_revision", None)
            else:
                relation_cache["latest_revision"] = previous_revision
            raise

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
        # 恢复作用域先退出，随后 atomic 处理体异常；其退出/COMMIT 异常不在捕获范围。
        with transaction.atomic(), self._restore_failed_revision_memory():
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
            for name in manifest.fields:
                if self.get_external_field_state(name).status == "UNLOADED":
                    # 已验证引用直接绑定真实 Revision，不为中间状态提交物化正文。
                    self._external_content_field_states[name] = ExternalFieldState(
                        "UNLOADED", "REVISION", revision.pk,
                        manifest.fields[name].payload_sha256, manifest,
                    )
                else:
                    self._set_external_field_loaded(
                        name, getattr(self, name), manifest, source="REVISION", revision_id=revision.pk,
                    )
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

        if not is_initial_insert and not in_controlled_save and not in_publication_action:
            loaded_hashes = getattr(self, "_external_content_loaded_hashes", {}) or {}
            for field_name, field_cfg in policy.external_fields.items():
                if update_fields is None or field_name in update_fields:
                    field_state = self.get_external_field_state(field_name)
                    baseline_hash = field_state.baseline_hash or loaded_hashes.get(field_name)
                    if field_state.status in {"DIRTY", "CLEARED"} and baseline_hash is None:
                        raise DirectSaveForbiddenError(
                            f"字段 {field_name} 有未提交正文；请调用 save_revision()"
                        )
                    current_val = getattr(self, field_name, None)
                    _, current_hash = field_cfg.codec.encode(current_val, field=self._meta.get_field(field_name))
                    baseline_hash = self.get_external_field_state(field_name).baseline_hash or baseline_hash
                    if current_hash != baseline_hash:
                        previous = self.get_external_field_state(field_name)
                        self._external_content_field_states[field_name] = replace(
                            previous, status=self._external_value_status(current_val),
                            source="NEW" if previous.source == "NEW" else "EDIT",
                        )
                        raise DirectSaveForbiddenError(
                            f"外置正文页面 (page_id={self.pk}) 的字段 {field_name} 被直接修改，禁止调用 save()。"
                            f"正文修改必须通过 save_revision() 创建不可变版本并写入 MongoDB。"
                        )

        # SQL 仅持久化空哨兵，真实正文必须在异常退出时同样恢复到内存。
        staged_backup: Dict[str, Any] = {}
        missing = object()
        try:
            with self._external_internal_assignment(), _suppress_external_content_autoload():
                for field_name, field_cfg in policy.external_fields.items():
                    if update_fields is None or field_name in update_fields:
                        # 不为中间 SQL 保存物化 deferred 字段，保留原始内存/缺省状态。
                        staged_backup[field_name] = self.__dict__.get(field_name, missing)
                        setattr(self, field_name, field_cfg.codec.get_empty_sentinel())
                return super().save(*args, **kwargs)  # type: ignore[misc]
        finally:
            with self._external_internal_assignment():
                for field_name, original_val in staged_backup.items():
                    if original_val is missing:
                        self.__dict__.pop(field_name, None)
                    else:
                        setattr(self, field_name, original_val)

    def publish(self, revision: Any, *args: Any, **kwargs: Any) -> Any:
        """
        在同一 MySQL 事务内锁定并校验 Revision 与 Mongo 正文，再推进正式发布指针。
        排期发布不得提前公开；重复发布同一 Revision 保持幂等。
        """
        if revision is None or getattr(revision, "pk", None) is None:
            raise ValueError("发布时必须提供有效的 Revision 实例")

        is_scheduled_execution = kwargs.get("log_action") == "wagtail.publish.scheduled"

        with transaction.atomic(), _restore_failed_publication_memory(self):
            candidate = ExternalContentPublicationService.lock_and_validate_revision(
                self.pk,
                revision.pk,
                store=getattr(self, "_test_content_store", None),
            )
            # 排期与正式指针必须使用同一锁内指定版本；调用者的排期/页面
            # 内存可能陈旧，不能使原生动作与外置正式指针走向不同分支。
            revision = candidate.revision
            if is_scheduled_execution:
                validate_scheduled_revision(revision)

            # 严格解析排期时间
            raw_content = revision.content if isinstance(revision.content, dict) else json.loads(revision.content)
            approved_go_live_at = getattr(revision, "approved_go_live_at", None)
            page_go_live_at = raw_content.get("go_live_at")
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
        with transaction.atomic(), _restore_failed_publication_memory(self):
            if not getattr(self, "live", False) and self.pk and commit:
                db_live = type(self).objects.filter(pk=self.pk).values_list("live", flat=True).first()
                if db_live:
                    self.live = True
            with preserve_threadlocal_context(_controlled_save_context, "in_controlled_save", True):
                result = super().unpublish(*args, commit=commit, **kwargs)  # type: ignore[misc]

            if commit:
                self.live = False
                self.live_revision = None
                ExternalContentPublicationService.advance_unpublish_generation(self.pk)
                ExternalContentPublicationService.clear_published_pointer(self.pk)
                logger.info("external_content_page_unpublished page_id=%s", self.pk)

            return result

    def load_published_content(
        self, *, preserve_edits: bool = False, fields: Optional[set[str]] = None,
    ) -> None:
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
            coordinator.load_external_content_from_manifest(
                self, manifest, source="LIVE", revision_id=state.published_revision_id,
                preserve_edits=preserve_edits, fields=fields,
            )
        except ContentStoreError as exc:
            raise PublicationBodyUnavailableError(
                f"已发布页面 (page_id={self.pk}) 的外置正文不可用: {exc}"
            ) from exc

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
        fields = kwargs.get("fields", args[1] if len(args) > 1 else None)
        external = {name for name, backend in (self.content_storage or {}).items() if backend == "mongo"}
        selected = external if fields is None else external.intersection(fields)
        with self._external_internal_assignment(), _suppress_external_content_autoload():
            result = super().refresh_from_db(*args, **kwargs)
        source = "LIVE" if getattr(self, "live", False) else "REVISION"
        for name in selected:
            self._external_content_field_states[name] = ExternalFieldState(
                "UNLOADED", source, revision_id=self.__dict__.get(
                    "live_revision_id" if source == "LIVE" else "latest_revision_id"
                ),
            )
            self.__dict__.get("_external_content_loaded_hashes", {}).pop(name, None)
        if fields is None and getattr(self, "live", False):
            self.load_published_content()
        elif selected:
            for name in selected:
                self._load_external_field_source(name)
        return result

    def serve(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        """
        前台渲染前加载并校验正式发布正文。
        """
        if self.live:
            self.load_published_content()
        return super().serve(request, *args, **kwargs)  # type: ignore[misc]

    def copy(self, *args: Any, **kwargs: Any) -> Any:
        """所有复制入口由 Action 逐节点围栏；不在外围授予跨页读取权限。"""
        return super().copy(*args, **kwargs)

    def create_alias(self, *args: Any, **kwargs: Any) -> Any:
        """Alias Action 统一负责围栏和各递归层初始化，禁止外围宽授权窗口。"""
        with transaction.atomic():
            return super().create_alias(*args, **kwargs)
