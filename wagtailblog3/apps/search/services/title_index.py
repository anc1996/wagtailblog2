"""独立标题建议索引的精简 mapping 和创建适配。"""

from __future__ import annotations

from dataclasses import dataclass

from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import (
    ContentSearchElasticsearchError,
    _response_body,
    get_content_search_client_for_connection,
)


@dataclass(frozen=True)
class TitleSuggestionIndexCreateResult:
    index_created: bool


def default_title_suggestion_index_name(index_prefix, version="v001"):
    if not isinstance(version, str) or not version.startswith("v") or not version[1:].isdigit():
        raise ValueError("标题建议索引版本无效")
    return validate_content_index_name(f"{index_prefix}-title-{version}", index_prefix)


def build_title_suggestion_index_template(index_name, shards=1, replicas=0):
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


def create_title_suggestion_index(connection_name, index_name):
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
    properties = mapping.get(index_name, {}).get("mappings", {}).get("properties", {})
    required = {"page_id", "title", "locale_id", "searchable", "popularity"}
    if mapping.get(index_name, {}).get("mappings", {}).get("dynamic") != "strict" or not required.issubset(properties):
        raise ContentSearchElasticsearchError("es_title_index_mapping_mismatch", retryable=False)
    return TitleSuggestionIndexCreateResult(index_created=True)
