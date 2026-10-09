"""
RedisIdempotencyStore 单元测试（L0 级别，零网络 IO / 零真实数据库 / 零真实 Redis 连接）。

验证覆盖：
1. 默认缓存别名仅限 django-redis RedisCache，在 LocMem 等非 Redis 缓存下严格 Fail-Closed (StoreUnavailable)。
2. 权威写客户端读取与写入：cache.make_key 严格仅调用一次，并通过 cache.client.get_client(write=True) 直连主库。
3. 32 位小写 16 进制身份标识严格校验。
4. JSON 规范序列化：sort_keys=True、紧凑 separators=(',', ':')、禁止 NaN/Infinity。
5. create: NX PX 剩余 retain_until 毫秒数原子创建；已存在或已过期拒绝。
6. read: 权威写客户端读取；解析 dict；损坏或非 dict 数据 Fail-Closed (StoreUnavailable)。
7. cas: 单键 Lua 脚本比对完整规范序列化 expected 记录；PTTL>0 约束；min(PTTL, remaining) 约束；禁止复活已丢失记录，禁止延长 TTL。
8. 专项细化（Integration refinement）：
   - 正常状态转移下 expected/replacement retain_until 必须完全一致；
   - 仅且仅当 expected.state == 'reserved' 且 replacement.state == 'issued' 时允许缩短 retain_until（绝不延长）；
   - 其他状态转移下任何缩短或延长 retain_until 均被严格拒绝。
9. 可控服务器时钟模拟 CAS 落点延迟/时钟偏差；这是适配器协议证据，不是实际 Redis Lua 执行证据。
"""

import json
import math
from unittest import TestCase
from unittest.mock import patch

from django.core.cache.backends.locmem import LocMemCache
from django.test import override_settings
from django_redis.cache import RedisCache

from external_content.idempotency_store import (
    RedisIdempotencyStore,
    StoreUnavailable,
)


class FakeRedisClient:
    """Fake Redis 客户端，严格模拟单键操作与 CAS Lua 契约，不执行真实网络 IO 与套接字。"""

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.raise_on_set = False
        self.raise_on_get = False
        self.raise_on_eval = False
        self.last_eval_args: dict | None = None
        # 固定合成时间，绝不以实际 epoch 让 L0 的 now=10000 记录全部过期。
        self.server_now = 10000.0

    def set(self, name: str, value: str | bytes, nx: bool = False, px: int | None = None) -> bool | None:
        if self.raise_on_set:
            raise RuntimeError("Fake Redis set network failure")
        if nx and name in self.store:
            return None
        pttl = int(px) if px is not None else -1
        self.store[name] = {
            "value": value if isinstance(value, str) else value.decode("utf-8"),
            "pttl": pttl,
        }
        return True

    def get(self, name: str) -> str | None:
        if self.raise_on_get:
            raise RuntimeError("Fake Redis get network failure")
        if name not in self.store:
            return None
        return self.store[name]["value"]

    def eval(self, script: str, numkeys: int, *keys_and_args) -> int:
        if self.raise_on_eval:
            raise RuntimeError("Fake Redis eval script execution failure")
        self.last_eval_args = {
            "script": script,
            "numkeys": numkeys,
            "keys_and_args": keys_and_args,
        }
        if numkeys != 1:
            raise ValueError(f"Expected numkeys=1, got {numkeys}")
        key = keys_and_args[0]
        expected_str = keys_and_args[1]
        replacement_str = keys_and_args[2]
        remaining_str = keys_and_args[3]

        if key not in self.store:
            return 0
        current = self.store[key]["value"]
        if current != expected_str:
            return 0
        pttl = self.store[key]["pttl"]
        if pttl <= 0:
            return 0
        try:
            remaining = int(remaining_str)
        except (ValueError, TypeError):
            return 0
        if remaining <= 0:
            return 0
        # 四参数协议增加调用方时间；兼容旧三参数协议以证明旧适配器的有效 RED。
        # 替身仅模拟服务器契约，不解释 Lua；真实脚本执行仍须另行授权验收。
        if len(keys_and_args) == 5:
            expected = json.loads(expected_str)
            replacement = json.loads(replacement_str)
            now = max(self.server_now, float(keys_and_args[4]))

            def live(deadline: object) -> bool:
                return type(deadline) in (int, float) and math.isfinite(deadline) and deadline > now

            if not live(replacement["retain_until"]):
                return 0
            state, next_state = expected.get("state"), replacement.get("state")
            if state == "reserved" and next_state == "issued":
                if not live(expected.get("reservation_deadline")) or not live(replacement.get("token_deadline")):
                    return 0
            elif state in {"issued", "failed"} and next_state == "processing":
                if not live(expected.get("token_deadline")) or not live(replacement.get("processing_deadline")):
                    return 0
            elif state == "processing":
                if next_state not in {"succeeded", "failed", "unknown"}:
                    return 0
                if replacement.get("owner") != expected.get("owner"):
                    return 0
                deadline = expected.get("processing_deadline")
                if type(deadline) not in (int, float) or not math.isfinite(deadline):
                    return 0
                if now >= deadline and next_state != "unknown":
                    return 0
            remaining = min(remaining, math.floor((replacement["retain_until"] - now) * 1000))
        new_ttl = min(pttl, remaining)
        if new_ttl <= 0:
            return 0
        self.store[key] = {"value": replacement_str, "pttl": new_ttl}
        return 1


