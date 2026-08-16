"""根据当前编辑态正文生成可审阅的博客元数据建议。"""

import html
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from django.conf import settings
from django.utils.html import strip_tags


MAX_TITLE_LENGTH = 50
MAX_INTRO_LENGTH = 150
MAX_TAG_COUNT = 5
MIN_TAG_COUNT = 3
MAX_TAG_LENGTH = 20
MAX_GENERATION_ATTEMPTS = 3


class MetadataGenerationError(Exception):
    """向调用方暴露的、不会包含正文或上游响应的生成错误。"""


class MetadataConfigurationError(MetadataGenerationError):
    """外部模型配置不完整或不满足当前协议。"""


class MetadataResponseError(MetadataGenerationError):
    """外部模型没有返回符合约定的结构化元数据。"""


@dataclass(frozen=True)
class MetadataSuggestion:
    title: str
    intro: str
    tags: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "intro": self.intro, "tags": self.tags}


class ResponsesClient(Protocol):
    """隔离 OpenAI SDK，便于测试替换且不让业务代码依赖响应对象细节。"""

    def generate(self, *, instructions: str, content: str) -> str: ...


def _normalise_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _rich_text_to_text(value: Any) -> str:
    if not isinstance(value, str):
        return _normalise_text(value)
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return _normalise_text(strip_tags(value))
    if isinstance(data, dict) and isinstance(data.get("blocks"), list):
        return _normalise_text("\n".join(str(block.get("text", "")) for block in data["blocks"] if isinstance(block, dict)))
    return _normalise_text(strip_tags(value))


def _markdown_to_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(^|\n)\s{0,3}#{1,6}\s*", r"\1", text)
    return _normalise_text(re.sub(r"[>*_`~]", " ", text))


def _text_from_block(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    value = block.get("value")
    if block_type == "rich_text":
        return _rich_text_to_text(value)
    if block_type == "markdown_block":
        return _markdown_to_text(value)
    if block_type in {"code_block", "mermaid_chart"}:
        code = value.get("code") if isinstance(value, dict) else value
        label = "流程图/图表" if block_type == "mermaid_chart" else "代码"
        text = _normalise_text(code)[:1200]
        return f"[{label}]\n{text}" if text else ""
    if block_type == "table_block" and isinstance(value, dict):
        rows = value.get("data")
        if isinstance(rows, list):
            values = []
            for row in rows[:20]:
                if isinstance(row, list):
                    values.append(" | ".join(_normalise_text(strip_tags(str(cell))) for cell in row if _normalise_text(strip_tags(str(cell)))))
            return _normalise_text("\n".join(values))
    if block_type == "embed_block" and isinstance(value, dict):
        return _normalise_text(value.get("title"))
    if block_type == "raw_html":
        return _normalise_text(strip_tags(str(value or "")))
    # 媒体、文档和块 ID 不含可靠语义，不能作为外部模型上下文发送。
    return ""


def extract_body_context(body: Any, *, max_chars: int | None = None) -> str:
    """从浏览器提交的 StreamField JSON 提取受限纯文本，不读取 MongoDB。"""
    if not isinstance(body, list):
        raise MetadataGenerationError("正文格式无效。")
    limit = max_chars or getattr(settings, "AI_METADATA_MAX_CONTEXT_CHARS", 24000)
    parts = [_text_from_block(block) for block in body if isinstance(block, dict)]
    context = _normalise_text("\n\n".join(part for part in parts if part))
    if not context:
        raise MetadataGenerationError("正文中没有可用于生成元数据的文本。")
    return context[:limit]


def _clean_scalar(value: Any, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise MetadataResponseError(f"模型返回的{field_name}不是文本。")
    result = _normalise_text(html.unescape(strip_tags(value)))
    if not result or len(result) > max_length:
        raise MetadataResponseError(f"模型返回的{field_name}为空或超过长度限制。")
    return result


def validate_suggestion(payload: Any) -> MetadataSuggestion:
    """只接受窄化的 JSON 契约，避免模型输出直接写入后台表单。"""
    if not isinstance(payload, dict):
        raise MetadataResponseError("模型没有返回对象形式的元数据。")
    title = _clean_scalar(payload.get("title"), field_name="标题", max_length=MAX_TITLE_LENGTH)
    raw_intro = payload.get("intro")
    if raw_intro is None and isinstance(payload.get("description"), str):
        # 部分 OpenAI 兼容模型会稳定使用 description 命名；只接受字符串别名，保持类型边界。
        raw_intro = payload["description"]
    intro = _clean_scalar(raw_intro, field_name="简介", max_length=MAX_INTRO_LENGTH)
    raw_tags = payload.get("tags")
    if not isinstance(raw_tags, list):
        raise MetadataResponseError("模型返回的标签不是列表。")
    tags: list[str] = []
    for raw_tag in raw_tags:
        tag = _clean_scalar(raw_tag, field_name="标签", max_length=MAX_TAG_LENGTH)
        if tag.casefold() not in {item.casefold() for item in tags}:
            tags.append(tag)
    if not MIN_TAG_COUNT <= len(tags) <= MAX_TAG_COUNT:
        raise MetadataResponseError("模型返回的标签数量不在允许范围内。")
    return MetadataSuggestion(title=title, intro=intro, tags=tags)


class OpenAIResponsesClient:
    """使用 Responses API，显式关闭响应存储。"""

    def __init__(self):
        api_key = getattr(settings, "AI_METADATA_API_KEY", "")
        base_url = getattr(settings, "AI_METADATA_BASE_URL", "")
        model = getattr(settings, "AI_METADATA_MODEL", "")
        if not api_key or not base_url or not model:
            raise MetadataConfigurationError("AI 元数据生成服务尚未完成测试环境配置。")
        if getattr(settings, "AI_METADATA_PROVIDER", "openai") != "openai":
            raise MetadataConfigurationError("AI 元数据生成服务仅支持 OpenAI 兼容的 Responses API。")
        if getattr(settings, "AI_METADATA_RESPONSE_STORAGE", False):
            raise MetadataConfigurationError("AI 元数据生成服务不允许开启响应存储。")
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.reasoning_effort = getattr(settings, "AI_METADATA_REASONING_EFFORT", "")
        self.timeout_seconds = getattr(settings, "AI_METADATA_TIMEOUT_SECONDS", 60)

    def generate(self, *, instructions: str, content: str) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": content,
            "store": False,
            "timeout": self.timeout_seconds,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "blog_metadata",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string"},
                            "intro": {"type": "string"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["title", "intro", "tags"],
                    },
                }
            },
        }
        if self.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        response = self.client.responses.create(**kwargs)
        output_text = getattr(response, "output_text", "")
        if not output_text:
            raise MetadataResponseError("模型没有返回可用的元数据。")
        return output_text


