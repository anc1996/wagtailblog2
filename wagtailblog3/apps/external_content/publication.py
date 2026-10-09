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
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Optional

from django.db import transaction
from django.utils import timezone

from external_content.coordinator import ExternalContentCoordinator
from external_content.locking import (
    check_page_write_not_blocked,
    lock_alias_lifecycle_closure,
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


@contextmanager
def _restore_failed_publication_memory(page: Any) -> Iterator[None]:
    """只恢复业务体失败的发布元数据，不刷新 SQL 或覆盖用户未提交的正文。

    必须置于最外层 atomic 内侧：最终 COMMIT 未知或提交后回调失败时，
    不能把恢复旧内存伪装成数据库已回滚；调用者应丢弃该实例并只读核对。
    """
    missing = object()
    names = (
        "live", "live_revision_id", "go_live_at", "has_unpublished_changes",
        "expired", "first_published_at", "last_published_at",
    )
    before = {name: page.__dict__.get(name, missing) for name in names}
    cache = getattr(getattr(page, "_state", None), "fields_cache", None)
    live_revision = cache.get("live_revision", missing) if cache is not None else missing
    try:
        yield
    except BaseException:
        for name, value in before.items():
            if value is missing:
                page.__dict__.pop(name, None)
            else:
                page.__dict__[name] = value
        if cache is not None:
            if live_revision is missing:
                cache.pop("live_revision", None)
            else:
                cache["live_revision"] = live_revision
        raise


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
        if approved_revision_id is not None and (
            type(approved_revision_id) is not int
            or approved_revision_id != revision_id
        ):
            raise PublicationRevisionInvalidError("审批指针必须指向本次已校验的指定 Revision")

        with transaction.atomic():
            # 发布会同步下游 Alias；必须在任何正文读取前锁定整个来源/目标闭包。
            locks = lock_alias_lifecycle_closure(page_id).pages[page_id]
            page_base = locks.page
            revision = next((row for row in locks.revisions if row.pk == revision_id), None)
            if revision is None:
                # 不允许在 Intent 层之后补锁任意 Revision，包括属于其他页面的版本。
                raise PublicationRevisionInvalidError(
                    f"指定 Revision {revision_id} 不在页面 {page_id} 的锁集合中"
                )
            page = page_base.specific
            expected_model_label = page._meta.label_lower

            state = locks.state
            if state is None:
                state, _ = ExternalContentPublicationState.objects.select_for_update().get_or_create(
                    page_id=page.pk,
                    defaults={"model_label": expected_model_label, "publication_generation": 0},
                )

            # Revision 真实归属严格校验 (content_type 与 object_id)
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
            manifest.validate_against_page(page)
            manifest.validate_against_revision(revision)

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
            # 本入口只补齐原生发布已落地的指定版本；不能把保存/排期变成正式发布。
            if not candidate.page.live or candidate.page.live_revision_id != revision_id:
                return state

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
            locks = lock_alias_lifecycle_closure(page_id).pages[page_id]
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
            locks = lock_alias_lifecycle_closure(page_id).pages[page_id]
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
        *,
        executing_page: Any,
    ) -> Any:
        """统一受控下架协调器（Page.unpublish 与 UnpublishAction 共享，P0-3 闭环）。"""
        with transaction.atomic():
            # 如果正在执行删除流程，跳过写入检查（删除流程本身已经加锁并登记了意图）
            from external_content.deletion import _deletion_context
            deleting_ids = getattr(_deletion_context, 'deleting_page_ids', None)
            in_safe_deletion = deleting_ids is not None and page_id in deleting_ids
            # 删除上下文沿用已授权批次的锁；普通下架先围栏全部 Alias 再进入原生代码。
            locks = (
                lock_page_lifecycle_hierarchy(page_id, revision_ids=None, check_write=False)
                if in_safe_deletion else lock_alias_lifecycle_closure(page_id).pages[page_id]
            )
            page_base = locks.page
            model_label = (
                page_base.specific_class._meta.label_lower
                if hasattr(page_base, "specific_class") and page_base.specific_class
                else page_base._meta.label_lower
            )

            alias_source_id = getattr(page_base, "alias_of_id", None)
            # 原生下架会 full save 执行对象；锁内授权不能使 caller 的空关系
            # 绕过保存校验，或借用外层授权把普通页写成 Alias。漂移即退出整操作。
            if (
                getattr(executing_page, "pk", None) != page_id
                or getattr(executing_page, "alias_of_id", None) != alias_source_id
            ):
                raise ExternalContentOperationBlockedError(
                    "受控下架执行对象与锁内页面或 Alias 来源关系不一致，拒绝执行"
                )

            # 原生下架会保存 Alias 元数据；来源授权只取锁内关系，不信任
            # 调用者可变的 alias_of_id，也不向后续动作泄漏嵌套授权。
            from external_content.mixins import _controlled_alias_context

            with ExitStack() as authorization, preserve_threadlocal_context(
                _controlled_save_context, "in_controlled_save", True,
            ):
                # 非 Alias 同样覆盖为无授权，防止继承外层来源；退出精确恢复。
                authorization.enter_context(preserve_threadlocal_context(
                    _controlled_alias_context, "in_alias", alias_source_id is not None,
                ))
                authorization.enter_context(preserve_threadlocal_context(
                    _controlled_alias_context, "alias_update_source_page_id", alias_source_id,
                ))
                result = unpublish_callable()

            # 若处于非提交模式（commit=False），禁止提前推进代次与清空指针
            if getattr(_controlled_save_context, "commit_false", False):
                return result

            # 非提交动作不得额外初始化外置 State；原生递归子 Alias 的
            # commit=True 仍独立落地，不能因为父动作非提交而跳过其原生回调。
            state = locks.state
            if state is None:
                state, _ = ExternalContentPublicationState.objects.select_for_update().get_or_create(
                    page_id=page_id,
                    defaults={"model_label": model_label, "publication_generation": 0},
                )

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
            locks = lock_alias_lifecycle_closure(int(page_id)).pages[int(page_id)]
            page = locks.page.specific

            locked_state = WorkflowState.objects.select_for_update().get(pk=workflow_state.pk)
            if (
                locked_state.status != WorkflowState.STATUS_APPROVED
                or locked_state.get_next_task() is not None
            ):
                raise PublicationWorkflowRevisionDriftError("Workflow 尚未完整批准，禁止调用完成发布入口")
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

            if user is not None and not any(
                action[0] == "approve"
                for action in approved_task.task.specific.get_actions(page, user)
            ):
                from django.core.exceptions import PermissionDenied

                raise PermissionDenied("Workflow 完成发布需要当前审批任务的审核权限")

            approved_rev = next(
                (revision for revision in locks.revisions
                 if revision.pk == approved_task.revision.pk), None
            )
            if approved_rev is None:
                raise PublicationRevisionInvalidError("Workflow 审批 Revision 不在目标页面锁集合中")

            # 1. 严格优先校验 Revision 真实归属 (content_type 与 object_id)
            base_ct = page.get_base_content_type()
            if approved_rev.base_content_type_id != base_ct.id or str(approved_rev.object_id) != str(page.pk):
                raise PublicationRevisionInvalidError(
                    f"Workflow 批准的 Revision (ID: {approved_rev.pk}) 的 content_type 或 object_id 与目标页面 (ID: {page.pk}) 不匹配"
                )

            # 新草稿不改变审批输入；发布审批版本 A，保留更晚草稿 B，不隐式换成 latest。

            test_store = getattr(page, "_test_content_store", None)
            cls.lock_and_validate_revision(
                page.pk,
                approved_rev.pk,
                approved_revision_id=approved_rev.pk,
                store=test_store,
            )
            # 严格委托 page.publish 处理排期与立即发布分流，禁止提前无条件推进正式指针
            # 审批权不等于页面发布权；仅已完成且有审核权的内部入口跳过重复发布权限检查。
            page.publish(approved_rev, user=user, skip_permission_checks=True)


