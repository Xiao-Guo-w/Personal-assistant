"""
LangGraph 图状态。

字段说明：
- messages：LangChain 消息列表，add_messages reducer 自动追加
- stage：IDLE / WAITING_CONFIRMATION
- pending_action：等待确认时非空
- session_id：业务会话 ID（同时作为 thread_id）
- user_id：用户 ID（长期记忆归属）
- timezone：用户时区（系统提示注入）
"""

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    # add_messages 是 reducer：节点返回的 messages 会被追加而非覆盖
    messages: Annotated[list[BaseMessage], add_messages]

    stage: str                     # IDLE / WAITING_CONFIRMATION
    pending_action: dict | None    # 等待确认时非空
    session_id: str                # 与 thread_id 一致
    user_id: int                   # 用户 ID（长期记忆隔离）
    timezone: str                  # 用户时区