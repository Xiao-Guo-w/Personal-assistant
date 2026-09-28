"""
会话 CRUD。

Session 表只存元信息（id / 归属 / 标题 / 时间戳），
真正的对话状态在 LangGraph 的 checkpoints.db 里。

核心安全函数：assert_owned —— 多用户隔离的第一道关。
"""

import uuid

from sqlalchemy import select

from .db import session_scope
from .models import Session


async def create_session(user_id: int, title: str = "新会话") -> dict:
    """新建会话。id 同时作为 LangGraph 的 thread_id。"""
    sid = str(uuid.uuid4())
    async with session_scope() as db:
        sess = Session(id=sid, user_id=user_id, title=title)
        db.add(sess)
        await db.flush()
        return {
            "id": sess.id,
            "user_id": sess.user_id,
            "title": sess.title,
        }


async def list_sessions(user_id: int) -> list[dict]:
    """列出某用户的所有会话，按更新时间倒序。"""
    async with session_scope() as db:
        result = await db.execute(
            select(Session)
            .where(Session.user_id == user_id)
            .order_by(Session.updated_at.desc())
        )
        rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "title": r.title,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


async def get_session(session_id: str) -> dict | None:
    """查单个会话元信息。"""
    async with session_scope() as db:
        result = await db.execute(
            select(Session).where(Session.id == session_id)
        )
        row = result.scalar_one_or_none()
    if row is None:
        return None
    return {"id": row.id, "user_id": row.user_id, "title": row.title}


async def assert_owned(session_id: str, user_id: int) -> dict:
    """
    校验会话属于当前用户。

    多用户隔离的核心：任何操作 session 的 API 都要先过这一关。
    用户 A 拿到用户 B 的 session_id 也不能访问，会抛 PermissionError。
    """
    sess = await get_session(session_id)
    if sess is None:
        raise PermissionError("会话不存在")
    if sess["user_id"] != user_id:
        raise PermissionError("无权访问该会话")
    return sess


async def update_title(session_id: str, title: str) -> None:
    """更新会话标题（用于首条消息设置标题）。"""
    async with session_scope() as db:
        result = await db.execute(
            select(Session).where(Session.id == session_id)
        )
        row = result.scalar_one_or_none()
        if row:
            row.title = title[:128]


async def delete_session(session_id: str) -> None:
    """删除会话元信息。图状态由 checkpointer 自行保留或清理。"""
    async with session_scope() as db:
        result = await db.execute(
            select(Session).where(Session.id == session_id)
        )
        row = result.scalar_one_or_none()
        if row:
            await db.delete(row)