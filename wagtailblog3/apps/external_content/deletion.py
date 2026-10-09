"""
Wagtail Page 正文外置通用协议 - 删除意图与禁用中的 Tombstone GC。

删除时记录持久意图，SQL 删除不得级联删除 Mongo 不可变正文。
GC 的最终 token/租约、冻结及完整引用围栏尚未闭合，因此公开回收入口
无条件拒绝执行；下方保留的旧算法不是可用性或并发安全证明。
bulk/祖先删除的整批闭包和原生树锁也仍需独立复核与隔离集成验证。
"""

from __future__ import annotations

import datetime
import logging
import uuid
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from functools import wraps
from typing import Any, Dict, Iterator, List, Literal, Optional, Set

from django.db import models, router, transaction
from django.utils import timezone
from wagtail.models import Page, Revision

from external_content.exceptions import ExternalContentOperationBlockedError, ManifestValidationError
from external_content.manifest import ExternalContentManifest
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)
from external_content.store import ContentVersionStore

logger = logging.getLogger(__name__)


from external_content.locking import check_page_write_not_blocked, preserve_threadlocal_context
import threading

# 线程局部上下文，用于在删除流程中跳过写入检查（按 page_id 精确豁免）
_deletion_context = threading.local()

DeletionFailureStage = Literal[
    "execute", "record-intent", "native-action", "tree-delete", "collector",
    "pre-delete", "post-delete", "intent-status-save",
    "transaction-enter", "transaction-exit",
]


def _log_deletion_failure(
    stage: DeletionFailureStage, diagnostic_logger: Optional[logging.Logger] = None,
) -> None:
    """诊断只含固定事件与阶段；日志故障不能替换正在传播的原始业务异常。"""
    try:
        (diagnostic_logger if diagnostic_logger is not None else logger).error(
            "external_content_deletion_failed stage=%s", stage,
        )
    except BaseException:
        # 运行时诊断不是可靠审计；不能为记录失败引入补偿写入或二次异常。
        pass


@contextmanager
def _deletion_failure_stage(
    stage: DeletionFailureStage, diagnostic_logger: Optional[logging.Logger] = None,
) -> Iterator[None]:
    """记录实际观察到的边界，可有多层记录，不推测底层 SQL 故障或提交结果。"""
    try:
        yield
    except BaseException:
        _log_deletion_failure(stage, diagnostic_logger)
        raise


@contextmanager
def _deletion_atomic(using: Optional[str] = None) -> Iterator[None]:
    """区分事务入口/退出与原业务异常；退出失败中性记录，绝不宣称已回滚。"""
    entered = False
    body_error: Optional[BaseException] = None
    try:
        with transaction.atomic(using=using):
            entered = True
            try:
                yield
            except BaseException as error:
                body_error = error
                raise
    except BaseException as error:
        if not entered:
            _log_deletion_failure("transaction-enter")
        elif body_error is None or error is not body_error:
            _log_deletion_failure("transaction-exit")
        raise


@contextmanager
def safe_deletion_scope(page_ids: set[int]) -> Iterator[None]:
    """使用本层独立集合，精确恢复外层对象及属性缺席，避免重叠授权与异常泄漏。"""
    scoped_ids = set(getattr(_deletion_context, "deleting_page_ids", set()))
    scoped_ids.update(page_ids)
    with preserve_threadlocal_context(_deletion_context, "deleting_page_ids", scoped_ids):
        yield


