"""正式搜索快照的只读值对象；规范 JSON 同时是 SQL 存储与摘要的边界。"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping


class PublishedSnapshotError(ValueError):
    """固定脱敏错误，不能把正文、请求或连接信息放入异常。"""


def canonical_json(data: Any) -> str:
    """拒绝非 JSON 值及 NaN，保留原 Markdown 字面值。"""
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise PublishedSnapshotError("snapshot_invalid_json") from None


def canonical_digest(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def positive_int(value: Any, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (type(value) is int and value > 0)


def effective_projection_schema_version(value: Any) -> int:
    """旧 NULL 只代表 legacy1；不以迁移默认值把旧事件冒充新协议。"""
    if value is None:
        return 1
    if type(value) is not int or value not in (1, 2):
        raise PublishedSnapshotError("projection_schema_unsupported")
    return value


def utc_datetime(value: datetime | str | None) -> str | None:
    """首次发布时间必须有时区；规范成 UTC，禁止猜测站点时区。"""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        raise PublishedSnapshotError("snapshot_datetime_invalid") from None


def _keys(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise PublishedSnapshotError("snapshot_keys_invalid")


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _manifest(data: Any, snapshot: dict) -> None:
    """完整保留显式字段身份；未知协议/codec 与跨页指针一律拒绝。"""
    _keys(data, {"protocol_version", "content_storage_schema", "model_label", "page_id", "fields"})
    if type(data["protocol_version"]) is not int or data["protocol_version"] != 1:
        raise PublishedSnapshotError("manifest_protocol_unsupported")
    if not positive_int(data["content_storage_schema"]) or not positive_int(data["page_id"]):
        raise PublishedSnapshotError("manifest_identity_invalid")
    allowed_owner = snapshot["publication_ref"]["source_page_id"] or snapshot["page_id"]
    if data["model_label"] != snapshot["model_label"] or data["page_id"] != allowed_owner:
        raise PublishedSnapshotError("manifest_identity_mismatch")
    if not isinstance(data["fields"], dict) or not data["fields"]:
        raise PublishedSnapshotError("manifest_fields_invalid")
    for name, entry in data["fields"].items():
        if not isinstance(name, str) or not name.strip():
            raise PublishedSnapshotError("manifest_field_invalid")
        _keys(entry, {"codec", "schema_version", "version_id", "payload_sha256"}, {"model_label", "page_id", "field_name"})
        if entry["codec"] != "streamfield.v1" or type(entry["schema_version"]) is not int or entry["schema_version"] != 1:
            raise PublishedSnapshotError("manifest_codec_unsupported")
        if not isinstance(entry["version_id"], str) or not entry["version_id"].strip() or not _sha(entry["payload_sha256"]):
            raise PublishedSnapshotError("manifest_pointer_invalid")
        for key, expected in (("model_label", data["model_label"]), ("page_id", data["page_id"]), ("field_name", name)):
            if key in entry and (type(entry[key]) is not type(expected) or entry[key] != expected):
                raise PublishedSnapshotError("manifest_field_identity_mismatch")


@dataclass(frozen=True)
class PublishedSnapshotV1:
    """内部只存不可变 JSON 字符串；导出副本不能修改已捕获快照。"""

    _canonical: str

    def __post_init__(self) -> None:
        # 直接构造也不允许绕过 from_dict 的身份/schema 检查。
        try:
            data = json.loads(self._canonical)
        except (TypeError, ValueError):
            raise PublishedSnapshotError("snapshot_invalid_json") from None
        self._validate(data)
        object.__setattr__(self, "_canonical", canonical_json(data))

    @staticmethod
    def _validate(data: Any) -> None:
        _keys(data, {
            "snapshot_schema_version", "snapshot_kind", "page_id", "model_label",
            "search_type", "publication_generation", "publication_ref",
            "published_metadata", "body_ref", "projection_schema_version",
        })
        if type(data["snapshot_schema_version"]) is not int or data["snapshot_schema_version"] != 1:
            raise PublishedSnapshotError("snapshot_schema_unsupported")
        if type(data["projection_schema_version"]) is not int or data["projection_schema_version"] != 2:
            raise PublishedSnapshotError("projection_schema_unsupported")
        if data["snapshot_kind"] not in ("blog_v1", "external_v1"):
            raise PublishedSnapshotError("snapshot_kind_unsupported")
        if not positive_int(data["page_id"]) or not positive_int(data["publication_generation"]):
            raise PublishedSnapshotError("snapshot_identity_invalid")
        if not isinstance(data["model_label"], str) or not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", data["model_label"]):
            raise PublishedSnapshotError("snapshot_model_invalid")
        if not isinstance(data["search_type"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", data["search_type"]) or data["search_type"] == "all":
            raise PublishedSnapshotError("snapshot_search_type_invalid")
        ref = data["publication_ref"]
        _keys(ref, {"published_revision_id", "source_page_id", "source_revision_id"})
        if any(not positive_int(v, nullable=True) for v in ref.values()):
            raise PublishedSnapshotError("snapshot_revision_invalid")
        if (ref["source_page_id"] is None) != (ref["source_revision_id"] is None):
            raise PublishedSnapshotError("snapshot_source_incomplete")
        if ref["published_revision_id"] is None and ref["source_revision_id"] is None:
            raise PublishedSnapshotError("snapshot_revision_missing")
        if ref["source_page_id"] == data["page_id"]:
            raise PublishedSnapshotError("snapshot_source_invalid")
        metadata = data["published_metadata"]
        business = {"intro", "date", "tag_ids", "category_ids"} if data["snapshot_kind"] == "blog_v1" else set()
        _keys(metadata, {"title", "summary", "first_published_at"} | business)
        if not isinstance(metadata["title"], str) or not isinstance(metadata["summary"], str):
            raise PublishedSnapshotError("snapshot_metadata_invalid")
        if metadata["first_published_at"] is None:
            raise PublishedSnapshotError("snapshot_first_publication_missing")
        metadata["first_published_at"] = utc_datetime(metadata["first_published_at"])
        if data["snapshot_kind"] == "external_v1":
            _manifest(data["body_ref"], data)
        else:
            if data["model_label"] != "blog.blogpage":
                raise PublishedSnapshotError("snapshot_blog_identity_invalid")
            if not isinstance(metadata["intro"], str):
                raise PublishedSnapshotError("snapshot_metadata_invalid")
            for name in ("tag_ids", "category_ids"):
                if not isinstance(metadata[name], list) or any(not positive_int(v) for v in metadata[name]):
                    raise PublishedSnapshotError("snapshot_business_ids_invalid")
            date = metadata["date"]
            if date is not None:
                from datetime import date as date_type
                try:
                    if not isinstance(date, str) or date_type.fromisoformat(date).isoformat() != date:
                        raise ValueError
                except ValueError:
                    raise PublishedSnapshotError("snapshot_business_date_invalid") from None
            body = data["body_ref"]
            _keys(body, {"mongo_content_id", "body_version_id", "body_sha256", "body_schema_version"})
            if body["mongo_content_id"] is not None and (not isinstance(body["mongo_content_id"], str) or not body["mongo_content_id"]):
                raise PublishedSnapshotError("snapshot_blog_pointer_invalid")
            if not isinstance(body["body_version_id"], str) or not body["body_version_id"] or not _sha(body["body_sha256"]) or type(body["body_schema_version"]) is not int or body["body_schema_version"] != 1:
                raise PublishedSnapshotError("snapshot_blog_pointer_invalid")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, expected_digest: str | None = None) -> "PublishedSnapshotV1":
        snapshot = cls(canonical_json(data))
        if expected_digest is not None and (not _sha(expected_digest) or snapshot.digest != expected_digest):
            raise PublishedSnapshotError("snapshot_digest_mismatch")
        return snapshot

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self._canonical.encode("utf-8")).hexdigest()

    @property
    def page_id(self) -> int:
        return self.to_dict()["page_id"]

    @property
    def snapshot_kind(self) -> str:
        return self.to_dict()["snapshot_kind"]

    def validate_state(self, state: Any) -> None:
        """当前 SQL State 的严格围栏；不读取 latest_revision 或补齐缺值。"""
        data = self.to_dict()
        for key in ("page_id", "model_label", "search_type", "snapshot_kind", "publication_generation", "projection_schema_version"):
            actual = getattr(state, key, None)
            if type(actual) is not type(data[key]) or actual != data[key]:
                raise PublishedSnapshotError("snapshot_state_mismatch")
        if getattr(state, "snapshot_digest", None) != self.digest:
            raise PublishedSnapshotError("snapshot_state_digest_mismatch")
        # Python 的 3 == 3.0 / True == 1 不能作为 JSON 身份等价，更不能绕过摘要。
        if canonical_json(getattr(state, "published_snapshot", None)) != self._canonical:
            raise PublishedSnapshotError("snapshot_state_payload_mismatch")
