"""
图的所有节点 + 路由函数。

设计原则：
- 每个节点职责单一，返回 state update（dict）
- 路由函数纯判断，不做副作用
- 节点全部 async
"""

import json
from datetime import datetime, timezone

from langchain_core.messages import (
    AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage,
)

from .config import settings
from .context import (
    SUMMARY_PROMPT, build_system_msg, compact_tool_result, trim_history,
)
from .executor import execute_with_retry, make_idempotency_key
from .llm import get_llm
from .state import AgentState
from .tools import ALL_TOOLS, get_tool_meta


_llm = get_llm()
_llm_with_tools = _llm.bind_tools(ALL_TOOLS)


CONFIRM_WORDS = {"确认", "确定", "是", "好", "ok", "yes", "y"}
CANCEL_WORDS = {"取消", "算了", "不用", "no", "n", "cancel"}


def _format_confirmation(tool_name: str, args: dict) -> str:
    """把待执行操作翻译成人话。"""
    if tool_name == "create_calendar_event":
        return (
            "即将创建日程：\n"
            f"- 标题：{args.get('title')}\n"
            f"- 开始：{args.get('start')}\n"
            f"- 结束：{args.get('end')}\n"
            f"- 参与人：{', '.join(args.get('attendees') or []) or '无'}\n"
            "回复“确认”或“取消”。"
        )
    if tool_name == "send_email":
        return (
            "即将发送邮件：\n"
            f"- 收件人：{args.get('to')}\n"
            f"- 主题：{args.get('subject')}\n"
            f"- 正文：{args.get('body')}\n"
            "回复“确认”或“取消”。"
        )
    if tool_name == "create_reminder":
        return (
            "即将创建提醒：\n"
            f"- 内容：{args.get('text')}\n"
            f"- 时间：{args.get('remind_at')}\n"
            "回复“确认”或“取消”。"
        )
    if tool_name == "create_notion_page":
        return (
            "即将在 Notion 中创建页面：\n"
            f"- 标题：{args.get('title')}\n"
            f"- 数据库：{args.get('database_id') or '（默认数据库）'}\n"
            "回复“确认”或“取消”。"
        )
    if tool_name == "update_notion_page":
        return (
            "即将更新 Notion 页面：\n"
            f"- 页面 ID：{args.get('page_id')}\n"
            f"- 更新属性：{args.get('properties')}\n"
            "回复“确认”或“取消”。"
        )
    if tool_name == "archive_notion_page":
        return (
            "即将归档 Notion 页面：\n"
            f"- 页面 ID：{args.get('page_id')}\n"
            "归档后页面进入回收站，回复“确认”或“取消”。"
        )
    return f"即将执行 {tool_name}，参数：{args}。回复“确认”或“取消”。"


def _format_result(tool_name: str, result: dict) -> str:
    """工具结果 → 用户友好文案。"""
    if tool_name == "create_calendar_event":
        return f"✅ 日程已创建：{result.get('title')}（{result.get('start')}）"
    if tool_name == "list_calendar_events":
        return f"✅ 查询到 {len(result.get('events', []))} 个日程"
    if tool_name == "send_email":
        return f"✅ 邮件已发送至 {result.get('to')}"
    if tool_name == "search_email":
        return f"✅ 找到 {len(result.get('messages', []))} 封相关邮件"
    if tool_name == "create_reminder":
        return f"✅ 提醒已设置：{result.get('reminder_id')}"
    if tool_name == "create_notion_page":
        return f"✅ Notion 页面已创建：{result.get('title')}（{result.get('url')}）"
    if tool_name == "query_notion_database":
        return f"✅ 查询到 {result.get('count', 0)} 条记录"
    if tool_name == "update_notion_page":
        return f"✅ Notion 页面已更新：{result.get('page_id')}"
    if tool_name == "archive_notion_page":
        return f"✅ Notion 页面已归档：{result.get('page_id')}"
    if tool_name == "remember_preference":
        return f"✅ 已记住偏好：{result.get('key')} = {result.get('value')}"
    return f"✅ 操作完成：{result}"


# ============================================================
# 路由函数
# ============================================================

def route_entry(state: AgentState) -> str:
    """START 处路由。"""
    if state.get("stage") == "WAITING_CONFIRMATION":
        return "handle_confirmation"
    return "prepare_context"


def route_after_llm(state: AgentState) -> str:
    """LLM 之后路由：按工具风险分流。"""
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return "end"
    meta = get_tool_meta(last.tool_calls[0]["name"])
    return "confirm" if meta["risk"] == "confirm" else "execute"