def wrap_native_page_deletion() -> None:
    """补齐原生删除日志和树计数的外围事务，Collector 只隔离窗口、不预授权。"""
    from django.db.models.deletion import Collector
    from treebeard.mp_tree import MP_NodeQuerySet
    from wagtail.actions.delete_page import DeletePageAction

    if not getattr(DeletePageAction.execute, "_external_content_deletion_wrapped", False):
        original_execute = DeletePageAction.execute

        @wraps(original_execute)
        def atomic_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
            using = kwargs.get("using") or router.db_for_write(type(self.page), instance=self.page)
            with _deletion_failure_stage("native-action"), _deletion_atomic(using=using):
                return original_execute(self, *args, **kwargs)

        atomic_execute._external_content_deletion_wrapped = True
        DeletePageAction.execute = atomic_execute

    if not getattr(MP_NodeQuerySet.delete, "_external_content_deletion_wrapped", False):
        original_queryset_delete = MP_NodeQuerySet.delete

        @wraps(original_queryset_delete)
        def atomic_queryset_delete(self: Any, *args: Any, **kwargs: Any) -> Any:
            if not issubclass(self.model, Page):
                return original_queryset_delete(self, *args, **kwargs)
            with _deletion_failure_stage("tree-delete"), _deletion_atomic(using=self.db):
                return original_queryset_delete(self, *args, **kwargs)

        atomic_queryset_delete._external_content_deletion_wrapped = True
        MP_NodeQuerySet.delete = atomic_queryset_delete

    if not getattr(Collector.delete, "_external_content_deletion_wrapped", False):
        original_collector_delete = Collector.delete

        @wraps(original_collector_delete)
        def scoped_collector_delete(self: Any, *args: Any, **kwargs: Any) -> Any:
            from external_content.mixins import ExternalContentPageMixin

            if not any(
                issubclass(model, ExternalContentPageMixin) and instances
                for model, instances in self.data.items()
            ):
                return original_collector_delete(self, *args, **kwargs)
            with _deletion_failure_stage("collector"), safe_deletion_scope(set()):
                return original_collector_delete(self, *args, **kwargs)

        scoped_collector_delete._external_content_deletion_wrapped = True
        Collector.delete = scoped_collector_delete


def _validated_pending_deletion_intent(
    intents: List[ExternalContentDeletionIntent],
    *,
    page_id: int,
    model_label: str,
) -> Optional[ExternalContentDeletionIntent]:
    """只接受锁内唯一合法等待意图，不能选择性忽略同页墓碑或认领状态。"""
    pending_intents = []
    for intent in intents:
        if intent.status == DeletionIntentStatus.CANCELLED:
            continue
        if (
            intent.status != DeletionIntentStatus.PENDING
            or intent.model_label != model_label
        ):
            raise ExternalContentOperationBlockedError(
                f"页面 (ID: {page_id}) 的删除意图身份或状态不允许推进"
            )
        pending_intents.append(intent)
    if len(pending_intents) > 1:
        raise ExternalContentOperationBlockedError(
            f"页面 (ID: {page_id}) 存在多个等待意图，拒绝选择性推进"
        )
    return pending_intents[0] if pending_intents else None


@contextmanager
def _restore_fields_on_failure(
    instance: Any, fields: tuple[str, ...], snapshot: Optional[Dict[str, Any]] = None,
) -> Iterator[None]:
    """仅异常时恢复本服务改动的内存字段；不代表数据库提交结果或完整树对象恢复。"""
    before = deepcopy(snapshot) if snapshot is not None else {
        field: deepcopy(getattr(instance, field)) for field in fields
    }
    try:
        yield
    except BaseException:
        for field, value in before.items():
            setattr(instance, field, value)
        raise