class FakeRedisClientWrapper:
    """包装 FakeRedisClient 以模拟 django-redis client.get_client(write=True)。"""

    def __init__(self, raw_client: FakeRedisClient) -> None:
        self.raw_client = raw_client
        self.get_client_calls: list[dict] = []

    def get_client(self, write: bool = True) -> FakeRedisClient:
        self.get_client_calls.append({"write": write})
        return self.raw_client


class FakeRedisCache(RedisCache):
    """Fake django-redis RedisCache，记录 make_key 与 client 调用。"""

    def __init__(self) -> None:
        self.server = "127.0.0.1:6379"
        self.params = {}
        self.raw_client = FakeRedisClient()
        self._client_wrapper = FakeRedisClientWrapper(self.raw_client)
        self.make_key_calls: list[str] = []

    @property
    def client(self) -> FakeRedisClientWrapper:
        return self._client_wrapper

    def make_key(self, key: str, version: int | None = None) -> str:
        self.make_key_calls.append(key)
        return f":1:{key}"


class TestRedisIdempotencyStoreUnit(TestCase):
    def test_client_and_key_failures_are_safe_store_errors(self) -> None:
        """连接工厂和键网关故障也必须阻断，且不把底层诊断内容带入异常。"""
        record = {"version": 1, "retain_until": self.now + 100}
        operations = [
            lambda: self.store.create(self.valid_id, record, self.now),
            lambda: self.store.read(self.valid_id),
            lambda: self.store.cas(self.valid_id, record, dict(record, version=2), self.now),
        ]
        for target, attribute in [(self.fake_cache.client, "get_client"),
                                  (self.fake_cache, "make_key")]:
            for operation in operations:
                with self.subTest(attribute=attribute), patch.object(
                    target, attribute, side_effect=RuntimeError("unsafe-diagnostic-sentinel")
                ):
                    try:
                        operation()
                    except Exception as exc:
                        self.assertIsInstance(exc, StoreUnavailable)
                        self.assertNotIn("unsafe-diagnostic-sentinel", str(exc))
                        self.assertTrue(exc.__suppress_context__)
                    else:
                        self.fail("存储故障未阻断")

    def test_raw_redis_errors_do_not_expose_diagnostic_content(self) -> None:
        """原始 Redis 错误不输出可能含连接凭据或请求片段的诊断内容。"""
        record = {"version": 1, "retain_until": self.now + 100}
        for method, operation in [
            ("set", lambda: self.store.create(self.valid_id, record, self.now)),
            ("get", lambda: self.store.read(self.valid_id)),
            ("eval", lambda: self.store.cas(
                self.valid_id, record, dict(record, version=2), self.now)),
        ]:
            with self.subTest(method=method), patch.object(
                self.fake_cache.raw_client, method,
                side_effect=RuntimeError("unsafe-diagnostic-sentinel"),
            ):
                with self.assertRaises(StoreUnavailable) as caught:
                    operation()
                self.assertNotIn("unsafe-diagnostic-sentinel", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_invalid_utf8_is_a_safe_store_error(self) -> None:
        """损坏字节不能泄漏为 UnicodeDecodeError 或进入业务状态机。"""
        with patch.object(self.fake_cache.raw_client, "get", return_value=b"\xff"):
            try:
                self.store.read(self.valid_id)
            except Exception as exc:
                self.assertIsInstance(exc, StoreUnavailable)
            else:
                self.fail("损坏字节未阻断")

    def test_invalid_identity_is_not_echoed(self) -> None:
        """非法身份仅返回结构错误，不回显调用者传入内容。"""
        with self.assertRaises(ValueError) as caught:
            self.store.read("unsafe-diagnostic-sentinel")
        self.assertNotIn("unsafe-diagnostic-sentinel", str(caught.exception))

    def setUp(self) -> None:
        self.valid_id = "0123456789abcdef0123456789abcdef"
        self.now = 10000.0
        self.fake_cache = FakeRedisCache()
        self.store = RedisIdempotencyStore(cache=self.fake_cache)

    def _deadline_records(self, state: str, next_state: str) -> tuple[dict, dict]:
        """独立手算的小时间夹具；不借用生产方法生成期望截止值。"""
        expected = {
            "state": state, "version": 2, "owner": "worker-1",
            "reservation_deadline": 10010.0, "token_deadline": 10020.0,
            "processing_deadline": 10030.0, "retain_until": 10100.0,
        }
        replacement = dict(expected, state=next_state, version=3)
        if state == "reserved":
            replacement.update(token_deadline=10040.0, retain_until=10080.0)
        elif state in {"issued", "failed"} and next_state == "processing":
            replacement.update(owner="worker-2", processing_deadline=10050.0)
        return expected, replacement

    def _assert_deadline_cas(
        self, expected: dict, replacement: dict, *, server_now: float,
        allowed: bool, caller_now: float = 10000.0,
    ) -> None:
        """在 create 后推进服务器时钟，保持正 PTTL 以隔离逻辑 deadline 缺口。"""
        cache = FakeRedisCache()
        store = RedisIdempotencyStore(cache=cache)
        self.assertTrue(store.create(self.valid_id, expected, self.now))
        raw_key = f":1:external-content:request:v1:{self.valid_id}"
        original = dict(cache.raw_client.store[raw_key])
        cache.raw_client.server_now = server_now
        self.assertEqual(store.cas(self.valid_id, expected, replacement, caller_now), allowed)
        self.assertEqual(store.read(self.valid_id), replacement if allowed else expected)
        if allowed:
            self.assertLessEqual(cache.raw_client.store[raw_key]["pttl"], original["pttl"])
        else:
            self.assertEqual(cache.raw_client.store[raw_key], original)

    def test_atomic_reservation_deadline_rejects_stale_caller_clock(self) -> None:
        """Python 检查之后恰好到期/已过期，不能继续签发；正 PTTL 不代表可执行。"""
        expected, replacement = self._deadline_records("reserved", "issued")
        for server_now in (10010.0, 10010.001):
            with self.subTest(server_now=server_now):
                self._assert_deadline_cas(expected, replacement, server_now=server_now, allowed=False)

    def test_atomic_token_deadline_blocks_first_claim_and_failed_retry(self) -> None:
        """issued 首次领取和 failed 重试都必须按落点时间拒绝过期 Token。"""
        for state in ("issued", "failed"):
            expected, replacement = self._deadline_records(state, "processing")
            for server_now in (10020.0, 10020.001):
                with self.subTest(state=state, server_now=server_now):
                    self._assert_deadline_cas(expected, replacement, server_now=server_now, allowed=False)

    def test_atomic_new_deadline_must_still_be_live_at_landing(self) -> None:
        """已排队至新签发/新领取窗口也过期的 CAS 不得落盘。"""
        for state, next_state, server_now in (
            ("reserved", "issued", 10009.0),
            ("issued", "processing", 10019.0),
            ("failed", "processing", 10019.0),
        ):
            expected, replacement = self._deadline_records(state, next_state)
            field = "token_deadline" if state == "reserved" else "processing_deadline"
            replacement[field] = server_now
            with self.subTest(state=state):
                self._assert_deadline_cas(expected, replacement, server_now=server_now, allowed=False)

    def test_atomic_processing_timeout_allows_only_unknown(self) -> None:
        """过期 owner 不能迟到写成功/失败或重新领取；只能保留同 owner 待核对。"""
        for next_state in ("succeeded", "failed", "processing", "issued", "reserved"):
            expected, replacement = self._deadline_records("processing", next_state)
            for server_now in (10030.0, 10030.001):
                with self.subTest(next_state=next_state, server_now=server_now):
                    self._assert_deadline_cas(expected, replacement, server_now=server_now, allowed=False)
        expected, replacement = self._deadline_records("processing", "unknown")
        self._assert_deadline_cas(expected, replacement, server_now=10030.0, allowed=True)

    def test_processing_cannot_transfer_owner_or_renew_lease(self) -> None:
        """即使完整 expected 正确也不能借 CAS 将 processing 自动转给另一 owner。"""
        for next_state, server_now in (("processing", 10001.0), ("unknown", 10030.0)):
            expected, replacement = self._deadline_records("processing", next_state)
            replacement.update(owner="worker-2", processing_deadline=10099.0)
            with self.subTest(next_state=next_state):
                self._assert_deadline_cas(expected, replacement, server_now=server_now, allowed=False)

    def test_atomic_retain_until_blocks_even_unknown_with_positive_pttl(self) -> None:
        """固定保留截止到期后，连 unknown 都不得重新写入或延长物理 TTL。"""
        for state, next_state in (
            ("reserved", "issued"), ("issued", "processing"), ("processing", "unknown"),
        ):
            expected, replacement = self._deadline_records(state, next_state)
            for server_now in (replacement["retain_until"], replacement["retain_until"] + 0.001):
                with self.subTest(state=state, server_now=server_now):
                    self._assert_deadline_cas(expected, replacement, server_now=server_now, allowed=False)

    def test_deadline_transition_just_before_boundary_still_succeeds(self) -> None:
        """边界前仍可落盘，不把合法完成错误阻断，也不新增宽限窗口。"""
        for state, next_state, server_now in (
            ("reserved", "issued", 10009.999), ("issued", "processing", 10019.999),
            ("failed", "processing", 10019.999), ("processing", "succeeded", 10029.999),
            ("processing", "failed", 10029.999), ("processing", "unknown", 10000.0),
        ):
            expected, replacement = self._deadline_records(state, next_state)
            with self.subTest(state=state, next_state=next_state):
                self._assert_deadline_cas(expected, replacement, server_now=server_now, allowed=True)

    def test_caller_ahead_and_server_ahead_both_expire_conservatively(self) -> None:
        """两个进程时钟不同步时任一观测已到截止就阻断，而不是重算/续期。"""
        expected, replacement = self._deadline_records("issued", "processing")
        for server_now, caller_now in ((10020.0, 10000.0), (10000.0, 10020.0)):
            with self.subTest(server_now=server_now, caller_now=caller_now):
                self._assert_deadline_cas(
                    expected, replacement, server_now=server_now, caller_now=caller_now, allowed=False,
                )

    def test_server_elapsed_time_caps_ttl_instead_of_client_remaining(self) -> None:
        """客户端剩余 100s，但服务器落点已过去 10s；写回最多只能保留 90s。"""
        expected, replacement = self._deadline_records("processing", "succeeded")
        self.assertTrue(self.store.create(self.valid_id, expected, self.now))
        self.fake_cache.raw_client.server_now = 10010.0
        self.assertTrue(self.store.cas(self.valid_id, expected, replacement, self.now))
        raw_key = f":1:external-content:request:v1:{self.valid_id}"
        self.assertEqual(self.fake_cache.raw_client.store[raw_key]["pttl"], 90000)

    def test_submillisecond_remaining_is_not_rounded_into_extra_lifetime(self) -> None:
        """服务器只剩半毫秒时直接拒绝，不能向上取整扩大保留窗口。"""
        expected, replacement = self._deadline_records("processing", "unknown")
        self._assert_deadline_cas(expected, replacement, server_now=10099.9995, allowed=False)

    def test_cas_compares_owner_state_version_and_all_other_expected_fields(self) -> None:
        """晚到 owner 或另一快照的完整 expected 任一差异均不能覆盖当前记录。"""
        record, replacement = self._deadline_records("processing", "succeeded")
        record["binding"] = "digest-A"
        replacement["binding"] = "digest-A"
        self.assertTrue(self.store.create(self.valid_id, record, self.now))
        for field, value in (
            ("owner", "worker-old"), ("state", "issued"), ("version", 1),
            ("binding", "digest-old"), ("processing_deadline", 10031.0),
        ):
            with self.subTest(field=field):
                stale = dict(record, **{field: value})
                candidate = dict(replacement, version=stale["version"] + 1)
                self.assertFalse(self.store.cas(self.valid_id, stale, candidate, self.now))
                self.assertEqual(self.store.read(self.valid_id), record)

    def test_deadline_transition_rejects_missing_or_non_numeric_deadline(self) -> None:
        """坏 deadline 不得被 tonumber 宽松转换成字符串/布尔值授权。"""
        for state, next_state, field in (
            ("reserved", "issued", "reservation_deadline"),
            ("issued", "processing", "token_deadline"),
            ("processing", "succeeded", "processing_deadline"),
        ):
            for value in (None, "10040", True):
                expected, replacement = self._deadline_records(state, next_state)
                expected[field] = value
                with self.subTest(state=state, value=value):
                    self._assert_deadline_cas(expected, replacement, server_now=10000.0, allowed=False)

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
    def test_default_cache_locmem_fails_closed(self) -> None:
        """未显式传 cache 时默认使用 caches['default']，在 LocMem 下必须 Fail-Closed。"""
        store = RedisIdempotencyStore()
        with self.assertRaises(StoreUnavailable):
            store.read(self.valid_id)

    def test_explicit_locmem_cache_fails_closed(self) -> None:
        """显式传入 LocMemCache 必须 Fail-Closed 抛出 StoreUnavailable。"""
        locmem = LocMemCache("test-locmem", {})
        with self.assertRaises(StoreUnavailable):
            RedisIdempotencyStore(cache=locmem)

    def test_invalid_identity_validation(self) -> None:
        """身份标识必须严格为 32 位小写 16 进制字符，其余格式抛出 ValueError。"""
        invalid_identities = [
            "",
            "123",
            "0123456789abcdef0123456789abcde",  # 31 字符
            "0123456789abcdef0123456789abcdef0",  # 33 字符
            "0123456789ABCDEF0123456789ABCDEF",  # 大写
            "0123456789abcdef0123456789abcdeg",  # 非 hex (g)
            12345,  # 非 str
            None,
        ]
        valid_record = {"version": 1, "retain_until": self.now + 100}
        for bad_id in invalid_identities:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    self.store.read(bad_id)
                with self.assertRaises(ValueError):
                    self.store.create(bad_id, valid_record, self.now)
                with self.assertRaises(ValueError):
                    self.store.cas(bad_id, valid_record, dict(valid_record, version=2), self.now)

    def test_make_key_called_exactly_once_and_authoritative_write_client(self) -> None:
        """每次操作严格调用 cache.make_key 一次，且必须使用 get_client(write=True)。"""
        record = {"version": 1, "retain_until": self.now + 100}

        # 1. create
        self.fake_cache.make_key_calls.clear()
        self.fake_cache.client.get_client_calls.clear()
        self.assertTrue(self.store.create(self.valid_id, record, self.now))
        self.assertEqual(len(self.fake_cache.make_key_calls), 1)
        self.assertEqual(
            self.fake_cache.make_key_calls[0],
            f"external-content:request:v1:{self.valid_id}",
        )
        self.assertEqual(self.fake_cache.client.get_client_calls, [{"write": True}])

        # 2. read
        self.fake_cache.make_key_calls.clear()
        self.fake_cache.client.get_client_calls.clear()
        read_record = self.store.read(self.valid_id)
        self.assertIsNotNone(read_record)
        self.assertEqual(len(self.fake_cache.make_key_calls), 1)
        self.assertEqual(
            self.fake_cache.make_key_calls[0],
            f"external-content:request:v1:{self.valid_id}",
        )
        self.assertEqual(self.fake_cache.client.get_client_calls, [{"write": True}])

        # 3. cas
        self.fake_cache.make_key_calls.clear()
        self.fake_cache.client.get_client_calls.clear()
        replacement = dict(record, version=2)
        self.assertTrue(self.store.cas(self.valid_id, record, replacement, self.now))
        self.assertEqual(len(self.fake_cache.make_key_calls), 1)
        self.assertEqual(
            self.fake_cache.make_key_calls[0],
            f"external-content:request:v1:{self.valid_id}",
        )
        self.assertEqual(self.fake_cache.client.get_client_calls, [{"write": True}])

    def test_create_success_and_sets_px_ttl(self) -> None:
        """create 以 NX PX 设定剩余 retain_until 毫秒数。"""
        record = {"schema": 1, "state": "reserved", "version": 1, "retain_until": self.now + 10.5}
        created = self.store.create(self.valid_id, record, self.now)
        self.assertTrue(created)

        raw_key = f":1:external-content:request:v1:{self.valid_id}"
        stored = self.fake_cache.raw_client.store[raw_key]
        self.assertEqual(stored["pttl"], 10500)
        loaded = json.loads(stored["value"])
        self.assertEqual(loaded["state"], "reserved")
        self.assertEqual(loaded["version"], 1)

    def test_create_already_exists_returns_false(self) -> None:
        """若键已存在，create NX 返回 False，不覆盖现有数据。"""
        record = {"version": 1, "retain_until": self.now + 100}
        self.assertTrue(self.store.create(self.valid_id, record, self.now))
        second_record = {"version": 99, "retain_until": self.now + 200}
        self.assertFalse(self.store.create(self.valid_id, second_record, self.now))

        # 确认未被覆盖
        self.assertEqual(self.store.read(self.valid_id)["version"], 1)

    def test_create_expired_now_returns_false(self) -> None:
        """now >= retain_until 时 create 直接拒绝，返回 False。"""
        record = {"version": 1, "retain_until": self.now}
        self.assertFalse(self.store.create(self.valid_id, record, self.now))
        self.assertFalse(self.store.create(self.valid_id, record, self.now + 1.0))

    def test_create_redis_error_sanitized_to_store_unavailable(self) -> None:
        """Redis 网络/底层错误统一转换为 StoreUnavailable。"""
        self.fake_cache.raw_client.raise_on_set = True
        record = {"version": 1, "retain_until": self.now + 100}
        with self.assertRaises(StoreUnavailable):
            self.store.create(self.valid_id, record, self.now)

    def test_read_returns_dict_when_found(self) -> None:
        """read 成功反序列化 JSON 为 dict。"""
        record = {"version": 1, "retain_until": self.now + 100, "state": "reserved"}
        self.assertTrue(self.store.create(self.valid_id, record, self.now))
        result = self.store.read(self.valid_id)
        self.assertEqual(result, record)

    def test_read_returns_none_when_missing(self) -> None:
        """read 键不存在时返回 None。"""
        self.assertIsNone(self.store.read(self.valid_id))

    def test_read_corrupted_json_raises_store_unavailable(self) -> None:
        """read 遇到非合法 JSON 数据时 Fail-Closed 抛出 StoreUnavailable。"""
        raw_key = f":1:external-content:request:v1:{self.valid_id}"
        self.fake_cache.raw_client.store[raw_key] = {"value": "{malformed json", "pttl": 1000}
        with self.assertRaises(StoreUnavailable):
            self.store.read(self.valid_id)

    def test_read_non_dict_json_raises_store_unavailable(self) -> None:
        """read 遇到合法 JSON 但非 dict 结构时 Fail-Closed 抛出 StoreUnavailable。"""
        raw_key = f":1:external-content:request:v1:{self.valid_id}"
        self.fake_cache.raw_client.store[raw_key] = {"value": "[1, 2, 3]", "pttl": 1000}
        with self.assertRaises(StoreUnavailable):
            self.store.read(self.valid_id)

    def test_read_redis_error_sanitized_to_store_unavailable(self) -> None:
        """read 遭遇 Redis 错误时统一抛出 StoreUnavailable。"""
        self.fake_cache.raw_client.raise_on_get = True
        with self.assertRaises(StoreUnavailable):
            self.store.read(self.valid_id)

    def test_cas_success_lua_atomically_updates(self) -> None:
        """cas 完整比对成功后原子更新，版本 +1，TTL 正确继承。"""
        record = {"state": "issued", "version": 2, "retain_until": self.now + 100,
                  "token_deadline": self.now + 100}
        self.assertTrue(self.store.create(self.valid_id, record, self.now))

        replacement = {"state": "processing", "version": 3, "retain_until": self.now + 100,
                       "owner": "worker-1", "processing_deadline": self.now + 50}
        success = self.store.cas(self.valid_id, record, replacement, self.now + 10)
        self.assertTrue(success)
        self.assertEqual(self.store.read(self.valid_id), replacement)

    def test_cas_fails_when_key_missing_never_creates(self) -> None:
        """cas 键不存在时绝不复活或创建记录，直接返回 False。"""
        expected = {"version": 1, "retain_until": self.now + 100}
        replacement = {"version": 2, "retain_until": self.now + 100}
        self.assertFalse(self.store.cas(self.valid_id, expected, replacement, self.now))
        self.assertIsNone(self.store.read(self.valid_id))

    def test_cas_fails_when_current_record_differs_from_expected(self) -> None:
        """cas 对比完整规范序列化；若 current 与 expected 任一字段不一致则返回 False。"""
        record = {"state": "issued", "version": 2, "binding": "hash1", "retain_until": self.now + 100}
        self.assertTrue(self.store.create(self.valid_id, record, self.now))

        # expected 中 binding 不一致
        stale_expected = dict(record, binding="stale_hash")
        replacement = dict(record, version=3)
        self.assertFalse(self.store.cas(self.valid_id, stale_expected, replacement, self.now))
        self.assertEqual(self.store.read(self.valid_id)["version"], 2)

    def test_cas_fails_when_version_not_incremented_by_one(self) -> None:
        """cas 校验 replacement['version'] == expected['version'] + 1，否则拒绝。"""
        record = {"version": 2, "retain_until": self.now + 100}
        self.assertTrue(self.store.create(self.valid_id, record, self.now))

        # 版本未递增
        same_ver = dict(record, version=2)
        self.assertFalse(self.store.cas(self.valid_id, record, same_ver, self.now))
        # 版本跳变
        jump_ver = dict(record, version=4)
        self.assertFalse(self.store.cas(self.valid_id, record, jump_ver, self.now))

    def test_cas_fails_when_expired_now_ge_retain_until(self) -> None:
        """now >= retain_until 时 cas 拒绝执行。"""
        record = {"version": 1, "retain_until": self.now + 100}
        self.assertTrue(self.store.create(self.valid_id, record, self.now))
        replacement = dict(record, version=2)
        self.assertFalse(self.store.cas(self.valid_id, record, replacement, self.now + 100))
        self.assertFalse(self.store.cas(self.valid_id, record, replacement, self.now + 101))

    def test_cas_fails_when_extending_retain_until(self) -> None:
        """cas 严禁延长 retain_until，任何延长时间的尝试均被拒绝。"""
        record = {"state": "reserved", "version": 1, "retain_until": self.now + 100}
        self.assertTrue(self.store.create(self.valid_id, record, self.now))

        # 尝试延长 retain_until
        extended = dict(record, state="issued", version=2, retain_until=self.now + 200)
        self.assertFalse(self.store.cas(self.valid_id, record, extended, self.now))

    def test_cas_shortening_retain_until_permitted_only_for_reserved_to_issued(self) -> None:
        """
        Integration refinement 专项测试：
        仅允许 expected.state=='reserved' 且 replacement.state=='issued' 缩短 retain_until。
        """
        # 初始预留 48h
        reserved_record = {
            "state": "reserved",
            "version": 1,
            "retain_until": self.now + 172800,
            "reservation_deadline": self.now + 86400,
        }
        self.assertTrue(self.store.create(self.valid_id, reserved_record, self.now))

        # 准备阶段缩短至 24h
        issued_record = {
            "state": "issued",
            "version": 2,
            "retain_until": self.now + 86400,
            "token_deadline": self.now + 1800,
        }
        self.assertTrue(self.store.cas(self.valid_id, reserved_record, issued_record, self.now + 10))
        updated = self.store.read(self.valid_id)
        self.assertEqual(updated["state"], "issued")
        self.assertEqual(updated["retain_until"], self.now + 86400)

        # 确认 Redis 键的 TTL 确实被缩短
        raw_key = f":1:external-content:request:v1:{self.valid_id}"
        stored = self.fake_cache.raw_client.store[raw_key]
        self.assertLessEqual(stored["pttl"], 86400 * 1000)

    def test_cas_shortening_retain_until_rejected_for_other_states(self) -> None:
        """非 reserved->issued 状态转移时，任何缩短 retain_until 的行为均被严格拒绝。"""
        record = {"state": "issued", "version": 2, "retain_until": self.now + 86400}
        self.assertTrue(self.store.create(self.valid_id, record, self.now))

        # issued -> processing 尝试缩短 retain_until
        shortened_processing = {
            "state": "processing",
            "version": 3,
            "retain_until": self.now + 43200,
        }
        self.assertFalse(self.store.cas(self.valid_id, record, shortened_processing, self.now))

    def test_cas_ttl_is_min_of_old_pttl_and_remaining_never_extends(self) -> None:
        """Lua 脚本中新 TTL 必须取 math.min(pttl, remaining)，绝不借由 CAS 延长物理过期。"""
        record = {"state": "issued", "version": 2, "retain_until": self.now + 100}
        self.assertTrue(self.store.create(self.valid_id, record, self.now))

        raw_key = f":1:external-content:request:v1:{self.valid_id}"
        # 人为把 Redis 中的 PTTL 衰减为 5000ms
        self.fake_cache.raw_client.store[raw_key]["pttl"] = 5000

        replacement = dict(record, version=3)
        # 此时 remaining 是 90000ms
        self.assertTrue(self.store.cas(self.valid_id, record, replacement, self.now + 10))
        # 结果 TTL 必须仍然 <= 5000ms
        self.assertLessEqual(self.fake_cache.raw_client.store[raw_key]["pttl"], 5000)

    def test_cas_redis_error_sanitized_to_store_unavailable(self) -> None:
        """cas 发生 Redis 异常时统一转换为 StoreUnavailable。"""
        record = {"version": 1, "retain_until": self.now + 100}
        self.assertTrue(self.store.create(self.valid_id, record, self.now))
        self.fake_cache.raw_client.raise_on_eval = True
        with self.assertRaises(StoreUnavailable):
            self.store.cas(self.valid_id, record, dict(record, version=2), self.now)

    def test_canonical_json_encoding_sort_keys_compact_no_nan(self) -> None:
        """序列化必须规范：sort_keys=True、紧凑 separators=(',', ':')、禁止 NaN/Inf。"""
        # 乱序 key 在序列化后应完全确定
        unordered = {"z": 1, "a": 2, "m": 3, "version": 1, "retain_until": self.now + 100}
        self.assertTrue(self.store.create(self.valid_id, unordered, self.now))

        raw_key = f":1:external-content:request:v1:{self.valid_id}"
        raw_value = self.fake_cache.raw_client.store[raw_key]["value"]
        # 确保无空格，且 key 字典序
        self.assertIn('"a":2', raw_value)
        self.assertNotIn(": ", raw_value)
        self.assertNotIn(", ", raw_value)

        # NaN / Inf 校验
        nan_record = {"version": 1, "retain_until": float("nan")}
        with self.assertRaises(ValueError):
            self.store.create(self.valid_id, nan_record, self.now)

        inf_record = {"version": 1, "retain_until": float("inf")}
        with self.assertRaises(ValueError):
            self.store.create(self.valid_id, inf_record, self.now)
