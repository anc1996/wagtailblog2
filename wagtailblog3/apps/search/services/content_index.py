"""独立精简内容索引的版本化 mapping 和安全创建入口。"""

from __future__ import annotations

import re
from typing import Any, Mapping

from search.services.published_snapshot import PublishedSnapshotError, effective_projection_schema_version

# mapping 采用不可变物理索引；新增字段必须创建新版本索引后再切换 alias。
CONTENT_INDEX_MAPPING_VERSION = "v005"
CONTENT_INDEX_ANALYZER_PROFILES = {
    "legacy_standard": {
        "title": None,
        "intro": None,
        "body_text": None,
    },
    "balanced": {
        "title": "content_ik_max_word",
        "intro": "content_ik_max_word",
        "body_text": "content_ik_smart",
    },
    "ik_max_word": {
        "title": "content_ik_max_word",
        "intro": "content_ik_max_word",
        "body_text": "content_ik_max_word",
    },
    "ik_smart": {
        "title": "content_ik_smart",
        "intro": "content_ik_smart",
        "body_text": "content_ik_smart",
    },
}
CONTENT_INDEX_REQUIRED_FIELDS = frozenset(
    {
        "page_id",
        "content_version",
        "content_hash",
        "body_version_id",
        "publication_generation",
        "operation",
        "title",
        "intro",
        "body_text",
        "date",
        "first_published_at",
        "locale_id",
        "tag_ids",
        "category_ids",
        "searchable",
    }
)
CONTENT_INDEX_V2_FIELD_TYPES = {
    "page_id": "long", "locale_id": "long", "site_ids": "long", "ancestor_ids": "long",
    "model_label": "keyword", "search_type": "keyword", "snapshot_kind": "keyword",
    "title": "text", "summary": "text", "body_text": "text",
    "first_published_at": "date", "sort_date": "date", "content_version": "long",
    "publication_generation": "long", "projection_schema_version": "integer",
    "snapshot_digest": "keyword", "content_hash": "keyword",
    "searchable": "boolean", "is_deleted": "boolean",
}
CONTENT_INDEX_V2_REQUIRED_FIELDS = frozenset(CONTENT_INDEX_V2_FIELD_TYPES)
_INDEX_VERSION_PATTERN = re.compile(r"v[0-9]{3,16}\Z")
_INDEX_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")


def validate_content_index_version(version: Any) -> str:
    """限制版本格式，避免命令把别名或通配符当作物理索引。"""

    if not isinstance(version, str) or not _INDEX_VERSION_PATTERN.fullmatch(version):
        raise ValueError("内容索引版本必须为 v001 形式")
    return version


def validate_content_index_name(index_name: Any, index_prefix: str) -> str:
    """物理索引必须位于配置前缀下，禁止别名、通配符和跨环境名称。"""

    if not isinstance(index_name, str) or not _INDEX_NAME_PATTERN.fullmatch(index_name):
        raise ValueError("内容索引名包含不允许的字符")
    required_prefix = f"{index_prefix}-"
    if not index_name.startswith(required_prefix):
        raise ValueError("内容索引名必须使用当前环境的内容索引前缀")
    return index_name


def default_content_index_name(
    index_prefix: str,
    version: Any = CONTENT_INDEX_MAPPING_VERSION,
) -> str:
    """生成稳定的版本化物理索引名，读别名不参与任何写入。"""

    return f"{index_prefix}-{validate_content_index_version(version)}"


def content_index_template_name(index_name: str) -> str:
    """模板与单一物理索引一一对应，避免不同 analyzer 原型互相覆盖。"""

    return f"{index_name}-template"


def content_index_mapping_version(version: Any, analyzer_profile: str) -> str:
    """将 mapping 版本和 analyzer 实验配置一起写入构建审计记录。"""

    validate_content_index_version(version)
    if analyzer_profile not in CONTENT_INDEX_ANALYZER_PROFILES:
        raise ValueError("未知的内容索引 analyzer 配置")
    return f"content-{version}-{analyzer_profile}"


