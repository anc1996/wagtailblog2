"""单作者短窗口防重；只登记最小身份，不提供持久 exactly-once 承诺。"""

import hashlib
import json
import math
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from django.core import signing


class RecordStore(Protocol):
    """权威单键原子存储边界。"""

    def create(self, identity: str, record: dict, now: float) -> bool: ...
    def read(self, identity: str) -> dict | None: ...
    def cas(self, identity: str, expected: dict, replacement: dict, now: float) -> bool: ...


@dataclass(frozen=True)
class Claim:
    """领取快照也是 CAS 证据；旧 owner 不得写回新领取的结果。"""

    identity: str
    record: dict
    replay: bool = False


class GuardError(RuntimeError):
    """仅携带安全错误码，不包含签发载体或请求正文。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class IdempotencyService:
    """预留身份不可执行；实际参数准备时才开始 30 分钟执行窗口。"""

    def __init__(self, store: RecordStore, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock
        self.signer = signing.Signer(salt="external-content.request.v1")

    def _sign(self, identity: str, user_id: int, scope: dict, purpose: str,
              binding: str = "") -> str:
        return self.signer.sign_object({
            "id": identity, "user": user_id, "scope": scope,
            "purpose": purpose, "binding": binding,
        })

    def _load(self, token: str, user_id: int, purpose: str,
              scope: dict | None = None) -> tuple[str, dict, dict]:
        try:
            payload = self.signer.unsign_object(token)
            if (not isinstance(payload, dict) or payload["purpose"] != purpose
                    or type(payload["user"]) is not int
                    or not self._valid_scope(payload["scope"])
                    or not isinstance(payload["binding"], str)):
                raise ValueError
            identity = payload["id"]
            if not isinstance(identity, str) or not re.fullmatch("[0-9a-f]{32}", identity):
                raise ValueError
        except (signing.BadSignature, ValueError, KeyError, TypeError):
            raise GuardError("token") from None
        if payload["user"] != user_id or (scope is not None and payload["scope"] != scope):
            raise GuardError("binding")
        record = self.store.read(identity)
        self._validate_record(record)
        if self.clock() >= record["retain_until"]:
            raise GuardError("unknown")
        if record["user"] != user_id or record["scope"] != payload["scope"]:
            raise GuardError("binding")
        if purpose == "execute" and payload["binding"] != record["binding"]:
            raise GuardError("binding")
        return identity, record, payload

    @staticmethod
    def _valid_scope(scope: object) -> bool:
        """缓存/签名载体仍须结构校验，不把损坏身份用于权限查询。"""
        if not isinstance(scope, dict) or not isinstance(scope.get("kind"), str):
            return False
        if scope.get("kind") == "create":
            return (type(scope.get("parent")) is int and scope["parent"] > 0
                    and isinstance(scope.get("model"), str)
                    and re.fullmatch(r"\w+\.\w+", scope["model"]) is not None)
        return (scope.get("kind") in {"edit", "copy", "delete", "unpublish", "convert_alias"}
                and type(scope.get("page")) is int and scope["page"] > 0)

    @classmethod
    def _validate_record(cls, record: object) -> None:
        """丢失/损坏记录仅待核对；所有消费路径（含 CAS 复读）共享此边界。"""
        if not isinstance(record, dict):
            raise GuardError("unknown")
        numeric_fields = ("retain_until", "reservation_deadline", "token_deadline",
                          "processing_deadline")
        valid = (
            type(record.get("schema")) is int and record["schema"] == 1
            and type(record.get("user")) is int
            and cls._valid_scope(record.get("scope"))
            and isinstance(record.get("state"), str)
            and record["state"] in {"reserved", "issued", "processing", "succeeded",
                                       "failed", "unknown"}
            and type(record.get("version")) is int and record["version"] > 0
            and isinstance(record.get("binding"), str)
            and isinstance(record.get("owner"), str)
            and all(type(record.get(key)) in (int, float)
                    and math.isfinite(record[key]) for key in numeric_fields)
            and "issued_at" in record and "result" in record
            and (record["result"] is None or isinstance(record["result"], dict))
            and cls.valid_authorization(
                record.get("authorization"),
                copy_parent_required=(record["scope"]["kind"] == "copy"
                                      and record["state"] != "reserved"))
        )
        if not valid:
            raise GuardError("unknown")
        if record["state"] == "reserved":
            valid = record["issued_at"] is None
        else:
            valid = (type(record["issued_at"]) in (int, float)
                     and math.isfinite(record["issued_at"])
                     and re.fullmatch("[0-9a-f]{64}", record["binding"]) is not None)
        if record["state"] == "succeeded":
            result = record["result"]
            valid = valid and cls.valid_result(result)
        if not valid:
            raise GuardError("unknown")

    @staticmethod
    def valid_authorization(value: object, *, copy_parent_required: bool = False) -> bool:
        """复制执行记录必须保留目标；缺失时不得回退源父页降级授权。"""
        return (isinstance(value, dict) and type(value.get("publish")) is bool
                and set(value) <= {"publish", "copy_parent"}
                and (not copy_parent_required or "copy_parent" in value)
                and ("copy_parent" not in value
                     or (type(value["copy_parent"]) is int and value["copy_parent"] > 0)))

    @staticmethod
    def valid_result(result: object) -> bool:
        """成功结果只允许可复验的身份；JSON 身份损坏不能进入渲染/重放分支。"""
        if (not isinstance(result, dict) or type(result.get("page")) is not int
                or result["page"] <= 0 or type(result.get("json", False)) is not bool):
            return False
        if not result.get("json"):
            return True
        updates = result.get("field_updates")
        return (type(result.get("revision")) is int and result["revision"] > 0
                and type(result.get("created")) is bool and isinstance(updates, dict)
                and all(isinstance(key, str) and isinstance(value, str)
                        and (not value or value.isdecimal()) for key, value in updates.items()))

    def reserve(self, user_id: int, scope: dict) -> str:
        """GET 预留身份防止重复准备在丢键后复活，尚无可执行业务 Token。"""
        now = self.clock()
        identity = secrets.token_hex(16)
        record = {
            "schema": 1, "user": user_id, "scope": scope, "state": "reserved",
            "binding": "", "owner": "", "version": 1, "issued_at": None,
            "token_deadline": 0, "processing_deadline": 0,
            "reservation_deadline": now + 86400,
            # 非执行预留最长 48h；准备时缩短至精确的签发后 24h，绝不续期。
            "retain_until": now + 172800, "result": None,
            "authorization": {"publish": False},
        }
        if not self.store.create(identity, record, now):
            raise GuardError("unknown")
        return self._sign(identity, user_id, scope, "reserve")

    @staticmethod
    def _binding(user_id: int, scope: dict, digest: str) -> str:
        encoded = json.dumps([user_id, scope, digest], sort_keys=True,
                             ensure_ascii=False, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def prepare(self, ticket: str, user_id: int, scope: dict, digest: str, *,
                authorization: dict | None = None) -> str:
        """同一预留只能绑定一次；记录消失时拒绝而非重新签发。"""
        identity, record, _ = self._load(ticket, user_id, "reserve", scope)
        now = self.clock()
        authorization = {"publish": False} if authorization is None else authorization
        if not self.valid_authorization(authorization, copy_parent_required=scope["kind"] == "copy"):
            raise GuardError("binding")
        binding = self._binding(user_id, scope, digest)
        if record["state"] == "reserved":
            if now >= record["reservation_deadline"]:
                raise GuardError("expired")
            updated = dict(record, state="issued", binding=binding, version=2,
                           issued_at=now, token_deadline=now + 1800,
                           retain_until=now + 86400, authorization=dict(authorization))
            if not self.store.cas(identity, record, updated, now):
                # 一次有界复读覆盖准备连击，不以 CAS 失败重新创建记录。
                record = self.store.read(identity)
                self._validate_record(record)
            else:
                record = updated
        if record["binding"] != binding or record["authorization"] != authorization:
            raise GuardError("binding")
        if record["state"] == "reserved":
            raise GuardError("unknown")
        if now >= record["token_deadline"]:
            raise GuardError("expired")
        return self._sign(identity, user_id, scope, "execute", binding)

    def _timeout(self, identity: str, record: dict) -> dict:
        if record["state"] == "processing" and self.clock() >= record["processing_deadline"]:
            updated = dict(record, state="unknown", version=record["version"] + 1)
            if self.store.cas(identity, record, updated, self.clock()):
                return updated
            raise GuardError("unknown")
        return record

    def claim(self, token: str, user_id: int, scope: dict, digest: str) -> Claim:
        """短时独占领取；processing 超时只待核对，不接管。"""
        identity, record, _ = self._load(token, user_id, "execute", scope)
        if record["binding"] != self._binding(user_id, scope, digest):
            raise GuardError("binding")
        record = self._timeout(identity, record)
        now = self.clock()
        if now >= record["token_deadline"]:
            raise GuardError("expired")
        if record["state"] == "succeeded":
            return Claim(identity, record, replay=True)
        if record["state"] not in {"issued", "failed"}:
            raise GuardError(record["state"] if record["state"] == "processing" else "unknown")
        updated = dict(record, state="processing", owner=secrets.token_hex(16),
                       version=record["version"] + 1, processing_deadline=now + 300)
        if not self.store.cas(identity, record, updated, now):
            raise GuardError("processing")
        return Claim(identity, updated)

    def _finish(self, claim: Claim, state: str, result: dict | None = None) -> bool:
        """提交之后的缓存故障不得改变已提交 SQL/Mongo 业务结果。"""
        if claim.replay:
            return False
        now = self.clock()
        if now >= claim.record["processing_deadline"]:
            state, result = "unknown", None
        updated = dict(claim.record, state=state, result=result,
                       version=claim.record["version"] + 1)
        try:
            changed = self.store.cas(claim.identity, claim.record, updated, now)
        except Exception:
            return False
        return changed and state != "unknown"

    def complete(self, claim: Claim, result: dict) -> bool:
        """调用方只能在最终 SQL on_commit 中登记完成。"""
        return self._finish(claim, "succeeded", result)

    def fail(self, claim: Claim, *, rollback_proven: bool = False) -> bool:
        """只有整体受控回滚有证据时才允许使用新 owner 重试。"""
        return self._finish(claim, "failed" if rollback_proven else "unknown")

    def status(self, token: str, user_id: int) -> dict:
        """仅核对，不领取；状态入口仍需调用方复核当前权限。"""
        identity, record, _ = self._load(token, user_id, "execute")
        record = self._timeout(identity, record)
        return {"state": record["state"], "scope": record["scope"], "result": record["result"],
                "authorization": record["authorization"]}
