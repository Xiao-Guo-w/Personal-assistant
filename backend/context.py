"""
上下文管理：三层策略。

Layer 1 - 滑动窗口：trim_messages 保留最近若干 token 的消息
Layer 2 - 工具结果压缩：ToolMessage 进上下文前按工具类型精简
Layer 3 - 旧消息摘要：消息条数超阈值时，交给 LLM 压缩旧消息

对外接口：
- trim_history(messages)                → 裁剪后的消息列表
- compact_tool_result(name, result)     → 压缩后的工具结果 dict
- build_system_msg(user_id, timezone)   → 带用户偏好的系统提示
"""

import json
from datetime import datetime, timezone

import tiktoken
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    SystemMessage,
    trim_messages,
)

from .config import settings
from .memory import get_user_memories

# tiktoken 编码器：gpt-4o-mini 用 cl100k_base
_encoder = tiktoken.get_encoding("cl100k_base")


def _token_counter(messages: list[BaseMessage]) -> int:
    """
    精确计数 token 数。

    trim_messages 会调用这个函数，超过 max_tokens 就裁掉最老的消息。
    用 tiktoken 而不是字符数估算，避免中英文混排时估算偏差。
    """
    total = 0
    for m in messages:
        content = m.content if isinstance(m.content, str) else str(m.content)
        total += len(_encoder.encode(content))
        # 工具调用也会占用 token，粗略加个权重
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            total += 50 * len(m.tool_calls)
    return total


# 全局 trimmer：一次构造，多次复用
_trimmer = trim_messages(
    max_tokens=settings.max_history_tokens,
    strategy="last",              # 保留最新的
    token_counter=_token_counter,
    include_system=False,         # 系统提示单独注入，不参与裁剪
    allow_partial=False,          # 不拆散 tool_calls / ToolMessage 配对
    start_on="human",             # 裁剪后必须以人类消息开头
)


def trim_history(messages: list[BaseMessage]) -> list[BaseMessage]:
    """
    Layer 1：滑动窗口。

    保留最近 N 条消息（按 token 预算）。
    allow_partial=False 保证 tool_calls 和对应 ToolMessage 要么都留要么都丢。
    start_on="human" 避免 LLM 看到孤儿的 ToolMessage。
    """
    return _trimmer.invoke(messages)


def compact_tool_result(tool_name: str, result: dict) -> dict:
    """
    Layer 2：工具结果压缩。

    查询类工具（邮件、日程）返回可能很大，只保留关键字段。
    写操作结果很小，原样返回。

    原始 JSON 一旦被 LLM 总结成自然语言就没用了，
    压缩后能显著降低多轮对话的 token 占用。
    """
    if tool_name == "search_email":
        msgs = result.get("messages", [])
        return {
            "count": len(msgs),
            "messages": [
                {
                    "id": m.get("id"),
                    "from": m.get("from"),
                    "subject": m.get("subject"),
                    # snippet 截断，避免整封正文进入上下文
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
                {
                    "title": e.get("title"),
                    "start": e.get("start"),
                    "end": e.get("end"),
                }
                for e in events
            ],
        }

    # 其他工具结果较小，仅做长度保护
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) > settings.tool_result_max_chars:
        return {"truncated": raw[: settings.tool_result_max_chars] + "..."}
    return result


async def build_system_msg(user_id: int, timezone_str: str = "Asia/Shanghai") -> SystemMessage:
    """
    构造系统提示，注入用户偏好和时区。

    流程：
    1. 从 ORM 读该用户所有长期记忆
    2. 格式化成 "- key: value" 列表
    3. 拼进 SYSTEM_PROMPT_TEMPLATE

    这样即使换了会话（thread_id 变了），偏好也依然生效。
    """
    memories = await get_user_memories(user_id)
    if memories:
        memory_block = "\n".join(f"- {k}: {v}" for k, v in memories.items())
    else:
        memory_block = "（暂无）"

    return SystemMessage(
        content=SYSTEM_PROMPT_TEMPLATE.format(
            now=datetime.now(timezone.utc).isoformat(),
            timezone=timezone_str,
            memories=memory_block,
        )
    )


# 系统提示词模板：{memories} 注入在 build_system_msg 里完成
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


# 摘要提示词：把旧消息压缩成保留关键事实的摘要
SUMMARY_PROMPT = """把以下对话压缩成不超过 5 句的中文摘要。
必须保留：
- 用户明确表达的偏好或约束
- 已完成的工具调用及关键参数（如日程标题、时间、收件人）
- 未完成的任务和挂起操作

不要保留：
- 寒暄、感谢、重复确认
- 工具的原始 JSON 数据

只输出摘要正文，不要前后缀。"""