def generate_metadata(
    body: Any,
    *,
    language: str = "zh-hans",
    client: ResponsesClient | None = None,
    prompt_template: dict[str, str] | None = None,
) -> MetadataSuggestion:
    """生成建议但不保存页面、标签或任何正文数据。"""
    context = extract_body_context(body)
    instructions = (
        "你是中文技术博客编辑。只能依据提供的正文生成元数据，不得补造正文没有支持的事实。"
        "标题准确、具体、不夸张，最多 50 个汉字；简介必须是最多 150 个汉字的纯文本 JSON 字符串，不能是 null、数组或对象；"
        "标签为 3 到 5 个可检索的技术主题词，每个最多 20 个汉字，避免技术、博客、教程等泛词。"
        "只返回符合 JSON schema 的结果。"
    )
    if prompt_template:
        instructions = (
            f"标题任务：{prompt_template['title']}\n"
            f"简介任务：{prompt_template['intro']}\n"
            f"标签任务：{prompt_template['tags']}\n"
            + instructions
        )
    metadata_client = client or OpenAIResponsesClient()
    content = f"页面语言：{language}\n\n正文：\n{context}"
    for attempt in range(MAX_GENERATION_ATTEMPTS):
        try:
            output = metadata_client.generate(
                instructions=instructions,
                content=content,
            )
        except MetadataGenerationError:
            raise
        except Exception as error:
            raise MetadataGenerationError("AI 元数据生成请求失败，请稍后重试。") from error
        try:
            return validate_suggestion(json.loads(output))
        except json.JSONDecodeError as error:
            response_error = MetadataResponseError("模型没有返回有效 JSON。")
            response_error.__cause__ = error
        except MetadataResponseError as error:
            response_error = error
        if attempt == MAX_GENERATION_ATTEMPTS - 1:
            raise response_error
        # 个别上游模型会偶发违背 JSON schema；次数受限，避免无限重复外发正文或消耗配额。
        instructions = f"{instructions}\n上一份输出不符合字段类型或长度约束。请重新生成，并严格返回 JSON。"

    raise MetadataResponseError("模型没有返回有效元数据。")