def record_deletion_intent(
    page: Page,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
    safe_period_seconds: int = 7 * 86400,
) -> ExternalContentDeletionIntent:
    """
    持久化记录外置正文页面删除意图清单。

    收集并固化页面当前关联的所有不可变正文版本号、历史 Revision ID 以及别名页面 ID。
    计算安全隔离保护期截止时间 safe_gc_after，将意图置为 PENDING 状态。

    :param page: 目标 Wagtail Page 实例
    :param actor_id: 操作人标识（如 user_id）
    :param request_id: 链路追踪 ID
    :param safe_period_seconds: 安全隔离保护期秒数（默认 7 天）
    :return: 已持久化的 ExternalContentDeletionIntent 实例
    """
    page_id = page.pk
    model_label = page._meta.label_lower

    # 安全隔离期硬契约校验
    min_safe_period = 7 * 86400  # 7 天
    if safe_period_seconds < min_safe_period:
        raise ValueError(
            f"安全隔离期不得低于 7 天 ({min_safe_period} 秒)，实际传入 {safe_period_seconds} 秒"
        )

    # 快照封套在 atomic 外退出，才能涵盖保存以及事务退出时抛出的异常。
    with _deletion_failure_stage("record-intent"), \
            ExitStack() as failure_state, _deletion_atomic():
        # 使用统一锁原语获取全局单调锁序 (符合 Sol 审查要求)
        from external_content.locking import lock_page_lifecycle_hierarchy

        locks = lock_page_lifecycle_hierarchy(page_id, revision_ids=None, check_write=False)
        state = locks.state
        revisions = locks.revisions
        intents = locks.intents

        existing_intent = _validated_pending_deletion_intent(
            intents, page_id=page_id, model_label=model_label
        )

        version_ids: Set[str] = set()
        version_metadata: List[Dict[str, Any]] = []  # 存储每个版本的完整元数据（列表格式）
        identities: Dict[str, Dict[str, Any]] = {}

        def collect_manifest(raw_manifest: Any) -> None:
            """严校验而不读 Mongo；共享不可变指针保留声明身份，不重绑为删除页。"""
            parsed = ExternalContentManifest.from_dict(raw_manifest)
            for field_name, pointer in parsed.fields.items():
                vid = pointer.version_id
                identity = {
                    "model_label": parsed.model_label,
                    "page_id": parsed.page_id,
                    "field_name": field_name,
                    "codec": pointer.codec,
                    "schema_version": pointer.schema_version,
                    "payload_sha256": pointer.payload_sha256,
                }
                if vid in identities:
                    if identities[vid] != identity:
                        raise ManifestValidationError("同一正文版本存在冲突身份，拒绝冻结不完整清单")
                    continue
                identities[vid] = identity
                version_ids.add(vid)
                version_metadata.append({"version_id": vid, "expected_identity": identity})

        # 1. 从锁屏障内已取得的 ExternalContentPublicationState 读取正式、审批、草稿 Manifest 中引用的版本
        if state:
            for manifest_dict in [state.published_manifest, state.approved_manifest, state.draft_manifest]:
                if manifest_dict is not None:
                    collect_manifest(manifest_dict)

        # 2. 从锁屏障内已取得的全部历史 Revision 获取序列化数据中的 _external_content_manifest 版本
        revision_ids: List[int] = []
        for rev in revisions:
            revision_ids.append(rev.pk)
            content = getattr(rev, "content", ) or {}
            if isinstance(content, dict):
                # 旧 Revision 可以缺命名空间；显式空值不是缺席，必须失败关闭。
                if "_external_content_manifest" in content:
                    collect_manifest(content["_external_content_manifest"])

        # 3. 收集别名页面 ID
        alias_ids: List[int] = list(page.aliases.values_list("id", flat=True))

        safe_gc_after = timezone.now() + datetime.timedelta(seconds=safe_period_seconds)

        manifest_payload = {
            "body_version_ids": sorted(list(version_ids)),
            "revision_ids": revision_ids,
            "alias_page_ids": alias_ids,
            "versions": version_metadata,  # 新增：每个版本的完整元数据（列表格式）
        }

        if existing_intent and existing_intent.status == DeletionIntentStatus.PENDING:
            failure_state.enter_context(_restore_fields_on_failure(
                existing_intent, ("manifest", "safe_gc_after", "actor_id", "request_id", "updated_at")
            ))
            existing_intent.manifest = manifest_payload
            # 重复登记时，safe_gc_after 不能被缩短，保持原有的（更晚的）时间
            if safe_gc_after > existing_intent.safe_gc_after:
                existing_intent.safe_gc_after = safe_gc_after
            existing_intent.actor_id = actor_id or existing_intent.actor_id
            existing_intent.request_id = request_id or existing_intent.request_id
            existing_intent.save(update_fields=["manifest", "safe_gc_after", "actor_id", "request_id", "updated_at"])
            logger.info(
                "external_content_deletion_intent_updated intent_id=%s page_id=%s model=%s versions_count=%d",
                existing_intent.pk,
                page_id,
                model_label,
                len(version_ids),
            )
            return existing_intent

        intent = ExternalContentDeletionIntent.objects.create(
            page_id=page_id,
            model_label=model_label,
            status=DeletionIntentStatus.PENDING,
            manifest=manifest_payload,
            safe_gc_after=safe_gc_after,
            actor_id=actor_id,
            request_id=request_id,
            collected_versions=[],
            failed_versions={},
            retry_count=0,
        )

        logger.info(
            "external_content_deletion_intent_recorded intent_id=%s page_id=%s model=%s versions_count=%d safe_gc_after=%s",
            intent.pk,
            page_id,
            model_label,
            len(version_ids),
            safe_gc_after,
        )
        return intent


