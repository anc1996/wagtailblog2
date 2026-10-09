"""
Wagtail Page 正文外置通用协议 - 页面复制 (Copy)、别名 (Alias) 与跨语言翻译 (Translation) 协同处理器。

核心契约（AD-09）：
1. 复制与跨语言翻译必须为目标页面派生专属不可变正文版本：
   - 目标页面的 Manifest 与 page_id 严格绑定目标页面的真实主键 target_page.id；
   - 在 MongoDB 中为 target_page 保存独立版本记录，严禁借用源页面 version_id 与源页面 page_id；
   - 历史 Revision 快照重绑定（copy_revisions=True）：目标页面的所有历史 Revision 快照内部
     的 _external_content_manifest 必须彻底重绑定为 target_page.pk，版本记录独立派生；
   - 存储优化与复用缓存：在派生历史版本时，建立 source_to_target_vid_map 内存映射，
     相同正文版本在目标页面复用同一个目标版本 ID，在 MongoDB 中仅持久化一份，杜绝存储膨胀；
   - 失败时整树 SQL 回滚；Mongo 准备版本保留为孤儿，等待独立 GC，不在请求中物理删除；
   - 修改复制后的副本绝不影响源页面；
2. 别名页面（Alias）托管关系：
   - 别名页面记录 source_page_id，不伪造本地 Revision；
   - 别名页面读取正文时透传源页面已发布的 Manifest 与不可变版本；
   - 源页面重新发布时同步推进所有关联别名页面的发布状态指针；
   - 别名转换为独立页面（ConvertAliasPageAction）时断开托管关系，生成独立版本与发布世代。
"""

from __future__ import annotations

import json
import logging
import uuid
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Dict, Iterator, List, Optional

from django.db import connection, transaction
from django.utils import timezone
from wagtail.models import Page, Revision

from external_content.coordinator import ExternalContentCoordinator
from external_content.exceptions import (
    ContentStoreError,
    ContentVersionNotFoundError,
    CorruptedContentPayloadError,
    ExternalContentError,
    ExternalContentOperationBlockedError,
    ManifestError,
    ManifestValidationError,
    PublicationRevisionInvalidError,
)
from external_content.manifest import ExternalContentManifest
from external_content.locking import (
    AliasLifecycleLocks,
    _nowait_lock_errors,
    lock_alias_lifecycle_closure,
    lock_alias_lifecycle_closures,
    lock_lifecycle_closure,
    lock_translation_lifecycle_closure,
    lock_page_lifecycle_hierarchy,
    preserve_threadlocal_context,
    read_alias_source_chain,
    PageLifecycleLocks,
    translation_lifecycle_context,
    translation_member,
    _discover_alias_closure,
)
from external_content.models import ExternalContentPublicationState
from external_content.publication import ExternalContentPublicationService
from external_content.registry import get_policy_for_version, get_storage_policy_for_model
from external_content.store import ContentVersionStore

logger = logging.getLogger(__name__)


def _translation_topology(action: Any) -> Dict[int, Page]:
    """非锁定完整 census；不是当前读，也不保证多语句或复核后拓扑稳定。"""
    manager = Page.objects
    source = manager.get(pk=action.page.pk)
    sources = (
        list(manager.filter(path__startswith=source.path).order_by("pk"))
        if action.include_subtree else [source]
    )
    discovered = {page.pk: page for page in sources}
    for page in sources:
        parent = manager.get(path=page.path[:-Page.steplen]) if page.depth > 1 else None
        while parent is not None:
            discovered[parent.pk] = parent
            if not parent.is_root():
                for translated in manager.filter(
                    translation_key=parent.translation_key, locale_id=action.locale.pk,
                ):
                    discovered[translated.pk] = translated
            if parent.is_root():
                break
            parent = manager.get(path=parent.path[:-Page.steplen])
    # 策略探测也必须覆盖 Alias 根源，不允许普通类型的混合来源漏入原生分支。
    frontier = set(discovered)
    while frontier:
        extra: Dict[int, Page] = {}
        for pk in sorted(frontier):
            _, closure, _ = _discover_alias_closure(pk)
            extra.update(closure)
        frontier = set(extra) - set(discovered)
        discovered.update(extra)
    # treebeard 的父 INSERT 锁和 Alias 来源/目标都纳入同一 Page 层。
    return discovered


def _lock_translation_sources(action: Any) -> Dict[str, Any]:
    """全局分层围栏完成后才允许 execute 首次还原草稿。"""
    discovered = _translation_topology(action)

    def fingerprint(page: Page) -> tuple[Any, ...]:
        return (
            page.path, page.depth, page.content_type_id, page.alias_of_id,
            page.translation_key, page.locale_id,
        )

    # 值快照不持有可变 Page；后续点锁或夹具共享实例变化不能改写 expected。
    expected = MappingProxyType({pk: fingerprint(page) for pk, page in discovered.items()})

    def validate(pages: Any) -> None:
        for pk, value in expected.items():
            if fingerprint(pages[pk]) != value:
                raise ExternalContentOperationBlockedError("翻译获锁期间页面拓扑发生变化")
        # 锁定当前版本来自已知 PK 点锁。未知成员只能通过完整非锁定 census
        # 被观察并拒绝，绝不先锁后比较，也不把复核限制成 K 的投影。
        current = _translation_topology(action)
        if {pk: fingerprint(page) for pk, page in current.items()} != expected:
            raise ExternalContentOperationBlockedError("翻译获锁期间源树或目标父页发生变化")

    locks, aliases = lock_translation_lifecycle_closure(sorted(expected), validate_pages=validate)
    return {"locks": locks, "alias_locks": aliases, "created_ids": set()}


@contextmanager
def _translation_insert_scope() -> Iterator[None]:
    """仅 Copy/Alias 节点持久化窗口可申请无 PK 新身份，不扩大既存闭包。"""
    frame = getattr(translation_lifecycle_context, "frame", None)
    if frame is None:
        yield
        return
    with preserve_threadlocal_context(translation_lifecycle_context, "insert_frame", frame):
        yield


