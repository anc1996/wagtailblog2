from datetime import datetime, timezone as dt_timezone
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

import copy
import threading
import time
from typing import Any, Dict, List, Optional
import unittest
from unittest.mock import MagicMock, patch
import uuid

import pymongo
from pymongo.errors import DuplicateKeyError, WriteConcernError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from external_content.exceptions import (
    ContentStoreError,
    ContentVersionIdentityMismatchError,
    ContentVersionNotFoundError,
    CorruptedContentPayloadError,
    ExternalContentOperationBlockedError,
)
from external_content.store import (
    ContentVersionStore,
    check_indexes,
    compute_canonical_hash,
    ensure_indexes,
    get_collection,
    get_mongo_client,
    get_read_concern,
    get_write_concern,
)


class FakeInsertOneResult:
    """纯内存 insert_one 结果替身。"""

    def __init__(self, inserted_id: Any) -> None:
        self.inserted_id = inserted_id


class FakeUpdateResult:
    """纯内存 update_one 结果替身。"""

    def __init__(self, matched_count: int, modified_count: int) -> None:
        self.matched_count = matched_count
        self.modified_count = modified_count


class FakeDeleteResult:
    """纯内存 delete_one 结果替身。"""

    def __init__(self, deleted_count: int) -> None:
        self.deleted_count = deleted_count


