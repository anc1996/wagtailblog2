"""
Wagtail Page 正文外置通用协议 - 策略注册表与基线固化。

核心契约：
1. CodecRegistry：精确类型匹配注册；自定义 Field 必须显式声明 __external_content_compatible__ = True，严禁从父类隐式继承；
2. ContentStoragePolicy：基于 Page 声明的 content_storage 与 content_storage_schema 提取外置字段与 Codec 映射；
3. 策略基线原子持久化与防漂移：同版本下策略不可被静默篡改；提供跨进程文件锁与原子替换，锁内拒绝覆盖已有异构记录；
4. 历史策略版本回溯：支持按 schema_version 回溯历史策略快照，严格重算验签快照指纹与模型身份，读路径严格只读绝不自动登记。
"""

import contextlib
from dataclasses import asdict, dataclass
import hashlib
import json
import os
import tempfile
from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple, Type, Union

from django.conf import settings
from django.db import models
from wagtail.fields import StreamField
from wagtail.models import Page

from external_content.codecs.base import BaseContentCodec
from external_content.codecs.streamfield import StreamFieldV1Codec
from external_content.exceptions import (
    CodecNotFoundError,
    FieldStorageConfigError,
)

# 允许外置的字段黑名单（严禁外置的关系、主键、元数据与权限字段）
FORBIDDEN_EXTERNAL_FIELDS: Set[str] = {
    "id",
    "page_ptr",
    "content_type",
    "live",
    "has_unpublished_changes",
    "first_published_at",
    "last_published_at",
    "go_live_at",
    "expire_at",
    "expired",
    "locked",
    "locked_at",
    "locked_by",
    "title",
    "slug",
    "url_path",
    "owner",
    "search_description",
    "show_in_menus",
    "seo_title",
    "depth",
    "numchild",
    "path",
    "translation_key",
    "locale",
    "latest_revision_created_at",
    "alias_of",
}

# 首期支持的存储后端白名单
SUPPORTED_BACKENDS: Set[str] = {"mongo"}

# 默认基线持久化文件路径（位于 external_content 应用内部）
_DEFAULT_BASELINE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "policy_baselines.json",
)

_active_baseline_file: str = _DEFAULT_BASELINE_FILE


def set_active_baseline_file(path: str) -> None:
    """设置当前活动的策略基线文件路径（供测试注入临时文件隔离）。"""
    global _active_baseline_file
    _active_baseline_file = path


def reset_active_baseline_file() -> None:
    """重置策略基线文件路径为默认值。"""
    global _active_baseline_file
    _active_baseline_file = _DEFAULT_BASELINE_FILE


def get_active_baseline_file() -> str:
    """获取当前活动的策略基线文件路径。"""
    return _active_baseline_file


@contextlib.contextmanager
def file_lock(lock_path: str):
    """跨平台跨进程排他文件锁。"""
    dir_name = os.path.dirname(os.path.abspath(lock_path))
    os.makedirs(dir_name, exist_ok=True)
    lock_file = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_file.close()


def init_baseline_file(file_path: Optional[str] = None) -> None:
    """显式初始化空的策略基线文件骨架。"""
    target_path = file_path or get_active_baseline_file()
    dir_name = os.path.dirname(os.path.abspath(target_path))
    os.makedirs(dir_name, exist_ok=True)
    init_data = {"version": 1, "policies": {}}
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(init_data, f, indent=2)


def load_policy_baselines() -> Dict[str, Any]:
    """
    从磁盘加载策略基线字典。

    失败关闭原则：指定基线文件不存在或格式损坏，必须抛出异常拒绝隐式通过。
    """
    file_path = get_active_baseline_file()
    if not os.path.exists(file_path):
        raise FieldStorageConfigError(
            f"策略基线文件不存在: {file_path}，拒绝启动以防基线丢失或无凭证启动",
            details={"file_path": file_path},
        )
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
            if not isinstance(data, dict) or "policies" not in data:
                raise ValueError("基线文件缺少顶层 'policies' 节点")
            return data
    except Exception:
        raise FieldStorageConfigError(
            f"策略基线文件损坏或格式无效: {file_path}，拒绝启动以防数据策略失控",
            details={"file_path": file_path},
        ) from None