def is_external_content_target(obj: Any) -> bool:
    """
    多态识别目标对象或其所属模型是否属于受控外置正文目标。
    支持 Page、Page 子类或注册了外置策略的模型。
    """
    if obj is None:
        return False
    # 识别阶段不能访问 specific：自定义模型的 hydration 可能直接读 Mongo。
    target_cls = obj if isinstance(obj, type) else (getattr(obj, "specific_class", None) or type(obj))
    if not isinstance(obj, type) and getattr(obj, "content_storage", None):
        return True
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


def _lock_publication_action_revisions(
    page_id: int, revision_id: int, previous_revision_id: Optional[int],
) -> tuple[Any, Any]:
    """从完整 Alias 围栏中选定版本，校验后回绑锁内 Page，不补查/补锁。"""
    from wagtail.models import Revision
    from external_content.registry import get_storage_policy_for_model

    locks = lock_alias_lifecycle_closure(page_id).pages[page_id]
    specific_cls = locks.page.specific_class
    try:
        policy = get_storage_policy_for_model(specific_cls, allow_record_new=False)
    except Exception as exc:
        raise ExternalContentOperationBlockedError("解析外置存储策略失败，拒绝发布") from exc
    if (
        not policy
        or not getattr(policy, "external_fields", None)
        or (getattr(policy, "model_class", None) is not None
            and policy.model_class != specific_cls)
        or (getattr(policy, "model_label", None)
            and policy.model_label != specific_cls._meta.label_lower)
    ):
        raise ExternalContentOperationBlockedError("外置存储策略无效或模型不匹配，拒绝发布")

    def select_revision(selected_id: int) -> Any:
        revision = next((row for row in locks.revisions if row.pk == selected_id), None)
        if revision is None:
            raise PublicationRevisionInvalidError(
                f"Revision {selected_id} 不在页面 {page_id} 的锁集合中，禁止发布"
            )
        base_ct = locks.page.get_base_content_type()
        if (
            revision.base_content_type_id != base_ct.id
            or str(revision.object_id) != str(page_id)
            or revision.content_type_id != locks.page.content_type_id
        ):
            raise PublicationRevisionInvalidError(
                f"Revision {selected_id} 的页面或模型归属与锁内目标不匹配"
            )
        # 必须先校验身份，才允许原生 as_object 通过 P01 还原读取不可变正文。
        manifest = _extract_manifest_from_revision(revision)
        # 围栏返回基础 Page；其 _meta 是 wagtailcore.page，不是具体外置模型。
        # 此处用锁内 content_type 解析出的类核对，不为校验而抢先 specific。
        if manifest.page_id != page_id or manifest.model_label != specific_cls._meta.label_lower:
            raise ManifestIdentityMismatchError("发布 Manifest 与锁内页面/具体模型不匹配")
        manifest.validate_against_revision(revision)
        return revision

    revision = select_revision(revision_id)
    previous = select_revision(previous_revision_id) if previous_revision_id is not None else None
    page = locks.page.specific
    # GenericForeignKey 的缓存 API 不修改 Revision 身份列。显式覆盖缓存，防止
    # 原生构造器/previous_revision.as_object 使用调用者持有的陈旧 content_object。
    for selected in (revision, previous):
        if selected is not None:
            Revision.content_object.set_cached_value(selected, page)
    return revision, previous


