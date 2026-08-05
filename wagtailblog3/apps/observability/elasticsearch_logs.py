"""可观测性日志中心的 Elasticsearch 可选读模型与索引同步边界。

职责：生成 Filebeat ingest pipeline 和配置，维护独立日志索引的读写别名，执行受限
搜索、汇总、批量索引及清理后的 ``delete_by_query``。数据流为：注册日志文件由
Filebeat 写入 write alias，经 pipeline 规范化后由后台通过 read alias 查询；文件
清理时由 outbox 传入受 inode、偏移与时间截点约束的删除计划。
关键依赖：Django settings、Elasticsearch Python 客户端、日志注册表、签名游标和文本
脱敏器。文件读取器仍是降级后的事实来源；本模块不读取或修改 MongoDB、页面正文或
业务表。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from functools import lru_cache
from pathlib import Path
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core import signing

from .parser import LogRecord
from .registry import LOG_FILE_BY_KEY, LOG_FILE_SPECS
from .sanitizer import project_relative_path, sanitize_log_text


CURSOR_SALT = "observability.elasticsearch-log-pages.v1"
CURSOR_MAX_AGE = 30 * 60
MAX_PAGE_SIZE = 200
DEFAULT_FAILURE_COOLDOWN = 5
DEFAULT_TIMEZONE = "Asia/Shanghai"

# A failed ES request must not add the network timeout to every subsequent
# admin request while the optional read model is down. Each worker keeps a
# small local circuit-breaker window; successful requests close it again.
_circuit_open_until = 0.0
logger = logging.getLogger(__name__)


LOG_MAPPINGS = {
    "dynamic": False,
    "properties": {
        "@timestamp": {"type": "date"},
        "observed_at": {"type": "date"},
        "level": {"type": "keyword"},
        "kind": {"type": "keyword"},
        "domain": {"type": "keyword"},
        "logger": {"type": "keyword"},
        "source_key": {"type": "keyword"},
        "source_identity": {"type": "keyword"},
        "document_id": {"type": "keyword"},
        "source_label": {"type": "keyword"},
        "source_path": {"type": "keyword"},
        "source_device": {"type": "keyword"},
        "source_inode": {"type": "keyword"},
        "relative_path": {"type": "keyword"},
        "function": {"type": "keyword"},
        "line": {"type": "integer"},
        "message": {"type": "text"},
        "raw": {"type": "text", "index": False},
        "traceback": {"type": "text", "index": False},
        "request_id": {"type": "keyword"},
        "trace_id": {"type": "keyword"},
        "user_id": {"type": "long"},
        "http_status": {"type": "integer"},
        "duration_ms": {"type": "float"},
        "exception_type": {"type": "keyword"},
        "pid": {"type": "integer"},
        "thread": {"type": "keyword"},
        "rotation": {"type": "integer"},
        "start_offset": {"type": "long"},
        "end_offset": {"type": "long"},
    },
}


def _pipeline_name(config: dict[str, Any]) -> str:
    """确定 ingest pipeline 名称。

    参数：``config`` 为 ES 日志配置。
    返回：显式配置名，或从 write alias 推导的 v2 pipeline 名。
    异常：无；缺失配置使用稳定默认值以让 Filebeat 与索引准备保持一致。
    """
    configured = str(config.get("INGEST_PIPELINE") or "").strip()
    if configured:
        return configured
    write_alias = str(config.get("WRITE_INDEX") or "wagtailblog-logs-write")
    prefix = write_alias[:-6] if write_alias.endswith("-write") else write_alias
    return f"{prefix}-normalize-v2"


def build_ingest_pipeline(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the versioned pipeline used by the generated Filebeat config."""
    config = config or _config()
    timezone = str(config.get("TIMEZONE") or DEFAULT_TIMEZONE)
    return {
        "description": "Normalize wagtailblog observability records v2",
        "processors": [
            {
                "rename": {
                    "field": "message",
                    "target_field": "raw",
                    "ignore_missing": True,
                    "override": False,
                }
            },
            {
                "gsub": {
                    "field": "raw",
                    "pattern": (
                        r"(?i)(password|passwd|token|secret|api_key|access_key)"
                        r"(\s*[:=]\s*)[^\s,;&}\]]+"
                    ),
                    "replacement": r"$1$2[REDACTED]",
                    "ignore_missing": True,
                }
            },
            {
                "gsub": {
                    "field": "raw",
                    "pattern": r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+",
                    "replacement": r"$1[REDACTED]",
                    "ignore_missing": True,
                }
            },
            {
                "gsub": {
                    "field": "raw",
                    "pattern": r"(?i)(cookie\s*[:=]\s*)[^\r\n]+",
                    "replacement": r"$1[REDACTED]",
                    "ignore_missing": True,
                }
            },
            {
                "grok": {
                    "field": "raw",
                    "patterns": [
                        r"^\[%{TIMESTAMP_ISO8601:log_timestamp}\]\s+"
                        r"%{LOGLEVEL:level}\s+\[(?<location>[^\]]+)\]\s+"
                        r"\[pid=%{INT:pid:int}\s+thread=(?<thread>[^\]]+)\]\s*"
                        r"(?<message>[\s\S]*)$"
                    ],
                    "ignore_failure": True,
                }
            },
            {
                "grok": {
                    "field": "location",
                    "patterns": [
                        r"(?<logger>[^|]+)\|(?<relative_path>[^:]+):"
                        r"(?<function>[^:]+):%{INT:line:int}",
                        r"(?<logger>.*)",
                    ],
                    "ignore_missing": True,
                    "ignore_failure": True,
                }
            },
            {
                "set": {
                    "field": "observed_at",
                    "copy_from": "@timestamp",
                    "override": False,
                    "ignore_empty_value": True,
                    "ignore_failure": True,
                },
            },
            {
                "date": {
                    "field": "log_timestamp",
                    "target_field": "@timestamp",
                    "formats": ["yyyy-MM-dd HH:mm:ss"],
                    "timezone": timezone,
                    "if": "ctx.log_timestamp != null && ctx.log_timestamp != ''",
                    "ignore_failure": True,
                }
            },
            {
                "set": {
                    "field": "message",
                    "copy_from": "raw",
                    "override": False,
                    "ignore_empty_value": True,
                    "ignore_failure": True,
                }
            },
            {
                "set": {
                    "field": "source_device",
                    "copy_from": "log.file.device_id",
                    "override": False,
                    "ignore_empty_value": True,
                    "ignore_failure": True,
                },
            },
            {
                "set": {
                    "field": "source_inode",
                    "copy_from": "log.file.inode",
                    "override": False,
                    "ignore_empty_value": True,
                    "ignore_failure": True,
                },
            },
            {
                "set": {
                    "field": "start_offset",
                    "copy_from": "log.offset",
                    "override": False,
                    "ignore_empty_value": True,
                    "ignore_failure": True,
                },
            },
            {
                "remove": {
                    "field": ["log_timestamp", "location"],
                    "ignore_missing": True,
                }
            },
        ],
        "on_failure": [
            {
                "set": {
                    "field": "ingest_error",
                    "value": "log_normalization_failed",
                    "override": False,
                }
            }
        ],
    }