def wrap_copy_page_for_translation_action() -> None:
    """保留原生翻译参数、权限、walk 和父 Alias，只扩展整操作事务边界。"""
    from django.db.models.signals import post_save, pre_save
    from wagtail.actions.copy_for_translation import CopyPageForTranslationAction

    if getattr(CopyPageForTranslationAction, "_external_content_wrapped", False):
        return
    original_execute = CopyPageForTranslationAction.execute
    original_node = CopyPageForTranslationAction._copy_for_translation
    original_walk = CopyPageForTranslationAction.walk

    def controlled_walk(self: Any, current_page: Page) -> None:
        """保持 8.0 原生 walk 顺序，仅在子页草稿还原之前检查成员身份。"""
        if getattr(translation_lifecycle_context, "frame", None) is None:
            return original_walk(self, current_page)
        from wagtail.signals import copy_for_translation_done
        for child_page in current_page.get_children():
            translation_member(child_page.pk)
            translated_page = self._copy_for_translation(
                child_page if child_page.live else child_page.get_latest_revision_as_object(),
                self.locale, self.copy_parents, self.alias, self.exclude_fields,
            )
            copy_for_translation_done.send(
                sender=self.__class__, source_obj=child_page.specific,
                target_obj=translated_page,
            )
            self.walk(child_page)

    def controlled_node(self: Any, page: Page, *args: Any, **kwargs: Any) -> Page:
        frame = getattr(translation_lifecycle_context, "frame", None)
        if frame is not None:
            translation_member(page.pk)
        return original_node(self, page, *args, **kwargs)

    def controlled_execute(self: Any, skip_permission_checks: bool = False) -> Page:
        self.check(skip_permission_checks=skip_permission_checks)
        existing = getattr(translation_lifecycle_context, "frame", None)
        if existing is not None:
            translation_member(self.page.pk)
            return original_execute(self, skip_permission_checks=skip_permission_checks)
        # 仅加固本次调用的事务封套，可能嵌套为 savepoint，并不拥有最外层事务。
        # RR 快照、非合作 writer 与无外置分流幻影尚未证明消除；不是完整 R3 修复。
        with transaction.atomic():
            discovered = _translation_topology(self)
            has_external = any(
                (policy := get_storage_policy_for_model(
                    page.specific_class, allow_record_new=False,
                )) and policy.external_fields for page in discovered.values()
            )
            # 普通页面保留原生成功/错误语义，不强制外置围栏或 frame 登记。
            if not has_external:
                return original_execute(self, skip_permission_checks=skip_permission_checks)
            frame = _lock_translation_sources(self)
            pending: Dict[int, Page] = {}

            def before_insert(sender: Any, instance: Any, raw: bool = False, **kw: Any) -> None:
                """SQL 前校验基础 PK/全部 MTI 父链；leaf created 不能证明基础 INSERT。"""
                if (getattr(translation_lifecycle_context, "frame", None) is not frame
                        or not isinstance(instance, Page)):
                    return
                # 只读实例已有字段，避免 deferred 属性为身份校验发起额外 SQL。
                identities = [
                    instance.__dict__.get(field.attname)
                    for field in instance._meta.concrete_fields
                    if field.primary_key or (
                        field.is_relation and getattr(field.remote_field, "parent_link", False)
                    )
                ]
                pk = instance.__dict__.get(instance._meta.pk.attname)
                if (not raw and not instance._state.adding and pk in frame["locks"]
                        and all(value is None or value == pk for value in identities)):
                    return
                if (raw or not instance._state.adding
                        or any(value is not None for value in identities)
                        or getattr(translation_lifecycle_context, "insert_frame", None) is not frame):
                    raise ExternalContentOperationBlockedError("翻译拒绝未知既存或非受控新建身份")
                pending[id(instance)] = instance

            def after_insert(sender: Any, instance: Any, created: bool = False,
                             raw: bool = False, **kw: Any) -> None:
                # 新 Alias 的 State 在 INSERT 后初始化；后续递归只读本事务记录，
                # 不为刷新上下文重新获取任何既存 Page 锁。
                if (getattr(translation_lifecycle_context, "frame", None) is frame
                        and sender is ExternalContentPublicationState and not raw
                        and instance.page_id in frame["created_ids"]):
                    member = frame["locks"][instance.page_id]
                    frame["locks"][instance.page_id] = replace(member, state=instance)
                    return
                if (getattr(translation_lifecycle_context, "frame", None) is not frame
                        or not isinstance(instance, Page) or raw
                        or pending.pop(id(instance), None) is not instance):
                    return
                if not created or instance.pk is None or instance.pk in frame["locks"]:
                    raise ExternalContentOperationBlockedError("翻译新增身份与既存闭包冲突")
                frame["created_ids"].add(instance.pk)
                frame["locks"][instance.pk] = PageLifecycleLocks(
                    page=instance, state=None, revisions=[], intents=[],
                )

            original_page = self.page
            pre_save.connect(before_insert, weak=False)
            post_save.connect(after_insert, weak=False)
            try:
                with preserve_threadlocal_context(translation_lifecycle_context, "frame", frame):
                    self.page = frame["locks"][self.page.pk].page
                    return original_execute(self, skip_permission_checks=skip_permission_checks)
            finally:
                self.page = original_page
                pre_save.disconnect(before_insert)
                post_save.disconnect(after_insert)

    CopyPageForTranslationAction.execute = controlled_execute
    CopyPageForTranslationAction._copy_for_translation = controlled_node
    CopyPageForTranslationAction.walk = controlled_walk
    CopyPageForTranslationAction._external_content_wrapped = True


def _derive_manifest_for_target(
    src_manifest_dict: Dict[str, Any],
    target_page: Page,
    policy: Any,
    store: ContentVersionStore,
    source_to_target_vid_map: Dict[str, str],
    created_version_ids: List[str],
) -> Dict[str, Any]:
    """External-content lifecycle validation helper."""
    source_manifest = ExternalContentManifest.from_dict(src_manifest_dict)
    target_manifest_dict: Dict[str, Any] = {
        "protocol_version": source_manifest.protocol_version,
        "content_storage_schema": source_manifest.content_storage_schema,
        "model_label": target_page._meta.label_lower,
        "page_id": target_page.pk,
        "fields": {},
    }
    for field_name, source_entry in source_manifest.fields.items():
        if field_name not in policy.external_fields:
            continue
        source_doc = store.get_version(
            source_entry.version_id,
            expected_identity={
                "model_label": source_manifest.model_label,
                "page_id": source_manifest.page_id,
                "field_name": field_name,
                "codec": source_entry.codec,
                "schema_version": source_entry.schema_version,
                "payload_sha256": source_entry.payload_sha256,
            },
        )
        dedup_key = (
            f"{field_name}:{source_entry.codec}:{source_entry.schema_version}:"
            f"{source_entry.payload_sha256}"
        )
        if dedup_key in source_to_target_vid_map:
            new_vid = source_to_target_vid_map[dedup_key]
        else:
            new_vid, is_new = store.save_version_with_status(
                model_label=target_page._meta.label_lower,
                page_id=target_page.pk,
                field_name=field_name,
                codec=source_entry.codec,
                schema_version=source_entry.schema_version,
                payload_sha256=source_entry.payload_sha256,
                payload=source_doc["payload"],
            )
            source_to_target_vid_map[dedup_key] = new_vid
            if is_new:
                created_version_ids.append(new_vid)
        target_manifest_dict["fields"][field_name] = {
            "codec": source_entry.codec,
            "schema_version": source_entry.schema_version,
            "version_id": new_vid,
            "payload_sha256": source_entry.payload_sha256,
            "model_label": target_page._meta.label_lower,
            "page_id": target_page.pk,
            "field_name": field_name,
        }
    return target_manifest_dict


def _validate_revision_manifest(
    page: Page,
    revision: Revision,
    manifest: ExternalContentManifest,
    store: ContentVersionStore,
    validate_business: bool = False,
) -> None:
    """External-content lifecycle validation helper."""
    base_ct = page.get_base_content_type()
    if revision.base_content_type_id != base_ct.id or str(revision.object_id) != str(page.pk):
        raise ManifestValidationError(
            f"External content revision does not belong to page",
            details={"error_category": "revision_identity_mismatch"},
        )
    manifest.validate_against_page(page)
    manifest.validate_against_revision(revision)
    revision_manifest = _manifest_from_revision(revision)
    if revision_manifest.to_dict() != manifest.to_dict():
        raise ManifestValidationError(
            f"External content lifecycle validation failed",
            details={"page_id": page.pk, "revision_id": revision.pk, "error_category": "manifest_drift"},
        )
    ExternalContentCoordinator(store=store).validate_manifest_payloads(manifest)
    if validate_business and hasattr(page, "validate_publication_revision"):
        page.validate_publication_revision(revision)


def _validate_published_pointer(
    page: Page,
    state: ExternalContentPublicationState,
    store: ContentVersionStore,
    *,
    revisions: List[Revision],
) -> tuple[Revision, ExternalContentManifest]:
    """只验证完整围栏内的正式 Revision，不在 Intent 层后另行补锁。"""
    if (
        not page.live
        or page.live_revision_id != state.published_revision_id
        or not state.published_revision_id
        or not state.published_manifest
    ):
        raise PublicationRevisionInvalidError(
            f"External content lifecycle validation failed",
            details={"page_id": page.pk, "error_category": "published_pointer_missing"},
        )
    revision = next(
        (row for row in revisions if row.pk == state.published_revision_id), None
    )
    if revision is None:
        raise PublicationRevisionInvalidError(
            f"External content lifecycle validation failed",
            details={"page_id": page.pk, "error_category": "revision_not_found"},
        )
    manifest = ExternalContentManifest.from_dict(state.published_manifest)
    _validate_revision_manifest(page, revision, manifest, store)
    return revision, manifest


def _manifest_from_revision(revision: Revision) -> ExternalContentManifest:
    """External-content lifecycle validation helper."""
    import json

    content = revision.content
    if isinstance(content, str):
        content = json.loads(content)
    if not isinstance(content, dict) or not isinstance(content.get("_external_content_manifest"), dict):
        raise PublicationRevisionInvalidError(
            f"External content lifecycle validation failed",
            details={"revision_id": revision.pk, "error_category": "manifest_missing"},
        )
    return ExternalContentManifest.from_dict(content["_external_content_manifest"])


def _alias_content_dict(content: Any) -> Dict[str, Any]:
    """只接受完整 JSON 对象快照，不接受页面实例、部分字段或其他隐式序列化形式。"""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise PublicationRevisionInvalidError("Alias Revision 内容不是有效 JSON 快照") from exc
    if not isinstance(content, dict):
        raise PublicationRevisionInvalidError("Alias Revision 内容必须是完整字典快照")
    return content


