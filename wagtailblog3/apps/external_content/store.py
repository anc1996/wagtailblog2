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
import threading
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
import uuid

from django.conf import settings
from datetime import datetime, timezone as dt_timezone
import re
import pymongo
from pymongo.collection import Collection
from pymongo.errors import (
    AutoReconnect,
    ConnectionFailure,
    DuplicateKeyError,
    ExecutionTimeout,
    NetworkTimeout,
    OperationFailure,
    PyMongoError,
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

logger = logging.getLogger(__name__)

COLLECTION_NAME = "external_content_versions"
_active_collection_name: Optional[str] = None


def set_active_collection_name(name: Optional[str]) -> None:
    """设置当前活跃的不可变版本集合名（用于测试环境隔离）。"""
    global _active_collection_name
    _active_collection_name = name


def reset_active_collection_name() -> None:
    """重置当前活跃的集合名为默认集合。"""
    global _active_collection_name
    _active_collection_name = None


def get_active_collection_name() -> str:
    """获取当前生效的集合名。"""
    return _active_collection_name or COLLECTION_NAME

MODEL_LABEL_REGEX = re.compile(r"\A[a-z0-9_]+\.[a-z0-9_]+\Z")
SHA256_HEX_REGEX = re.compile(r"\A[0-9a-f]{64}\Z")
FIELD_NAME_REGEX = re.compile(r"\A[a-z0-9_]+\Z")
CODEC_REGEX = re.compile(r"\A[a-zA-Z0-9_.-]+\Z")
_mongo_client: Optional[pymongo.MongoClient] = None
_client_lock = threading.Lock()

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

def get_mongo_settings() -> Dict[str, Any]:
    """安全获取 Django Mongo 配置，若 settings 未配置或缺失则平稳回退至空字典。"""
    try:
        configured = getattr(settings, "configured", False)
    except Exception as exc:
        raise ContentStoreError(
            "读取 Django settings 配置状态发生异常",
            details={"error_category": "settings_error"},
        ) from exc

    if not configured:
        return {}

    try:
        if not hasattr(settings, "MONGO_DB"):
            raise ContentStoreError(
                "Django settings 已配置但缺失 'MONGO_DB' 配置项，显式失败关闭",
                details={"error_category": "settings_error"},
            )
        mongo_db = getattr(settings, "MONGO_DB")
        if not isinstance(mongo_db, dict):
            raise ContentStoreError(
                f"Django settings 中的 'MONGO_DB' 必须为 dict 类型，当前为: {type(mongo_db).__name__}",
                details={"error_category": "settings_error"},
            )

        # 严格校验必填键 NAME, HOST, PORT
        for required_key in ("NAME", "HOST", "PORT"):
            if required_key not in mongo_db:
                raise ContentStoreError(
                    f"Django settings 'MONGO_DB' 缺失必填配置键: {required_key}",
                    details={"error_category": "settings_error", "field": required_key},
                )

        name = mongo_db["NAME"]
        if not isinstance(name, str) or not name.strip():
            raise ContentStoreError(
                "Django settings 'MONGO_DB.NAME' 必须为非空字符串",
                details={"error_category": "settings_error", "field": "NAME"},
            )

        host = mongo_db["HOST"]
        if not isinstance(host, str) or not host.strip():
            raise ContentStoreError(
                "Django settings 'MONGO_DB.HOST' 必须为非空字符串",
                details={"error_category": "settings_error", "field": "HOST"},
            )

        port = mongo_db["PORT"]
        if type(port) is not int or isinstance(port, bool) or not (1 <= port <= 65535):
            raise ContentStoreError(
                "Django settings 'MONGO_DB.PORT' 必须为 1~65535 范围内的整数",
                details={"error_category": "settings_error", "field": "PORT"},
            )

        return mongo_db
    except ContentStoreError:
        raise
    except Exception as exc:
        raise ContentStoreError(
            "读取 Django settings 的 'MONGO_DB' 配置发生异常",
            details={"error_category": "settings_error"},
        ) from exc


def classify_mongo_exception(exc: BaseException) -> Tuple[str, str]:
    """
    对底层数据库或驱动异常进行有限错误码分类与安全脱敏。

    杜绝将原始异常字符串（可能包含敏感连接凭据、IP、密码等）或正文 payload 暴露，
    仅向调用方提供受控分类码与安全描述，同时在异常链中保留 __cause__ 以供内部排查。

    :param exc: 捕获的底层异常对象
    :return: Tuple[error_category, safe_message] 有限错误码分类与脱敏说明
    """
    if isinstance(
        exc,
        (ServerSelectionTimeoutError, NetworkTimeout, ExecutionTimeout, AutoReconnect, ConnectionFailure),
    ):
        return "network_timeout", "MongoDB 网络连接或操作超时，无法连接至集群"
    if isinstance(exc, WriteConcernError):
        return "write_concern_error", "MongoDB 写入未能满足配置的持久性要求 (WriteConcern 未确认)"
    if isinstance(exc, OperationFailure):
        return "operation_failure", "MongoDB 操作执行失败或权限拒绝"
    if isinstance(exc, PyMongoError):
        return "mongo_error", "MongoDB 底层仓储发生驱动异常"
    return "storage_error", "不可变正文仓储发生未知异常"


def validate_identity(identity: Any, *, context: str = "identity") -> Dict[str, Any]:
    """
    严格校验正文版本的 6 维强类型业务身份。

    严格规则：
    1. 必须为字典对象；
    2. 必须完整包含 6 项身份键；
    3. model_label 必须符合小写 app.model 规范正则；
    4. page_id 必须为正整数 (严格拒绝 bool 和 float)；
    5. field_name 必须为非空字符串；
    6. codec 必须为非空字符串；
    7. schema_version 必须为正整数 (严格拒绝 bool 和 float)；
    8. payload_sha256 必须为 64 位小写十六进制字符串。

    :param identity: 待校验的字典对象
    :param context: 报错上下文标识
    :return: 校验合规的 6 身份字典
    :raises ContentVersionIdentityMismatchError: 任何校验不合规时立即阻断
    """
    if not isinstance(identity, dict):
        raise ContentVersionIdentityMismatchError(
            f"{context} 必须为字典对象，拒绝非字典类型",
            details={"error_category": "identity_mismatch", "context": context},
        )

    missing = [k for k in REQUIRED_IDENTITY_KEYS if k not in identity]
    if missing:
        raise ContentVersionIdentityMismatchError(
            f"{context} 缺少必要身份键: {missing}，拒绝处理以防身份不完整",
            details={"error_category": "identity_mismatch", "context": context, "missing_keys": missing},
        )

    # 1. model_label: 必须是小写 app.model 格式
    model_label = identity["model_label"]
    if not isinstance(model_label, str) or not MODEL_LABEL_REGEX.fullmatch(model_label):
        raise ContentVersionIdentityMismatchError(
            f"{context}.model_label 格式不符合小写 app.model 规范",
            details={"error_category": "identity_mismatch", "context": context, "field": "model_label"},
        )

    # 2. page_id: 必须是正整数 (严格拒绝 bool / float / <=0)
    page_id = identity["page_id"]
    if type(page_id) is not int or isinstance(page_id, bool) or page_id <= 0:
        raise ContentVersionIdentityMismatchError(
            f"{context}.page_id 必须为正整数 (严格拒绝 bool/float/非正数)",
            details={"error_category": "identity_mismatch", "context": context, "field": "page_id"},
        )

    # 3. field_name: 必须为非空字符串且符合小写字母数字下划线规范
    field_name = identity["field_name"]
    if not isinstance(field_name, str) or not FIELD_NAME_REGEX.fullmatch(field_name):
        raise ContentVersionIdentityMismatchError(
            f"{context}.field_name 格式不符合规范或包含非法字符",
            details={"error_category": "identity_mismatch", "context": context, "field": "field_name"},
        )

    # 4. codec: 必须为非空字符串且符合规范字符集
    codec = identity["codec"]
    if not isinstance(codec, str) or not CODEC_REGEX.fullmatch(codec):
        raise ContentVersionIdentityMismatchError(
            f"{context}.codec 格式不符合规范或包含非法字符",
            details={"error_category": "identity_mismatch", "context": context, "field": "codec"},
        )

    # 5. schema_version: 必须是正整数 (严格拒绝 bool / float / <=0)
    schema_version = identity["schema_version"]
    if type(schema_version) is not int or isinstance(schema_version, bool) or schema_version <= 0:
        raise ContentVersionIdentityMismatchError(
            f"{context}.schema_version 必须为正整数 (严格拒绝 bool/float/非正数)",
            details={"error_category": "identity_mismatch", "context": context, "field": "schema_version"},
        )

    # 6. payload_sha256: 必须为 64 位小写十六进制字符串
    payload_sha256 = identity["payload_sha256"]
    if not isinstance(payload_sha256, str) or not SHA256_HEX_REGEX.fullmatch(payload_sha256):
        raise ContentVersionIdentityMismatchError(
            f"{context}.payload_sha256 必须为 64 位小写十六进制字符串",
            details={"error_category": "identity_mismatch", "context": context, "field": "payload_sha256"},
        )

    return {
        "model_label": model_label,
        "page_id": page_id,
        "field_name": field_name,
        "codec": codec,
        "schema_version": schema_version,
        "payload_sha256": payload_sha256,
    }


def get_mongo_client() -> pymongo.MongoClient:
    """获取或初始化共享 MongoClient 实例（并发线程安全双重检查锁）。"""
    global _mongo_client
    if _mongo_client is None:
        with _client_lock:
            if _mongo_client is None:
                mongo_settings = get_mongo_settings()
                if not mongo_settings:
                    raise ContentStoreError(
                        "未配置 MongoDB 设置，无法建立数据库连接",
                        details={"error_category": "settings_error"},
                    )
                host = mongo_settings["HOST"]
                port = mongo_settings["PORT"]
                try:
                    _mongo_client = pymongo.MongoClient(
                        host=host,
                        port=port,
                        serverSelectionTimeoutMS=5000,
                    )
                except Exception as exc:
                    _mongo_client = None
                    category, safe_msg = classify_mongo_exception(exc)
                    raise ContentStoreError(safe_msg, details={"error_category": category}) from exc
    return _mongo_client


def reset_mongo_client() -> None:
    """重置共享 MongoClient 实例（供测试与连接刷新使用）。"""
    global _mongo_client
    with _client_lock:
        if _mongo_client is not None:
            try:
                _mongo_client.close()
            except Exception:
                pass
            _mongo_client = None


def get_write_concern() -> pymongo.write_concern.WriteConcern:
    """
    根据配置解析不可变版本集合的写入关注 (Write Concern)。

    默认配置为安全优先的 majority + journal。
    在单节点测试或未启用副本集的独立开发环境中，支持由配置覆盖。
    """
    mongo_settings = get_mongo_settings()
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
    mongo_settings = get_mongo_settings()
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
    mongo_settings = get_mongo_settings()
    if not mongo_settings:
        raise ContentStoreError(
            "未配置 MongoDB 设置，无法获取不可变正文集合",
            details={"error_category": "settings_error"},
        )
    cli = client or get_mongo_client()
    db_name = mongo_settings["NAME"]
    wc = write_concern if write_concern is not None else get_write_concern()
    rc = read_concern if read_concern is not None else get_read_concern()
    db = cli[db_name]
    coll_name = get_active_collection_name()
    return db.get_collection(coll_name, write_concern=wc, read_concern=rc)


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
    try:
        canonical_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (ValueError, TypeError) as exc:
        raise ContentStoreError(
            "Payload 序列化失败，包含无效值（如 NaN/Inf）或不可序列化对象",
            details={"error_category": "invalid_payload"},
        ) from exc
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

    @property
    def collection(self) -> Collection:
        if self._collection is None:
            self._collection = get_collection()
        return self._collection

    def check_indexes_ready(self) -> None:
        """
        断言集合已正确配置必要唯一索引，未就绪时阻断写入以防幂等失效。
        动态实时核验索引，杜绝永久缓存导致的外部索引漂移/误删安全漏洞。
        """
        is_ready, errors = check_indexes(self.collection)
        if not is_ready:
            raise ContentStoreError(
                f"MongoDB 不可变正文集合 ({self.collection.name}) 唯一索引未就绪，阻断写入以防破坏不可变幂等约束: {errors}。"
                "请先运行 'python manage.py init_external_content_indexes' 初始化索引"
            )

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
        持久化保存不可变字段正文版本（兼容调用）。

        :return: version_id (唯一版本标识)
        """
        vid, _ = self.save_version_with_status(
            model_label=model_label,
            page_id=page_id,
            field_name=field_name,
            codec=codec,
            schema_version=schema_version,
            payload_sha256=payload_sha256,
            payload=payload,
        )
        return vid

    def save_version_with_status(
        self,
        *,
        model_label: str,
        page_id: int,
        field_name: str,
        codec: str,
        schema_version: int,
        payload_sha256: str,
        payload: Any,
    ) -> Tuple[str, bool]:
        """
        持久化保存不可变字段正文版本，并明确返回是否为新创建的文档。

        若同页、同字段、同内容已存在，则幂等返回现存 (version_id, False)；
        若本次成功新插入文档，则返回 (new_version_id, True)。
        复用前必须重算比对现存文档的 payload 哈希，确保底层数据未发生位翻转或篡改。

        :return: Tuple[version_id, is_new] (版本标识, 是否为新插入)
        :raises ContentStoreError: 写入或查询失败，或索引未就绪
        :raises CorruptedContentPayloadError: 冲突复用时检测到底层现存记录损坏
        """
        # 前置检查唯一索引完备性
        if self.enforce_index_check:
            self.check_indexes_ready()

        # 前置校验 6 键强类型身份
        validate_identity({
            "model_label": model_label,
            "page_id": page_id,
            "field_name": field_name,
            "codec": codec,
            "schema_version": schema_version,
            "payload_sha256": payload_sha256,
        }, context="save_version")

        # 前置核对哈希自洽性
        computed_hash = compute_canonical_hash(payload)
        if computed_hash != payload_sha256:
            raise ContentStoreError(
                "传入的 payload_sha256 与正文计算哈希不一致，阻断写入",
                details={"error_category": "hash_mismatch"},
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
            "created_at": datetime.now(dt_timezone.utc),
        }

        try:
            self.collection.insert_one(doc)
            return new_version_id, True
        except DuplicateKeyError:
            # 唯一索引冲突：存在相同归属与哈希的已有版本，无条件强制使用 majority ReadConcern 检索
            # 坚决不沿用 collection/settings 可能降级的 local 读取级别，
            # 确保只有已达到多数派持久性确认的版本才被复用，彻底拦截 WriteConcern 未确认版本的重试绕行
            majority_rc = pymongo.read_concern.ReadConcern(level="majority")
            read_coll = self.collection.with_options(read_concern=majority_rc)
            try:
                existing = read_coll.find_one({
                    "model_label": model_label,
                    "page_id": page_id,
                    "field_name": field_name,
                    "codec": codec,
                    "schema_version": schema_version,
                    "payload_sha256": payload_sha256,
                })
            except Exception as exc:
                category, safe_msg = classify_mongo_exception(exc)
                raise ContentStoreError(safe_msg, details={"error_category": category}) from exc
            if existing and "version_id" in existing:
                vid = existing.get("version_id")
                if not isinstance(vid, str) or not vid.strip():
                    raise ContentStoreError(
                        "底层文档的 version_id 无效",
                        details={"error_category": "invalid_version_id"},
                    )
                # 校验底层已存文档的 6 身份及强类型
                validate_identity(existing, context="existing_document")
                for key in REQUIRED_IDENTITY_KEYS:
                    expected_val = locals()[key]
                    actual_val = existing.get(key)
                    if type(actual_val) is not type(expected_val) or actual_val != expected_val:
                        raise ContentVersionIdentityMismatchError(
                            f"底层文档身份字段 '{key}' 与传入不匹配",
                            details={"field": key, "error_category": "identity_mismatch"},
                        )
                # 交叉重算现存文档的 payload 哈希验签，防止复用到底层损坏的记录
                existing_payload = existing.get("payload")
                recomputed = compute_canonical_hash(existing_payload)
                if recomputed != payload_sha256 or existing.get("payload_sha256") != payload_sha256:
                    raise CorruptedContentPayloadError(
                        "复用现存不可变版本时验签失败: 底层文档哈希与预期不符",
                        details={"error_category": "corrupted_payload"},
                    )
                return str(existing["version_id"]), False
            raise ContentStoreError(
                "检测到版本冲突，但在 majority 读取级别下未检索到已确认的不可变正文，拒绝复用未确认版本",
                details={"error_category": "unconfirmed_version_conflict"},
            )
        except CorruptedContentPayloadError:
            raise
        except ContentVersionIdentityMismatchError:
            raise
        except ContentStoreError:
            raise
        except Exception as exc:
            category, safe_msg = classify_mongo_exception(exc)
            raise ContentStoreError(safe_msg, details={"error_category": category}) from exc

    def cleanup_unreferenced_version(self, version_id: str, page_id: int) -> bool:
        """严格同步硬熔断：禁止同步清理未引用的外部正文版本，必须由异步 GC 或安全期机制处理。"""
        raise ExternalContentOperationBlockedError(
            "禁止同步清理未引用的外部正文版本，必须由异步 GC 或安全期巡检机制安全处理，防止事务回滚竞争误删正文"
        )

    def get_raw_version_document(self, version_id: str) -> Optional[Dict[str, Any]]:
        """仅供排查与巡检的底层原始版本文档查询，不执行严格的 6 键业务身份校验。"""
        if not isinstance(version_id, str) or not version_id.strip():
            return None
        try:
            return self.collection.find_one({"version_id": version_id})
        except Exception as exc:
            category, safe_msg = classify_mongo_exception(exc)
            raise ContentStoreError(safe_msg, details={"error_category": category}) from exc

    def delete_version(
        self,
        version_id: str,
        *,
        expected_identity: Dict[str, Any],
    ) -> bool:
        """
        根据 version_id 及完整 6 键身份严格核验后物理删除指定的不可变正文文档。

        仅供 GC 回收器在安全隔离期过后、确认全局无引用时调用。

        :param version_id: 版本全局唯一标识
        :param expected_identity: 预期的完整 6 键身份字典
        :return: bool 是否成功删除了文档（若文档本不存在则返回 False）
        """
        if not isinstance(version_id, str) or not version_id.strip():
            raise ContentVersionIdentityMismatchError(
                "delete_version 的 version_id 必须为非空字符串",
                details={"error_category": "invalid_version_id", "field": "version_id"},
            )
        validated_identity = validate_identity(expected_identity, context="expected_identity")

        if self.enforce_index_check:
            self.check_indexes_ready()

        try:
            existing = self.collection.find_one({"version_id": version_id})
        except Exception as exc:
            category, safe_msg = classify_mongo_exception(exc)
            raise ContentStoreError(safe_msg, details={"error_category": category}) from exc

        if existing is None:
            return False

        validate_identity(existing, context="stored_document")
        for key in REQUIRED_IDENTITY_KEYS:
            exp_val = validated_identity[key]
            act_val = existing.get(key)
            if type(act_val) is not type(exp_val) or act_val != exp_val:
                raise ContentVersionIdentityMismatchError(
                    f"删除核验失败，字段 '{key}' 身份不匹配",
                    details={"field": key, "error_category": "identity_mismatch"},
                )

        selector = dict(validated_identity, version_id=version_id)
        try:
            res = self.collection.delete_one(selector)
            return bool(res.deleted_count > 0)
        except Exception as exc:
            category, safe_msg = classify_mongo_exception(exc)
            raise ContentStoreError(safe_msg, details={"error_category": category}) from exc

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

        if not isinstance(version_id, str) or not version_id.strip():
            raise ContentVersionIdentityMismatchError(
                "get_version 的 version_id 必须为非空字符串",
                details={"error_category": "invalid_version_id", "field": "version_id"},
            )
        validated_expected = validate_identity(expected_identity, context="expected_identity")

        try:
            doc = self.collection.find_one({"version_id": version_id})
        except Exception as exc:
            category, safe_msg = classify_mongo_exception(exc)
            raise ContentStoreError(safe_msg, details={"error_category": category}) from exc

        if not doc:
            raise ContentVersionNotFoundError(
                "不可变正文版本未找到",
                details={"error_category": "version_not_found"},
            )

        # 校验库中文档的 6 身份合法性
        validate_identity(doc, context="stored_document")

        # 强类型双向比对全部 6 项业务身份
        for key in REQUIRED_IDENTITY_KEYS:
            expected_val = validated_expected[key]
            actual_val = doc.get(key)
            if type(actual_val) is not type(expected_val) or actual_val != expected_val:
                raise ContentVersionIdentityMismatchError(
                    f"正文版本身份校验失败: '{key}' 身份不匹配",
                    details={
                        "field": key,
                        "error_category": "identity_mismatch",
                    },
                )

        # 读时重算哈希，防范底层数据损坏或非授权篡改
        computed_hash = compute_canonical_hash(doc.get("payload"))
        stored_hash = doc.get("payload_sha256")
        if computed_hash != stored_hash:
            raise CorruptedContentPayloadError(
                "不可变正文版本哈希校验损坏，计算所得哈希与记录不符",
                details={
                    "error_category": "corrupted_payload",
                },
            )

        return doc
