# wagtailblog3/ai_backends.py
from dataclasses import dataclass
from typing import Any, Self
from wagtail_ai.ai.openai import OpenAIBackend, OpenAIBackendConfig, OpenAIResponse


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

        # =========================================================
        # 🚨 极客调试输出区（会在控制台终端打印）
        # =========================================================
        print("\n" + "="*50)
        print("🚀 [Wagtail-AI BACKENDS 引擎] 成功拦截请求！")
        print(f"🤖 目标模型 (MODEL_ID): {self.config.model_id}")
        print(f"🌐 目标网关 (API_BASE): {self.config.api_base}")
        print(f"🔑 使用 Token: {self.get_openai_api_key()[:8]}...[隐藏]")
        print(f"🌡️ 温度参数 (TEMP): {self.config.temperature}")
        print("="*50 + "\n")

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