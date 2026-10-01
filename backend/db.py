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
        # create_all 只建缺失的表，不会给已存在的表补列；
        # 这里对后加的列做一次幂等 ALTER TABLE（见 _apply_light_migrations）
        await conn.run_sync(_apply_light_migrations)


def _apply_light_migrations(conn) -> None:
    """
    轻量迁移：给已经存在的表补上后加的列。

    为什么需要：用户库里已经有 reminders 表时，create_all 不会补列，
    于是新增字段会直接报 "no such column"。SQLite 支持 ADD COLUMN，
    这里按 PRAGMA 结果逐个补，重复执行安全（已存在的列跳过）。
    只做加列，不做改类型/删列 —— 需要更复杂的变更时再换 Alembic。
    """
    wanted = {
        "reminders": {
            "kind": "VARCHAR(16) DEFAULT 'reminder'",
            "payload_json": "TEXT DEFAULT ''",
        },
    }

    for table, columns in wanted.items():
        rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue   # 表还不存在：本次 create_all 已按最新定义建好
        existing = {row[1] for row in rows}
        for name, ddl in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


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
