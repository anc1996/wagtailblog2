import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from django.db import IntegrityError, transaction

from blog.models import MarkdownImportBatch


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Mapping[str, "JsonValue"] | Sequence["JsonValue"]


class IdempotencyKeyError(ValueError):
    """表示客户端幂等键不是 RFC 4122 UUIDv4。"""


class IdempotencyConflictError(ValueError):
    """表示同一用户重复使用幂等键提交了不同的导入请求。"""


@dataclass(frozen=True)
class BatchClaim:
    batch: MarkdownImportBatch
    created: bool


def validate_idempotency_key(value: str | uuid.UUID) -> uuid.UUID:
    try:
        key = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise IdempotencyKeyError("idempotency_key_invalid") from exc
    if key.version != 4 or key.variant != uuid.RFC_4122:
        raise IdempotencyKeyError("idempotency_key_not_uuid4")
    return key


def build_request_fingerprint(payload: JsonValue) -> str:
    """对稳定导入契约生成指纹，不纳入临时路径或 multipart 边界。"""

    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("request_fingerprint_payload_invalid") from exc
    return hashlib.sha256(canonical).hexdigest()


def _claim_existing(batch, request_fingerprint: str) -> BatchClaim:
    if batch.request_fingerprint != request_fingerprint:
        raise IdempotencyConflictError("idempotency_conflict")
    return BatchClaim(batch=batch, created=False)


def claim_import_batch(
    *,
    user_id: int,
    idempotency_key: str | uuid.UUID,
    request_fingerprint: str,
    target_parent_id: int,
    manager=None,
) -> BatchClaim:
    """认领导入批次，并把同一幂等键的并发创建收敛到唯一记录。"""

    key = validate_idempotency_key(idempotency_key)
    batch_manager = manager or MarkdownImportBatch.objects
    lookup = {"user_id": user_id, "idempotency_key": key}
    existing = batch_manager.filter(**lookup).first()
    if existing is not None:
        return _claim_existing(existing, request_fingerprint)

    try:
        # 内层事务允许唯一约束失败后，在外层事务中继续读取竞争请求创建的记录。
        with transaction.atomic():
            batch = batch_manager.create(
                **lookup,
                request_fingerprint=request_fingerprint,
                target_parent_id=target_parent_id,
            )
    except IntegrityError as exc:
        try:
            competing_batch = batch_manager.get(**lookup)
        except MarkdownImportBatch.DoesNotExist:
            # 无法读取同键记录时，异常可能来自其他约束或数据库故障，必须保留原始错误。
            raise exc
        return _claim_existing(competing_batch, request_fingerprint)

    return BatchClaim(batch=batch, created=True)