def _alias_snapshot_key(content: Any) -> str:
    """按 Revision 的持久化编码比较内存/数据库快照，保留 JSON 类型与非法值门禁。"""
    try:
        return json.dumps(
            _alias_content_dict(content),
            cls=Revision._meta.get_field("content").encoder,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PublicationRevisionInvalidError("Alias Revision 快照含非法 JSON 值") from exc


def _alias_revision_manifest(
    root: Page,
    revision: Revision,
    manifest_data: Dict[str, Any],
) -> ExternalContentManifest:
    """用数据库 Page/Revision 核验根身份及策略；此步骤不加载或写入正文。"""
    root_class = root.specific_class
    if (
        root_class is None
        or revision.content_type_id != root.content_type_id
        or revision.base_content_type_id != root.get_base_content_type().pk
        or str(revision.object_id) != str(root.pk)
    ):
        raise PublicationRevisionInvalidError("Alias 正式 Revision 不属于数据库根源页面")
    manifest = ExternalContentManifest.from_dict(manifest_data)
    manifest.validate_against_page(root.specific_deferred)
    manifest.validate_against_revision(revision)
    snapshot_manifest = _manifest_from_revision(revision)
    if snapshot_manifest.to_dict() != manifest.to_dict():
        raise PublicationRevisionInvalidError("Alias 正式 State 与 Revision Manifest 不一致")
    # 可选冗余身份键也必须一致，不能在 from_dict 归一化时悄悄丢弃篡改。
    for raw_manifest in (
        manifest_data,
        _alias_content_dict(revision.content)["_external_content_manifest"],
    ):
        for field_name, field_data in raw_manifest["fields"].items():
            expected = {
                "model_label": manifest.model_label,
                "page_id": manifest.page_id,
                "field_name": field_name,
            }
            for key, value in expected.items():
                if key in field_data and (
                    type(field_data[key]) is not type(value) or field_data[key] != value
                ):
                    raise PublicationRevisionInvalidError("Alias Manifest 字段冗余身份不一致")
    policy = get_policy_for_version(root_class, manifest.content_storage_schema)
    if (
        policy.schema_version != manifest.content_storage_schema
        or set(policy.external_fields) != set(manifest.fields)
    ):
        raise PublicationRevisionInvalidError("Alias Manifest 字段集合或存储策略不一致")
    for field_name, entry in manifest.fields.items():
        codec = policy.external_fields[field_name].codec
        if entry.codec != codec.codec_id or entry.schema_version != codec.schema_version:
            raise PublicationRevisionInvalidError("Alias Manifest Codec 身份不一致")
    return manifest


def _validate_alias_state(
    page: Page,
    state: Optional[ExternalContentPublicationState],
    root: Page,
    root_state: Optional[ExternalContentPublicationState],
    revisions: List[Revision],
    *,
    current: bool,
) -> None:
    """Alias 必须证明直属来源和根快照；目标允许尚未推进的自洽旧正式快照。"""
    if (
        state is None
        or state.model_label != root.specific_class._meta.label_lower
        or page.content_type_id != root.content_type_id
        or state.source_page_id != page.alias_of_id
        or state.published_revision_id is not None
        or state.draft_revision_id is not None
        or state.approved_revision_id is not None
        or state.draft_manifest is not None
        or state.approved_manifest is not None
    ):
        raise PublicationRevisionInvalidError("Alias State 缺失、模型或直属来源不一致")
    generation = root_state.publication_generation if root_state else 0
    if state.publication_generation > generation:
        raise PublicationRevisionInvalidError("Alias 发布代次超过根源正式状态")
    if state.published_manifest is None:
        if state.source_revision_id is not None or page.live:
            raise PublicationRevisionInvalidError("Alias 缺少正式 Manifest 却声明正式来源")
        if current and root_state and root_state.published_manifest:
            raise PublicationRevisionInvalidError("Alias 来源尚未绑定根源当前正式快照")
        return
    revision = next(
        (candidate for candidate in revisions if candidate.pk == state.source_revision_id), None
    )
    if revision is None:
        raise PublicationRevisionInvalidError("Alias State 未绑定归属正确的根源 Revision")
    manifest = _alias_revision_manifest(root, revision, state.published_manifest)
    if current and (
        root_state is None
        or state.source_revision_id != root_state.published_revision_id
        or state.publication_generation != root_state.publication_generation
        or manifest.to_dict()
        != ExternalContentManifest.from_dict(root_state.published_manifest).to_dict()
    ):
        raise PublicationRevisionInvalidError("Alias 来源链未绑定根源当前正式状态")


def _canonical_alias_source(
    page_id: int,
    *,
    locks: Optional[AliasLifecycleLocks] = None,
    allow_unpublished: bool = False,
) -> tuple[Page, Optional[ExternalContentPublicationState], Optional[Revision], Optional[ExternalContentManifest]]:
    """从真实数据库链解析正式根源；只读加载和事务入口共用同一身份门禁。"""
    if locks is None:
        chain = read_alias_source_chain(page_id)
        states = {
            state.page_id: state
            for state in ExternalContentPublicationState.objects.filter(
                page_id__in=[page.pk for page in chain]
            )
        }
        root = chain[-1]
        revisions = list(Revision.objects.for_instance(root).order_by("pk"))
    else:
        chain = [locks.pages[chain_id].page for chain_id in locks.source_chain]
        states = {
            chain_id: locks.pages[chain_id].state for chain_id in locks.source_chain
        }
        root = chain[-1]
        revisions = locks.pages[root.pk].revisions
    root_class = root.specific_class
    if root_class is None or any(page.content_type_id != root.content_type_id for page in chain):
        raise PublicationRevisionInvalidError("Alias 来源链的数据库模型类型不一致")
    policy = get_storage_policy_for_model(root_class, allow_record_new=False)
    if policy is None or not policy.external_fields:
        raise PublicationRevisionInvalidError("Alias 根源模型没有外置正文策略")
    root_state = states.get(root.pk)
    if root_state is not None and (
        root_state.model_label != root_class._meta.label_lower
        or root_state.source_page_id is not None
        or root_state.source_revision_id is not None
    ):
        raise PublicationRevisionInvalidError("Alias 根源 State 身份错误")
    revision = None
    manifest = None
    if root_state and root_state.published_revision_id and root_state.published_manifest:
        revision = next(
            (candidate for candidate in revisions if candidate.pk == root_state.published_revision_id),
            None,
        )
        if (
            revision is None
            or not root.live
            or root.live_revision_id != revision.pk
            or root_state.publication_generation < 1
        ):
            raise PublicationRevisionInvalidError("Alias 根源正式 Page/State/Revision 指针不一致")
        manifest = _alias_revision_manifest(root, revision, root_state.published_manifest)
    elif (
        not allow_unpublished
        or root.live
        or root.live_revision_id is not None
        or (root_state and (root_state.published_revision_id or root_state.published_manifest))
    ):
        raise PublicationRevisionInvalidError("Alias 根源缺少当前正式 Revision/Manifest")
    for alias in chain[:-1]:
        has_local_revisions = (
            bool(locks.pages[alias.pk].revisions)
            if locks is not None
            else Revision.objects.for_instance(alias).exists()
        )
        if has_local_revisions:
            raise PublicationRevisionInvalidError("Alias 不得拥有本地 Revision")
        _validate_alias_state(
            alias, states.get(alias.pk), root, root_state, revisions, current=True
        )
    return root, root_state, revision, manifest


def validate_alias_manifest_source(page: Page, manifest: ExternalContentManifest) -> None:
    """跨页读取只认数据库 Alias 链及正式 State，不凭 in_alias 或可变实例放行。"""
    root, _, revision, canonical_manifest = _canonical_alias_source(page.pk)
    if (
        root.pk == page.pk
        or page._meta.label_lower != root.specific_class._meta.label_lower
        or revision is None
        or canonical_manifest is None
        or canonical_manifest.to_dict() != manifest.to_dict()
    ):
        raise PublicationRevisionInvalidError("跨页 Manifest 未获数据库 Alias 来源链证明")


def _validate_alias_targets(
    locks: AliasLifecycleLocks,
    root: Page,
    root_state: Optional[ExternalContentPublicationState],
    *,
    new_alias_id: Optional[int] = None,
) -> None:
    """推进前校验整个递归闭包，旧目标快照必须是根源 Revision 的完整合法凭证。"""
    for target_id in locks.target_ids:
        if target_id == locks.source_chain[0]:
            continue
        target = locks.pages[target_id]
        if target.page.content_type_id != root.content_type_id or target.revisions:
            raise PublicationRevisionInvalidError("Alias 目标模型错误或存在本地 Revision")
        if target_id == new_alias_id and target.state is None:
            continue
        _validate_alias_state(
            target.page, target.state, root, root_state,
            locks.pages[root.pk].revisions, current=False,
        )


def prepare_alias_update(
    source_page: Page,
    revision: Optional[Revision] = None,
    content: Any = None,
) -> tuple[Revision, Dict[str, Any]]:
    """在外层事务中围栏并绑定根源正式 DB 快照，返回互不共享的 canonical pair。

    不修复来源状态，不创建 Alias Revision，不豁免排期。调用方必须让任何
    验证、锁或递归保存异常退出整个发布事务；本函数不提前释放行锁。
    """
    if not connection.in_atomic_block:
        raise RuntimeError("prepare_alias_update 必须在外层 transaction.atomic 中调用")
    locks = lock_alias_lifecycle_closure(source_page.pk)
    root, root_state, canonical_revision, manifest = _canonical_alias_source(
        source_page.pk, locks=locks
    )
    if (
        source_page._meta.label_lower != root.specific_class._meta.label_lower
        or canonical_revision is None
        or manifest is None
    ):
        raise PublicationRevisionInvalidError("Alias 同步源实例与数据库来源身份不一致")
    canonical_content = _alias_content_dict(canonical_revision.content)
    snapshot_key = _alias_snapshot_key(canonical_content)
    if revision is not None and (
        not isinstance(revision, Revision)
        or revision.pk != canonical_revision.pk
        or revision.content_type_id != canonical_revision.content_type_id
        or revision.base_content_type_id != canonical_revision.base_content_type_id
        or str(revision.object_id) != str(canonical_revision.object_id)
        or _alias_snapshot_key(revision.content) != snapshot_key
    ):
        raise PublicationRevisionInvalidError("Alias 输入 Revision 不是根源当前正式 DB 快照")
    if content is not None and _alias_snapshot_key(content) != snapshot_key:
        raise PublicationRevisionInvalidError("Alias 输入内容不是完整根源正式 DB 快照")
    _validate_alias_targets(locks, root, root_state)
    ExternalContentCoordinator(
        store=getattr(source_page, "_test_content_store", None)
    ).validate_manifest_payloads(manifest)
    canonical_revision.content = deepcopy(canonical_content)
    return canonical_revision, deepcopy(canonical_content)


def duplicate_external_content_for_copy(
    source_page: Page,
    target_page: Page,
    keep_live: bool = True,
    copy_revisions: bool = True,
    user: Any = None,
) -> None:
    """
    为复制或派生生成的目标页面创建专属目标页面的独立不可变正文版本与 Manifest。

    步骤：
    1. 确认目标页面具备持久化主键；
    2. 从源页面向目标页面内存补全实体正文数据；
    3. 调用 ExternalContentCoordinator 为目标页面准备专属不可变版本写入 MongoDB（以 target_page.pk 标识隔离）；
    4. 若 copy_revisions=True，遍历已复制给目标页面的历史 Revision 快照，将其中的
       _external_content_manifest 重绑定为 target_page.pk 与独立派生版本；
       引入 source_to_target_vid_map 缓存，同一源版本内容在目标页面复用相同目标版本 ID，
       减少 MongoDB 存储空间占用；
    5. SQL 失败不撤销 Mongo 准备版本，保留孤儿等待独立对账，不做物理补偿删除；
    6. 更新目标页面的 ExternalContentPublicationState；
    7. 若 keep_live=True，同步更新 published_manifest 并推进发布世代；若 keep_live=False，仅保留 draft_manifest。

    :param source_page: 源页面实例
    :param target_page: 复制产生的目标页面实例
    :param keep_live: 是否保持已发布状态
    :param copy_revisions: 是否复制并重绑定历史版本快照
    :param user: 操作用户

    此遗留辅助入口未由当前 CopyPageAction 使用；其锁前读取、逐页取锁和
    Revision 推断尚未满足 D3 闭包契约，不应作为新增复制/翻译入口调用。
    """
    if not target_page.pk:
        raise ExternalContentError("复制正文外置失败：目标页面尚未取得持久化主键 ID")

    policy = get_storage_policy_for_model(type(target_page), allow_record_new=False)
    if not policy or not policy.external_fields:
        return

    # 确保目标页面在内存中具备各外置字段的实体数据
    for field_name in policy.external_fields.keys():
        val = getattr(target_page, field_name, None)
        if val is None or val == policy.external_fields[field_name].codec.get_empty_sentinel():
            source_val = getattr(source_page, field_name, None)
            if source_val is not None:
                setattr(target_page, field_name, source_val)

    coordinator = ExternalContentCoordinator()
    store = coordinator.store

    # 记录准备版本供故障对账；SQL 回滚不会撤销 Mongo，不能据此物理删除。
    created_version_ids: List[str] = []
    # 源版本 ID 到目标版本 ID 的内存映射缓存，用于减少存储膨胀
    source_to_target_vid_map: Dict[str, str] = {}

    try:
        # 遗留单页锁路径不构成 D3 全闭包分层顺序，需另行替换与验证。
        from external_content.locking import lock_page_lifecycle_hierarchy
        from django.db import transaction

        with transaction.atomic():
            # page_id 排序仍是逐页全层锁，不能作为跨页无死锁证明。
            first_id, second_id = sorted([source_page.pk, target_page.pk])
            first_locks = lock_page_lifecycle_hierarchy(
                first_id, revision_ids=None, check_write=True
            )
            second_locks = lock_page_lifecycle_hierarchy(
                second_id, revision_ids=None, check_write=True
            )

            # 根据实际 page_id 分配锁对象
            if source_page.pk == first_id:
                source_locks, target_locks = first_locks, second_locks
            else:
                source_locks, target_locks = second_locks, first_locks

            # 使用锁定后的 state
            source_state = source_locks.state
            source_specific = source_locks.page.specific
            source_published_revision = None
            if source_specific.live:
                if source_state is None:
                    raise PublicationRevisionInvalidError(
                        f"Source page has an invalid or missing published pointer",
                        details={"page_id": source_specific.pk, "error_category": "published_pointer_missing"},
                    )
                source_published_revision, _ = _validate_published_pointer(
                    source_specific, source_state, store, revisions=source_locks.revisions
                )
            source_effective_manifest = None
            if source_state:
                source_effective_manifest = source_state.published_manifest or source_state.draft_manifest

            # 为 target_page 当前正文生成独立最新版本的 Manifest
            target_manifest = coordinator.prepare_external_content_for_revision(target_page)
            for field_name, field_ptr in target_manifest.fields.items():
                created_version_ids.append(field_ptr.version_id)
                dedup_key = f"{field_name}:{field_ptr.payload_sha256}"
                source_to_target_vid_map[dedup_key] = field_ptr.version_id
                if source_effective_manifest:
                    src_field_ptr = source_effective_manifest.get("fields", {}).get(field_name)
                    if src_field_ptr and src_field_ptr.get("payload_sha256") == field_ptr.payload_sha256:
                        if "version_id" in src_field_ptr:
                            source_to_target_vid_map[src_field_ptr["version_id"]] = field_ptr.version_id

            # 若复制历史快照，逐个重构快照中的 Manifest 与正文版本
            if copy_revisions:
                target_revisions = list(Revision.objects.for_instance(target_page).order_by("pk"))
                for rev in target_revisions:
                    content = rev.content
                    if not isinstance(content, dict) or "_external_content_manifest" not in content:
                        continue

                    rev_manifest_dict = content["_external_content_manifest"]
                    if not isinstance(rev_manifest_dict, dict):
                        continue

                    # Validate external-content identity before lifecycle pointer writes.
                    source_revision_manifest = ExternalContentManifest.from_dict(rev_manifest_dict)
                    manifest_modified = False
                    fields_dict = rev_manifest_dict.get("fields", {})

                    # 验证 Manifest 必需字段
                    if not fields_dict or not isinstance(fields_dict, dict):
                        from external_content.exceptions import ManifestValidationError
                        raise ManifestValidationError(
                            "Manifest 缺失 'fields' 字段或格式错误，无法复制",
                            details={"error_category": "invalid_manifest"}
                        )

                    for field_name, field_data in fields_dict.items():
                        if field_name not in policy.external_fields:
                            continue

                        # 验证字段数据的完整性
                        if not isinstance(field_data, dict):
                            from external_content.exceptions import ManifestValidationError
                            raise ManifestValidationError(
                                f"Manifest 字段 '{field_name}' 数据格式错误，无法复制",
                                details={"field_name": field_name, "error_category": "invalid_field_data"}
                            )

                        src_vid = field_data.get("version_id")
                        if not src_vid:
                            continue

                        # 验证 FieldManifestEntry 必需字段（不包括外层的 model_label/page_id/field_name）
                        required_field_keys = ["codec", "schema_version", "version_id", "payload_sha256"]
                        for key in required_field_keys:
                            if key not in field_data:
                                from external_content.exceptions import ManifestValidationError
                                raise ManifestValidationError(
                                    f"Manifest 字段 '{field_name}' 缺少必需的键 '{key}'，无法复制",
                                    details={"field_name": field_name, "missing_key": key, "error_category": "missing_field_key"}
                                )

                        # 使用内容哈希作为去重键，而不是源版本 ID
                        # 这样相同内容（不同 Revision）可以复用同一个目标版本
                        payload_sha256 = field_data.get("payload_sha256")
                        dedup_key = f"{field_name}:{payload_sha256}"

                        if dedup_key in source_to_target_vid_map:
                            # 命中复用缓存，直接复用已派生的独立版本，防止存储膨胀
                            new_vid = source_to_target_vid_map[dedup_key]
                        else:
                            # 未缓存，读取源版本的 payload
                            # 使用 get_raw_version_document 避免严格的身份校验
                            # 因为历史 Revision 的 Manifest 可能记录的是不同的 page_id
                            # Validate external-content identity before lifecycle pointer writes.
                            source_entry = source_revision_manifest.fields.get(field_name)
                            if source_entry is None:
                                raise ManifestValidationError(
                                    f"Source revision manifest is missing an external field",
                                    details={"field_name": field_name, "error_category": "invalid_manifest"},
                                )
                            src_doc = store.get_version(
                                src_vid,
                                expected_identity={
                                    "model_label": source_revision_manifest.model_label,
                                    "page_id": source_revision_manifest.page_id,
                                    "field_name": field_name,
                                    "codec": source_entry.codec,
                                    "schema_version": source_entry.schema_version,
                                    "payload_sha256": source_entry.payload_sha256,
                                },
                            )

                            # Validate external-content identity before lifecycle pointer writes.
                            new_vid, is_new = store.save_version_with_status(
                                model_label=target_page._meta.label_lower,
                                page_id=target_page.pk,
                                field_name=field_name,
                                codec=source_entry.codec,
                                schema_version=source_entry.schema_version,
                                payload_sha256=source_entry.payload_sha256,
                                payload=src_doc["payload"],
                            )
                            source_to_target_vid_map[dedup_key] = new_vid
                            if is_new:
                                created_version_ids.append(new_vid)

                        field_data["version_id"] = new_vid
                        field_data["model_label"] = target_page._meta.label_lower
                        field_data["page_id"] = target_page.pk
                        field_data["field_name"] = field_name
                        field_data["codec"] = field_data.get("codec")
                        field_data["schema_version"] = field_data.get("schema_version")
                        field_data["payload_sha256"] = payload_sha256
                        manifest_modified = True

                    if rev_manifest_dict.get("page_id") != target_page.pk:
                        rev_manifest_dict["page_id"] = target_page.pk
                        manifest_modified = True

                    if rev_manifest_dict.get("model_label") != target_page._meta.label_lower:
                        rev_manifest_dict["model_label"] = target_page._meta.label_lower
                        manifest_modified = True

                    if manifest_modified:
                        content["_external_content_manifest"] = rev_manifest_dict
                        rev.content = content
                        rev.save(update_fields=["content"])

            # 更新目标页面的 PublicationState（在同一事务内）
            state, _ = ExternalContentPublicationState.objects.select_for_update().get_or_create(
                page_id=target_page.pk,
                defaults={"model_label": target_page._meta.label_lower, "publication_generation": 0},
            )

            update_fields = ["draft_manifest", "updated_at"]
            if source_state and source_state.draft_manifest:
                target_draft_manifest = _derive_manifest_for_target(
                    src_manifest_dict=source_state.draft_manifest,
                    target_page=target_page,
                    policy=policy,
                    store=store,
                    source_to_target_vid_map=source_to_target_vid_map,
                    created_version_ids=created_version_ids,
                )
            else:
                target_draft_manifest = target_manifest.to_dict()

            state.draft_manifest = target_draft_manifest

            if keep_live and target_page.live:
                # Validate external-content identity before lifecycle pointer writes.
                # Validate external-content identity before lifecycle pointer writes.
                if source_state and source_state.published_manifest and source_published_revision:
                    source_revisions = list(source_locks.revisions)
                    target_revisions = list(Revision.objects.for_instance(target_page).select_for_update().order_by("pk"))
                    candidate_revisions: List[Revision] = []
                    try:
                        source_index = [rev.pk for rev in source_revisions].index(source_published_revision.pk)
                    except ValueError:
                        source_index = -1
                    if source_index >= 0 and source_index < len(target_revisions):
                        candidate_revisions.append(target_revisions[source_index])
                    # Validate external-content identity before lifecycle pointer writes.
                    source_hashes = {
                        field_name: field_data.get("payload_sha256")
                        for field_name, field_data in source_state.published_manifest.get("fields", {}).items()
                    }
                    for candidate in target_revisions:
                        if candidate not in candidate_revisions:
                            try:
                                candidate_manifest = _manifest_from_revision(candidate)
                            except Exception:
                                continue
                            candidate_hashes = {
                                field_name: field_data.payload_sha256
                                for field_name, field_data in candidate_manifest.fields.items()
                            }
                            if candidate_hashes == source_hashes:
                                candidate_revisions.append(candidate)
                    if not candidate_revisions:
                        raise PublicationRevisionInvalidError(
                            f"Target page has no matching published revision",
                            details={"page_id": target_page.pk, "error_category": "revision_not_found"},
                        )
                    published_revision = candidate_revisions[0]
                    published_manifest = _manifest_from_revision(published_revision)
                    _validate_revision_manifest(target_locks.page.specific, published_revision, published_manifest, store, validate_business=True)
                    state.published_manifest = published_manifest.to_dict()
                    state.published_revision_id = published_revision.pk
                    # 原生复制把最新草稿设为 live_revision；正式指针必须与已校验的发布快照同事务对齐。
                    Page.objects.filter(pk=target_page.pk).update(
                        live_revision_id=published_revision.pk,
                    )
                    target_page.live_revision = published_revision
                else:
                    raise PublicationRevisionInvalidError(
                        f"Target page has no matching published revision",
                        details={"page_id": target_page.pk, "error_category": "published_pointer_missing"},
                    )
                state.publication_generation += 1
                update_fields.extend(["published_manifest", "published_revision_id", "publication_generation"])
            else:
                state.published_manifest = None
                state.published_revision_id = None
                update_fields.extend(["published_manifest", "published_revision_id"])

            state.save(update_fields=update_fields)

            # 内存同步
            effective_manifest = state.published_manifest or state.draft_manifest
            if effective_manifest and "fields" in effective_manifest:
                target_page._external_content_loaded_hashes = {
                    field_name: field_ptr.get("payload_sha256")
                    for field_name, field_ptr in effective_manifest["fields"].items()
                }
    except Exception as exc:
        logger.error(
            "external_content_copy_failed target_page=%s rolling_back_versions=%s err=%s",
            target_page.pk,
            created_version_ids,
            exc,
        )
        if isinstance(exc, (ManifestError, ContentStoreError, ExternalContentError)):
            raise
        raise ExternalContentError(f"页面复制外置正文处理失败: {exc}") from exc

    logger.info(
        "external_content_duplicated_for_copy source_page=%s target_page=%s keep_live=%s manifest_page_id=%s versions_created=%d",
        source_page.pk,
        target_page.pk,
        keep_live,
        target_manifest.page_id,
        len(created_version_ids),
    )


def setup_external_content_for_alias(
    source_page: Page,
    alias_page: Page,
    user: Any = None,
) -> ExternalContentPublicationState:
    """初始化直属托管关系；共享正式根源 Manifest，不创建本地 Alias Revision。"""
    with transaction.atomic():
        locks = lock_alias_lifecycle_closure(source_page.pk)
        root, root_state, source_revision, source_manifest = _canonical_alias_source(
            source_page.pk, locks=locks, allow_unpublished=True
        )
        alias_locks = locks.pages.get(alias_page.pk)
        if (
            alias_locks is None
            or alias_locks.page.alias_of_id != source_page.pk
            or alias_page._meta.label_lower != root.specific_class._meta.label_lower
            or source_page._meta.label_lower != root.specific_class._meta.label_lower
        ):
            raise PublicationRevisionInvalidError("Alias 初始化目标不是数据库直属同类型别名")
        _validate_alias_targets(locks, root, root_state, new_alias_id=alias_page.pk)
        if source_manifest is not None:
            ExternalContentCoordinator(
                store=getattr(source_page, "_test_content_store", None)
            ).validate_manifest_payloads(source_manifest)
        state = alias_locks.state
        if state is None:
            state = ExternalContentPublicationState.objects.create(
                page_id=alias_page.pk,
                model_label=alias_page._meta.label_lower,
                publication_generation=0,
            )
        state.source_page_id = source_page.pk
        state.source_revision_id = source_revision.pk if source_revision else None
        state.published_manifest = source_manifest.to_dict() if source_manifest else None
        state.published_revision_id = None
        state.publication_generation = root_state.publication_generation if root_state else 0
        state.save(
            update_fields=[
                "source_page_id",
                "source_revision_id",
                "published_manifest",
                "published_revision_id",
                "publication_generation",
                "updated_at",
            ]
        )

    logger.info(
        "external_content_alias_setup alias_page=%s source_page=%s",
        alias_page.pk,
        source_page.pk,
    )
    return state


def sync_external_content_aliases(
    source_page: Page,
    revision: Any = None,
) -> List[int]:
    """推进直属 Alias State，根源身份与直属 source 分离；原生代码负责递归保存页面。"""
    with transaction.atomic():
        # 发布信号也直接调用本入口，不能依赖 Mixin 已经替它验证过输入。
        canonical_revision, canonical_content = prepare_alias_update(source_page, revision=revision)
        locks = lock_alias_lifecycle_closure(source_page.pk)
        alias_ids = [
            target_id for target_id in locks.target_ids
            if locks.pages[target_id].page.alias_of_id == source_page.pk
        ]
        if not alias_ids:
            return []
        root_state = locks.pages[locks.source_chain[-1]].state
        if root_state is None:
            raise PublicationRevisionInvalidError("Alias 同步根源正式 State 缺失")
        source_manifest = ExternalContentManifest.from_dict(
            canonical_content["_external_content_manifest"]
        )
        updated_alias_ids: List[int] = []
        for alias_id in alias_ids:
            alias_locks = locks.pages[alias_id]
            state = alias_locks.state
            if state is None:
                raise PublicationRevisionInvalidError("Alias 同步目标 State 缺失")
            state.source_page_id = source_page.pk
            state.source_revision_id = canonical_revision.pk
            state.published_manifest = source_manifest.to_dict()
            state.published_revision_id = None
            state.publication_generation = root_state.publication_generation
            state.save(
                update_fields=[
                    "source_page_id",
                    "source_revision_id",
                    "published_manifest",
                    "published_revision_id",
                    "publication_generation",
                    "updated_at",
                ]
            )
            updated_alias_ids.append(alias_id)

    logger.info(
        "external_content_aliases_synced source_page=%s count=%d",
        source_page.pk,
        len(updated_alias_ids),
    )
    return updated_alias_ids


def _lock_copy_sources(action: Any) -> Dict[str, Any]:
    """Mongo 读取前 NOWAIT 围栏源树及目标父页，用当前锁定读复核树位置。"""
    source = Page.objects.get(pk=action.page.pk)
    sources = (
        list(source.get_descendants(inclusive=True).order_by("pk"))
        if action.recursive else [source]
    )
    parent_id = action.to.pk if action.to is not None else source.get_parent().pk
    discovered = {page.pk: page for page in sources}
    discovered[parent_id] = Page.objects.get(pk=parent_id)
    def validate_pages(pages: Any) -> None:
        """仅在 Page 层当前读复核，不在 State/Intent 后发现并增补源节点。"""
        for page_id, expected in discovered.items():
            actual = pages[page_id]
            if (
                actual.path, actual.depth, actual.content_type_id, actual.alias_of_id
            ) != (
                expected.path, expected.depth, expected.content_type_id, expected.alias_of_id
            ):
                raise ExternalContentOperationBlockedError("复制获锁期间页面拓扑发生变化")
        if action.recursive:
            current_ids = set(
                Page.objects.select_for_update(nowait=True)
                .filter(path__startswith=source.path)
                .values_list("pk", flat=True)
            )
            if current_ids != {page.pk for page in sources}:
                raise ExternalContentOperationBlockedError("复制获锁期间源子树发生变化")
    external_ids = []
    for page in sources:
        policy = get_storage_policy_for_model(page.specific_class, allow_record_new=False)
        if policy and policy.external_fields:
            external_ids.append(page.pk)
    locks, alias_locks = lock_alias_lifecycle_closures(
        external_ids, additional_page_ids=sorted(discovered),
        validate_pages=validate_pages,
    )
    return {"locks": locks, "alias_locks": alias_locks}


def _copy_node_revisions(
    source: Page, frame: Dict[str, Any], store: ContentVersionStore, *, keep_live: bool
) -> tuple[List[Revision], Revision, Optional[Revision]]:
    """只用锁定的真实 Revision 主键选择 A/B，Alias 仅物化 canonical 正式快照。"""
    locks = frame["locks"][source.pk]
    if source.alias_of_id:
        _, _, revision, manifest = _canonical_alias_source(
            source.pk, locks=frame["alias_locks"][source.pk]
        )
        if revision is None or manifest is None:
            raise PublicationRevisionInvalidError("Alias 复制缺少正式正文快照")
        ExternalContentCoordinator(store=store).validate_manifest_payloads(manifest)
        return [revision], revision, revision if keep_live and source.live else None
    state = locks.state
    if (
        state is None
        or state.model_label != source._meta.label_lower
        or state.source_page_id is not None
        or state.source_revision_id is not None
        or state.draft_revision_id != source.latest_revision_id
        or not state.draft_manifest
    ):
        raise PublicationRevisionInvalidError("复制源草稿 Page/State 身份或指针不一致")
    revisions = locks.revisions
    latest = next(
        (revision for revision in revisions if revision.pk == state.draft_revision_id), None
    )
    if latest is None:
        raise PublicationRevisionInvalidError("复制源最新 Revision 缺失")
    _alias_revision_manifest(source, latest, state.draft_manifest)
    published = None
    if source.live:
        published, _ = _validate_published_pointer(
            source, state, store, revisions=locks.revisions
        )
        _alias_revision_manifest(source, published, state.published_manifest)
    return revisions, latest, published if keep_live else None


def _clone_copy_revision(
    action: Any,
    source: Page,
    target: Page,
    revision: Revision,
    *,
    child_map: Dict[Any, Any],
    excluded: List[str],
    target_data: Dict[str, Any],
    overrides: Dict[str, Any],
    policy: Any,
    store: ContentVersionStore,
    version_map: Dict[str, str],
    created_versions: List[str],
) -> Revision:
    """先派生目标 Manifest 再保存历史快照；绝不走 save_revision 重写历史正文。"""
    from modelcluster.models import get_all_child_relations

    content = deepcopy(_alias_content_dict(revision.content))
    manifest = _alias_revision_manifest(
        source, revision, content["_external_content_manifest"]
    )
    content["_external_content_manifest"] = _derive_manifest_for_target(
        manifest.to_dict(), target, policy, store, version_map, created_versions
    )
    content["pk"] = target.pk
    for relation in get_all_child_relations(source):
        accessor = relation.get_accessor_name()
        for child in content.get(accessor, []):
            child[relation.field.name] = target.pk
            copied_child = child_map.get((relation, child["pk"]))
            child["pk"] = copied_child.pk if copied_child else None
            if action.reset_translation_key and "translation_key" in child:
                child["translation_key"] = action.generate_translation_key(child["translation_key"])
    for field_name in excluded:
        if field_name in content:
            content[field_name] = target_data.get(field_name)
    for field_name in overrides:
        model_field = target._meta.get_field(field_name)
        if not model_field.many_to_many:
            serialized_name = model_field.name
            content[serialized_name] = target_data.get(serialized_name)
    clone = deepcopy(revision)
    clone.pk = None
    clone._state.adding = True
    clone.object_id = str(target.pk)
    clone.approved_go_live_at = None
    clone.content = content
    clone.save(force_insert=True)
    return clone


def _copy_external_node(
    action: Any,
    source: Page,
    *,
    to: Optional[Page],
    update_attrs: Optional[Dict[str, Any]],
    exclude_fields: Optional[List[str]],
    mpnode_attrs: Any,
    frame: Dict[str, Any],
) -> Page:
    """接入 Wagtail 8.0 节点复制边界，信号前完成正文、A/B 指针和业务门禁。"""
    from wagtail.log_actions import log
    from wagtail.models import PageViewRestriction
    from wagtail.models.copying import _copy, _copy_m2m_relations
    from wagtail.models.i18n import TranslatableMixin
    from wagtail.signals import page_published
    from external_content.mixins import _suppress_external_content_autoload

    policy = get_storage_policy_for_model(type(source), allow_record_new=False)
    excluded = (
        source.default_exclude_fields_in_copy
        + source.exclude_fields_in_copy
        + (exclude_fields or [])
    )
    overrides = update_attrs or {}
    protected = set(policy.external_fields) | {
        "pk", "id", "path", "depth", "numchild", "alias_of", "alias_of_id",
        "live", "live_revision", "live_revision_id", "latest_revision", "latest_revision_id",
    }
    if set(overrides) & protected or set(excluded) & set(policy.external_fields):
        raise ExternalContentError("外置复制禁止覆盖正文、生命周期身份或排除正文")
    if to is not None and to.pk not in frame["locks"]:
        # 现有父页必须属于入口发现的闭包；变化只能退出整个 Action 事务。
        raise ExternalContentOperationBlockedError("复制目标父页不在原有生命周期闭包中")
    store = ContentVersionStore()
    revisions, latest, published = _copy_node_revisions(
        source, frame, store, keep_live=action.keep_live
    )
    base_attrs = {"alias_of": None, **overrides}
    if action.user:
        base_attrs["owner"] = action.user
    if action.reset_translation_key:
        base_attrs["translation_key"] = uuid.uuid4()
    with _suppress_external_content_autoload():
        target, child_map = _copy(source, exclude_fields=excluded, update_attrs=base_attrs)
        target.alias_of_id = None
        target.live = False
        target.live_revision_id = None
        target.latest_revision_id = None
        target.first_published_at = None
        target.last_published_at = None
        for field_name, config in policy.external_fields.items():
            setattr(target, field_name, config.codec.get_empty_sentinel())
        for (relation, _), child in child_map.items():
            if action.process_child_object:
                action.process_child_object(source, target, relation, child)
            if action.reset_translation_key and isinstance(child, TranslatableMixin):
                child.translation_key = action.generate_translation_key(child.translation_key)
        if mpnode_attrs:
            target.path, target.depth = mpnode_attrs
            target.save(clean=False)
        elif to is not None:
            target = to.add_child(instance=target)
        else:
            target = source.add_sibling(instance=target)
        mpnode_attrs = (target.path, target.depth)
        frame["locks"][target.pk] = lock_page_lifecycle_hierarchy(
            target.pk, revision_ids=None, check_write=True, nowait=True
        )
        _copy_m2m_relations(source, target, exclude_fields=excluded, update_attrs=base_attrs)
        target_data = target.serializable_data()
    selected = revisions if action.copy_revisions and not source.alias_of_id else [
        revision for revision in revisions
        if revision.pk in {latest.pk, published.pk if published else None}
    ]
    canonical_source = (
        frame["alias_locks"][source.pk].pages[
            frame["alias_locks"][source.pk].source_chain[-1]
        ].page.specific_deferred
        if source.alias_of_id else source
    )
    revision_map: Dict[int, Revision] = {}
    version_map: Dict[str, str] = {}
    created_versions: List[str] = []
    for revision in selected:
        clone = _clone_copy_revision(
            action, canonical_source, target, revision, child_map=child_map,
            excluded=excluded, target_data=target_data,
            overrides=overrides if revision.pk == latest.pk else {},
            policy=policy, store=store, version_map=version_map,
            created_versions=created_versions,
        )
        revision_map[revision.pk] = clone
    draft = revision_map[latest.pk]
    live_revision = revision_map[published.pk] if published else None
    draft_manifest = _manifest_from_revision(draft)
    live_manifest = _manifest_from_revision(live_revision) if live_revision else None
    now = timezone.now()
    ExternalContentPublicationState.objects.create(
        page_id=target.pk, model_label=target._meta.label_lower,
        draft_revision_id=draft.pk, draft_manifest=draft_manifest.to_dict(),
        published_revision_id=live_revision.pk if live_revision else None,
        published_manifest=live_manifest.to_dict() if live_manifest else None,
        publication_generation=1 if live_revision else 0,
        published_at=now if live_revision else None,
    )
    target.latest_revision = draft
    target.live_revision = live_revision
    target.live = live_revision is not None
    target.draft_title = draft.content.get("title", target.title)
    target.has_unpublished_changes = live_revision is None or draft.pk != live_revision.pk
    target.first_published_at = now if live_revision else None
    target.last_published_at = now if live_revision else None
    target.save(clean=False, update_fields=[
        "latest_revision", "live_revision", "live", "draft_title",
        "has_unpublished_changes",
        "first_published_at", "last_published_at",
    ])
    effective_manifest = live_manifest or draft_manifest
    ExternalContentCoordinator(store=store).load_external_content_from_manifest(
        target, effective_manifest
    )
    target._external_content_loaded_hashes = {
        name: entry.payload_sha256 for name, entry in effective_manifest.fields.items()
    }
    if live_revision:
        _validate_revision_manifest(
            target, live_revision, live_manifest, store, validate_business=True
        )
        page_published.send(
            sender=target.specific_class, instance=target, revision=live_revision
        )
    if action.log_action:
        parent = source.get_parent()
        log(instance=target, action=action.log_action, user=action.user, data={
            "page": {
                "id": target.pk, "title": target.get_admin_display_title(),
                "locale": {"id": target.locale_id, "language_code": target.locale.language_code},
            },
            "source": {
                "id": parent.pk, "title": parent.specific_deferred.get_admin_display_title(),
            } if parent else None,
            "destination": {
                "id": to.pk, "title": to.specific_deferred.get_admin_display_title(),
            } if to else None,
            "keep_live": target.live and action.keep_live,
            "source_locale": {"id": source.locale_id, "language_code": source.locale.language_code},
        })
        if live_revision:
            log(instance=target, action="wagtail.publish", user=action.user, revision=live_revision)
    if action.recursive:
        numchild = 0
        for child in source.get_children().specific().iterator():
            newdepth = mpnode_attrs[1] + 1
            child_attrs = (
                Page._get_path(mpnode_attrs[0], newdepth, numchild), newdepth
            )
            numchild += 1
            action._copy_page(child, to=target, _mpnode_attrs=child_attrs)
        if numchild:
            target.numchild = numchild
            target.save(clean=False, update_fields=["numchild"])
    restriction_parent = to if to is not None else action.page.get_parent()
    if not restriction_parent.get_view_restrictions().exists():
        for restriction in action.page.view_restrictions.all():
            copied_restriction = PageViewRestriction(
                restriction_type=restriction.restriction_type,
                password=restriction.password, page=target,
            )
            copied_restriction.save(user=action.user)
            copied_restriction.groups.set(restriction.groups.all())
    logger.info("external_content_copy_complete source=%s target=%s", source.pk, target.pk)
    return target


def wrap_copy_page_action() -> None:
    """只包装 Action 实例的节点入口，不改全局信号，也不豁免 Manifest 身份。"""
    from wagtail.actions.copy_page import CopyPageAction

    if getattr(CopyPageAction, "_external_content_wrapped", False):
        return
    original_execute = CopyPageAction.execute
    original_copy_page = CopyPageAction._copy_page

    @_translation_insert_scope()
    def controlled_copy_page(
        self: Any, page: Page, to: Optional[Page] = None,
        update_attrs: Optional[Dict[str, Any]] = None,
        exclude_fields: Optional[List[str]] = None, _mpnode_attrs: Any = None,
    ) -> Page:
        frame = getattr(self, "_external_content_copy_frame", None)
        policy = get_storage_policy_for_model(page.specific_class, allow_record_new=False)
        if not policy or not policy.external_fields:
            return original_copy_page(
                self, page, to=to, update_attrs=update_attrs,
                exclude_fields=exclude_fields, _mpnode_attrs=_mpnode_attrs,
            )
        if frame is None or page.pk not in frame["locks"]:
            raise ExternalContentOperationBlockedError("外置节点复制缺少 Action 事务围栏")
        source = frame["locks"][page.pk].page.specific
        return _copy_external_node(
            self, source, to=to, update_attrs=update_attrs,
            exclude_fields=exclude_fields, mpnode_attrs=_mpnode_attrs, frame=frame,
        )

    def controlled_copy_execute(self: Any, skip_permission_checks: bool = False) -> Page:
        self.check(skip_permission_checks=skip_permission_checks)
        translation = getattr(translation_lifecycle_context, "frame", None)
        if translation is not None:
            translation_member(self.page.pk)
            if self.to is not None:
                translation_member(self.to.pk)
            with preserve_threadlocal_context(self, "_external_content_copy_frame", translation):
                return original_execute(self, skip_permission_checks=skip_permission_checks)
        sources = (
            self.page.get_descendants(inclusive=True)
            if self.recursive else [self.page]
        )
        has_external = any(
            (policy := get_storage_policy_for_model(
                page.specific_class, allow_record_new=False
            )) and policy.external_fields
            for page in sources
        )
        if not has_external:
            return original_execute(self, skip_permission_checks=skip_permission_checks)
        with transaction.atomic():
            frame = _lock_copy_sources(self)
            # 缺省与显式 None 不等价；嵌套及异常退出都恢复调用前的精确绑定。
            with preserve_threadlocal_context(self, "_external_content_copy_frame", frame):
                return original_execute(self, skip_permission_checks=skip_permission_checks)

    CopyPageAction._copy_page = controlled_copy_page
    CopyPageAction.execute = controlled_copy_execute
    CopyPageAction._external_content_wrapped = True


def wrap_create_page_alias_action() -> None:
    """
    原生 Action 的页面、树、日志与逐层外置状态共同提交或回滚。
    每层窗口仅授权其直属来源，包含普通 Index 下递归创建的外置子页。
    """
    try:
        from wagtail.actions.create_alias import CreatePageAliasAction
    except ImportError:
        return

    if getattr(CreatePageAliasAction, "_external_content_wrapped", False):
        return

    original_execute = CreatePageAliasAction.execute
    original_create_alias = CreatePageAliasAction._create_alias

    def controlled_create_alias_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        with transaction.atomic():
            return original_execute(self, *args, **kwargs)

    @_translation_insert_scope()
    def controlled_create_alias(self: Any, page: Page, **kwargs: Any) -> Page:
        from external_content.mixins import _controlled_alias_context

        # 私有入口也保持原子边界；新增来源锁全部 NOWAIT，不先请求阻塞源锁。
        with transaction.atomic():
            if getattr(translation_lifecycle_context, "frame", None) is not None:
                translation_member(page.pk)
                parent = kwargs.get("parent")
                if parent is not None:
                    translation_member(parent.pk)
            locks = lock_alias_lifecycle_closure(page.pk)
            source_specific = locks.pages[page.pk].page.specific
            if hasattr(page, "_test_content_store"):
                source_specific._test_content_store = page._test_content_store
            policy = get_storage_policy_for_model(type(source_specific), allow_record_new=False)
            if policy and policy.external_fields:
                root, root_state, _, manifest = _canonical_alias_source(
                    page.pk, locks=locks, allow_unpublished=True
                )
                _validate_alias_targets(locks, root, root_state)
                if manifest is not None:
                    ExternalContentCoordinator(
                        store=getattr(page, "_test_content_store", None)
                    ).load_external_content_from_manifest(source_specific, manifest)
                    source_specific._external_content_loaded_hashes = {
                        field_name: entry.payload_sha256
                        for field_name, entry in manifest.fields.items()
                    }
            with preserve_threadlocal_context(_controlled_alias_context, "in_alias", True):
                with preserve_threadlocal_context(
                    _controlled_alias_context, "alias_update_source_page_id", page.pk
                ):
                    alias_page = original_create_alias(self, source_specific, **kwargs)
                    if policy and policy.external_fields:
                        setup_external_content_for_alias(
                            source_page=source_specific,
                            alias_page=alias_page,
                            user=kwargs.get("user"),
                        )
                        alias_page._external_content_alias_processed = True
                    return alias_page

    CreatePageAliasAction.execute = controlled_create_alias_execute
    CreatePageAliasAction._create_alias = controlled_create_alias
    CreatePageAliasAction._external_content_wrapped = True


def wrap_convert_alias_page_action() -> None:
    """
    包装 Wagtail 原生 ConvertAliasPageAction.execute，拦截别名页面转换为独立页面。
    在转换前预加载正文，转换后解绑 source_page_id，生成独立版本与发布世代。
    """
    try:
        from wagtail.actions.convert_alias import ConvertAliasPageAction
    except ImportError:
        return

    if getattr(ConvertAliasPageAction, "_external_content_wrapped", False):
        return

    original_execute = ConvertAliasPageAction.execute

    def controlled_convert_alias_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        # 策略识别仅取类，不访问 specific；实例可能陈旧，不能用它授予来源资格。
        page = self.page
        page_class = getattr(page, "specific_class", type(page))
        policy = get_storage_policy_for_model(page_class, allow_record_new=False)
        if not policy or not policy.external_fields:
            return original_execute(self, *args, **kwargs)

        with transaction.atomic():
            closure = lock_alias_lifecycle_closure(page.pk)
            locks = closure.pages[page.pk]
            if not locks.page.alias_of_id:
                raise ExternalContentOperationBlockedError("转换目标已不再是数据库 Alias")
            if closure.target_ids != [page.pk]:
                # 下游转换/重绑尚无完整协议，拒绝整次操作而不是部分推进。
                raise ExternalContentOperationBlockedError("Alias 转换存在下游 Alias，拒绝操作")
            _, _, _, manifest = _canonical_alias_source(
                page.pk, locks=closure, allow_unpublished=True
            )
            if manifest is None:
                raise PublicationRevisionInvalidError("Alias 转换缺少可证明的正式正文快照")
            spec = locks.page.specific
            coordinator = ExternalContentCoordinator(
                store=getattr(page, "_test_content_store", None)
            )
            coordinator.load_external_content_from_manifest(spec, manifest)
            spec._external_content_loaded_hashes = {
                name: entry.payload_sha256 for name, entry in manifest.fields.items()
            }
            if hasattr(page, "_test_content_store"):
                spec._test_content_store = page._test_content_store
            # 获锁实例只用于本次执行；失败污染实例应丢弃，不把 SQL 撤回当作内存恢复。
            # 提交结果未知也仅恢复 Action 的原绑定，不猜测或补偿持久结果。
            with preserve_threadlocal_context(self, "page", spec):
                # 行锁跨度覆盖 Mongo 验签、原生断链/save_revision 及最终 State 落地。
                converted_page = original_execute(self, *args, **kwargs)
                conv_specific = converted_page.specific
                # save_revision 已更新草稿 State；重读而非使用获锁时的陈旧草稿。
                # 新 Revision 由本事务在已围栏 Page 下创建，不在 Intent 层后补锁。
                state = ExternalContentPublicationState.objects.get(page_id=page.pk)
                state.source_page_id = None
                state.source_revision_id = None
                update_fields = ["source_page_id", "source_revision_id", "updated_at"]
                if conv_specific.live:
                    live_revision_id = conv_specific.live_revision_id
                    if not live_revision_id or live_revision_id != state.draft_revision_id:
                        raise PublicationRevisionInvalidError("Alias 转换正式指针未绑定新草稿 Revision")
                    live_revision = Revision.objects.get(pk=live_revision_id)
                    live_manifest = _manifest_from_revision(live_revision)
                    _validate_revision_manifest(
                        conv_specific, live_revision, live_manifest,
                        coordinator.store, validate_business=True,
                    )
                    state.published_manifest = live_manifest.to_dict()
                    state.published_revision_id = live_revision.pk
                    state.publication_generation += 1
                    update_fields.extend([
                        "published_manifest", "published_revision_id", "publication_generation",
                    ])
                else:
                    state.published_manifest = None
                    state.published_revision_id = None
                    update_fields.extend(["published_manifest", "published_revision_id"])
                state.save(update_fields=update_fields)
                return converted_page

    ConvertAliasPageAction.execute = controlled_convert_alias_execute
    ConvertAliasPageAction._external_content_wrapped = True