def record_policy_baseline(
    model_label: str,
    schema_version: int,
    fingerprint: str,
    snapshot: Dict[str, Any],
) -> None:
    """
    更新策略基线：跨进程锁 + 锁内比对防覆盖 + 临时文件 + os.replace + fsync 原子持久化记录。
    若基线文件已存在但损坏，严禁清空初始化，必须失败关闭！
    若锁内检测到该版本已记录且指纹不同，拒绝覆盖！
    """
    file_path = get_active_baseline_file()
    lock_path = file_path + ".lock"

    with file_lock(lock_path):
        if not os.path.exists(file_path):
            # 文件不存在时初始化空骨架
            data = {"version": 1, "policies": {}}
        else:
            # 文件存在时严谨加载；若损坏由 load_policy_baselines 抛出异常，绝不吞没损坏
            data = load_policy_baselines()

        key = f"{model_label}:v{schema_version}"
        existing_policies = data.get("policies", {})

        if key in existing_policies:
            existing_fp = existing_policies[key].get("fingerprint")
            if existing_fp != fingerprint:
                raise FieldStorageConfigError(
                    f"模型 {model_label} (v{schema_version}) 策略已在基线中存在且指纹不符（已存在: {existing_fp}，待写入: {fingerprint}）。"
                    "严禁在并发竞争中篡改或覆盖已有版本策略基线，请升级 schema_version",
                    details={"key": key, "existing_fp": existing_fp, "new_fp": fingerprint},
                )
            # 指纹一致，幂等成功，无需重复写盘
            return

        data["policies"][key] = {
            "fingerprint": fingerprint,
            "snapshot": snapshot,
        }

        dir_name = os.path.dirname(os.path.abspath(file_path))
        os.makedirs(dir_name, exist_ok=True)

        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_baseline_")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, file_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise


@dataclass(frozen=True)
class FieldStorageConfig:
    """单个外置字段的解析配置。"""
    field_name: str
    backend: str
    codec: BaseContentCodec

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field_name": self.field_name,
            "backend": self.backend,
            "codec": self.codec.codec_id,
            "codec_schema_version": self.codec.schema_version,
        }


