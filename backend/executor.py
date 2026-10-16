"""
工具执行器：幂等 → 重试 → 审计。

特判分支：
- remember_preference：走异步 ORM
- 邮箱系列：走用户级 QQ 邮箱客户端
- 日程系列：落系统自己的库（backend/schedules.py），并顺带排提醒
- 提醒 / 定时邮件：落库排队，由后台调度器到点通过 QQ 邮箱投递
- 知识库检索：走向量 + 关键词混合检索

其余工具走 @tool handler（丢线程池）。
"""

import asyncio
import hashlib
import json
import random

from sqlalchemy import select

from .config import settings
from .db import session_scope
from .memory import set_user_memory
from .models import AuditLog, Idempotency
from .tools import TOOL_MAP, get_tool_meta


class RetryableError(Exception):
    """可重试错误（超时 / 429 / 5xx）。"""


class NonRetryableError(Exception):
    """不可重试错误（400 / 401 / 404）。"""


# 属于"代码或参数写错"的异常：重试不可能成功，直接判定为不可重试。
# 典型场景：SDK 升级后方法被移除（AttributeError）、工具参数缺字段（KeyError）、
# 参数校验不通过（ValueError）。
# 之前这类错误会被当成可重试错误白跑 3 次，并写下一串 retry 审计，掩盖真正原因。
_PROGRAMMING_ERRORS = (
    AttributeError, TypeError, KeyError, IndexError, NotImplementedError, ValueError,
)


def make_idempotency_key(
    tool_name: str, args: dict, session_id: str, user_id: int
) -> str:
    """
    生成幂等键。

    user_id 参与 hash 是为了多用户隔离：
    不同用户即使工具、参数、会话名完全相同，也不会命中同一缓存。
    """
    raw = json.dumps(
        {"u": user_id, "t": tool_name, "a": args, "s": session_id},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


async def _cache_get(key: str) -> dict | None:
    async with session_scope() as db:
        result = await db.execute(
            select(Idempotency).where(Idempotency.key == key)
        )
        row = result.scalar_one_or_none()
    return json.loads(row.result) if row else None


async def _cache_set(key: str, result: dict) -> None:
    async with session_scope() as db:
        existing = await db.execute(
            select(Idempotency).where(Idempotency.key == key)
        )
        if existing.scalar_one_or_none() is None:
            db.add(Idempotency(
                key=key, result=json.dumps(result, ensure_ascii=False),
            ))


async def _audit(
    user_id: int, session_id: str, tool_name: str,
    args: dict, result, status: str,
) -> None:
    """写审计日志。default=str 兜底 datetime 等不可 JSON 化对象。"""
    async with session_scope() as db:
        db.add(AuditLog(
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name,
            args=json.dumps(args, ensure_ascii=False),
            result=json.dumps(result, ensure_ascii=False, default=str),
            status=status,
        ))


async def execute_with_retry(
    session_id: str,
    tool_name: str,
    args: dict,
    user_id: int,
    idempotency_key: str | None = None,
    max_retries: int | None = None,
    dialogue: str = "",
) -> dict:
    """
    异步执行工具：幂等 → 重试 → 审计。

    三层保护：
    1. 幂等：先查缓存，命中直接返回
    2. 重试：只对可重试工具重试，指数退避 + 抖动
    3. 审计：无论成功失败都落一条日志

    dialogue：最近几轮对话的紧凑文本，只有知识库检索会用到
    （查询改写要消解多轮对话里的指代）。
    """
    meta = get_tool_meta(tool_name)
    max_retries = max_retries or settings.max_retries

    # ---------- 特判 1：记忆写入 ----------
    if tool_name == "remember_preference":
        try:
            await set_user_memory(user_id, args["key"], args["value"])
            result = {"key": args["key"], "value": args["value"], "status": "remembered"}
            await _audit(user_id, session_id, tool_name, args, result, "success")
            return result
        except Exception as e:  # noqa: BLE001
            await _audit(user_id, session_id, tool_name, args, {"error": str(e)}, "failed")
            raise

    # ---------- 特判 2：邮箱系列 ----------
    if tool_name in ("search_email", "send_email"):
        return await _execute_email_tool(
            session_id, tool_name, args, user_id,
            idempotency_key, max_retries, meta,
        )

    # ---------- 特判 3：日程系列 ----------
    if tool_name in (
        "create_schedule", "list_schedules", "update_schedule", "cancel_schedule",
    ):
        return await _execute_schedule_tool(
            session_id, tool_name, args, user_id,
            idempotency_key, max_retries, meta,
        )

    # ---------- 特判 4：提醒 / 定时任务系列（定时邮件也落在这张表） ----------
    if tool_name in (
        "create_reminder", "list_reminders", "cancel_reminder", "schedule_email",
    ):
        return await _execute_reminder_tool(
            session_id, tool_name, args, user_id,
            idempotency_key, max_retries, meta,
        )

    # ---------- 特判 5：知识库检索 ----------
    if tool_name == "search_knowledge_base":
        return await _execute_kb_tool(
            session_id, tool_name, args, user_id, dialogue,
        )

    # ---------- 通用工具路径 ----------
    tool = TOOL_MAP.get(tool_name)
    if tool is None:
        raise NonRetryableError(f"未知工具：{tool_name}")

    if idempotency_key:
        cached = await _cache_get(idempotency_key)
        if cached is not None:
            # 幂等命中：复用上次结果，同样要留痕并标记（见上面几个特判分支）
            flagged = {**cached, "deduplicated": True}
            await _audit(user_id, session_id, tool_name, args, flagged, "cached")
            return flagged

    last_err: Exception | None = None
    for i in range(max_retries):
        try:
            result = await asyncio.to_thread(tool.invoke, args)
            if idempotency_key:
                await _cache_set(idempotency_key, result)
            await _audit(user_id, session_id, tool_name, args, result, "success")
            return result
        except NonRetryableError as e:
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "failed")
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            will_retry = meta["retryable"] and i < max_retries - 1
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "retry" if will_retry else "failed")
            if not will_retry:
                raise
            await asyncio.sleep(2 ** i + random.random())

    raise last_err or RuntimeError("unknown error")


