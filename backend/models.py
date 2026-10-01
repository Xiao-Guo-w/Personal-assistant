"""
ORM 模型定义。

分类：
1. 用户体系：User（含 onboarded）/ UserToken / Session
2. 业务辅助：Idempotency / AuditLog
3. 长期记忆：UserMemory
4. 集成配置：UserIntegration（config 中的敏感字段加密存储）

LangGraph 的 checkpoints 表由官方 checkpointer 自己管理。
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""
    pass


# ============================================================
# 一、用户体系
# ============================================================

class User(Base):
    """
    用户主体。

    onboarded：是否已完成引导流程。
    注册后为 False，前端据此决定是否展示引导页。
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    onboarded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )

    def __repr__(self) -> str:
        return f"<User {self.id}:{self.username}>"


class UserToken(Base):
    """登录 token。支持多端登录。"""
    __tablename__ = "user_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class Session(Base):
    """会话元信息。id 与 LangGraph thread_id 一致。"""
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(128), default="新会话")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(),
    )


# ============================================================
# 二、业务辅助表
# ============================================================

class Idempotency(Base):
    """幂等表：key = hash(user+tool+args+session)。"""
    __tablename__ = "idempotency"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class AuditLog(Base):
    """审计日志：每次工具调用都写一条，可追溯到用户。"""
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    args: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )


# ============================================================
# 三、长期记忆
# ============================================================

class UserMemory(Base):
    """跨会话的用户长期记忆。(user_id, key) 唯一。"""
    __tablename__ = "user_memory"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_memory"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


# ============================================================
# 四、集成配置（多用户隔离 + 加密存储）
# ============================================================

class UserIntegration(Base):
    """
    用户级第三方集成配置。

    每个 (user_id, provider) 唯一。config 是 JSON 字符串。
    敏感字段（token / secret / key）写入前加密，读取时解密。
    """
    __tablename__ = "user_integrations"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_user_provider"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    config: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<UserIntegration {self.user_id}:{self.provider}>"