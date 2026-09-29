"""
Wagtail Page 正文外置通用协议 - MongoDB 不可变版本仓储。

集合名称：external_content_versions
约束：
1. 只增不改：只允许 insert_one，严禁使用 $set 更新已有正文；
2. 幂等复用：基于 (model_label, page_id, field_name, codec, schema_version, payload_sha256) 建立唯一索引，相同归属与正文复用已有版本；
3. 读时重算验签：读取正文时重算 SHA-256 并交叉验证页面/字段/版本身份；
4. 冲突复用重验：DuplicateKeyError 命中复用时，严格重算验签现存文档 payload，杜绝复用损坏记录；
5. 索引完备性硬门禁：精确核验索引键、字段顺序、unique=True、禁止 sparse 与 partialFilterExpression，未通过拒绝写入；
6. 共享连接：复用 MongoClient 单例生命周期。
"""

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid

from django.conf import settings
from django.utils import timezone
import pymongo
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from external_content.exceptions import (
    ContentStoreError,
    ContentVersionIdentityMismatchError,
    ContentVersionNotFoundError,
    CorruptedContentPayloadError,
)

logger = logging.getLogger(__name__)

COLLECTION_NAME = "external_content_versions"
_mongo_client: Optional[pymongo.MongoClient] = None

REQUIRED_IDENTITY_KEYS: Tuple[str, ...] = (
    "model_label",
    "page_id",
    "field_name",
    "codec",
    "schema_version",
    "payload_sha256",
)

EXPECTED_VERSION_ID_KEY = [("version_id", 1)]
EXPECTED_COMPOUND_KEY = [
    ("model_label", 1),
    ("page_id", 1),
    ("field_name", 1),
    ("codec", 1),
    ("schema_version", 1),
    ("payload_sha256", 1),
]


def get_mongo_client() -> pymongo.MongoClient:
    """获取或初始化共享 MongoClient 实例。"""
    global _mongo_client
    if _mongo_client is None:
        mongo_settings = getattr(settings, "MONGO_DB", {})
        host = mongo_settings.get("HOST", "127.0.0.1")
        port = mongo_settings.get("PORT", 27017)
        _mongo_client = pymongo.MongoClient(
            host=host,
            port=port,
            serverSelectionTimeoutMS=5000,
        )
    return _mongo_client


def get_write_concern() -> pymongo.write_concern.WriteConcern:
    """
    根据配置解析不可变版本集合的写入关注 (Write Concern)。

    默认配置为安全优先的 majority + journal。
    在单节点测试或未启用副本集的独立开发环境中，支持由配置覆盖。
    """
    mongo_settings = getattr(settings, "MONGO_DB", {})
    w = mongo_settings.get("WRITE_CONCERN_W", "majority")
    j = mongo_settings.get("WRITE_CONCERN_J", True)
    return pymongo.write_concern.WriteConcern(w=w, j=j)


def get_read_concern() -> pymongo.read_concern.ReadConcern:
    """
    根据配置解析不可变版本集合的读取关注 (Read Concern)。

    与 Write Concern (majority) 强一致配对：优先采用 majority 级别，
    确保在复用已有版本时，检索到的文档必须是已获得多数派确认持久化的版本，
    彻底阻断因 WriteConcern 超时未达成多数派确认的脏写版本被后续重试复用。
    测试环境或未配置副本集的单节点环境，若未开启多数派读，允许由配置覆盖为 local。
    """
    mongo_settings = getattr(settings, "MONGO_DB", {})
    level = mongo_settings.get("READ_CONCERN_LEVEL")
    if level is None:
        w = mongo_settings.get("WRITE_CONCERN_W", "majority")
        level = "majority" if str(w).lower() == "majority" else "local"
    return pymongo.read_concern.ReadConcern(level=level)


