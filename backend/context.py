"""
上下文管理：三层策略。

Layer 1 - 滑动窗口：trim_messages 保留最近若干 token 的消息
Layer 2 - 工具结果压缩：ToolMessage 进上下文前按工具类型精简
Layer 3 - 旧消息摘要：消息条数超阈值时，交给 LLM 压缩旧消息
"""

import json
from datetime import datetime, timezone

import tiktoken
from langchain_core.messages import (
    AIMessage, BaseMessage, SystemMessage, trim_messages,
)

from .config import settings
from .memory import get_user_memories

_encoder = tiktoken.get_encoding("cl100k_base")


def _token_counter(messages: list[BaseMessage]) -> int:
    """精确 token 计数。"""
    total = 0
    for m in messages:
        content = m.content if isinstance(m.content, str) else str(m.content)
        total += len(_encoder.encode(content))
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            total += 50 * len(m.tool_calls)
    return total


_trimmer = trim_messages(
    max_tokens=settings.max_history_tokens,
    strategy="last",
    token_counter=_token_counter,
    include_system=False,
    allow_partial=False,
    start_on="human",
)


def trim_history(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Layer 1：滑动窗口。"""
    return _trimmer.invoke(messages)


def compact_tool_result(tool_name: str, result: dict) -> dict:
    """Layer 2：工具结果压缩。"""
    # 失败结果必须原样透传：下面按工具重拼字段时会把 "error" 丢掉，
    # 于是"调用失败"在模型眼里就变成了"查询到 0 条"，用户收到的是
    # "没找到页面"而不是报错（Notion 查询失败被吞掉就是这个原因）。
    if isinstance(result, dict) and result.get("error"):
        return result

    if tool_name == "search_email":
        msgs = result.get("messages", [])
        return {
            "count": len(msgs),
            "messages": [
                {
                    "id": m.get("id"),
                    "from": m.get("from"),
                    "subject": m.get("subject"),
                    "snippet": (m.get("snippet") or "")[:200],
                }
                for m in msgs
            ],
        }

    if tool_name == "list_calendar_events":
        events = result.get("events", [])
        return {
            "count": len(events),
            "events": [
                {"title": e.get("title"), "start": e.get("start"), "end": e.get("end")}
                for e in events
            ],
        }

    if tool_name == "list_notion_pages":
        pages = result.get("pages", [])
        return {
            "count": len(pages),
            "pages": [
                {"page_id": p.get("page_id"), "title": p.get("title")}
                for p in pages
            ],
        }

    if tool_name in ("create_notion_page", "update_notion_page", "archive_notion_page"):
        return result

    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) > settings.tool_result_max_chars:
        return {"truncated": raw[: settings.tool_result_max_chars] + "..."}
    return result


async def build_system_msg(user_id: int, timezone_str: str = "Asia/Shanghai") -> SystemMessage:
    """构造系统提示，注入用户偏好和时区。"""
    memories = await get_user_memories(user_id)
    memory_block = (
        "\n".join(f"- {k}: {v}" for k, v in memories.items())
        if memories else "（暂无）"
    )

    return SystemMessage(
        content=SYSTEM_PROMPT_TEMPLATE.format(
            now=datetime.now(timezone.utc).isoformat(),
            timezone=timezone_str,
            memories=memory_block,
        )
    )


SYSTEM_PROMPT_TEMPLATE = """你是一个个人事务助理，可以帮用户管理日历、邮件、Notion 和提醒。
当前时间（UTC）：{now}
用户时区：{timezone}

已知用户长期偏好：
{memories}

规则：
1. 用户说的“明天/今晚”等相对时间，要结合当前时间转成带时区的 ISO8601。
2. 缺少必要参数时，直接追问，不要编造。
3. 需要查询信息时，先调用工具再回答。
4. 用户明确表达长期偏好时（如“以后默认会议 1 小时”），调用 remember_preference 工具。
5. 回复用简洁友好的中文。
"""


SUMMARY_PROMPT = """把以下对话压缩成不超过 5 句的中文摘要。
必须保留：
- 用户明确表达的偏好或约束
- 已完成的工具调用及关键参数（如日程标题、时间、收件人）
- 未完成的任务和挂起操作

不要保留：
- 寒暄、感谢、重复确认
- 工具的原始 JSON 数据

只输出摘要正文，不要前后缀。"""
