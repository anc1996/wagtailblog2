"""D3 P-05 会话内核 L0：确定性替身，不连接 Redis、ES 或数据库。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
import json
from threading import Event, Lock
import traceback
import unittest
from unittest.mock import patch

try:
    session = import_module("search.services.search_session")
except ModuleNotFoundError as error:
    if error.name != "search.services.search_session":
        raise
    session = None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeStore:
    """JSON 往返 + 互斥 CAS，模拟持久检查点而非复用引擎内存对象。"""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.rows: dict = {}
        self.lock = Lock()

    def create(self, session_id: str, document: dict, ttl_seconds: float) -> bool:
        with self.lock:
            if session_id in self.rows:
                return False
            self.rows[session_id] = (
                0, json.dumps(document, sort_keys=True), self.clock() + ttl_seconds
            )
            return True

    def read(self, session_id: str):
        with self.lock:
            row = self.rows.get(session_id)
            if row is None or row[2] <= self.clock():
                return None
            return session.StoredSession(row[0], json.loads(row[1]))

    def compare_and_swap(
        self, session_id: str, version: int, document: dict, ttl_seconds: float
    ) -> bool:
        with self.lock:
            row = self.rows.get(session_id)
            if row is None or row[2] <= self.clock() or row[0] != version:
                return False
            self.rows[session_id] = (
                version + 1, json.dumps(document, sort_keys=True), self.clock() + ttl_seconds
            )
            return True


class FakeSource:
    """按稳定快照位置取批；被耗尽来源再次调用会暴露内核回头查询。"""

    def __init__(self, candidates: list) -> None:
        self.candidates = candidates
        self.calls: list[tuple[int, int]] = []
        self.entered: Event | None = None
        self.release: Event | None = None
        self.failure: Exception | None = None

    def fetch(self, state, batch_size: int):
        if self.failure:
            raise self.failure
        offset = int(state.search_after[0]) if state.search_after else 0
        if self.calls and offset >= len(self.candidates):
            raise AssertionError("exhausted source fetched again")
        self.calls.append((offset, batch_size))
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("test concurrency barrier timed out")
        end = min(offset + batch_size, len(self.candidates))
        return session.FetchBatch(
            tuple(self.candidates[offset:end]),
            session.FetchState((end,), "server-only-pit"),
            end == len(self.candidates),
        )


class Validator:
    def __init__(self) -> None:
        self.hidden: set[int] = set()
        self.calls: list[int] = []
        self.failure: Exception | None = None

    def __call__(self, candidate):
        if self.failure:
            raise self.failure
        self.calls.append(candidate.page_id)
        if candidate.page_id in self.hidden:
            return None
        return {"page_id": candidate.page_id, "title": f"公开页{candidate.page_id}"}


class SearchSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(session, "D3 分页会话内核尚未实现")
        self.clock = Clock()
        self.store = FakeStore(self.clock)
        self.validator = Validator()

    def candidate(self, pk: int, date: datetime | None = None):
        return session.Candidate(pk, date, {"version": 1, "marker": "server-only-candidate"})

    def engine(self, sources: dict, **config):
        self.sources = sources
        self.binding = session.SessionBinding(
            query="tail-word",
            scope={
                "site_id": 1, "root_page_id": 2, "locale_id": 3,
                "visibility": "public", "search_type": "all",
            },
            page_size=2,
            registry_epoch="registry-1",
            index_epochs={key: "index-1" for key in sources},
            source_snapshots={key: "snapshot-1" for key in sources},
        )
        self.signer = session.SessionTokenSigner(b"unit-test-signing-key-not-a-credential")
        return session.SearchSessionEngine(
            sources=sources, validator=self.validator, store=self.store,
            signer=self.signer, config=session.SessionConfig(**config), clock=self.clock,
        )

    def ids(self, page) -> list[int]:
        return [result["page_id"] for result in page.results]

    def test_rrf_cumulative_rank_preserves_unconsumed_candidates(self) -> None:
        """捕获每批重置 rank、整批丢弃缓冲和来源优先级倒置。"""
        a = FakeSource([self.candidate(pk) for pk in (90, 91, 92)])
        b = FakeSource([self.candidate(pk) for pk in (10, 11, 12)])
        engine = self.engine({"a": a, "b": b}, batch_size=2)
        first = engine.page(engine.create(self.binding), self.binding)
        second = engine.page(first.next_cursor, self.binding)
        third = engine.page(second.next_cursor, self.binding)
        self.assertEqual(self.ids(first), [90, 10])
        self.assertEqual(self.ids(second), [91, 11])
        self.assertEqual(self.ids(third), [92, 12])
        self.assertTrue(first.has_more)
        self.assertFalse(third.has_more)
        self.assertIsNone(third.next_cursor)
        self.assertIsNone(first.total)
        self.assertEqual(first.total_relation, "unknown")
        self.assertEqual(first.returned_count, 2)

    def test_continuous_pagination_reaches_tail_beyond_100(self) -> None:
        source = FakeSource([self.candidate(pk) for pk in range(1, 258)])
        engine = self.engine({"a": source}, batch_size=7)
        self.binding = replace(self.binding, page_size=13)
        token = engine.create(self.binding)
        seen: list[int] = []
        while token:
            page = engine.page(token, self.binding)
            seen.extend(self.ids(page))
            token = page.next_cursor
        self.assertEqual(seen, list(range(1, 258)))
        self.assertFalse(page.has_more)

    def test_source_without_safe_head_blocks_other_buffer_and_resumes_scan(self) -> None:
        a = FakeSource([self.candidate(pk) for pk in (1, 2, 3)])
        b = FakeSource([self.candidate(pk) for pk in (10, 11, 12)])
        self.validator.hidden.update({1, 2})
        engine = self.engine({"a": a, "b": b}, batch_size=1, max_fetches=2)
        first = engine.page(engine.create(self.binding), self.binding)
        self.assertEqual(self.ids(first), [])
        self.assertIsNone(first.has_more)
        self.assertTrue(first.partial)
        self.assertEqual(first.continuation_kind, "scan")
        token = first.next_cursor
        seen = []
        for _ in range(8):
            resumed = engine.page(token, self.binding)
            seen.extend(self.ids(resumed))
            token = resumed.next_cursor
            if token is None:
                break
        # a 的累计 rank=3，不能在 b 的 rank=1/2 前输出。
        self.assertEqual(seen, [10, 11, 3, 12])

    def test_filtered_batches_do_not_reset_rank_or_claim_exhaustion(self) -> None:
        a = FakeSource([self.candidate(pk) for pk in (1, 2, 3, 4, 5)])
        b = FakeSource([self.candidate(pk) for pk in (20, 21, 22)])
        self.validator.hidden.update({1, 2, 3, 4})
        engine = self.engine({"a": a, "b": b}, batch_size=2, max_fetches=1)
        token = engine.create(self.binding)
        seen = []
        pages = []
        for _ in range(12):
            page = engine.page(token, self.binding)
            pages.append(page)
            seen.extend(self.ids(page))
            token = page.next_cursor
            if token is None:
                break
        self.assertEqual(seen, [20, 21, 22, 5])
        self.assertTrue(any(page.has_more is None for page in pages))
        self.assertFalse(pages[-1].has_more)

    def test_lookahead_must_find_safe_candidate_not_merely_nonempty_buffer(self) -> None:
        source = FakeSource([self.candidate(pk) for pk in (1, 2, 3)])
        self.validator.hidden.add(3)
        engine = self.engine({"a": source}, batch_size=3)
        page = engine.page(engine.create(self.binding), self.binding)
        self.assertEqual(self.ids(page), [1, 2])
        self.assertFalse(page.has_more)
        self.assertIsNone(page.next_cursor)

    def test_full_page_without_lookahead_proof_is_unknown_and_partial(self) -> None:
        source = FakeSource([self.candidate(pk) for pk in (1, 2, 3)])
        engine = self.engine({"a": source}, batch_size=2, max_fetches=1)
        page = engine.page(engine.create(self.binding), self.binding)
        self.assertEqual(self.ids(page), [1, 2])
        self.assertIsNone(page.has_more)
        self.assertTrue(page.partial)
        self.assertEqual(page.continuation_kind, "scan")
        next_page = engine.page(page.next_cursor, self.binding)
        self.assertEqual(self.ids(next_page), [3])
        self.assertFalse(next_page.has_more)

    def test_empty_source_terminal_state_never_restarts(self) -> None:
        empty = FakeSource([])
        active = FakeSource([self.candidate(pk) for pk in range(1, 9)])
        engine = self.engine({"empty": empty, "active": active}, batch_size=2)
        token = engine.create(self.binding)
        seen = []
        while token:
            page = engine.page(token, self.binding)
            seen.extend(self.ids(page))
            token = page.next_cursor
        self.assertEqual(seen, list(range(1, 9)))
        self.assertEqual(len(empty.calls), 1)

    def test_replay_revalidates_same_page_without_source_or_checkpoint_advance(self) -> None:
        source = FakeSource([self.candidate(pk) for pk in (1, 2, 3, 4)])
        engine = self.engine({"a": source}, batch_size=4)
        token = engine.create(self.binding)
        original = engine.page(token, self.binding)
        calls = len(source.calls)
        self.validator.hidden.add(1)
        replay = engine.page(token, self.binding)
        self.assertEqual(self.ids(replay), [2])
        self.assertEqual(replay.next_cursor, original.next_cursor)
        self.assertEqual(len(source.calls), calls)
        next_page = engine.page(replay.next_cursor, self.binding)
        self.assertEqual(self.ids(next_page), [3, 4])

    def test_replay_revoked_lookahead_downgrades_more_to_unknown(self) -> None:
        source = FakeSource([self.candidate(pk) for pk in (1, 2, 3)])
        engine = self.engine({"a": source}, batch_size=3)
        token = engine.create(self.binding)
        original = engine.page(token, self.binding)
        self.validator.hidden.add(3)
        replay = engine.page(token, self.binding)
        self.assertTrue(original.has_more)
        self.assertIsNone(replay.has_more)
        self.assertTrue(replay.partial)
        terminal = engine.page(replay.next_cursor, self.binding)
        self.assertFalse(terminal.has_more)
        self.assertEqual(self.ids(terminal), [])

    def test_parallel_requests_do_not_fetch_twice_or_fork_next_checkpoint(self) -> None:
        source = FakeSource([self.candidate(pk) for pk in (1, 2, 3)])
        source.entered, source.release = Event(), Event()
        engine = self.engine({"a": source}, batch_size=3)
        token = engine.create(self.binding)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(engine.page, token, self.binding)
            self.assertTrue(source.entered.wait(5))
            try:
                with self.assertRaises(session.SearchSessionBusy):
                    engine.page(token, self.binding)
            finally:
                source.release.set()
            original = running.result(timeout=5)
        replay = engine.page(token, self.binding)
        self.assertEqual(self.ids(original), [1, 2])
        self.assertEqual(replay.next_cursor, original.next_cursor)
        self.assertEqual(len(source.calls), 1)

    def test_date_merge_normalizes_utc_and_places_missing_last(self) -> None:
        utc = timezone.utc
        plus8 = timezone(timedelta(hours=8))
        a = FakeSource([
            self.candidate(9, datetime(2026, 10, 8, 8, tzinfo=plus8)),
            self.candidate(10, None),
        ])
        b = FakeSource([
            self.candidate(1, datetime(2026, 10, 8, 0, tzinfo=utc)),
            self.candidate(2, datetime(2026, 10, 9, 0, tzinfo=utc)),
        ])
        engine = self.engine({"a": a, "b": b}, batch_size=2)
        self.binding = replace(self.binding, order_by="date", page_size=4)
        page = engine.page(engine.create(self.binding), self.binding)
        self.assertEqual(self.ids(page), [9, 1, 2, 10])

    def test_descending_date_and_missing_values_remain_globally_ordered(self) -> None:
        a = FakeSource([
            self.candidate(2, datetime(2026, 10, 10, tzinfo=timezone.utc)),
            self.candidate(3, None),
        ])
        b = FakeSource([
            self.candidate(1, datetime(2026, 10, 9, tzinfo=timezone.utc)),
        ])
        engine = self.engine({"a": a, "b": b}, batch_size=1)
        self.binding = replace(self.binding, order_by="-date", page_size=4)
        page = engine.page(engine.create(self.binding), self.binding)
        self.assertEqual(self.ids(page), [2, 1, 3])

    def test_date_range_excludes_missing_and_uses_half_open_utc_boundaries(self) -> None:
        source = FakeSource([
            self.candidate(1, datetime(2026, 10, 7, tzinfo=timezone.utc)),
            self.candidate(2, datetime(2026, 10, 8, tzinfo=timezone.utc)),
            self.candidate(3, datetime(2026, 10, 9, tzinfo=timezone.utc)),
            self.candidate(4, None),
        ])
        engine = self.engine({"a": source}, batch_size=4)
        self.binding = replace(
            self.binding, order_by="date",
            start_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
            end_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
        )
        page = engine.page(engine.create(self.binding), self.binding)
        self.assertEqual(self.ids(page), [2])
        self.assertFalse(page.has_more)

    def test_naive_datetime_and_non_monotonic_source_fail_closed(self) -> None:
        for candidates in (
            [self.candidate(1, datetime(2026, 10, 8))],
            [
                self.candidate(1, datetime(2026, 10, 9, tzinfo=timezone.utc)),
                self.candidate(2, datetime(2026, 10, 8, tzinfo=timezone.utc)),
            ],
        ):
            with self.subTest(candidates=candidates):
                engine = self.engine({"a": FakeSource(candidates)}, batch_size=1)
                self.binding = replace(self.binding, order_by="date", page_size=3)
                with self.assertRaises(session.SearchSessionSourceError):
                    engine.page(engine.create(self.binding), self.binding)

    def test_source_failure_does_not_commit_partial_other_source_page(self) -> None:
        a = FakeSource([self.candidate(1)])
        b = FakeSource([self.candidate(2)])
        b.failure = RuntimeError("source unavailable")
        engine = self.engine({"a": a, "b": b}, batch_size=1)
        token = engine.create(self.binding)
        with self.assertRaises(session.SearchSessionSourceError):
            engine.page(token, self.binding)
        b.failure = None
        # 恢复仍从原始检查点开始；不是从 a 已消费/已耗尽的假进度开始。
        a.calls.clear()
        page = engine.page(token, self.binding)
        self.assertEqual(self.ids(page), [1, 2])

    def test_validator_service_failure_propagates_without_success_response(self) -> None:
        engine = self.engine({"a": FakeSource([self.candidate(1)])})
        token = engine.create(self.binding)
        self.validator.failure = RuntimeError("SQL or Mongo unavailable")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            engine.page(token, self.binding)
        self.validator.failure = None
        self.sources["a"].calls.clear()
        self.assertEqual(self.ids(engine.page(token, self.binding)), [1])

    def test_validator_failure_survives_lease_cleanup_failure(self) -> None:
        """次生 CAS 故障不得改变原业务异常身份、来源栈或输出敏感上下文。"""
        engine = self.engine({"a": FakeSource([self.candidate(1)])})
        token = engine.create(self.binding)
        original = RuntimeError("validator failure sentinel")
        self.validator.failure = original
        compare_and_swap = self.store.compare_and_swap
        cleanup_versions = []

        def fail_cleanup(session_id, version, document, ttl_seconds):
            if version == 1:
                cleanup_versions.append(version)
                raise session.SearchSessionStoreUnavailable()
            return compare_and_swap(session_id, version, document, ttl_seconds)

        with patch.object(self.store, "compare_and_swap", side_effect=fail_cleanup):
            with self.assertLogs("search.services.search_session", level="WARNING") as logs:
                try:
                    engine.page(token, self.binding)
                except Exception as error:
                    self.assertIs(error, original)
                    frames = traceback.extract_tb(error.__traceback__)
                    self.assertEqual(frames[-1].name, "__call__")
                    self.assertNotIn("fail_cleanup", [frame.name for frame in frames])
                else:
                    self.fail("validator failure returned a success page")
        self.assertEqual(cleanup_versions, [1])
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].getMessage(), "search_session_lease_release_failed")
        self.assertEqual(logs.records[0].args, ())
        self.assertIsNone(logs.records[0].exc_info)
        self.assertIsNone(logs.records[0].stack_info)
        claims = self.signer.decode(token)
        row = self.store.read(claims["session_id"])
        self.assertEqual(row.version, 1)
        self.assertIsNotNone(row.document["lease"])
        self.assertIsNone(row.document["checkpoints"][claims["checkpoint_id"]]["plan"])

    def test_token_contains_handles_and_binding_hash_only(self) -> None:
        source = FakeSource([self.candidate(pk) for pk in (1, 2, 3)])
        engine = self.engine({"a": source}, batch_size=3)
        token = engine.create(self.binding)
        page = engine.page(token, self.binding)
        for value in (token, page.next_cursor):
            claims = self.signer.decode(value)
            self.assertEqual(set(claims), {"session_id", "checkpoint_id", "binding_hash"})
            self.assertNotIn("server-only", json.dumps(claims))
            self.assertNotIn("tail-word", json.dumps(claims))
        with self.assertRaises(session.SearchSessionTokenError):
            engine.page(token + "bad", self.binding)

    def test_all_scope_and_epoch_changes_reject_old_token(self) -> None:
        engine = self.engine({"a": FakeSource([self.candidate(1)])})
        token = engine.create(self.binding)
        variants = [
            replace(self.binding, query="other"),
            replace(self.binding, page_size=3),
            replace(self.binding, order_by="date"),
            replace(self.binding, source_snapshots={"a": "snapshot-2"}),
            replace(self.binding, start_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            replace(self.binding, end_at=datetime(2027, 1, 1, tzinfo=timezone.utc)),
        ]
        for key in self.binding.scope:
            variants.append(replace(self.binding, scope={**self.binding.scope, key: "changed"}))
        for binding in variants:
            with self.subTest(binding=binding):
                with self.assertRaises(session.SearchSessionBindingError):
                    engine.page(token, binding)
        for binding in (
            replace(self.binding, registry_epoch="registry-2"),
            replace(self.binding, index_epochs={"a": "index-2"}),
        ):
            with self.assertRaises(session.SearchSessionExpired):
                engine.page(token, binding)

    def test_expiry_is_absolute_not_sliding_and_old_checkpoints_expire(self) -> None:
        engine = self.engine(
            {"a": FakeSource([self.candidate(pk) for pk in range(1, 30)])},
            batch_size=3, ttl_seconds=20, max_checkpoints=2,
        )
        original = engine.create(self.binding)
        first = engine.page(original, self.binding)
        self.clock.now += 10
        second = engine.page(first.next_cursor, self.binding)
        with self.assertRaises(session.SearchSessionExpired):
            engine.page(original, self.binding)
        self.clock.now += 10
        with self.assertRaises(session.SearchSessionExpired):
            engine.page(second.next_cursor, self.binding)

    def test_scan_candidate_budget_does_not_become_total_result_limit(self) -> None:
        engine = self.engine(
            {"a": FakeSource([self.candidate(pk) for pk in range(1, 19)])},
            batch_size=20, max_scan_candidates=3,
        )
        self.binding = replace(self.binding, page_size=8)
        token = engine.create(self.binding)
        seen = []
        while token:
            page = engine.page(token, self.binding)
            seen.extend(self.ids(page))
            token = page.next_cursor
        self.assertEqual(seen, list(range(1, 19)))
        self.assertTrue(all(size <= 3 for _, size in self.sources["a"].calls))

    def test_nonprogressing_source_is_error_not_infinite_scan(self) -> None:
        class Stuck:
            def fetch(self, state, batch_size):
                return session.FetchBatch((), state, False)
        engine = self.engine({"a": Stuck()})
        with self.assertRaises(session.SearchSessionSourceError):
            engine.page(engine.create(self.binding), self.binding)

    def test_state_capacity_exceeded_is_explicit_not_silent_truncation(self) -> None:
        large = session.Candidate(1, None, {"reference": "x" * 10000})
        engine = self.engine({"a": FakeSource([large])}, max_state_bytes=2000)
        token = engine.create(self.binding)
        with self.assertRaises(session.SearchSessionCapacityError):
            engine.page(token, self.binding)

    def test_scope_is_defensively_bound_against_caller_mutation(self) -> None:
        engine = self.engine({"a": FakeSource([self.candidate(1)])})
        binding = deepcopy(self.binding)
        token = engine.create(binding)
        binding.scope["site_id"] = 999
        with self.assertRaises(session.SearchSessionBindingError):
            engine.page(token, binding)

    def test_serialized_object_key_order_does_not_change_source_ties(self) -> None:
        engine = self.engine({
            "z_first": FakeSource([self.candidate(99)]),
            "a_second": FakeSource([self.candidate(1)]),
        }, batch_size=1)
        page = engine.page(engine.create(self.binding), self.binding)
        self.assertEqual(self.ids(page), [99, 1])

    def test_checkpoint_retention_uses_chronology_not_random_handle_order(self) -> None:
        engine = self.engine(
            {"a": FakeSource([self.candidate(pk) for pk in range(1, 11)])},
            batch_size=3, max_checkpoints=2,
        )
        # JSON object 排序会重排随机 key；检查点必须用独立时间顺序维护。
        handles = (letter * 32 for letter in "ZYXWVUTSRQPONMLK")
        with patch("search.services.search_session.secrets.token_urlsafe", side_effect=handles):
            original = engine.create(self.binding)
            first = engine.page(original, self.binding)
            second = engine.page(first.next_cursor, self.binding)
        with self.assertRaises(session.SearchSessionExpired):
            engine.page(original, self.binding)
        self.assertEqual(self.ids(second), [3, 4])
        replay = engine.page(first.next_cursor, self.binding)
        self.assertEqual(self.ids(replay), [3, 4])
        self.assertEqual(replay.next_cursor, second.next_cursor)

    def test_expired_lease_cannot_overwrite_recovered_checkpoint(self) -> None:
        source = FakeSource([self.candidate(pk) for pk in (1, 2, 3)])
        source.entered, source.release = Event(), Event()
        gate = source.release
        engine = self.engine({"a": source}, batch_size=3, lease_seconds=5)
        token = engine.create(self.binding)
        with ThreadPoolExecutor(max_workers=2) as pool:
            stale = pool.submit(engine.page, token, self.binding)
            self.assertTrue(source.entered.wait(5))
            self.clock.now += 6
            source.entered = None
            recovered = engine.page(token, self.binding)
            gate.set()
            with self.assertRaises(session.SearchSessionBusy):
                stale.result(timeout=5)
        replay = engine.page(token, self.binding)
        self.assertEqual(self.ids(replay), [1, 2])
        self.assertEqual(replay.next_cursor, recovered.next_cursor)
        tail = engine.page(replay.next_cursor, self.binding)
        self.assertEqual(self.ids(tail), [3])

    def test_all_filtered_sources_return_proven_empty_terminal_page(self) -> None:
        engine = self.engine({
            "a": FakeSource([self.candidate(1)]),
            "b": FakeSource([self.candidate(2)]),
        })
        self.validator.hidden.update({1, 2})
        page = engine.page(engine.create(self.binding), self.binding)
        self.assertEqual(self.ids(page), [])
        self.assertFalse(page.has_more)
        self.assertFalse(page.partial)
        self.assertIsNone(page.next_cursor)

    def test_elapsed_scan_budget_returns_resumable_unknown_without_fetch(self) -> None:
        source = FakeSource([self.candidate(1)])
        engine = self.engine({"a": source}, scan_seconds=1)
        ticks = iter((0.0, 2.0, 2.0))
        engine.monotonic = lambda: next(ticks)
        first = engine.page(engine.create(self.binding), self.binding)
        self.assertEqual(self.ids(first), [])
        self.assertIsNone(first.has_more)
        self.assertTrue(first.partial)
        self.assertEqual(source.calls, [])
        engine.monotonic = lambda: 0.0
        terminal = engine.page(first.next_cursor, self.binding)
        self.assertEqual(self.ids(terminal), [1])
        self.assertFalse(terminal.has_more)

    def test_source_snapshot_expiry_requires_explicit_session_restart(self) -> None:
        source = FakeSource([self.candidate(1)])
        source.failure = session.SearchSessionExpired()
        engine = self.engine({"a": source})
        with self.assertRaises(session.SearchSessionExpired):
            engine.page(engine.create(self.binding), self.binding)

    def test_session_expiry_during_fetch_is_expired_not_retryable_busy(self) -> None:
        """TTL 在源调用期间到期时必须要求重启，不能返回可重试的忙状态。"""
        clock = self.clock

        class SlowSource(FakeSource):
            def fetch(self, state, batch_size):
                result = super().fetch(state, batch_size)
                clock.now += 6
                return result

        engine = self.engine(
            {"a": SlowSource([self.candidate(1)])},
            ttl_seconds=5, lease_seconds=15,
        )
        token = engine.create(self.binding)
        with self.assertRaises(session.SearchSessionExpired):
            engine.page(token, self.binding)
        with self.assertRaises(session.SearchSessionExpired):
            engine.page(token, self.binding)

    def test_initial_render_revalidation_crossing_ttl_is_expired(self) -> None:
        """提交后的首次最终复验也受原绝对期限约束。"""
        source = FakeSource([self.candidate(1), self.candidate(2)])
        engine = self.engine({"a": source}, batch_size=2, ttl_seconds=5)
        self.binding = replace(self.binding, page_size=1)
        token = engine.create(self.binding)

        def slow_render(candidate):
            result = self.validator(candidate)
            if len(self.validator.calls) == 3:
                self.clock.now += 6
            return result

        engine.validator = slow_render
        with self.assertRaises(session.SearchSessionExpired):
            engine.page(token, self.binding)
        self.assertEqual(len(source.calls), 1)

    def test_replay_render_revalidation_crossing_ttl_is_expired(self) -> None:
        """重放不推进候选，但其响应复验不能越过会话寿命。"""
        source = FakeSource([self.candidate(1), self.candidate(2)])
        engine = self.engine({"a": source}, batch_size=2, ttl_seconds=5)
        self.binding = replace(self.binding, page_size=1)
        token = engine.create(self.binding)
        self.assertEqual(self.ids(engine.page(token, self.binding)), [1])

        def slow_replay(candidate):
            result = self.validator(candidate)
            self.clock.now = 1006.0
            return result

        engine.validator = slow_replay
        with self.assertRaises(session.SearchSessionExpired):
            engine.page(token, self.binding)
        self.assertEqual(len(source.calls), 1)

    def test_explicit_empty_initial_state_mapping_is_not_silently_replaced(self) -> None:
        engine = self.engine({"a": FakeSource([self.candidate(1)])})
        with self.assertRaises(ValueError):
            engine.create(self.binding, initial_states={})

    def test_malformed_token_variants_are_parameters_not_store_reads(self) -> None:
        engine = self.engine({"a": FakeSource([self.candidate(1)])})
        for token in (None, "", "bad", "bad.sig", "💥", "x" * 1025):
            with self.subTest(token_length=len(token) if token else 0):
                with self.assertRaises(session.SearchSessionTokenError):
                    engine.page(token, self.binding)


class RedisStoreBoundaryTests(unittest.TestCase):
    """只测注入客户端/编解码/失败关闭边界，不执行或仿冒真实 Redis Lua。"""

    def setUp(self) -> None:
        self.assertIsNotNone(session, "D3 分页会话内核尚未实现")

    def test_byte_hash_fields_are_decoded_and_bad_records_fail_closed(self) -> None:
        class ReadClient:
            row = {b"version": b"7", b"document": b'{"schema": 1}'}

            def hgetall(self, key):
                return self.row

        client = ReadClient()
        store = session.RedisSessionStore(client)
        value = store.read("opaque-handle")
        self.assertEqual(value.version, 7)
        self.assertEqual(value.document, {"schema": 1})
        for row in (
            {b"version": b"7", b"document": b"broken-json"},
            {b"version": b"-1", b"document": b"{}"},
            {b"version": b"7", b"document": b"[]"},
        ):
            client.row = row
            with self.assertRaises(session.SearchSessionStoreUnavailable):
                store.read("opaque-handle")
        client.row = {}
        self.assertIsNone(store.read("opaque-handle"))

    def test_client_failures_do_not_fall_back_to_unlocked_local_state(self) -> None:
        class FailedClient:
            def hgetall(self, *args):
                raise OSError("Redis unavailable")

            def eval(self, *args):
                raise OSError("Redis unavailable")

        store = session.RedisSessionStore(FailedClient())
        for operation in (
            lambda: store.read("opaque-handle"),
            lambda: store.create("opaque-handle", {"schema": 1}, 10),
            lambda: store.compare_and_swap("opaque-handle", 0, {"schema": 1}, 10),
        ):
            with self.assertRaises(session.SearchSessionStoreUnavailable):
                operation()


if __name__ == "__main__":
    unittest.main()
