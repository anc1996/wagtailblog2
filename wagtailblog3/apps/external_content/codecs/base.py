"""
Wagtail Page 正文外置通用协议 - Codec 抽象基类。

定义字段内容编解码器协议：规范化、确定性哈希计算、编码/解码无损往返及空哨兵。
异常信息执行全流程脱敏，禁止在异常消息中带出任何正文数据或底层序列化数据片段。
"""

import abc
import hashlib
import json
from typing import Any, Tuple

from external_content.exceptions import CodecPayloadError


class BaseContentCodec(abc.ABC):
    """
    通用字段正文编解码器抽象基类。

    所有特定 Field 的 Codec 均需继承此类并实现抽象方法。
    """

    codec_id: str
    schema_version: int

    @abc.abstractmethod
    def normalize_payload(self, value: Any, field: Any = None, **kwargs: Any) -> Any:
        """
        将 Python 实例或原始输入转换为规范化、确定性的 JSON 可序列化结构。

        :param value: 字段当前值（如 StreamValue、字符串或块列表）
        :return: 规范化的 JSON 可序列化数据结构
        :raises CodecPayloadError: 输入数据损坏或无法规范化
        """
        raise NotImplementedError

    def compute_payload_hash(self, normalized_payload: Any) -> str:
        """
        计算规范化 payload 的确定性 SHA-256 小写摘要。

        严格保证：键字典排序、无额外空格、UTF-8 编码、禁止 NaN/Inf。
        异常完全脱敏，不回显底层错误附带的任何正文字符。

        :param normalized_payload: 规范化的 JSON 数据结构
        :return: 64 位小写十六进制字符串
        :raises CodecPayloadError: 包含非法浮点数或无法确定性序列化
        """
        try:
            canonical_json = json.dumps(
                normalized_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (ValueError, TypeError):
            # 严格脱敏与解耦：使用 from None 阻断异常链，防止底层 exc 输出正文片段
            raise CodecPayloadError(
                "Payload 规范化序列化失败（禁止 NaN/Inf 或包含不可序列化对象）"
            ) from None

        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def encode(self, value: Any, field: Any = None, **kwargs: Any) -> Tuple[Any, str]:
        """
        对字段值执行编码，返回 (规范化 payload, payload 摘要)。

        :param value: 待编码字段对象
        :return: (normalized_payload, payload_sha256)
        """
        normalized = self.normalize_payload(value, field=field, **kwargs)
        self.validate_payload(normalized)
        payload_hash = self.compute_payload_hash(normalized)
        return normalized, payload_hash

    @abc.abstractmethod
    def decode(self, payload: Any, field: Any = None, **kwargs: Any) -> Any:
        """
        将规范化 payload 解码为 Django/Wagtail 字段的 Python 运行时对象。

        :param payload: 从 Mongo 读取的原始规范化数据
        :param field: 目标 Django 模型 Field 实例（必须提供，用于契约校验）
        :return: 字段的运行时对象（例如 StreamValue）
        :raises CodecPayloadError: payload 数据格式错误或解码丢失字段
        """
        raise NotImplementedError

    def is_empty(self, value: Any) -> bool:
        """判断字段值是否为空或空哨兵。"""
        if value is None:
            return True
        if value == self.get_empty_sentinel():
            return True
        if hasattr(value, "__len__") and len(value) == 0:
            return True
        return False

    @property
    def empty_sentinel(self) -> Any:
        """空哨兵属性便捷访问。"""
        return self.get_empty_sentinel()

    @abc.abstractmethod
    def get_empty_sentinel(self) -> Any:
        """
        获取该字段在 MySQL / Revision 中存储的空哨兵占位值。

        :return: 空占位对象（如 StreamField 对应 []，TextField 对应 ""）
        """
        raise NotImplementedError

    @abc.abstractmethod
    def validate_payload(self, payload: Any) -> None:
        """
        校验 payload 是否符合当前 codec_id 及 schema_version 的契约要求。

        :param payload: 待校验的 payload 数据
        :raises CodecPayloadError: 校验不通过
        """
        raise NotImplementedError