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
from typing import TYPE_CHECKING, Any, Mapping, Optional

from django.db import transaction
from django.utils import timezone

from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import (
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

            try:
                page_base = Page.objects.select_for_update().get(pk=page_id)
            except Page.DoesNotExist as exc:
                raise PublicationRevisionInvalidError(f"页面不存在: page_id={page_id}") from exc

            page = page_base.specific
            expected_model_label = page._meta.label_lower

            state, _ = ExternalContentPublicationState.objects.select_for_update().get_or_create(
                page_id=page.pk,
                defaults={"model_label": expected_model_label, "publication_generation": 0},
            )

            try:
                revision = Revision.objects.select_for_update().get(
                    pk=revision_id,
                    object_id=str(page.pk),
                )
            except Revision.DoesNotExist as exc:
                raise PublicationRevisionInvalidError(
                    f"未找到属于页面 {page.pk} 的 Revision: revision_id={revision_id}"
                ) from exc

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
            from wagtail.models import Page

            try:
                page = Page.objects.get(pk=page_id).specific
                model_label = page._meta.label_lower
            except Exception:
                model_label = ""

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
        """在下架完成时清空正式发布指针（幂等）。"""
        with transaction.atomic():
            ExternalContentPublicationState.objects.filter(page_id=page_id).update(
                published_revision_id=None,
                published_manifest=None,
                published_at=None,
            )
            logger.info("external_content_published_pointer_cleared page_id=%s", page_id)

    @classmethod
    def finish_workflow_action(cls, workflow_state: Any, user: Any = None) -> None:
        """
        Wagtail Workflow 完成时锁定并发布准确的审批 Revision，杜绝 Revision 漂移。
        """
        with transaction.atomic():
            from wagtail.models import TaskState, WorkflowState

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
            page = locked_state.content_object.specific

            # 检查页面最新 Revision 是否发生漂移
            latest_rev = page.get_latest_revision()
            if latest_rev and latest_rev.pk != approved_rev.pk:
                raise PublicationWorkflowRevisionDriftError(
                    f"Workflow 批准的 Revision ({approved_rev.pk}) 与页面最新修订 ({latest_rev.pk}) 不一致，禁止隐式发布未审核草稿"
                )

            cls.lock_and_validate_revision(
                page.pk,
                approved_rev.pk,
                approved_revision_id=approved_rev.pk,
            )
            # 严格委托 page.publish 处理排期与立即发布分流，禁止提前无条件推进正式指针
            page.publish(approved_rev, user=user)


def wrap_publish_page_revision_action() -> None:
    """
    包装 Wagtail 的 PublishRevisionAction.execute，确保发布动作整体处于原子事务内。
    包含：
    1. 排期到期执行强验与状态重读行级锁，阻断排期状态变更竞态；
    2. 设置受控发布上下文 (in_publication_action = True)；
    3. 若页面在保存后在信号中出现异常，整体验收事务回滚，绝不残留 MySQL live=True。
    """
    try:
        from wagtail.actions.publish_revision import PublishRevisionAction
    except ImportError:
        return

    if getattr(PublishRevisionAction, "_external_content_atomic_wrapped", False):
        return

    original_execute = PublishRevisionAction.execute

    def controlled_execute(self: Any, skip_permission_checks: bool = False) -> Any:
        with transaction.atomic():
            obj = getattr(self, "object", None)
            specific_obj = getattr(obj, "specific", obj) if obj else None
            policy = None
            if specific_obj:
                try:
                    from external_content.registry import get_storage_policy_for_model
                    policy = get_storage_policy_for_model(type(specific_obj), allow_record_new=False)
                except Exception:
                    policy = None

            if policy and policy.external_fields:
                # 若 revision 带有未来排期时间，同步至 self.object.go_live_at，确保 Wagtail 严格维持 live=False
                rev_approved_at = getattr(self.revision, "approved_go_live_at", None)
                if rev_approved_at and rev_approved_at > timezone.now():
                    self.object.go_live_at = rev_approved_at

                if self.log_action == "wagtail.publish.scheduled":
                    from wagtail.models import Revision
                    locked_rev = Revision.objects.select_for_update().get(pk=self.revision.pk)
                    validate_scheduled_revision(locked_rev)
                    self.revision = locked_rev

            _controlled_save_context.in_publication_action = True
            try:
                return original_execute(self, skip_permission_checks=skip_permission_checks)
            finally:
                _controlled_save_context.in_publication_action = False

    PublishRevisionAction.execute = controlled_execute
    PublishRevisionAction._external_content_atomic_wrapped = True


def wrap_unpublish_page_action() -> None:
    """
    包装 Wagtail 的 UnpublishAction.execute，确保下架动作整体处于原子事务内。
    若页面下架后在信号中出现异常，整体验收事务回滚，绝不残留 MySQL live=False。
    """
    try:
        from wagtail.actions.unpublish import UnpublishAction
    except ImportError:
        return

    if getattr(UnpublishAction, "_external_content_atomic_wrapped", False):
        return

    original_execute = UnpublishAction.execute

    def controlled_execute(self: Any, skip_permission_checks: bool = False) -> Any:
        with transaction.atomic():
            return original_execute(self, skip_permission_checks=skip_permission_checks)

    UnpublishAction.execute = controlled_execute
    UnpublishAction._external_content_atomic_wrapped = True
