"""基于 Redis 的请求幂等存储适配器实现。

守住的冻结契约：
1. 默认/非 Redis 缓存后端严格 Fail-Closed (抛出 StoreUnavailable)，禁止在 LocMem 等非真实分布式缓存下运行；
2. 32 位小写 16 进制身份标识严格校验 (^[0-9a-f]{32}$)，不合法抛出 ValueError；
3. 单次操作 cache.make_key 严格仅调用一次，并通过 cache.client.get_client(write=True) 直连主库权威写客户端；
4. 规范化 JSON 编码：sort_keys=True、紧凑 separators=(',', ':')、allow_nan=False、禁止 NaN/Infinity；
5. create: NX PX 剩余 retain_until 毫秒数原子写入，已存在或已超时拒绝；
6. read: 直连主库读取并反序列化，损坏或非 dict 数据 Fail-Closed (StoreUnavailable)；
7. cas: 单键 Lua 脚本比对完整 canonical expected 记录，PTTL>0 约束，min(pttl, remaining) 约束，禁止复活丢失键，禁止延长物理过期；
   Redis TIME 在原子落点复验已有 deadline；调用方时间只能进一步收紧截止，不能放宽服务器时间。
8. 专项状态细化 (Integration refinement)：
   - 仅且仅当 expected['state'] == 'reserved' 且 replacement['state'] == 'issued' 时允许缩短 retain_until；
   - 严禁延长 retain_until；
   - 其他状态转移下 retain_until 必须与 expected 完全一致，否则拒绝。
"""

import json
import math
import re
from typing import Any

from django.core.cache import caches
from django_redis.cache import RedisCache

_IDENTITY_REGEX = re.compile(r"^[0-9a-f]{32}$")

_CAS_LUA_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if not current or current ~= ARGV[1] then
    return 0
end
local pttl = redis.call('PTTL', KEYS[1])
if pttl <= 0 then
    return 0
end
local remaining = tonumber(ARGV[3])
if not remaining or remaining <= 0 then
    return 0
end
local function finite_number(value)
    return type(value) == 'number' and value == value
        and value ~= math.huge and value ~= -math.huge
end
local caller_now = tonumber(ARGV[4])
if not finite_number(caller_now) then
    return 0
end
local server_time = redis.call('TIME')
local server_now = tonumber(server_time[1]) + tonumber(server_time[2]) / 1000000
-- 慢客户端不能放行服务器已过期的操作；快客户端只会保守阻断，不会续期。
local now = math.max(server_now, caller_now)
local expected_ok, expected = pcall(cjson.decode, current)
local replacement_ok, replacement = pcall(cjson.decode, ARGV[2])
if not expected_ok or not replacement_ok
    or type(expected) ~= 'table' or type(replacement) ~= 'table' then
    return 0
end
local function live(deadline)
    return finite_number(deadline) and now < deadline
end
if not finite_number(expected.version) or not finite_number(replacement.version)
    or replacement.version ~= expected.version + 1 then
    return 0
end
if not live(expected.retain_until) or not live(replacement.retain_until)
    or replacement.retain_until > expected.retain_until then
    return 0
end
if replacement.retain_until < expected.retain_until
    and not (expected.state == 'reserved' and replacement.state == 'issued') then
    return 0
end
if expected.state == 'reserved' and replacement.state == 'issued' then
    if not live(expected.reservation_deadline) or not live(replacement.token_deadline) then
        return 0
    end
elseif (expected.state == 'issued' or expected.state == 'failed')
    and replacement.state == 'processing' then
    if not live(expected.token_deadline) or not live(replacement.processing_deadline) then
        return 0
    end
elseif expected.state == 'processing' then
    -- processing 不允许续租或转交 owner；超时只能显式转 unknown，不能改写为可重试。
    if replacement.state ~= 'succeeded' and replacement.state ~= 'failed'
        and replacement.state ~= 'unknown' then
        return 0
    end
    if replacement.owner ~= expected.owner or not finite_number(expected.processing_deadline) then
        return 0
    end
    if now >= expected.processing_deadline and replacement.state ~= 'unknown' then
        return 0
    end
