"""
图的所有节点 + 路由函数。

设计原则：
- 每个节点职责单一，返回 state update（dict），不直接修改 state
- 路由函数纯判断，不做副作用
- 节点全部 async，与异步 ORM / 异步 LLM 对齐
"""

import json
from datetime import datetime, timezone

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from .config import settings
from .context import (
    SUMMARY_PROMPT,
    build_system_msg,
    compact_tool_result,
    trim_history,
)
from .executor import execute_with_retry, make_idempotency_key
from .llm import get_llm
from .state import AgentState
from .tools import ALL_TOOLS, get_tool_meta


# 绑定工具的 LLM：一次性绑定，后续 ainvoke 复用
_llm = get_llm()
_llm_with_tools = _llm.bind_tools(ALL_TOOLS)


# 用户确认 / 取消的常见表达，集中管理便于扩展
CONFIRM_WORDS = {"确认", "确定", "是", "好", "ok", "yes", "y"}
CANCEL_WORDS = {"取消", "算了", "不用", "no", "n", "cancel"}


def _format_confirmation(tool_name: str, args: dict) -> str:
    """
    把待执行的操作翻译成人话，让用户看得懂再确认。

    危险操作的确认文案要清晰展示参数，
    而不是给一句模糊的「确认执行吗？」。
    """
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
    if tool_name == "create_notion_page":
        return (
            "即将创建 Notion 页面：\n"
            f"- 标题：{args.get('title')}\n"
            f"- 数据库：{args.get('database_id')}\n"
            "回复“确认”或“取消”。"
        )
    if tool_name == "create_reminder":
        return (
            "即将创建提醒：\n"
            f"- 内容：{args.get('text')}\n"
            f"- 时间：{args.get('remind_at')}\n"
            "回复“确认”或“取消”。"
        )
    # 兜底：未知工具也把参数展示出来
    return f"即将执行 {tool_name}，参数：{args}。回复“确认”或“取消”。"


def _format_result(tool_name: str, result: dict) -> str:
    """工具返回的 dict → 用户友好的执行成功文案。"""
    if tool_name == "create_calendar_event":
        return f"✅ 日程已创建：{result.get('title')}（{result.get('start')}）"
    if tool_name == "send_email":
        return f"✅ 邮件已发送，message_id={result.get('message_id')}"
    if tool_name == "create_notion_page":
        return f"✅ Notion 页面已创建：{result.get('url')}"
    if tool_name == "create_reminder":
        return f"✅ 提醒已设置：{result.get('reminder_id')}"
    if tool_name == "remember_preference":
        return f"✅ 已记住偏好：{result.get('key')} = {result.get('value')}"
    return f"✅ 操作完成：{result}"


# ============================================================
# 路由函数（同步，纯判断）
# ============================================================

def route_entry(state: AgentState) -> str:
    """
    START 处的路由。

    - WAITING_CONFIRMATION → 走确认处理节点
    - 其他 → 走 prepare_context（可能触发摘要）再进 LLM
    """
    if state.get("stage") == "WAITING_CONFIRMATION":
        return "handle_confirmation"
    return "prepare_context"


def route_after_llm(state: AgentState) -> str:
    """
    LLM 之后的路由。

    - 没有 tool_calls → 结束
    - 有 tool_calls 且风险 confirm → 进入确认准备
    - 有 tool_calls 且风险 safe → 直接执行
    """
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return "end"

    tool_name = last.tool_calls[0]["name"]
    meta = get_tool_meta(tool_name)
    return "confirm" if meta["risk"] == "confirm" else "execute"


# ============================================================
# 节点函数（异步）
# ============================================================

async def prepare_context_node(state: AgentState) -> dict:
    """
    Layer 3：旧消息摘要。

    当消息条数超过阈值时：
    1. 取出最老的 N 条
    2. 让 LLM 压缩成一段摘要
    3. 删除旧消息，插入一条 "[历史摘要] ..."

    摘要后消息条数骤降，后续 trim 和 LLM 调用都会更轻量。
    消息数不到阈值时直接返回空，不产生任何副作用。
    """
    msgs = state["messages"]
    if len(msgs) < settings.summarize_threshold:
        return {}   # 无操作

    keep = settings.summarize_keep_recent
    old = msgs[:-keep]
    if not old:
        return {}

    # 让 LLM 摘要旧消息（不带工具，纯文本压缩）
    summary_msg = await _llm.ainvoke(
        [SystemMessage(content=SUMMARY_PROMPT), *old]
    )
    summary_text = summary_msg.content or "（摘要生成失败）"

    # RemoveMessage 删旧消息 + 插入一条摘要 AIMessage
    # add_messages reducer 会先执行删除，再追加新消息
    return {
        "messages": [
            *[RemoveMessage(id=m.id) for m in old if getattr(m, "id", None)],
            AIMessage(content=f"[历史摘要] {summary_text}"),
        ]
    }


