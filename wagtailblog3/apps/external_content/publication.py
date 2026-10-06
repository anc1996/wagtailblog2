"""
Wagtail Page 正文外置通用协议 - 发布与生命周期协调服务。

职责：
1. 在 MySQL 事务内对 Page、Revision 和 ExternalContentPublicationState 执行行级锁定；
2. 校验指定 Revision 的 Manifest 与 MongoDB 对应字段不可变版本的哈希与完整性；
3. 原子推进正式指针 (published_revision_id / published_manifest) 与单调递增公开代次 (publication_generation)；
4. 保证下架 (unpublish) 代次递增与指针安全清空，并具备幂等性；
5. 提供信号层与 Action 层的幂等边界接入 (ensure_published_revision)；
6. 严格拦截 Workflow 审批与 Revision 漂移。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from django.db import transaction
from django.utils import timezone

from external_content.coordinator import ExternalContentCoordinator
from external_content.locking import (
    check_page_write_not_blocked,
    lock_page_lifecycle_hierarchy,
    preserve_threadlocal_context,
)
from external_content.exceptions import (
    ExternalContentOperationBlockedError,
    ContentStoreError,
    ContentVersionIdentityMismatchError,
    ContentVersionNotFoundError,
    CorruptedContentPayloadError,
    ManifestError,
    ManifestIdentityMismatchError,
    PublicationBodyUnavailableError,
    PublicationRevisionInvalidError,
    PublicationWorkflowRevisionDriftError,
)
from external_content.manifest import ExternalContentManifest
from external_content.models import ExternalContentPublicationState
from external_content.store import ContentVersionStore

import threading

logger = logging.getLogger(__name__)

_controlled_save_context = threading.local()

if TYPE_CHECKING:
    from wagtail.models import Page, Revision, WorkflowState


@dataclass(frozen=True)
class ExternalContentPublicationCandidate:
    """
    已锁定并完成 MongoDB 正文版本双重验签的发布候选对象。

    包含：
    - page: 目标页面实例（持有行锁）
    - revision: 目标 Revision 实例（持有行锁）
    - state: 页面发布状态记录（持有行锁）
    - manifest: 解析并验签通过的不可变 Manifest
    """
    page: Any
    revision: Any
    state: ExternalContentPublicationState
    manifest: ExternalContentManifest


def _extract_manifest_from_revision(revision: Any) -> ExternalContentManifest:
    """从 Revision 中提取并解析 ExternalContentManifest。"""
    raw_content = getattr(revision, "content", None)
    if raw_content is None:
        raise PublicationRevisionInvalidError("Revision 内容为空")

    if isinstance(raw_content, str):
        try:
            content_dict = json.loads(raw_content)
        except Exception as exc:
            raise PublicationRevisionInvalidError(
                f"Revision content JSON 解析失败: {exc}",
                details={"revision_id": getattr(revision, "pk", None)},
            ) from exc
    elif isinstance(raw_content, dict):
        content_dict = raw_content
    else:
        raise PublicationRevisionInvalidError(
            f"未知的 Revision content 类型: {type(raw_content)}",
            details={"revision_id": getattr(revision, "pk", None)},
        )

    raw_manifest = content_dict.get("_external_content_manifest")
    if not raw_manifest or not isinstance(raw_manifest, dict):
        raise PublicationRevisionInvalidError(
            "Revision content 中缺少 _external_content_manifest 或格式非法",
            details={"revision_id": getattr(revision, "pk", None)},
        )

    try:
        return ExternalContentManifest.from_dict(raw_manifest)
    except ManifestError as exc:
        raise PublicationRevisionInvalidError(
            f"Manifest 解析校验失败: {exc}",
            details={"revision_id": getattr(revision, "pk", None)},
        ) from exc


def validate_scheduled_revision(revision: Any) -> None:
    """
    校验 Wagtail 定时发布任务传入的已批准 Revision。
    """
    approved_at = getattr(revision, "approved_go_live_at", None)
    if not approved_at:
        raise PublicationRevisionInvalidError(
            "指定 Revision 未获得定时发布排期批准 (缺少 approved_go_live_at)",
            details={"revision_id": getattr(revision, "pk", None)},
        )
    if approved_at > timezone.now():
        raise PublicationRevisionInvalidError(
            "指定 Revision 的排期时间尚未到达",
            details={
                "revision_id": getattr(revision, "pk", None),
                "approved_go_live_at": str(approved_at),
            },
        )


class ExternalContentPublicationService:
    """页面正文外置发布全周期协调服务。"""

    @classmethod
    def lock_and_validate_revision(
        cls,
        page_id: int,
        revision_id: int,
        *,
        approved_revision_id: Optional[int] = None,
        store: Optional[ContentVersionStore] = None,
    ) -> ExternalContentPublicationCandidate:
        """
        在当前或独立 MySQL 事务内对 Page、State、Revision 执行行级锁并核对 Mongo 正文。
        """
        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 1:
            raise ValueError(f"非法的 page_id: {page_id}")
        if not isinstance(revision_id, int) or isinstance(revision_id, bool) or revision_id < 1:
            raise ValueError(f"非法的 revision_id: {revision_id}")

        with transaction.atomic():
            from wagtail.models import Page, Revision

            # 全局单调统一锁序 Level 1~4
            locks = lock_page_lifecycle_hierarchy(page_id, [revision_id], check_write=True)
            page_base = locks.page
            page = page_base.specific
            expected_model_label = page._meta.label_lower

            state = locks.state
            if state is None:
                state, _ = ExternalContentPublicationState.objects.select_for_update().get_or_create(
                    page_id=page.pk,
                    defaults={"model_label": expected_model_label, "publication_generation": 0},
                )

            # Revision 真实归属严格校验 (content_type 与 object_id)
            try:
                revision = Revision.objects.select_for_update().get(pk=revision_id)
            except Revision.DoesNotExist as exc:
                raise PublicationRevisionInvalidError(
                    f"未找到属于页面 {page.pk} 的 Revision: revision_id={revision_id}"
                ) from exc

            base_ct = page_base.get_base_content_type()
            if revision.base_content_type_id != base_ct.id or str(revision.object_id) != str(page_base.pk):
                raise PublicationRevisionInvalidError(
                    f"Revision (ID: {revision_id}) 的 content_type 或 object_id 与目标页面 (ID: {page_id}) 不匹配"
                )

            manifest = _extract_manifest_from_revision(revision)

            # 身份围栏检查
            if manifest.page_id != page.pk:
                raise ManifestIdentityMismatchError(
                    f"Manifest page_id ({manifest.page_id}) 与页面主键 ({page.pk}) 不匹配"
                )
            if manifest.model_label != expected_model_label:
                raise ManifestIdentityMismatchError(
                    f"Manifest model_label ({manifest.model_label}) 与页面实际模型 ({expected_model_label}) 不匹配"
                )

            # 校验 Mongo 版本与哈希
            coordinator = ExternalContentCoordinator(store=store)

            try:
                coordinator.validate_manifest_payloads(manifest)
            except (ContentStoreError, ContentVersionNotFoundError, ContentVersionIdentityMismatchError, CorruptedContentPayloadError) as exc:
                logger.error(
                    "external_content_publish_validation_failed page_id=%s revision_id=%s error=%s",
                    page.pk,
                    revision.pk,
                    exc,
                )
                raise PublicationBodyUnavailableError(
                    f"发布校验失败：MongoDB 正文版本不可用或哈希损坏 ({exc})",
                    details={"page_id": page.pk, "revision_id": revision.pk},
                ) from exc
            except Exception as exc:
                logger.error(
                    "external_content_publish_validation_unexpected page_id=%s revision_id=%s error=%s",
                    page.pk,
                    revision.pk,
                    exc,
                    exc_info=True,
                )
                raise PublicationBodyUnavailableError(
                    f"发布校验未知异常: {exc}",
                    details={"page_id": page.pk, "revision_id": revision.pk},
                ) from exc

            # 扩展：页面模型针对锁定 Revision 的业务完整性发布门禁（杜绝 Action 绕过与草稿漂移）
            if hasattr(page, "validate_publication_revision"):
                page.validate_publication_revision(revision)

            # 更新 State 草稿凭证（仅当尚未记录草稿或目标 Revision 大于等于当前已记录草稿版本时才更新，绝不倒退草稿指针）
            update_fields = ["updated_at"]
            if state.draft_revision_id is None or revision.pk >= state.draft_revision_id:
                state.draft_revision_id = revision.pk
                state.draft_manifest = manifest.to_dict()
                update_fields.extend(["draft_revision_id", "draft_manifest"])

            if approved_revision_id is not None:
                if not isinstance(approved_revision_id, int) or isinstance(approved_revision_id, bool):
                    raise ValueError(f"非法的 approved_revision_id: {approved_revision_id}")
                state.approved_revision_id = approved_revision_id
                state.approved_manifest = manifest.to_dict()
                update_fields.extend(["approved_revision_id", "approved_manifest"])

            state.save(update_fields=update_fields)

            logger.info(
                "external_content_candidate_validated page_id=%s revision_id=%s approved_revision_id=%s",
                page.pk,
                revision.pk,
                approved_revision_id,
            )
            return ExternalContentPublicationCandidate(page, revision, state, manifest)

    @classmethod
    def promote_published_candidate(
        cls,
        candidate: ExternalContentPublicationCandidate,
    ) -> ExternalContentPublicationState:
        """
        在同一 MySQL 事务内切换正式指针并递增公开代次。
        """
        state = candidate.state
        state.published_revision_id = candidate.revision.pk
        state.published_manifest = candidate.manifest.to_dict()
        state.published_at = timezone.now()
        state.publication_generation = (state.publication_generation or 0) + 1

        state.save(
            update_fields=[
                "published_revision_id",
                "published_manifest",
                "published_at",
                "publication_generation",
                "updated_at",
            ]
        )
        logger.info(
            "external_content_published_promoted page_id=%s revision_id=%s generation=%s",
            candidate.page.pk,
            candidate.revision.pk,
            state.publication_generation,
        )
        return state

    @classmethod
    def ensure_published_revision(
        cls,
        page_id: int,
        revision_id: int,
        store: Optional[ContentVersionStore] = None,
    ) -> ExternalContentPublicationState:
        """
        幂等接入点：在 Wagtail 已完成页面保存或发出发布信号后补齐正式指针。
        若当前已是该 Revision 且 Manifest 一致，不重复增加代次。
        """
        with transaction.atomic():
            candidate = cls.lock_and_validate_revision(page_id, revision_id, store=store)
            state = candidate.state

            # 幂等性判断：已发布相同 Revision 且 Manifest 匹配时直接返回
            if (
                state.published_revision_id == revision_id
                and state.published_manifest == candidate.manifest.to_dict()
            ):
                return state

            # 若页面尚未 live 或 Revision 排期尚未到期，不提升为正式发布
            approved_at = getattr(candidate.revision, "approved_go_live_at", None)
            if approved_at and approved_at > timezone.now():
                logger.info(
                    "external_content_scheduled_not_due page_id=%s revision_id=%s approved_go_live_at=%s",
                    page_id,
                    revision_id,
                    approved_at,
                )
                return state

            return cls.promote_published_candidate(candidate)

    @classmethod
    def advance_unpublish_generation(cls, page_id: int) -> ExternalContentPublicationState:
        """
        取消发布时递增公开代次并清空指针。
        具备严格幂等性：若已经是下架状态（已清空正式指针），绝不重复递增代次。
        """
        with transaction.atomic():
            # 统一单调全级锁序与写入冻结核验
            locks = lock_page_lifecycle_hierarchy(page_id, revision_ids=None, check_write=True)
            page_base = locks.page
            model_label = (
                page_base.specific_class._meta.label_lower
                if hasattr(page_base, "specific_class") and page_base.specific_class
                else page_base._meta.label_lower
            )

            state = locks.state
            if state is None:
                state, _ = ExternalContentPublicationState.objects.select_for_update().get_or_create(
                    page_id=page_id,
                    defaults={"model_label": model_label, "publication_generation": 0},
                )

            # 幂等边界：仅当持有正式指针时才推进代次并清空
            if state.published_revision_id is not None or state.published_manifest is not None:
                state.publication_generation = (state.publication_generation or 0) + 1
                state.published_revision_id = None
                state.published_manifest = None
                state.published_at = None
                state.save(
                    update_fields=[
                        "publication_generation",
                        "published_revision_id",
                        "published_manifest",
                        "published_at",
                        "updated_at",
                    ]
                )
                logger.info(
                    "external_content_unpublish_generation_advanced page_id=%s generation=%s",
                    page_id,
                    state.publication_generation,
                )

            return state

    @classmethod
    def clear_published_pointer(cls, page_id: int) -> None:
        """在下架完成时清空正式发布指针（严格在持有全级锁的事务内执行，P0-2 闭环）。"""
        with transaction.atomic():
            locks = lock_page_lifecycle_hierarchy(page_id, revision_ids=None, check_write=True)
            state = locks.state
            if state:
                state.published_revision_id = None
                state.published_manifest = None
                state.published_at = None
                state.save(
                    update_fields=[
                        "published_revision_id",
                        "published_manifest",
                        "published_at",
                        "updated_at",
                    ]
                )
            logger.info("external_content_published_pointer_cleared page_id=%s", page_id)

    @classmethod
    def execute_controlled_unpublish(
        cls,
        page_id: int,
        unpublish_callable: Callable[[], Any],
    ) -> Any:
        """统一受控下架协调器（Page.unpublish 与 UnpublishAction 共享，P0-3 闭环）。"""
        with transaction.atomic():
            # 如果正在执行删除流程，跳过写入检查（删除流程本身已经加锁并登记了意图）
            from external_content.deletion import _deletion_context
            deleting_ids = getattr(_deletion_context, 'deleting_page_ids', None)
            in_safe_deletion = deleting_ids is not None and page_id in deleting_ids
            locks = lock_page_lifecycle_hierarchy(page_id, revision_ids=None, check_write=(not in_safe_deletion))
            page_base = locks.page
            model_label = (
                page_base.specific_class._meta.label_lower
                if hasattr(page_base, "specific_class") and page_base.specific_class
                else page_base._meta.label_lower
            )

            state = locks.state
            if state is None:
                state, _ = ExternalContentPublicationState.objects.select_for_update().get_or_create(
                    page_id=page_id,
                    defaults={"model_label": model_label, "publication_generation": 0},
                )

            with preserve_threadlocal_context(_controlled_save_context, "in_controlled_save", True):
                result = unpublish_callable()

            # 若处于非提交模式（commit=False），禁止提前推进代次与清空指针
            if getattr(_controlled_save_context, "commit_false", False):
                return result

            if state.published_revision_id is not None or state.published_manifest is not None:
                state.publication_generation = (state.publication_generation or 0) + 1
                state.published_revision_id = None
                state.published_manifest = None
                state.published_at = None
                state.save(
                    update_fields=[
                        "publication_generation",
                        "published_revision_id",
                        "published_manifest",
                        "published_at",
                        "updated_at",
                    ]
                )
                logger.info(
                    "external_content_unpublish_generation_advanced page_id=%s generation=%s",
                    page_id,
                    state.publication_generation,
                )

            return result

    @classmethod
    def finish_workflow_action(cls, workflow_state: Any, user: Any = None) -> None:
        """
        Wagtail Workflow 完成时锁定并发布准确的审批 Revision，杜绝 Revision 漂移。
        严格锁序：Page -> State -> Revision -> Intent (Level 1~4) -> WorkflowState (Level 5，P1-3 闭环)。
        """
        from wagtail.models import TaskState, WorkflowState

        content_obj = getattr(workflow_state, "content_object", None)
        page_id = getattr(content_obj, "pk", None) or getattr(workflow_state, "object_id", None)
        if not page_id:
            raise PublicationWorkflowRevisionDriftError("无法从 WorkflowState 提取有效的 page_id")

        with transaction.atomic():
            locks = lock_page_lifecycle_hierarchy(int(page_id), revision_ids=None, check_write=True)
            page = locks.page.specific

            locked_state = WorkflowState.objects.select_for_update().get(pk=workflow_state.pk)
            approved_task = (
                locked_state.task_states.filter(
                    status__in=(TaskState.STATUS_APPROVED, TaskState.STATUS_SKIPPED),
                    revision__isnull=False,
                )
                .select_related("revision")
                .order_by("-finished_at", "-id")
                .first()
            )
            if not approved_task or not approved_task.revision:
                raise PublicationWorkflowRevisionDriftError("未找到 Workflow 绑定的审批 Revision")

            approved_rev = approved_task.revision

            # 1. 严格优先校验 Revision 真实归属 (content_type 与 object_id)
            base_ct = page.get_base_content_type()
            if approved_rev.base_content_type_id != base_ct.id or str(approved_rev.object_id) != str(page.pk):
                raise PublicationRevisionInvalidError(
                    f"Workflow 批准的 Revision (ID: {approved_rev.pk}) 的 content_type 或 object_id 与目标页面 (ID: {page.pk}) 不匹配"
                )

            # 2. 检查页面最新 Revision 是否发生草稿漂移
            latest_rev = page.get_latest_revision()
            if latest_rev and latest_rev.pk != approved_rev.pk:
                raise PublicationWorkflowRevisionDriftError(
                    f"Workflow 批准的 Revision ({approved_rev.pk}) 与页面最新修订 ({latest_rev.pk}) 不一致，禁止隐式发布未审核草稿"
                )

            test_store = getattr(page, "_test_content_store", None)
            cls.lock_and_validate_revision(
                page.pk,
                approved_rev.pk,
                approved_revision_id=approved_rev.pk,
                store=test_store,
            )
            # 严格委托 page.publish 处理排期与立即发布分流，禁止提前无条件推进正式指针
            page.publish(approved_rev, user=user)


def is_external_content_target(obj: Any) -> bool:
    """
    多态识别目标对象或其所属模型是否属于受控外置正文目标。
    支持 Page、Page 子类或注册了外置策略的模型。
    """
    if obj is None:
        return False
    target_cls = obj if isinstance(obj, type) else type(getattr(obj, "specific", obj))
    if hasattr(target_cls, "content_storage") and getattr(target_cls, "content_storage"):
        return True
    try:
        from external_content.registry import get_registered_models, get_storage_policy_for_model
        registered = get_registered_models()
        if target_cls in registered:
            return True
        policy = get_storage_policy_for_model(target_cls, allow_record_new=False)
        if policy and policy.external_fields:
            return True
    except Exception:
        pass
    return False


def _wrap_single_publish_action(cls: type) -> None:
    """对具体的发布动作类执行安全包装与原子生命周期锁嵌入。"""
    if getattr(cls, "_external_content_atomic_wrapped", False):
        return

    original_execute = cls.execute

    def controlled_execute(self: Any, skip_permission_checks: bool = False) -> Any:
        with transaction.atomic():
            obj = getattr(self, "object", None)
            rev = getattr(self, "revision", None)

            # 判断是否涉及外置正文受控模型
            is_target = is_external_content_target(obj)
            if not is_target and rev:
                rev_obj = getattr(rev, "content_object", None)
                if is_external_content_target(rev_obj):
                    is_target = True

            if is_target:
                if obj is None or not getattr(obj, "pk", None):
                    raise ExternalContentOperationBlockedError(
                        f"受控外置页面对象缺失或尚未持久化主键，拒绝执行发布: {obj}"
                    )
                if rev is None or not getattr(rev, "pk", None):
                    raise ExternalContentOperationBlockedError(
                        f"受控外置页面待发布 Revision 缺失或尚未持久化主键，拒绝执行发布: {rev}"
                    )

                specific_obj = getattr(obj, "specific", obj)
                try:
                    from external_content.registry import get_storage_policy_for_model
                    policy = get_storage_policy_for_model(type(specific_obj), allow_record_new=False)
                except Exception as exc:
                    raise ExternalContentOperationBlockedError(
                        f"解析对象 {specific_obj} 的外置存储策略失败，拒绝执行发布: {exc}"
                    ) from exc

                policy_cls = getattr(policy, "model_class", None)
                obj_meta = getattr(specific_obj, "_meta", None)
                expected_label = getattr(obj_meta, "label_lower", None) or (
                    f"{getattr(obj_meta, 'app_label', '')}.{getattr(obj_meta, 'model_name', '')}".strip(".")
                    if obj_meta else None
                )
                mismatch = False
                if policy_cls is not None and policy_cls != type(specific_obj):
                    mismatch = True
                elif getattr(policy, "model_label", None) and expected_label and policy.model_label != expected_label:
                    mismatch = True

                if not policy or not getattr(policy, "external_fields", None) or mismatch:
                    raise ExternalContentOperationBlockedError(
                        f"外置受控对象 {specific_obj} 的存储策略无效、缺失外置字段或模型不匹配，严格阻断发布"
                    )

                page_id = int(specific_obj.pk)
                # 统一单调全级锁序与写入冻结核验
                locks = lock_page_lifecycle_hierarchy(page_id, [rev.pk], check_write=True)
                locked_rev = next((r for r in locks.revisions if r.pk == rev.pk), None)
                if not locked_rev:
                    raise PublicationRevisionInvalidError(
                        f"待发布 Revision (ID: {rev.pk}) 未在目标页面 (ID: {page_id}) 的锁集合中命中，禁止发布跨页面或不存在的 Revision"
                    )

                # 强验 Revision 的实际归属
                base_ct = specific_obj.get_base_content_type() if hasattr(specific_obj, "get_base_content_type") else None
                if base_ct:
                    if getattr(locked_rev, "base_content_type_id", None) != base_ct.id or str(getattr(locked_rev, "object_id", "")) != str(page_id):
                        raise PublicationRevisionInvalidError(
                            f"待发布 Revision (ID: {locked_rev.pk}) 的 content_type 或 object_id 与目标页面 (ID: {page_id}) 不匹配"
                        )

                self.revision = locked_rev

                # 若 revision 设置了未来发布时间，同步至 self.object.go_live_at，确保 Wagtail 严格维持 live=False
                rev_approved_at = getattr(self.revision, "approved_go_live_at", None)
                if rev_approved_at and rev_approved_at > timezone.now():
                    self.object.go_live_at = rev_approved_at

                if getattr(self, "log_action", None) == "wagtail.publish.scheduled":
                    validate_scheduled_revision(self.revision)

            with preserve_threadlocal_context(_controlled_save_context, "in_publication_action", True):
                return original_execute(self, skip_permission_checks=skip_permission_checks)

    cls.execute = controlled_execute
    cls._external_content_atomic_wrapped = True


def _wrap_single_unpublish_action(cls: type) -> None:
    """对具体的下架动作类执行安全包装，下架先于原生执行获取全级锁。"""
    if getattr(cls, "_external_content_atomic_wrapped", False):
        return

    original_execute = cls.execute

    def controlled_execute(self: Any, skip_permission_checks: bool = False) -> Any:
        obj = getattr(self, "object", None)
        is_target = is_external_content_target(obj)

        if is_target:
            if obj is None or not getattr(obj, "pk", None):
                raise ExternalContentOperationBlockedError(
                    f"受控外置页面对象缺失或尚未持久化主键，拒绝执行下架: {obj}"
                )

            specific_obj = getattr(obj, "specific", obj)
            try:
                from external_content.registry import get_storage_policy_for_model
                policy = get_storage_policy_for_model(type(specific_obj), allow_record_new=False)
            except Exception as exc:
                raise ExternalContentOperationBlockedError(
                    f"解析对象 {specific_obj} 的外置存储策略失败，拒绝执行下架: {exc}"
                ) from exc

            policy_cls = getattr(policy, "model_class", None)
            obj_meta = getattr(specific_obj, "_meta", None)
            expected_label = getattr(obj_meta, "label_lower", None) or (
                f"{getattr(obj_meta, 'app_label', '')}.{getattr(obj_meta, 'model_name', '')}".strip(".")
                if obj_meta else None
            )
            mismatch = False
            if policy_cls is not None and policy_cls != type(specific_obj):
                mismatch = True
            elif getattr(policy, "model_label", None) and expected_label and policy.model_label != expected_label:
                mismatch = True

            if not policy or not getattr(policy, "external_fields", None) or mismatch:
                raise ExternalContentOperationBlockedError(
                    f"外置受控对象 {specific_obj} 的存储策略无效、缺失外置字段或模型不匹配，严格阻断下架"
                )

            page_id = int(specific_obj.pk)
            return ExternalContentPublicationService.execute_controlled_unpublish(
                page_id=page_id,
                unpublish_callable=lambda: original_execute(self, skip_permission_checks=skip_permission_checks),
            )

        with transaction.atomic():
            return original_execute(self, skip_permission_checks=skip_permission_checks)

    cls.execute = controlled_execute
    cls._external_content_atomic_wrapped = True


def wrap_publish_page_revision_action() -> None:
    """
    包装 Wagtail 8.0 的 PublishRevisionAction 与 PublishPageRevisionAction。
    确保发布动作整体处于原子事务内，并在外置页面场景单调获取 Level 1~4 全级生命周期锁。
    """
    classes_to_wrap = []
    try:
        from wagtail.actions.publish_revision import PublishRevisionAction
        classes_to_wrap.append(PublishRevisionAction)
    except ImportError as exc:
        raise RuntimeError("无法导入 wagtail.actions.publish_revision.PublishRevisionAction，外置正文生命周期保护无法建立") from exc

    try:
        from wagtail.actions.publish_page_revision import PublishPageRevisionAction
        classes_to_wrap.append(PublishPageRevisionAction)
    except ImportError as exc:
        raise RuntimeError("无法导入 wagtail.actions.publish_page_revision.PublishPageRevisionAction，外置正文生命周期保护无法建立") from exc

    for cls in classes_to_wrap:
        _wrap_single_publish_action(cls)


def wrap_unpublish_page_action() -> None:
    """
    包装 Wagtail 8.0 的 UnpublishAction 与 UnpublishPageAction。
    确保下架动作由 ExternalContentPublicationService 受控执行，锁先于原生下架获取。
    """
    classes_to_wrap = []
    try:
        from wagtail.actions.unpublish import UnpublishAction
        classes_to_wrap.append(UnpublishAction)
    except ImportError as exc:
        raise RuntimeError("无法导入 wagtail.actions.unpublish.UnpublishAction，外置正文生命周期保护无法建立") from exc

    try:
        from wagtail.actions.unpublish_page import UnpublishPageAction
        classes_to_wrap.append(UnpublishPageAction)
    except ImportError as exc:
        raise RuntimeError("无法导入 wagtail.actions.unpublish_page.UnpublishPageAction，外置正文生命周期保护无法建立") from exc

    for cls in classes_to_wrap:
        _wrap_single_unpublish_action(cls)


def wrap_workflow_state_finish() -> None:
    """
    包装 Wagtail 的 WorkflowState.finish。
    在修改 WorkflowState 自身状态 (Level 5) 前，若绑定的目标对象为受控外置页面，
    强制先获取目标 Page 的 Level 1~4 全级生命周期锁，从根源消除全局跨模型逆序死锁环。
    """
    try:
        from wagtail.models import WorkflowState
    except ImportError as exc:
        raise RuntimeError("无法导入 wagtail.models.WorkflowState，工作流锁序保护无法建立") from exc

    if getattr(WorkflowState, "_external_content_atomic_wrapped", False):
        return

    original_finish = WorkflowState.finish

    def controlled_finish(self: Any, user: Any = None) -> Any:
        content_obj = getattr(self, "content_object", None)
        page_id = getattr(content_obj, "pk", None) or getattr(self, "object_id", None)
        is_target = is_external_content_target(content_obj)

        if is_target and page_id:
            with transaction.atomic():
                # 按照全局单调锁序：先持有 Page 全级生命周期锁 (Level 1~4)
                lock_page_lifecycle_hierarchy(int(page_id), revision_ids=None, check_write=True)
                # 再进入原生 finish()，修改 WorkflowState 状态 (Level 5) 并触发 on_finish
                return original_finish(self, user=user)

        return original_finish(self, user=user)

    WorkflowState.finish = controlled_finish
    WorkflowState._external_content_atomic_wrapped = True
