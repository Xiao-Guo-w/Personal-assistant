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
        # 页面模式：正文可能很长，确认文案里只给预览
        content = (args.get("content") or "").strip()
        preview = f"{content[:50]}…" if len(content) > 50 else (content or "（无正文）")
        return (
            "即将在 Notion 中创建子页面：\n"
            f"- 标题：{args.get('title')}\n"
            f"- 父页面：{args.get('parent_page_id') or '（默认父页面）'}\n"
            f"- 正文：{preview}\n"
            "回复“确认”或“取消”。"
        )
    if tool_name == "update_notion_page":
        return (
            "即将更新 Notion 页面：\n"
            f"- 页面 ID：{args.get('page_id')}\n"
            f"- 新标题：{args.get('title') or '（不改）'}\n"
            f"- 追加正文：{(args.get('append_content') or '（无）')[:50]}\n"
            "回复“确认”或“取消”。"
        )
    if tool_name == "archive_notion_page":
        return (
            "即将归档 Notion 页面：\n"
            f"- 页面 ID：{args.get('page_id')}\n"
            "归档后页面进入回收站，回复“确认”或“取消”。"
        )
    return f"即将执行 {tool_name}，参数：{args}。回复“确认”或“取消”。"


def _format_confirmation_batch(actions: list[dict]) -> str:
    """
    把待确认动作渲染成人话。

    单个动作沿用原来的文案；多个动作编号列出，一次"确认"全部执行，
    避免 LLM 一轮给出多个操作时后面的被静默丢弃。
    """
    if len(actions) == 1:
        return _format_confirmation(actions[0]["tool"], actions[0]["args"])

    parts = [f"本轮有 {len(actions)} 个操作待确认，回复“确认”将按顺序全部执行："]
    for i, action in enumerate(actions, 1):
        detail = _format_confirmation(action["tool"], action["args"])
        detail = detail.replace("\n回复“确认”或“取消”。", "")
        parts.append(f"\n【{i}】{detail}")
    parts.append("\n回复“确认”或“取消”。")
    return "\n".join(parts)


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
    if tool_name == "list_notion_pages":
        return f"✅ 找到 {result.get('count', 0)} 个子页面"
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
    """
    LLM 之后路由：按工具风险分流。

    只要本轮出现任意一个危险工具，就整批交给确认节点处理
    （旧的只判断 tool_calls[0]，一次多个调用时会漏掉后面的动作）。
    """
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return "end"
    need_confirm = any(
        get_tool_meta(tc["name"])["risk"] == "confirm"
        for tc in last.tool_calls
    )
    return "confirm" if need_confirm else "execute"


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
    """
    危险操作：挂起等待确认（本轮所有工具调用都在这里分流）。

    LLM 一轮里可能同时给出多个工具调用（例如一次要求建 3 个 Notion 页面）。
    旧实现只看 tool_calls[0]：后面的调用既没执行也没回消息，会被静默丢弃；
    而且留在状态里的 tool_call 没有对应的 ToolMessage，下一轮模型请求
    会因"工具调用没有回执"被判为非法（400）。现在的做法是：

    - 安全工具：当场执行，立刻补上 ToolMessage
    - 危险工具：排成队列，用户确认一次后按顺序全部执行
    - 每个 tool_call 都能拿到一条回执，状态里不再出现孤儿调用
    """
    last_ai: AIMessage = state["messages"][-1]

    safe_msgs: list[ToolMessage] = []
    queue: list[dict] = []
    queued_ids: list[str] = []

    for tc in last_ai.tool_calls:
        if get_tool_meta(tc["name"])["risk"] != "confirm":
            # 安全工具不需要确认：直接执行并把结果回填给模型
            try:
                result = await execute_with_retry(
                    state["session_id"], tc["name"], tc["args"],
                    user_id=state["user_id"],
                )
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}
            safe_msgs.append(ToolMessage(
                content=json.dumps(
                    compact_tool_result(tc["name"], result), ensure_ascii=False,
                ),
                tool_call_id=tc["id"],
            ))
            continue

        queue.append({
            "tool": tc["name"],
            "args": tc["args"],
            "idempotency_key": make_idempotency_key(
                tc["name"], tc["args"], state["session_id"], state["user_id"],
            ),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        queued_ids.append(tc["id"])

    if not queue:
        # 兜底：理论上路由已经保证至少有危险工具，避免空队列把会话卡在确认态
        return {"messages": safe_msgs}

    # 排队中的动作先回一条"待确认"回执（tool_call 必须有回应），
    # 真正执行在 handle_confirmation_node，结果以新的 AIMessage 返回。
    pending_msgs = [
        ToolMessage(
            content=json.dumps(
                {
                    "status": "pending_confirmation",
                    "tool": action["tool"],
                    "note": "已挂起，等待用户确认后执行。",
                },
                ensure_ascii=False,
            ),
            tool_call_id=call_id,
        )
        for action, call_id in zip(queue, queued_ids)
    ]

    return {
        "stage": "WAITING_CONFIRMATION",
        "pending_action": queue[0],    # 兼容旧接口/前端：队列首项
        "pending_actions": queue,      # 真正的执行队列
        "messages": [
            *safe_msgs,
            *pending_msgs,
            AIMessage(content=_format_confirmation_batch(queue)),
        ],
    }


async def execute_safe_tool_node(state: AgentState) -> dict:
    """安全操作：逐个执行本轮所有工具调用 + 压缩结果。"""
    last_ai: AIMessage = state["messages"][-1]

    tool_msgs: list[ToolMessage] = []
    for tc in last_ai.tool_calls:
        try:
            result = await execute_with_retry(
                state["session_id"], tc["name"], tc["args"],
                user_id=state["user_id"],
            )
        except Exception as e:  # noqa: BLE001
            result = {"error": str(e)}

        tool_msgs.append(ToolMessage(
            content=json.dumps(
                compact_tool_result(tc["name"], result), ensure_ascii=False,
            ),
            tool_call_id=tc["id"],
        ))
    return {"messages": tool_msgs}


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
    """处理确认阶段输入：确认后按顺序执行队列里的全部动作。"""
    queue = _pending_queue(state)

    last_human = None
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage):
            last_human = m
            break
    text = (last_human.content if last_human else "").strip().lower()

    if not queue:
        return {"stage": "IDLE",
                "messages": [AIMessage(content="当前没有待确认的操作。")]}

    if text in CANCEL_WORDS:
        return {"stage": "IDLE", "pending_action": None, "pending_actions": [],
                "messages": [AIMessage(content="已取消该操作。")]}

    if text not in CONFIRM_WORDS:
        return {"messages": [AIMessage(content="请回复“确认”或“取消”。")]}

    # 过期判断以队列里最早的动作时间为准（队列是一次生成、一次确认）
    created = datetime.fromisoformat(queue[0]["created_at"])
    age = (datetime.now(timezone.utc) - created).total_seconds()
    if age > settings.confirm_ttl_seconds:
        return {"stage": "IDLE", "pending_action": None, "pending_actions": [],
                "messages": [AIMessage(content="操作已过期，请重新发起。")]}

    replies: list[str] = []
    for action in queue:
        try:
            result = await execute_with_retry(
                state["session_id"], action["tool"], action["args"],
                user_id=state["user_id"],
                idempotency_key=action["idempotency_key"],
            )
            replies.append(_format_result(action["tool"], result))
        except Exception as e:  # noqa: BLE001
            # 单个动作失败不影响后面的：逐个报告，用户能看清哪一步没成
            replies.append(f"❌ {action['tool']} 执行失败：{e}")

    return {"stage": "IDLE", "pending_action": None, "pending_actions": [],
            "messages": [AIMessage(content="\n".join(replies))]}


def _pending_queue(state: AgentState) -> list[dict]:
    """
    取出待确认动作队列。

    兼容旧会话的检查点：那边只存了单数的 pending_action。
    """
    queue = state.get("pending_actions") or []
    if queue:
        return list(queue)
    single = state.get("pending_action")
    return [single] if single else []