async def llm_node(state: AgentState) -> dict:
    """
    调用绑定工具的 LLM。

    上下文处理：
    - Layer 1：trim_history 保留最近若干 token
    - 长期记忆：build_system_msg 注入用户偏好和时区
    """
    system_msg = await build_system_msg(
        state["user_id"],
        state.get("timezone", "Asia/Shanghai"),
    )
    trimmed = trim_history(state["messages"])
    msgs = [system_msg] + trimmed

    ai_msg: AIMessage = await _llm_with_tools.ainvoke(msgs)
    return {"messages": [ai_msg]}


async def prepare_confirmation_node(state: AgentState) -> dict:
    """
    危险操作：不执行，改成把操作挂起，返回确认文案。

    用 RemoveMessage 移除上一条带 tool_calls 的 AIMessage，
    保持对话历史干净（否则下次 LLM 会看到未完成的工具调用而困惑）。
    """
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
            # 记录创建时间，用于 TTL 过期判断
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "messages": [
            RemoveMessage(id=last_ai.id),
            AIMessage(content=confirm_text),
        ],
    }


async def execute_safe_tool_node(state: AgentState) -> dict:
    """
    安全操作：执行工具，并用压缩后的结果构造 ToolMessage。

    Layer 2 关键点：结果先进 compact_tool_result 精简，
    再作为 ToolMessage.content 存入上下文，避免大 JSON 占用上下文。
    """
    last_ai: AIMessage = state["messages"][-1]
    tc = last_ai.tool_calls[0]

    try:
        result = await execute_with_retry(
            state["session_id"],
            tc["name"],
            tc["args"],
            user_id=state["user_id"],
        )
    except Exception as e:  # noqa: BLE001
        # 执行失败也以 ToolMessage 回填，让 LLM 用自然语言总结
        result = {"error": str(e)}

    # 压缩工具结果
    compact = compact_tool_result(tc["name"], result)

    tool_msg = ToolMessage(
        content=json.dumps(compact, ensure_ascii=False),
        tool_call_id=tc["id"],
    )
    return {"messages": [tool_msg]}


async def summarize_node(state: AgentState) -> dict:
    """
    工具执行后再次调用 LLM，把工具结果组织成自然语言。
    这里用不带工具的 LLM，避免模型再次发起工具调用。
    """
    system_msg = await build_system_msg(
        state["user_id"], state.get("timezone", "Asia/Shanghai"),
    )
    trimmed = trim_history(state["messages"])
    msgs = [system_msg] + trimmed

    ai_msg: AIMessage = await _llm.ainvoke(msgs)
    return {"messages": [ai_msg]}


async def handle_confirmation_node(state: AgentState) -> dict:
    """
    处理 WAITING_CONFIRMATION 阶段的用户输入。

    三种情况：
    1. 取消 → 清空 pending_action
    2. 确认 → TTL 检查 + 执行
    3. 其他 → 追问，状态保持 WAITING_CONFIRMATION
    """
    pending = state.get("pending_action")

    # 找最近的用户消息
    last_human = None
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            last_human = m
            break
    text = (last_human.content if last_human else "").strip().lower()

    # 异常状态：没有 pending
    if not pending:
        return {
            "stage": "IDLE",
            "messages": [AIMessage(content="当前没有待确认的操作。")],
        }

    # 取消
    if text in CANCEL_WORDS:
        return {
            "stage": "IDLE",
            "pending_action": None,
            "messages": [AIMessage(content="已取消该操作。")],
        }

    # 非法输入：追问，状态不变
    if text not in CONFIRM_WORDS:
        return {"messages": [AIMessage(content="请回复“确认”或“取消”。")]}

    # TTL 检查
    created = datetime.fromisoformat(pending["created_at"])
    age = (datetime.now(timezone.utc) - created).total_seconds()
    if age > settings.confirm_ttl_seconds:
        return {
            "stage": "IDLE",
            "pending_action": None,
            "messages": [AIMessage(content="操作已过期，请重新发起。")],
        }

    # 执行。带幂等键：即使因重试或用户连点两次也不会重复执行
    try:
        result = await execute_with_retry(
            state["session_id"],
            pending["tool"],
            pending["args"],
            user_id=state["user_id"],
            idempotency_key=pending["idempotency_key"],
        )
        reply = _format_result(pending["tool"], result)
    except Exception as e:  # noqa: BLE001
        reply = f"执行失败：{e}"

    # 无论成功失败都回到 IDLE
    return {
        "stage": "IDLE",
        "pending_action": None,
        "messages": [AIMessage(content=reply)],
    }