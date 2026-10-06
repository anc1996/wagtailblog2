"""
Wagtail Page 正文外置通用协议 - Codec 子系统定向测试。

验证 StreamFieldV1Codec 的规范化、哈希计算、往返编解码、空值哨兵、未知块防御、
块 ID 稳定性、强制 field 能力要求与异常链解耦 (from None) 脱敏防护。
"""

import logging
import math
import traceback
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

    def test_roundtrip_rejects_unknown_null_block_attribute(self) -> None:
        """未知块属性即使为 null 也不能在往返时静默丢失。"""
        payload = [
            {"type": "heading", "value": "标题", "id": "id-null", "unknown": None}
        ]
        for entrypoint in ("decode", "encode"):
            with self.subTest(entrypoint=entrypoint):
                with self.assertRaises(CodecPayloadError):
                    getattr(self.codec, entrypoint)(payload, field=self.field)

    def test_roundtrip_rejects_nested_unknown_null_struct_attribute(self) -> None:
        """嵌套结构未知 null 属性不能被宽泛的空值规范化掩盖。"""
        field = StreamField([
            ("section", blocks.StructBlock([
                ("details", blocks.StructBlock([("text", blocks.CharBlock())])),
            ])),
        ])
        payload = [{
            "type": "section",
            "id": "id-nested-null",
            "value": {"details": {"text": "说明", "unknown": None}},
        }]
        for entrypoint in ("decode", "encode"):
            with self.subTest(entrypoint=entrypoint):
                with self.assertRaises(CodecPayloadError):
                    getattr(self.codec, entrypoint)(payload, field=field)

    def test_roundtrip_rejects_unknown_null_in_list_and_stream_children(self) -> None:
        """列表和子 StreamBlock 的结构字段执行同一未知属性门禁。"""
        struct = blocks.StructBlock([("text", blocks.CharBlock())])
        field = StreamField([
            ("list", blocks.ListBlock(struct)),
            ("stream", blocks.StreamBlock([("details", struct)])),
        ])
        for block_type, item_type in (("list", "item"), ("stream", "details")):
            payload = [{
                "type": block_type,
                "id": "id-container",
                "value": [{
                    "type": item_type,
                    "id": "id-child",
                    "value": {"text": "说明", "unknown": None},
                }],
            }]
            with self.subTest(block_type=block_type):
                with self.assertRaises(CodecPayloadError):
                    self.codec.decode(payload, field=field)

    def test_roundtrip_allows_only_declared_default_null_additions(self) -> None:
        """可选声明字段补默认 null 合法，但原始编码载荷和摘要不得改变。"""
        details = blocks.StructBlock([
            ("text", blocks.CharBlock()),
            ("optional", blocks.IntegerBlock(required=False, default=None)),
        ])
        field = StreamField([
            ("struct", blocks.StructBlock([("details", details)])),
            ("list", blocks.ListBlock(details)),
            ("stream", blocks.StreamBlock([("details", details)])),
        ])
        payloads = (
            [{"type": "struct", "id": "id-struct", "value": {"details": {"text": "说明"}}}],
            [{
                "type": "list", "id": "id-list",
                "value": [{"type": "item", "id": "id-item", "value": {"text": "说明"}}],
            }],
            [{
                "type": "stream", "id": "id-stream",
                "value": [{"type": "details", "id": "id-details", "value": {"text": "说明"}}],
            }],
        )
        for payload in payloads:
            with self.subTest(block_type=payload[0]["type"]):
                encoded, digest = self.codec.encode(payload, field=field)
                self.assertEqual(encoded, payload)
                self.assertEqual(digest, self.codec.compute_payload_hash(payload))
                decoded = self.codec.decode(payload, field=field)
                prepared = field.stream_block.get_prep_value(decoded)
                if payload[0]["type"] == "struct":
                    actual = prepared[0]["value"]["details"]
                else:
                    actual = prepared[0]["value"][0]["value"]
                self.assertEqual(actual, {"text": "说明", "optional": None})

    def test_roundtrip_preserves_explicit_declared_null(self) -> None:
        """已明确存储的 null 保持存在，不将它转换为字段缺失。"""
        field = StreamField([
            ("details", blocks.StructBlock([
                ("optional", blocks.IntegerBlock(required=False, default=None)),
            ])),
        ])
        payload = [{"type": "details", "id": "id-explicit", "value": {"optional": None}}]
        decoded = self.codec.decode(payload, field=field)
        self.assertEqual(field.stream_block.get_prep_value(decoded), payload)

    def test_encode_unknown_type_redacts_exception_and_chain(self) -> None:
        """未知块 type 来自输入，消息、details 和格式化异常链均不得回显。"""
        marker = "".join(("synthetic-unknown-", "private-type"))
        caught_error: Exception | None = None
        try:
            self.codec.encode(
                [{"type": marker, "id": "id-private", "value": "说明"}],
                field=self.field,
            )
        except Exception as error:
            caught_error = error
        self.assertIsInstance(caught_error, CodecPayloadError)
        outputs = (
            str(caught_error),
            repr(vars(caught_error)),
            repr(caught_error.args),
            "".join(traceback.format_exception(caught_error)),
        )
        self.assertFalse(
            any(marker in output for output in outputs),
            "未知块拒绝异常不得回显输入 type",
        )

    def _assert_roundtrip_mismatch_is_redacted(self, entrypoint: str) -> None:
        """真实往返丢失额外字段时拒绝正文，且日志和异常均不得携带载荷。"""
        body_marker = "".join(("synthetic-roundtrip-", "body-private"))
        extra_marker = "".join(("synthetic-roundtrip-", "extra-private"))
        extra_key = "".join(("synthetic_roundtrip_", "extra_key"))
        block_id = "".join(("synthetic-roundtrip-", "block-id"))
        corrupted_payload = [
            {
                "type": "heading",
                "value": body_marker,
                "id": block_id,
                extra_key: extra_marker,
            }
        ]
        caught_error: Exception | None = None
        with self.assertLogs("external_content.codecs.streamfield", level="ERROR") as captured:
            try:
                getattr(self.codec, entrypoint)(corrupted_payload, field=self.field)
            except Exception as error:
                caught_error = error

        # 在异常处理之外仅断言布尔值，避免测试失败回显正文或意外异常链。
        self.assertTrue(
            isinstance(caught_error, CodecPayloadError),
            "往返损坏必须以 CodecPayloadError 失败关闭",
        )
        self.assertTrue(
            "往返编解码" in str(caught_error),
            "夹具必须触发往返一致性校验而非其他拒绝分支",
        )
        sensitive_fragments = (body_marker, extra_marker, extra_key, block_id)
        exception_output = (
            str(caught_error),
            repr(caught_error),
            repr(caught_error.args),
            repr(vars(caught_error)),
            "".join(traceback.format_exception(caught_error)),
        )
        self.assertFalse(
            any(
                fragment in output
                for fragment in sensitive_fragments
                for output in exception_output
            ),
            "往返拒绝异常及格式化异常链不得包含正文载荷",
        )
        formatter = logging.Formatter()
        for record in captured.records:
            log_output = (
                str(record.msg),
                repr(record.args),
                record.getMessage(),
                formatter.format(record),
                record.exc_text or "",
                repr(vars(record)),
            )
            self.assertFalse(
                any(
                    fragment in output
                    for fragment in sensitive_fragments
                    for output in log_output
                ),
                "往返拒绝日志消息、参数和异常输出不得包含正文载荷",
            )

    def test_decode_roundtrip_mismatch_redacts_logs_and_exception(self) -> None:
        """解码损坏的真实 StreamField 时保持失败关闭并脱敏日志。"""
        self._assert_roundtrip_mismatch_is_redacted("decode")

    def test_encode_with_field_roundtrip_mismatch_redacts_logs_and_exception(self) -> None:
        """编码提供目标字段时，同一往返校验入口不得泄露正文。"""
        self._assert_roundtrip_mismatch_is_redacted("encode")

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