@dataclass(frozen=True)
class ContentStoragePolicy:
    """模型级外置字段完整存储策略。"""
    model_label: str
    schema_version: int
    external_fields: Dict[str, FieldStorageConfig]

    def to_dict(self) -> Dict[str, Any]:
        """生成策略规范化快照字典（按字段名排序以保指纹稳定）。"""
        return {
            "model_label": self.model_label,
            "schema_version": self.schema_version,
            "external_fields": {
                name: cfg.to_dict()
                for name, cfg in sorted(self.external_fields.items())
            },
        }

    def fingerprint(self) -> str:
        """计算策略配置的确定性 SHA-256 指纹。"""
        canonical_json = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class CodecRegistry:
    """
    字段编解码器注册表。

    契约规定：
    1. 字段类型精确匹配；
    2. 自定义 Field 子类严禁隐式继承，其自身 __dict__ 必须显式声明 __external_content_compatible__ = True；
    3. 禁止未经授权重复覆盖注册。
    """

    def __init__(self) -> None:
        self._codecs_by_field_type: Dict[Type[models.Field], BaseContentCodec] = {}
        self._codecs_by_id: Dict[str, BaseContentCodec] = {}

    def register(
        self,
        field_type: Type[models.Field],
        codec: BaseContentCodec,
        *,
        allow_override: bool = False,
    ) -> None:
        """
        为指定 Field 类型注册 Codec。

        :raises TypeError: field_type 非 Django Field 子类或 codec 非 BaseContentCodec 实例
        :raises FieldStorageConfigError: 未授权覆盖注册或子类未显式声明兼容
        """
        if not (isinstance(field_type, type) and issubclass(field_type, models.Field)):
            raise TypeError(f"field_type 必须是 Django models.Field 的子类，当前为: {field_type}")

        if not isinstance(codec, BaseContentCodec):
            raise TypeError(f"codec 必须是 BaseContentCodec 实例，当前为: {codec}")

        if not allow_override and field_type in self._codecs_by_field_type:
            existing = self._codecs_by_field_type[field_type]
            raise FieldStorageConfigError(
                f"Field 类型 {field_type.__name__} 已注册 Codec ({existing.codec_id})，未授权禁止覆盖注册",
                details={"field_type": field_type.__name__, "existing_codec": existing.codec_id},
            )

        self._codecs_by_field_type[field_type] = codec
        self._codecs_by_id[codec.codec_id] = codec

    def get_codec_for_field(self, field: models.Field) -> BaseContentCodec:
        """
        按 Field 实例精确查找匹配的 Codec。

        自定义子类保护：若 field 的类不是直接注册的基类，其自身类 __dict__ 必须显式声明
        __external_content_compatible__ = True，严禁从基类继承该属性。

        :raises CodecNotFoundError: 未找到已注册 Codec 或自定义子类未显式授权兼容
        """
        field_cls = type(field)

        # 1. 精确匹配
        if field_cls in self._codecs_by_field_type:
            return self._codecs_by_field_type[field_cls]

        # 2. 检查继承链中是否存在已注册基类
        matched_base: Optional[Type[models.Field]] = None
        for registered_type in self._codecs_by_field_type:
            if issubclass(field_cls, registered_type):
                matched_base = registered_type
                break

        if matched_base is not None:
            # 严格核验：__external_content_compatible__ 必须定义在 field_cls 自身 __dict__ 中
            cls_dict = getattr(field_cls, "__dict__", {})
            if cls_dict.get("__external_content_compatible__") is True:
                return self._codecs_by_field_type[matched_base]

            raise CodecNotFoundError(
                f"Field 类型 {field_cls.__name__} 继承自已支持的基类 {matched_base.__name__}，"
                f"但未在自身类定义中显式声明 '__external_content_compatible__ = True'，拒绝隐式外置以防行为漂移",
                details={"field_class": field_cls.__name__, "base_class": matched_base.__name__},
            )

        raise CodecNotFoundError(
            f"未为 Field 类型 {field_cls.__name__} 找到已注册的 Codec",
            details={"field_class": field_cls.__name__},
        )

    def get_codec(self, codec_id: str) -> BaseContentCodec:
        """按 codec_id 查找 Codec。"""
        if codec_id not in self._codecs_by_id:
            raise CodecNotFoundError(
                f"未找到标识符为 '{codec_id}' 的 Codec",
                details={"codec_id": codec_id},
            )
        return self._codecs_by_id[codec_id]


# 全局默认 Codec 注册表单例
default_codec_registry = CodecRegistry()
# 注册 Wagtail StreamField 默认 V1 编解码器
default_codec_registry.register(StreamField, StreamFieldV1Codec())


