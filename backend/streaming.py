"""
SSE 流式输出：把一轮对话的生成过程逐 token 推给前端。

为什么单独一个模块：/api/chat 要等整张图跑完才返回，而一轮对话通常要
串行 2~3 次 LLM 调用（主回答 + 工具执行后的总结，长会话里还可能有一次
历史摘要）。用户在最慢的这段里看不到任何反馈，体感远差于实际耗时。

只"流"最终回答文本（两个节点）：
- llm：纯聊天轮次的直接回答
- summarize：工具执行后的总结

其余文本整条下发，不逐字流：
- 平台生成的确认 / 取消 / 执行结果文案（在流之后才产生）
- 被"防演戏"逻辑替换掉的文本（必须整体替换，逐字流出就成了幻觉）

正因如此，token 事件之后还会补一条 replace 做最终校准：前端按
"先追加、后对齐"处理即可，两条路径共用同一个气泡。

另外还有一类 step 事件，只带一句状态提示、不带正文：
一轮对话里最长的等待往往发生在没有任何 token 的时段（判定要调哪些工具、
工具的网络调用、KB 的查询改写与向量化），流式对这段无能为力。
step 事件专门把这段"看不见的等待"变成可见的状态行。
"""

import json
import logging
from typing import AsyncIterator

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

logger = logging.getLogger(__name__)

# 只有这两个节点产出的文本才是"给用户看的最终答案"。
# prepare_context 的历史摘要、execute_safe_tool 里知识库检索的查询改写
# 同样会触发 LLM 调用，但它们的 token 不能泄漏给前端，所以按节点名过滤。
_STREAM_NODES = {"llm", "summarize"}

# ---------- 进度提示 ----------
_START_STEP = "正在思考…"
_SUMMARIZE_STEP = "正在整理结果…"

_TOOL_STEPS = {
    "search_knowledge_base": "正在检索知识库…",
    "create_schedule": "正在创建日程…",
    "list_schedules": "正在查询日程…",
    "update_schedule": "正在修改日程…",
    "cancel_schedule": "正在取消日程…",
    "search_email": "正在检索邮件…",
    "send_email": "正在发送邮件…",
    "schedule_email": "正在安排定时邮件…",
    "create_reminder": "正在创建提醒…",
    "list_reminders": "正在查询提醒…",
    "cancel_reminder": "正在取消提醒…",
    "remember_preference": "正在记录偏好…",
}


async def stream_turn(
    graph,
    *,
    session_id: str,
    user_id: int,
    timezone: str,
    message: str,
) -> AsyncIterator[dict]:
    """执行一轮对话，产出 {"event": ..., "data": ...} 形式的事件。"""
    config = {"configurable": {"thread_id": session_id}}
    inputs = {
        "messages": [HumanMessage(content=message)],
        "session_id": session_id,
        "user_id": user_id,
        "timezone": timezone,
    }

    streamed = ""                 # 已经推给前端的文本
    final_state: dict | None = None

    # 立刻给一条进度提示：后面的 llm 判定/工具调用期间可能长时间没有 token，
    # 前端先亮起状态行，用户不会对着空白界面干等
    yield {"event": "step", "data": {"text": _START_STEP}}

    # values 模式每步给出完整状态，最后一条即终态；messages 模式给出 token 分片；
    # updates 模式在节点完成时给出该节点的 state 增量，用来推"正在做什么"
    async for mode, payload in graph.astream(
        inputs, config, stream_mode=["messages", "updates", "values"],
    ):
        if mode == "messages":
            chunk, metadata = payload
            if (metadata or {}).get("langgraph_node") not in _STREAM_NODES:
                continue
            text = _chunk_text(chunk)
            if not text:
                continue
            streamed += text
            yield {"event": "token", "data": {"text": text}}
        elif mode == "updates":
            step = _step_from_update(payload)
            if step:
                yield {"event": "step", "data": {"text": step}}
        elif mode == "values":
            final_state = payload

    final_state = final_state or {}
    stage = final_state.get("stage") or "IDLE"
    status = "WAITING_CONFIRMATION" if stage == "WAITING_CONFIRMATION" else "DONE"
    final_reply = _last_ai_text(final_state.get("messages", []))

    # 只在和已推送内容不一致时才补 replace：
    # 覆盖平台生成的整条文案（streamed 为空）与"防演戏"的整条替换两种情况
    if final_reply != streamed:
        yield {"event": "replace", "data": {"text": final_reply}}

    yield {
        "event": "done",
        "data": {
            "session_id": session_id,
            "status": status,
            "pending_action": final_state.get("pending_action"),
        },
    }


def _step_from_update(payload) -> str | None:
    """
    把节点完成时的 state 增量翻译成一句进度提示。

    只关心两个节点：
    - llm：拿到工具调用清单，就能告诉用户"正在查日程 / 正在发邮件"
    - execute_safe_tool：工具跑完了，接下来 summarize 会在 prefill 上耗一段，
      先提示"正在整理结果"
    """
    if not isinstance(payload, dict):
        return None

    for node, update in payload.items():
        if node == "llm":
            names = _tool_names(update)
            if names:
                return _tool_step(names)
        elif node == "execute_safe_tool":
            return _SUMMARIZE_STEP
    return None


def _tool_names(update) -> list[str]:
    """从节点返回的 state 增量里取出本轮调用的工具名。"""
    names: list[str] = []
    messages = (update or {}).get("messages") or []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else None
            if name:
                names.append(str(name))
    return names


def _tool_step(names: list[str]) -> str:
    """工具名 → 用户能看懂的状态行；一轮多个动作时只说总数。"""
    unique = list(dict.fromkeys(names))
    if len(unique) == 1:
        return _TOOL_STEPS.get(unique[0], f"正在执行 {unique[0]}…")
    return f"正在执行 {len(unique)} 个操作…"


def _chunk_text(chunk) -> str:
    """从流式分片里取出文本。非文本内容（工具调用参数等）返回空串。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _last_ai_text(messages: list[BaseMessage]) -> str:
    """取最后一条有内容的助手消息（与 /api/chat 的取法保持一致）。"""
    for m in reversed(messages):
        if not isinstance(m, AIMessage):
            continue
        content = m.content if isinstance(m.content, str) else ""
        if content:
            return content
    return ""


def sse_format(event: str, data: dict) -> str:
    """把事件编码成一帧 SSE。json.dumps 会转义换行，不会破坏帧结构。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