def build_content_index_template(
    index_name: str,
    analyzer_profile: str = "balanced",
    version: Any = CONTENT_INDEX_MAPPING_VERSION,
    shards: Any = 1,
    replicas: Any = 0,
    refresh_interval: str = "30s",
    projection_schema_version: int = 1,
) -> dict[str, Any]:
    """构造单索引模板；字段白名单防止草稿或原始正文意外进入 ES。"""

    version = validate_content_index_version(version)
    schema = effective_projection_schema_version(projection_schema_version)
    profile = CONTENT_INDEX_ANALYZER_PROFILES.get(analyzer_profile)
    if profile is None:
        raise ValueError("未知的内容索引 analyzer 配置")
    try:
        shards = int(shards)
        replicas = int(replicas)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("分片和副本数必须是整数") from error
    if shards < 1 or replicas < 0:
        raise ValueError("分片数至少为 1，副本数不能为负数")
    if not isinstance(refresh_interval, str) or not refresh_interval:
        raise ValueError("刷新间隔不能为空")

    text_fields = {}
    for field_name, analyzer_name in profile.items():
        field_definition = {"type": "text"}
        if analyzer_name:
            field_definition["analyzer"] = analyzer_name
            field_definition["search_analyzer"] = "content_ik_smart"
        text_fields[field_name] = field_definition
    properties = {
        "page_id": {"type": "long"},
        "content_version": {"type": "long"},
        # hash 只供一致性读取，不参与查询、聚合或额外的 doc_values 存储。
        "content_hash": {"type": "keyword", "index": False, "doc_values": False},
        "body_version_id": {"type": "keyword", "index": False, "doc_values": False},
        "publication_generation": {"type": "long"},
        "operation": {"type": "keyword"},
        **text_fields,
        "date": {"type": "date"},
        "first_published_at": {"type": "date"},
        "locale_id": {"type": "long"},
        "tag_ids": {"type": "long"},
        "category_ids": {"type": "long"},
        "searchable": {"type": "boolean"},
    }
    if schema == 2:
        properties = {
            name: {"type": field_type}
            for name, field_type in CONTENT_INDEX_V2_FIELD_TYPES.items()
        }
        # 简介沿用同一 analyzer 策略，但不把 v1 的 intro 混入 v2。
        for name, profile_name in (("title", "title"), ("summary", "intro"), ("body_text", "body_text")):
            if profile[profile_name]:
                properties[name].update(
                    analyzer=profile[profile_name], search_analyzer="content_ik_smart",
                )
        for name in ("content_hash", "snapshot_digest"):
            properties[name].update(index=False, doc_values=False)
    index_settings = {
        "number_of_shards": shards,
        "number_of_replicas": replicas,
        "refresh_interval": refresh_interval,
        "codec": "best_compression",
    }
    if any(profile.values()):
        # 标准分词集群不声明 IK tokenizer，确保独立集群无需安装无用插件。
        index_settings["analysis"] = {
            "analyzer": {
                "content_ik_max_word": {
                    "type": "custom",
                    "tokenizer": "ik_max_word",
                },
                "content_ik_smart": {
                    "type": "custom",
                    "tokenizer": "ik_smart",
                },
            }
        }

    metadata = {
        "application": "wagtailblog-content-search",
        "mapping_version": content_index_mapping_version(version, analyzer_profile),
        "analyzer_profile": analyzer_profile,
    }
    mappings = {"dynamic": "strict", "properties": properties}
    if schema == 2:
        metadata["projection_schema_version"] = schema
        # 模板顶层 _meta 不会进入物理索引；必须在 mappings 内另存协议版本。
        mappings["_meta"] = dict(metadata)
    return {
        "index_patterns": [index_name],
        "template": {
            "settings": index_settings,
            "mappings": mappings,
        },
        "_meta": metadata,
    }


def content_index_mapping_matches_schema(mapping: Mapping[str, Any], schema: Any) -> bool:
    """字段集合和类型必须匹配协议；旧 v1 无 _meta 仍保留显式兼容。"""
    schema = effective_projection_schema_version(schema)
    expected = (
        CONTENT_INDEX_V2_FIELD_TYPES if schema == 2 else
        {
            name: definition["type"]
            for name, definition in build_content_index_template(
                "schema-check-v005", analyzer_profile="legacy_standard",
            )["template"]["mappings"]["properties"].items()
        }
    )
    properties = mapping.get("properties", {})
    if mapping.get("dynamic") != "strict" or set(properties) != set(expected):
        return False
    if any(properties[name].get("type") != field_type for name, field_type in expected.items()):
        return False
    metadata = mapping.get("_meta", {})
    if schema == 2:
        return type(metadata.get("projection_schema_version")) is int and metadata["projection_schema_version"] == 2
    return type(metadata.get("projection_schema_version", 1)) is int and metadata.get("projection_schema_version", 1) == 1


def content_index_v2_contract_matches(
    mapping: Mapping[str, Any], index_name: str,
) -> bool:
    """schema、物理版本、analyzer 与完整字段定义必须属于同一不可变契约。"""
    if not content_index_mapping_matches_schema(mapping, 2):
        return False
    metadata = mapping.get("_meta", {})
    version = index_name.rsplit("-", 1)[-1]
    profile = metadata.get("analyzer_profile")
    try:
        expected = build_content_index_template(
            index_name, analyzer_profile=profile, version=version,
            projection_schema_version=2,
        )
    except (ValueError, TypeError, PublishedSnapshotError):
        return False
    return mapping == expected["template"]["mappings"]


def content_index_template_matches(
    existing_template: Mapping[str, Any],
    expected_template: Mapping[str, Any],
) -> bool:
    """仅复用完整契约一致的模板，避免 strict mapping 漂移导致 Bulk 单项 400。

    参数：ES ``get_index_template`` 响应和本地待创建模板定义。
    返回：仅当 index pattern、应用元数据及 settings/mappings 全部一致时返回 ``True``。
    副作用：无；缺少任一契约字段即视为不匹配，由调用方停止覆盖并人工处理。
    """
    templates = existing_template.get("index_templates", [])
    if len(templates) != 1:
        return False
    template = templates[0].get("index_template", {})
    return (
        template.get("index_patterns") == expected_template.get("index_patterns")
        and template.get("_meta") == expected_template.get("_meta")
        and template.get("template") == expected_template.get("template")
    )
