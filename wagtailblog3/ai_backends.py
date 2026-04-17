# ai_backends.py (无需修改，确保存在即可)
from dataclasses import dataclass
from typing import Any, Self
from wagtail_ai.ai.base import BaseAIBackendConfigSettings
from wagtail_ai.ai.openai import OpenAIBackend, OpenAIBackendConfig, OpenAIResponse

class OpenAICompatibleBackendConfigSettingsDict(BaseAIBackendConfigSettings):
    BASE_URL: str

@dataclass(kw_only=True)
class OpenAICompatibleBackendConfig(OpenAIBackendConfig):
    base_url: str

    @classmethod
    def from_settings(
            cls, config: OpenAICompatibleBackendConfigSettingsDict, **kwargs: Any
    ) -> Self:
        kwargs.setdefault("base_url", config.get("BASE_URL"))
        return super().from_settings(config, **kwargs)

class OpenAICompatibleBackend(OpenAIBackend):
    config_cls = OpenAICompatibleBackendConfig

    def chat_completions(self, messages: list[dict[str, Any]]) -> OpenAIResponse:
        # 这里覆盖了官方写死的 URL
        import requests
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.get_openai_api_key()}",
        }
        payload = {
            "model": self.config.model_id,
            "messages": messages,
            "max_tokens": self.config.token_limit,
        }
        # 使用配置中的 base_url
        url = f"{self.config.base_url}/chat/completions"
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return OpenAIResponse(response)