def get_storage_policy_for_model(
    model_class: Type[models.Model],
    registry: Optional[CodecRegistry] = None,
    allow_record_new: bool = True,
) -> Optional[ContentStoragePolicy]:
    """
    解析指定模型的 content_storage 与 content_storage_schema 策略。

    若模型未声明 content_storage，返回 None；
    若配置存在非法设置（非 dict、非法 schema、未知字段、关系字段、非法后端、无 Codec），均抛出 FieldStorageConfigError；
    若检测到在同一个 content_storage_schema 下修改了策略字段或配置（与磁盘基线不符），则抛出 FieldStorageConfigError 阻断策略漂移。

    :param model_class: 待解析的 Django 模型类
    :param registry: Codec 注册表，默认使用 default_codec_registry
    :param allow_record_new: 当模型尚未在基线中记录时，是否允许写入新基线（启动检查及只读解析中必须为 False）
    :return: ContentStoragePolicy 或 None
    :raises FieldStorageConfigError: 配置不合规或同版本策略非法篡改
    """
    content_storage = getattr(model_class, "content_storage", None)
    if content_storage is None:
        return None

    reg = registry or default_codec_registry

    if not isinstance(content_storage, dict):
        raise FieldStorageConfigError(
            f"模型 {model_class.__name__} 的 content_storage 必须为 dict 类型，当前为 {type(content_storage).__name__}",
            details={"model": model_class.__name__},
        )

    schema_version = getattr(model_class, "content_storage_schema", 1)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise FieldStorageConfigError(
            f"模型 {model_class.__name__} 的 content_storage_schema 必须为正整数 (>=1)，当前为: {schema_version}",
            details={"model": model_class.__name__, "schema_version": schema_version},
        )

    if not content_storage:
        raise FieldStorageConfigError(
            f"模型 {model_class.__name__} 声明了 content_storage 但字典为空，必须至少指定一个外置字段",
            details={"model": model_class.__name__},
        )

    external_fields: Dict[str, FieldStorageConfig] = {}

    for field_name, backend in content_storage.items():
        if not isinstance(field_name, str) or not field_name.strip():
            raise FieldStorageConfigError("外置字段名称必须为非空字符串")

        if field_name in FORBIDDEN_EXTERNAL_FIELDS:
            raise FieldStorageConfigError(
                f"字段 '{field_name}' 属于核心保留/关系字段，严禁配置为外置存储",
                details={"model": model_class.__name__, "field_name": field_name},
            )

        if backend not in SUPPORTED_BACKENDS:
            raise FieldStorageConfigError(
                f"字段 '{field_name}' 配置了不受支持的存储后端 '{backend}'，首期仅支持: {sorted(SUPPORTED_BACKENDS)}",
                details={"model": model_class.__name__, "field_name": field_name, "backend": backend},
            )

        try:
            field = model_class._meta.get_field(field_name)
        except Exception as exc:
            raise FieldStorageConfigError(
                f"模型 {model_class.__name__} 未找到声明的外置字段 '{field_name}'",
                details={"model": model_class.__name__, "field_name": field_name},
            ) from exc

        if getattr(field, "is_relation", False):
            raise FieldStorageConfigError(
                f"模型 {model_class.__name__} 的字段 '{field_name}' 为关系字段，严禁外置",
                details={"model": model_class.__name__, "field_name": field_name},
            )

        try:
            codec = reg.get_codec_for_field(field)
        except CodecNotFoundError as exc:
            raise FieldStorageConfigError(
                f"模型 {model_class.__name__} 的字段 '{field_name}'（类型 {type(field).__name__}）无可用的 Codec: {exc}",
                details={"model": model_class.__name__, "field_name": field_name},
            ) from exc

        external_fields[field_name] = FieldStorageConfig(
            field_name=field_name,
            backend=backend,
            codec=codec,
        )

    model_label = getattr(model_class._meta, "label_lower", model_class.__name__.lower())

    policy = ContentStoragePolicy(
        model_label=model_label,
        schema_version=schema_version,
        external_fields=external_fields,
    )

    # 查验磁盘持久化基线，校验防漂移
    history_key = f"{model_label}:v{schema_version}"
    current_fp = policy.fingerprint()
    baselines_data = load_policy_baselines()
    registered_policies = baselines_data.get("policies", {})

    if history_key in registered_policies:
        expected_fp = registered_policies[history_key].get("fingerprint")
        if expected_fp != current_fp:
            raise FieldStorageConfigError(
                f"检测到模型 {model_label} 的外置存储策略与持久化基线不符（当前 content_storage_schema 仍为 {schema_version}）。"
                "严禁在同版本下修改外置字段集合或后端；请递增 content_storage_schema 版本号并制定双读迁移或提取升级计划。",
                details={"model_label": model_label, "schema_version": schema_version},
            )
    else:
        if allow_record_new:
            record_policy_baseline(model_label, schema_version, current_fp, policy.to_dict())
        else:
            raise FieldStorageConfigError(
                f"模型 {model_label} (v{schema_version}) 尚未在策略基线中固化，拒绝未备案的外置策略",
                details={"model_label": model_label, "schema_version": schema_version},
            )

    return policy


