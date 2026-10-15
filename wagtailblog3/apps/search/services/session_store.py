"""短期搜索会话的存储协议；只注入 Redis 客户端，不读取连接配置。

版本 CAS 同时用于领取扫描租约和提交检查点。业务候选与正文不进入日志；
此处的 Lua 原子性必须另经真实 Redis 验收，L0 替身不证明服务能力。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Protocol


class SearchSessionStoreUnavailable(RuntimeError):
    """存储故障必须关闭本次查询，不能回退进程内缓存。"""

    code = "session_store_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class StoredSession:
    version: int
    document: dict[str, Any]


class SessionStore(Protocol):
    """read 必须返回独立副本；CAS 失败/过期不得创建或覆盖会话。"""

    def create(
        self, session_id: str, document: dict[str, Any], ttl_seconds: float
    ) -> bool: ...

    def read(self, session_id: str) -> StoredSession | None: ...

    def compare_and_swap(
        self, session_id: str, version: int, document: dict[str, Any], ttl_seconds: float
    ) -> bool: ...


_CREATE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
redis.call('HSET', KEYS[1], 'version', '0', 'document', ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return 1
"""

_CAS = """
local current = redis.call('HGET', KEYS[1], 'version')
if not current or current ~= ARGV[1] then return 0 end
local remaining = redis.call('PTTL', KEYS[1])
if remaining <= 0 then return 0 end
redis.call('HSET', KEYS[1], 'version', ARGV[2], 'document', ARGV[3])
redis.call('PEXPIRE', KEYS[1], math.min(remaining, tonumber(ARGV[4])))
return 1
"""


class RedisSessionStore:
    """单 key Lua CAS，兼容 decode_responses 开关，不执行外部连接初始化。

    客户端应由主入口注入专用 Redis 命名空间。CAS 只缩短剩余 TTL，
    不因持续翻页延长源快照寿命，也不在过期后悄悄重新建会话。
    """

    def __init__(self, client: Any, key_prefix: str = "search:session:v1:") -> None:
        self.client = client
        self.key_prefix = key_prefix

    @staticmethod
    def _dump(document: dict[str, Any]) -> str:
        return json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _ttl(seconds: float) -> int:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("session TTL must be positive")
        return max(1, math.ceil(seconds * 1000))

    def create(
        self, session_id: str, document: dict[str, Any], ttl_seconds: float
    ) -> bool:
        try:
            return bool(self.client.eval(
                _CREATE, 1, self.key_prefix + session_id,
                self._dump(document), self._ttl(ttl_seconds),
            ))
        except Exception as error:
            raise SearchSessionStoreUnavailable() from error

    def read(self, session_id: str) -> StoredSession | None:
        try:
            row = self.client.hgetall(self.key_prefix + session_id)
            if not row:
                return None
            row = {
                key.decode("utf-8") if isinstance(key, bytes) else key: value
                for key, value in row.items()
            }
            document = json.loads(row["document"])
            version = int(row["version"])
            if not isinstance(document, dict) or version < 0:
                raise ValueError("invalid session record")
            return StoredSession(version, document)
        except Exception as error:
            raise SearchSessionStoreUnavailable() from error

    def compare_and_swap(
        self, session_id: str, version: int, document: dict[str, Any], ttl_seconds: float
    ) -> bool:
        try:
            return bool(self.client.eval(
                _CAS, 1, self.key_prefix + session_id,
                str(version), str(version + 1), self._dump(document),
                self._ttl(ttl_seconds),
            ))
        except Exception as error:
            raise SearchSessionStoreUnavailable() from error
