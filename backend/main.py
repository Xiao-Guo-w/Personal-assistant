"""
FastAPI 入口（多用户版）。

设计要点：
- lifespan 里初始化 ORM 引擎和 LangGraph checkpointer
- 所有涉及用户数据的接口都必须 Depends(current_user)
- 会话相关接口必须先 assert_owned，实现多用户隔离
- /api/chat 是主入口，自动处理状态机、确认、重试、审计
"""
from dotenv import load_dotenv

load_dotenv()


import uuid

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from .auth import current_user
from .db import close_db, init_db
from .graph import close_graph, get_graph, init_graph
from .memory import get_user_memories
from .sessions import (
    assert_owned,
    create_session,
    delete_session,
    list_sessions,
    update_title,
)
from .users import (
    authenticate,
    create_user,
    issue_token,
    revoke_token,
)


# ============================================================
# 应用生命周期
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    启动顺序：
    1. 先初始化 ORM 引擎（幂等 / 审计表）——executor 依赖它
    2. 再初始化 LangGraph checkpointer
    关闭时逆序清理。
    """
    await init_db()
    await init_graph()
    yield
    await close_graph()
    await close_db()


app = FastAPI(title="Personal Assistant Agent", lifespan=lifespan)

# 开发阶段允许所有来源；生产环境务必收敛到具体域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# 请求 / 响应模型
# ============================================================

class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    display_name: str = ""
    timezone: str = "Asia/Shanghai"


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    token: str
    user: dict


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    status: str
    pending_action: dict | None = None


# ============================================================
# 认证接口
# ============================================================

@app.post("/api/auth/register", response_model=AuthResponse)
async def register(req: RegisterRequest):
    """注册。注册后直接颁发 token，前端无需再调登录。"""
    try:
        user = await create_user(
            req.username, req.password, req.display_name, req.timezone,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    token = await issue_token(user["id"])
    return AuthResponse(token=token, user=user)


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    """登录，返回 token。"""
    user = await authenticate(req.username, req.password)
    if user is None:
        raise HTTPException(401, "用户名或密码错误")
    token = await issue_token(user["id"])
    return AuthResponse(token=token, user=user)


@app.post("/api/auth/logout")
async def logout(authorization: str | None = None):
    """登出：删除 token。"""
    if authorization and authorization.startswith("Bearer "):
        await revoke_token(authorization.removeprefix("Bearer ").strip())
    return {"ok": True}


@app.get("/api/me")
async def me(user: dict = Depends(current_user)):
    """当前登录用户信息。"""
    return user


# ============================================================
# 会话接口
# ============================================================

@app.get("/api/sessions")
async def sessions_list(user: dict = Depends(current_user)):
    """列出当前用户所有会话。"""
    return await list_sessions(user["id"])


@app.post("/api/sessions")
async def sessions_create(user: dict = Depends(current_user)):
    """新建会话。"""
    return await create_session(user["id"])


@app.delete("/api/sessions/{session_id}")
async def sessions_delete(session_id: str, user: dict = Depends(current_user)):
    """删除会话（先校验归属）。"""
    try:
        await assert_owned(session_id, user["id"])
    except PermissionError as e:
        raise HTTPException(403, str(e))
    await delete_session(session_id)
    return {"ok": True}


# ============================================================
# 聊天接口
# ============================================================

@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: dict = Depends(current_user)):
    """
    主聊天入口。

    流程：
    1. 校验 session 归属当前用户（多用户隔离）
    2. 把消息和用户上下文送入 LangGraph
    3. 从返回的 state 提取回复

    首次发消息时自动更新会话标题为消息前 30 字。
    """
    # 归属校验：防止用户 A 操作 B 的会话
    try:
        await assert_owned(req.session_id, user["id"])
    except PermissionError as e:
        raise HTTPException(403, str(e))

    config = {"configurable": {"thread_id": req.session_id}}

    try:
        result = await get_graph().ainvoke(
            {
                "messages": [HumanMessage(content=req.message)],
                "session_id": req.session_id,
                "user_id": user["id"],
                "timezone": user["timezone"],
            },
            config=config,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"agent error: {e}")

    # 首条消息设置会话标题
    existing = await list_sessions(user["id"])
    for s in existing:
        if s["id"] == req.session_id and s["title"] == "新会话":
            await update_title(req.session_id, req.message[:30])
            break

    # 从最新消息里找最后一条非空 AIMessage 作为回复
    reply = ""
    for m in reversed(result["messages"]):
        if isinstance(m, AIMessage) and m.content:
            reply = m.content
            break

    stage = result.get("stage") or "IDLE"
    status = "WAITING_CONFIRMATION" if stage == "WAITING_CONFIRMATION" else "DONE"

    return ChatResponse(
        session_id=req.session_id,
        reply=reply,
        status=status,
        pending_action=result.get("pending_action"),
    )


@app.get("/api/sessions/{session_id}")
async def session_detail(session_id: str, user: dict = Depends(current_user)):
    """查看会话状态（先校验归属）。"""
    try:
        await assert_owned(session_id, user["id"])
    except PermissionError as e:
        raise HTTPException(403, str(e))

    config = {"configurable": {"thread_id": session_id}}
    snapshot = await get_graph().aget_state(config)
    if not snapshot or not snapshot.values:
        raise HTTPException(404, "会话状态不存在")

    return {
        "id": session_id,
        "stage": snapshot.values.get("stage"),
        "pending_action": snapshot.values.get("pending_action"),
        "messages": [
            {
                "role": _role(m),
                "content": m.content if hasattr(m, "content") else str(m),
            }
            for m in snapshot.values.get("messages", [])
        ],
    }


# ============================================================
# 记忆接口
# ============================================================

@app.get("/api/memory")
async def get_memory(user: dict = Depends(current_user)):
    """查看当前用户的长期记忆。"""
    return await get_user_memories(user["id"])


def _role(msg) -> str:
    """LangChain 消息类型 → 简单 role 字符串。"""
    name = type(msg).__name__
    return {
        "HumanMessage": "user",
        "AIMessage": "assistant",
        "ToolMessage": "tool",
        "SystemMessage": "system",
    }.get(name, "unknown")


@app.get("/health")
async def health():
    """健康检查。"""
    return {"ok": True}