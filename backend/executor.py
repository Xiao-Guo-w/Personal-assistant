"""
工具执行器：幂等 → 重试 → 审计。

职责边界：
- nodes.py 决定「调用哪个工具、参数是什么」
- executor 决定「怎么安全地执行它」

所有数据库操作走 ORM，与 LangGraph 无关，可独立测试。
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
    """读幂等缓存；未命中返回 None。"""
    async with session_scope() as db:
        result = await db.execute(
            select(Idempotency).where(Idempotency.key == key)
        )
        row = result.scalar_one_or_none()
    return json.loads(row.result) if row else None


async def _cache_set(key: str, result: dict) -> None:
    """
    写幂等缓存。

    先查再插，保证重复 set 同一 key 不报错。
    高并发下应改 INSERT ... ON CONFLICT DO NOTHING。
    """
    async with session_scope() as db:
        existing = await db.execute(
            select(Idempotency).where(Idempotency.key == key)
        )
        if existing.scalar_one_or_none() is None:
            db.add(Idempotency(
                key=key,
                result=json.dumps(result, ensure_ascii=False),
            ))


async def _audit(
    user_id: int, session_id: str, tool_name: str,
    args: dict, result, status: str,
) -> None:
    """每次工具调用写一条审计记录。"""
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

    特判：remember_preference 不走同步 handler，直接走 ORM。
    因为记忆写入是异步数据库操作，不适合放在同步 @tool 里。
    """
    meta = get_tool_meta(tool_name)
    max_retries = max_retries or settings.max_retries

    # ---------- 特判：记忆写入 ----------
    if tool_name == "remember_preference":
        try:
            await set_user_memory(user_id, args["key"], args["value"])
            result = {"key": args["key"], "value": args["value"], "status": "remembered"}
            await _audit(user_id, session_id, tool_name, args, result, "success")
            return result
        except Exception as e:  # noqa: BLE001
            await _audit(user_id, session_id, tool_name, args, {"error": str(e)}, "failed")
            raise

    # ---------- 通用工具路径 ----------
    tool = TOOL_MAP.get(tool_name)
    if tool is None:
        raise NonRetryableError(f"未知工具：{tool_name}")

    # 幂等命中：直接返回
    if idempotency_key:
        cached = await _cache_get(idempotency_key)
        if cached is not None:
            return cached

    last_err: Exception | None = None
    for i in range(max_retries):
        try:
            # 同步 @tool 丢线程池执行，避免阻塞事件循环
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
            # 异步退避：asyncio.sleep，绝不能用 time.sleep
            # 抖动避免大量请求同时重试造成「惊群」
            await asyncio.sleep(2 ** i + random.random())

    raise last_err or RuntimeError("unknown error")