def execute_safe_page_deletion(
    page: Page,
    intent: Optional[ExternalContentDeletionIntent] = None,
    actor_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> ExternalContentDeletionIntent:
    """
    受控安全删除外置正文页面。

    步骤：
    1. 在共同事务中锁定页面、Revision 与全部意图，校验唯一等待意图及其归属；
    2. 执行 MySQL 页面及关联 Revision 删除，保留发布状态作为审计记录；
    3. 更新意图状态为 MYSQL_DELETED；
    4. 核心红线保护：MongoDB 正文版本在此阶段绝对保留，阻止级联物理丢失。

    :param page: 待删除的 Wagtail Page 实例
    :param intent: 已登记的删除意图实例（可选）
    :param actor_id: 操作人标识
    :param request_id: 链路追踪标识
    :return: 状态更新为 MYSQL_DELETED 的 ExternalContentDeletionIntent 实例
    """
    page_id = page.pk
    model_label = page._meta.label_lower

    # 仅保住调用者的目标 PK 和本服务意图字段；外围批次失败的其他对象须丢弃重读。
    # COMMIT 结果未知时，即使内存已恢复，也不得据此自动重试，必须先核对 SQL 权威状态。
    with _deletion_failure_stage("execute"), \
            _restore_fields_on_failure(page, (page._meta.pk.attname,)), \
            ExitStack() as failure_state, _deletion_atomic():
        from external_content.locking import lock_page_lifecycle_hierarchy

        locks = lock_page_lifecycle_hierarchy(page_id, revision_ids=None, check_write=False)
        locked_intent = _validated_pending_deletion_intent(
            locks.intents, page_id=page_id, model_label=model_label
        )
        if locks.page.content_type.model_class() is not type(page):
            raise ExternalContentOperationBlockedError("删除页面模型与数据库身份不一致")
        intent_fields = (
            "status", "manifest", "safe_gc_after", "actor_id", "request_id", "updated_at",
        )
        # 可选分支的嵌套登记会返回 fresh ORM 对象；先保存锁内已有意图的冻结字段，
        # 再将该快照绑定到实际内部返回对象，不能误用重新登记后的字段作失败基线。
        existing_snapshot = {
            field: deepcopy(getattr(locked_intent, field)) for field in intent_fields
        } if intent is None and locked_intent is not None else None
        if intent is None:
            locked_intent = record_deletion_intent(
                page, actor_id=actor_id, request_id=request_id
            )
        elif (
            locked_intent is None
            or intent.pk != locked_intent.pk
            or intent.page_id != page_id
            or intent.model_label != model_label
        ):
            raise ExternalContentOperationBlockedError("删除意图与锁内页面身份不一致")

        failure_state.enter_context(_restore_fields_on_failure(
            locked_intent, intent_fields, snapshot=existing_snapshot,
        ))
        # Collector 仍核验并补齐版本清单；执行器仅暂时持有精确的本页删除窗口。
        with preserve_threadlocal_context(page, "_controlled_safe_deletion_in_progress", True):
            with safe_deletion_scope({page_id}):
                page.delete()
        locked_intent.refresh_from_db()
        locked_intent.status = DeletionIntentStatus.MYSQL_DELETED
        with _deletion_failure_stage("intent-status-save"):
            locked_intent.save(update_fields=["status", "updated_at"])

        logger.info(
            "external_content_safe_page_deleted intent_id=%s page_id=%s",
            locked_intent.pk,
            page_id,
        )
    return locked_intent


def perform_garbage_collection(
    now: Optional[datetime.datetime] = None,
    worker_id: Optional[str] = None,
    claim_duration_seconds: int = 300,
    batch_size: int = 50,
) -> Dict[str, int]:
    """
    拒绝尚未完成最终围栏验证的外置正文回收。

    保留签名兼容旧调用方，但所有参数均不能解除禁用。异常发生在时钟、
    ORM、认领或 Mongo 操作之前，存量正文及意图不被此入口改动。
    恢复回收需补齐安全期、token/租约/代次、冻结和完整引用复核，
    并另行获得验证与执行授权；旧预扫描结果不能代替最终围栏。

    :raises ExternalContentOperationBlockedError: GC 当前硬禁用。
    """
    # P-02 审查：旧回收器缺少最终 token/租约复核、全引用闭包与 F 阶段围栏。
    # 无论调用方参数如何，先拒绝且不扫描/认领/触碰 Mongo；保留意图与存量。
    # 恢复执行须另行补齐协议并获得验证/授权，不提供配置绕过开关。
    raise ExternalContentOperationBlockedError(
        "GC 已禁用：最终 token/freeze/references 围栏未闭合，禁止认领或物理删除正文"
    )

    current_time = now or timezone.now()

    # 未来时钟校验：禁止传入未来时间
    if now is not None and now > timezone.now():
        raise ValueError(f"perform_garbage_collection 禁止传入未来时钟，传入时间: {now}, 当前时间: {timezone.now()}")

    active_worker = worker_id or f"gc_worker_{uuid.uuid4().hex[:8]}"
    claim_duration = datetime.timedelta(seconds=claim_duration_seconds)
    claim_expires = current_time + claim_duration

    # 1. 过滤已度过安全隔离期的候选意图 ID
    # 严格封死：必须 safe_gc_after <= current_time，绝无例外！
    # 候选状态：MYSQL_DELETED、FAILED，或者 CLAIMED 但租约已过期的异常意图
    candidate_filter = (
        models.Q(status__in=[DeletionIntentStatus.MYSQL_DELETED, DeletionIntentStatus.FAILED])
        | models.Q(status=DeletionIntentStatus.CLAIMED, claim_expires_at__lte=current_time)
    )

    candidate_ids = list(
        ExternalContentDeletionIntent.objects.filter(
            safe_gc_after__lte=current_time,
        ).filter(
            candidate_filter
        ).values_list("id", flat=True)[:batch_size]
    )

    logger.info(
        "external_content_gc_candidate_scan current_time=%s candidate_count=%d",
        current_time,
        len(candidate_ids),
    )

    if not candidate_ids:
        return {
            "collected_intents": 0,
            "deleted_versions": 0,
        }

    collected_intents = 0
    deleted_versions_count = 0
    store = ContentVersionStore()

    # 算法与性能优化：批量内存集合预加载 (Batch Hash Set Indexing)
    # 彻底消除对每个 version_id 反复执行全表 SQL LIKE (%vid%) 的 O(N * M) 性能灾难
    # 1. 预加载所有活跃页面的有效版本 ID 集合进行 O(1) 检索
    active_state_vids: Set[str] = set()

    # 收集所有候选意图对应的 page_id（这些页面的 PublicationState 应该被排除）
    candidate_page_ids = set(
        ExternalContentDeletionIntent.objects.filter(
            id__in=candidate_ids
        ).values_list("page_id", flat=True)
    )

    # 只查询那些不在删除意图中的页面的 PublicationState
    # 已删除页面的 PublicationState 虽然保留作为审计日志，但其引用的版本应该被GC回收
    active_states = ExternalContentPublicationState.objects.exclude(
        page_id__in=candidate_page_ids
    ).values(
        "page_id", "published_manifest", "draft_manifest", "approved_manifest"
    )

    manifest_validation_failed = False

    for st in active_states:
        for m_key in ["published_manifest", "draft_manifest", "approved_manifest"]:
            m_dict = st.get(m_key)
            if m_dict is None:
                continue

            # Fail-Closed: 遇到非预期的 Manifest 结构时，记录错误并标记验证失败
            # 不抛出异常（避免阻塞其他意图的GC），但拒绝删除任何版本
            if not isinstance(m_dict, dict):
                logger.error(
                    "gc_corrupted_manifest_structure page_id=%s manifest_key=%s type=%s",
                    st.get("page_id"),
                    m_key,
                    type(m_dict).__name__
                )
                manifest_validation_failed = True
                continue

            fields = m_dict.get("fields", {})
            if fields and not isinstance(fields, dict):
                logger.error(
                    "gc_corrupted_manifest_fields page_id=%s manifest_key=%s fields_type=%s",
                    st.get("page_id"),
                    m_key,
                    type(fields).__name__
                )
                manifest_validation_failed = True
                continue

            if isinstance(fields, dict):
                for _, f_ptr in fields.items():
                    if isinstance(f_ptr, dict) and f_ptr.get("version_id"):
                        active_state_vids.add(str(f_ptr["version_id"]))

    # 如果发现损坏的 Manifest，立即返回，拒绝删除任何版本（Fail-Closed）
    if manifest_validation_failed:
        logger.warning(
            "gc_aborted_due_to_corrupted_manifest collected_intents=0 deleted_versions=0"
        )
        return {
            "collected_intents": 0,
            "deleted_versions": 0,
        }

    # 2. 预加载其他进行中删除意图的待删/保护版本集合进行 O(1) 检索
    other_intents_vids_map: Dict[str, Set[str]] = {}
    active_intents = ExternalContentDeletionIntent.objects.filter(
        status__in=[
            DeletionIntentStatus.PENDING,
            DeletionIntentStatus.MYSQL_DELETED,
            DeletionIntentStatus.CLAIMED,
            DeletionIntentStatus.FAILED,
        ]
    ).values("id", "manifest")
    for it in active_intents:
        m = it.get("manifest") or {}
        body_vids = set(m.get("body_version_ids") or [])
        other_intents_vids_map[str(it["id"])] = body_vids

    # 2. 逐个意图使用短事务原子认领（避免长事务大表锁导致并发超时）
    for intent_id in candidate_ids:
        with transaction.atomic():
            intent = ExternalContentDeletionIntent.objects.filter(
                id=intent_id,
                safe_gc_after__lte=current_time,
            ).select_for_update(skip_locked=True).first()

            if not intent:
                # 已经被其他 Worker 抢先认领或安全跳过
                continue

            # 再次确认当前意图是否可认领（未被有效占用）
            if intent.status == DeletionIntentStatus.CLAIMED and intent.claim_expires_at and intent.claim_expires_at > current_time:
                continue
            if intent.status not in [DeletionIntentStatus.MYSQL_DELETED, DeletionIntentStatus.FAILED, DeletionIntentStatus.CLAIMED]:
                continue

            # 成功认领意图，建立 claim 围栏
            intent.status = DeletionIntentStatus.CLAIMED
            intent.claimed_at = current_time
            intent.claimed_worker = active_worker
            intent.claim_expires_at = claim_expires
            intent.save(update_fields=["status", "claimed_at", "claimed_worker", "claim_expires_at", "updated_at"])

        # 3. 逐版本核查与回收（脱离事务保护执行）
        version_ids = intent.manifest.get("body_version_ids", [])
        collected = set(intent.collected_versions or [])
        failed_map = dict(intent.failed_versions or {})

        # 排除当前意图后，汇总其他活跃意图引用的版本集合 (O(1) 检索支持)
        other_active_vids: Set[str] = set()
        for it_id, vids_set in other_intents_vids_map.items():
            if it_id != str(intent.pk):
                other_active_vids.update(vids_set)

        all_versions_processed = True

        for vid in version_ids:
            if vid in collected:
                # 幂等性：已回收版本不重复物理删除
                continue

            # 复合引用归零核查 1：活跃发布状态 (O(1) 内存哈希检查)
            if vid in active_state_vids:
                logger.warning("gc_version_skipped_active_state_ref version_id=%s intent_id=%s", vid, intent.pk)
                failed_map[vid] = "referenced_in_publication_state"
                all_versions_processed = False
                continue

            # 复合引用归零核查 2：其他未完成删除意图 (O(1) 内存哈希检查)
            if vid in other_active_vids:
                logger.warning("gc_version_skipped_other_intent_ref version_id=%s intent_id=%s", vid, intent.pk)
                failed_map[vid] = "shared_in_other_active_intent"
                all_versions_processed = False
                continue

            # 复合引用归零核查 3：存活 Revision 核查（仅通过前两道过滤才执行精准检查）
            ref_in_revisions = Revision.objects.filter(
                content__icontains=vid
            ).exists()
            if ref_in_revisions:
                logger.warning("gc_version_skipped_revision_ref version_id=%s intent_id=%s", vid, intent.pk)
                failed_map[vid] = "referenced_in_revision"
                all_versions_processed = False
                continue

            # 引用彻底归零，安全物理删除 Mongo 库中的版本
            try:
                # 从 manifest.versions 列表中查找该版本的元数据
                version_meta = None
                for item in intent.manifest.get("versions", []):
                    if item.get("version_id") == vid:
                        version_meta = item.get("expected_identity")
                        break

                if not version_meta:
                    logger.error("gc_version_missing_manifest version_id=%s intent_id=%s", vid, intent.pk)
                    failed_map[vid] = "missing_manifest_entry"
                    all_versions_processed = False
                    continue

                expected_identity = {
                    "model_label": version_meta.get("model_label"),
                    "page_id": version_meta.get("page_id"),
                    "field_name": version_meta.get("field_name"),
                    "codec": version_meta.get("codec"),
                    "schema_version": version_meta.get("schema_version"),
                    "payload_sha256": version_meta.get("payload_sha256"),
                }

                deleted = store.delete_version(vid, expected_identity=expected_identity)
                collected.add(vid)
                failed_map.pop(vid, None)
                if deleted:
                    deleted_versions_count += 1
            except Exception as exc:
                logger.error("gc_delete_version_failed version_id=%s error=%s", vid, exc, exc_info=True)
                # 规范截断错误信息为 128 字符，防止异常堆栈过长导致 MySQL JSON 字段异常膨胀
                failed_map[vid] = f"store_delete_error: {str(exc)[:128]}"
                all_versions_processed = False

        # 4. 回写处理结果
        with transaction.atomic():
            intent.refresh_from_db()
            # 确保在处理期间没有被外部强制重置
            if intent.status != DeletionIntentStatus.CLAIMED:
                continue

            intent.collected_versions = sorted(list(collected))
            intent.failed_versions = failed_map

            # 检查是否所有版本均已回收完毕
            if all_versions_processed and len(collected) >= len(version_ids):
                intent.status = DeletionIntentStatus.COMPLETED
                intent.completed_at = timezone.now()
                intent.claimed_at = None
                intent.claimed_worker = None
                intent.claim_expires_at = None
                intent.claim_token = None  # 清空认领令牌
                intent.save(
                    update_fields=[
                        "status",
                        "collected_versions",
                        "failed_versions",
                        "completed_at",
                        "claimed_at",
                        "claimed_worker",
                        "claim_expires_at",
                        "claim_token",
                        "updated_at",
                    ]
                )
                collected_intents += 1
            else:
                intent.status = DeletionIntentStatus.FAILED
                intent.retry_count += 1
                intent.last_error = f"部分未回收版本: {sorted(list(failed_map.keys()))}"
                intent.claim_expires_at = None  # 释放租约围栏供下一次安全重试
                intent.claim_token = None  # 清空认领令牌
                intent.save(
                    update_fields=[
                        "status",
                        "collected_versions",
                        "failed_versions",
                        "retry_count",
                        "last_error",
                        "claim_expires_at",
                        "claim_token",
                        "updated_at",
                    ]
                )

    return {
        "collected_intents": collected_intents,
        "deleted_versions": deleted_versions_count,
    }


def cancel_pending_deletion_intent(
    intent_id: str | uuid.UUID,
) -> ExternalContentDeletionIntent:
    """
    受控取消处于等待中或失败状态的删除意图 (PENDING / FAILED)。

    契约保护红线：
    1. 若 MySQL 实体已被物理删除 (MYSQL_DELETED)，绝对禁止假性恢复并抛出 ExternalContentOperationBlockedError；
    2. 若意图已完成 GC 回收 (COMPLETED)，不可恢复并抛出 ExternalContentOperationBlockedError；
    3. CLAIMED（包括过期或缺失租约）、GC_ELIGIBLE 与未知状态均不可通过取消入口恢复；
    4. 仅允许取消 PENDING 与尚无回收墓碑的 FAILED，完整清空租约围栏并保留审计记录；
    5. CANCELLED 幂等返回，不重复写入；其他活跃意图仍然冻结该页面。
    """
    # 使用统一锁原语，符合全局锁序（Sol 审查要求）
    from external_content.locking import lock_page_lifecycle_hierarchy

    with transaction.atomic():
        # 先获取 intent 的基本信息（不加锁）
        intent_temp = ExternalContentDeletionIntent.objects.get(id=intent_id)
        page_id = intent_temp.page_id

        # 锁定页面层级（包括 Intent）
        locks = lock_page_lifecycle_hierarchy(page_id, revision_ids=None, check_write=False)

        # 从已锁定的 intents 中找到目标 intent
        # 预读仅确定锁目标；状态判断必须使用获锁后的对象及规范化 UUID。
        intent = next(
            (
                locked_intent
                for locked_intent in locks.intents
                if locked_intent.pk == intent_temp.pk
                and locked_intent.page_id == page_id
            ),
            None,
        )

        if not intent:
            raise ExternalContentDeletionIntent.DoesNotExist(f"删除意图 (id={intent_id}) 不存在")

        if intent.status == DeletionIntentStatus.CANCELLED:
            return intent

        if (
            intent.status not in (DeletionIntentStatus.PENDING, DeletionIntentStatus.FAILED)
            or intent.collected_versions
            or intent.completed_at is not None
        ):
            raise ExternalContentOperationBlockedError(
                f"删除意图 (id={intent.pk}) 的状态或回收记录不允许取消，不能据此恢复已删除正文。",
                details={"intent_id": str(intent.pk), "status": intent.status},
            )

        intent.status = DeletionIntentStatus.CANCELLED
        intent.claimed_at = None
        intent.claim_expires_at = None
        intent.claimed_worker = None
        intent.claim_token = None
        intent.save(
            update_fields=[
                "status",
                "claimed_at",
                "claim_expires_at",
                "claimed_worker",
                "claim_token",
                "updated_at",
            ]
        )
        logger.info("external_content_deletion_intent_cancelled intent_id=%s page_id=%s", intent.pk, intent.page_id)
        return intent


# 保持向后兼容别名
restore_deleted_page_external_content = cancel_pending_deletion_intent
