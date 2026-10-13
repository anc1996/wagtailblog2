"""F2：真实 Draftail/StreamField 表单往返的纯内存回归，不接触双库。"""

from copy import deepcopy
import json
import re
from typing import Any
from unittest.mock import patch

from django.test import SimpleTestCase
from wagtail.models import Page
from wagtail.test.utils.form_data import nested_form_data, streamfield

from external_content.codecs.streamfield import StreamFieldV1Codec
from guides.models import GuidePage, GuidePageForm


class BodyRoundtripForm(GuidePageForm):
    """仅绑定被测字段，避免主题、页面树与权限等数据库依赖。"""

    class Meta:
        model = GuidePage
        fields = ["summary", "body"]


class GuideRichTextFormRoundtripTests(SimpleTestCase):
    """恢复必须发生在模型赋值前，不能以忽略 Hash 差异伪装版本复用。"""

    def setUp(self) -> None:
        self.field = GuidePage._meta.get_field("body")
        self.codec = StreamFieldV1Codec()
        self.baseline = [
            {"type": "lead", "id": "lead-id", "value": "<p> 导读 <b>重点</b> </p>"},
            {
                "type": "step", "id": "step-id",
                "value": {
                    "title": "安装步骤",
                    "description": '<p> 步骤 <a href="https://example.org/old">资料</a> </p>',
                    "image": None,
                },
            },
            {
                "type": "tip", "id": "tip-id",
                "value": {"tip_type": "warning", "content": "<p> 注意事项 </p>"},
            },
            {
                "type": "faq", "id": "faq-id",
                "value": {"question": "如何核验？", "answer": "<p> 核验答案 </p>"},
            },
            {
                "type": "code", "id": "code-id",
                "value": {"language": "bash", "code": "echo  ok", "caption": "命令"},
            },
            {"type": "markdown_block", "id": "markdown-id", "value": " 原始  **Markdown** \n"},
        ]

    def _post_data(self, payload: list[dict[str, Any]]) -> dict[str, Any]:
        """使用各块真实 widget 生成 ContentState，独立于待修复比较函数。"""
        form_blocks = []
        rich_fields = {"step": "description", "tip": "content", "faq": "answer"}
        for item in payload:
            block = self.field.stream_block.child_blocks[item["type"]]
            value = deepcopy(item["value"])
            if item["type"] == "lead":
                value = block.field.widget.format_value(value)
            elif item["type"] in rich_fields:
                name = rich_fields[item["type"]]
                value[name] = block.child_blocks[name].field.widget.format_value(value[name])
                if item["type"] == "step":
                    value["image"] = value["image"] or ""
            form_blocks.append((item["type"], value))
        data = nested_form_data({"summary": "仅更新摘要", "body": streamfield(form_blocks)})
        for index, item in enumerate(payload):
            data[f"body-{index}-id"] = item["id"]
        return data

    def _form(self, submitted: list[dict[str, Any]], *, baseline: Any = None) -> BodyRoundtripForm:
        page = GuidePage(
            content_type_id=1, locale_id=1,
            body=deepcopy(self.baseline if baseline is None else baseline),
        )
        form = BodyRoundtripForm(data=self._post_data(submitted), instance=page)
        form.is_deferred_validation = True
        return form

    def _clean(self, form: BodyRoundtripForm) -> list[dict[str, Any]]:
        # 仅替换模型级数据库约束；真实 widget、块清洗、Form.clean 和模型赋值不替换。
        with patch.object(Page, "full_clean", return_value=None):
            self.assertTrue(form.is_valid(), form.errors.as_json())
        return self.field.stream_block.get_prep_value(form.cleaned_data["body"])

    def test_summary_only_roundtrip_restores_exact_original_payload_and_hash(self) -> None:
        """缺少恢复分支时，原始空白字节和 Hash 漂移；该测试应先红。"""
        form = self._form(self.baseline)
        widget = self.field.stream_block.child_blocks["lead"].field.widget
        source = self.baseline[0]["value"]
        normalized = widget.value_from_datadict({"lead": widget.format_value(source)}, {}, "lead")
        self.assertNotEqual(normalized, source)
        self.assertEqual(self._clean(form), self.baseline)
        self.assertEqual(form.instance.summary, "仅更新摘要")
        self.assertEqual(
            self.codec.encode(form.instance.body, field=self.field),
            self.codec.encode(self.baseline, field=self.field),
        )

    def test_real_richtext_changes_are_not_swallowed_even_with_summary_edit(self) -> None:
        """新增/删除文字、链接与样式差异均不允许复用旧字节。"""
        changes = [
            (0, None, "<p>新增导读文字</p>"),
            (0, None, "<p>导读</p>"),
            (0, None, "<p>导读 <i>重点</i></p>"),
            (1, "description", '<p>步骤 <a href="https://example.org/new">资料</a></p>'),
            (2, "content", "<p>新的警告文字</p>"),
            (3, "answer", "<p>新的核验答案</p>"),
        ]
        for index, name, value in changes:
            with self.subTest(index=index, field=name, value=value):
                submitted = deepcopy(self.baseline)
                if name is None:
                    submitted[index]["value"] = value
                else:
                    submitted[index]["value"][name] = value
                form = self._form(submitted)
                result = self._clean(form)
                actual = result[index]["value"] if name is None else result[index]["value"][name]
                self.assertEqual(re.sub(r' data-block-key="[^"]*"', "", actual), value)
                self.assertNotEqual(
                    self.codec.encode(result, field=self.field)[1],
                    self.codec.encode(self.baseline, field=self.field)[1],
                )

    def test_code_markdown_and_step_title_changes_are_not_normalized(self) -> None:
        """不能把非富文本空白当成 Draftail 噪声，原始 Markdown 必须无损。"""
        for index, name, value in [
            (1, "title", "新步骤标题"),
            (4, "code", "echo ok"),
            (5, None, "原始 **Markdown**\n"),
        ]:
            with self.subTest(index=index, field=name):
                submitted = deepcopy(self.baseline)
                if name is None:
                    submitted[index]["value"] = value
                else:
                    submitted[index]["value"][name] = value
                result = self._clean(self._form(submitted))
                actual = result[index]["value"] if name is None else result[index]["value"][name]
                self.assertEqual(actual, value)
                self.assertNotEqual(result, self.baseline)

    def test_block_identity_order_insertion_and_removal_are_real_changes(self) -> None:
        changed_id = deepcopy(self.baseline)
        changed_id[1]["id"] = "replacement-step-id"
        inserted = deepcopy(self.baseline)
        inserted.append({
            "type": "code", "id": "new-code",
            "value": {"language": "text", "code": "new", "caption": ""},
        })
        for submitted in [
            changed_id, [self.baseline[1], self.baseline[0], *self.baseline[2:]],
            inserted, self.baseline[:-1],
        ]:
            with self.subTest(ids=[item["id"] for item in submitted]):
                result = self._clean(self._form(submitted))
                self.assertNotEqual(result, self.baseline)
                self.assertEqual([item["id"] for item in result], [item["id"] for item in submitted])

    def test_explicit_clear_is_not_replaced_with_original_body(self) -> None:
        """当前 body 必填，显式清空应保留原有校验错误，不能恢复旧正文冒充成功。"""
        form = self._form([])
        with patch.object(Page, "full_clean", return_value=None):
            self.assertFalse(form.is_valid())
        self.assertIn("body", form.errors)

    def test_new_page_empty_baseline_does_not_swallow_new_content(self) -> None:
        result = self._clean(self._form(self.baseline, baseline=[]))
        self.assertEqual(len(result), len(self.baseline))
        self.assertIn("导读", result[0]["value"])

    def test_selected_edit_instance_is_baseline_not_live_or_latest_lookup(self) -> None:
        """历史/草稿实例自身即基线；本测试禁止一切数据库查询。"""
        selected = deepcopy(self.baseline)
        selected[0]["value"] = "<p> 历史或草稿导读 </p>"
        form = self._form(selected, baseline=selected)
        self.assertEqual(self._clean(form), selected)
        # full_clean 之后 instance 已被模型表单赋值，重清洗仍必须使用冻结基线。
        with patch.object(Page, "full_clean", return_value=None):
            form.full_clean()
        self.assertEqual(self.field.stream_block.get_prep_value(form.instance.body), selected)

    def test_nested_lists_inline_code_and_linebreak_roundtrip_preserves_bytes(self) -> None:
        """旧 HTML 的列表、行内代码与换行仍只允许单向编辑器往返噪声。"""
        baseline = deepcopy(self.baseline)
        baseline[0]["value"] = "<p> 第一段 <i>斜体</i> </p><p>第二段<br/> 下一行 </p>"
        baseline[1]["value"]["description"] = (
            "<ol><li> 首项 <code>echo  ok</code> </li>"
            "<li> 次项 <ul><li> 嵌套项 </li></ul></li></ol>"
        )
        baseline[2]["value"]["content"] = "<p> 注意 <code>echo  ok</code> </p>"
        result = self._clean(self._form(baseline, baseline=baseline))
        self.assertEqual(result, baseline)
        self.assertEqual(
            self.codec.encode(result, field=self.field),
            self.codec.encode(baseline, field=self.field),
        )

    def test_editor_entered_whitespace_and_paragraph_changes_are_not_swallowed(self) -> None:
        """直接改原始 ContentState，不能经 HTML 重解析预先抹平真实输入。"""
        for field_name in ["body-0-value", "body-1-value-description"]:
            for change in ["leading", "trailing", "repeated", "paragraph"]:
                with self.subTest(field=field_name, change=change):
                    form = self._form(self.baseline)
                    state = json.loads(form.data[field_name])
                    paragraph = state["blocks"][0]
                    if change == "leading":
                        paragraph["text"] = " " + paragraph["text"]
                        for ranges in ["inlineStyleRanges", "entityRanges"]:
                            for span in paragraph[ranges]:
                                span["offset"] += 1
                    elif change == "trailing":
                        paragraph["text"] += " "
                    elif change == "repeated":
                        paragraph["text"] += "  新词"
                    else:
                        added = deepcopy(paragraph)
                        added.update(key="new-paragraph", text="新段落",
                                     inlineStyleRanges=[], entityRanges=[])
                        state["blocks"].append(added)
                    raw = json.dumps(state)
                    form.data[field_name] = raw
                    if field_name == "body-0-value":
                        block = self.field.stream_block.child_blocks["lead"]
                    else:
                        block = self.field.stream_block.child_blocks["step"].child_blocks["description"]
                    expected_html = block.field.widget.value_from_datadict({"value": raw}, {}, "value")
                    result = self._clean(form)
                    actual = (result[0]["value"] if field_name == "body-0-value"
                              else result[1]["value"]["description"])
                    self.assertEqual(actual, expected_html)
                    self.assertNotEqual(
                        self.codec.encode(result, field=self.field)[1],
                        self.codec.encode(self.baseline, field=self.field)[1],
                    )
