"""
Wagtail Page 正文外置通用协议 - 多字段协调器 (Coordinator) 接口。

负责：
1. 收集页面声明的全部外置字段，规范化编码并持久化至 MongoDB 不可变版本仓储；
2. 构建 Revision 绑定的 ExternalContentManifest 指针清单；
3. 依据 Manifest 从不可变仓储安全拉取正文并解码还原至页面实例；
4. 严格断言：
   - 字段集合必须完全闭环（不完整指针与未知冗余字段均失败关闭）；
   - 严格核验 content_storage_schema、字段 codec 与 codec schema_version；
   - 支持根据 manifest.content_storage_schema 安全回溯历史策略快照；
   - 普通序列化不产生 Mongo 写副作用。
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from wagtail.models import Page

from external_content.exceptions import (
    FieldStorageConfigError,
    ManifestIdentityMismatchError,
    ManifestValidationError,
)
from external_content.manifest import ExternalContentManifest, FieldManifestEntry
from external_content.registry import (
    ContentStoragePolicy,
    get_policy_for_version,
    get_storage_policy_for_model,
)
from external_content.store import ContentVersionStore


class ExternalContentCoordinator:
    """
    外置正文多字段生命周期协调器。
    """

    def __init__(self, store: Optional[ContentVersionStore] = None) -> None:
        self.store = store or ContentVersionStore()
        self.last_created_version_ids: List[str] = []

    def prepare_external_content_for_revision(
        self,
        page: Page,
        policy: Optional[ContentStoragePolicy] = None,
    ) -> ExternalContentManifest:
        """
        显式为当前页面准备不可变 Revision 正文版本。

        先完成所有字段的编码与来源核验，再存入 MongoDB，构建整页 Manifest。
        只有此显式入口会产生 MongoDB 写入副作用，普通 serializable_data 绝不写入。

        :param page: 已持久化具备 pk 的 Wagtail Page 实例
        :param policy: 预先解析的模型存储策略（若无则自动读取）
        :return: 包含全部外置字段指针的 ExternalContentManifest
        :raises FieldStorageConfigError: 模型未配置有效策略或 page 尚未保存
        """
        # RB-01：身份/策略拒绝也属于新的零写入尝试，不能沿用上次的孤儿诊断。
        created_version_ids: List[str] = []
        self.last_created_version_ids = created_version_ids
        if not page.pk:
            raise FieldStorageConfigError("页面尚未持久化生成主键 ID，无法准备不可变正文版本")

        pol = policy or get_storage_policy_for_model(type(page), allow_record_new=False)
        if pol is None or not pol.external_fields:
            raise FieldStorageConfigError(f"模型 {page.__class__.__name__} 未配置任何外置字段策略")

        fields_manifest: Dict[str, FieldManifestEntry] = {}
        staged_payloads: Dict[str, tuple[Any, str]] = {}

        try:
            # RB-01：全部前置校验通过前不写版本；后字段非法不能产生前字段孤儿。
            for field_name, field_cfg in pol.external_fields.items():
                state_getter = getattr(page, "get_external_field_state", None)
                field_state = state_getter(field_name) if callable(state_getter) else None
                if field_state is not None and field_state.status == "UNLOADED":
                    # 校验完整来源但不物化字段；空 SQL 占位绝不是可复用的正文。
                    page._load_external_field_source(field_name, materialize=False)
                    field_state = state_getter(field_name)
                    source = field_state.manifest
                    entry = source.fields[field_name]
                    if (
                        source.model_label == pol.model_label and source.page_id == page.pk
                        and source.content_storage_schema == pol.schema_version
                        and entry.codec == field_cfg.codec.codec_id
                        and entry.schema_version == field_cfg.codec.schema_version
                    ):
                        # 来源加载已经完整校验过，仍在复用点核对该不可变引用。
                        self.store.get_version(entry.version_id, expected_identity={
                            "model_label": pol.model_label, "page_id": page.pk,
                            "field_name": field_name, "codec": entry.codec,
                            "schema_version": entry.schema_version, "payload_sha256": entry.payload_sha256,
                        })
                        fields_manifest[field_name] = entry
                        continue
                value = getattr(page, field_name, None)
                target_field = page._meta.get_field(field_name)
                # 调用 Codec 进行规范化并计算确定性 SHA-256（传入目标字段实例进行写入前严格校验）
                normalized_payload, payload_sha256 = field_cfg.codec.encode(value, field=target_field)

                field_state = state_getter(field_name) if callable(state_getter) else None
                source_manifest = field_state.manifest if field_state is not None else None
                entry = source_manifest.fields.get(field_name) if source_manifest is not None else None
                if (
                    entry is not None
                    and source_manifest.model_label == pol.model_label
                    and source_manifest.page_id == page.pk
                    and source_manifest.content_storage_schema == pol.schema_version
                    and entry.codec == field_cfg.codec.codec_id
                    and entry.schema_version == field_cfg.codec.schema_version
                    and entry.payload_sha256 == payload_sha256
                ):
                    # 未改变字段复用引用前仍校验仓储存在性/哈希/身份；不靠内存摘要放行。
                    self.store.get_version(entry.version_id, expected_identity={
                        "model_label": pol.model_label, "page_id": page.pk,
                        "field_name": field_name, "codec": entry.codec,
                        "schema_version": entry.schema_version, "payload_sha256": payload_sha256,
                    })
                    fields_manifest[field_name] = entry
                    continue
                if field_state is not None and field_state.status == "LOADED":
                    # StreamValue 原地修改绕过 __setattr__，必须在重新编码处捕获。
                    from dataclasses import replace
                    page._external_content_field_states[field_name] = replace(
                        field_state, status="CLEARED" if not normalized_payload else "DIRTY",
                        source="NEW" if field_state.source == "NEW" else "EDIT",
                    )

                staged_payloads[field_name] = (normalized_payload, payload_sha256)

            # RB-02：存储阶段仍可能部分成功；只记录确认 ID，未知 ACK 不重试、不删除。
            for field_name, (normalized_payload, payload_sha256) in staged_payloads.items():
                field_cfg = pol.external_fields[field_name]
                version_id, is_new = self.store.save_version_with_status(
                    model_label=pol.model_label,
                    page_id=page.pk,
                    field_name=field_name,
                    codec=field_cfg.codec.codec_id,
                    schema_version=field_cfg.codec.schema_version,
                    payload_sha256=payload_sha256,
                    payload=normalized_payload,
                )
                if is_new:
                    created_version_ids.append(version_id)

                fields_manifest[field_name] = FieldManifestEntry(
                    codec=field_cfg.codec.codec_id,
                    schema_version=field_cfg.codec.schema_version,
                    version_id=version_id,
                    payload_sha256=payload_sha256,
                )
        except Exception:
            # G4/RB-02：保留不可变孤儿供只读对账；GC 当前硬禁用，严禁删除补偿。
            if created_version_ids:
                safe_page_id = page.pk if type(page.pk) is int else None
                logger.warning(
                    "EXTERNAL_CONTENT_PREPARE_FAILED: page_id=%s, created_version_count=%s",
                    safe_page_id,
                    len(created_version_ids),
                )
            raise

        return ExternalContentManifest(
            protocol_version=1,
            content_storage_schema=pol.schema_version,
            model_label=pol.model_label,
            page_id=page.pk,
            fields={name: fields_manifest[name] for name in pol.external_fields},
        )

    def load_external_content_from_manifest(
        self,
        page: Page,
        manifest: ExternalContentManifest,
        policy: Optional[ContentStoragePolicy] = None,
        *,
        source: str = "REVISION",
        revision_id: Optional[int] = None,
        preserve_edits: bool = False,
        fields: Optional[set[str]] = None,
    ) -> None:
        """
        依据 Manifest 将外置正文解码还原至页面实例。

        严格防跨页与版本身份校验：
        1. 验证同页身份；跨页 Alias 必须由数据库来源链和正式快照证明，保留受控 Copy 契约；
        2. 核验 content_storage_schema，按版本解析策略（支持历史版本安全还原）；
        3. 严格核验字段集合完全闭环：若缺少策略声明的外置字段或有多余字段，一律失败关闭；
        4. 严格核验字段 codec 标识与 schema_version 匹配；
        5. 从 MongoDB 读取不可变文档并执行读时哈希重算；
        6. 验证版本指针中的 field_name、codec、schema_version 与 payload_sha256；
        7. 通过 Codec 解码还原为页面运行时对象并挂载回实例。

        :param page: 目标 Wagtail Page 实例
        :param manifest: 待读取的 ExternalContentManifest
        :param policy: 预先解析的模型存储策略
        :raises ManifestIdentityMismatchError: 页面身份与 Manifest 不符
        :raises ManifestValidationError: 字段集合不闭环、版本不匹配或字段属性非法
        """
        # 1. 同页历史快照仍合法；跨页 Alias 必须由数据库链和正式 State 证明。
        expected_label = getattr(page._meta, "label_lower", "")
        if manifest.model_label != expected_label:
            raise ManifestIdentityMismatchError(
                f"Manifest model_label ('{manifest.model_label}') 与实际页面模型 ('{expected_label}') 不一致"
            )
        if manifest.page_id == page.pk:
            manifest.validate_against_page(page)
        else:
            # 普通跨页是身份错误，不能误入要求正式 Revision 的 Alias 来源解析。
            # 实例上的 alias_of_id 只选择校验路径，不赋予跨页读取资格。
            if not getattr(page, "alias_of_id", None):
                manifest.validate_against_page(page)
            from external_content.copying import validate_alias_manifest_source

            validate_alias_manifest_source(page, manifest)

        # 2. 解析策略并核验 content_storage_schema
        pol = policy or get_policy_for_version(type(page), manifest.content_storage_schema)
        if pol.schema_version != manifest.content_storage_schema:
            raise ManifestValidationError(
                f"Manifest content_storage_schema ({manifest.content_storage_schema}) 与策略版本 ({pol.schema_version}) 不一致"
            )

        # 3. 严格核验字段集合完全闭环（不得遗漏任何外置字段，亦不得有多余未知字段）
        expected_fields = set(pol.external_fields.keys())
        manifest_fields = set(manifest.fields.keys())
        if manifest_fields != expected_fields:
            missing = expected_fields - manifest_fields
            extra = manifest_fields - expected_fields
            if missing:
                raise ManifestValidationError(
                    f"Manifest 缺少策略登记的外置字段指针: {sorted(missing)}，不完整指针拒绝加载"
                )
            if extra:
                raise ManifestValidationError(
                    f"Manifest 包含策略未登记的多余字段指针: {sorted(extra)}"
                )
        if fields is not None and not fields.issubset(expected_fields):
            raise ManifestValidationError("定向加载包含未登记的外置字段")

        # 4. 核验字段 codec 与 schema_version，并拉取不可变文档
        # 4. 阶段 1：校验并解码所有字段至暂存字典，确保原子性（失败关闭，绝不部分修改实例）
        staged_values: Dict[str, Any] = {}
        for field_name, entry in manifest.fields.items():
            field_cfg = pol.external_fields[field_name]
            if entry.codec != field_cfg.codec.codec_id:
                raise ManifestValidationError(
                    f"字段 '{field_name}' Manifest 中的 codec ('{entry.codec}') 与策略声明 ('{field_cfg.codec.codec_id}') 不一致"
                )
            if entry.schema_version != field_cfg.codec.schema_version:
                raise ManifestValidationError(
                    f"字段 '{field_name}' Manifest 中的 schema_version ({entry.schema_version}) 与策略声明 ({field_cfg.codec.schema_version}) 不一致"
                )

            # 5. 从仓储读取并校验不可变文档
            doc = self.store.get_version(
                entry.version_id,
                expected_identity={
                    "model_label": manifest.model_label,
                    "page_id": manifest.page_id,
                    "field_name": field_name,
                    "codec": entry.codec,
                    "schema_version": entry.schema_version,
                    "payload_sha256": entry.payload_sha256,
                },
            )

            # 6. 解码至暂存字典，确保全部成功后才写回实例内存（原子性失败关闭）
            target_field = page._meta.get_field(field_name)
            decoded_value = field_cfg.codec.decode(doc["payload"], field=target_field)
            staged_values[field_name] = decoded_value

        # 7. 全部字段解码与验证成功后，一次性赋值到页面实例
        binder = getattr(page, "_bind_external_field_source", None)
        if callable(binder):
            for field_name in manifest.fields:
                binder(field_name, manifest, source=source, revision_id=revision_id)
        for field_name, decoded_value in staged_values.items():
            if fields is not None and field_name not in fields:
                continue
            state_getter = getattr(page, "get_external_field_state", None)
            if (
                preserve_edits and callable(state_getter)
                and state_getter(field_name).status != "UNLOADED"
            ):
                # 也保留已物化字段，避免另一个字段懒加载覆盖尚未识别的原地修改。
                continue
            setter = getattr(page, "_set_external_field_loaded", None)
            if callable(setter):
                setter(field_name, decoded_value, manifest, source=source, revision_id=revision_id)
            else:
                setattr(page, field_name, decoded_value)

    def validate_manifest_payloads(
        self,
        manifest: ExternalContentManifest,
    ) -> None:
        """
        逐字段核对 Manifest 中所有字段版本在 MongoDB 中的存在性、哈希与 6 项身份元数据。

        用于发布候选锁定 (lock_and_validate_revision) 与门禁检查。
        若任一版本缺失、哈希不匹配或网络异常，抛出异常。
        """
        for field_name, entry in manifest.fields.items():
            self.store.get_version(
                entry.version_id,
                expected_identity={
                    "model_label": manifest.model_label,
                    "page_id": manifest.page_id,
                    "field_name": field_name,
                    "codec": entry.codec,
                    "schema_version": entry.schema_version,
                    "payload_sha256": entry.payload_sha256,
                },
            )
