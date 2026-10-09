"""ID-01～06：可信身份、状态转移与固定截止时间，无真实缓存 IO。"""

import copy
from threading import RLock
from unittest import TestCase

from external_content.idempotency import GuardError, IdempotencyService


class MemoryStore:
    """模拟原子接口，不声称测试了 Redis Lua。"""

    def __init__(self) -> None:
        self.records = {}
        self.lock = RLock()
        self.break_cas = False

    def create(self, identity, record, now):
        with self.lock:
            if identity in self.records:
                return False
            self.records[identity] = copy.deepcopy(record)
            return True

    def read(self, identity):
        return copy.deepcopy(self.records.get(identity))

    def cas(self, identity, expected, replacement, now):
        with self.lock:
            if self.break_cas:
                raise RuntimeError("cache unavailable")
            if self.records.get(identity) != expected:
                return False
            self.records[identity] = copy.deepcopy(replacement)
            return True


class TestIdempotency(TestCase):
    def test_malformed_signed_payload_is_rejected_without_internal_error(self):
        """即使签名有效，缺字段/非法身份也不得造成 500 或访问另一存储键。"""
        for payload in [
            {"purpose": "execute", "id": "a" * 32},
            {"purpose": "execute", "id": "z" * 32, "user": 3, "scope": self.scope,
             "binding": ""},
            {"purpose": "execute", "id": "a" * 32, "user": 3, "scope": None},
        ]:
            with self.subTest():
                token = self.service.signer.sign_object(payload)
                try:
                    self.service.claim(token, 3, self.scope, "body-A")
                except Exception as exc:
                    self.assertIsInstance(exc, GuardError)
                    self.assertEqual(exc.code, "token")
                else:
                    self.fail("损坏载体未阻断")
        self.assertEqual(self.store.records, {})

    def test_corrupt_record_is_blocked_without_internal_error_or_claim(self):
        """字段缺失、状态或时限损坏只可待核对，不执行、不领取、不重建。"""
        _, token = self.issue()
        identity, initial = next(iter(self.store.records.items()))
        variants = [{}, dict(initial, retain_until="invalid"),
                    dict(initial, token_deadline=float("nan")),
                    dict(initial, state="unexpected"), dict(initial, version=True),
                    dict(initial, scope=[]), dict(initial, binding=None),
                    dict(initial, owner=[]), dict(initial, result=[]),
                    dict(initial, state=[]), dict(initial, scope={"kind": []})]
        for record in variants:
            with self.subTest():
                self.store.records[identity] = copy.deepcopy(record)
                try:
                    self.service.claim(token, 3, self.scope, "body-A")
                except Exception as exc:
                    self.assertIsInstance(exc, GuardError)
                    self.assertEqual(exc.code, "unknown")
                else:
                    self.fail("损坏记录未阻断")
                current = self.store.records[identity]
                self.assertNotEqual(current.get("state"), "processing")
                self.assertEqual(len(self.store.records), 1)

    def setUp(self) -> None:
        self.now = 10000.0
        self.store = MemoryStore()
        self.service = IdempotencyService(self.store, clock=lambda: self.now)
        self.scope = {"kind": "edit", "page": 7}

    def issue(self, digest="body-A"):
        ticket = self.service.reserve(3, self.scope)
        self.assertIsNotNone(ticket, "提交准备身份未签发")
        return ticket, self.service.prepare(ticket, 3, self.scope, digest)

    def claim(self):
        ticket, token = self.issue()
        return token, self.service.claim(token, 3, self.scope, "body-A")

    def error(self, code, function, *args):
        with self.assertRaises(GuardError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_double_prepare_returns_same_execution_identity(self):
        ticket, token = self.issue()
        self.assertEqual(self.service.prepare(ticket, 3, self.scope, "body-A"), token)
        self.assertEqual(len(self.store.records), 1)

    def test_changed_snapshot_cannot_issue_second_identity(self):
        ticket, _ = self.issue()
        self.error("binding", self.service.prepare, ticket, 3, self.scope, "body-B")
        self.assertEqual(len(self.store.records), 1)

    def test_changed_user_rejected(self):
        _, token = self.issue()
        self.error("binding", self.service.claim, token, 4, self.scope, "body-A")

    def test_changed_parent_rejected_before_second_key(self):
        _, token = self.issue()
        self.error("binding", self.service.claim, token, 3, {"kind": "edit", "page": 8}, "body-A")
        self.assertEqual(len(self.store.records), 1)

    def test_tampered_token_rejected(self):
        _, token = self.issue()
        self.error("token", self.service.claim, token + "x", 3, self.scope, "body-A")

    def test_processing_duplicate_never_receives_owner(self):
        token, owner = self.claim()
        self.error("processing", self.service.claim, token, 3, self.scope, "body-A")
        self.assertEqual(self.store.read(owner.identity)["version"], 3)

    def test_success_replay_returns_minimal_result(self):
        token, owner = self.claim()
        self.assertTrue(self.service.complete(owner, {"page": 7, "redirect": "/admin/"}))
        replay = self.service.claim(token, 3, self.scope, "body-A")
        self.assertTrue(replay.replay)
        self.assertEqual(replay.record["result"], {"page": 7, "redirect": "/admin/"})

    def test_token_thirty_minute_boundary(self):
        _, token = self.issue()
        self.now += 1800
        self.error("expired", self.service.claim, token, 3, self.scope, "body-A")

    def test_preparation_starts_validity_not_get_reservation(self):
        ticket = self.service.reserve(3, self.scope)
        self.assertIsNotNone(ticket, "提交准备身份未签发")
        self.now += 3600
        token = self.service.prepare(ticket, 3, self.scope, "body-A")
        self.now += 1799
        self.assertFalse(self.service.claim(token, 3, self.scope, "body-A").replay)

    def test_result_retention_fixed_from_preparation(self):
        ticket = self.service.reserve(3, self.scope)
        self.assertIsNotNone(ticket, "提交准备身份未签发")
        self.now += 3600
        token = self.service.prepare(ticket, 3, self.scope, "body-A")
        owner = self.service.claim(token, 3, self.scope, "body-A")
        retain = self.now + 86400
        self.now += 10
        self.service.complete(owner, {"page": 7})
        self.assertEqual(self.store.read(owner.identity)["retain_until"], retain)

    def test_processing_timeout_only_marks_unknown(self):
        token, owner = self.claim()
        self.now += 300
        self.error("unknown", self.service.claim, token, 3, self.scope, "body-A")
        self.assertEqual(self.store.read(owner.identity)["state"], "unknown")

    def test_late_owner_cannot_overwrite_unknown(self):
        _, owner = self.claim()
        self.now += 300
        self.assertFalse(self.service.complete(owner, {"page": 7}))
        self.assertEqual(self.store.read(owner.identity)["state"], "unknown")

    def test_lost_record_is_not_recreated_by_prepare(self):
        ticket, _ = self.issue()
        self.store.records.clear()
        self.error("unknown", self.service.prepare, ticket, 3, self.scope, "body-A")
        self.assertEqual(self.store.records, {})

    def test_lost_record_is_not_recreated_by_execution(self):
        token, _ = self.claim()
        self.store.records.clear()
        self.error("unknown", self.service.claim, token, 3, self.scope, "body-A")

    def test_proven_rollback_retry_has_new_owner(self):
        token, first = self.claim()
        self.assertTrue(self.service.fail(first, rollback_proven=True))
        second = self.service.claim(token, 3, self.scope, "body-A")
        self.assertNotEqual(first.record["owner"], second.record["owner"])
        self.assertFalse(self.service.complete(first, {"page": 7}))

    def test_uncertain_failure_never_retries(self):
        token, owner = self.claim()
        self.service.fail(owner, rollback_proven=False)
        self.error("unknown", self.service.claim, token, 3, self.scope, "body-A")

    def test_commit_cache_failure_does_not_raise_or_undo_business(self):
        _, owner = self.claim()
        self.store.break_cas = True
        self.assertFalse(self.service.complete(owner, {"page": 7}))
        self.assertEqual(self.store.read(owner.identity)["state"], "processing")

    def test_record_contains_no_request_or_signed_carrier(self):
        ticket, token = self.issue()
        record = next(iter(self.store.records.values()))
        text = str(record)
        self.assertNotIn(ticket, text)
        self.assertNotIn(token, text)
        self.assertNotIn("正文私密", text)

    def test_unknown_inspection_never_claims(self):
        token, owner = self.claim()
        self.now += 301
        self.assertEqual(self.service.status(token, 3)["state"], "unknown")
        self.assertEqual(self.store.read(owner.identity)["owner"], owner.record["owner"])

    def test_corrupt_succeeded_json_result_is_not_replayed(self):
        token, owner = self.claim()
        initial = self.store.read(owner.identity)
        for result in [
            {"page": True}, {"page": 0}, {"page": 7, "json": []},
            {"page": 7, "json": True}, {"page": 7, "json": True, "revision": False},
            {"page": 7, "json": True, "revision": 11, "created": [],
             "field_updates": {}},
            {"page": 7, "json": True, "revision": 11, "created": False,
             "field_updates": {"title": "正文"}},
        ]:
            with self.subTest(result=result):
                self.store.records[owner.identity] = dict(initial, state="succeeded", result=result)
                self.error("unknown", self.service.claim, token, 3, self.scope, "body-A")

    def test_reservation_expiry_and_result_expiry_are_fixed_boundaries(self):
        ticket = self.service.reserve(3, self.scope)
        self.now += 86400
        self.error("expired", self.service.prepare, ticket, 3, self.scope, "body-A")
        self.assertEqual(len(self.store.records), 1)
        _, token = self.issue()
        owner = self.service.claim(token, 3, self.scope, "body-A")
        self.service.complete(owner, {"page": 7})
        self.now += 86400
        self.error("unknown", self.service.status, token, 3)

    def test_status_preserves_minimal_authorization_requirement(self):
        _, token = self.issue()
        identity, record = next(iter(self.store.records.items()))
        self.store.records[identity] = dict(record, authorization={"publish": True})
        self.assertEqual(self.service.status(token, 3).get("authorization"), {"publish": True})

    def test_corrupt_authorization_is_not_consumed(self):
        _, token = self.issue()
        identity, initial = next(iter(self.store.records.items()))
        for authorization in ([], {}, {"publish": "yes"},
                              {"publish": False, "copy_parent": True}):
            with self.subTest():
                self.store.records[identity] = dict(initial, authorization=authorization)
                self.error("unknown", self.service.status, token, 3)

    def test_copy_prepare_requires_resolved_parent_without_consuming_reservation(self) -> None:
        """非执行预留尚无复制目标；签发执行身份时目标必须明确。"""
        scope = {"kind": "copy", "page": 7}
        ticket = self.service.reserve(3, scope)
        self.assertEqual(next(iter(self.store.records.values()))["state"], "reserved")
        identity, initial = next(iter(self.store.records.items()))
        for authorization in (None, {"publish": False}, {"publish": True}):
            with self.subTest(authorization=authorization):
                self.store.records[identity] = copy.deepcopy(initial)
                with self.assertRaises(GuardError) as caught:
                    self.service.prepare(ticket, 3, scope, "body-A", authorization=authorization)
                self.assertEqual(caught.exception.code, "binding")
                self.assertEqual(next(iter(self.store.records.values()))["state"], "reserved")
                self.assertEqual(len(self.store.records), 1)

    def test_copy_status_and_claim_reject_missing_parent_in_executable_record(self) -> None:
        """丢失目标授权证据不能回退到源父页，也不能领取或重放。"""
        scope = {"kind": "copy", "page": 7}
        ticket = self.service.reserve(3, scope)
        token = self.service.prepare(ticket, 3, scope, "body-A",
                                     authorization={"publish": True, "copy_parent": 42})
        identity, initial = next(iter(self.store.records.items()))
        for state in ("issued", "processing", "succeeded", "failed", "unknown"):
            with self.subTest(state=state):
                record = dict(initial, state=state, authorization={"publish": False},
                              result={"page": 9} if state == "succeeded" else None)
                self.store.records[identity] = copy.deepcopy(record)
                self.error("unknown", self.service.status, token, 3)
                self.error("unknown", self.service.claim, token, 3, scope, "body-A")
                self.assertEqual(self.store.read(identity), record)

    def test_copy_authorization_survives_success_and_cannot_change_on_prepare(self) -> None:
        scope = {"kind": "copy", "page": 7}
        ticket = self.service.reserve(3, scope)
        authorization = {"publish": True, "copy_parent": 42}
        token = self.service.prepare(ticket, 3, scope, "body-A", authorization=authorization)
        authorization["copy_parent"] = 43
        with self.assertRaises(GuardError) as caught:
            self.service.prepare(ticket, 3, scope, "body-A", authorization=authorization)
        self.assertEqual(caught.exception.code, "binding")
        owner = self.service.claim(token, 3, scope, "body-A")
        self.assertTrue(self.service.complete(owner, {"page": 9}))
        self.assertEqual(self.service.status(token, 3)["authorization"],
                         {"publish": True, "copy_parent": 42})
        self.assertTrue(self.service.claim(token, 3, scope, "body-A").replay)