async def _execute_kb_tool(
    session_id: str,
    tool_name: str,
    args: dict,
    user_id: int,
    dialogue: str = "",
) -> dict:
    """
    知识库检索执行。

    刻意**不走幂等缓存**：检索是只读且廉价的，而幂等键包含 session_id，
    一旦缓存就会让"同一会话里重复问同一个问题"永远返回旧结果 ——
    用户刚补传了新文件、再问一次还是搜不到，只会以为入库失败。

    也不走重试循环：网络层重试已经在 Embedder 内部处理，
    检索本身是本地查询，失败基本是配置或数据问题，重试没有意义。
    """
    from .config import settings

    if not settings.kb_enabled:
        raise NonRetryableError("知识库功能未启用")

    query = (args.get("query") or "").strip()
    if not query:
        raise NonRetryableError("检索语句为空")

    try:
        top_k = int(args.get("top_k") or settings.kb_top_k)
    except (TypeError, ValueError):
        top_k = settings.kb_top_k
    top_k = max(1, min(top_k, 20))

    # 多轮对话里用户可能只说"那它呢"：先把指代消解成自包含语句。
    # 改写结果与原始 query **一起**检索，不替换原话 ——
    # 改写会引入幻觉，只留改写结果等于把幻觉请进结果集。
    extra_queries: list[str] = []
    if settings.kb_query_rewrite:
        from .kb.rewrite import rewrite_query

        rewritten = await rewrite_query(query, dialogue)
        if rewritten:
            extra_queries.append(rewritten)

    from .kb.search import search

    try:
        hits = await search(
            user_id, query, top_k=top_k, extra_queries=extra_queries,
        )
    except Exception as e:  # noqa: BLE001
        await _audit(user_id, session_id, tool_name, args, {"error": str(e)}, "failed")
        raise NonRetryableError(f"知识库检索失败：{e}") from e

    await _audit(
        user_id, session_id, tool_name, args,
        {"query": query, "rewritten": extra_queries, "count": len(hits)},
        "success",
    )

    return {
        "query": query,
        "count": len(hits),
        "hits": [
            {
                "source": hit["title"],
                "text": hit["text"][: settings.kb_snippet_max_chars],
            }
            for hit in hits
        ],
        "note": "" if hits else "知识库里没有找到相关内容，请如实告知用户，不要编造",
    }


