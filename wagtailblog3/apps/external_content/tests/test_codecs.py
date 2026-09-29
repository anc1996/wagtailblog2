"""
Wagtail Page 正文外置通用协议 - Codec 子系统定向测试。

验证 StreamFieldV1Codec 的规范化、哈希计算、往返编解码、空值哨兵、未知块防御、
块 ID 稳定性、强制 field 能力要求与异常链解耦 (from None) 脱敏防护。
"""

import math
from django.test import SimpleTestCase
from wagtail import blocks
from wagtail.fields import StreamField

from external_content.codecs.streamfield import StreamFieldV1Codec
from external_content.exceptions import CodecPayloadError


class SensitiveErrorBlock(blocks.CharBlock):
    """用于测试 to_python 抛出包含敏感文本异常时的脱敏与异常解耦。"""
    def to_python(self, value):
        raise ValueError(f"自定义解析错误：含用户私密文章正文片段 '{value}'")


class StreamFieldV1CodecTests(SimpleTestCase):
    """StreamFieldV1Codec 核心能力与安全边界测试。"""

    def setUp(self) -> None:
        self.codec = StreamFieldV1Codec()
        self.stream_block = blocks.StreamBlock([
            ("heading", blocks.CharBlock()),
            ("paragraph", blocks.TextBlock()),
            ("markdown", blocks.RawHTMLBlock()),
            ("sensitive", SensitiveErrorBlock()),
        ])
        self.field = StreamField(self.stream_block)

    def test_codec_identity(self) -> None:
        """验证 Codec 的稳定标识与版本号。"""
        self.assertEqual(self.codec.codec_id, "streamfield.v1")
        self.assertEqual(self.codec.schema_version, 1)

    def test_normalize_payload_deterministic(self) -> None:
        """验证不同字典键顺序输入时规范化产出严格一致。"""
        block_a = {"id": "blk-1", "type": "heading", "value": "标题测试"}
        block_b = {"type": "heading", "value": "标题测试", "id": "blk-1"}

        hash_a = self.codec.compute_payload_hash([block_a])
        hash_b = self.codec.compute_payload_hash([block_b])

        self.assertEqual(hash_a, hash_b)
        self.assertEqual(len(hash_a), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in hash_a))

    def test_encode_and_decode_roundtrip_stream_value(self) -> None:
        """验证 StreamValue 实例经过 encode 与 decode 后无损往返。"""
        raw_blocks = [
            {"type": "heading", "value": "章节一", "id": "id-001"},
            {"type": "markdown", "value": "# 一级标题\n- 列表项 1\n- 列表项 2", "id": "id-002"},
        ]
        stream_val = self.field.to_python(raw_blocks)

        encoded_payload, payload_hash = self.codec.encode(stream_val)

        self.assertIsInstance(encoded_payload, list)
        self.assertEqual(len(encoded_payload), 2)
        self.assertEqual(encoded_payload[0]["id"], "id-001")
        self.assertEqual(encoded_payload[0]["type"], "heading")
        self.assertEqual(encoded_payload[0]["value"], "章节一")
        self.assertEqual(encoded_payload[1]["value"], "# 一级标题\n- 列表项 1\n- 列表项 2")

        decoded_val = self.codec.decode(encoded_payload, field=self.field)
        self.assertEqual(len(decoded_val), 2)
        self.assertEqual(decoded_val[0].block_type, "heading")
        self.assertEqual(decoded_val[0].value, "章节一")
        self.assertEqual(decoded_val[0].id, "id-001")
        self.assertEqual(decoded_val[1].value, "# 一级标题\n- 列表项 1\n- 列表项 2")

    def test_encode_raw_list(self) -> None:
        """验证直接传入原始块列表也能正确编码并计算摘要。"""
        raw_blocks = [{"type": "paragraph", "value": "纯文本段落", "id": "id-p1"}]
        encoded_payload, payload_hash = self.codec.encode(raw_blocks)
        self.assertEqual(encoded_payload, raw_blocks)
        self.assertIsInstance(payload_hash, str)
        self.assertEqual(len(payload_hash), 64)

    def test_empty_sentinel_and_explicit_empty_list(self) -> None:
        """验证空哨兵为 []，显式空列表正文能正确往返。"""
        self.assertEqual(self.codec.get_empty_sentinel(), [])

        encoded_empty, hash_empty = self.codec.encode([])
        self.assertEqual(encoded_empty, [])

        empty_val = self.field.to_python([])
        encoded_val, hash_val = self.codec.encode(empty_val)
        self.assertEqual(encoded_val, [])
        self.assertEqual(hash_empty, hash_val)

        decoded_empty = self.codec.decode([], field=self.field)
        self.assertEqual(len(decoded_empty), 0)

    def test_encode_rejects_none_fail_closed(self) -> None:
        """验证待编码输入为 None 时失败关闭，防止将未加载状态误判为空正文。"""
        with self.assertRaises(CodecPayloadError):
            self.codec.encode(None)

    def test_decode_without_field_raises_error(self) -> None:
        """验证 decode 未提供 field 实例时必须失败关闭，严禁无契约校验的裸解。"""
        valid_payload = [{"type": "heading", "value": "标题", "id": "id-1"}]
        with self.assertRaises(CodecPayloadError) as ctx:
            self.codec.decode(valid_payload, field=None)
        self.assertIn("必须提供目标 field 实例", str(ctx.exception))

    def test_decode_rejects_field_lacking_streamfield_capabilities(self) -> None:
        """验证目标 field 缺少必需能力（如缺少 stream_block）时必须失败关闭。"""
        class IncompleteField:
            pass

        valid_payload = [{"type": "heading", "value": "标题", "id": "id-1"}]
        with self.assertRaises(CodecPayloadError) as ctx:
            self.codec.decode(valid_payload, field=IncompleteField())
        self.assertIn("缺失 StreamField 必需的核心能力", str(ctx.exception))

    def test_encode_rejects_missing_or_blank_block_id(self) -> None:
        """验证块缺少 id 或为空白 id 时必须失败关闭，防止 ID 随机漂移。"""
        with self.assertRaises(CodecPayloadError):
            self.codec.encode([{"type": "heading", "value": "测试"}])

        with self.assertRaises(CodecPayloadError):
            self.codec.encode([{"type": "heading", "value": "测试", "id": ""}])

    def test_encode_rejects_missing_block_value(self) -> None:
        """验证块缺少 value 字段时拒绝。"""
        with self.assertRaises(CodecPayloadError):
            self.codec.encode([{"type": "heading", "id": "id-1"}])

    def test_decode_rejects_unknown_block_type_fail_closed(self) -> None:
        """验证解码未知块类型时失败关闭，严禁 Wagtail 静默丢块。"""
        unknown_blocks = [
            {"type": "nonexistent_block_type", "value": "秘密用户内容", "id": "id-unk"}
        ]
        with self.assertRaises(CodecPayloadError) as ctx:
            self.codec.decode(unknown_blocks, field=self.field)
        self.assertNotIn("秘密用户内容", str(ctx.exception))

    def test_decode_rejects_unmapped_extra_attributes_fail_closed(self) -> None:
        """验证包含未定义冗余属性的块在往返校验时被拦截，严禁静默丢属性。"""
        corrupted_block = [
            {"type": "heading", "value": "标题", "id": "id-1", "unexpected_extra_key": "will_be_dropped"}
        ]
        with self.assertRaises(CodecPayloadError):
            self.codec.decode(corrupted_block, field=self.field)

    def test_decode_exception_redaction_and_chain_decoupling(self) -> None:
        """验证底层 to_python 抛出异常时完全脱敏并使用 from None 阻断异常链。"""
        sensitive_payload = [
            {"type": "sensitive", "value": "TopSecretPassword123", "id": "id-sens"}
        ]
        with self.assertRaises(CodecPayloadError) as ctx:
            self.codec.decode(sensitive_payload, field=self.field)

        exc = ctx.exception
        # 1. 异常消息中绝对不含用户私密文本
        self.assertNotIn("TopSecretPassword123", str(exc))
        self.assertNotIn("含用户私密文章正文片段", str(exc))
        # 2. 异常链完全解耦 (from None 机制保证 __cause__ 为 None 且 __suppress_context__ 为 True)
        self.assertIsNone(exc.__cause__)
        self.assertTrue(exc.__suppress_context__)

    def test_reject_non_list_payload(self) -> None:
        """验证非列表 payload 必须拒绝并抛出 CodecPayloadError。"""
        with self.assertRaises(CodecPayloadError):
            self.codec.encode("invalid string payload")

        with self.assertRaises(CodecPayloadError):
            self.codec.encode({"blocks": []})

        with self.assertRaises(CodecPayloadError):
            self.codec.decode("not a list", field=self.field)

    def test_reject_corrupted_block_structure(self) -> None:
        """验证块不是字典时拒绝。"""
        with self.assertRaises(CodecPayloadError):
            self.codec.encode(["just a string block"])

    def test_reject_nan_and_inf_payload(self) -> None:
        """验证浮点数 NaN 和 Inf 在规范化时被严格拒绝。"""
        with self.assertRaises(CodecPayloadError):
            self.codec.encode([{"type": "heading", "value": math.nan, "id": "b1"}])

        with self.assertRaises(CodecPayloadError):
            self.codec.encode([{"type": "heading", "value": math.inf, "id": "b1"}])
    def test_encode_with_target_field_rejects_unknown_block_type(self) -> None:
        """验证 encode 传入目标 field 时，若存在未定义块类型则提前失败关闭。"""
        bad_blocks = [
            {"type": "heading", "value": "正常标题", "id": "id-01"},
            {"type": "unregistered_custom_block", "value": "未定义内容", "id": "id-02"},
        ]
        with self.assertRaises(CodecPayloadError) as ctx:
            self.codec.encode(bad_blocks, field=self.field)
        self.assertIn("未在目标字段中定义", str(ctx.exception))
