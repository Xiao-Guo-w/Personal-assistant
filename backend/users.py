"""
用户 CRUD 与 token 管理。

所有数据库操作走 session_scope，返回 dict 或 ORM 对象，
避免 ORM 对象泄漏到路由外（脱离 session 后访问会报错）。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .config import settings
from .db import session_scope
from .models import User, UserToken
from .security import generate_token, hash_password, verify_password


async def create_user(
    username: str,
    password: str,
    display_name: str = "",
    timezone_str: str = "Asia/Shanghai",
) -> dict:
    """
    注册新用户。

    唯一性双重校验：
    1. 应用层先查（给用户友好提示）
    2. DB 层 unique 约束（并发注册时兜底）
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
        )
        db.add(user)
        # flush 让 user.id 立即可用（不用等 commit）
        await db.flush()
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "timezone": user.timezone,
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
    }


async def issue_token(user_id: int) -> str:
    """
    登录成功后颁发 token。

    同用户可以有多个有效 token（多端登录），
    过期时间统一由 settings.token_ttl_seconds 控制。
    """
    token = generate_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.token_ttl_seconds
    )
    async with session_scope() as db:
        db.add(UserToken(token=token, user_id=user_id, expires_at=expires_at))
    return token


async def resolve_token(token: str) -> dict | None:
    """
    根据 token 查用户。

    过期 token 顺手清理，避免表膨胀。
    """
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
        # SQLite 存的 datetime 是 naive，比较前补 UTC tzinfo
        exp = user_token.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            await db.delete(user_token)   # 顺手清理过期
            return None

        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "timezone": user.timezone,
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
    }