# ============================================================
# 节点函数
# ============================================================

async def prepare_context_node(state: AgentState) -> dict:
    """Layer 3：旧消息摘要。"""
    msgs = state["messages"]
    if len(msgs) < settings.summarize_threshold:
        return {}

    keep = settings.summarize_keep_recent
    old = msgs[:-keep]
    if not old:
        return {}

    summary_msg = await _llm.ainvoke(
        [SystemMessage(content=SUMMARY_PROMPT), *old]
    )
    summary_text = summary_msg.content or "（摘要生成失败）"

    return {
        "messages": [
            *[RemoveMessage(id=m.id) for m in old if getattr(m, "id", None)],
            AIMessage(content=f"[历史摘要] {summary_text}"),
        ]
    }


async def llm_node(state: AgentState) -> dict:
    """调用绑定工具的 LLM。"""
    system_msg = await build_system_msg(
        state["user_id"], state.get("timezone", "Asia/Shanghai"),
    )
    trimmed = trim_history(state["messages"])
    msgs = [system_msg] + trimmed

    ai_msg: AIMessage = await _llm_with_tools.ainvoke(msgs)
    return {"messages": [ai_msg]}


async def prepare_confirmation_node(state: AgentState) -> dict:
    """危险操作：挂起，返回确认文案。"""
    last_ai: AIMessage = state["messages"][-1]
    tc = last_ai.tool_calls[0]
    tool_name = tc["name"]
    args = tc["args"]

    key = make_idempotency_key(
        tool_name, args, state["session_id"], state["user_id"]
    )
    confirm_text = _format_confirmation(tool_name, args)

    return {
        "stage": "WAITING_CONFIRMATION",
        "pending_action": {
            "tool": tool_name,
            "args": args,
            "idempotency_key": key,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "messages": [
            RemoveMessage(id=last_ai.id),
            AIMessage(content=confirm_text),
        ],
    }


async def execute_safe_tool_node(state: AgentState) -> dict:
    """安全操作：执行工具 + 压缩结果。"""
    last_ai: AIMessage = state["messages"][-1]
    tc = last_ai.tool_calls[0]

    try:
        result = await execute_with_retry(
            state["session_id"], tc["name"], tc["args"],
            user_id=state["user_id"],
        )
    except Exception as e:  # noqa: BLE001
        result = {"error": str(e)}

    compact = compact_tool_result(tc["name"], result)
    tool_msg = ToolMessage(
        content=json.dumps(compact, ensure_ascii=False),
        tool_call_id=tc["id"],
    )
    return {"messages": [tool_msg]}


async def summarize_node(state: AgentState) -> dict:
    """工具执行后调 LLM 生成总结。"""
    system_msg = await build_system_msg(
        state["user_id"], state.get("timezone", "Asia/Shanghai"),
    )
    trimmed = trim_history(state["messages"])
    msgs = [system_msg] + trimmed

    ai_msg: AIMessage = await _llm.ainvoke(msgs)
    return {"messages": [ai_msg]}


async def handle_confirmation_node(state: AgentState) -> dict:
    """处理确认阶段输入。"""
    pending = state.get("pending_action")

    last_human = None
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            last_human = m
            break
    text = (last_human.content if last_human else "").strip().lower()

    if not pending:
        return {"stage": "IDLE",
                "messages": [AIMessage(content="当前没有待确认的操作。")]}

    if text in CANCEL_WORDS:
        return {"stage": "IDLE", "pending_action": None,
                "messages": [AIMessage(content="已取消该操作。")]}

    if text not in CONFIRM_WORDS:
        return {"messages": [AIMessage(content="请回复“确认”或“取消”。")]}

    created = datetime.fromisoformat(pending["created_at"])
    age = (datetime.now(timezone.utc) - created).total_seconds()
    if age > settings.confirm_ttl_seconds:
        return {"stage": "IDLE", "pending_action": None,
                "messages": [AIMessage(content="操作已过期，请重新发起。")]}

    try:
        result = await execute_with_retry(
            state["session_id"], pending["tool"], pending["args"],
            user_id=state["user_id"],
            idempotency_key=pending["idempotency_key"],
        )
        reply = _format_result(pending["tool"], result)
    except Exception as e:  # noqa: BLE001
        reply = f"执行失败：{e}"

    return {"stage": "IDLE", "pending_action": None,
            "messages": [AIMessage(content=reply)]}