"""
ChatOpenAI 单例。

- max_retries=0：重试由 executor 统一管理，SDK 不许偷偷重试
- temperature=0：让工具调用更稳定，减少参数幻觉
"""
import os

from langchain_openai import ChatOpenAI

from .config import settings



_kwargs = {
    "model": settings.model,
    "api_key": settings.openai_api_key,
    "base_url": settings.openai_base_url,
    "temperature": 0,
    "max_retries": 0,
    "max_tokens": settings.max_output_tokens,
}

# Agnes 的 OpenAI 兼容 Thinking 模式
if settings.enable_thinking:
    _kwargs["extra_body"] = {
        "chat_template_kwargs": {
            "enable_thinking": True,
        }
    }

_llm = ChatOpenAI(**_kwargs)

def get_llm() -> ChatOpenAI:
    return _llm