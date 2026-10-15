# -*- coding: utf-8 -*-
"""
Wagtail Page 正文外置通用协议 - 统一冻结与全局锁序管理模块 (G4)。

核心硬契约：
1. 单页生命周期分层锁序：
   Page -> PublicationState -> Revision (按 pk 严格升序) -> DeletionIntent
   所有排他锁必须在同一个 transaction.atomic() 块内按序获取；
   逐页调用不构成多页全局锁序，也不保证其他原生 SQL 无死锁。
2. 基础 Page 锁定规范：
   lock_writable_page(page_id: int) -> Page 必须且仅返回基础 Page 实例
   (Page.objects.select_for_update().get(pk=page_id))，严禁调用 .specific，
   严禁触发任何 hydration 或正文反序列化拉取。
3. 统一冻结与 Fail-Closed：
   任何 status != DeletionIntentStatus.CANCELLED 的删除意图（包括 COMPLETED 永久墓碑、
   PENDING、CLAIMED、MYSQL_DELETED、GC_ELIGIBLE、FAILED）均强制冻结页面的保存、
   发布、审批、下架与别名写入。若 Page 缺失或不存在，严格执行 Fail-Closed 阻断。
4. 跨库锁持有跨度：
   保存与发布操作的行锁必须在进入 Mongo 操作前获取，并跨越 Mongo 准备与 SQL 原生 Action，
   直至最外层数据库事务最终提交。
5. 线程局部嵌套安全与原值恢复：
   线程上下文管理器在退出时必须精准恢复原值，严禁粗暴重置，禁止宽泛吞咽异常。
6. 可选 NOWAIT 围栏：
   默认保留阻塞锁；显式启用时所有生命周期层级均使用 NOWAIT。
   仅转换 MySQL ER_LOCK_NOWAIT，调用方必须让异常退出整个 Action 事务。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from threading import local
from typing import Any, Callable, Iterator, List, Mapping, Optional, Sequence

from django.db import NotSupportedError, OperationalError, connection, transaction
from wagtail.models import Page, Revision

from external_content.exceptions import ExternalContentOperationBlockedError
from external_content.models import (
    DeletionIntentStatus,
    ExternalContentDeletionIntent,
    ExternalContentPublicationState,
)

logger = logging.getLogger(__name__)

_MYSQL_ER_LOCK_NOWAIT = 3572

# 仅翻译整操作入口建立此上下文；既存行不得在低层锁后补锁。
translation_lifecycle_context = local()


def translation_member(page_id: int) -> Optional[PageLifecycleLocks]:
    """返回已围栏成员；未知既存身份必须退出整个翻译事务。"""
    frame = getattr(translation_lifecycle_context, "frame", None)
    if frame is None:
        return None
    if page_id not in frame["locks"]:
        raise ExternalContentOperationBlockedError("翻译访问原闭包外的既存页面")
    return frame["locks"][page_id]


@contextmanager
def _nowait_lock_errors(page_id: int, *, nowait: bool) -> Iterator[None]:
    """仅转换显式 NOWAIT 的 MySQL 锁冲突，不吞掉超时、死锁或其他数据库错误。"""
    if nowait and not connection.features.has_select_for_update_nowait:
        raise NotSupportedError("当前数据库不支持 NOWAIT，拒绝降级为阻塞锁")
    try:
        yield
    except OperationalError as exc:
        if (
            nowait
            and connection.vendor == "mysql"
            and exc.args
            and exc.args[0] == _MYSQL_ER_LOCK_NOWAIT
        ):
            raise ExternalContentOperationBlockedError(
                f"页面 (ID: {page_id}) 的 NOWAIT 生命周期锁冲突，必须回滚整个操作后重试"
            ) from exc
        raise


@dataclass(frozen=True)
class PageLifecycleLocks:
    """
    页面生命周期受控行级锁屏蔽持有对象。

    属性：
    - page: 已持有排他行锁的基础 Page 实例（非 specific，未执行正文 hydration）
    - state: 已持有排他行锁的 ExternalContentPublicationState 实例（若存在）
    - revisions: 已持有排他行锁的 Revision 列表（严格按 pk 升序排序）
    - intents: 已持有排他行锁的 ExternalContentDeletionIntent 列表（按 pk/id 升序排序）
    """

    page: Page
    state: Optional[ExternalContentPublicationState]
    revisions: List[Revision]
    intents: List[ExternalContentDeletionIntent]


@dataclass(frozen=True)
class AliasLifecycleLocks:
    """Alias 来源祖先链和递归目标闭包的事务围栏，不代表树或 M2M 全局锁。"""

    source_chain: List[int]
    target_ids: List[int]
    pages: dict[int, PageLifecycleLocks]


def read_alias_source_chain(page_id: int) -> List[Page]:
    """仅读取基础 Page 的数据库来源链；不信任实例上的 alias_of，也不加载正文。"""
    if type(page_id) is not int or page_id <= 0:
        raise ExternalContentOperationBlockedError("Alias 来源必须是已持久化的页面")
    chain: List[Page] = []
    visited: set[int] = set()
    current_id = page_id
    while current_id is not None:
        if current_id in visited:
            raise ExternalContentOperationBlockedError("Alias 来源链存在环，拒绝操作")
        visited.add(current_id)
        try:
            page = Page.objects.get(pk=current_id)
        except Page.DoesNotExist as exc:
            raise ExternalContentOperationBlockedError("Alias 来源链缺少数据库页面") from exc
        chain.append(page)
        current_id = page.alias_of_id
    return chain


def _discover_alias_closure(
    page_id: int,
) -> tuple[List[Page], dict[int, Page], set[int]]:
    """只用基础 Page 发现来源链及递归目标，不加载正文或获取下层锁。"""
    chain = read_alias_source_chain(page_id)
    discovered = {page.pk: page for page in chain}
    target_ids: set[int] = {page_id}
    frontier = {page_id}
    while frontier:
        children = list(Page.objects.filter(alias_of_id__in=frontier).order_by("pk"))
        frontier = set()
        for child in children:
            if child.pk in target_ids or child.pk in discovered:
                raise ExternalContentOperationBlockedError("Alias 目标闭包存在环或反向来源")
            discovered[child.pk] = child
            target_ids.add(child.pk)
            frontier.add(child.pk)
    return chain, discovered, target_ids


def lock_alias_lifecycle_closures(
    page_ids: Sequence[int],
    *,
    additional_page_ids: Sequence[int] = (),
    validate_pages: Optional[Callable[[Mapping[int, Page]], None]] = None,
) -> tuple[dict[int, PageLifecycleLocks], dict[int, AliasLifecycleLocks]]:
    """多个 Alias/复制源先求并集再分层锁定，不逐闭包补锁。

    返回 (全部生命周期锁, 各入口的 Alias 闭包)。additional_page_ids 通常为
    混合复制树中的普通 Page 与目标父页。调用者的拓扑复核同样位于 Page 层。
    """
    if not connection.in_atomic_block:
        raise RuntimeError("Alias 围栏必须在活跃 transaction.atomic 块内执行")
    topologies = {
        page_id: _discover_alias_closure(page_id) for page_id in sorted(set(page_ids))
    }
    discovered_ids = set(additional_page_ids)
    for _, discovered, _ in topologies.values():
        discovered_ids.update(discovered)

    def validate_topologies(pages: Mapping[int, Page]) -> None:
        """拓扑当前读仅在 Page 层执行；发现变化立即终止，不扩展闭包。"""
        for page_id, (_, discovered, target_ids) in topologies.items():
            for locked_id, expected in discovered.items():
                actual = pages[locked_id]
                if (
                    actual.alias_of_id != expected.alias_of_id
                    or actual.content_type_id != expected.content_type_id
                ):
                    raise ExternalContentOperationBlockedError("获锁期间 Alias 来源或模型发生变化")
            current_children = list(
                Page.objects.select_for_update(nowait=True)
                .filter(alias_of_id__in=target_ids)
                .order_by("pk")
            )
            if {page.pk for page in current_children} != target_ids - {page_id}:
                raise ExternalContentOperationBlockedError("获锁期间 Alias 递归目标闭包发生变化")
            for child in current_children:
                expected = pages[child.pk]
                if (
                    child.alias_of_id != expected.alias_of_id
                    or child.content_type_id != expected.content_type_id
                ):
                    raise ExternalContentOperationBlockedError("获锁后 Alias 目标拓扑复核失败")
        if validate_pages is not None:
            validate_pages(pages)

    try:
        locked = lock_lifecycle_closure(
            sorted(discovered_ids), nowait=True, validate_pages=validate_topologies
        )
    except Page.DoesNotExist as exc:
        raise ExternalContentOperationBlockedError("获锁期间 Alias 页面已消失") from exc
    return locked, {
        page_id: AliasLifecycleLocks(
            source_chain=[page.pk for page in chain], target_ids=sorted(target_ids),
            pages={pk: locked[pk] for pk in discovered},
        )
        for page_id, (chain, discovered, target_ids) in topologies.items()
    }


def lock_alias_lifecycle_closure(page_id: int) -> AliasLifecycleLocks:
    """NOWAIT 全层锁定单个 Alias 闭包；拓扑变化必须退出整个 Action 事务。"""
    frame = getattr(translation_lifecycle_context, "frame", None)
    if frame is not None:
        translation_member(page_id)
        return _translation_alias_locks(page_id, frame["locks"])
    _, closures = lock_alias_lifecycle_closures([page_id])
    return closures[page_id]


def _translation_alias_locks(
    page_id: int, members: Mapping[int, PageLifecycleLocks],
) -> AliasLifecycleLocks:
    """仅由已围栏成员构造 Alias 关系；不能发现或锁定额外既存 Page。"""
    chain: List[int] = []
    current = page_id
    while current is not None:
        if current in chain or current not in members:
            raise ExternalContentOperationBlockedError("翻译 Alias 来源链存在环或越出闭包")
        chain.append(current)
        current = members[current].page.alias_of_id
    targets = {page_id}
    while True:
        expanded = targets | {
            pk for pk, row in members.items() if row.page.alias_of_id in targets
        }
        if expanded == targets:
            break
        targets = expanded
    return AliasLifecycleLocks(
        source_chain=chain, target_ids=sorted(targets),
        pages={pk: members[pk] for pk in set(chain) | targets},
    )


def lock_translation_lifecycle_closure(
    page_ids: Sequence[int],
    *,
    validate_pages: Callable[[Mapping[int, Page]], None],
) -> tuple[dict[int, PageLifecycleLocks], dict[int, AliasLifecycleLocks]]:
    """翻译专用：Page 复核后冻结低层身份，按各层主键逐点 NOWAIT 取锁。

    不扩充 Page 闭包，不补锁或原地重试；非翻译 helper 保持原契约。
    非锁定发现与点锁之间及发现后新增行仍有窗口；不证明完整 R3、
    MVCC 可见性或真实 InnoDB 首次锁全序，也不触发正文 hydration。
    """
    if not connection.in_atomic_block:
        raise RuntimeError("翻译生命周期闭包必须在活跃 transaction.atomic 块内执行")
    ids = sorted(set(page_ids))
    if not ids or any(type(page_id) is not int or page_id < 1 for page_id in ids):
        raise ExternalContentOperationBlockedError("生命周期闭包需要有效持久化 Page ID")
    with _nowait_lock_errors(ids[0], nowait=True):
        try:
            pages = {page_id: lock_writable_page(page_id, nowait=True) for page_id in ids}
        except Page.DoesNotExist as exc:
            raise ExternalContentOperationBlockedError("翻译获锁期间页面已消失") from exc
        validate_pages(pages)

        # 只冻结标量身份，不持有发现阶段的可变模型；所有 Page 校验先于低层查询。
        state_plan = tuple(sorted(set(
            ExternalContentPublicationState.objects.filter(page_id__in=ids)
            .values_list("pk", flat=True)
        )))
        base_type = pages[ids[0]].get_base_content_type()
        base_type_id = base_type.pk
        object_pages = {str(page_id): page_id for page_id in ids}
        revision_plan = tuple(sorted(set(
            Revision.objects.filter(
                base_content_type=base_type, object_id__in=list(object_pages),
            ).values_list("pk", "object_id", "base_content_type_id")
        )))
        intent_plan = tuple(sorted(set(
            ExternalContentDeletionIntent.objects.filter(page_id__in=ids)
            .values_list("pk", "page_id")
        )))

        states: dict[int, ExternalContentPublicationState] = {}
        for pk in state_plan:
            try:
                state = ExternalContentPublicationState.objects.select_for_update(
                    nowait=True,
                ).get(pk=pk)
            except ExternalContentPublicationState.DoesNotExist as exc:
                raise ExternalContentOperationBlockedError("翻译已发现的发布状态消失") from exc
            # State.page_id 本身就是 PK；未发现的缺失槽位仍合法，不创建替代行。
            if state.pk != pk or state.page_id != pk or pk not in pages:
                raise ExternalContentOperationBlockedError("翻译发布状态身份发生变化")
            states[pk] = state

        revisions: dict[int, List[Revision]] = {page_id: [] for page_id in ids}
        for pk, object_id, content_type_id in revision_plan:
            try:
                revision = Revision.objects.select_for_update(nowait=True).get(pk=pk)
            except Revision.DoesNotExist as exc:
                raise ExternalContentOperationBlockedError("翻译已发现的版本消失") from exc
            if (
                (revision.pk, revision.object_id, revision.base_content_type_id)
                != (pk, object_id, content_type_id)
                or object_id not in object_pages
                or content_type_id != base_type_id
            ):
                raise ExternalContentOperationBlockedError("翻译版本关联身份发生变化")
            revisions[object_pages[object_id]].append(revision)

        intents: dict[int, List[ExternalContentDeletionIntent]] = {
            page_id: [] for page_id in ids
        }
        for pk, page_id in intent_plan:
            # 使用原 UUID 主键；按关联重新查找会掩盖换绑，不得附加关联过滤条件。
            try:
                intent = ExternalContentDeletionIntent.objects.select_for_update(
                    nowait=True,
                ).get(pk=pk)
            except ExternalContentDeletionIntent.DoesNotExist as exc:
                raise ExternalContentOperationBlockedError("翻译已发现的删除意图消失") from exc
            if intent.pk != pk or intent.page_id != page_id or page_id not in pages:
                raise ExternalContentOperationBlockedError("翻译删除意图关联身份发生变化")
            if intent.status != DeletionIntentStatus.CANCELLED:
                raise ExternalContentOperationBlockedError(
                    f"页面 (ID: {page_id}) 存在未取消的删除意图 "
                    f"(ID: {intent.pk}, 状态: {intent.status})，写入被冻结"
                )
            intents[page_id].append(intent)

        locked = {
            page_id: PageLifecycleLocks(
                page=pages[page_id], state=states.get(page_id),
                revisions=revisions[page_id], intents=intents[page_id],
            )
            for page_id in ids
        }
    return locked, {pk: _translation_alias_locks(pk, locked) for pk in locked}


def lock_lifecycle_closure(
    page_ids: Sequence[int],
    *,
    check_write: bool = True,
    nowait: bool = False,
    validate_pages: Optional[Callable[[Mapping[int, Page]], None]] = None,
) -> dict[int, PageLifecycleLocks]:
    """一次锁定完整 Page 闭包，禁止获锁后增补既存页面或在事务内重试。

    Page PK、State page_id、Revision PK、Intent PK 分别全局升序。
    validate_pages 用于 Page 层的拓扑当前读；变化必须抛异常退出外层事务。
    本接口不触发 specific、不读 Mongo、不创建缺失状态。
    """
    if not connection.in_atomic_block:
        raise RuntimeError("生命周期闭包必须在活跃 transaction.atomic 块内执行")
    ids = sorted(set(page_ids))
    if not ids or any(type(page_id) is not int or page_id < 1 for page_id in ids):
        raise ExternalContentOperationBlockedError("生命周期闭包需要有效持久化 Page ID")
    options = {"nowait": True} if nowait else {}
    with _nowait_lock_errors(ids[0], nowait=nowait):
        pages = {page_id: lock_writable_page(page_id, nowait=nowait) for page_id in ids}
        if validate_pages is not None:
            validate_pages(pages)
        states = {
            state.page_id: state
            for state in ExternalContentPublicationState.objects.select_for_update(**options)
            .filter(page_id__in=ids).order_by("page_id")
        }
        revisions: dict[int, List[Revision]] = {page_id: [] for page_id in ids}
        # 所有成员都是基础 Wagtail Page，因此共享 Page 的 base_content_type。
        for revision in Revision.objects.select_for_update(**options).filter(
            base_content_type=pages[ids[0]].get_base_content_type(),
            object_id__in=[str(page_id) for page_id in ids],
        ).order_by("id"):
            revisions[int(revision.object_id)].append(revision)
        intents: dict[int, List[ExternalContentDeletionIntent]] = {
            page_id: [] for page_id in ids
        }
        for intent in ExternalContentDeletionIntent.objects.select_for_update(**options) \
                .filter(page_id__in=ids).order_by("id"):
            intents[intent.page_id].append(intent)
            if check_write and intent.status != DeletionIntentStatus.CANCELLED:
                raise ExternalContentOperationBlockedError(
                    f"页面 (ID: {intent.page_id}) 存在未取消的删除意图 "
                    f"(ID: {intent.pk}, 状态: {intent.status})，写入被冻结"
                )
        return {
            page_id: PageLifecycleLocks(
                page=pages[page_id], state=states.get(page_id),
                revisions=revisions[page_id], intents=intents[page_id],
            )
            for page_id in ids
        }


def lock_writable_page(page_id: int, *, nowait: bool = False) -> Page:
    """
    锁定并返回指定 page_id 的基础 Page 实例（Level 1 锁）。

    硬契约：
    1. 必须在活跃事务 (connection.in_atomic_block) 内执行；
    2. 返回基础 Page 实例 (Page.objects.select_for_update().get(pk=page_id))；
    3. 严禁调用 .specific，严禁触发任何正文 hydration；
    4. 若页面不存在，严格执行 Fail-Closed 抛出 Page.DoesNotExist。

    :param page_id: 目标页面主键 ID
    :param nowait: 是否立即拒绝锁冲突；默认保留阻塞锁
    :return: 已持有 select_for_update 行锁的基础 Page 实例
    :raises RuntimeError: 不在 transaction.atomic() 事务块内
    :raises Page.DoesNotExist: 页面不存在 (Fail-Closed)
    :raises ExternalContentOperationBlockedError: MySQL NOWAIT 锁冲突，须退出整个 Action 事务
    :raises NotSupportedError: 显式请求 NOWAIT，但数据库不支持
    """
    if not connection.in_atomic_block:
        raise RuntimeError("lock_writable_page 必须在活跃事务 (transaction.atomic) 块内执行")
    lock_options = {"nowait": True} if nowait else {}
    with _nowait_lock_errors(page_id, nowait=nowait):
        try:
            return Page.objects.select_for_update(**lock_options).get(pk=page_id)
        except Page.DoesNotExist:
            raise Page.DoesNotExist(f"目标页面 (ID: {page_id}) 不存在，Fail-Closed 阻断")


def check_page_write_not_blocked(page_id: int, for_update: bool = False) -> None:
    """
    统一页面写入与操作冻结检查。

    硬契约：
    1. 若数据库中无该 page_id 对应的 Page，严格执行 Fail-Closed 抛出异常；
    2. 检查该页面关联的所有 ExternalContentDeletionIntent；
    3. 任何 status != DeletionIntentStatus.CANCELLED 的意图（包含 COMPLETED 永久墓碑、
       PENDING、CLAIMED、MYSQL_DELETED、GC_ELIGIBLE、FAILED）均判定为写入冻结；
    4. 处于冻结状态时抛出 ExternalContentOperationBlockedError，阻止保存、发布、审批、下架及别名写入；
    5. 仅当所有意图均为 CANCELLED（或无任何删除意图）且页面存在时才允许操作；
    6. 若 for_update=True，查询意图时附加 select_for_update()。
    7. 若当前线程处于安全删除上下文且当前 page_id 在删除批次中，跳过写入检查，
       允许删除流程中 Wagtail 触发的 unpublish/save 操作通过。

    :param page_id: 页面主键 ID
   :param for_update: 是否施加排他行级锁
   :raises ExternalContentOperationBlockedError: 页面处于冻结状态或页面缺失 (Fail-Closed)
   """
    from external_content.deletion import _deletion_context
    # 仅当前 page_id 正在受控删除流程中时，跳过写入检查
    # 这允许 pre_delete 登记意图后，Wagtail 触发的 unpublish/save 不会被本次删除意图自阻断
    deleting_ids = getattr(_deletion_context, 'deleting_page_ids', None)
    if deleting_ids is not None and page_id in deleting_ids:
        return

    if not Page.objects.filter(pk=page_id).exists():
        raise ExternalContentOperationBlockedError(
            f"目标页面 (ID: {page_id}) 不存在或已丢失，Fail-Closed 阻断写入"
        )

    qs = ExternalContentDeletionIntent.objects.filter(page_id=page_id)
    if for_update:
        qs = qs.select_for_update().order_by("id")
    else:
        qs = qs.order_by("id")

    for intent in qs:
        if intent.status != DeletionIntentStatus.CANCELLED:
            raise ExternalContentOperationBlockedError(
                f"页面 (ID: {page_id}) 存在未取消的删除意图 (ID: {intent.id}, 状态: {intent.status})，写入被冻结"
            )


def lock_page_lifecycle_hierarchy(
    page_id: int,
    revision_ids: Optional[Sequence[int]] = None,
    *,
    check_write: bool = True,
    nowait: bool = False,
) -> PageLifecycleLocks:
    """
    按照单页分层锁序在当前事务内锁定页面生命周期全级模型。

    单页锁序硬契约：
    Level 1: Page (基础 Page，无 .specific，无 hydration)
    Level 2: ExternalContentPublicationState
    Level 3: Revision（按 pk 单调递增严格升序排序）
    Level 4: ExternalContentDeletionIntent（按 pk/id 升序排序）

    :param page_id: 目标页面主键 ID
    :param revision_ids: 指定待锁定的 Revision ID 列表（若为 None 则锁定页面所有关联 Revision）
    :param check_write: 是否同时核验 check_page_write_not_blocked 写入冻结
    :param nowait: 是否对全部层级启用 NOWAIT；默认保留阻塞锁
    :return: 包含全级已加锁对象的 PageLifecycleLocks 实例
    :raises RuntimeError: 不在 transaction.atomic() 事务块内
    :raises ExternalContentOperationBlockedError: 页面冻结或 MySQL NOWAIT 锁冲突，须退出整个 Action 事务
    :raises Page.DoesNotExist: 页面不存在 (Fail-Closed)
    :raises NotSupportedError: 显式请求 NOWAIT，但数据库不支持

    本函数不新建事务、不局部重试；锁由调用方最外层事务持有。
    多页调用的安全围栏由调用方负责，不承诺其他 SQL 或范围锁零等待。
    """
    if not connection.in_atomic_block:
        raise RuntimeError("lock_page_lifecycle_hierarchy 必须在活跃事务 (transaction.atomic) 块内执行")

    frame = getattr(translation_lifecycle_context, "frame", None)
    if frame is not None:
        member = translation_member(page_id)
        if page_id not in frame["created_ids"]:
            return member
        # 本事务 INSERT 的身份可继续初始化；不是凭 ID 缺席推断“新行”。

    lock_options = {"nowait": True} if nowait else {}
    with _nowait_lock_errors(page_id, nowait=nowait):
        # Level 1: Page 排他锁（基础 Page，无 .specific，无 hydration）
        page = lock_writable_page(page_id, **lock_options)

        # Level 2: ExternalContentPublicationState 排他锁
        state = ExternalContentPublicationState.objects.select_for_update(
            **lock_options
        ).filter(page_id=page_id).first()

        # Level 3: Revision 排他锁（严格按 pk 升序单调加锁）
        rev_qs = Revision.objects.select_for_update(**lock_options).filter(
            base_content_type=page.get_base_content_type(),
            object_id=str(page.pk),
        )
        if revision_ids is not None:
            sorted_ids = sorted(set(revision_ids))
            rev_qs = rev_qs.filter(id__in=sorted_ids)
        revisions = list(rev_qs.order_by("id"))

        # Level 4: ExternalContentDeletionIntent 排他锁（按 pk/id 升序）
        intents = list(
            ExternalContentDeletionIntent.objects.select_for_update(**lock_options)
            .filter(page_id=page_id)
            .order_by("id")
        )

        if check_write:
            for intent in intents:
                if intent.status != DeletionIntentStatus.CANCELLED:
                    raise ExternalContentOperationBlockedError(
                        f"页面 (ID: {page_id}) 存在未取消的删除意图 (ID: {intent.id}, 状态: {intent.status})，写入被冻结"
                    )
        return PageLifecycleLocks(page=page, state=state, revisions=revisions, intents=intents)


@contextmanager
def preserve_threadlocal_context(
    target: Any,
    attr_name: str,
    new_value: Any,
) -> Iterator[None]:
    """
    线程局部变量安全嵌套上下文管理器。

    硬契约：
    1. 进入上下文时精准捕获 target 上 attr_name 的当前原值；
    2. 将 attr_name 设置为 new_value；
    3. 退出时在 finally 块中无条件恢复原值（若进入前不存在该属性，则退出时执行 delattr，严禁粗暴赋 False/None）；
    4. 严禁宽泛捕获或吞咽业务异常，原样透传且不丢失异常上下文。

    :param target: 线程局部对象（如 threading.local 实例）
    :param attr_name: 属性名
    :param new_value: 上下文内的新值
    """
    has_orig = hasattr(target, attr_name)
    orig_val = getattr(target, attr_name) if has_orig else None
    setattr(target, attr_name, new_value)
    try:
        yield
    finally:
        if has_orig:
            setattr(target, attr_name, orig_val)
        else:
            if hasattr(target, attr_name):
                delattr(target, attr_name)
