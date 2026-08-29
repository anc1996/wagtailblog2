"""独立精简内容索引的版本化 mapping 和安全创建入口。"""

from __future__ import annotations

import re
from typing import Any, Mapping


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
) -> dict[str, Any]:
    """构造单索引模板；字段白名单防止草稿或原始正文意外进入 ES。"""

    version = validate_content_index_version(version)
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

    return {
        "index_patterns": [index_name],
        "template": {
            "settings": index_settings,
            "mappings": {
                "dynamic": "strict",
                "properties": properties,
            },
        },
        "_meta": {
            "application": "wagtailblog-content-search",
            "mapping_version": content_index_mapping_version(version, analyzer_profile),
            "analyzer_profile": analyzer_profile,
        },
    }


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
