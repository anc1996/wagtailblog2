import pymongo
from unittest.mock import patch
"""
Wagtail Page 正文外置通用协议 - ContentVersionStore 不可变仓储定向测试。

验证：
1. 不可变版本保存与唯一 version_id 生成；
2. 相同归属与正文幂等复用已有版本；
3. 冲突复用时重算底层正文哈希，发现损坏阻断复用 (CorruptedContentPayloadError)；
4. 传入哈希与内容哈希自洽性校验；
5. 读时重算哈希与篡改防御 (CorruptedContentPayloadError)；
6. 不存在版本抛出 ContentVersionNotFoundError；
7. 交叉核验 expected_identity 阻断跨页、跨模型、跨字段与版本越权 (ContentVersionIdentityMismatchError)；
8. 索引创建与复合唯一索引保证；
9. 索引未就绪时硬门禁阻断写入 (ContentStoreError) 与 check_indexes 准确报告。
"""

import uuid
from django.conf import settings
from django.test import TestCase

from external_content.exceptions import (
    ContentStoreError,
    ContentVersionIdentityMismatchError,
    ContentVersionNotFoundError,
    CorruptedContentPayloadError,
)
from external_content.store import (
    get_read_concern,
    get_write_concern,
    get_collection,
    ContentVersionStore,
    check_indexes,
    compute_canonical_hash,
    ensure_indexes,
    get_mongo_client,
)