end
-- 向下取整，不足 1ms 就拒绝；客户端剩余 TTL 仅作更严格的上界。
local server_remaining = math.floor((replacement.retain_until - now) * 1000)
local new_ttl = math.min(pttl, remaining, server_remaining)
if new_ttl <= 0 then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'PX', new_ttl)
return 1
""".strip()


class StoreUnavailable(RuntimeError):
    """存储后端不可用或发生底层异常时的 Fail-Closed 错误。"""


def _validate_identity(identity: Any) -> str:
    """校验 32 位小写 16 进制字符串身份标识。"""
    if not isinstance(identity, str) or not _IDENTITY_REGEX.fullmatch(identity):
        raise ValueError("Identity must be a 32-character lowercase hexadecimal string")
    return identity


def _validate_cache_backend(cache: Any) -> None:
    """Fail-Closed 校验缓存后端：必须为 django-redis 的 RedisCache 实例且支持权威写客户端。"""
    if cache is None:
        raise StoreUnavailable("Cache backend is None")
    if not isinstance(cache, RedisCache):
        raise StoreUnavailable(
            f"Cache backend {type(cache).__name__} is not a valid django-redis RedisCache instance"
        )
    if not hasattr(cache, "client") or not hasattr(cache.client, "get_client"):
        raise StoreUnavailable("Cache backend client does not support get_client(write=True)")


def _serialize_record(record: dict) -> str:
    """规范化 JSON 序列化：sort_keys=True、紧凑 separators=(',', ':')、allow_nan=False。"""
    if not isinstance(record, dict):
        raise ValueError("Record must be a dict")
    try:
        return json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        )
    except (ValueError, TypeError):
        raise ValueError("Record cannot be encoded as canonical JSON") from None


class RedisIdempotencyStore:
    """Redis 请求幂等存储适配器。"""

    CAS_LUA_SCRIPT = _CAS_LUA_SCRIPT

    def __init__(self, cache: Any = None) -> None:
        """初始化存储适配器。若显式传入 cache 则立即进行 Fail-Closed 校验。"""
        if cache is not None:
            _validate_cache_backend(cache)
        self._cache = cache

    def _get_cache(self) -> Any:
        """获取并校验缓存实例；未传入时惰性获取 default 缓存并进行 Fail-Closed 校验。"""
        if self._cache is not None:
            return self._cache
        try:
            default_cache = caches["default"]
        except Exception:
            raise StoreUnavailable("Default cache unavailable") from None
        _validate_cache_backend(default_cache)
        return default_cache

    def create(self, identity: str, record: dict, now: float) -> bool:
        """
        原子创建初始幂等记录（NX 语义）。
        若键已存在或 now >= retain_until，返回 False；底层网络/Redis 异常转换为 StoreUnavailable。
        """
        _validate_identity(identity)
        if not isinstance(record, dict):
            raise ValueError("Record must be a dict")

        retain_until = record.get("retain_until")
        if retain_until is None:
            raise ValueError("Record missing retain_until")
        if (
            not isinstance(retain_until, (int, float))
            or math.isnan(retain_until)
            or math.isinf(retain_until)
        ):
            raise ValueError("Invalid retain_until value")

        if now >= retain_until:
            return False

        remaining_ms = int(round((retain_until - now) * 1000.0))
        if remaining_ms <= 0:
            return False

        serialized = _serialize_record(record)

        try:
            # 工厂/键网关同属存储边界，不能把带凭据的底层诊断泄漏到错误页或日志。
            cache = self._get_cache()
            raw_client = cache.client.get_client(write=True)
            raw_key = cache.make_key(f"external-content:request:v1:{identity}")
            res = raw_client.set(raw_key, serialized, nx=True, px=remaining_ms)
        except Exception:
            raise StoreUnavailable("Idempotency create unavailable") from None

        return bool(res)

    def read(self, identity: str) -> dict | None:
        """
        权威直连主库读取幂等记录。
        键不存在返回 None；若数据损坏或非 dict 则 Fail-Closed 抛出 StoreUnavailable。
        """
        _validate_identity(identity)

        try:
            cache = self._get_cache()
            raw_client = cache.client.get_client(write=True)
            raw_key = cache.make_key(f"external-content:request:v1:{identity}")
            raw_val = raw_client.get(raw_key)
        except Exception:
            raise StoreUnavailable("Idempotency read unavailable") from None

        if raw_val is None:
            return None

        try:
            if isinstance(raw_val, bytes):
                raw_val = raw_val.decode("utf-8")
            data = json.loads(raw_val)
        except (ValueError, TypeError):
            raise StoreUnavailable("Idempotency record corrupted") from None

        if not isinstance(data, dict):
            raise StoreUnavailable("Idempotency record is not a dict")

        return data

    def cas(self, identity: str, expected: dict, replacement: dict, now: float) -> bool:
        """
        基于 Lua 脚本的单键原子 CAS 状态变更。
        - 校验 replacement['version'] == expected['version'] + 1；
        - now >= retain_until 时拒绝；
        - 严禁延长 retain_until；
        - 仅且仅当 reserved -> issued 允许缩短 retain_until，其他状态转移必须严格一致；
        - Lua 用 Redis TIME 复验预留、执行、processing 与保留截止，过期 processing 只允许 unknown；
        - 时钟偏差不通过宽限补偿：任一时间观测过期就拒绝，不保证跨进程时钟同步；
        - TTL 取现存 PTTL、客户端剩余与服务器落点剩余的最小值，绝不扩窗。
        """
        _validate_identity(identity)
        if not isinstance(expected, dict) or not isinstance(replacement, dict):
            raise ValueError("expected and replacement must be dicts")

        # 1. 校验版本单调递增 (+1)
        exp_ver = expected.get("version")
        rep_ver = replacement.get("version")
        if not isinstance(exp_ver, int) or not isinstance(rep_ver, int) or rep_ver != exp_ver + 1:
            return False

        # 2. 校验 retain_until
        exp_retain = expected.get("retain_until")
        rep_retain = replacement.get("retain_until")
        if (
            not isinstance(exp_retain, (int, float))
            or not isinstance(rep_retain, (int, float))
            or math.isnan(exp_retain)
            or math.isinf(exp_retain)
            or math.isnan(rep_retain)
            or math.isinf(rep_retain)
        ):
            raise ValueError("Invalid retain_until in records")

        if now >= rep_retain:
            return False

        # 3. 校验 retain_until 变更规则 (Integration refinement)
        if rep_retain > exp_retain:
            return False
        elif rep_retain < exp_retain:
            if expected.get("state") != "reserved" or replacement.get("state") != "issued":
                return False

        remaining_ms = int(round((rep_retain - now) * 1000.0))
        if remaining_ms <= 0:
            return False

        # 4. 规范化序列化
        expected_str = _serialize_record(expected)
        replacement_str = _serialize_record(replacement)

        # 5. 直连主库并执行 Lua 原子 CAS
        try:
            cache = self._get_cache()
            raw_client = cache.client.get_client(write=True)
            raw_key = cache.make_key(f"external-content:request:v1:{identity}")
            res = raw_client.eval(
                self.CAS_LUA_SCRIPT,
                1,
                raw_key,
                expected_str,
                replacement_str,
                str(remaining_ms),
                str(now),
            )
        except Exception:
            raise StoreUnavailable("Idempotency transition unavailable") from None

        return bool(res == 1)
