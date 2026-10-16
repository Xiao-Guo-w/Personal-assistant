"""
ORM 模型定义。

分类：
1. 用户体系：User（含 onboarded）/ UserToken / Session
2. 业务辅助：Idempotency / AuditLog
3. 长期记忆：UserMemory
4. 集成配置：UserIntegration（config 中的敏感字段加密存储）
5. 日程：Schedule（系统内自建，不走第三方日历 API）
6. 提醒：Reminder（后台调度器投递，通过用户绑定的 QQ 邮箱通知）

LangGraph 的 checkpoints 表由官方 checkpointer 自己管理。
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
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


# ============================================================
# 五、日程
# ============================================================

class Schedule(Base):
    """
    系统内的日程（不依赖任何第三方日历 API）。

    时间口径和 Reminder 完全一致：UTC（naive）入库，展示/发信时按用户时区换算。

    status 流转：
      active    → 有效
      cancelled → 用户取消（软删除，保留记录便于回溯）

    日程本身不会自己"响"：需要在某个时间点通知用户时，由 schedules 服务
    顺带建一条 kind=reminder、schedule_id 指向本行的提醒，
    交给后台调度器通过用户绑定的 QQ 邮箱投递。
    """
    __tablename__ = "schedules"
    __table_args__ = (
        Index("ix_schedules_user_start", "user_id", "start_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # 结束时间可空：用户说"下午 3 点开会"但没给时长时，交给默认时长补算后再写入
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    location: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<Schedule {self.id}:{self.status}:{self.title[:20]}>"


# ============================================================
# 六、提醒
# ============================================================

class Reminder(Base):
    """
    定时提醒（通过用户绑定的 QQ 邮箱 + 站内收件箱投递）。

    时间统一用 UTC（naive）存储，和 created_at 的 CURRENT_TIMESTAMP 保持一致；
    展示和发信时再按用户时区换算，避免"存本地时间、算的时候又按 UTC"的经典坑。

    status 流转：
      pending   → 待触发
      firing    → 已被调度器原子抢占，正在投递（防止多进程/重启重复投递）
      fired     → 已投递
      cancelled → 用户取消
    """
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # 任务类型：reminder=到点通知自己；email=到点把邮件发给收件人
    kind: Mapped[str] = mapped_column(String(16), default="reminder", index=True)
    # 关联的日程 ID：日程提醒非空，普通提醒为空
    schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 定时邮件的内容（JSON：to / subject / body）；
    # 日程提醒存 {"remind_minutes": n}（改期时靠它把提醒一起挪）
    payload_json: Mapped[str] = mapped_column(Text, default="")
    text: Mapped[str] = mapped_column(Text)
    remind_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 投递渠道组合，例如 "email+in_app"；留空表示还没投递过
    fired_via: Mapped[str] = mapped_column(String(32), default="")
    # 邮件投递失败的原因；站内通知不受影响
    last_error: Mapped[str] = mapped_column(Text, default="")
    # 站内通知是否已被"知道了"（为空表示还没看）
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<Reminder {self.id}:{self.status}:{self.text[:20]}>"


# ============================================================
# 六、知识库（RAG）
#
# 分工：
# - kb_documents / kb_chunks：权威数据源，存正文与元数据
# - kb_chunks.embedding：向量也留一份副本（见下），使 Chroma 成为"可离线重建的派生索引"
# - kb_vec 不再由向量库承担：向量写进 Chroma，按 user_id 分 collection
#
# 为什么把向量副本存在 SQLite：
# 1. 块级查重命中时可以复用已有向量，不再调用 embedding 服务（省钱）
# 2. 换向量库 / 换 Chroma 的目录结构时，可以只重建索引，不用重新调用 embedding
# 3. Chroma 整个删掉也不会丢数据，恢复时不依赖外部服务
# ============================================================


class KbDocument(Base):
    """
    知识库文档。一行 = 用户上传的一个文件。

    status 流转：
      pending   → 已登记，等待解析
      indexing  → 解析完成，正在分块 / 向量化 / 写索引
      ready     → 全部完成，可检索、可删除
      failed    → 出错，展示原因，允许重试或删除

    content_text 存规范化后的全文：
    - 重试时不必要求用户重新上传
    - 将来调整分块参数可以就地重建，不用重新解析原文件
    因此原始文件不需要落盘保存。
    """
    __tablename__ = "kb_documents"
    __table_args__ = (
        UniqueConstraint("user_id", "content_hash", name="uq_kb_document_hash"),
        Index("ix_kb_documents_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    # 扩展名（不含点），如 pdf / docx / md / txt，前端按它分组
    ext: Mapped[str] = mapped_column(String(16), default="")
    mime: Mapped[str] = mapped_column(String(128), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # sha256(规范化全文)，用于文档级查重
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    # 记录向量化时用的模型与维度，换了模型要能检测出维度不匹配
    embedding_model: Mapped[str] = mapped_column(String(128), default="")
    embedding_dim: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<KbDocument {self.id}:{self.status}:{self.title[:20]}>"


class KbChunk(Base):
    """
    知识库分块。

    embedding 以 float32 小端序打包成 BLOB 存放。
    为 NULL 表示这个块还没有向量（解析成功但向量化失败时会是这样）。
    """
    __tablename__ = "kb_chunks"
    __table_args__ = (
        Index("ix_kb_chunks_doc", "doc_id"),
        Index("ix_kb_chunks_user_hash", "user_id", "content_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("kb_documents.id"), index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    # 块在文档内的顺序，相邻块合并时靠它判断连续性
    idx: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # sha256(块文本)，块级查重用
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    dimension: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<KbChunk {self.id}:doc={self.doc_id}:idx={self.idx}>"