class ContentVersionStoreTests(TestCase):
    """MongoDB 不可变正文版本仓储定向测试。"""

    def setUp(self) -> None:
        self.mongo_client = get_mongo_client()
        db_name = getattr(settings, "MONGO_DB", {}).get("NAME", "wagtailblog_test")
        self.coll_name = f"test_ext_versions_{uuid.uuid4().hex[:12]}"
        self.collection = self.mongo_client[db_name][self.coll_name]
        ensure_indexes(self.collection)
        self.store = ContentVersionStore(collection=self.collection)

        self.sample_payload = [
            {"id": "block-1", "type": "paragraph", "value": "测试正文段落"},
            {"id": "block-2", "type": "heading", "value": "标题内容"},
        ]
        self.sample_hash = compute_canonical_hash(self.sample_payload)

    def tearDown(self) -> None:
        self.collection.drop()

    def test_save_and_get_version_success(self) -> None:
        """测试正常保存并读取不可变版本。"""
        version_id = self.store.save_version(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.v1",
            schema_version=1,
            payload_sha256=self.sample_hash,
            payload=self.sample_payload,
        )
        self.assertTrue(bool(version_id))

        doc = self.store.get_version(
            version_id,
            expected_identity={
                "model_label": "guides.guidepage",
                "page_id": 101,
                "field_name": "body",
                "codec": "streamfield.v1",
                "schema_version": 1,
                "payload_sha256": self.sample_hash,
            },
        )
        self.assertEqual(doc["version_id"], version_id)
        self.assertEqual(doc["page_id"], 101)
        self.assertEqual(doc["payload"], self.sample_payload)
        self.assertEqual(doc["payload_sha256"], self.sample_hash)

    def test_save_version_idempotent_duplicate_reuse(self) -> None:
        """测试相同内容在同页面同字段下保存幂等复用已有 version_id。"""
        v1 = self.store.save_version(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.v1",
            schema_version=1,
            payload_sha256=self.sample_hash,
            payload=self.sample_payload,
        )
        v2 = self.store.save_version(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.v1",
            schema_version=1,
            payload_sha256=self.sample_hash,
            payload=self.sample_payload,
        )
        self.assertEqual(v1, v2)
        self.assertEqual(self.collection.count_documents({}), 1)

    def test_duplicate_key_reuse_detects_corrupted_existing_payload(self) -> None:
        """当发生唯一键冲突复用时，若现存文档底层的 payload 损坏，阻断复用并抛出异常。"""
        version_id = self.store.save_version(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.v1",
            schema_version=1,
            payload_sha256=self.sample_hash,
            payload=self.sample_payload,
        )
        # 破坏 MongoDB 中现存文档的 payload
        self.collection.update_one(
            {"version_id": version_id},
            {"$set": {"payload": [{"id": "corrupted", "type": "bad", "value": "损坏"}]}},
        )

        # 再次尝试保存相同内容，触发 DuplicateKeyError 复用路径，但因底层损坏应阻断
        with self.assertRaises(CorruptedContentPayloadError):
            self.store.save_version(
                model_label="guides.guidepage",
                page_id=101,
                field_name="body",
                codec="streamfield.v1",
                schema_version=1,
                payload_sha256=self.sample_hash,
                payload=self.sample_payload,
            )

    def test_save_version_with_distinct_payload_creates_new_version(self) -> None:
        """不同正文生成不同不可变版本。"""
        v1 = self.store.save_version(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.v1",
            schema_version=1,
            payload_sha256=self.sample_hash,
            payload=self.sample_payload,
        )
        payload2 = [{"id": "block-1", "type": "paragraph", "value": "修改后的正文"}]
        hash2 = compute_canonical_hash(payload2)
        v2 = self.store.save_version(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.v1",
            schema_version=1,
            payload_sha256=hash2,
            payload=payload2,
        )
        self.assertNotEqual(v1, v2)
        self.assertEqual(self.collection.count_documents({}), 2)

    def test_save_version_hash_mismatch_rejected(self) -> None:
        """传入的 payload_sha256 与计算哈希不符时阻断写入。"""
        with self.assertRaises(ContentStoreError):
            self.store.save_version(
                model_label="guides.guidepage",
                page_id=101,
                field_name="body",
                codec="streamfield.v1",
                schema_version=1,
                payload_sha256="0" * 64,
                payload=self.sample_payload,
            )

    def test_get_version_not_found(self) -> None:
        """请求不存在的 version_id 抛出 ContentVersionNotFoundError。"""
        dummy_identity = {
            "model_label": "guides.guidepage",
            "page_id": 999,
            "field_name": "body",
            "codec": "streamfield.v1",
            "schema_version": 1,
            "payload_sha256": "0" * 64,
        }
        with self.assertRaises(ContentVersionNotFoundError):
            self.store.get_version("non-existent-version-id", expected_identity=dummy_identity)

    def test_get_version_corrupted_payload_detected(self) -> None:
        """读时重算哈希检测出篡改或损坏时抛出 CorruptedContentPayloadError。"""
        version_id = self.store.save_version(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.v1",
            schema_version=1,
            payload_sha256=self.sample_hash,
            payload=self.sample_payload,
        )
        self.collection.update_one(
            {"version_id": version_id},
            {"$set": {"payload": [{"id": "block-1", "type": "paragraph", "value": "篡改内容"}]}},
        )
        with self.assertRaises(CorruptedContentPayloadError):
            valid_identity = {
                "model_label": "guides.guidepage",
                "page_id": 101,
                "field_name": "body",
                "codec": "streamfield.v1",
                "schema_version": 1,
                "payload_sha256": self.sample_hash,
            }
            self.store.get_version(version_id, expected_identity=valid_identity)

    def test_get_version_identity_mismatches_blocked(self) -> None:
        """身份交叉核验失败时阻断读取并抛出 ContentVersionIdentityMismatchError。"""
        version_id = self.store.save_version(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.v1",
            schema_version=1,
            payload_sha256=self.sample_hash,
            payload=self.sample_payload,
        )

        valid_identity = {
            "model_label": "guides.guidepage",
            "page_id": 101,
            "field_name": "body",
            "codec": "streamfield.v1",
            "schema_version": 1,
            "payload_sha256": self.sample_hash,
        }

        # 跨页读取
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.get_version(version_id, expected_identity=dict(valid_identity, page_id=999))

        # 跨模型读取
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.get_version(version_id, expected_identity=dict(valid_identity, model_label="blog.blogpage"))

        # 跨字段读取
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.get_version(version_id, expected_identity=dict(valid_identity, field_name="other_body"))

        # 编解码器不符
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.get_version(version_id, expected_identity=dict(valid_identity, codec="raw.v1"))

        # 架构版本不符
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.get_version(version_id, expected_identity=dict(valid_identity, schema_version=2))

        # 正文哈希不符
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.get_version(version_id, expected_identity=dict(valid_identity, payload_sha256="f" * 64))

    def test_unindexed_collection_blocks_write(self) -> None:
        """当集合未初始化唯一索引时，save_version 必须抛出 ContentStoreError 阻断写入。"""
        db_name = getattr(settings, "MONGO_DB", {}).get("NAME", "wagtailblog_test")
        unindexed_name = f"test_unindexed_{uuid.uuid4().hex[:12]}"
        unindexed_coll = self.mongo_client[db_name][unindexed_name]
        try:
            # 此时未建立任何索引
            is_ready, errors = check_indexes(unindexed_coll)
            self.assertFalse(is_ready)
            self.assertEqual(len(errors), 2)

            unindexed_store = ContentVersionStore(collection=unindexed_coll, enforce_index_check=True)
            with self.assertRaises(ContentStoreError) as ctx:
                unindexed_store.save_version(
                    model_label="guides.guidepage",
                    page_id=101,
                    field_name="body",
                    codec="streamfield.v1",
                    schema_version=1,
                    payload_sha256=self.sample_hash,
                    payload=self.sample_payload,
                )
            self.assertIn("唯一索引未就绪，阻断写入", str(ctx.exception))
        finally:
            unindexed_coll.drop()
    def test_invalid_index_specifications_rejected_by_check_indexes(self) -> None:
        """验证 check_indexes 精确核验：稀疏索引、部分索引或错误字段顺序均被识别并报告。"""
        db_name = getattr(settings, "MONGO_DB", {}).get("NAME", "wagtailblog_test")
        test_coll_name = f"test_bad_idx_{uuid.uuid4().hex[:12]}"
        bad_coll = self.mongo_client[db_name][test_coll_name]
        try:
            # 1. 建立稀疏索引
            bad_coll.create_index(
                "version_id",
                unique=True,
                sparse=True,
                name="external_content_version_id_unique",
            )
            # 2. 建立错误排序或不全的复合索引
            bad_coll.create_index(
                [("model_label", 1), ("field_name", 1)],
                unique=True,
                name="external_content_compound_identity_unique",
            )

            is_ready, errors = check_indexes(bad_coll)
            self.assertFalse(is_ready)
            self.assertTrue(any("严禁设置为 sparse=True" in e for e in errors))
            self.assertTrue(any("键规格或顺序不符" in e for e in errors))
        finally:
            bad_coll.drop()
    def test_get_version_missing_expected_identity_keys_rejected(self) -> None:
        """测试 get_version 缺少或提供残缺 expected_identity 时严格失败关闭。"""
        version_id = self.store.save_version(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.v1",
            schema_version=1,
            payload_sha256=self.sample_hash,
            payload=self.sample_payload,
        )

        # 1. 传入非字典
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.get_version(version_id, expected_identity=None)  # type: ignore

        # 2. 缺少必要键（如缺少 payload_sha256）
        incomplete_identity = {
            "model_label": "guides.guidepage",
            "page_id": 101,
            "field_name": "body",
            "codec": "streamfield.v1",
            "schema_version": 1,
        }
        with self.assertRaises(ContentVersionIdentityMismatchError) as ctx:
            self.store.get_version(version_id, expected_identity=incomplete_identity)
        self.assertIn("缺少必要身份键", str(ctx.exception))

    def test_write_concern_applied_to_collection_and_store(self) -> None:
        """验证 Write/Read Concern 配置正确挂载至 Collection 与 ContentVersionStore 写入路径。"""
        wc = get_write_concern()
        rc = get_read_concern()
        self.assertTrue(wc.document.get('j'))
        self.assertIn(wc.document.get("w"), ["majority", 1])
        self.assertIn(rc.level, ["majority", "local"])

        # 检查 get_collection 挂载的 write_concern 与 read_concern
        coll = get_collection()
        self.assertEqual(coll.write_concern.document, wc.document)
        self.assertEqual(coll.read_concern.level, rc.level)

        # 检查 ContentVersionStore 初始化时对 collection 写入关注与读取关注的保持
        custom_store = ContentVersionStore(collection=self.collection)
        self.assertEqual(custom_store.collection.write_concern.document, wc.document)
        self.assertEqual(custom_store.collection.read_concern.level, rc.level)

    def test_write_concern_error_fails_closed_without_downgrade(self) -> None:
        """测试写入未能满足 Write Concern 要求时严格失败关闭，绝不自动降级或返回版本。"""
        with patch.object(
            self.store.collection,
            "insert_one",
            side_effect=pymongo.errors.WriteConcernError("write concern majority timeout"),
        ):
            with self.assertRaises(ContentStoreError) as ctx:
                self.store.save_version(
                    model_label="guides.guidepage",
                    page_id=101,
                    field_name="body",
                    codec="streamfield.v1",
                    schema_version=1,
                    payload_sha256=self.sample_hash,
                    payload=self.sample_payload,
                )
            self.assertIn("未能满足配置的持久性要求", str(ctx.exception))

    def test_write_concern_failure_retry_duplicate_key_without_majority_fails_closed(self) -> None:
        """测试首次写入因 WriteConcern 失败，随后重试遇到重复键但该记录尚未获 majority 确认时，严格失败关闭。"""
        # 1. 模拟首次写入发生 WriteConcernError
        with patch.object(
            self.store.collection,
            "insert_one",
            side_effect=pymongo.errors.WriteConcernError("write concern majority timeout"),
        ):
            with self.assertRaises(ContentStoreError):
                self.store.save_version(
                    model_label="guides.guidepage",
                    page_id=101,
                    field_name="body",
                    codec="streamfield.v1",
                    schema_version=1,
                    payload_sha256=self.sample_hash,
                    payload=self.sample_payload,
                )

        # 2. 模拟随后同内容重试：由于主节点存在未获多数派确认的记录，insert_one 触发 DuplicateKeyError，
        # 但在 majority 读取级别下 find_one 无法查到已确认记录 (返回 None)
        with patch.object(
            self.store.collection,
            "insert_one",
            side_effect=pymongo.errors.DuplicateKeyError("E11000 duplicate key error"),
        ), patch.object(
            self.store.collection,
            "find_one",
            return_value=None,  # majority read concern 未看到未确认文档
        ):
            with self.assertRaises(ContentStoreError) as ctx:
                self.store.save_version(
                    model_label="guides.guidepage",
                    page_id=101,
                    field_name="body",
                    codec="streamfield.v1",
                    schema_version=1,
                    payload_sha256=self.sample_hash,
                    payload=self.sample_payload,
                )
            self.assertIn("未检索到已确认的不可变正文", str(ctx.exception))
