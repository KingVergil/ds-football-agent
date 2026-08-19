"""
DSFootball — LLM Provider 抽象基类

所有 LLM provider 继承此类，实现 _call_api 即可。
call() 统一处理 thinking 内容拼接。
"""

import os
import re
from abc import ABC, abstractmethod


class BaseLLMProvider(ABC):
    """LLM 调用抽象。

    子类只需实现 _call_api(system, messages) -> str，
    调用方统一使用 call(system, messages) -> str。

    模型策略：
    - call() 默认使用 provider 的 model（pro，thinking 开启），用于最终分析/归因/生成；
    - call_fast() 使用 fast_model + thinking 关闭，用于辅助性步骤（去重判断、摘要等）。
    """

    # 快速模型名（call_fast 使用），子类可覆盖或由环境变量配置
    fast_model: str = "deepseek-v4-flash"

    @abstractmethod
    def _call_api(self, system: str, messages: list[dict], temperature: float = None,
                  response_format: dict = None, model: str = None, thinking: bool = None) -> str:
        """发送请求，返回原始响应文本。子类必须实现。"""
        ...

    def call(self, system: str, messages: list[dict], temperature: float = None,
             response_format: dict = None, model: str = None, thinking: bool = None) -> str:
        """统一入口：调用 _call_api。

        model/thinking 为按次覆盖（None = 使用 provider 默认）。
        """
        return self._call_api(system, messages, temperature=temperature,
                              response_format=response_format, model=model, thinking=thinking)

    def call_fast(self, system: str, messages: list[dict], temperature: float = None,
                  response_format: dict = None) -> str:
        """快捷入口：fast 模型 + 关闭 thinking，用于非最终分析/归因/生成的辅助步骤。"""
        return self.call(system, messages, temperature=temperature,
                         response_format=response_format, model=self.fast_model, thinking=False)

    @staticmethod
    def strip_thinking(text: str) -> str:
        """移除 [thinking]...[/thinking] 块，用于解析结构化输出前清洗。"""
        return re.sub(
            r'\[thinking\].*?\[/thinking\]\s*', '',
            text, flags=re.DOTALL
        ).strip()
