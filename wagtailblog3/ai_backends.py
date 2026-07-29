# wagtailblog3/ai_backends.py
import logging
from dataclasses import dataclass
from typing import Any, Self
from wagtail_ai.ai.openai import OpenAIBackend, OpenAIBackendConfig, OpenAIResponse


logger = logging.getLogger(__name__)


@dataclass(kw_only=True)
class FlexibleOpenAIBackendConfig(OpenAIBackendConfig):
    temperature: float = 0.7
    # 显式声明 api_base，防止原版配置对象没有这个属性导致崩溃
    api_base: str = "https://api.openai.com/v1"

    @classmethod
    def from_settings(cls, config: Any, **kwargs: Any) -> Self:
        kwargs.setdefault("temperature", config.get("TEMPERATURE", 0.7))
        kwargs.setdefault("api_base", config.get("API_BASE", "https://api.openai.com/v1"))
        return super().from_settings(config, **kwargs)


class FlexibleOpenAIBackend(OpenAIBackend):
    config_cls = FlexibleOpenAIBackendConfig

    def chat_completions(self, messages: list[dict[str, Any]]) -> OpenAIResponse:
        import requests

        logger.info(
            "Wagtail AI request: model=%s api_base=%s temperature=%s",
            self.config.model_id,
            self.config.api_base,
            self.config.temperature,
        )

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.get_openai_api_key()}",
        }

        payload = {
            "model": self.config.model_id,
            "messages": messages,
            "max_tokens": self.config.token_limit,
            "temperature": self.config.temperature,
        }

        response = requests.post(
            f"{self.config.api_base}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.config.timeout_seconds,
        )

        response.raise_for_status()
        return OpenAIResponse(response)
