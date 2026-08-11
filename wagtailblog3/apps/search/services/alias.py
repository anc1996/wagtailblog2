"""独立内容索引 read alias 的只读检查和原子切换。"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from search.services.content_index import validate_content_index_name
from search.services.elasticsearch import (
    ContentSearchElasticsearchError,
    _read_error,
    _response_body,
    get_content_search_client,
)


@dataclass(frozen=True)
class ContentSearchAliasSwitchResult:
    alias: str
    previous_indices: tuple[str, ...]
    new_index: str


def validate_content_search_alias(alias=None):
    alias = alias or getattr(settings, "CONTENT_SEARCH_READ_ALIAS", "")
    if not alias:
        raise ContentSearchElasticsearchError("content_read_alias_not_configured", retryable=False)
    try:
        validate_content_index_name(alias, settings.CONTENT_SEARCH_INDEX_PREFIX)
    except ValueError as error:
        raise ContentSearchElasticsearchError("content_read_alias_invalid", retryable=False) from error
    return alias


def get_content_search_read_alias_indices(target, alias=None):
    alias = validate_content_search_alias(alias)
    try:
        response = get_content_search_client(target).indices.get_alias(name=alias)
    except Exception as error:
        status = getattr(getattr(error, "meta", None), "status", None)
        if status == 404 or getattr(error, "status_code", None) == 404:
            return ()
        raise _read_error(error) from error
    body = _response_body(response)
    if not isinstance(body, dict):
        raise ContentSearchElasticsearchError("es_invalid_alias_response", retryable=True)
    indices = tuple(sorted(str(index_name) for index_name in body))
    for index_name in indices:
        try:
            validate_content_index_name(index_name, settings.CONTENT_SEARCH_INDEX_PREFIX)
        except ValueError as error:
            raise ContentSearchElasticsearchError("content_alias_points_outside_prefix", retryable=False) from error
    return indices


def switch_content_search_read_alias(target, new_index, alias=None, expected_indices=None):
    alias = validate_content_search_alias(alias)
    try:
        validate_content_index_name(new_index, settings.CONTENT_SEARCH_INDEX_PREFIX)
    except ValueError as error:
        raise ContentSearchElasticsearchError("content_target_index_invalid", retryable=False) from error
    current_indices = get_content_search_read_alias_indices(target, alias)
    if expected_indices is not None and tuple(sorted(expected_indices)) != current_indices:
        raise ContentSearchElasticsearchError("content_read_alias_changed", retryable=False)
    actions = [
        {"remove": {"index": index_name, "alias": alias}}
        for index_name in current_indices
    ]
    actions.append({"add": {"index": new_index, "alias": alias}})
    try:
        get_content_search_client(target).indices.update_aliases(actions=actions)
    except ContentSearchElasticsearchError:
        raise
    except Exception as error:
        raise _read_error(error) from error
    return ContentSearchAliasSwitchResult(
        alias=alias,
        previous_indices=current_indices,
        new_index=new_index,
    )


def clear_content_search_read_alias(target, alias=None, expected_indices=None):
	"""原子移除内容 alias，使前台可回退到旧搜索实现。"""
	alias = validate_content_search_alias(alias)
	current_indices = get_content_search_read_alias_indices(target, alias)
	if expected_indices is not None and tuple(sorted(expected_indices)) != current_indices:
		raise ContentSearchElasticsearchError("content_read_alias_changed", retryable=False)
	if not current_indices:
		return current_indices
	actions = [
		{"remove": {"index": index_name, "alias": alias}}
		for index_name in current_indices
	]
	try:
		get_content_search_client(target).indices.update_aliases(actions=actions)
	except ContentSearchElasticsearchError:
		raise
	except Exception as error:
		raise _read_error(error) from error
	return current_indices
