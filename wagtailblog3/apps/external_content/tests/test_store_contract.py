"""以无网络仓储替身验证身份围栏，禁止测试触达真实正文集合。"""

from copy import deepcopy
from typing import Any
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
from pymongo.errors import (
    AutoReconnect,
    OperationFailure,
    DuplicateKeyError,
    ServerSelectionTimeoutError,
    WriteConcernError,
)

from external_content.exceptions import (
    ContentStoreError,
    ContentVersionIdentityMismatchError,
    ContentVersionNotFoundError,
    CorruptedContentPayloadError,
    ExternalContentOperationBlockedError,
)
from external_content.store import (
    ContentVersionStore,
    compute_canonical_hash,
    get_collection,
    get_mongo_settings,
    validate_identity,
)


class StoreIdentityContractTests(unittest.TestCase):
    """仓储身份必须强类型完整，物理回收必须携带原始身份证据。"""

    def setUp(self) -> None:
        """准备无数据库连接的不可变文档替身。"""
        self.payload: list[dict[str, Any]] = [
            {"id": "block-1", "type": "markdown_block", "value": "# 原始正文\n"}
        ]
        self.identity: dict[str, Any] = {
            "model_label": "guides.guidepage",
            "page_id": 1,
            "field_name": "body",
            "codec": "streamfield.v1",
            "schema_version": 1,
            "payload_sha256": compute_canonical_hash(self.payload),
        }
        self.document = dict(self.identity, version_id="version-1", payload=self.payload)
        self.collection = MagicMock()
        self.collection.with_options.return_value = self.collection
        self.collection.find_one.return_value = deepcopy(self.document)
        self.collection.delete_one.return_value.deleted_count = 1
        self.store = ContentVersionStore(
            collection=self.collection,
            enforce_index_check=False,
            write_concern=WriteConcern(w=1),
            read_concern=ReadConcern("local"),
        )
        self.store.check_indexes_ready = MagicMock()

    def test_boolean_expected_page_identity_is_rejected(self) -> None:
        """True 不得利用 Python 等值比较冒充 page_id=1。"""
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.get_version("version-1", dict(self.identity, page_id=True))
        self.collection.find_one.assert_not_called()

    def test_float_expected_schema_identity_is_rejected(self) -> None:
        """1.0 不得冒充整数 schema_version=1。"""
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.get_version("version-1", dict(self.identity, schema_version=1.0))
        self.collection.find_one.assert_not_called()

    def test_boolean_stored_identity_is_rejected(self) -> None:
        """库中损坏的布尔身份即使等值也必须失败关闭。"""
        self.collection.find_one.return_value["page_id"] = True
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.get_version("version-1", self.identity)

    def test_gc_deletion_requires_full_identity(self) -> None:
        """裸版本号不能作为正文物理删除授权。"""
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.delete_version("version-1", expected_identity={})
        self.collection.delete_one.assert_not_called()

    def test_gc_deletion_uses_exact_identity_selector(self) -> None:
        """核验后仍以完整身份限定删除，防止核验与删除间文档替换。"""
        deleted = self.store.delete_version(
            "version-1", expected_identity=self.identity
        )
        self.assertTrue(deleted)
        self.collection.delete_one.assert_called_once_with(
            dict(self.identity, version_id="version-1")
        )

    def test_gc_missing_version_is_idempotent(self) -> None:
        """崩溃重试时已删除版本仅返回未执行删除，不误删其他记录。"""
        self.collection.find_one.return_value = None
        self.assertFalse(
            self.store.delete_version("version-1", expected_identity=self.identity)
        )
        self.collection.delete_one.assert_not_called()

    def test_gc_deletion_mismatched_identity_is_rejected(self) -> None:
        """若库中文档与 expected_identity 身份不符，身份读验必须阻断删除。"""
        mismatched_doc = deepcopy(self.document)
        mismatched_doc["page_id"] = 999
        self.collection.find_one.return_value = mismatched_doc

        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.delete_version("version-1", expected_identity=self.identity)
        self.collection.delete_one.assert_not_called()

    def test_gc_deletion_rejects_empty_or_non_string_version_id(self) -> None:
        """delete_version 严禁传入空字符串或非字符串 version_id。"""
        for bad_vid in ("", "   ", None, 123):
            with self.subTest(vid=bad_vid):
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.delete_version(bad_vid, expected_identity=self.identity)  # type: ignore[arg-type]
        self.collection.find_one.assert_not_called()
        self.collection.delete_one.assert_not_called()

    def test_synchronous_orphan_cleanup_is_forbidden(self) -> None:
        """SQL 回滚留下的版本不能绕过安全期被同步补偿删除。"""
        with self.assertRaises(ExternalContentOperationBlockedError):
            self.store.cleanup_unreferenced_version("version-1", 1)
        self.collection.delete_one.assert_not_called()

    def test_save_version_rejects_boolean_page_id(self) -> None:
        """save_version 严禁使用 True 代替整数 page_id。"""
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.save_version(
                model_label="guides.guidepage",
                page_id=True,  # type: ignore[arg-type]
                field_name="body",
                codec="streamfield.v1",
                schema_version=1,
                payload_sha256=self.identity["payload_sha256"],
                payload=self.payload,
            )
        self.collection.insert_one.assert_not_called()

    def test_save_version_rejects_float_schema_version(self) -> None:
        """save_version 严禁使用 1.0 代替整数 schema_version。"""
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.save_version(
                model_label="guides.guidepage",
                page_id=1,
                field_name="body",
                codec="streamfield.v1",
                schema_version=1.0,  # type: ignore[arg-type]
                payload_sha256=self.identity["payload_sha256"],
                payload=self.payload,
            )
        self.collection.insert_one.assert_not_called()

    def test_save_version_rejects_invalid_model_label(self) -> None:
        """save_version 拒绝不规范的大写或非 app.model 格式 model_label。"""
        for invalid_label in ("GuidePage", "guides..guidepage", "guides/guidepage", "GUIDES.GUIDEPAGE", ""):
            with self.subTest(model_label=invalid_label):
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.save_version(
                        model_label=invalid_label,
                        page_id=1,
                        field_name="body",
                        codec="streamfield.v1",
                        schema_version=1,
                        payload_sha256=self.identity["payload_sha256"],
                        payload=self.payload,
                    )
        self.collection.insert_one.assert_not_called()

    def test_save_version_rejects_invalid_sha256(self) -> None:
        """save_version 拒绝非 64 位小写十六进制的 payload_sha256。"""
        for invalid_sha in ("bad_hash", "A" * 64, "123456", ""):
            with self.subTest(sha=invalid_sha):
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.save_version(
                        model_label="guides.guidepage",
                        page_id=1,
                        field_name="body",
                        codec="streamfield.v1",
                        schema_version=1,
                        payload_sha256=invalid_sha,
                        payload=self.payload,
                    )
        self.collection.insert_one.assert_not_called()

    def test_duplicate_key_reuse_validates_stored_identity_and_version_id(self) -> None:
        """DuplicateKey 冲突复用时必须验证库中文档的完整 6 身份及有效 version_id。"""
        self.collection.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error")
        corrupted_doc = deepcopy(self.document)
        corrupted_doc["page_id"] = True
        self.collection.find_one.return_value = corrupted_doc

        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.save_version(**dict(self.identity, payload=self.payload))

    def test_duplicate_key_reuse_validates_payload_hash_integrity(self) -> None:
        """DuplicateKey 冲突复用时若库中文档 payload 与 sha256 不符，抛出 CorruptedContentPayloadError。"""
        self.collection.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error")
        corrupted_doc = deepcopy(self.document)
        corrupted_doc["payload"] = [{"id": "bad", "type": "paragraph", "value": "篡改正文"}]
        self.collection.find_one.return_value = corrupted_doc

        with self.assertRaises(CorruptedContentPayloadError):
            self.store.save_version(**dict(self.identity, payload=self.payload))

    def test_mongo_exception_sanitizes_credentials_and_preserves_causality(self) -> None:
        """Mongo 异常脱敏：不泄露底层异常字符串或凭据，输出有限分类码，并保留异常因果。"""
        sensitive_error_msg = "connection failure to mongodb://app_user:s3cr3t_pwd@10.0.0.1:27017/?replicaSet=rs0 timed out"
        raw_exc = ServerSelectionTimeoutError(sensitive_error_msg)
        self.collection.insert_one.side_effect = raw_exc

        with self.assertRaises(ContentStoreError) as ctx:
            self.store.save_version(**dict(self.identity, payload=self.payload))

        exc = ctx.exception
        self.assertIs(exc.__cause__, raw_exc)
        self.assertNotIn("s3cr3t_pwd", str(exc))
        self.assertNotIn("10.0.0.1", str(exc))
        self.assertNotIn(sensitive_error_msg, str(exc))
        self.assertNotIn("s3cr3t_pwd", str(exc.details))
        self.assertEqual(exc.details.get("error_category"), "network_timeout")

    def test_write_concern_error_sanitizes_details_and_preserves_causality(self) -> None:
        """WriteConcernError 异常脱敏：分类为 write_concern_error，保留因果且脱敏。"""
        raw_wc_err = WriteConcernError("write concern majority timeout on node 192.168.1.50")
        self.collection.insert_one.side_effect = raw_wc_err

        with self.assertRaises(ContentStoreError) as ctx:
            self.store.save_version(**dict(self.identity, payload=self.payload))

        exc = ctx.exception
        self.assertIs(exc.__cause__, raw_wc_err)
        self.assertNotIn("192.168.1.50", str(exc))
        self.assertNotIn("192.168.1.50", str(exc.details))
        self.assertEqual(exc.details.get("error_category"), "write_concern_error")

    def test_save_version_success_without_django_settings(self) -> None:
        """纯无网络环境下 save_version 正常生成版本并写入，无需 Django settings。"""
        version_id = self.store.save_version(**dict(self.identity, payload=self.payload))
        self.assertTrue(bool(version_id))
        self.collection.insert_one.assert_called_once()
        inserted_doc = self.collection.insert_one.call_args[0][0]
        self.assertEqual(inserted_doc["version_id"], version_id)
        self.assertEqual(inserted_doc["model_label"], self.identity["model_label"])
        self.assertEqual(inserted_doc["page_id"], self.identity["page_id"])
        self.assertEqual(inserted_doc["payload_sha256"], self.identity["payload_sha256"])
        self.assertIsNotNone(inserted_doc.get("created_at"))

    def test_duplicate_key_reuse_success_without_corruption(self) -> None:
        """DuplicateKey 冲突复用：底层文档 6 身份及哈希完好时，成功幂等复用已存 version_id。"""
        self.collection.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error")
        existing_doc = deepcopy(self.document)
        self.collection.find_one.return_value = existing_doc

        vid, is_new = self.store.save_version_with_status(**dict(self.identity, payload=self.payload))
        self.assertEqual(vid, "version-1")
        self.assertFalse(is_new)

    def test_operation_failure_sanitizes_and_preserves_causality(self) -> None:
        """OperationFailure 异常脱敏：分类为 operation_failure，保留因果且不泄露凭据。"""
        sensitive_msg = "command find failed: auth failed for admin:pass123 on shard01"
        raw_exc = OperationFailure(sensitive_msg)
        self.collection.insert_one.side_effect = raw_exc

        with self.assertRaises(ContentStoreError) as ctx:
            self.store.save_version(**dict(self.identity, payload=self.payload))

        exc = ctx.exception
        self.assertIs(exc.__cause__, raw_exc)
        self.assertNotIn("pass123", str(exc))
        self.assertNotIn(sensitive_msg, str(exc))
        self.assertEqual(exc.details.get("error_category"), "operation_failure")

    def test_identity_rejects_non_dict_and_missing_keys(self) -> None:
        """身份校验器严格拒绝非 dict 对象及缺少 6 身份中任何一项的残缺字典。"""
        for bad_identity in ("not_a_dict", [1, 2, 3], None):
            with self.subTest(bad_identity=bad_identity):
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.get_version("version-1", expected_identity=bad_identity)  # type: ignore[arg-type]

        for missing_key in self.identity.keys():
            incomplete = {k: v for k, v in self.identity.items() if k != missing_key}
            with self.subTest(missing_key=missing_key):
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.get_version("version-1", expected_identity=incomplete)

    def test_nan_and_inf_identity_rejected(self) -> None:
        """page_id 与 schema_version 严格拒绝 NaN、Infinity 与 -Infinity 浮点特殊值。"""
        for bad_num in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad_page_id=bad_num):
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.get_version("version-1", expected_identity=dict(self.identity, page_id=bad_num))
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.delete_version("version-1", expected_identity=dict(self.identity, page_id=bad_num))
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.save_version(
                        model_label="guides.guidepage",
                        page_id=bad_num,  # type: ignore[arg-type]
                        field_name="body",
                        codec="streamfield.v1",
                        schema_version=1,
                        payload_sha256=self.identity["payload_sha256"],
                        payload=self.payload,
                    )
            with self.subTest(bad_schema_version=bad_num):
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.get_version("version-1", expected_identity=dict(self.identity, schema_version=bad_num))
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.delete_version("version-1", expected_identity=dict(self.identity, schema_version=bad_num))
                with self.assertRaises(ContentVersionIdentityMismatchError):
                    self.store.save_version(
                        model_label="guides.guidepage",
                        page_id=1,
                        field_name="body",
                        codec="streamfield.v1",
                        schema_version=bad_num,  # type: ignore[arg-type]
                        payload_sha256=self.identity["payload_sha256"],
                        payload=self.payload,
                    )

    def test_duplicate_key_reuse_validates_version_id_is_non_empty_str(self) -> None:
        """DuplicateKey 冲突复用时，底层文档的 version_id 若缺失、为空或非字符串必须抛出 ContentStoreError 阻断。"""
        self.collection.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error")
        for invalid_vid in ("", "   ", None, 12345, []):
            with self.subTest(invalid_vid=invalid_vid):
                corrupted_doc = deepcopy(self.document)
                corrupted_doc["version_id"] = invalid_vid
                self.collection.find_one.return_value = corrupted_doc
                with self.assertRaises(ContentStoreError):
                    self.store.save_version(**dict(self.identity, payload=self.payload))

    def test_duplicate_key_find_one_mongo_exception_sanitizes_and_preserves_causality(self) -> None:
        """DuplicateKey 冲突复用查询若发生 Mongo 异常，脱敏凭据并保留 __cause__。"""
        self.collection.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error")
        sensitive_msg = "connection refused to mongo://admin:super_secret@10.0.0.99:27017"
        raw_exc = AutoReconnect(sensitive_msg)
        self.collection.find_one.side_effect = raw_exc

        with self.assertRaises(ContentStoreError) as ctx:
            self.store.save_version(**dict(self.identity, payload=self.payload))

        exc = ctx.exception
        self.assertIs(exc.__cause__, raw_exc)
        self.assertNotIn("super_secret", str(exc))
        self.assertNotIn("10.0.0.99", str(exc))
        self.assertEqual(exc.details.get("error_category"), "network_timeout")

    def test_get_mongo_settings_explicitly_fails_on_misconfigured_settings(self) -> None:
        """Django settings 已配置但缺失或格式错误的 MONGO_DB 必须显式失败，未配置时安全返回空字典。"""
        fake_settings = MagicMock()
        fake_settings.configured = True

        # 1. 缺失 MONGO_DB 属性
        del fake_settings.MONGO_DB
        with patch("external_content.store.settings", fake_settings):
            with self.assertRaises(ContentStoreError) as ctx:
                get_mongo_settings()
            self.assertIn("MONGO_DB", str(ctx.exception))

        # 2. MONGO_DB 不是字典 (例如 None, 字符串或列表)
        for bad_val in (None, "mongodb://localhost", [1, 2]):
            fake_settings.MONGO_DB = bad_val
            with patch("external_content.store.settings", fake_settings):
                with self.assertRaises(ContentStoreError) as ctx:
                    get_mongo_settings()
                self.assertIn("MONGO_DB", str(ctx.exception))

        # 3. settings 未配置时 (离线纯单测)，安全返回空字典
        unconfigured_settings = MagicMock()
        unconfigured_settings.configured = False
        with patch("external_content.store.settings", unconfigured_settings):
            res = get_mongo_settings()
            self.assertEqual(res, {})

    def test_delete_version_mongo_exception_sanitizes_and_preserves_causality(self) -> None:
        """delete_version 底层若触发 Mongo 异常，脱敏凭据并保留 __cause__。"""
        sensitive_err = "failed to delete on cluster rs0 with auth secret_token_xyz"
        raw_exc = ServerSelectionTimeoutError(sensitive_err)
        self.collection.find_one.side_effect = raw_exc

        with self.assertRaises(ContentStoreError) as ctx:
            self.store.delete_version("version-1", expected_identity=self.identity)

        exc = ctx.exception
        self.assertIs(exc.__cause__, raw_exc)
        self.assertNotIn("secret_token_xyz", str(exc))
        self.assertNotIn(sensitive_err, str(exc))
        self.assertEqual(exc.details.get("error_category"), "network_timeout")

    def test_invalid_identity_error_and_details_do_not_leak_sensitive_values(self) -> None:
        """???? identity ??????????? details ????????????????"""
        sensitive_cases = [
            ("model_label", "secret_model_token/leak"),
            ("page_id", -999999),
            ("field_name", "   "),
            ("codec", "   "),
            ("schema_version", -888),
            ("payload_sha256", "invalid_secret_sha256_leak"),
        ]
        for field, bad_value in sensitive_cases:
            bad_identity = dict(self.identity, **{field: bad_value})
            with self.assertRaises(ContentVersionIdentityMismatchError) as ctx:
                validate_identity(bad_identity, context="test_context")
            exc = ctx.exception
            self.assertNotIn(str(bad_value), str(exc))
            self.assertNotIn("value", exc.details)
            self.assertEqual(exc.details.get("error_category"), "identity_mismatch")
            self.assertEqual(exc.details.get("field"), field)

    def test_identity_mismatch_error_and_details_do_not_leak_expected_or_actual_values(self) -> None:
        """get_version 与 delete_version 交叉核验失败时，严禁在错误信息与 details 中回显 expected/actual 值。"""
        sensitive_expected = "guides.sensitive_expected_label"
        sensitive_stored = "guides.sensitive_stored_label"

        # 1. get_version 错配核验
        self.collection.find_one.return_value = dict(self.document, model_label=sensitive_stored)
        with self.assertRaises(ContentVersionIdentityMismatchError) as ctx_get:
            self.store.get_version("version-1", expected_identity=dict(self.identity, model_label=sensitive_expected))
        exc_get = ctx_get.exception
        self.assertNotIn(sensitive_expected, str(exc_get))
        self.assertNotIn(sensitive_stored, str(exc_get))
        self.assertNotIn("expected", exc_get.details)
        self.assertNotIn("actual", exc_get.details)
        self.assertEqual(exc_get.details.get("error_category"), "identity_mismatch")
        self.assertEqual(exc_get.details.get("field"), "model_label")

        # 2. delete_version 错配核验
        with self.assertRaises(ContentVersionIdentityMismatchError) as ctx_del:
            self.store.delete_version("version-1", expected_identity=dict(self.identity, model_label=sensitive_expected))
        exc_del = ctx_del.exception
        self.assertNotIn(sensitive_expected, str(exc_del))
        self.assertNotIn(sensitive_stored, str(exc_del))
        self.assertNotIn("expected", exc_del.details)
        self.assertNotIn("actual", exc_del.details)
        self.assertEqual(exc_del.details.get("error_category"), "identity_mismatch")
        self.assertEqual(exc_del.details.get("field"), "model_label")

    def test_settings_read_failure_fails_closed_and_sanitizes_with_cause(self) -> None:
        """settings 配置读取异常时必须失败关闭，错误码归类为 settings_error，保留 __cause__ 且不泄露凭据。"""
        fake_settings = MagicMock()
        fake_settings.configured = True
        sensitive_err = "failed auth mongo://admin:super_secret_pw@192.168.1.50:27017"
        raw_exc = RuntimeError(sensitive_err)
        type(fake_settings).MONGO_DB = PropertyMock(side_effect=raw_exc)

        with patch("external_content.store.settings", fake_settings):
            with self.assertRaises(ContentStoreError) as ctx:
                get_mongo_settings()
            exc = ctx.exception
            self.assertIs(exc.__cause__, raw_exc)
            self.assertNotIn("super_secret_pw", str(exc))
            self.assertNotIn("192.168.1.50", str(exc))
            self.assertEqual(exc.details.get("error_category"), "settings_error")

    def test_canonical_hash_and_save_classify_nan_inf_and_unserializable_payload(self) -> None:
        """NaN、Infinity 及不可序列化 payload 在 compute_canonical_hash 与 save_version 中分类为 invalid_payload 并保留 cause。"""
        invalid_payloads = [
            {"nan_val": float("nan")},
            {"inf_val": float("inf")},
            {"neg_inf_val": float("-inf")},
            {"unserializable": object()},
        ]
        for bad_payload in invalid_payloads:
            with self.assertRaises(ContentStoreError) as ctx:
                compute_canonical_hash(bad_payload)
            exc = ctx.exception
            self.assertEqual(exc.details.get("error_category"), "invalid_payload")
            self.assertIsInstance(exc.__cause__, (ValueError, TypeError))

            with self.assertRaises(ContentStoreError) as ctx_save:
                self.store.save_version(**dict(self.identity, payload=bad_payload))
            self.assertEqual(ctx_save.exception.details.get("error_category"), "invalid_payload")

    def test_validate_identity_rejects_trailing_newlines_and_control_chars(self) -> None:
        """validate_identity 必须严格拒绝尾部换行符、回车符及控制字符。"""
        bad_identities = [
            dict(self.identity, model_label="guides.guidepage" + chr(10)),
            dict(self.identity, model_label="guides.guidepage" + chr(13)),
            dict(self.identity, payload_sha256=("a" * 64) + chr(10)),
            dict(self.identity, payload_sha256=("a" * 64) + chr(13)),
            dict(self.identity, field_name="body" + chr(10)),
            dict(self.identity, field_name="body" + chr(0)),
            dict(self.identity, codec="streamfield.v1" + chr(10)),
            dict(self.identity, codec="streamfield.v1" + chr(9)),
        ]
        for bad_id in bad_identities:
            with self.assertRaises(ContentVersionIdentityMismatchError):
                validate_identity(bad_id)

    def test_get_mongo_settings_fail_closed_on_invalid_or_incomplete_dict(self) -> None:
        """当 settings.configured 为 True 时，MONGO_DB 内部字段缺失、类型错误或非法端口必须失败关闭。"""
        fake_settings = MagicMock()
        fake_settings.configured = True

        bad_configs = [
            {},
            {"NAME": "test", "HOST": "localhost"},
            {"NAME": "test", "PORT": 27017},
            {"HOST": "localhost", "PORT": 27017},
            {"NAME": "", "HOST": "localhost", "PORT": 27017},
            {"NAME": "test", "HOST": "", "PORT": 27017},
            {"NAME": "test", "HOST": "localhost", "PORT": True},
            {"NAME": "test", "HOST": "localhost", "PORT": "27017"},
            {"NAME": "test", "HOST": "localhost", "PORT": 70000},
            {"NAME": "test", "HOST": "localhost", "PORT": 0},
            {"NAME": "test", "HOST": "localhost", "PORT": -1},
        ]
        for bad_cfg in bad_configs:
            fake_settings.MONGO_DB = bad_cfg
            with patch("external_content.store.settings", fake_settings):
                with self.assertRaises(ContentStoreError) as ctx:
                    get_mongo_settings()
                self.assertEqual(ctx.exception.details.get("error_category"), "settings_error")

    def test_all_exceptions_and_details_do_not_leak_hashes_version_ids_or_identities(self) -> None:
        """全链路异常及 details 字典中严禁泄露原始 hash、version_id 或敏感身份值。"""
        secret_hash = "f" * 64
        secret_vid = "secret-version-uuid-12345"

        # 1. save_version 哈希不匹配
        with self.assertRaises(ContentStoreError) as ctx_hash:
            self.store.save_version(**dict(self.identity, payload_sha256=secret_hash, payload=self.payload))
        exc_str = str(ctx_hash.exception)
        self.assertNotIn(secret_hash, exc_str)
        self.assertNotIn(self.identity["payload_sha256"], exc_str)
        self.assertNotIn(secret_hash, str(ctx_hash.exception.details))

        # 2. get_version 版本未找到
        self.collection.find_one.return_value = None
        with self.assertRaises(ContentVersionNotFoundError) as ctx_nf:
            self.store.get_version(version_id=secret_vid, expected_identity=self.identity)
        self.assertNotIn(secret_vid, str(ctx_nf.exception))
        self.assertNotIn(secret_vid, str(ctx_nf.exception.details))

        # 3. get_version 发现 payload 损坏
        tampered_doc = dict(self.identity, version_id=secret_vid, payload=[{"tampered": True}])
        self.collection.find_one.return_value = tampered_doc
        with self.assertRaises(CorruptedContentPayloadError) as ctx_corrupt:
            self.store.get_version(version_id=secret_vid, expected_identity=self.identity)
        self.assertNotIn(secret_vid, str(ctx_corrupt.exception))
        self.assertNotIn(secret_vid, str(ctx_corrupt.exception.details))
        self.assertNotIn("computed_hash", ctx_corrupt.exception.details)
        self.assertNotIn("stored_hash", ctx_corrupt.exception.details)

        # 4. delete_version 传入非法 version_id
        with self.assertRaises(ContentVersionIdentityMismatchError) as ctx_del:
            self.store.delete_version(version_id="", expected_identity=self.identity)
        self.assertNotIn("version_id", ctx_del.exception.details.get("version_id", ""))