async def _execute_schedule_tool(
    session_id: str, tool_name: str, args: dict, user_id: int,
    idempotency_key: str | None, max_retries: int, meta: dict,
) -> dict:
    """
    日程工具执行：落系统自己的库（backend/schedules.py）。

    日程是真实副作用（写库 + 排出一条到点发邮件的提醒），
    所以和邮箱一样走幂等 + 审计。
    """
    from .schedules import (
        ScheduleError, cancel_schedule, create_schedule, list_schedules,
        update_schedule,
    )

    if idempotency_key:
        cached = await _cache_get(idempotency_key)
        if cached is not None:
            # 幂等命中：这次没有真的落库，必须留痕并标记，
            # 否则前端会显示"已创建"，用户以为又建了一条
            flagged = {**cached, "deduplicated": True}
            await _audit(user_id, session_id, tool_name, args, flagged, "cached")
            return flagged

    last_err: Exception | None = None
    for i in range(max_retries):
        try:
            if tool_name == "create_schedule":
                result = await create_schedule(
                    user_id=user_id,
                    # 一律用 get + 默认值：缺参数时由 schedules 抛出可读的 ScheduleError，
                    # 而不是 KeyError 变成"工具内部错误"
                    title=args.get("title", ""),
                    start=args.get("start", ""),
                    end=args.get("end", ""),
                    location=args.get("location", ""),
                    description=args.get("description", ""),
                    remind_minutes=args.get("remind_minutes"),
                )
            elif tool_name == "list_schedules":
                result = await list_schedules(
                    user_id=user_id,
                    time_min=args.get("time_min", ""),
                    time_max=args.get("time_max", ""),
                    status=args.get("status", "active"),
                    limit=args.get("limit", 20),
                )
            elif tool_name == "update_schedule":
                result = await update_schedule(
                    user_id=user_id,
                    schedule_id=args.get("schedule_id", ""),
                    locate_title=args.get("locate_title", ""),
                    title=args.get("title", ""),
                    start=args.get("start", ""),
                    end=args.get("end", ""),
                    location=args.get("location", ""),
                    description=args.get("description", ""),
                    remind_minutes=args.get("remind_minutes"),
                )
            elif tool_name == "cancel_schedule":
                result = await cancel_schedule(
                    user_id=user_id,
                    schedule_id=args.get("schedule_id", ""),
                    locate_title=args.get("locate_title", ""),
                )
            else:
                raise NonRetryableError(f"未知日程工具：{tool_name}")

            if idempotency_key:
                await _cache_set(idempotency_key, result)
            await _audit(user_id, session_id, tool_name, args, result, "success")
            return result

        except ScheduleError as e:
            # 时间格式错 / 时间已过去 / 标题为空：参数问题，重试没用
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "failed")
            raise NonRetryableError(str(e))
        except _PROGRAMMING_ERRORS as e:
            # 代码/参数层面的错误：不重试，直接把原因暴露出来
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "failed")
            raise NonRetryableError(f"工具内部错误：{e}") from e
        except NonRetryableError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            will_retry = meta["retryable"] and i < max_retries - 1
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "retry" if will_retry else "failed")
            if not will_retry:
                raise
            await asyncio.sleep(2 ** i + random.random())

    raise last_err or RuntimeError("unknown error")