def get_policy_for_version(
    model_class_or_label: Any,
    schema_version: int,
    registry: Optional[CodecRegistry] = None,
) -> ContentStoragePolicy:
    """
    按 schema_version 读取当前或历史版本的内容存储策略。

    安全约束：
    1. 读路径严格只读，必须传 allow_record_new=False，绝不自动登记；
    2. 历史快照还原路径严格核验 model_label 与 schema_version 身份；
    3. 重建策略后必须重算验签 fingerprint，指纹不符即刻失败关闭。

    :param model_class_or_label: Django 模型类或小写模型标识（如 guides.guidepage）
    :param schema_version: 目标存储架构版本号
    :param registry: Codec 注册表
    :return: ContentStoragePolicy
    :raises FieldStorageConfigError: 未找到对应版本策略、快照损坏或验签失败
    """
    reg = registry or default_codec_registry

    if isinstance(model_class_or_label, str):
        model_label = model_class_or_label.lower()
        model_cls = None
    else:
        model_cls = model_class_or_label
        model_label = getattr(model_cls._meta, "label_lower", model_cls.__name__.lower())

    # 若请求的版本与当前代码模型定义的版本一致且有模型类，直接解析校验当前策略（严格只读）
    if model_cls is not None:
        current_schema = getattr(model_cls, "content_storage_schema", 1)
        if current_schema == schema_version:
            policy = get_storage_policy_for_model(model_cls, registry=reg, allow_record_new=False)
            if policy is not None:
                return policy

    # 否则从持久化基线快照中还原对应版本策略
    history_key = f"{model_label}:v{schema_version}"
    baselines = load_policy_baselines()
    registered_policies = baselines.get("policies", {})
    if history_key not in registered_policies:
        raise FieldStorageConfigError(
            f"未在策略基线中找到模型 {model_label} 的历史版本 v{schema_version} 策略记录，拒绝未知版本正文反序列化",
            details={"model_label": model_label, "schema_version": schema_version},
        )

    entry = registered_policies[history_key]
    recorded_fp = entry.get("fingerprint")
    snapshot = entry.get("snapshot", {})

    # 1. 严格核验快照身份
    snap_label = snapshot.get("model_label")
    snap_schema = snapshot.get("schema_version")
    if snap_label != model_label:
        raise FieldStorageConfigError(
            f"历史基线快照模型标识 ('{snap_label}') 与查询目标 ('{model_label}') 不符，阻断非法还原",
            details={"snapshot_label": snap_label, "target_label": model_label},
        )
    if snap_schema != schema_version:
        raise FieldStorageConfigError(
            f"历史基线快照 schema_version ({snap_schema}) 与查询目标 ({schema_version}) 不符，阻断非法还原",
            details={"snapshot_schema": snap_schema, "target_schema": schema_version},
        )

    # 2. 重建字段配置与 Codec
    ext_fields_data = snapshot.get("external_fields", {})
    reconstructed_fields: Dict[str, FieldStorageConfig] = {}

    for fname, fcfg in ext_fields_data.items():
        codec_id = fcfg.get("codec")
        expected_codec_schema = fcfg.get("codec_schema_version", 1)
        backend = fcfg.get("backend", "mongo")
        try:
            codec = reg.get_codec(codec_id)
        except CodecNotFoundError as exc:
            raise FieldStorageConfigError(
                f"历史策略 {history_key} 中字段 '{fname}' 所需 Codec '{codec_id}' 在当前注册表中未找到: {exc}",
                details={"history_key": history_key, "codec_id": codec_id},
            ) from exc

        if codec.schema_version != expected_codec_schema:
            raise FieldStorageConfigError(
                f"历史策略 {history_key} 中字段 '{fname}' 的 Codec '{codec_id}' 期望 schema_version={expected_codec_schema}，但当前注册的 Codec 版本为 {codec.schema_version}",
                details={"history_key": history_key, "expected": expected_codec_schema, "actual": codec.schema_version},
            )

        reconstructed_fields[fname] = FieldStorageConfig(
            field_name=fname,
            backend=backend,
            codec=codec,
        )

    reconstructed_policy = ContentStoragePolicy(
        model_label=model_label,
        schema_version=schema_version,
        external_fields=reconstructed_fields,
    )

    # 3. 严格重算验签快照指纹
    if reconstructed_policy.fingerprint() != recorded_fp:
        raise FieldStorageConfigError(
            f"历史基线快照指纹验签失败（记录指纹: {recorded_fp}，重建指纹: {reconstructed_policy.fingerprint()}），快照已损坏",
            details={"history_key": history_key},
        )

    return reconstructed_policy