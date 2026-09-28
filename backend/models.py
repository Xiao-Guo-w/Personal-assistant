"""
ORM 模型定义。

使用 SQLAlchemy 2.0 的 DeclarativeBase + Mapped 语法，
类型提示与列定义合一，IDE 能直接推断属性类型。

分四类：
1. 用户体系：User / UserToken / Session
2. 业务辅助：Idempotency / AuditLog
3. 长期记忆：UserMemory

LangGraph 的 checkpoints 表由官方 checkpointer 自己管理，
不放进模型，避免耦合。
"""

from datetime import datetime

from sqlalchemy import (
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
    """所有 ORM 模型的基类。SQLAlchemy 2.0 推荐写法。"""
    pass


# ============================================================
# 一、用户体系
# ============================================================

class User(Base):
    """
    用户主体。

    username 唯一，password_hash 由 security.hash_password 生成。
    timezone 用于系统提示词注入（相对时间转换依赖它）。
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 存 hash 而非明文；PBKDF2-HMAC-SHA256 十万次迭代
    password_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<User {self.id}:{self.username}>"


class UserToken(Base):
    """
    登录 token。

    简化方案：token 存明文（本身是 secrets 生成的 32 字节随机串）。
    生产建议：存 sha256(token)，避免数据库泄露后直接可用。
    """
    __tablename__ = "user_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 一个用户可以有多个有效 token（多端登录）
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class Session(Base):
    """
    会话元信息。

    与 LangGraph 的 thread_id 一一对应（id 就是 thread_id）。
    额外记录归属用户，用于：
    - 会话列表展示
    - 权限校验（防止用户 A 拿到 B 的 session_id 后偷看）
    - 标题、时间戳等元信息

    注意：真正的对话内容在 checkpoints.db 里，
    这里只存「会话的元信息」，避免重复存储。
    """
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
    """
    幂等表。

    key = hash(user_id + 工具名 + 参数 + 会话)，
    命中即返回缓存结果，避免写操作重复执行。

    user_id 参与 hash 是为了防止跨用户命中（多用户隔离）。
    """
    __tablename__ = "idempotency"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # result 存 JSON 字符串；用 Text 兼容 SQLite 和 Postgres
    result: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class AuditLog(Base):
    """
    审计日志。

    每次工具调用都写一条：谁、什么工具、什么参数、结果、状态。
    用于排障、追责、合规审计。
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    # args / result 是任意 JSON，用 Text 存序列化字符串
    args: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    # status: success / retry / failed
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, index=True
    )


# ============================================================
# 三、长期记忆
# ============================================================

class UserMemory(Base):
    """
    跨会话的用户长期记忆。

    (user_id, key) 唯一，同一偏好只有一条记录。
    每次 LLM 调用时动态注入到系统提示词。

    典型内容：
    - default_meeting_duration = 60
    - timezone = Asia/Shanghai
    - contact_zhangsan_email = zhangsan@example.com
    """
    __tablename__ = "user_memory"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_memory"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<UserMemory {self.user_id}:{self.key}={self.value}>"