"""独立标题建议索引的精简 mapping 和创建适配。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import (
    ContentSearchElasticsearchError,
    _response_body,
    get_content_search_client_for_connection,
)


@dataclass(frozen=True)
class TitleSuggestionIndexCreateResult:
    """标题建议索引创建结果；只报告是否新建，不包含 ES 响应正文。"""
    index_created: bool


def build_published_title_document(state: Any, *, locale_id: int) -> dict[str, Any]:
    """标题索引不读当前草稿，也不访问正文存储；沿用标题 v001 白名单。"""
    from search.services.published_snapshot import PublishedSnapshotV1

    snapshot = PublishedSnapshotV1.from_dict(
        state.published_snapshot, expected_digest=state.snapshot_digest,
    )
    snapshot.validate_state(state)
    return {
        "page_id": state.page_id,
        "title": snapshot.to_dict()["published_metadata"]["title"] if state.searchable else "",
        "locale_id": locale_id, "searchable": bool(state.searchable), "popularity": 0,
    }


def default_title_suggestion_index_name(index_prefix: str, version: str = "v001") -> str:
    """生成并校验带版本的标题建议索引名。"""
    if not isinstance(version, str) or not version.startswith("v") or not version[1:].isdigit():
        raise ValueError("标题建议索引版本无效")
    return validate_content_index_name(f"{index_prefix}-title-{version}", index_prefix)


def build_title_suggestion_index_template(
    index_name: str, shards: int = 1, replicas: int = 0
) -> dict[str, Any]:
    """构造 strict mapping；模板只允许预定义字段，避免动态字段泄漏。"""
    validate_content_index_name(index_name, index_name.rsplit("-title-", 1)[0])
    return {
        "index_patterns": [index_name],
        "template": {
            "settings": {
                "number_of_shards": int(shards),
                "number_of_replicas": int(replicas),
                "refresh_interval": "30s",
                "codec": "best_compression",
                "analysis": {
                    "analyzer": {
                        "title_suggestion_ik": {
                            "type": "custom",
                            "tokenizer": "ik_smart",
                        }
                    }
                },
            },
            "mappings": {
                "dynamic": "strict",
                "properties": {
                    "page_id": {"type": "long"},
                    "title": {
                        "type": "text",
                        "analyzer": "title_suggestion_ik",
                        "search_analyzer": "title_suggestion_ik",
                    },
                    "locale_id": {"type": "long"},
                    "searchable": {"type": "boolean"},
                    "popularity": {"type": "integer"},
                },
            },
        },
        "_meta": {"application": "wagtailblog-title-suggestions", "mapping_version": "title-v001"},
    }


def create_title_suggestion_index(
    connection_name: str, index_name: str
) -> TitleSuggestionIndexCreateResult:
    """创建标题建议索引并立即校验 mapping；已存在或不匹配时失败。

    该函数具有外部 ES 写入副作用，生产调用必须由管理命令的确认门禁控制。
    """
    client = get_content_search_client_for_connection(connection_name)
    template_name = f"{index_name}-template"
    definition = build_title_suggestion_index_template(index_name)
    try:
        if not client.indices.exists(index=index_name):
            client.indices.create(index=index_name, **definition["template"])
        else:
            raise ContentSearchElasticsearchError("es_title_index_already_exists", retryable=False)
        mapping = _response_body(client.indices.get_mapping(index=index_name))
    except ContentSearchElasticsearchError:
        raise
    except Exception as error:
        raise ContentSearchElasticsearchError("es_title_index_create_failed", retryable=False) from error
    if mapping.get(index_name, {}).get("mappings", {}) != definition["template"]["mappings"]:
        raise ContentSearchElasticsearchError("es_title_index_mapping_mismatch", retryable=False)
    return TitleSuggestionIndexCreateResult(index_created=True)
