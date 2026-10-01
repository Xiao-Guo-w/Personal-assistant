"""
异步数据库引擎 + 会话工厂。

- engine：全局引擎，管理连接池
- session_scope()：异步上下文管理器，自动 commit / rollback
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)

from .config import settings
from .models import Base

# 全局引擎：应用生命周期内只建一次
engine = create_async_engine(
    f"sqlite+aiosqlite:///{settings.db_path}",
    echo=False,
)

# 会话工厂：expire_on_commit=False 让 commit 后对象仍可访问属性
async_session_factory = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False,
)


async def init_db() -> None:
    """应用启动时调用一次。建表 + 开启 SQLite WAL。"""
    async with engine.begin() as conn:
        # SQLite 特有 PRAGMA，换 Postgres 时删掉这两行
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
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
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise