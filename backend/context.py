"""
上下文管理：三层策略。

Layer 1 - 滑动窗口：trim_messages 保留最近若干 token 的消息
Layer 2 - 工具结果压缩：ToolMessage 进上下文前按工具类型精简
Layer 3 - 旧消息摘要：消息条数超阈值时，交给 LLM 压缩旧消息
"""

import json

import tiktoken
from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, trim_messages,
)

from .config import settings
from .memory import get_user_memories
from .timeutil import DEFAULT_TZ, now_lines, time_hint

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
    # "没找到日程"而不是报错（第三方 API 查询失败被吞掉就是这个原因）。
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

    if tool_name == "list_schedules":
        items = result.get("schedules", [])
        return {
            "count": len(items),
            "from": result.get("from_local", ""),
            "schedules": [
                {
                    "schedule_id": s.get("schedule_id"),
                    "title": s.get("title"),
                    "start": s.get("start_at_local") or s.get("start_at"),
                    "end": s.get("end_at_local") or s.get("end_at"),
                    "location": s.get("location"),
                    "reminder_id": s.get("reminder_id"),
                }
                for s in items
            ],
        }

    if tool_name in ("create_schedule", "update_schedule", "cancel_schedule"):
        return result

    if tool_name == "list_reminders":
        # 提醒列表只保留可读字段，本地时间优先（remind_at_local）
        items = result.get("reminders", [])
        return {
            "count": len(items),
            "reminders": [
                {
                    "reminder_id": r.get("reminder_id"),
                    "text": r.get("text"),
                    "remind_at": r.get("remind_at_local") or r.get("remind_at"),
                    "status": r.get("status"),
                }
                for r in items
            ],
        }

    if tool_name in ("create_reminder", "cancel_reminder", "schedule_email"):
        return result

    if tool_name == "search_knowledge_base":
        # 片段正文在 executor 里已按 kb_snippet_max_chars 截断，
        # 这里只保留模型需要的字段，去掉分数等内部信息
        return {
            "count": result.get("count", 0),
            "hits": [
                {"source": h.get("source"), "text": h.get("text")}
                for h in result.get("hits", [])
            ],
            "note": result.get("note", ""),
        }

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
            now=now_lines(timezone_str),
            timezone=timezone_str,
            memories=memory_block,
            knowledge=KB_PROMPT_BLOCK if settings.kb_enabled else "",
        )
    )


def attach_time_hint(
    messages: list[BaseMessage], timezone_str: str = DEFAULT_TZ,
) -> list[BaseMessage]:
    """
    把"当前时间"贴在最后一条用户消息的正文前面（只改喂给模型的那一份）。

    为什么不能只靠系统提示：在历史会话里接着聊时，上下文里的时间全是对话当初的那些
    （"明天下午 3 点"、"2026-10-01 21:38"），模型很容易把对话开头那天当成"现在"，
    于是把相对时间算到错误的日期上。实测就出现过：10-11 在老会话里说"12 点"，
    被算成 10-02；问"今天几点"，答的还是 10-01。

    系统提示在上下文最前面，离用户最新的消息最远，一整段历史足以把它盖过去；
    贴在最后一条用户消息上，模型想不看见都难。

    只影响喂给模型的副本，不改状态、不改库里存的、也不改前端展示。
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, HumanMessage):
            continue
        # 多模态等非文本内容不动，避免把结构化内容拼坏
        if not isinstance(msg.content, str):
            return messages
        hinted = HumanMessage(
            content=f"{time_hint(timezone_str)}\n{msg.content}",
            id=getattr(msg, "id", None),
            name=getattr(msg, "name", None),
            additional_kwargs=dict(getattr(msg, "additional_kwargs", None) or {}),
        )
        return [*messages[:i], hinted, *messages[i + 1:]]
    return messages


KB_PROMPT_BLOCK = """
知识库（用户上传的文件）：
- 问题可能与用户上传的文件（产品文档、合同、笔记等）有关时，
  先调用 search_knowledge_base 检索，再基于查到的原文回答。
- 引用了文件内容时，标注来源文件名，写成【来源：文件名.pdf】。
- 检索没有命中时，如实说明"在你上传的文件里没有找到相关内容"，
  不要用通用知识冒充文件内容。
"""


SYSTEM_PROMPT_TEMPLATE = """你是一个个人事务助理，可以帮用户管理日程、邮件和提醒。
当前时间（判断“今天/明天”等相对时间只能以它为准，不要用对话历史里的时间）：
{now}
用户时区：{timezone}

已知用户长期偏好：
{memories}
{knowledge}

你的能力边界（不要提及其他日历、笔记或协作平台，那些都没有接入）：
- 日程建在系统自己的数据库里（create_schedule / list_schedules / update_schedule /
  cancel_schedule），不是外部日历
- 提醒和日程提醒都会通过用户绑定的 QQ 邮箱发出邮件，同时在应用内通知
- 邮件收发走用户绑定的 QQ 邮箱

规则：
1. 相对时间（“明天/今晚/下周一/过一会儿”）一概以上面的当前时间为基准换算成带时区偏移的
   ISO8601。对话历史里出现过的时间都是过去发生过的记录，不能当成“现在”。
   用户问“现在几点/今天几号”时，直接用上面的当前时间回答。
2. 时间参数要带正确的时区偏移（形如 2026-01-01T09:00:00+08:00）：偏移量取自上面的当前时间。
   不要把 UTC 的钟点原样贴上 +08:00 —— 那会整整差 8 小时，时间会落到过去。
3. 缺少必要参数时，直接追问，不要编造。
4. 需要查询信息时，先调用工具再回答。
5. 用户明确表达长期偏好时（如“以后默认会议 1 小时”），调用 remember_preference 工具。
6. 回复用简洁友好的中文。

执行类请求的处理方式（非常重要，违反会导致"看起来做了、其实没做"）：
7. 发邮件、建/改/取消日程、设提醒这类会产生真实副作用的操作，
   必须通过调用对应工具来完成；不调用工具，现实世界里什么都不会发生。
8. 不要自己写“即将…回复确认或取消”这类确认文案，也不要自己写“✅ 已发送 / 已创建 /
   已完成”这类结果文案：确认文案和执行结果都由系统在工具调用后生成并展示，
   你写得再像也不算执行过。
9. 在工具返回结果之前，绝对不要声称操作已经完成；没有结果就如实说明尚未执行。
10. 对话里带“[平台自动生成的内容…]”前缀的消息由系统产生，不是你写的，不要模仿其格式。
11. 用户要把某件事安排到将来时：占用时间段的安排用 create_schedule（可顺带挂邮件提醒），
    只需要到点提示一下就用 create_reminder。不确定会不会和已有安排撞车时，
    先 list_schedules 看一眼。
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
