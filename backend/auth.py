"""FastAPI 认证依赖：从 Authorization 头解析当前用户。"""

from fastapi import Header, HTTPException

from .users import resolve_token


async def current_user(
    authorization: str | None = Header(default=None),
) -> dict:
    """从 Authorization: Bearer <token> 解析用户；失败返回 401。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Authorization 头")

    token = authorization.removeprefix("Bearer ").strip()
    user = await resolve_token(token)
    if user is None:
        raise HTTPException(401, "token 无效或已过期")
    return user