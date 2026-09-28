"""
长期记忆读写（跨会话的用户偏好）。

存储层在 ORM 的 UserMemory 表。
每次 LLM 调用时，context.build_system_msg 会读取并注入系统提示词。
"""

from sqlalchemy import select

from .db import session_scope
from .models import UserMemory


async def get_user_memories(user_id: int) -> dict[str, str]:
    """读取用户所有偏好，返回 {key: value} 字典。"""
    async with session_scope() as db:
        result = await db.execute(
            select(UserMemory).where(UserMemory.user_id == user_id)
        )
        rows = result.scalars().all()
    return {row.key: row.value for row in rows}


async def set_user_memory(user_id: int, key: str, value: str) -> None:
    """
    写入 / 更新用户偏好。

    先查再改，保证 (user_id, key) 唯一。
    高并发下应换 INSERT ... ON CONFLICT DO UPDATE，入门够用。
    """
    async with session_scope() as db:
        existing = await db.execute(
            select(UserMemory).where(
                UserMemory.user_id == user_id,
                UserMemory.key == key,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            row.value = value
        else:
            db.add(UserMemory(user_id=user_id, key=key, value=value))