def get_collection(
    client: Optional[pymongo.MongoClient] = None,
    write_concern: Optional[pymongo.write_concern.WriteConcern] = None,
    read_concern: Optional[pymongo.read_concern.ReadConcern] = None,
) -> Collection:
    """获取 external_content_versions 集合对象，并应用指定的写入关注 (Write Concern) 与读取关注 (Read Concern)。"""
    cli = client or get_mongo_client()
    mongo_settings = getattr(settings, "MONGO_DB", {})
    db_name = mongo_settings.get("NAME", "wagtailblog_test")
    wc = write_concern if write_concern is not None else get_write_concern()
    rc = read_concern if read_concern is not None else get_read_concern()
    db = cli[db_name]
    return db.get_collection(COLLECTION_NAME, write_concern=wc, read_concern=rc)


def ensure_indexes(collection: Optional[Collection] = None) -> None:
    """
    确保不可变正文集合的唯一索引建立。

    仅在显式初始化、迁移或测试准备时调用，严禁在 Django 系统检查或启动导入时调用。
    """
    coll = collection if collection is not None else get_collection()
    coll.create_index(
        "version_id",
        unique=True,
        name="external_content_version_id_unique",
    )
    coll.create_index(
        [
            ("model_label", pymongo.ASCENDING),
            ("page_id", pymongo.ASCENDING),
            ("field_name", pymongo.ASCENDING),
            ("codec", pymongo.ASCENDING),
            ("schema_version", pymongo.ASCENDING),
            ("payload_sha256", pymongo.ASCENDING),
        ],
        unique=True,
        name="external_content_compound_identity_unique",
    )


def check_indexes(collection: Optional[Collection] = None) -> Tuple[bool, List[str]]:
    """
    检查 external_content_versions 集合的唯一索引是否完备。

    严格核验：
    1. 索引名称存在；
    2. 字段键名与升序排序完全匹配；
    3. unique 必须为 True；
    4. 禁止 sparse（稀疏）或 partialFilterExpression（部分索引），以防唯一约束存在盲区。

    :return: (is_ready, errors_list)
    """
    coll = collection if collection is not None else get_collection()
    info = coll.index_information()
    missing: List[str] = []

    # 1. 检查 version_id 索引
    v_idx = info.get("external_content_version_id_unique")
    if not v_idx:
        missing.append("缺少唯一索引: external_content_version_id_unique")
    else:
        if v_idx.get("key") != EXPECTED_VERSION_ID_KEY:
            missing.append(f"索引 external_content_version_id_unique 键规格不符: 期望 {EXPECTED_VERSION_ID_KEY}，实际 {v_idx.get('key')}")
        if v_idx.get("unique") is not True:
            missing.append("索引 external_content_version_id_unique 缺少 unique=True 约束")
        if v_idx.get("sparse") is True:
            missing.append("索引 external_content_version_id_unique 严禁设置为 sparse=True")
        if "partialFilterExpression" in v_idx:
            missing.append("索引 external_content_version_id_unique 严禁包含 partialFilterExpression")

    # 2. 检查复合身份索引
    c_idx = info.get("external_content_compound_identity_unique")
    if not c_idx:
        missing.append("缺少复合唯一索引: external_content_compound_identity_unique")
    else:
        if c_idx.get("key") != EXPECTED_COMPOUND_KEY:
            missing.append(f"索引 external_content_compound_identity_unique 键规格或顺序不符: 期望 {EXPECTED_COMPOUND_KEY}，实际 {c_idx.get('key')}")
        if c_idx.get("unique") is not True:
            missing.append("索引 external_content_compound_identity_unique 缺少 unique=True 约束")
        if c_idx.get("sparse") is True:
            missing.append("索引 external_content_compound_identity_unique 严禁设置为 sparse=True")
        if "partialFilterExpression" in c_idx:
            missing.append("索引 external_content_compound_identity_unique 严禁包含 partialFilterExpression")

    return (len(missing) == 0, missing)