class FakeMongoCollection:
    """纯内存 MongoDB 集合替身，杜绝网络与真实数据库连接。"""

    def __init__(
        self,
        name: str = "external_content_versions",
        write_concern: Optional[WriteConcern] = None,
        read_concern: Optional[ReadConcern] = None,
        documents: Optional[List[Dict[str, Any]]] = None,
        indexes: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.name = name
        self.write_concern = write_concern or WriteConcern(w="majority", j=True)
        self.read_concern = read_concern or ReadConcern(level="majority")
        self._documents: List[Dict[str, Any]] = documents if documents is not None else []
        self._indexes: Dict[str, Dict[str, Any]] = indexes if indexes is not None else {}
        self.with_options_calls: List[Dict[str, Any]] = []

    def with_options(
        self,
        write_concern: Optional[WriteConcern] = None,
        read_concern: Optional[ReadConcern] = None,
        **kwargs: Any,
    ) -> "FakeMongoCollection":
        call_record = {"write_concern": write_concern, "read_concern": read_concern, **kwargs}
        self.with_options_calls.append(call_record)
        new_coll = FakeMongoCollection(
            name=self.name,
            write_concern=write_concern or self.write_concern,
            read_concern=read_concern or self.read_concern,
            documents=self._documents,
            indexes=self._indexes,
        )
        new_coll.with_options_calls = self.with_options_calls
        return new_coll

    def drop_index(self, name: str) -> None:
        self._indexes.pop(name, None)

    def create_index(
        self,
        keys: Any,
        unique: bool = False,
        sparse: bool = False,
        name: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        if isinstance(keys, str):
            norm_keys = [(keys, 1)]
        elif isinstance(keys, list):
            norm_keys = list(keys)
        elif isinstance(keys, tuple):
            norm_keys = [keys]
        else:
            norm_keys = list(keys)

        index_name = name or "_".join(f"{k}_{d}" for k, d in norm_keys)
        info: Dict[str, Any] = {
            "key": norm_keys,
            "unique": unique,
        }
        if sparse:
            info["sparse"] = True
        for k, v in kwargs.items():
            info[k] = v
        self._indexes[index_name] = info
        return index_name

    def index_information(self) -> Dict[str, Dict[str, Any]]:
        return copy.deepcopy(self._indexes)

    def insert_one(self, doc: Dict[str, Any]) -> FakeInsertOneResult:
        for idx_name, idx_spec in self._indexes.items():
            if idx_spec.get("unique"):
                keys = [k for k, _ in idx_spec["key"]]
                for existing in self._documents:
                    if all(existing.get(k) == doc.get(k) for k in keys):
                        raise DuplicateKeyError(
                            f"E11000 duplicate key error collection: {self.name} index: {idx_name}"
                        )
        stored = copy.deepcopy(doc)
        if "_id" not in stored:
            stored["_id"] = uuid.uuid4().hex
        self._documents.append(stored)
        return FakeInsertOneResult(inserted_id=stored["_id"])

    def find_one(self, filter: Optional[Dict[str, Any]] = None, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if not filter:
            return copy.deepcopy(self._documents[0]) if self._documents else None
        for doc in self._documents:
            match = True
            for k, v in filter.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                if self.read_concern and getattr(self.read_concern, "level", None) == "majority":
                    if doc.get("_unconfirmed_majority"):
                        continue
                return copy.deepcopy(doc)
        return None

    def update_one(
        self,
        filter: Dict[str, Any],
        update: Dict[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> FakeUpdateResult:
        for doc in self._documents:
            match = True
            for k, v in filter.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                if "$set" in update:
                    for k, v in update["$set"].items():
                        doc[k] = copy.deepcopy(v)
                return FakeUpdateResult(matched_count=1, modified_count=1)
        return FakeUpdateResult(matched_count=0, modified_count=0)

    def delete_one(self, filter: Dict[str, Any], *args: Any, **kwargs: Any) -> FakeDeleteResult:
        for i, doc in enumerate(self._documents):
            match = True
            for k, v in filter.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                self._documents.pop(i)
                return FakeDeleteResult(deleted_count=1)
        return FakeDeleteResult(deleted_count=0)

    def count_documents(self, filter: Optional[Dict[str, Any]] = None, *args: Any, **kwargs: Any) -> int:
        if not filter:
            return len(self._documents)
        count = 0
        for doc in self._documents:
            match = True
            for k, v in filter.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                count += 1
        return count

    def drop(self) -> None:
        self._documents.clear()
        self._indexes.clear()


class ContentVersionStoreTests(unittest.TestCase):
    """MongoDB 不可变正文版本仓储定向测试（基于纯内存 Fake 替身）。"""

    def setUp(self) -> None:
        self.coll_name = f"test_ext_versions_{uuid.uuid4().hex[:12]}"
        self.collection = FakeMongoCollection(name=self.coll_name)
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
        unindexed_name = f"test_unindexed_{uuid.uuid4().hex[:12]}"
        unindexed_coll = FakeMongoCollection(name=unindexed_name)
        try:
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
            self.assertIn("唯一索引未就绪", str(ctx.exception))
        finally:
            unindexed_coll.drop()
    def test_invalid_index_specifications_rejected_by_check_indexes(self) -> None:
        """验证 check_indexes 精确核验：稀疏索引、部分索引或错误字段顺序均被识别并报告。"""
        test_coll_name = f"test_bad_idx_{uuid.uuid4().hex[:12]}"
        bad_coll = FakeMongoCollection(name=test_coll_name)
        try:
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
        mock_cli = MagicMock()
        mock_coll = MagicMock()
        mock_coll.write_concern = wc
        mock_coll.read_concern = rc
        mock_cli.__getitem__.return_value.get_collection.return_value = mock_coll
        with patch("external_content.store.get_mongo_settings", return_value={"NAME": "test_db", "HOST": "localhost", "PORT": 27017}):
            coll = get_collection(client=mock_cli)
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

        # 2. 模拟底层的脏写文档：虽然主节点有记录且能引发唯一索引冲突，但带有 _unconfirmed_majority=True
        unconfirmed_doc = {
            "model_label": "guides.guidepage",
            "page_id": 101,
            "field_name": "body",
            "codec": "streamfield.v1",
            "schema_version": 1,
            "version_id": "vid-unconfirmed-101",
            "payload_sha256": self.sample_hash,
            "payload": self.sample_payload,
            "created_at": datetime.now(dt_timezone.utc),
            "_unconfirmed_majority": True,
        }
        self.store.collection._documents.append(unconfirmed_doc)

        # 3. 模拟随后同内容重试：insert_one 触发 DuplicateKeyError
        # 此时由底层 FakeMongoCollection 按照真实 read_concern 决策可见性：
        # 如果代码错误地使用了 local 读，就会读取到 unconfirmed_doc 并错误复用；
        # 只有当代码严格强制提升为 majority 读时，FakeMongoCollection.find_one 才会因为 _unconfirmed_majority 过滤掉它返回 None，
        # 进而触发 ContentStoreError(unconfirmed_version_conflict)
        with patch.object(
            self.store.collection,
            "insert_one",
            side_effect=pymongo.errors.DuplicateKeyError("E11000 duplicate key error"),
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
            self.assertEqual(ctx.exception.details.get("error_category"), "unconfirmed_version_conflict")
            self.assertIn("未检索到已确认的不可变正文", str(ctx.exception))
    def test_save_version_with_status_tracks_is_new_accurately(self) -> None:
        """测试 save_version_with_status 精确返回 (version_id, is_new) 状态。"""
        payload = [{"type": "markdown", "value": "# 首次版本"}]
        sha = compute_canonical_hash(payload)

        vid1, is_new1 = self.store.save_version_with_status(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.json.v1",
            schema_version=1,
            payload_sha256=sha,
            payload=payload,
        )
        self.assertTrue(is_new1, "首次插入应返回 is_new=True")

        # 再次插入相同内容，应命中唯一索引幂等复用并返回 False
        vid2, is_new2 = self.store.save_version_with_status(
            model_label="guides.guidepage",
            page_id=101,
            field_name="body",
            codec="streamfield.json.v1",
            schema_version=1,
            payload_sha256=sha,
            payload=payload,
        )
        self.assertEqual(vid1, vid2, "相同内容应复用相同 version_id")
        self.assertFalse(is_new2, "重复插入应返回 is_new=False")

    def test_cleanup_unreferenced_version_with_page_id_isolation(self) -> None:
        """测试 cleanup_unreferenced_version 严格硬熔断阻断同步删除，且底层文档受保护不被删除。"""
        payload = [{"type": "markdown", "value": "# 临时版本正文"}]
        sha = compute_canonical_hash(payload)
        vid, _ = self.store.save_version_with_status(
            model_label="guides.guidepage",
            page_id=202,
            field_name="body",
            codec="streamfield.json.v1",
            schema_version=1,
            payload_sha256=sha,
            payload=payload,
        )
        with self.assertRaises(ExternalContentOperationBlockedError):
            self.store.cleanup_unreferenced_version(vid, page_id=202)
        raw_doc = self.store.get_raw_version_document(vid)
        self.assertIsNotNone(raw_doc, "被阻断清理的版本绝不能从底层集合被物理删除")
    def test_get_raw_version_document_retrieval(self) -> None:
        """测试 get_raw_version_document 仅用于裸查且不触发严格业务身份校验。"""
        payload = [{"type": "markdown", "value": "# 巡检排查正文"}]
        sha = compute_canonical_hash(payload)
        vid = self.store.save_version(
            model_label="guides.guidepage",
            page_id=303,
            field_name="body",
            codec="streamfield.json.v1",
            schema_version=1,
            payload_sha256=sha,
            payload=payload,
        )
        doc = self.store.get_raw_version_document(vid)
        self.assertIsNotNone(doc)
        self.assertEqual(doc["version_id"], vid)
        self.assertEqual(doc["page_id"], 303)

        # 查询不存在的版本
        self.assertIsNone(self.store.get_raw_version_document("non-existent-vid"))

    def test_delete_version_physical_deletion_and_idempotency_on_fake_collection(self) -> None:
        """基于 FakeMongoCollection 验证 delete_version 真实物理删除与幂等返回 False。"""
        payload = [{"type": "markdown", "value": "# 待删除版本"}]
        sha = compute_canonical_hash(payload)
        identity = {
            "model_label": "guides.guidepage",
            "page_id": 401,
            "field_name": "body",
            "codec": "streamfield.json.v1",
            "schema_version": 1,
            "payload_sha256": sha,
        }
        vid = self.store.save_version(**dict(identity, payload=payload))
        self.assertIsNotNone(self.collection.find_one({"version_id": vid}))

        # 首次删除：物理删除成功并返回 True
        res1 = self.store.delete_version(vid, expected_identity=identity)
        self.assertTrue(res1)
        self.assertIsNone(self.collection.find_one({"version_id": vid}))

        # 二次删除：幂等且返回 False
        res2 = self.store.delete_version(vid, expected_identity=identity)
        self.assertFalse(res2)

    def test_delete_version_identity_mismatch_blocks_and_does_not_delete(self) -> None:
        """delete_version 若身份不匹配抛出异常，且底层文档不被删除。"""
        payload = [{"type": "markdown", "value": "# 保护文档"}]
        sha = compute_canonical_hash(payload)
        identity = {
            "model_label": "guides.guidepage",
            "page_id": 402,
            "field_name": "body",
            "codec": "streamfield.json.v1",
            "schema_version": 1,
            "payload_sha256": sha,
        }
        vid = self.store.save_version(**dict(identity, payload=payload))

        wrong_identity = dict(identity, page_id=999)
        with self.assertRaises(ContentVersionIdentityMismatchError):
            self.store.delete_version(vid, expected_identity=wrong_identity)

        # 核心文档绝未被删除
        self.assertIsNotNone(self.collection.find_one({"version_id": vid}))

    def test_get_collection_binds_write_and_read_concerns_strictly(self) -> None:
        """get_collection 严格绑定 majority write concern 与 majority read concern。"""
        fake_client = MagicMock()
        fake_db = MagicMock()
        fake_client.__getitem__.return_value = fake_db
        fake_coll = MagicMock()
        fake_db.get_collection.return_value = fake_coll

        custom_wc = WriteConcern(w="majority", j=True)
        custom_rc = ReadConcern(level="majority")

        with patch("external_content.store.get_mongo_settings", return_value={"NAME": "test_db", "HOST": "localhost", "PORT": 27017}):
            get_collection(client=fake_client, write_concern=custom_wc, read_concern=custom_rc)

        fake_db.get_collection.assert_called_once()
        _, kwargs = fake_db.get_collection.call_args
        self.assertEqual(kwargs.get("write_concern"), custom_wc)
        self.assertEqual(kwargs.get("read_concern"), custom_rc)

    def test_duplicate_key_retry_explicitly_queries_with_majority_read_concern(self) -> None:
        """冲突复用重试时，严格使用 majority read concern 查询现存不可变版本。"""
        payload = [{"type": "markdown", "value": "# 重试读关注测试"}]
        sha = compute_canonical_hash(payload)
        mock_coll = MagicMock()
        mock_coll.index_information.return_value = {
            "external_content_version_id_unique": {"key": [("version_id", 1)], "unique": True},
            "external_content_compound_identity_unique": {
                "key": [
                    ("model_label", 1),
                    ("page_id", 1),
                    ("field_name", 1),
                    ("codec", 1),
                    ("schema_version", 1),
                    ("payload_sha256", 1),
                ],
                "unique": True,
            },
        }
        mock_coll.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error")
        mock_coll.read_concern = ReadConcern(level="majority")
        read_coll_mock = MagicMock()
        read_coll_mock.index_information.return_value = mock_coll.index_information.return_value
        read_coll_mock.name = 'test_mock_coll'
        mock_coll.with_options.side_effect = [mock_coll, read_coll_mock]
        read_coll_mock.find_one.return_value = {
            "model_label": "guides.guidepage",
            "page_id": 501,
            "field_name": "body",
            "codec": "streamfield.json.v1",
            "schema_version": 1,
            "payload_sha256": sha,
            "version_id": "vid-501",
            "payload": payload,
        }

        store = ContentVersionStore(collection=mock_coll)
        vid, is_new = store.save_version_with_status(
            model_label="guides.guidepage",
            page_id=501,
            field_name="body",
            codec="streamfield.json.v1",
            schema_version=1,
            payload_sha256=sha,
            payload=payload,
        )
        self.assertFalse(is_new)
        self.assertEqual(vid, "vid-501")
        self.assertEqual(mock_coll.with_options.call_count, 2)
        _, opts = mock_coll.with_options.call_args
        self.assertEqual(opts.get("read_concern").level, "majority")


    def test_duplicate_key_retry_forces_majority_even_when_store_is_local(self) -> None:
        """测试初始集合配置为 local 读关注时，DuplicateKey 重试检索仍无条件强制提升为 majority 读关注。"""
        # 创建 read_concern 为 local 的集合与 store
        local_rc = ReadConcern(level="local")
        local_coll = FakeMongoCollection(read_concern=local_rc)
        ensure_indexes(local_coll)
        store = ContentVersionStore(collection=local_coll, read_concern=local_rc)

        payload = {"type": "paragraph", "value": "冲突重试强制 majority 测试"}
        sha = compute_canonical_hash(payload)

        # 首次保存成功
        store.save_version(
            model_label="guides.guidepage",
            page_id=201,
            field_name="body",
            codec="streamfield.json.v1",
            schema_version=1,
            payload_sha256=sha,
            payload=payload,
        )

        # 清除此前的 with_options 记录
        local_coll.with_options_calls.clear()

        # 再次保存相同内容，触发 DuplicateKeyError 重试
        vid, is_new = store.save_version_with_status(
            model_label="guides.guidepage",
            page_id=201,
            field_name="body",
            codec="streamfield.json.v1",
            schema_version=1,
            payload_sha256=sha,
            payload=payload,
        )
        self.assertFalse(is_new)
        # 断言重试时调用的 with_options 必须明确使用了 read_concern.level == "majority"，决不能沿用 local
        retry_calls = [c for c in local_coll.with_options_calls if c.get("read_concern") is not None]
        self.assertTrue(len(retry_calls) >= 1, "未记录到使用 read_concern 的 with_options 调用")
        last_retry_rc = retry_calls[-1]["read_concern"]
        self.assertEqual(last_retry_rc.level, "majority", "DuplicateKey 重试未强制使用 majority 读关注！")

    def test_unconfirmed_majority_version_fails_closed_on_duplicate_key(self) -> None:
        """测试存在未获 majority 确认的重复键版本时，重试在 majority 下查不到，严格 fail-closed 抛出 unconfirmed_version_conflict。"""
        coll = FakeMongoCollection(read_concern=ReadConcern(level="local"))
        ensure_indexes(coll)
        store = ContentVersionStore(collection=coll, read_concern=ReadConcern(level='local'))

        payload = {"type": "paragraph", "value": "未确认多数派版本测试"}
        sha = compute_canonical_hash(payload)

        # 模拟主节点上有一条未达成多数派确认的脏写版本（在 local 可见，但 majority 不可见）
        unconfirmed_doc = {
            "model_label": "guides.guidepage",
            "page_id": 202,
            "field_name": "body",
            "codec": "streamfield.json.v1",
            "schema_version": 1,
            "version_id": "vid-unconfirmed",
            "payload_sha256": sha,
            "payload": payload,
            "created_at": datetime.now(dt_timezone.utc),
            "_unconfirmed_majority": True,
        }
        coll._documents.append(unconfirmed_doc)

        # 验证该文档在 local 读关注下确实可见（证明冲突客观存在）
        self.assertIsNotNone(coll.find_one({"payload_sha256": sha}))

        # 现在执行 save_version_with_status：写入触发 DuplicateKeyError，
        # 但重试必须强制以 majority 读取，由于该文档未获多数派确认，majority 下返回 None，必须抛出 ContentStoreError
        with self.assertRaises(ContentStoreError) as ctx:
            store.save_version_with_status(
                model_label="guides.guidepage",
                page_id=202,
                field_name="body",
                codec="streamfield.json.v1",
                schema_version=1,
                payload_sha256=sha,
                payload=payload,
            )
        self.assertEqual(ctx.exception.details.get("error_category"), "unconfirmed_version_conflict")
        self.assertIn("未检索到已确认的不可变正文", str(ctx.exception))

    def test_index_check_not_bypassed_after_index_removal(self) -> None:
        """测试索引就绪检查无永久缓存：首次成功写入后，若复合唯一索引被 drop，后续 save 必须实时阻断。"""
        coll = FakeMongoCollection()
        ensure_indexes(coll)
        store = ContentVersionStore(collection=coll, enforce_index_check=True)

        payload1 = {"type": "paragraph", "value": "第1次写入，索引就绪"}
        sha1 = compute_canonical_hash(payload1)
        store.save_version(
            model_label="guides.guidepage",
            page_id=203,
            field_name="body",
            codec="streamfield.json.v1",
            schema_version=1,
            payload_sha256=sha1,
            payload=payload1,
        )

        # 模拟外部运维或测试将唯一索引删除
        coll.drop_index("external_content_compound_identity_unique")

        payload2 = {"type": "paragraph", "value": "第2次写入，索引已漂移缺失"}
        sha2 = compute_canonical_hash(payload2)
        with self.assertRaises(ContentStoreError) as ctx:
            store.save_version(
                model_label="guides.guidepage",
                page_id=203,
                field_name="body",
                codec="streamfield.json.v1",
                schema_version=1,
                payload_sha256=sha2,
                payload=payload2,
            )
        self.assertIn("唯一索引未就绪", str(ctx.exception))

    def test_get_mongo_client_thread_safe_double_checked_locking(self) -> None:
        """测试 get_mongo_client 在并发环境下的线程安全单例与双重检查锁。"""
        from external_content import store as store_module

        # 保存原始客户端并重置
        orig_client = getattr(store_module, "_mongo_client", None)
        try:
            store_module._mongo_client = None
            clients = []
            errors = []

            def worker():
                try:
                    with patch("external_content.store.get_mongo_settings", return_value={"HOST": "127.0.0.1", "PORT": 27017, "NAME": "testdb"}):
                        with patch("pymongo.MongoClient") as mock_mc:
                            c = store_module.get_mongo_client()
                            clients.append(c)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(errors), 0, f"多线程并发调用报错: {errors}")
            self.assertEqual(len(clients), 10)
            # 必须全部是同一个实例引用
            first = clients[0]
            for c in clients[1:]:
                self.assertIs(c, first)
        finally:
            store_module._mongo_client = orig_client

    def test_get_mongo_client_failure_keeps_none_and_allows_subsequent_retry(self) -> None:
        """测试 MongoClient 初始化失败时，全局引用保持为 None，允许后续重试恢复。"""
        from external_content import store as store_module
        orig_client = getattr(store_module, "_mongo_client", None)
        try:
            store_module._mongo_client = None
            with patch("external_content.store.get_mongo_settings", return_value={"HOST": "127.0.0.1", "PORT": 27017, "NAME": "testdb"}):
                with patch("pymongo.MongoClient", side_effect=pymongo.errors.ServerSelectionTimeoutError("connection refused")):
                    with self.assertRaises(ContentStoreError):
                        store_module.get_mongo_client()
            # 失败后必须依然为 None，防止脏状态残留
            self.assertIsNone(store_module._mongo_client)

            # 重试成功时正常初始化
            with patch("external_content.store.get_mongo_settings", return_value={"HOST": "127.0.0.1", "PORT": 27017, "NAME": "testdb"}):
                with patch("pymongo.MongoClient") as mock_mc:
                    c = store_module.get_mongo_client()
                    self.assertIsNotNone(c)
                    self.assertIs(store_module._mongo_client, c)
        finally:
            store_module._mongo_client = orig_client
