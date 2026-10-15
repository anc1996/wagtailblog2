"""独立内容索引的最小 Elasticsearch 写入适配层。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from wagtail.search.backends import get_search_backend

from search.services.content_index import (
    CONTENT_INDEX_REQUIRED_FIELDS,
    CONTENT_INDEX_V2_REQUIRED_FIELDS,
    content_index_mapping_matches_schema,
    content_index_template_matches,
    content_index_v2_contract_matches,
)
from search.services.published_snapshot import (
    PublishedSnapshotError, effective_projection_schema_version,
)


@dataclass(frozen=True)
class ContentSearchWriteResult:
    """单文档外部版本写入结果；`superseded` 表示 ES 拒绝旧版本覆盖新版本。"""

    status: str


@dataclass(frozen=True)
class ContentSearchBulkWriteResult:
    """批量写入的成功与版本冲突计数，不代表业务 State 已被修改。"""

    succeeded: int
    superseded: int


@dataclass(frozen=True)
class ContentSearchIndexCreateResult:
    """索引模板和物理索引的创建结果，用于记录幂等初始化步骤。"""

    template_created: bool
    index_created: bool


class ContentSearchElasticsearchError(Exception):
    """ES 写入失败的脱敏分类，调用方据此决定重试或死信。"""

    def __init__(self, code: str, retryable: bool):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _response_body(response: Any) -> Any:
    """兼容 ES Python 客户端响应对象和测试替身。"""

    return getattr(response, "body", response)


def _exception_status(error: BaseException) -> int | None:
    meta = getattr(error, "meta", None)
    status = getattr(meta, "status", None)
    if status is None:
        status = getattr(error, "status_code", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _classify_status(status: int) -> ContentSearchWriteResult:
    if status == 409:
        return ContentSearchWriteResult(status="superseded")
    if 200 <= status < 300:
        return ContentSearchWriteResult(status="succeeded")
    if status == 429 or status >= 500:
        raise ContentSearchElasticsearchError(f"es_http_{status}", retryable=True)
    raise ContentSearchElasticsearchError(f"es_http_{status}", retryable=False)


def _read_error(error: BaseException) -> ContentSearchElasticsearchError:
    status = _exception_status(error)
    if status is None:
        return ContentSearchElasticsearchError("es_read_transport_error", retryable=True)
    return ContentSearchElasticsearchError(
        f"es_read_http_{status}",
        retryable=status == 429 or status >= 500,
    )


def get_content_search_client(target: Any) -> Any:
    """获取目标连接对应的 ES 客户端；目标索引名始终由调用方显式传入。"""

    return get_content_search_client_for_connection(target.connection_name)


def get_content_search_client_for_connection(connection_name: str) -> Any:
    """按配置连接名获取 ES 客户端，创建原型前不需要虚构 Target 记录。"""

    try:
        backend = get_search_backend(connection_name)
        client = getattr(backend, "es", None)
    except Exception as error:
        raise _read_error(error) from error
    if client is None:
        raise ContentSearchElasticsearchError("es_client_unavailable", retryable=False)
    return client


def create_content_search_index(
    connection_name: str,
    index_name: str,
    template_name: str,
    template_definition: Mapping[str, Any],
) -> ContentSearchIndexCreateResult:
    """创建精确物理索引并校验 mapping；绝不覆盖既有模板或索引。"""

    expected_mapping = template_definition.get("template", {}).get("mappings", {})
    schema = template_definition.get("_meta", {}).get("projection_schema_version", 1)
    try:
        valid = content_index_mapping_matches_schema(expected_mapping, schema)
    except PublishedSnapshotError:
        valid = False
    if not valid or (
        schema == 2 and (
            expected_mapping.get("_meta") != template_definition.get("_meta")
            or not content_index_v2_contract_matches(expected_mapping, index_name)
            or template_definition.get("index_patterns") != [index_name]
        )
    ):
        raise ContentSearchElasticsearchError("es_content_template_schema_mismatch", retryable=False)
    client = get_content_search_client_for_connection(connection_name)
    try:
        template_exists = bool(client.indices.exists_index_template(name=template_name))
        template_created = False
        if template_exists:
            existing_template = _response_body(
                client.indices.get_index_template(name=template_name)
            )
            if not content_index_template_matches(existing_template, template_definition):
                raise ContentSearchElasticsearchError(
                    "es_content_template_conflict",
                    retryable=False,
                )
        else:
            client.indices.put_index_template(
                name=template_name,
                index_patterns=template_definition["index_patterns"],
                template=template_definition["template"],
                meta=template_definition["_meta"],
                priority=200,
                create=True,
            )
            template_created = True

        if bool(client.indices.exists(index=index_name)):
            raise ContentSearchElasticsearchError(
                "es_content_index_already_exists",
                retryable=False,
            )
        client.indices.create(index=index_name)
        mapping = _response_body(client.indices.get_mapping(index=index_name))
    except ContentSearchElasticsearchError:
        raise
    except Exception as error:
        raise _read_error(error) from error

    index_mapping = mapping.get(index_name, {}).get("mappings", {}) if isinstance(mapping, dict) else {}
    if index_mapping != expected_mapping:
        raise ContentSearchElasticsearchError(
            "es_content_index_mapping_mismatch",
            retryable=False,
        )
    return ContentSearchIndexCreateResult(
        template_created=template_created,
        index_created=True,
    )


def verify_content_search_index(target: Any) -> bool:
    """启用双投递前只读核对精确物理索引的 mapping。"""

    try:
        mapping = _response_body(
            get_content_search_client(target).indices.get_mapping(index=target.index_name)
        )
    except ContentSearchElasticsearchError:
        raise
    except Exception as error:
        raise _read_error(error) from error
    index_mapping = mapping.get(target.index_name, {}).get("mappings", {}) if isinstance(mapping, dict) else {}
    try:
        valid = content_index_mapping_matches_schema(
            index_mapping, getattr(target, "projection_schema_version", 1),
        )
        if valid and effective_projection_schema_version(getattr(target, "projection_schema_version", 1)) == 2:
            valid = content_index_v2_contract_matches(index_mapping, target.index_name)
    except PublishedSnapshotError:
        valid = False
    if not valid:
        raise ContentSearchElasticsearchError(
            "es_content_index_mapping_mismatch",
            retryable=False,
        )
    return True


def _identity_source_fields(target: Any, include_identity: bool) -> tuple[str, ...]:
    """只读对账也按目标 schema，禁止把正文引用或原始快照带出 ES。"""
    try:
        schema = effective_projection_schema_version(getattr(target, "projection_schema_version", 1))
    except PublishedSnapshotError as error:
        raise ContentSearchElasticsearchError("es_target_schema_unsupported", retryable=False) from error
    common = ("page_id", "content_version", "content_hash", "searchable")
    if schema == 2:
        return common + ("is_deleted", "projection_schema_version") + (
            ("snapshot_digest", "publication_generation", "model_label", "search_type", "snapshot_kind")
            if include_identity else ()
        )
    return common + ("operation",) + (
        ("body_version_id", "publication_generation") if include_identity else ()
    )


def read_content_search_documents(
    target: Any,
    page_ids: Sequence[int],
    *,
    include_identity: bool = False,
) -> dict[int, Mapping[str, Any]]:
    """按页面 ID 批量读取最小字段，用于只读一致性检查。"""

    if not page_ids:
        return {}
    try:
        source_includes = _identity_source_fields(target, include_identity)
        response = get_content_search_client(target).mget(
            index=target.index_name,
            ids=[str(page_id) for page_id in page_ids],
            source_includes=source_includes,
        )
    except ContentSearchElasticsearchError:
        raise
    except Exception as error:
        raise _read_error(error) from error

    body = _response_body(response)
    if not isinstance(body, dict) or not isinstance(body.get("docs"), list):
        raise ContentSearchElasticsearchError("es_invalid_mget_response", retryable=True)
    documents = {}
    for item in body["docs"]:
        if not isinstance(item, dict) or not item.get("found"):
            continue
        source = item.get("_source") or {}
        try:
            page_id = int(source.get("page_id", item.get("_id")))
        except (TypeError, ValueError):
            continue
        documents[page_id] = source
    return documents


def scan_content_search_documents(
    target: Any,
    after_page_id: int,
    limit: int,
    *,
    include_identity: bool = False,
) -> list[tuple[int, Mapping[str, Any]]]:
    """通过 page_id 的 search_after 游标读取最小字段，不使用深 offset。"""

    try:
        source_includes = _identity_source_fields(target, include_identity)
        response = get_content_search_client(target).search(
            index=target.index_name,
            query={"match_all": {}},
            sort=[{"page_id": "asc"}],
            search_after=[after_page_id],
            size=limit,
            source_includes=source_includes,
            track_total_hits=False,
        )
    except ContentSearchElasticsearchError:
        raise
    except Exception as error:
        raise _read_error(error) from error

    body = _response_body(response)
    hits = body.get("hits", {}).get("hits") if isinstance(body, dict) else None
    if not isinstance(hits, list):
        raise ContentSearchElasticsearchError("es_invalid_search_response", retryable=True)
    documents = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        source = hit.get("_source") or {}
        try:
            page_id = int(source.get("page_id", hit.get("_id")))
        except (TypeError, ValueError):
            continue
        documents.append((page_id, source))
    return documents


def _bulk_operations(target: Any, documents: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    operations = []
    for document in documents:
        operations.extend(
            [
                {
                    "index": {
                        "_index": target.index_name,
                        "_id": str(document["page_id"]),
                        # external version 只使用 MySQL State 的单调 content_version；
                        # publication_generation 是公开身份字段，不能切换版本域。
                        "version": document["content_version"],
                        "version_type": "external",
                    }
                },
                document,
            ]
        )
    return operations


def validate_content_search_source(target: Any, document: Mapping[str, Any]) -> None:
    """请求构造前拒绝跨 schema、未知字段和非法版本，不依赖 ES 400 防泄露。"""
    try:
        schema = effective_projection_schema_version(getattr(target, "projection_schema_version", 1))
    except PublishedSnapshotError as error:
        raise ContentSearchElasticsearchError("es_target_schema_unsupported", retryable=False) from error
    allowed = CONTENT_INDEX_V2_REQUIRED_FIELDS if schema == 2 else CONTENT_INDEX_REQUIRED_FIELDS
    if not isinstance(document, Mapping) or set(document) - allowed:
        raise ContentSearchElasticsearchError("es_source_fields_invalid", retryable=False)
    if any(type(document.get(name)) is not int or document[name] <= 0 for name in ("page_id", "content_version")):
        raise ContentSearchElasticsearchError("es_source_version_invalid", retryable=False)
    if schema == 2:
        required = {
            "projection_schema_version", "model_label", "search_type", "snapshot_kind",
            "snapshot_digest", "publication_generation", "searchable", "is_deleted",
        }
        if not required <= document.keys() or type(document["projection_schema_version"]) is not int or document["projection_schema_version"] != 2:
            raise ContentSearchElasticsearchError("es_source_schema_mismatch", retryable=False)
        if (
            type(document["publication_generation"]) is not int or document["publication_generation"] <= 0
            or type(document["searchable"]) is not bool or type(document["is_deleted"]) is not bool
            or document["searchable"] == document["is_deleted"]
            or not isinstance(document["snapshot_digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", document["snapshot_digest"]) is None
            or any(not isinstance(document[name], str) or not document[name] for name in ("model_label", "search_type", "snapshot_kind"))
        ):
            raise ContentSearchElasticsearchError("es_source_identity_invalid", retryable=False)
        if document["searchable"] and not CONTENT_INDEX_V2_REQUIRED_FIELDS <= document.keys():
            raise ContentSearchElasticsearchError("es_source_projection_incomplete", retryable=False)
        if not document["searchable"]:
            # 墓碑不能携带正文、标题或范围；ES index 操作整体替换 _source 清除旧内容。
            if set(document) != required | {"page_id", "content_version"}:
                raise ContentSearchElasticsearchError("es_source_tombstone_fields_invalid", retryable=False)
        elif (
            any(not isinstance(document[name], str) for name in ("title", "summary", "body_text"))
            or type(document["locale_id"]) is not int or document["locale_id"] <= 0
            or any(
                not isinstance(document[name], list)
                or any(type(value) is not int or value <= 0 for value in document[name])
                for name in ("site_ids", "ancestor_ids")
            )
            or not isinstance(document["content_hash"], str)
            or re.fullmatch(r"[0-9a-f]{64}", document["content_hash"]) is None
            or any(
                not isinstance(document[name], str) or not document[name]
                for name in ("first_published_at", "sort_date")
            )
        ):
            raise ContentSearchElasticsearchError("es_source_projection_invalid", retryable=False)


def estimate_content_search_bulk_bytes(
    target: Any,
    documents: Iterable[Mapping[str, Any]],
) -> int:
    """估算 Bulk 请求体大小，按 UTF-8 字节而不是字符数限制批次。"""

    operations = _bulk_operations(target, documents)
    return sum(
        len(json.dumps(operation, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        + 1
        for operation in operations
    )


def _classify_bulk_response(
    body: Mapping[str, Any],
    document_count: int,
) -> ContentSearchBulkWriteResult:
    if not isinstance(body, dict):
        raise ContentSearchElasticsearchError("es_invalid_bulk_response", retryable=True)
    items = body.get("items") or []
    if len(items) != document_count:
        raise ContentSearchElasticsearchError("es_invalid_bulk_items", retryable=True)

    succeeded = 0
    superseded = 0
    for item in items:
        result = item.get("index") if isinstance(item, dict) else None
        if not isinstance(result, dict):
            raise ContentSearchElasticsearchError("es_invalid_bulk_item", retryable=True)
        try:
            status = int(result.get("status"))
        except (TypeError, ValueError):
            raise ContentSearchElasticsearchError("es_invalid_bulk_status", retryable=True)
        if status == 409:
            superseded += 1
        elif 200 <= status < 300:
            succeeded += 1
        elif status == 429 or status >= 500:
            raise ContentSearchElasticsearchError(
                f"es_bulk_item_http_{status}", retryable=True
            )
        else:
            raise ContentSearchElasticsearchError(
                f"es_bulk_item_http_{status}", retryable=False
            )
    return ContentSearchBulkWriteResult(succeeded=succeeded, superseded=superseded)


def write_content_search_documents(
    target: Any,
    documents: Iterable[Mapping[str, Any]],
) -> ContentSearchBulkWriteResult:
    """批量写入同一物理索引，并让每个文档使用自己的外部版本。"""

    documents = list(documents)
    if not documents:
        return ContentSearchBulkWriteResult(succeeded=0, superseded=0)

    for document in documents:
        validate_content_search_source(target, document)
    try:
        backend = get_search_backend(target.connection_name)
        client = backend.es
        response = client.bulk(
            operations=_bulk_operations(target, documents)
        )
    except ContentSearchElasticsearchError:
        raise
    except Exception as error:
        status = _exception_status(error)
        if status is not None:
            classified = _classify_status(status)
            if classified.status == "superseded":
                return ContentSearchBulkWriteResult(
                    succeeded=0,
                    superseded=len(documents),
                )
            return ContentSearchBulkWriteResult(
                succeeded=len(documents),
                superseded=0,
            )
        raise ContentSearchElasticsearchError("es_transport_error", retryable=True) from error

    return _classify_bulk_response(_response_body(response), len(documents))


def write_content_search_document(
    target: Any,
    document: Mapping[str, Any],
    content_version: int,
    *,
    publication_generation: int | None = None,
) -> ContentSearchWriteResult:
    """以 ES 外部版本写入单页文档，迟到事件只能得到冲突而不能覆盖新版本。"""

    document = dict(document)
    document["content_version"] = content_version
    if publication_generation is not None:
        document["publication_generation"] = publication_generation
    try:
        result = write_content_search_documents(target, [document])
    except ContentSearchElasticsearchError as error:
        if error.code.startswith("es_bulk_item_http_"):
            raise ContentSearchElasticsearchError(
                error.code.replace("es_bulk_item_http_", "es_http_", 1),
                retryable=error.retryable,
            ) from error
        raise
    if result.superseded:
        return ContentSearchWriteResult(status="superseded")
    return ContentSearchWriteResult(status="succeeded")