def _wrap_publish_action_constructor(cls: type) -> None:
    """原生构造期已会还原正文；首次 as_object 必须在冻结围栏之后。"""
    if getattr(cls, "_external_content_constructor_wrapped", False):
        return
    original_init = cls.__init__

    def controlled_init(
        self: Any, revision: Any, user: Any = None, changed: bool = True,
        log_action: bool = True, previous_revision: Any = None,
    ) -> None:
        # 只解析 ContentType 的模型类，不访问 content_object/specific 或正文。
        model = revision.content_type.model_class()
        self.__dict__.pop("_external_content_publication_identity", None)
        if not is_external_content_target(model):
            original_init(self, revision, user=user, changed=changed,
                          log_action=log_action, previous_revision=previous_revision)
            return
        try:
            page_id, revision_id = int(revision.object_id), int(revision.pk)
            previous_id = int(previous_revision.pk) if previous_revision is not None else None
        except (TypeError, ValueError) as exc:
            raise PublicationRevisionInvalidError("发布需要持久化 Page/Revision 身份") from exc
        if page_id <= 0 or revision_id <= 0 or (previous_id is not None and previous_id <= 0):
            raise PublicationRevisionInvalidError("发布需要有效 Page/Revision 主键")
        with transaction.atomic():
            locked_revision, locked_previous = _lock_publication_action_revisions(
                page_id, revision_id, previous_id,
            )
            original_init(self, locked_revision, user=user, changed=changed,
                          log_action=log_action, previous_revision=locked_previous)
            # 构造与 execute 可以不在同一事务：只保存指定身份，不延续锁的假象。
            # execute 必须再次取围栏并重新还原；不信任可变的 self.object/revision。
            self._external_content_publication_identity = (page_id, revision_id, previous_id)

    cls.__init__ = controlled_init
    cls._external_content_constructor_wrapped = True