def compute_canonical_hash(payload: Any) -> str:
    """计算 payload 的确定性 SHA-256 小写摘要。"""
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class ContentVersionStore:
    """
    MongoDB 不可变正文版本仓储。
    """

    def __init__(
        self,
        collection: Optional[Collection] = None,
        enforce_index_check: bool = True,
        write_concern: Optional[pymongo.write_concern.WriteConcern] = None,
        read_concern: Optional[pymongo.read_concern.ReadConcern] = None,
    ) -> None:
        wc = write_concern or get_write_concern()
        rc = read_concern or get_read_concern()
        if collection is not None:
            self._collection = collection.with_options(write_concern=wc, read_concern=rc)
        else:
            self._collection = get_collection(write_concern=wc, read_concern=rc)
        self.enforce_index_check = enforce_index_check
        self._indexes_checked: bool = False

    @property
    def collection(self) -> Collection:
        if self._collection is None:
            self._collection = get_collection()
        return self._collection

    def check_indexes_ready(self) -> None:
        """断言集合已正确配置必要唯一索引，未就绪时阻断写入以防幂等失效。"""
        if self._indexes_checked:
            return
        is_ready, errors = check_indexes(self.collection)
        if not is_ready:
            raise ContentStoreError(
                f"MongoDB 不可变正文集合 ({self.collection.name}) 唯一索引未就绪，阻断写入以防破坏不可变幂等约束: {errors}。"
                "请先运行 'python manage.py init_external_content_indexes' 初始化索引"
            )
        self._indexes_checked = True

    def save_version(
        self,
        *,
        model_label: str,
        page_id: int,
        field_name: str,
        codec: str,
        schema_version: int,
        payload_sha256: str,
        payload: Any,
    ) -> str:
        """
        持久化保存不可变字段正文版本。

        若同页、同字段、同内容已存在，则幂等返回现存 version_id。
        复用前必须重算比对现存文档的 payload 哈希，确保底层数据未发生位翻转或篡改。

        :return: version_id (唯一版本标识)
        :raises ContentStoreError: 写入或查询失败，或索引未就绪
        :raises CorruptedContentPayloadError: 冲突复用时检测到底层现存记录损坏
        """
        # 前置检查唯一索引完备性
        if self.enforce_index_check:
            self.check_indexes_ready()

        # 前置核对哈希自洽性
        computed_hash = compute_canonical_hash(payload)
        if computed_hash != payload_sha256:
            raise ContentStoreError(
                f"传入的 payload_sha256 ('{payload_sha256}') 与计算哈希 ('{computed_hash}') 不一致，阻断写入"
            )

        new_version_id = uuid.uuid4().hex
        doc = {
            "model_label": model_label,
            "page_id": page_id,
            "field_name": field_name,
            "codec": codec,
            "schema_version": schema_version,
            "version_id": new_version_id,
            "payload_sha256": payload_sha256,
            "payload": payload,
            "created_at": timezone.now(),
        }

        try:
            self.collection.insert_one(doc)
            return new_version_id
        except DuplicateKeyError:
            # 唯一索引冲突：存在相同归属与哈希的已有版本，使用配置的 ReadConcern 检索
            # 确保只有已达到预定持久性级别的版本才被复用，彻底拦截 WriteConcern 未确认版本的重试绕行
            rc = self.collection.read_concern or get_read_concern()
            read_coll = self.collection.with_options(read_concern=rc)
            existing = read_coll.find_one({
                "model_label": model_label,
                "page_id": page_id,
                "field_name": field_name,
                "codec": codec,
                "schema_version": schema_version,
                "payload_sha256": payload_sha256,
            })
            if existing and "version_id" in existing:
                # 交叉重算现存文档的 payload 哈希验签，防止复用到底层损坏的记录
                existing_payload = existing.get("payload")
                recomputed = compute_canonical_hash(existing_payload)
                if recomputed != payload_sha256 or existing.get("payload_sha256") != payload_sha256:
                    raise CorruptedContentPayloadError(
                        f"复用现存不可变版本 (version_id='{existing.get('version_id')}') 时验签失败: 底层文档哈希与预期不符",
                        details={"version_id": existing.get("version_id")},
                    )
                return str(existing["version_id"])
            raise ContentStoreError(
                "检测到版本冲突，但在要求的持久性读取级别下未检索到已确认的不可变正文，拒绝复用未确认版本",
                details={"model_label": model_label, "page_id": page_id, "field_name": field_name},
            )
        except pymongo.errors.WriteConcernError as exc:
            # 严格失败关闭：未达到配置要求的确认级别（如 majority 确认超时或失败），严禁自动弱化降级或返回未确认版本
            raise ContentStoreError(
                f"MongoDB 写入未能满足配置的持久性要求 (WriteConcernError: {exc})，拒绝生成版本以防数据丢失",
                details={"error": str(exc)},
            ) from exc
        except CorruptedContentPayloadError:
            raise
        except Exception as exc:
            raise ContentStoreError(f"写入 MongoDB 不可变版本失败: {exc}") from exc

    def get_version(
        self,
        version_id: str,
        expected_identity: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        获取指定 version_id 的不可变正文版本，并严格交叉核验预期身份与 SHA-256 摘要。

        失败关闭红线：
        1. expected_identity 为强制必填参数，严禁跳过身份核验；
        2. expected_identity 必须包含完整的 6 项身份键：model_label, page_id, field_name, codec, schema_version, payload_sha256；
        3. 任何一项身份不匹配或哈希损坏，立即抛出异常拒绝返回正文。

        :param version_id: 全局唯一版本 ID
        :param expected_identity: 预期的 6 项身份字典
        :return: 包含完整字段与 payload 的 MongoDB 文档字典
        :raises ContentVersionNotFoundError: 版本不存在
        :raises ContentVersionIdentityMismatchError: 身份字典缺失必填键或与文档身份不一致
        :raises CorruptedContentPayloadError: 正文 payload 哈希计算与声明不一致（数据损坏或被篡改）
        """
        if not isinstance(expected_identity, dict):
            raise ContentVersionIdentityMismatchError(
                "get_version 强制要求提供 expected_identity 身份字典，严禁无身份裸读",
                details={"version_id": version_id},
            )

        missing_keys = [k for k in REQUIRED_IDENTITY_KEYS if k not in expected_identity]
        if missing_keys:
            raise ContentVersionIdentityMismatchError(
                f"expected_identity 缺少必要身份键: {missing_keys}，拒绝读取以防身份不完整",
                details={"version_id": version_id, "missing_keys": missing_keys},
            )

        doc = self.collection.find_one({"version_id": version_id})
        if not doc:
            raise ContentVersionNotFoundError(
                f"不可变正文版本未找到: version_id={version_id}",
                details={"version_id": version_id},
            )

        # 交叉核验全部 6 项身份属性
        for key in REQUIRED_IDENTITY_KEYS:
            expected_val = expected_identity[key]
            actual_val = doc.get(key)
            if actual_val != expected_val:
                raise ContentVersionIdentityMismatchError(
                    f"正文版本身份校验失败: '{key}' 期望为 {expected_val!r}，实际存储为 {actual_val!r}",
                    details={
                        "version_id": version_id,
                        "key": key,
                        "expected": expected_val,
                        "actual": actual_val,
                    },
                )

        # 读时重算哈希，防范底层数据损坏或非授权篡改
        computed_hash = compute_canonical_hash(doc.get("payload"))
        stored_hash = doc.get("payload_sha256")
        if computed_hash != stored_hash:
            raise CorruptedContentPayloadError(
                f"不可变正文版本哈希校验损坏: version_id={version_id}，计算所得 {computed_hash} 与记录 {stored_hash} 不符",
                details={
                    "version_id": version_id,
                    "computed_hash": computed_hash,
                    "stored_hash": stored_hash,
                },
            )

        return doc