async def _execute_email_tool(
    session_id: str, tool_name: str, args: dict, user_id: int,
    idempotency_key: str | None, max_retries: int, meta: dict,
) -> dict:
    """邮箱工具执行：走用户级 QQ 邮箱客户端。"""
    from .email_client_wrapper import (
        EmailAuthError, EmailNotConfiguredError,
        search_email, send_email,
    )

    if idempotency_key:
        cached = await _cache_get(idempotency_key)
        if cached is not None:
            # 幂等命中：这次没有真的发信，必须留痕并标记，
            # 否则前端显示"已发送"，用户以为发出去了（实际是重复请求被跳过）
            flagged = {**cached, "deduplicated": True}
            await _audit(user_id, session_id, tool_name, args, flagged, "cached")
            return flagged

    last_err: Exception | None = None
    for i in range(max_retries):
        try:
            if tool_name == "search_email":
                result = await search_email(
                    user_id=user_id,
                    query=args.get("query", ""),
                    max_results=args.get("max_results", 5),
                )
            elif tool_name == "send_email":
                result = await send_email(
                    user_id=user_id,
                    to=args["to"],
                    subject=args["subject"],
                    body=args["body"],
                )
            else:
                raise NonRetryableError(f"未知邮箱工具：{tool_name}")

            if idempotency_key:
                await _cache_set(idempotency_key, result)
            await _audit(user_id, session_id, tool_name, args, result, "success")
            return result

        except (EmailNotConfiguredError, EmailAuthError) as e:
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "failed")
            raise NonRetryableError(str(e))
        except Exception as e:  # noqa: BLE001
            last_err = e
            will_retry = meta["retryable"] and i < max_retries - 1
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "retry" if will_retry else "failed")
            if not will_retry:
                raise
            await asyncio.sleep(2 ** i + random.random())

    raise last_err or RuntimeError("unknown error")


async def _execute_reminder_tool(
    session_id: str, tool_name: str, args: dict, user_id: int,
    idempotency_key: str | None, max_retries: int, meta: dict,
) -> dict:
    """
    提醒工具执行：落库 + 交给后台调度器投递。

    提醒是真实副作用（会往用户绑定的 QQ 邮箱发信），所以和邮箱一样走幂等 + 审计。
    """
    from .reminders import (
        ReminderError, cancel_reminder, create_reminder, create_scheduled_email,
        list_reminders,
    )

    if idempotency_key:
        cached = await _cache_get(idempotency_key)
        if cached is not None:
            # 幂等命中：这次没有真的落库，留痕 + 标记，避免"看起来又建了一条"
            flagged = {**cached, "deduplicated": True}
            await _audit(user_id, session_id, tool_name, args, flagged, "cached")
            return flagged

    last_err: Exception | None = None
    for i in range(max_retries):
        try:
            if tool_name == "create_reminder":
                result = await create_reminder(
                    user_id=user_id,
                    # 用 get + 默认值：缺参数时由 reminders 抛可读的 ReminderError，
                    # 而不是 KeyError 变成"工具内部错误"
                    text=args.get("text", ""),
                    remind_at=args.get("remind_at", ""),
                )
            elif tool_name == "list_reminders":
                result = await list_reminders(
                    user_id=user_id,
                    status=args.get("status", "pending"),
                    limit=args.get("limit", 10),
                )
            elif tool_name == "schedule_email":
                # 定时邮件：只落库排队，到点由调度器真正发出
                result = await create_scheduled_email(
                    user_id=user_id,
                    to=args.get("to", ""),
                    subject=args.get("subject", ""),
                    body=args.get("body", ""),
                    send_at=args.get("send_at", ""),
                )
            elif tool_name == "cancel_reminder":
                result = await cancel_reminder(
                    user_id=user_id,
                    reminder_id=args.get("reminder_id", ""),
                    text=args.get("text", ""),
                )
            else:
                raise NonRetryableError(f"未知提醒工具：{tool_name}")

            if idempotency_key:
                await _cache_set(idempotency_key, result)
            await _audit(user_id, session_id, tool_name, args, result, "success")
            return result

        except ReminderError as e:
            # 时间格式错 / 时间已过去 / 内容为空：参数问题，重试没用
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "failed")
            raise NonRetryableError(str(e))
        except _PROGRAMMING_ERRORS as e:
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "failed")
            raise NonRetryableError(f"工具内部错误：{e}") from e
        except NonRetryableError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            will_retry = meta["retryable"] and i < max_retries - 1
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "retry" if will_retry else "failed")
            if not will_retry:
                raise
            await asyncio.sleep(2 ** i + random.random())

    raise last_err or RuntimeError("unknown error")
