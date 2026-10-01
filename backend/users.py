"""
用户 CRUD 与 token 管理。

包含引导流程相关函数：update_user_profile / mark_onboarded。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import User, UserToken
from .security import generate_token, hash_password, verify_password


async def create_user(
    username: str, password: str, display_name: str = "",
    timezone_str: str = "Asia/Shanghai",
) -> dict:
    """
    注册新用户。

    onboarded 初始为 False，前端据此触发引导流程。
    """
    async with session_scope() as db:
        existing = await db.execute(
            select(User).where(User.username == username)
        )
        if existing.scalar_one_or_none():
            raise ValueError(f"用户名已存在：{username}")

        user = User(
            username=username,
            password_hash=hash_password(password),
            display_name=display_name or username,
            timezone=timezone_str,
            onboarded=False,
        )
        db.add(user)
        await db.flush()
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "timezone": user.timezone,
            "onboarded": user.onboarded,
        }


async def authenticate(username: str, password: str) -> dict | None:
    """校验用户名密码；失败返回 None。"""
    async with session_scope() as db:
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "timezone": user.timezone,
        "onboarded": user.onboarded,
    }


async def issue_token(user_id: int) -> str:
    """登录成功后颁发 token。"""
    token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.token_ttl_seconds
    )
    async with session_scope() as db:
        db.add(UserToken(token=token, user_id=user_id, expires_at=expires_at))
    return token


async def resolve_token(token: str) -> dict | None:
    """根据 token 查用户；过期 token 顺手清理。"""
    async with session_scope() as db:
        result = await db.execute(
            select(UserToken, User)
            .join(User, User.id == UserToken.user_id)
            .where(UserToken.token == token)
        )
        row = result.first()

        if row is None:
            return None

        user_token, user = row
        # SQLite 存的 datetime 是 naive，比较前补 UTC
        exp = user_token.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            await db.delete(user_token)
            return None

        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "timezone": user.timezone,
            "onboarded": user.onboarded,
        }


async def revoke_token(token: str) -> None:
    """登出：删除 token。"""
    async with session_scope() as db:
        result = await db.execute(
            select(UserToken).where(UserToken.token == token)
        )
        row = result.scalar_one_or_none()
        if row:
            await db.delete(row)


async def get_user_by_id(user_id: int) -> dict | None:
    """按 ID 查用户信息。"""
    async with session_scope() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
    if user is None:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "timezone": user.timezone,
        "onboarded": user.onboarded,
    }


async def update_user_profile(
    user_id: int,
    display_name: str | None = None,
    timezone_str: str | None = None,
) -> dict:
    """更新用户基础资料。None 表示不改该字段。"""
    async with session_scope() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise ValueError("用户不存在")

        if display_name is not None:
            user.display_name = display_name
        if timezone_str is not None:
            user.timezone = timezone_str

        await db.flush()
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "timezone": user.timezone,
            "onboarded": user.onboarded,
        }


async def mark_onboarded(user_id: int) -> None:
    """标记用户已完成引导。"""
    async with session_scope() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is not None:
            user.onboarded = True