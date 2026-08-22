"""独立内容索引的最小 Elasticsearch 写入适配层。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable, Mapping, Sequence

from wagtail.search.backends import get_search_backend

from search.services.content_index import (
    CONTENT_INDEX_REQUIRED_FIELDS,
    content_index_template_matches,
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
    properties = index_mapping.get("properties", {})
    if (
        index_mapping.get("dynamic") != "strict"
        or not CONTENT_INDEX_REQUIRED_FIELDS.issubset(properties)
    ):
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
    properties = index_mapping.get("properties", {})
    if (
        index_mapping.get("dynamic") != "strict"
        or not CONTENT_INDEX_REQUIRED_FIELDS.issubset(properties)
    ):
        raise ContentSearchElasticsearchError(
            "es_content_index_mapping_mismatch",
            retryable=False,
        )
    return True


def read_content_search_documents(
    target: Any,
    page_ids: Sequence[int],
) -> dict[int, Mapping[str, Any]]:
    """按页面 ID 批量读取最小字段，用于只读一致性检查。"""

    if not page_ids:
        return {}
    try:
        response = get_content_search_client(target).mget(
            index=target.index_name,
            ids=[str(page_id) for page_id in page_ids],
            source_includes=(
                "page_id",
                "content_version",
                "content_hash",
                "searchable",
                "operation",
            ),
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
) -> list[tuple[int, Mapping[str, Any]]]:
    """通过 page_id 的 search_after 游标读取最小字段，不使用深 offset。"""

    try:
        response = get_content_search_client(target).search(
            index=target.index_name,
            query={"match_all": {}},
            sort=[{"page_id": "asc"}],
            search_after=[after_page_id],
            size=limit,
            source_includes=(
                "page_id",
                "content_version",
                "content_hash",
                "searchable",
                "operation",
            ),
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
                        "version": document["content_version"],
                        "version_type": "external",
                    }
                },
                document,
            ]
        )
    return operations


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
) -> ContentSearchWriteResult:
    """以 ES 外部版本写入单页文档，迟到事件只能得到冲突而不能覆盖新版本。"""

    document = dict(document)
    document["content_version"] = content_version
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
