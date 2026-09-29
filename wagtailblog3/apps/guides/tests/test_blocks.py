"""
专题指南 StreamField 块与 Codec 定向测试。

验证：
1. 七类顶层块定义合法性与 schema；
2. 块 ID、顺序与嵌套数据结构在 StreamFieldV1Codec 下的无损往返；
3. 未知块拦截与防静默丢块；
4. 原始 Markdown 字符串保持原样入库，不转义不预渲染 HTML；
5. 空哨兵 [] 序列化无副作用。
"""

from typing import Any, Dict, List
from django.test import TestCase
from wagtail.blocks import StreamValue
from wagtail.fields import StreamField

from external_content.codecs.streamfield import StreamFieldV1Codec
from external_content.exceptions import CodecPayloadError
from guides.blocks import (
    GuideCodeBlock,
    GuideFAQBlock,
    GuideLeadBlock,
    GuideReferenceBlock,
    GuideStepBlock,
    GuideStreamBlock,
    GuideTipBlock,
)


class GuideBlocksAndCodecTestCase(TestCase):
    """专题指南块与通用 Codec 编解码测试用例。"""

    def setUp(self) -> None:
        self.stream_block = GuideStreamBlock()
        self.field = StreamField(self.stream_block, use_json_field=True)
        self.codec = StreamFieldV1Codec()

    def test_seven_blocks_defined_in_stream_block(self) -> None:
        """测试七类核心块均在 GuideStreamBlock 中声明登记。"""
        child_blocks = self.stream_block.child_blocks
        expected_blocks = {
            "lead",
            "step",
            "code",
            "tip",
            "faq",
            "reference",
            "markdown_block",
        }
        self.assertTrue(expected_blocks.issubset(set(child_blocks.keys())))

    def test_all_seven_blocks_lossless_roundtrip(self) -> None:
        """测试包含七类块的完整 StreamField payload 无损编解码往返。"""
        raw_payload: List[Dict[str, Any]] = [
            {
                "id": "block-lead-01",
                "type": "lead",
                "value": "<p>这是专题指南的核心导读，帮助读者快速建立全局认识。</p>",
            },
            {
                "id": "block-step-01",
                "type": "step",
                "value": {
                    "title": "第一步：准备开发环境",
                    "description": "<p>安装必要的依赖并配置环境参数。</p>",
                    "image": None,
                },
            },
            {
                "id": "block-code-01",
                "type": "code",
                "value": {
                    "language": "python",
                    "code": "def hello():\n    return 'Hello Wagtail Guides'\n",
                    "caption": "示例代码 1.1",
                },
            },
            {
                "id": "block-tip-01",
                "type": "tip",
                "value": {
                    "tip_type": "warning",
                    "content": "<p>请勿在生产环境直接执行未经验证的脚本。</p>",
                },
            },
            {
                "id": "block-faq-01",
                "type": "faq",
                "value": {
                    "question": "如果配置失败该如何排查？",
                    "answer": "<p>请检查系统日志并在管理后台确认参数。</p>",
                },
            },
            {
                "id": "block-ref-01",
                "type": "reference",
                "value": {
                    "title": "Wagtail 官方文档",
                    "url": "https://docs.wagtail.org/",
                    "source": "Wagtail Documentation",
                },
            },
            {
                "id": "block-md-01",
                "type": "markdown_block",
                "value": "### 深入原理解析\n\n- 原生 Markdown 字符串\n- 包含公式 $E=mc^2$\n- 保障入库不丢失字符",
            },
        ]

        # 1. 规范化并校验
        normalized = self.codec.normalize_payload(raw_payload, field=self.field)
        self.assertEqual(len(normalized), 7)
        self.assertEqual(normalized[0]["id"], "block-lead-01")
        self.assertEqual(normalized[6]["id"], "block-md-01")

        # 2. 解码为 StreamValue
        decoded = self.codec.decode(normalized, field=self.field)
        self.assertIsInstance(decoded, StreamValue)
        self.assertEqual(len(decoded), 7)

        # 3. 再次提取并验证严格对称无损
        prep_value = self.field.get_prep_value(decoded)
        self.assertEqual(prep_value, raw_payload)

    def test_markdown_block_preserves_raw_string(self) -> None:
        """测试 markdown_block 严格保留原始 Markdown 字符串，绝不提前转为 HTML。"""
        raw_markdown = "# 标题\n\n```python\nprint(123)\n```\n\n- 列表项"
        raw_payload = [
            {
                "id": "md-block-test",
                "type": "markdown_block",
                "value": raw_markdown,
            }
        ]

        normalized = self.codec.normalize_payload(raw_payload, field=self.field)
        self.assertEqual(normalized[0]["value"], raw_markdown)

        decoded = self.codec.decode(normalized, field=self.field)
        prep = self.field.get_prep_value(decoded)
        self.assertEqual(prep[0]["value"], raw_markdown)

    def test_unknown_block_rejected_by_codec(self) -> None:
        """测试未在 GuideStreamBlock 中登记的未知块类型被严格拒绝。"""
        unknown_payload = [
            {
                "id": "unknown-01",
                "type": "unregistered_custom_block",
                "value": {"foo": "bar"},
            }
        ]
        with self.assertRaises(CodecPayloadError) as cm:
            self.codec.normalize_payload(unknown_payload, field=self.field)
        self.assertIn("未在目标字段中定义", str(cm.exception))

    def test_empty_stream_normalized_to_empty_list(self) -> None:
        """测试空 StreamValue 或空列表安全规范化为 []。"""
        empty_payload: List[Dict[str, Any]] = []
        normalized = self.codec.normalize_payload(empty_payload, field=self.field)
        self.assertEqual(normalized, [])

        decoded = self.codec.decode(normalized, field=self.field)
        self.assertEqual(len(decoded), 0)