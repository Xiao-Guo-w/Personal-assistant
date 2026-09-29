"""
LangGraph 装配。

图结构：

    START
      │
      │ route_entry（按 stage 分流）
      ├──────────────────────────┐
      ▼                          ▼
    prepare_context        handle_confirmation
      │                          │
      ▼                          ▼
     llm                        END
      │
      │ route_after_llm
      ├──→ END
      ├──→ prepare_confirmation ──→ END
      └──→ execute_safe_tool → summarize → END

checkpointer 使用 AsyncSqliteSaver，按 thread_id 持久化图状态。

重要：AsyncSqliteSaver.from_conn_string 是 async context manager，
必须进入它并把 checkpointer 保存在全局，直到应用关闭；
否则出了 with 块连接就关了，后续请求全崩。
"""

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from .config import settings
from .nodes import (
    execute_safe_tool_node,
    handle_confirmation_node,
    llm_node,
    prepare_confirmation_node,
    prepare_context_node,
    route_after_llm,
    route_entry,
    summarize_node,
)
from .state import AgentState

# 全局单例；由 init_graph() 在 lifespan 中初始化
_graph = None
_checkpointer_cm = None   # 保存 context manager，用于关闭时释放


async def init_graph() -> None:
    """异步初始化编译好的图。"""
    global _graph, _checkpointer_cm

    _checkpointer_cm = AsyncSqliteSaver.from_conn_string(settings.checkpoint_db)
    checkpointer = await _checkpointer_cm.__aenter__()

    builder = StateGraph(AgentState)

    # 注册节点
    builder.add_node("prepare_context", prepare_context_node)
    builder.add_node("llm", llm_node)
    builder.add_node("prepare_confirmation", prepare_confirmation_node)
    builder.add_node("execute_safe_tool", execute_safe_tool_node)
    builder.add_node("summarize", summarize_node)
    builder.add_node("handle_confirmation", handle_confirmation_node)

    # START 处条件路由
    builder.add_conditional_edges(
        START,
        route_entry,
        {
            "handle_confirmation": "handle_confirmation",
            "prepare_context": "prepare_context",
        },
    )

    # 摘要后进入 LLM
    builder.add_edge("prepare_context", "llm")

    # LLM 后按工具风险分流
    builder.add_conditional_edges(
        "llm",
        route_after_llm,
        {
            "end": END,
            "confirm": "prepare_confirmation",
            "execute": "execute_safe_tool",
        },
    )

    # 简单边
    builder.add_edge("prepare_confirmation", END)
    builder.add_edge("execute_safe_tool", "summarize")
    builder.add_edge("summarize", END)
    builder.add_edge("handle_confirmation", END)

    _graph = builder.compile(checkpointer=checkpointer)


async def close_graph() -> None:
    """释放 checkpointer 连接。"""
    global _checkpointer_cm, _graph
    if _checkpointer_cm is not None:
        await _checkpointer_cm.__aexit__(None, None, None)
        _checkpointer_cm = None
    _graph = None


def get_graph():
    """对外暴露图实例。调用前必须确保 init_graph 已完成。"""
    if _graph is None:
        raise RuntimeError("Graph 未初始化，请先 await init_graph()")
    return _graph