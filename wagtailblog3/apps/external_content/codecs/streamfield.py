"""
Wagtail Page 正文外置通用协议 - StreamField v1 编解码器。

针对 Wagtail StreamField 字段的规范化、结构校验与严格无损往返。
正文直接保存 StreamField 原始块列表，不增加外部包装层，严格保护原始 Markdown 字符串。
异常信息执行全流程脱敏与异常链解耦 (from None)，严禁在异常栈或日志中泄露正文 payload 或用户输入片段。
"""

import math
from typing import Any, Dict, List, Optional

from wagtail.blocks import StreamValue
from wagtail.fields import StreamField

from external_content.codecs.base import BaseContentCodec
from external_content.exceptions import CodecPayloadError


def _assert_no_nan_or_inf(data: Any) -> None:
    """递归检查数据中是否含有浮点数 NaN 或 Inf。"""
    if isinstance(data, float):
        if math.isnan(data) or math.isinf(data):
            raise CodecPayloadError("StreamField payload 中禁止包含 NaN 或 Inf 浮点值")
    elif isinstance(data, dict):
        for k, v in data.items():
            _assert_no_nan_or_inf(k)
            _assert_no_nan_or_inf(v)
    elif isinstance(data, (list, tuple)):
        for item in data:
            _assert_no_nan_or_inf(item)


