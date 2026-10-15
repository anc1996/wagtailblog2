"""D3 §7 持续扫描内核：无 Django/SQL/ES 连接，无 HTTP 或 UI 行为。

接入顺序：
1. 来源负责完整 Scope、排他分类、稳定快照及源内排序，绝不使用漂移 offset。
2. validator 每次验证权限/版本/正文身份，返回安全展示值或 None；服务故障抛出。
3. create 生成随机检查点句柄，page 以 CAS 租约一次性推进、保存候选而非最终 JSON。

两个源的 PIT 不是跨存储原子快照；结果在每次响应前复验，不能据此宣称
SQL/Mongo/ES 全局原子性。默认工作量参数只是保守初值，未经过真实压测。
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
from typing import Any, Callable, Mapping, Protocol

from .session_store import (
    RedisSessionStore,
    SearchSessionStoreUnavailable,
    SessionStore,
    StoredSession,
)


RRF_K = 60
_VERSION = 1
_HANDLE = re.compile(r"^[A-Za-z0-9_-]{32}$")
logger = logging.getLogger(__name__)


class SearchSessionError(RuntimeError):
    code = "search_session_error"

    def __init__(self) -> None:
        super().__init__(self.code)


class SearchSessionTokenError(SearchSessionError):
    code = "cursor_invalid"


class SearchSessionBindingError(SearchSessionError):
    code = "cursor_binding_mismatch"


class SearchSessionExpired(SearchSessionError):
    code = "cursor_expired"


class SearchSessionBusy(SearchSessionError):
    """调用方可重试同一个句柄；不能为竞争请求另开一个推进分支。"""

    code = "cursor_busy"


class SearchSessionSourceError(SearchSessionError):
    code = "session_source_unavailable"


class SearchSessionCapacityError(SearchSessionError):
    code = "session_capacity_exceeded"


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("first_published_at requires an aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Candidate:
    """payload 为 JSON 可序列化候选/版本引用；不要携带已渲染的安全结果缓存。"""

    page_id: int
    first_published_at: datetime | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchState:
    """仅服务端保存；null 位置只表示初次查询，耗尽另由 SourceState 记录。"""

    search_after: tuple[Any, ...] | None = None
    pit_id: str | None = None


@dataclass(frozen=True)
class FetchBatch:
    candidates: tuple[Candidate, ...]
    state: FetchState
    exhausted: bool


class CandidateSource(Protocol):
    """同一 state 可安全重试；候选必须按 binding 指定的全局源内键有序。

    date/-date 使用 first_published_at UTC、缺值最后、PK 升序打平；
    relevance 保持源排名。跨源 Page 归属排他由注册表/查询适配器保证。
    """

    def fetch(self, state: FetchState, batch_size: int) -> FetchBatch: ...


@dataclass(frozen=True)
class SessionBinding:
    """scope 必须是完整 SearchScope 的 JSON 快照，不能只提供 LANGUAGE_CODE。

    站点时区的用户日期应由入口先转换为 aware start_at/end_at，使用 [start,end)。
    index_epochs/source_snapshots 必须逐来源提供，来源顺序也进入绑定摘要。
    """

    query: str
    scope: Mapping[str, Any]
    page_size: int
    registry_epoch: str
    index_epochs: Mapping[str, str]
    source_snapshots: Mapping[str, str]
    order_by: str = "relevance"
    start_at: datetime | None = None
    end_at: datetime | None = None


@dataclass(frozen=True)
class SessionConfig:
    batch_size: int = 32
    max_fetches: int = 16
    max_scan_candidates: int = 256
    scan_seconds: float = 2.0
    ttl_seconds: float = 120.0
    lease_seconds: float = 15.0
    max_checkpoints: int = 4
    max_state_bytes: int = 2 * 1024 * 1024
    max_page_size: int = 100

    def __post_init__(self) -> None:
        for value in (
            self.batch_size, self.max_fetches, self.max_scan_candidates,
            self.max_checkpoints, self.max_state_bytes, self.max_page_size,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("session work limits must be positive integers")
        if self.max_checkpoints < 2:
            raise ValueError("at least input and next checkpoint must be retained")
        for value in (self.scan_seconds, self.ttl_seconds, self.lease_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("session time limits must be positive")


@dataclass(frozen=True)
class SessionPage:
    results: tuple[Mapping[str, Any], ...]
    has_more: bool | None
    next_cursor: str | None
    partial: bool
    continuation_kind: str | None
    total: None = None
    total_relation: str = "unknown"

    @property
    def returned_count(self) -> int:
        return len(self.results)


class SessionTokenSigner:
    """标准库 HMAC，不依赖 Django settings；token 中只有三个不透明声明。

    注入独立签名密钥，不能记录密钥/token。到期由服务端绝对 TTL 和 epoch 控制，
    不把 PIT、排序、候选、查询字符串或正文编码到客户端。
    """

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("session signing key must contain at least 32 bytes")
        self._key = key

    def encode(self, session_id: str, checkpoint_id: str, binding_hash: str) -> str:
        payload = _json({
            "session_id": session_id, "checkpoint_id": checkpoint_id,
            "binding_hash": binding_hash,
        }).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.digest(self._key, b"search.session.v1:" + encoded, "sha256")
        return encoded.decode("ascii") + "." + base64.urlsafe_b64encode(
            signature
        ).rstrip(b"=").decode("ascii")

    def decode(self, token: str) -> dict[str, str]:
        try:
            if not isinstance(token, str) or len(token) > 1024:
                raise ValueError
            encoded, signature = token.encode("ascii").split(b".")
            expected = hmac.digest(self._key, b"search.session.v1:" + encoded, "sha256")
            actual = base64.b64decode(signature + b"=" * (-len(signature) % 4), altchars=b"-_", validate=True)
            if not hmac.compare_digest(actual, expected):
                raise ValueError
            data = json.loads(base64.b64decode(
                encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            ))
            if not isinstance(data, dict) or set(data) != {
                "session_id", "checkpoint_id", "binding_hash"
            }:
                raise ValueError
            if any(
                not isinstance(data[name], str) or not _HANDLE.fullmatch(data[name])
                for name in ("session_id", "checkpoint_id")
            ):
                raise ValueError
            if not isinstance(data["binding_hash"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", data["binding_hash"]
            ):
                raise ValueError
            return data
        except (ValueError, TypeError, UnicodeError) as error:
            raise SearchSessionTokenError() from error


def _candidate_data(candidate: Candidate) -> dict[str, Any]:
    try:
        if not isinstance(candidate, Candidate) or type(candidate.page_id) is not int or candidate.page_id <= 0:
            raise ValueError
        date = _utc(candidate.first_published_at)
        # JSON 往返固定值对象，隔离来源或 validator 对 payload 的原地修改。
        return json.loads(_json({
            "page_id": candidate.page_id,
            "first_published_at": date.isoformat() if date else None,
            "payload": dict(candidate.payload),
        }))
    except (ValueError, TypeError, OverflowError) as error:
        raise SearchSessionSourceError() from error


def _candidate(data: dict[str, Any]) -> Candidate:
    date = data["first_published_at"]
    return Candidate(
        data["page_id"], datetime.fromisoformat(date) if date else None,
        deepcopy(data["payload"]),
    )


def _position_data(position: FetchState) -> dict[str, Any]:
    if not isinstance(position, FetchState):
        raise SearchSessionSourceError()
    if position.search_after is not None and (
        not isinstance(position.search_after, tuple) or not position.search_after
    ):
        raise SearchSessionSourceError()
    if position.pit_id is not None and not isinstance(position.pit_id, str):
        raise SearchSessionSourceError()
    try:
        return json.loads(_json({
            "search_after": position.search_after, "pit_id": position.pit_id
        }))
    except (ValueError, TypeError) as error:
        raise SearchSessionSourceError() from error


def _position(data: dict[str, Any]) -> FetchState:
    value = data["search_after"]
    return FetchState(tuple(value) if value is not None else None, data["pit_id"])


class SearchSessionEngine:
    """有界扫描，不限制会话总结果数；所有活动源具备安全队首后才选择。

    会话 CAS 租约在任何 fetch 前领取，提交检查点仍带版本围栏。持有者崩溃后
    租约到期可从原输入检查点重试；适配器必须保证快照 fetch 的可重试性。
    """

    def __init__(
        self, *, sources: Mapping[str, CandidateSource],
        validator: Callable[[Candidate], Mapping[str, Any] | None],
        store: SessionStore, signer: SessionTokenSigner,
        config: SessionConfig | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not sources or any(not isinstance(key, str) or not key for key in sources):
            raise ValueError("named sources are required")
        self.sources = dict(sources)
        self.validator = validator
        self.store = store
        self.signer = signer
        self.config = config or SessionConfig()
        self.clock = clock
        self.monotonic = monotonic

    def _binding_data(self, binding: SessionBinding) -> dict[str, Any]:
        if (
            not isinstance(binding.query, str) or not binding.scope
            or binding.order_by not in ("relevance", "date", "-date")
            or type(binding.page_size) is not int
            or not 1 <= binding.page_size <= self.config.max_page_size
            or not binding.registry_epoch
            or set(binding.index_epochs) != set(self.sources)
            or set(binding.source_snapshots) != set(self.sources)
            or any(not value for value in binding.index_epochs.values())
            or any(not value for value in binding.source_snapshots.values())
        ):
            raise ValueError("incomplete session binding")
        start, end = _utc(binding.start_at), _utc(binding.end_at)
        if start is not None and end is not None and start >= end:
            raise ValueError("date range must be nonempty")
        return json.loads(_json({
            "query": binding.query, "scope": dict(binding.scope),
            "order_by": binding.order_by, "page_size": binding.page_size,
            "start_at": start.isoformat() if start else None,
            "end_at": end.isoformat() if end else None,
            "registry_epoch": binding.registry_epoch,
            "index_epochs": dict(binding.index_epochs),
            "source_snapshots": dict(binding.source_snapshots),
            "source_order": list(self.sources),
        }))

    @staticmethod
    def _binding_hash(data: dict[str, Any]) -> str:
        return hashlib.sha256(_json(data).encode("utf-8")).hexdigest()

    def _capacity(self, document: dict[str, Any]) -> None:
        if len(_json(document).encode("utf-8")) > self.config.max_state_bytes:
            raise SearchSessionCapacityError()

    def create(
        self, binding: SessionBinding,
        initial_states: Mapping[str, FetchState] | None = None,
    ) -> str:
        data = self._binding_data(binding)
        if initial_states is None:
            initial_states = {name: FetchState() for name in self.sources}
        if set(initial_states) != set(self.sources):
            raise ValueError("initial states must match sources")
        checkpoint_id = secrets.token_urlsafe(24)
        digest = self._binding_hash(data)
        document = {
            "schema": _VERSION, "binding": data, "binding_hash": digest,
            "expires_at": self.clock() + self.config.ttl_seconds,
            "lease": None,
            "checkpoint_order": [checkpoint_id],
            "checkpoints": {checkpoint_id: {
                "state": {name: {
                    "position": _position_data(initial_states[name]),
                    "rank": 0, "buffer": [], "exhausted": False,
                    "last_candidate": None,
                } for name in self.sources},
                "plan": None,
            }},
        }
        self._capacity(document)
        for _ in range(3):
            session_id = secrets.token_urlsafe(24)
            if self.store.create(session_id, document, self.config.ttl_seconds):
                return self.signer.encode(session_id, checkpoint_id, digest)
        raise SearchSessionStoreUnavailable()

    def _read(
        self, claims: dict[str, str], binding: SessionBinding
    ) -> StoredSession:
        row = self.store.read(claims["session_id"])
        if row is None or row.document["expires_at"] <= self.clock():
            raise SearchSessionExpired()
        document = row.document
        if document.get("schema") != _VERSION:
            raise SearchSessionExpired()
        if claims["binding_hash"] != document["binding_hash"]:
            raise SearchSessionBindingError()
        data = self._binding_data(binding)
        if self._binding_hash(data) != document["binding_hash"]:
            old = document["binding"]
            if any(data[key] != old[key] for key in ("registry_epoch", "index_epochs", "source_order")):
                raise SearchSessionExpired()
            raise SearchSessionBindingError()
        if claims["checkpoint_id"] not in document["checkpoints"]:
            raise SearchSessionExpired()
        return row

    def page(self, token: str, binding: SessionBinding) -> SessionPage:
        """同游标重放只复验已存候选；不会补齐被撤权的旧页或再次 fetch。"""
        claims = self.signer.decode(token)
        row = self._read(claims, binding)
        expires_at = row.document["expires_at"]
        checkpoint_id = claims["checkpoint_id"]
        plan = row.document["checkpoints"][checkpoint_id]["plan"]
        if plan is not None:
            return self._render(plan, claims, expires_at)
        lease = row.document["lease"]
        if lease is not None and lease["until"] > self.clock():
            raise SearchSessionBusy()
        document = deepcopy(row.document)
        owner = secrets.token_urlsafe(24)
        lease_until = min(
            self.clock() + self.config.lease_seconds, document["expires_at"]
        )
        document["lease"] = {"owner": owner, "until": lease_until}
        if not self.store.compare_and_swap(
            claims["session_id"], row.version, document,
            document["expires_at"] - self.clock(),
        ):
            # CAS 竞争胜方可能已经完成；只允许读回同一不可变页。
            fresh = self._read(claims, binding)
            plan = fresh.document["checkpoints"][checkpoint_id]["plan"]
            if plan is not None:
                return self._render(plan, claims, expires_at)
            raise SearchSessionBusy()
        version = row.version + 1
        try:
            state = deepcopy(document["checkpoints"][checkpoint_id]["state"])
            plan = self._scan(state, binding)
            if self.clock() >= document["expires_at"]:
                raise SearchSessionExpired()
            if self.clock() >= lease_until:
                raise SearchSessionBusy()
            if plan["has_more"] is not False:
                next_id = secrets.token_urlsafe(24)
                plan["next_checkpoint_id"] = next_id
                document["checkpoints"][next_id] = {"state": state, "plan": None}
                document["checkpoint_order"].append(next_id)
            else:
                plan["next_checkpoint_id"] = None
            document["checkpoints"][checkpoint_id]["plan"] = plan
            while len(document["checkpoints"]) > self.config.max_checkpoints:
                # JSON object 顺序不是存储契约；随机句柄也不能作为时间顺序。
                del document["checkpoints"][document["checkpoint_order"].pop(0)]
            document["lease"] = None
            self._capacity(document)
            if not self.store.compare_and_swap(
                claims["session_id"], version, document,
                document["expires_at"] - self.clock(),
            ):
                fresh = self._read(claims, binding)
                other = fresh.document["checkpoints"][checkpoint_id]["plan"]
                if other is not None:
                    return self._render(other, claims, expires_at)
                raise SearchSessionBusy()
        except Exception:
            # 失败只释放自己的租约，恢复原始检查点；不删除任何源快照/正文。
            abandoned = deepcopy(row.document)
            abandoned["lease"] = None
            remaining = abandoned["expires_at"] - self.clock()
            if remaining > 0:
                try:
                    self.store.compare_and_swap(
                        claims["session_id"], version, abandoned, remaining
                    )
                except Exception:
                    logger.warning("search_session_lease_release_failed")
            raise
        return self._render(plan, claims, expires_at)

    def _sort_key(
        self, entry: dict[str, Any], source_order: int, order_by: str
    ) -> tuple[Any, ...]:
        candidate = _candidate(entry["candidate"])
        if order_by == "relevance":
            return (-Fraction(1, RRF_K + entry["rank"]), source_order, candidate.page_id)
        date = candidate.first_published_at
        if date is None:
            return (1, 0, source_order, candidate.page_id)
        # 整数微秒避免平台 timestamp 浮点精度造成日期 tie 错判。
        delta = date - datetime(1970, 1, 1, tzinfo=timezone.utc)
        micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        return (0, -micros if order_by == "-date" else micros, source_order, candidate.page_id)

    @staticmethod
    def _in_range(candidate: Candidate, binding: SessionBinding) -> bool:
        if binding.start_at is None and binding.end_at is None:
            return True
        date = candidate.first_published_at
        return date is not None and (
            binding.start_at is None or date >= binding.start_at
        ) and (binding.end_at is None or date < binding.end_at)

    def _validate(self, candidate: Candidate) -> Mapping[str, Any] | None:
        result = self.validator(candidate)
        if result is not None and not isinstance(result, Mapping):
            raise TypeError("validator must return a safe mapping or None")
        return result

    def _fetch(
        self, name: str, state: dict[str, Any], size: int, order_by: str
    ) -> int:
        old_position = deepcopy(state["position"])
        try:
            batch = self.sources[name].fetch(_position(old_position), size)
        except SearchSessionExpired:
            # 源快照失效必须显式重启，不能当作普通临时故障或偷偷重建 PIT。
            raise
        except Exception as error:
            raise SearchSessionSourceError() from error
        if (
            not isinstance(batch, FetchBatch) or type(batch.exhausted) is not bool
            or not isinstance(batch.candidates, (tuple, list))
            or len(batch.candidates) > size
        ):
            raise SearchSessionSourceError()
        position = _position_data(batch.state)
        if not batch.exhausted and (
            position == old_position or position["search_after"] is None
        ):
            raise SearchSessionSourceError()
        entries = []
        for candidate in batch.candidates:
            entry = {
                "candidate": _candidate_data(candidate), "rank": state["rank"] + 1
            }
            previous = state["last_candidate"]
            if previous is not None and self._sort_key(entry, 0, order_by) < self._sort_key(previous, 0, order_by):
                raise SearchSessionSourceError()
            state["rank"] += 1
            state["last_candidate"] = entry
            entries.append(entry)
        state["position"] = position
        state["buffer"].extend(entries)
        state["exhausted"] = batch.exhausted
        return len(entries)

    def _scan(
        self, state: dict[str, Any], binding: SessionBinding
    ) -> dict[str, Any]:
        selected = []
        fetched = 0
        scanned = 0
        deadline = self.monotonic() + self.config.scan_seconds
        # 仅限本次响应使用的合格判定；不会保存安全展示值或在下次请求复用。
        safe: set[tuple[str, int]] = set()
        lookahead = None
        has_more = None
        while True:
            heads = []
            missing = []
            for order, name in enumerate(self.sources):
                source_state = state[name]
                buffer = source_state["buffer"]
                while buffer:
                    if self.monotonic() >= deadline:
                        break
                    entry = buffer[0]
                    identity = (name, entry["rank"])
                    if identity not in safe:
                        candidate = _candidate(entry["candidate"])
                        if not self._in_range(candidate, binding) or self._validate(candidate) is None:
                            buffer.pop(0)
                            continue
                        safe.add(identity)
                    heads.append((self._sort_key(entry, order, binding.order_by), name, entry))
                    break
                if not buffer and not source_state["exhausted"]:
                    missing.append(name)
            if self.monotonic() >= deadline:
                break
            if missing:
                advanced = False
                # 每轮各源最多一批，避免过滤密集来源独占一轮全部预算。
                for name in missing:
                    if (
                        fetched >= self.config.max_fetches
                        or scanned >= self.config.max_scan_candidates
                        or self.monotonic() >= deadline
                    ):
                        break
                    size = min(
                        self.config.batch_size, self.config.max_scan_candidates - scanned
                    )
                    scanned += self._fetch(name, state[name], size, binding.order_by)
                    fetched += 1
                    advanced = True
                if not advanced:
                    break
                continue
            if not heads:
                has_more = False
                break
            _, name, entry = min(heads, key=lambda item: item[0])
            if len(selected) == binding.page_size:
                lookahead = entry["candidate"]
                has_more = True
                break
            selected.append(entry["candidate"])
            state[name]["buffer"].pop(0)
        return {
            "candidates": selected, "lookahead": lookahead,
            "has_more": has_more, "page_size": binding.page_size,
        }

    def _render(
        self, plan: dict[str, Any], claims: dict[str, str], expires_at: float
    ) -> SessionPage:
        # 检查点保存候选，不保存 validator 结果；重复请求也执行即时范围/版本校验。
        results = tuple(
            result for data in plan["candidates"]
            if (result := self._validate(_candidate(data))) is not None
        )
        has_more = plan["has_more"]
        if has_more is True and self._validate(_candidate(plan["lookahead"])) is None:
            has_more = None
        next_id = plan["next_checkpoint_id"]
        next_cursor = self.signer.encode(
            claims["session_id"], next_id, claims["binding_hash"]
        ) if next_id is not None else None
        partial = has_more is None or len(results) < len(plan["candidates"])
        if self.clock() >= expires_at:
            raise SearchSessionExpired()
        return SessionPage(
            results, has_more, next_cursor, partial,
            "scan" if has_more is None else ("results" if has_more else None),
        )
