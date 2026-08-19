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
        model: 模型名，默认 deepseek-v4-pro（可 DEEPSEEK_MODEL 覆盖）
        fast_model: 快速模型名（call_fast 用），默认 deepseek-v4-flash（可 DEEPSEEK_FAST_MODEL 覆盖）
        max_tokens: 最大输出 token 数
        temperature: 采样温度
        budget_tokens: thinking budget（仅 deepseek 系列支持）
        thinking: 默认是否开启 thinking（按次可用 thinking=False 关闭）
        timeout: 请求超时秒数
    """

    def __init__(
        self,
        api_key: str = None,
        model: str = None,
        fast_model: str = None,
        max_tokens: int = 200000,
        temperature: float = 0.3,
        budget_tokens: int = 131072,
        timeout: int = 300,
        thinking: bool = True,
    ):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
        self.fast_model = fast_model or os.environ.get("DEEPSEEK_FAST_MODEL", "deepseek-v4-flash")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.budget_tokens = budget_tokens
        self.timeout = timeout
        self.thinking = thinking

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def _call_api(self, system: str, messages: list[dict], temperature: float = None,
                  response_format: dict = None, model: str = None, thinking: bool = None) -> str:
        """调用 DeepSeek chat completions API

        model: 按次覆盖模型名（None = 使用 self.model）
        thinking: 按次覆盖 thinking 开关（None = 使用 self.thinking）

        失败策略（发布语义）：不再静默返回空串（旧行为会让下游显示 "dry-run" 并假装 0 订单）。
        缺 key / 网络失败 / API 异常一律抛 RuntimeError，由调用方决定响亮失败或兜底。
        """
        if not self.api_key:
            raise RuntimeError(
                "[DeepSeek] DEEPSEEK_API_KEY 未设置：请配置环境变量（或 ~/.zshrc 导出）后重试"
            )

        temp = temperature if temperature is not None else self.temperature
        use_model = model or self.model
        use_thinking = self.thinking if thinking is None else thinking

        payload = {
            "model": use_model,
            "messages": [
                {"role": "system", "content": system},
                *messages,
            ],
            "max_tokens": self.max_tokens,
            "temperature": temp,
            "thinking": (
                {"type": "enabled", "budget_tokens": self.budget_tokens}
                if use_thinking
                else {"type": "disabled"}
            ),
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

            raise RuntimeError(f"[DeepSeek] API 返回异常: {str(data)[:400]}")

        except requests.RequestException as e:
            raise RuntimeError(f"[DeepSeek] 请求失败: {e}") from e