class StreamFieldV1Codec(BaseContentCodec):
    """
    StreamField 第一版通用编解码器 (streamfield.v1)。

    契约规定：
    1. payload 为原始块列表 (list of block dicts)；
    2. 每个块必须包含稳定且非空的 id、合法的 type 与 value；
    3. markdown_block 的 Markdown 字符串保持原样，不在此处渲染为 HTML；
    4. 空哨兵统一为 Python 列表 []；
    5. None 被严格拒绝，必须显式区分未加载状态与空正文 []；
    6. 解码必须强制提供具备完整 StreamField 能力的目标 field 实例，禁止无类型校验的裸解；
    7. 解码时如遇字段未定义的未知块类型或冗余字段，必须失败关闭，严禁 Wagtail 静默丢块；
    8. 解码与重新编码执行无损对称性校验，确保绝无正文丢失；
    9. 所有底层转换及惰性求值异常使用 from None 阻断异常链，并清除 __context__，防止用户正文泄漏。
    """

    codec_id: str = "streamfield.v1"
    schema_version: int = 1

    def normalize_payload(self, value: Any, field: Any = None, **kwargs: Any) -> List[Dict[str, Any]]:
        """
        将 StreamValue 或原始块列表规范化为块列表。

        :param value: StreamValue 实例或已由块构建好的块列表
        :return: 规范化的块列表
        :raises CodecPayloadError: 输入为 None、非列表或块格式非法
        """
        if value is None:
            raise CodecPayloadError(
                "StreamField 待编码值不能为 None。显式空正文须提供空列表 [] 或空 StreamValue，以防将未加载/defer 状态误判为空正文。"
            )

        if isinstance(value, StreamValue):
            if hasattr(value, "stream_block") and hasattr(value.stream_block, "get_prep_value"):
                try:
                    raw_list = value.stream_block.get_prep_value(value)
                except Exception:
                    raise CodecPayloadError("StreamValue get_prep_value 提取原始块列表失败") from None
            elif hasattr(value, "raw_data") and value.raw_data is not None:
                raw_list = value.raw_data
            else:
                raw_list = []
        elif isinstance(value, list):
            raw_list = value
        else:
            raise CodecPayloadError(
                f"StreamField 输入必须为 StreamValue 或块列表，不支持类型: {type(value).__name__}"
            )

        self.validate_payload(raw_list)

        # 若提供了目标 StreamField 实例，在写入前提前执行严格未知块防御与无损往返验证，避免存入无法解码的孤儿版本
        if field is not None:
            if hasattr(field, "stream_block") and hasattr(field.stream_block, "child_blocks"):
                known_blocks = field.stream_block.child_blocks
                for index, block in enumerate(raw_list):
                    b_type = block.get("type")
                    if b_type not in known_blocks:
                        raise CodecPayloadError(
                            f"StreamField 第 {index} 个块类型 '{b_type}' 未在目标字段中定义，拒绝写入以防生成无法解码的孤儿版本",
                            details={"index": index, "block_type": b_type},
                        )
            self.decode(raw_list, field=field)

        return raw_list

    def validate_payload(self, payload: Any) -> None:
        """
        严格校验 payload 结构合法性与块 ID 稳定性（全流程脱敏）。

        :param payload: 待校验数据
        :raises CodecPayloadError: 非列表、缺少 id/type/value 或包含非法浮点数
        """
        if not isinstance(payload, list):
            raise CodecPayloadError(
                f"StreamField payload 必须为列表，当前类型: {type(payload).__name__}"
            )

        _assert_no_nan_or_inf(payload)

        for index, block in enumerate(payload):
            if not isinstance(block, dict):
                raise CodecPayloadError(
                    f"StreamField 第 {index} 个块必须为字典，当前类型: {type(block).__name__}"
                )

            block_type = block.get("type")
            if not isinstance(block_type, str) or not block_type.strip():
                raise CodecPayloadError(
                    f"StreamField 第 {index} 个块缺少非空有效的 'type' 属性"
                )

            if "value" not in block:
                raise CodecPayloadError(
                    f"StreamField 第 {index} 个块缺少 'value' 属性"
                )

            block_id = block.get("id")
            if not isinstance(block_id, str) or not block_id.strip():
                raise CodecPayloadError(
                    f"StreamField 第 {index} 个块缺少稳定有效的非空 'id' 属性"
                )

    def decode(self, payload: Any, field: Any = None, **kwargs: Any) -> Any:
        """
        将规范化 payload 解码为 StreamValue。

        严格防丢块与无损往返检查：
        1. 强制要求传入具备完整 StreamField 能力的目标 field 实例；
        2. 验证每个块类型是否在目标 field 的 child_blocks 中；
        3. 捕获 field.to_python 及惰性求值过程中的全部异常，阻断异常链并脱敏；
        4. 验证解码后块数量不发生减少；
        5. 验证每个块的 id 不发生漂移或被自动重新生成；
        6. 对解码产物执行反向再编码对比，若 Wagtail 静默丢弃了冗余字段或未识别属性，立即失败关闭。

        :param payload: 原始块列表
        :param field: 目标 StreamField 字段对象（强制要求，且必须具备完整 StreamField 能力）
        :return: StreamValue 运行时对象
        :raises CodecPayloadError: 缺失 field、缺少能力、块校验不通过或解码导致数据静默丢失
        """
        if field is None:
            raise CodecPayloadError(
                "StreamFieldV1Codec.decode 必须提供目标 field 实例以执行严格的类型校验与无损往返检查"
            )

        # 强制能力校验：field 必须具备 StreamField 的全部核心属性与方法，严禁绕过
        if (
            not hasattr(field, "stream_block")
            or not hasattr(field, "to_python")
            or not hasattr(field.stream_block, "child_blocks")
            or not hasattr(field.stream_block, "get_prep_value")
        ):
            raise CodecPayloadError(
                "目标 field 缺失 StreamField 必需的核心能力（stream_block/to_python/child_blocks/get_prep_value），拒绝解码以防数据丢失"
            )

        self.validate_payload(payload)

        # 1. 检查未知块类型
        known_blocks = field.stream_block.child_blocks
        for index, block in enumerate(payload):
            b_type = block["type"]
            if b_type not in known_blocks:
                raise CodecPayloadError(
                    f"StreamField 第 {index} 个块类型未在目标字段中定义，拒绝解码以防正文丢失",
                    details={"index": index},
                )

        # 2. 调用 to_python 与触发展开遍历，使用 from None 阻断异常链并脱敏
        try:
            decoded = field.to_python(payload)

            # 3. 验证解码后块数量完整性（触发 StreamValue 展开）
            if len(decoded) != len(payload):
                raise CodecPayloadError(
                    "StreamField 解码后块数量与原始块数量不一致，拒绝以防数据静默丢失",
                    details={"expected_count": len(payload), "actual_count": len(decoded)},
                )

            # 4. 验证块 ID 稳定性（逐块求值校验）
            for index, child in enumerate(decoded):
                expected_id = payload[index]["id"]
                if child.id != expected_id:
                    raise CodecPayloadError(
                        "StreamField 块解码后 ID 发生漂移，拒绝以防版本身份失效",
                        details={"index": index},
                    )
        except CodecPayloadError:
            # Codec 自身抛出的错误直接脱敏上抛
            raise
        except Exception:
            err = CodecPayloadError("StreamField 数据转换为运行时对象失败")
            err.__cause__ = None
            err.__context__ = None
            raise err from None

        # 5. 无损往返校验：确保 Wagtail 重新取值与原始 payload 完全等价
        try:
            re_prepped = field.stream_block.get_prep_value(decoded)
        except Exception:
            err = CodecPayloadError("StreamField 反向再编码校验失败")
            err.__cause__ = None
            err.__context__ = None
            raise err from None

        re_normalized = self.normalize_payload(re_prepped)
        if re_normalized != payload:
            raise CodecPayloadError(
                "StreamField 往返编解码结果不一致（可能含有未定义属性或冗余嵌套字段），拒绝以防数据静默丢失"
            )

        return decoded

    def get_empty_sentinel(self) -> List[Any]:
        """StreamField 在 MySQL/Revision 中保留的空哨兵为 []。"""
        return []