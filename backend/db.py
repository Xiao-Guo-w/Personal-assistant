"""
异步数据库引擎 + 会话工厂。

核心组件：
- engine：全局引擎，管理连接池
- async_session_factory：会话工厂
- session_scope()：异步上下文管理器，自动 commit / rollback

为什么用 session 而不共享连接？
- SQLAlchemy 的 Session 不是协程安全的
- 每个业务操作应该拿一个独立 session，用后即关
- 引擎内部维护连接池，开销远小于频繁开关连接
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings
from .models import Base

# 全局引擎：应用生命周期内只建一次
# echo=False 避免 SQL 日志污染输出，调试时可改 True
engine = create_async_engine(
    f"sqlite+aiosqlite:///{settings.db_path}",
    echo=False,
)

# 会话工厂：
# - expire_on_commit=False 让 commit 后对象仍可访问属性
#   异步下不设 False 容易触发 MissingGreenlet 错误
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """
    应用启动时调用一次。
    - 开启 SQLite WAL 让读写并发友好
    - 建表（幂等：已存在的表不会重建）
    """
    # 1. 单独连接设置PRAGMA，不能放在事务内
    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
        await conn.commit()

    # 2. 建表，可以放在事务
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)



async def close_db() -> None:
    """应用关闭时释放连接池。"""
    await engine.dispose()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """
    会话作用域。

    用法：
        async with session_scope() as db:
            db.add(...)
            # 退出块自动 commit；异常自动 rollback

    这是 SQLAlchemy 推荐的 Session 使用模式，
    比手动 begin/commit/rollback 更安全。
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise