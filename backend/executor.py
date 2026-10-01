"""
工具执行器：幂等 → 重试 → 审计。

特判分支：
- remember_preference：走异步 ORM
- Notion 系列：走用户级 OAuth 客户端
- 邮箱系列：走用户级 QQ 邮箱客户端
- 日历系列：走用户级飞书日历客户端

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
# 典型场景：SDK 升级后方法被移除（AttributeError，Notion databases.query 就是这样）、
# 工具参数缺字段（KeyError）、参数校验不通过（ValueError）。
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
) -> dict:
    """
    异步执行工具：幂等 → 重试 → 审计。

    三层保护：
    1. 幂等：先查缓存，命中直接返回
    2. 重试：只对可重试工具重试，指数退避 + 抖动
    3. 审计：无论成功失败都落一条日志
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

    # ---------- 特判 2：Notion 系列 ----------
    if tool_name in (
        "create_notion_page", "list_notion_pages",
        "update_notion_page", "archive_notion_page",
    ):
        return await _execute_notion_tool(
            session_id, tool_name, args, user_id,
            idempotency_key, max_retries, meta,
        )

    # ---------- 特判 3：邮箱系列 ----------
    if tool_name in ("search_email", "send_email"):
        return await _execute_email_tool(
            session_id, tool_name, args, user_id,
            idempotency_key, max_retries, meta,
        )

    # ---------- 特判 4：日历系列 ----------
    if tool_name in ("list_calendar_events", "create_calendar_event"):
        return await _execute_calendar_tool(
            session_id, tool_name, args, user_id,
            idempotency_key, max_retries, meta,
        )

    # ---------- 通用工具路径 ----------
    tool = TOOL_MAP.get(tool_name)
    if tool is None:
        raise NonRetryableError(f"未知工具：{tool_name}")

    if idempotency_key:
        cached = await _cache_get(idempotency_key)
        if cached is not None:
            return cached

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


async def _execute_notion_tool(
    session_id: str, tool_name: str, args: dict, user_id: int,
    idempotency_key: str | None, max_retries: int, meta: dict,
) -> dict:
    """Notion 工具执行：走用户级 OAuth 客户端（页面模式）。"""
    from .notion_client_wrapper import (
        NotionAuthError, NotionNotConfiguredError,
        archive_page, create_page_under_parent, list_child_pages, update_page,
    )

    if idempotency_key:
        cached = await _cache_get(idempotency_key)
        if cached is not None:
            return cached

    last_err: Exception | None = None
    for i in range(max_retries):
        try:
            if tool_name == "create_notion_page":
                result = await create_page_under_parent(
                    user_id=user_id,
                    parent_page_id=args.get("parent_page_id", ""),
                    title=args["title"],
                    content=args.get("content", ""),
                )
            elif tool_name == "list_notion_pages":
                result = await list_child_pages(
                    user_id=user_id,
                    parent_page_id=args.get("parent_page_id", ""),
                    page_size=args.get("page_size", 10),
                )
            elif tool_name == "update_notion_page":
                result = await update_page(
                    user_id=user_id,
                    page_id=args["page_id"],
                    title=args.get("title", ""),
                    append_content=args.get("append_content", ""),
                )
            elif tool_name == "archive_notion_page":
                result = await archive_page(user_id=user_id, page_id=args["page_id"])
            else:
                raise NonRetryableError(f"未知 Notion 工具：{tool_name}")

            if idempotency_key:
                await _cache_set(idempotency_key, result)
            await _audit(user_id, session_id, tool_name, args, result, "success")
            return result

        except (NotionNotConfiguredError, NotionAuthError) as e:
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "failed")
            raise NonRetryableError(str(e))
        except _PROGRAMMING_ERRORS as e:
            # 代码/参数层面的错误：不重试，直接把原因暴露出来
            await _audit(user_id, session_id, tool_name, args,
                         {"error": str(e)}, "failed")
            raise NonRetryableError(f"工具内部错误：{e}") from e
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
            return cached

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


async def _execute_calendar_tool(
    session_id: str, tool_name: str, args: dict, user_id: int,
    idempotency_key: str | None, max_retries: int, meta: dict,
) -> dict:
    """日历工具执行：走用户级飞书日历客户端。"""
    from .feishu_calendar_wrapper import (
        FeishuApiError, FeishuAuthError, FeishuNotConfiguredError,
        create_event, list_events,
    )

    if idempotency_key:
        cached = await _cache_get(idempotency_key)
        if cached is not None:
            return cached

    last_err: Exception | None = None
    for i in range(max_retries):
        try:
            if tool_name == "list_calendar_events":
                result = await list_events(
                    user_id=user_id,
                    time_min=args["time_min"],
                    time_max=args["time_max"],
                )
            elif tool_name == "create_calendar_event":
                result = await create_event(
                    user_id=user_id,
                    title=args["title"],
                    start=args["start"],
                    end=args["end"],
                    attendees=args.get("attendees"),
                    description=args.get("description", ""),
                )
            else:
                raise NonRetryableError(f"未知日历工具：{tool_name}")

            if idempotency_key:
                await _cache_set(idempotency_key, result)
            await _audit(user_id, session_id, tool_name, args, result, "success")
            return result

        except (FeishuNotConfiguredError, FeishuAuthError, FeishuApiError) as e:
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