def _wrap_single_publish_action(cls: type) -> None:
    """执行期重新锁定指定版本并还原；保留原生权限检查与参数语义。"""
    if getattr(cls, "_external_content_atomic_wrapped", False):
        return

    original_execute = cls.execute

    def controlled_execute(self: Any, skip_permission_checks: bool = False) -> Any:
        with transaction.atomic():
            obj = getattr(self, "object", None)
            rev = getattr(self, "revision", None)
            identity = getattr(self, "_external_content_publication_identity", None)

            # 判断是否涉及外置正文受控模型
            is_target = identity is not None or is_external_content_target(obj)
            if not is_target and rev:
                rev_obj = getattr(rev, "content_object", None)
                if is_external_content_target(rev_obj):
                    is_target = True

            if is_target:
                if identity is None and (obj is None or not getattr(obj, "pk", None)):
                    raise ExternalContentOperationBlockedError(
                        f"受控外置页面对象缺失或尚未持久化主键，拒绝执行发布: {obj}"
                    )
                if identity is None and (rev is None or not getattr(rev, "pk", None)):
                    raise ExternalContentOperationBlockedError(
                        f"受控外置页面待发布 Revision 缺失或尚未持久化主键，拒绝执行发布: {rev}"
                    )

                if identity is None:
                    previous = getattr(self, "previous_revision", None)
                    identity = (int(obj.pk), int(rev.pk),
                                int(previous.pk) if previous is not None else None)
                self.revision, self.previous_revision = _lock_publication_action_revisions(*identity)
                self.object = self.revision.as_object()
                self.instance = self.object
                from wagtail.permissions import policy_registry
                self.permission_policy = policy_registry.get(self.object)

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

            specific_cls = getattr(obj, "specific_class", None) or type(obj)
            try:
                from external_content.registry import get_storage_policy_for_model
                policy = get_storage_policy_for_model(specific_cls, allow_record_new=False)
            except Exception as exc:
                raise ExternalContentOperationBlockedError(
                    "解析外置存储策略失败，拒绝执行下架"
                ) from exc

            policy_cls = getattr(policy, "model_class", None)
            obj_meta = getattr(specific_cls, "_meta", None)
            expected_label = getattr(obj_meta, "label_lower", None) or (
                f"{getattr(obj_meta, 'app_label', '')}.{getattr(obj_meta, 'model_name', '')}".strip(".")
                if obj_meta else None
            )
            mismatch = False
            if policy_cls is not None and policy_cls != specific_cls:
                mismatch = True
            elif getattr(policy, "model_label", None) and expected_label and policy.model_label != expected_label:
                mismatch = True

            if not policy or not getattr(policy, "external_fields", None) or mismatch:
                raise ExternalContentOperationBlockedError(
                    "外置存储策略无效、缺失外置字段或模型不匹配，严格阻断下架"
                )

            page_id = int(obj.pk)
            # 防重边界按本 Action 的 commit 设置；嵌套 Alias 的 commit=True
            # 必须独立生效，不能继承父动作的非提交标记而留下正式指针。
            with transaction.atomic(), _restore_failed_publication_memory(obj):
                with preserve_threadlocal_context(
                    _controlled_save_context, "commit_false", not getattr(self, "commit", True),
                ):
                    return ExternalContentPublicationService.execute_controlled_unpublish(
                        page_id=page_id,
                        unpublish_callable=lambda: original_execute(self, skip_permission_checks=skip_permission_checks),
                        executing_page=obj,
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
        _wrap_publish_action_constructor(cls)
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
                lock_alias_lifecycle_closure(int(page_id))
                # 再进入原生 finish()，修改 WorkflowState 状态 (Level 5) 并触发 on_finish
                return original_finish(self, user=user)

        return original_finish(self, user=user)

    WorkflowState.finish = controlled_finish
    WorkflowState._external_content_atomic_wrapped = True