class LogSearchUnavailable(RuntimeError):
    """Raised when the optional ES read model cannot serve a query."""


@dataclass(slots=True)
class ElasticsearchPage:
    records: list[LogRecord]
    next_cursor: str
    has_more: bool
    bytes_read: int = 0


@dataclass(slots=True)
class LogIndexCleanupPlan:
    selectors: list[dict[str, Any]]
    cutoff: datetime
    spec_keys: tuple[str, ...]
    estimated_bytes: int = 0

    def as_payload(self) -> dict[str, Any]:
        """序列化删除计划，供 MySQL outbox 持久化。

        参数：无。
        返回：仅包含 JSON 基本类型的选择器、截点、注册项和估算字节数。
        异常：无。
        """
        return {
            "selectors": self.selectors,
            "cutoff": _as_es_timestamp(self.cutoff),
            "spec_keys": list(self.spec_keys),
            "estimated_bytes": self.estimated_bytes,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "LogIndexCleanupPlan":
        """从 outbox JSON 恢复删除计划。

        参数：``payload`` 为持久化计划字典。
        返回：可执行的 ``LogIndexCleanupPlan``。
        异常：无效字段使用保守默认值；调用方仍受注册表选择器约束。
        """
        cutoff = _parse_timestamp(payload.get("cutoff"))
        if cutoff is None:
            cutoff = datetime.now().replace(tzinfo=None)
        return cls(
            selectors=[item for item in payload.get("selectors", []) if isinstance(item, dict)],
            cutoff=cutoff,
            spec_keys=tuple(str(item) for item in payload.get("spec_keys", [])),
            estimated_bytes=max(0, int(payload.get("estimated_bytes", 0) or 0)),
        )


@dataclass(slots=True)
class LogIndexDeleteResult:
    deleted: int = 0
    task_id: str = ""
    version_conflicts: int = 0
    timed_out: bool = False
    asynchronous: bool = False


class LogIndexSyncError(LogSearchUnavailable):
    """An ES write failure with retry classification for the outbox worker."""

    def __init__(self, message: str, *, status_code: int | None = None):
        """创建带 HTTP 状态码的索引同步异常。

        参数：``message`` 为安全错误摘要，``status_code`` 为可选 ES 状态码。
        返回：无。
        异常：该构造器本身不抛出。
        """
        super().__init__(message)
        self.status_code = status_code
        self.transient = status_code not in {400, 401, 403}


def _config() -> dict[str, Any]:
    """读取并规范化 ES 日志配置。"""
    value = getattr(settings, "ELASTICSEARCH_LOGGING", {})
    return value if isinstance(value, dict) else {}


def _configured_timezone(config: dict[str, Any] | None = None):
    """解析配置时区，未知时区回退到项目默认时区。"""
    config = config or _config()
    name = str(
        config.get("TIMEZONE")
        or getattr(settings, "TIME_ZONE", DEFAULT_TIMEZONE)
    )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return datetime_timezone.utc


def _as_es_timestamp(
    value: datetime | None, config: dict[str, Any] | None = None
) -> str | None:
    """Convert UI/file-reader local datetimes to Elasticsearch UTC timestamps."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_configured_timezone(config))
    return value.astimezone(datetime_timezone.utc).isoformat()


def is_enabled() -> bool:
    """返回 ES 日志读模型是否显式启用。"""
    return bool(_config().get("ENABLED"))


def _failure_cooldown() -> int:
    """返回失败熔断窗口秒数，并限制为合理范围。"""
    try:
        value = int(_config().get("FAILURE_COOLDOWN", DEFAULT_FAILURE_COOLDOWN))
    except (TypeError, ValueError):
        value = DEFAULT_FAILURE_COOLDOWN
    return max(0, min(value, 300))


def _circuit_is_open() -> bool:
    """判断当前进程是否仍处于 ES 失败熔断窗口。"""
    return monotonic() < _circuit_open_until


def _open_circuit() -> None:
    """打开本进程 ES 熔断器，避免每个后台请求重复等待网络超时。"""
    global _circuit_open_until
    already_open = _circuit_is_open()
    cooldown = _failure_cooldown()
    _circuit_open_until = monotonic() + cooldown if cooldown else 0.0
    if cooldown and not already_open:
        logger.warning(
            "Elasticsearch log read model unavailable; falling back to bounded files",
            extra={"event": "observability.elasticsearch.circuit_open"},
        )


def _close_circuit() -> None:
    """在成功 ES 请求后关闭本进程熔断器。"""
    global _circuit_open_until
    _circuit_open_until = 0.0


def _run_request(operation, message: str):
    """Run one ES request and apply the local fail-open circuit breaker."""
    if _circuit_is_open():
        raise LogSearchUnavailable("Elasticsearch 日志后端暂时不可用，请稍后重试")
    try:
        response = operation(_client())
    except LogSearchUnavailable:
        _open_circuit()
        raise
    except Exception as exc:
        _open_circuit()
        raise LogSearchUnavailable(message) from exc
    _close_circuit()
    return response


def _decode_search_after(value: str) -> list[Any] | None:
    """验证并解码 ES ``search_after`` 签名游标。

    参数：``value`` 为客户端游标，空值代表第一页。
    返回：排序值列表或 ``None``。
    异常：签名无效、过期或结构错误时抛出 ``ValueError``。
    """
    if not value:
        return None
    try:
        payload = signing.loads(value, salt=CURSOR_SALT, max_age=CURSOR_MAX_AGE)
    except signing.BadSignature as exc:
        raise ValueError("Elasticsearch 分页游标无效或已过期") from exc
    if not isinstance(payload, list) or len(payload) != 2:
        raise ValueError("Elasticsearch 分页游标格式无效")
    return payload


def _encode_search_after(value: list[Any] | None) -> str:
    """将 ES 排序值签名为不可伪造的分页游标。"""
    if value is None:
        return ""
    return signing.dumps(value, salt=CURSOR_SALT, compress=True)


@lru_cache(maxsize=1)
def _client():
    """延迟创建并缓存 Elasticsearch 客户端。

    参数：无。
    返回：已按 TLS 与认证配置初始化的客户端。
    异常：后端未启用、依赖缺失、地址缺失或初始化失败时抛出
    ``LogSearchUnavailable``。

    客户端只在首次实际请求时创建，避免文件回退模式加载不必要的网络依赖。
    """
    config = _config()
    if not config.get("ENABLED"):
        raise LogSearchUnavailable("Elasticsearch 日志查询未启用")
    try:
        from elasticsearch import Elasticsearch
    except ImportError as exc:
        raise LogSearchUnavailable("未安装 elasticsearch Python 客户端") from exc

    urls = [str(url).strip() for url in (config.get("URLS") or []) if str(url).strip()]
    if not urls:
        raise LogSearchUnavailable("未配置 Elasticsearch 服务地址")
    try:
        timeout = float(config.get("TIMEOUT", 2))
    except (TypeError, ValueError):
        timeout = 2.0
    timeout = max(0.1, min(timeout, 30.0))
    kwargs: dict[str, Any] = {
        "hosts": urls,
        "request_timeout": timeout,
        "verify_certs": bool(config.get("VERIFY_CERTS", True)),
    }
    if config.get("CA_CERTS"):
        kwargs["ca_certs"] = config["CA_CERTS"]
    if config.get("API_KEY"):
        kwargs["api_key"] = config["API_KEY"]
    elif config.get("USERNAME"):
        kwargs["basic_auth"] = (
            config.get("USERNAME", ""),
            config.get("PASSWORD", ""),
        )
    try:
        return Elasticsearch(**kwargs)
    except Exception as exc:
        raise LogSearchUnavailable("无法初始化 Elasticsearch 客户端") from exc


def reset_client_cache() -> None:
    """Clear the process-local client after configuration or credentials change."""
    _client.cache_clear()
    _close_circuit()


def _concrete_index_name(config: dict[str, Any]) -> str:
    """从写别名推导首个具体索引名，保持别名可平滑切换。"""
    alias = str(config.get("WRITE_INDEX") or "wagtailblog-logs-write")
    suffix = "-write"
    prefix = alias[:-len(suffix)] if alias.endswith(suffix) else alias
    return f"{prefix}-000001"


def _catalog_source_key(value: str) -> str:
    """Strip the reader's inode suffix when a registered catalog key exists."""
    source_key = str(value or "")
    catalog_key = source_key.partition("|")[0]
    return catalog_key if catalog_key in LOG_FILE_BY_KEY else source_key


def record_domain(record: LogRecord) -> str:
    """Resolve an indexed record back to its registered log domain."""
    source_key = _catalog_source_key(record.source_key)
    spec = LOG_FILE_BY_KEY.get(source_key)
    if spec:
        return spec.domain
    return next(
        (
            item.domain
            for item in LOG_FILE_BY_KEY.values()
            if item.relative_path == record.source_path
        ),
        "",
    )


def build_filebeat_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a secret-free Filebeat config for the registered active files."""
    config = config or _config()
    root = Path(getattr(settings, "LOG_DIR", "logs"))
    inputs = []
    for spec in LOG_FILE_SPECS:
        path = str(root / spec.relative_path).replace("\\", "/")
        inputs.append(
            {
                "type": "filestream",
                "id": f"wagtailblog-{spec.key}",
                "paths": [path],
                "parsers": [
                    {
                        "multiline": {
                            "type": "pattern",
                            "pattern": r"^\[\d{4}-\d{2}-\d{2} ",
                            "negate": True,
                            "match": "after",
                        }
                    }
                ],
                "fields_under_root": True,
                "fields": {
                    "domain": spec.domain,
                    "kind": spec.kind,
                    "source_key": spec.key,
                    "source_label": spec.label,
                    "source_path": spec.relative_path,
                    "rotation": 0,
                },
            }
        )
    output: dict[str, Any] = {
        "hosts": ["${ELASTICSEARCH_LOG_URL}"],
        "index": str(config.get("WRITE_INDEX") or "wagtailblog-logs-write"),
        # Filebeat sends bulk request query parameters from this map. Using
        # `output.elasticsearch.pipeline` is not reliable with the custom
        # filestream inputs on Filebeat 8.17 and silently skips the ingest
        # pipeline, so keep the pipeline ID in the explicit request params.
        "parameters": {"pipeline": _pipeline_name(config)},
    }
    auth_mode = str(config.get("AUTH_MODE") or "").strip().lower()
    if not auth_mode:
        auth_mode = "api_key" if config.get("API_KEY") else "basic" if config.get("USERNAME") else ""
    if auth_mode == "api_key":
        output["api_key"] = "${ELASTICSEARCH_LOG_API_KEY}"
    elif auth_mode == "basic":
        output["username"] = "${ELASTICSEARCH_LOG_USERNAME}"
        output["password"] = "${ELASTICSEARCH_LOG_PASSWORD}"
    elif auth_mode:
        raise ValueError("ELASTICSEARCH_LOG_AUTH_MODE must be api_key or basic")
    if config.get("CA_CERTS"):
        output["ssl.certificate_authorities"] = [str(config["CA_CERTS"])]
    if not bool(config.get("VERIFY_CERTS", True)):
        output["ssl.verification_mode"] = "none"
    return {
        "filebeat.inputs": inputs,
        "processors": [
            {
                "fingerprint": {
                    "fields": [
                        "source_key",
                        "log.file.path",
                        "log.offset",
                        "message",
                    ],
                    "target_field": "document_id",
                    "method": "sha256",
                }
            },
            {
                "fingerprint": {
                    "fields": [
                        "source_key",
                        "log.file.path",
                        "log.offset",
                        "message",
                    ],
                    # Beats maps @metadata._id to the Elasticsearch document
                    # _id. This makes at-least-once retries idempotent.
                    "target_field": "@metadata._id",
                    "method": "sha256",
                }
            },
        ],
        "setup.template.enabled": False,
        "setup.ilm.enabled": False,
        "output.elasticsearch": output,
    }


def _response_body(response: Any) -> dict[str, Any]:
    """兼容不同客户端响应对象并返回响应字典。"""
    body = getattr(response, "body", response)
    if isinstance(body, dict):
        return body
    try:
        return dict(body)
    except (TypeError, ValueError):
        return {}


def _doc_count(value: Any) -> int:
    """把 ES 返回的文档计数安全转换为非负整数。"""
    if not isinstance(value, dict):
        return 0
    try:
        return max(0, int(value.get("doc_count", 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _status_code(exc: Exception) -> int | None:
    """从不同 ES 异常形态中提取 HTTP 状态码，无法识别时返回 ``None``。"""
    status = getattr(exc, "status_code", None)
    if status is None:
        meta = getattr(exc, "meta", None)
        status = getattr(meta, "status", None)
        if status is None and isinstance(meta, dict):
            status = meta.get("status")
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_not_found(exc: Exception) -> bool:
    """判断异常是否表示索引、别名或 task 不存在。"""
    return _status_code(exc) == 404 or "resource_not_found_exception" in str(exc).lower()


def _is_already_exists(exc: Exception) -> bool:
    """判断异常是否为可幂等处理的资源已存在冲突。"""
    return _status_code(exc) in {400, 409} and "already_exists" in str(exc).lower()


def build_cleanup_plan(
    specs,
    cleanup_result,
    *,
    cutoff: datetime,
) -> LogIndexCleanupPlan:
    """Build bounded ES deletion clauses from registry-owned cleanup results."""
    allowed_specs = {
        spec.key: spec
        for spec in specs
        if LOG_FILE_BY_KEY.get(spec.key) == spec
    }
    selectors: list[dict[str, Any]] = []
    estimated_bytes = 0
    cutoff_value = _as_es_timestamp(cutoff)

    for result in cleanup_result.file_results:
        if not result.get("succeeded"):
            continue
        source_key = str(result.get("source_key") or "")
        source_path = str(result.get("source_path") or "")
        spec = allowed_specs.get(source_key)
        if spec is None or source_path != spec.relative_path:
            continue
        try:
            rotation = int(result.get("rotation", 0))
            bytes_before = max(0, int(result.get("bytes_before", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if rotation < 0 or rotation > spec.backup_count:
            continue

        filters: list[dict[str, Any]] = [
            {"term": {"source_key": source_key}},
            {"term": {"source_path": source_path}},
            {"term": {"rotation": rotation}},
        ]
        time_filter: dict[str, Any] = {
            "bool": {
                "should": [
                    {"range": {"observed_at": {"lte": cutoff_value}}},
                    {
                        "bool": {
                            "filter": [
                                {
                                    "bool": {
                                        "must_not": [
                                            {"exists": {"field": "observed_at"}}
                                        ]
                                    }
                                },
                                {"range": {"@timestamp": {"lte": cutoff_value}}},
                            ]
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }
        pre_device = result.get("pre_device")
        pre_inode = result.get("pre_inode")
        if pre_device is not None and pre_inode is not None:
            identity_filters: list[dict[str, Any]] = [
                {"term": {"source_device": str(pre_device)}},
                {"term": {"source_inode": str(pre_inode)}},
                time_filter,
            ]
            if rotation == 0 and bytes_before:
                identity_filters.append(
                    {
                        "bool": {
                            "should": [
                                {"range": {"start_offset": {"lt": bytes_before}}},
                                {
                                    "bool": {
                                        "must_not": [
                                            {"exists": {"field": "start_offset"}}
                                        ]
                                    }
                                },
                            ],
                            "minimum_should_match": 1,
                        }
                    }
                )
            filters.append(
                {
                    "bool": {
                        "should": [
                            {"bool": {"filter": identity_filters}},
                            {
                                "bool": {
                                    "filter": [
                                        {
                                            "bool": {
                                                "should": [
                                                    {
                                                        "bool": {
                                                            "must_not": [
                                                                {
                                                                    "exists": {
                                                                        "field": "source_device"
                                                                    }
                                                                }
                                                            ]
                                                        }
                                                    },
                                                    {
                                                        "bool": {
                                                            "must_not": [
                                                                {
                                                                    "exists": {
                                                                        "field": "source_inode"
                                                                    }
                                                                }
                                                            ]
                                                        }
                                                    },
                                                ],
                                                "minimum_should_match": 1,
                                            }
                                        },
                                        time_filter,
                                    ],
                                }
                            },
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        else:
            filters.append(time_filter)
        selectors.append({"bool": {"filter": filters}})
        estimated_bytes += bytes_before

    return LogIndexCleanupPlan(
        selectors=selectors,
        cutoff=cutoff,
        spec_keys=tuple(allowed_specs),
        estimated_bytes=estimated_bytes,
    )


def _delete_query(plan: LogIndexCleanupPlan) -> dict[str, Any]:
    """将受控清理计划转换为 ES 删除查询。

    参数：``plan`` 来自注册表与实际文件身份生成的删除计划。
    返回：只匹配计划选择器的 bool 查询；无选择器时返回 ``match_none``。
    异常：无。

    永远不使用空查询，防止配置或序列化错误演变为整索引删除。
    """
    if not plan.selectors:
        return {"match_none": {}}
    return {
        "bool": {
            "should": plan.selectors,
            "minimum_should_match": 1,
        }
    }


def _run_index_sync_request(operation, message: str):
    """执行 ES 写操作并将底层异常标准化为 ``LogIndexSyncError``。

    参数：``operation`` 接收客户端并执行请求，``message`` 为安全错误前缀。
    返回：ES 响应对象。
    异常：连接、权限、超时或响应失败时抛出 ``LogIndexSyncError``。
    """
    try:
        return operation(_client())
    except LogIndexSyncError:
        raise
    except LogSearchUnavailable as exc:
        raise LogIndexSyncError(str(exc)) from exc
    except Exception as exc:
        raise LogIndexSyncError(
            message,
            status_code=_status_code(exc),
        ) from exc


def delete_logs_by_plan(
    plan: LogIndexCleanupPlan,
    *,
    wait_for_completion: bool = True,
) -> LogIndexDeleteResult:
    """Delete matching read-model documents without touching aliases or indices."""
    if not plan.selectors:
        return LogIndexDeleteResult()
    config = _config()
    if not config.get("ENABLED"):
        raise LogIndexSyncError("Elasticsearch log synchronization is disabled")
    index = str(config.get("READ_INDEX") or "").strip()
    if not index:
        raise LogIndexSyncError(
            "Elasticsearch log read alias is not configured",
            status_code=400,
        )
    kwargs: dict[str, Any] = {
        "index": index,
        "query": _delete_query(plan),
        "conflicts": "proceed",
        "wait_for_completion": wait_for_completion,
        "slices": "auto",
        "refresh": True,
    }
    response = _run_index_sync_request(
        lambda client: client.delete_by_query(**kwargs),
        "Elasticsearch log cleanup failed",
    )
    body = _response_body(response)
    failures = body.get("failures") or []
    if failures:
        raise LogIndexSyncError(
            f"Elasticsearch log cleanup returned {len(failures)} shard failures"
        )
    return LogIndexDeleteResult(
        deleted=max(0, int(body.get("deleted", 0) or 0)),
        task_id=str(body.get("task") or ""),
        version_conflicts=max(0, int(body.get("version_conflicts", 0) or 0)),
        timed_out=bool(body.get("timed_out", False)),
        asynchronous=not wait_for_completion,
    )


def get_delete_task_result(task_id: str) -> tuple[bool, LogIndexDeleteResult]:
    """Poll one ES delete-by-query task."""
    response = _run_index_sync_request(
        lambda client: client.tasks.get(task_id=task_id),
        "Elasticsearch log cleanup task polling failed",
    )
    body = _response_body(response)
    if not body.get("completed"):
        return False, LogIndexDeleteResult(task_id=task_id, asynchronous=True)
    if body.get("error"):
        error = body["error"]
        status = error.get("status") if isinstance(error, dict) else None
        raise LogIndexSyncError(
            "Elasticsearch log cleanup task failed",
            status_code=_safe_int(status),
        )
    task_response = body.get("response") or {}
    if not isinstance(task_response, dict):
        task_response = {}
    failures = task_response.get("failures") or []
    if failures:
        raise LogIndexSyncError(
            f"Elasticsearch log cleanup task returned {len(failures)} shard failures"
        )
    return True, LogIndexDeleteResult(
        deleted=max(0, int(task_response.get("deleted", 0) or 0)),
        task_id=task_id,
        version_conflicts=max(
            0, int(task_response.get("version_conflicts", 0) or 0)
        ),
        timed_out=bool(task_response.get("timed_out", False)),
        asynchronous=True,
    )


def pending_cleanup_exclusions(limit: int = 100) -> list[dict[str, Any]]:
    """Return durable cleanup tombstones while physical ES deletion is pending."""
    try:
        from .models import LogIndexSyncJob

        payloads = LogIndexSyncJob.objects.exclude(state="completed").order_by(
            "created_at"
        ).values_list("selector", flat=True)[: max(1, min(limit, 500))]
        clauses: list[dict[str, Any]] = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            for clause in payload.get("selectors", []):
                if isinstance(clause, dict):
                    clauses.append(clause)
                if len(clauses) >= 512:
                    return clauses
        return clauses
    except Exception:
        # The read model remains optional during migrations and database outages.
        return []


def _alias_info(client, alias: str) -> dict[str, Any]:
    """读取一个别名的索引映射；别名不存在时返回空字典。"""
    try:
        response = client.indices.get_alias(name=alias)
    except Exception as exc:
        if _is_not_found(exc):
            return {}
        raise
    return _response_body(response)


def _assert_write_alias_available(
    write_info: dict[str, Any], concrete: str, write_alias: str
) -> None:
    """确认 write alias 不会覆盖属于其他具体索引的写入口。

    参数：客户端、目标具体索引和 write alias。
    返回：无。
    异常：别名已指向其他写索引时抛出 ``LogIndexSyncError``。
    """
    conflicting_targets = set(write_info) - {concrete}
    if conflicting_targets:
        names = ", ".join(sorted(conflicting_targets))
        raise LogSearchUnavailable(
            f"Elasticsearch 写入别名 {write_alias} 已指向其他索引: {names}"
        )


def _ensure_aliases(client, concrete: str, read_alias: str, write_alias: str) -> None:
    """Repair only missing aliases; refuse to take over another write index."""
    read_info = _alias_info(client, read_alias)
    write_info = _alias_info(client, write_alias)
    _assert_write_alias_available(write_info, concrete, write_alias)

    actions: list[dict[str, Any]] = []
    if concrete not in read_info:
        actions.append({"add": {"index": concrete, "alias": read_alias}})

    concrete_info = write_info.get(concrete, {})
    aliases = concrete_info.get("aliases", {}) if isinstance(concrete_info, dict) else {}
    write_metadata = aliases.get(write_alias, {}) if isinstance(aliases, dict) else {}
    if concrete not in write_info or write_metadata.get("is_write_index") is not True:
        actions.append(
            {
                "add": {
                    "index": concrete,
                    "alias": write_alias,
                    "is_write_index": True,
                }
            }
        )
    if actions:
        client.indices.update_aliases(actions=actions)


def _ensure_ingest_pipeline(client, config: dict[str, Any]) -> None:
    """Create the pipeline once; never overwrite an operator-managed one."""
    pipeline = _pipeline_name(config)
    try:
        response = client.ingest.get_pipeline(id=pipeline)
    except Exception as exc:
        if not _is_not_found(exc):
            raise
        client.ingest.put_pipeline(id=pipeline, **build_ingest_pipeline(config))
        return
    if not _response_body(response):
        # Some test doubles and older clients return an empty body for a
        # successful GET. Treat that as present rather than overwriting it.
        return


def _prepare_log_index_with_client(client, config: dict[str, Any]) -> str:
    """使用给定客户端创建或修复日志索引、别名、mapping 和 pipeline。

    参数：``client`` 为 ES 客户端，``config`` 为日志索引配置。
    返回：准备完成的具体索引名。
    异常：别名冲突、ES API 失败时抛出 ``LogIndexSyncError``。

    索引准备只允许创建缺失资源或补齐 mapping，避免请求路径隐式重建数据。
    """
    concrete = _concrete_index_name(config)
    read_alias = str(config.get("READ_INDEX") or "")
    write_alias = str(config.get("WRITE_INDEX") or "")
    if not read_alias or not write_alias:
        raise LogSearchUnavailable("未配置 Elasticsearch 日志别名")
    if read_alias == write_alias:
        raise LogSearchUnavailable("读写别名必须使用不同名称")
    if concrete in {read_alias, write_alias}:
        raise LogSearchUnavailable("日志实体索引名称不能与别名相同")

    _ensure_ingest_pipeline(client, config)
    exists = bool(client.indices.exists(index=concrete))
    write_info = _alias_info(client, write_alias)
    _assert_write_alias_available(write_info, concrete, write_alias)
    settings_body: dict[str, Any] = {
        "number_of_shards": max(1, int(config.get("NUMBER_OF_SHARDS", 1))),
        "number_of_replicas": max(0, int(config.get("NUMBER_OF_REPLICAS", 0))),
    }
    if config.get("ILM_POLICY"):
        settings_body["index.lifecycle.name"] = config["ILM_POLICY"]
        settings_body["index.lifecycle.rollover_alias"] = write_alias

    if not exists:
        try:
            client.indices.create(
                index=concrete,
                settings=settings_body,
                mappings=LOG_MAPPINGS,
                aliases={
                    read_alias: {},
                    write_alias: {"is_write_index": True},
                },
            )
        except Exception as exc:
            # Another deploy may have won the create race. Alias repair remains
            # safe and idempotent; all other failures are surfaced.
            if not _is_already_exists(exc):
                raise
    else:
        # Adding properties is idempotent and keeps an existing v1 index
        # queryable while Filebeat moves to the versioned v2 pipeline.
        client.indices.put_mapping(index=concrete, **LOG_MAPPINGS)
    _ensure_aliases(client, concrete, read_alias, write_alias)
    return concrete


def prepare_log_index() -> str:
    """Create the first log index and aliases; never deletes existing data."""
    config = _config()
    if not config.get("ENABLED"):
        raise LogSearchUnavailable("请先启用 Elasticsearch 日志配置")
    return _run_request(
        lambda client: _prepare_log_index_with_client(client, config),
        "无法创建 Elasticsearch 日志索引",
    )


def get_log_summary(*, since: datetime | None = None) -> dict[str, Any]:
    """Return bounded overview counters using one Elasticsearch aggregation."""
    if not is_enabled():
        raise LogSearchUnavailable("Elasticsearch 日志查询未启用")
    config = _config()
    index = config.get("READ_INDEX")
    if not index:
        raise LogSearchUnavailable("未配置 Elasticsearch 日志读取索引")
    filters: list[dict[str, Any]] = []
    if since:
        filters.append(
            {"range": {"@timestamp": {"gte": _as_es_timestamp(since, config)}}}
        )
    body = {
        "size": 0,
        "track_total_hits": False,
        "query": {"bool": {"filter": filters}},
        "aggs": {
            "error_count": {"filter": {"term": {"kind": "error"}}},
            "warning_count": {
                "filter": {
                    "bool": {
                        "filter": [
                            {"term": {"kind": "activity"}},
                            {"term": {"level": "WARNING"}},
                        ]
                    }
                }
            },
            "domains": {
                "terms": {"field": "domain", "size": 100},
                "aggs": {
                    "errors": {"filter": {"term": {"kind": "error"}}}
                },
            },
        },
    }
    response = _run_request(
        lambda client: client.search(index=index, **body),
        "Elasticsearch 日志概览查询失败",
    )
    response = _response_body(response)
    aggregations = response.get("aggregations") or {}
    if not isinstance(aggregations, dict):
        raise LogSearchUnavailable("Elasticsearch 日志概览响应格式无效")
    domain_aggregation = aggregations.get("domains") or {}
    if not isinstance(domain_aggregation, dict):
        domain_aggregation = {}
    domains = {
        str(bucket.get("key")): _doc_count(bucket.get("errors"))
        for bucket in domain_aggregation.get("buckets", [])
        if isinstance(bucket, dict)
    }
    return {
        "error_count": _doc_count(aggregations.get("error_count")),
        "warning_count": _doc_count(aggregations.get("warning_count")),
        "errors_by_domain": domains,
    }


def record_document(record: LogRecord, *, domain: str = "") -> tuple[str, dict[str, Any]]:
    """Convert a sanitized LogRecord to a stable, indexable ES document."""
    source_identity = record.source_key or record.source_path
    source_key = _catalog_source_key(source_identity)
    identity_parts = source_identity.split("|")
    source_device = identity_parts[-2] if len(identity_parts) >= 3 else None
    source_inode = identity_parts[-1] if len(identity_parts) >= 3 else None
    spec = LOG_FILE_BY_KEY.get(source_key)
    raw = sanitize_log_text(record.raw)
    content_fingerprint = hashlib.blake2s(
        raw.encode("utf-8"), digest_size=16
    ).hexdigest()
    document_key = "|".join(
        (
            source_identity,
            str(record.start_offset),
            str(record.end_offset),
            content_fingerprint,
        )
    )
    document_id = hashlib.sha256(document_key.encode("utf-8")).hexdigest()
    timestamp = _as_es_timestamp(record.timestamp)
    relative_path = project_relative_path(sanitize_log_text(record.relative_path))
    source_path = project_relative_path(
        sanitize_log_text(record.source_path or (spec.relative_path if spec else ""))
    )
    document: dict[str, Any] = {
        "@timestamp": timestamp,
        "observed_at": timestamp,
        "level": record.level,
        "kind": "error" if record.level in {"ERROR", "CRITICAL"} else "activity",
        "domain": domain or record_domain(record),
        "document_id": document_id,
        "logger": sanitize_log_text(record.logger),
        "source_key": source_key,
        "source_identity": source_identity,
        "source_device": source_device,
        "source_inode": source_inode,
        "source_label": sanitize_log_text(
            record.source_label or (spec.label if spec else source_key)
        ),
        "source_path": source_path,
        "relative_path": relative_path,
        "message": sanitize_log_text(record.message),
        "raw": raw,
        "traceback": sanitize_log_text(record.traceback),
        "function": sanitize_log_text(record.function),
        "line": record.line,
        "pid": record.pid,
        "thread": sanitize_log_text(record.thread),
        "rotation": record.rotation,
        "start_offset": record.start_offset,
        "end_offset": record.end_offset,
    }
    return document_id, {key: value for key, value in document.items() if value is not None}


def bulk_index_records(records: list[tuple[str, dict[str, Any]]]) -> int:
    """Index sanitized records in one bounded bulk request."""
    if not records:
        return 0
    config = _config()
    index = config.get("WRITE_INDEX")
    if not index:
        raise LogSearchUnavailable("未配置 Elasticsearch 日志写入索引")
    operations: list[dict[str, Any]] = []
    for document_id, document in records:
        operations.append({"index": {"_index": index, "_id": document_id}})
        operations.append(document)
    response = _run_request(
        lambda client: client.bulk(operations=operations, index=index),
        "Elasticsearch 日志批量写入失败",
    )
    response = _response_body(response)
    if response.get("errors"):
        failed = [
            item
            for item in response.get("items", [])
            if isinstance(item, dict) and item.get("index", {}).get("error")
        ]
        raise LogSearchUnavailable(f"Elasticsearch 日志批量写入部分失败: {len(failed)}")
    return len(records)


def _safe_int(value: Any, default: int | None = None, *, minimum: int | None = None) -> int | None:
    """安全转换 ES 字段为整数，并可施加最小值限制。"""
    try:
        converted = int(value) if value is not None else default
    except (TypeError, ValueError, OverflowError):
        return default
    if converted is None:
        return None
    if minimum is not None and converted < minimum:
        return default
    return converted


def _parse_timestamp(value: Any) -> datetime | None:
    """解析 ES 时间字段为 datetime，无法解析时返回 ``None``。"""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(float(value) / 1000, tz=datetime_timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime_timezone.utc)
        return parsed.astimezone(_configured_timezone()).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _record_from_source(source: dict[str, Any]) -> LogRecord:
    """将脱敏后的 ES ``_source`` 映射回统一 ``LogRecord``。

    参数：``source`` 为 ES 文档字段。
    返回：供模板与文件读取器共用的日志记录。
    异常：字段类型异常通过安全转换降级，不因单条脏数据中断整页查询。
    """
    timestamp = source.get("@timestamp") or source.get("timestamp")
    parsed_timestamp = _parse_timestamp(timestamp)

    source_key = _catalog_source_key(
        str(source.get("source_key") or source.get("source") or "")
    )
    spec = LOG_FILE_BY_KEY.get(source_key)
    source_path = project_relative_path(
        str(source.get("source_path") or (spec.relative_path if spec else ""))
    )
    relative_path = str(source.get("relative_path") or "")
    if relative_path:
        relative_path = project_relative_path(relative_path)
    raw = sanitize_log_text(str(source.get("raw") or source.get("message") or ""))
    message = sanitize_log_text(str(source.get("message") or raw))
    traceback = sanitize_log_text(str(source.get("traceback") or ""))
    line = _safe_int(source.get("line"), minimum=0)
    pid = _safe_int(source.get("pid"), minimum=0)

    return LogRecord(
        timestamp=parsed_timestamp,
        level=str(source.get("level") or "UNKNOWN"),
        logger=str(source.get("logger") or ""),
        relative_path=relative_path,
        function=str(source.get("function") or ""),
        line=line,
        pid=pid,
        thread=str(source.get("thread") or ""),
        message=message,
        traceback=traceback,
        raw=raw,
        source_key=source_key,
        source_label=str(source.get("source_label") or (spec.label if spec else source_key)),
        source_path=source_path,
        rotation=_safe_int(source.get("rotation"), 0, minimum=0) or 0,
        start_offset=_safe_int(source.get("start_offset"), 0, minimum=0) or 0,
        end_offset=_safe_int(source.get("end_offset"), 0, minimum=0) or 0,
    )


def search_logs(
    *,
    domain: str = "",
    kind: str = "error",
    level: str = "",
    keyword: str = "",
    since: datetime | None = None,
    until: datetime | None = None,
    include_rotated: bool = False,
    page_size: int = 100,
    cursor: str = "",
) -> ElasticsearchPage:
    """Search the log read model with bounded results and search_after."""
    if not is_enabled():
        raise LogSearchUnavailable("Elasticsearch 日志查询未启用")
    page_size = min(max(int(page_size), 1), MAX_PAGE_SIZE)
    config = _config()
    index = config.get("READ_INDEX")
    if not index:
        raise LogSearchUnavailable("未配置 Elasticsearch 日志读取索引")

    filters: list[dict[str, Any]] = []
    if domain:
        filters.append({"term": {"domain": domain}})
    if kind:
        filters.append({"term": {"kind": kind}})
    if level:
        filters.append({"term": {"level": level}})
    if not include_rotated:
        filters.append({"term": {"rotation": 0}})
    date_range: dict[str, str] = {}
    if since:
        date_range["gte"] = _as_es_timestamp(since, config)
    if until:
        date_range["lte"] = _as_es_timestamp(until, config)
    if date_range:
        filters.append({"range": {"@timestamp": date_range}})

    query: dict[str, Any] = {"bool": {"filter": filters}}
    cleanup_exclusions = pending_cleanup_exclusions()
    if cleanup_exclusions:
        query["bool"]["must_not"] = cleanup_exclusions
    if keyword:
        query["bool"]["must"] = [
            {
                "simple_query_string": {
                    "query": keyword,
                    "fields": ["message", "logger", "source_path"],
                    "default_operator": "and",
                }
            }
        ]
    else:
        query["bool"]["must"] = [{"match_all": {}}]

    body: dict[str, Any] = {
        "query": query,
        "size": page_size + 1,
        "track_total_hits": False,
        "sort": [
            {"@timestamp": {"order": "desc", "unmapped_type": "date"}},
            {"document_id": {"order": "desc", "unmapped_type": "keyword"}},
        ],
        "_source": True,
    }
    search_after = _decode_search_after(cursor)
    if search_after is not None:
        body["search_after"] = search_after

    response = _run_request(
        lambda client: client.search(index=index, **body),
        "Elasticsearch 日志查询失败",
    )
    response = _response_body(response)

    hits_body = response.get("hits") or {}
    if not isinstance(hits_body, dict):
        raise LogSearchUnavailable("Elasticsearch 日志查询响应格式无效")
    hits = hits_body.get("hits") or []
    if not isinstance(hits, list):
        raise LogSearchUnavailable("Elasticsearch 日志查询响应格式无效")
    has_more = len(hits) > page_size
    hits = hits[:page_size]
    records = [
        _record_from_source(hit.get("_source") or {})
        for hit in hits
        if isinstance(hit, dict) and isinstance(hit.get("_source") or {}, dict)
    ]
    next_sort = hits[-1].get("sort") if has_more and hits and isinstance(hits[-1], dict) else None
    return ElasticsearchPage(
        records=records,
        next_cursor=_encode_search_after(next_sort),
        has_more=has_more,
    )
