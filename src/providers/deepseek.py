"""
DeepSeek API Provider

用法:
    provider = DeepSeekProvider()
    response = provider.call(system_prompt, messages)
"""

import os
import requests

from ..base_llm import BaseLLMProvider


class DeepSeekProvider(BaseLLMProvider):
    """DeepSeek API 调用封装。

    Args:
        api_key: DeepSeek API key，默认从 DEEPSEEK_API_KEY 环境变量读取
        model: 模型名，默认 deepseek-v4-pro
        max_tokens: 最大输出 token 数
        temperature: 采样温度
        budget_tokens: thinking budget（仅 deepseek 系列支持）
        timeout: 请求超时秒数
    """

    def __init__(
        self,
        api_key: str = None,
        model: str = "deepseek-v4-pro",
        max_tokens: int = 200000,
        temperature: float = 0.3,
        budget_tokens: int = 131072,
        timeout: int = 300,
    ):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.budget_tokens = budget_tokens
        self.timeout = timeout

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def call(self, system: str, messages: list[dict], temperature: float = None,
             response_format: dict = None) -> str:
        """调用 DeepSeek API，支持 per-call temperature 覆盖和 JSON Output。"""
        return self._call_api(system, messages, temperature=temperature,
                              response_format=response_format)

    def _call_api(self, system: str, messages: list[dict], temperature: float = None,
                  response_format: dict = None) -> str:
        """调用 DeepSeek chat completions API"""
        if not self.api_key:
            print("[DeepSeek] DEEPSEEK_API_KEY 未设置")
            return ""

        temp = temperature if temperature is not None else self.temperature

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                *messages,
            ],
            "max_tokens": self.max_tokens,
            "temperature": temp,
            "thinking": {"type": "enabled", "budget_tokens": self.budget_tokens},
        }
        if response_format:
            payload["response_format"] = response_format

        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            data = resp.json()

            if "choices" in data:
                msg = data["choices"][0]["message"]
                thinking = msg.get("reasoning_content", "")
                content = msg.get("content", "")
                if thinking:
                    return f"[thinking]\n{thinking}\n[/thinking]\n\n{content}"
                return content

            print(f"[LLM error] {data}")
            return ""

        except requests.RequestException as e:
            print(f"[LLM request failed] {e}")
            return ""
