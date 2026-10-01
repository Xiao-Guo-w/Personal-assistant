"""
FastAPI 入口（多用户隔离 + 加密存储 + 引导 + OAuth 集成）。

接口分类：
- 认证：/api/auth/*、/api/me、/api/onboarding/*
- 会话：/api/sessions/*
- 聊天：/api/chat
- 记忆：/api/memory/*
- OAuth：/api/oauth/feishu/*、/api/oauth/notion/*
- 集成状态：/api/integrations/*
- QQ 邮箱配置（手动）：/api/integrations/email
"""
from dotenv import load_dotenv

load_dotenv()


import asyncio
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from .auth import current_user
from .config import settings
from .crypto import EncryptionError, _get_fernet
from .db import close_db, init_db
from .graph import close_graph, get_graph, init_graph
from .memory import delete_user_memory, list_user_memories_detailed
from .oauth_helpers import generate_state, verify_state
from .sessions import (
    assert_owned, create_session, delete_session, list_sessions, update_title,
)
from .users import (
    authenticate,
    create_user,
    issue_token,
    mark_onboarded,
    revoke_token,
    update_user_profile,
)


# ============================================================
# 应用生命周期
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    启动顺序：
    1. 校验加密密钥（fail-fast）
    2. 初始化 ORM 引擎
    3. 初始化 LangGraph checkpointer
    4. 启动提醒调度后台任务（可用配置关闭）
    关闭时逆序清理。
    """
    try:
        _get_fernet()
    except EncryptionError as e:
        raise RuntimeError(f"启动失败：{e}") from e

    await init_db()
    await init_graph()

    # 提醒调度：进程内后台循环，到点的提醒由它投递（邮件 + 站内）
    reminder_stop: asyncio.Event | None = None
    reminder_task: asyncio.Task | None = None
    if settings.reminder_scheduler_enabled:
        from .reminders import run_reminder_scheduler

        reminder_stop = asyncio.Event()
        reminder_task = asyncio.create_task(run_reminder_scheduler(reminder_stop))

    yield

    # 先停调度循环，再关连接
    if reminder_stop is not None:
        reminder_stop.set()
    if reminder_task is not None:
        try:
            await asyncio.wait_for(reminder_task, timeout=5)
        except asyncio.TimeoutError:
            reminder_task.cancel()

    from .notion_client_wrapper import close_all_clients
    await close_all_clients()
    await close_graph()
    await close_db()


app = FastAPI(title="Personal Assistant Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
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


class EmailConfigRequest(BaseModel):
    """QQ 邮箱配置请求（无 OAuth，需手动填写）。"""
    address: str = Field(description="QQ 邮箱地址")
    auth_code: str = Field(min_length=16, max_length=32, description="SMTP/IMAP 授权码")


class UpdateProfileRequest(BaseModel):
    display_name: str | None = None
    timezone: str | None = None


class CompleteOnboardingRequest(BaseModel):
    default_meeting_duration: int | None = None
    timezone: str | None = None


# ============================================================
# 认证接口
# ============================================================

@app.post("/api/auth/register", response_model=AuthResponse)
async def register(req: RegisterRequest):
    """注册。onboarded=False 会触发前端引导流程。"""
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


@app.patch("/api/me")
async def update_me(req: UpdateProfileRequest, user: dict = Depends(current_user)):
    """更新当前用户资料。"""
    try:
        updated = await update_user_profile(
            user["id"],
            display_name=req.display_name,
            timezone_str=req.timezone,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return updated


# ============================================================
# 引导流程
# ============================================================

@app.get("/api/onboarding/status")
async def onboarding_status(user: dict = Depends(current_user)):
    """查询当前用户的引导状态。"""
    return {"onboarded": user.get("onboarded", False)}


@app.post("/api/onboarding/complete")
async def complete_onboarding(
    req: CompleteOnboardingRequest,
    user: dict = Depends(current_user),
):
    """完成引导：保存偏好 + 标记已引导。"""
    from .memory import set_user_memory

    if req.timezone:
        await update_user_profile(user["id"], timezone_str=req.timezone)

    if req.default_meeting_duration is not None:
        await set_user_memory(
            user["id"],
            "default_meeting_duration",
            str(req.default_meeting_duration),
        )

    await mark_onboarded(user["id"])
    return {"ok": True}


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
# 聊天接口
# ============================================================

@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: dict = Depends(current_user)):
    """主聊天入口。"""
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


# ============================================================
# 记忆接口
# ============================================================

@app.get("/api/memory")
async def get_memory(user: dict = Depends(current_user)):
    """查看当前用户的长期记忆（带展示元数据）。"""
    return await list_user_memories_detailed(user["id"])


@app.delete("/api/memory/{key}")
async def delete_memory(key: str, user: dict = Depends(current_user)):
    """删除一条长期记忆。"""
    ok = await delete_user_memory(user["id"], key)
    if not ok:
        raise HTTPException(404, "记忆不存在")
    return {"ok": True}


# ============================================================
# 飞书 OAuth
# ============================================================

@app.get("/api/oauth/feishu/authorize")
async def feishu_authorize(user: dict = Depends(current_user)):
    """返回飞书授权 URL。"""
    from .feishu_calendar_wrapper import build_authorize_url

    state = generate_state(user["id"])
    url = build_authorize_url(state)
    return {"authorize_url": url, "state": state}


@app.get("/api/oauth/feishu/callback")
async def feishu_callback(code: str = "", state: str = "", error: str = ""):
    """飞书 OAuth 回调，换取 user_access_token 并加密存储。"""
    from .feishu_calendar_wrapper import (
        FeishuAuthError, exchange_code_for_token, save_oauth_result,
    )

    if error:
        return HTMLResponse(_oauth_result_html(
            success=False, provider="飞书日历",
            message=f"授权被拒绝：{error}",
        ))

    try:
        user_id = verify_state(state)
    except ValueError as e:
        return HTMLResponse(_oauth_result_html(
            success=False, provider="飞书日历",
            message=f"state 校验失败：{e}",
        ))

    try:
        token_data = await exchange_code_for_token(code)
        await save_oauth_result(user_id, token_data)
        return HTMLResponse(_oauth_result_html(
            success=True, provider="飞书日历",
            message=f"已连接到飞书账号：{token_data.get('name', '')}",
        ))
    except FeishuAuthError as e:
        return HTMLResponse(_oauth_result_html(
            success=False, provider="飞书日历", message=str(e),
        ))


# ============================================================
# Notion OAuth
# ============================================================

@app.get("/api/oauth/notion/authorize")
async def notion_authorize(user: dict = Depends(current_user)):
    """返回 Notion 授权 URL。"""
    from .notion_client_wrapper import build_authorize_url

    state = generate_state(user["id"])
    url = build_authorize_url(state)
    return {"authorize_url": url, "state": state}


@app.get("/api/oauth/notion/callback")
async def notion_callback(code: str = "", state: str = "", error: str = ""):
    """Notion OAuth 回调，换取 access_token 并加密存储。"""
    from .notion_client_wrapper import (
        NotionAuthError, exchange_code_for_token, save_oauth_result,
    )

    if error:
        return HTMLResponse(_oauth_result_html(
            success=False, provider="Notion",
            message=f"授权被拒绝：{error}",
        ))

    try:
        user_id = verify_state(state)
    except ValueError as e:
        return HTMLResponse(_oauth_result_html(
            success=False, provider="Notion",
            message=f"state 校验失败：{e}",
        ))

    try:
        token_data = await exchange_code_for_token(code)
        await save_oauth_result(user_id, token_data)
        return HTMLResponse(_oauth_result_html(
            success=True, provider="Notion",
            message=f"已连接到工作区：{token_data.get('workspace_name', '')}",
        ))
    except NotionAuthError as e:
        return HTMLResponse(_oauth_result_html(
            success=False, provider="Notion", message=str(e),
        ))


# ============================================================
# 集成状态查询与断开
# ============================================================

@app.get("/api/integrations/status")
async def integrations_status(user: dict = Depends(current_user)):
    """查询用户所有集成的连接状态。"""
    from .integrations import get_integration

    result = {}

    feishu = await get_integration(user["id"], "feishu_calendar")
    result["feishu_calendar"] = {
        "connected": bool(feishu and feishu.get("open_id")),
        "name": feishu.get("name", "") if feishu else "",
        "open_id": feishu.get("open_id", "") if feishu else "",
    }

    notion = await get_integration(user["id"], "notion")
    result["notion"] = {
        "connected": bool(notion and notion.get("access_token")),
        "workspace_name": notion.get("workspace_name", "") if notion else "",
        "workspace_id": notion.get("workspace_id", "") if notion else "",
        "default_parent_page_id": notion.get("default_parent_page_id", "") if notion else "",
    }

    email = await get_integration(user["id"], "email")
    result["email"] = {
        "connected": bool(email and email.get("address")),
        "address": email.get("address", "") if email else "",
    }

    return result


@app.delete("/api/integrations/{provider}")
async def disconnect_integration(provider: str, user: dict = Depends(current_user)):
    """断开某个集成。"""
    from .integrations import delete_integration

    if provider == "notion":
        from .notion_client_wrapper import invalidate_client
        await invalidate_client(user["id"])
    elif provider in ("feishu_calendar", "email"):
        pass
    else:
        raise HTTPException(400, f"未知 provider：{provider}")

    ok = await delete_integration(user["id"], provider)
    if not ok:
        raise HTTPException(404, f"未连接 {provider}")
    return {"ok": True}


# ============================================================
# Notion 辅助接口
# ============================================================

@app.put("/api/integrations/notion/default-parent-page")
async def set_notion_default_parent_page(
    page_id: str,
    user: dict = Depends(current_user),
):
    """
    设置 Notion 默认父页面（页面模式下新页面的落点）。

    写入前先真正访问一次 Notion（inspect_parent_page）：
    挡住拼错 / 被截断的 ID，以及"这个页面没授权给集成"的情况。
    """
    from .notion_client_wrapper import (
        NotionAuthError, NotionNotConfiguredError,
        inspect_parent_page, set_default_parent_page,
    )

    try:
        page = await inspect_parent_page(user["id"], page_id)
    except (NotionNotConfiguredError, NotionAuthError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001 —— Notion 侧异常统一转成可读错误
        raise HTTPException(502, f"校验父页面失败：{e}")

    await set_default_parent_page(user["id"], page["page_id"])
    return {
        "ok": True,
        "page_id": page["page_id"],
        "title": page.get("title", ""),
    }


@app.get("/api/integrations/notion/pages")
async def list_notion_pages(user: dict = Depends(current_user)):
    """列出用户授权范围内可见的页面（用于选择默认父页面）。"""
    from .notion_client_wrapper import NotionAuthError, list_pages

    try:
        return {"pages": await list_pages(user["id"])}
    except NotionAuthError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"查询页面列表失败：{e}")


# ============================================================
# QQ 邮箱（手动配置，无 OAuth）
# ============================================================

@app.put("/api/integrations/email")
async def set_email_config(req: EmailConfigRequest, user: dict = Depends(current_user)):
    """保存 QQ 邮箱配置（QQ 邮箱无 OAuth 接口，需手动填写授权码）。"""
    from .integrations import set_integration
    await set_integration(user["id"], "email", {
        "address": req.address,
        "auth_code": req.auth_code,
    })
    return {"ok": True}


@app.get("/api/integrations/email")
async def get_email_config(user: dict = Depends(current_user)):
    """查看 QQ 邮箱配置（脱敏）。"""
    from .crypto import mask
    from .integrations import get_integration

    config = await get_integration(user["id"], "email")
    if config is None:
        raise HTTPException(404, "未配置 QQ 邮箱")

    return {
        "address": config.get("address", ""),
        "auth_code_masked": mask(config.get("auth_code", ""), 6, 4),
        "configured": True,
    }


# ============================================================
# 提醒接口
# ============================================================

@app.get("/api/reminders")
async def reminders_list(
    status: str = "pending",
    limit: int = 10,
    user: dict = Depends(current_user),
):
    """查询提醒列表。status 取 pending / fired / cancelled / all。"""
    from .reminders import list_reminders

    return await list_reminders(user["id"], status, limit)


@app.get("/api/reminders/inbox")
async def reminders_inbox(user: dict = Depends(current_user)):
    """
    站内提醒通知：已触发但用户还没点"知道了"的提醒。

    前端每隔几十秒轮询一次，到点的提醒就会在聊天页顶部冒出来。
    """
    from .reminders import list_inbox

    return await list_inbox(user["id"])


@app.post("/api/reminders/{reminder_id}/ack")
async def reminders_ack(reminder_id: int, user: dict = Depends(current_user)):
    """标记站内提醒为已知晓。"""
    from .reminders import acknowledge_reminder

    if not await acknowledge_reminder(user["id"], reminder_id):
        raise HTTPException(404, "提醒不存在")
    return {"ok": True}


@app.post("/api/reminders/{reminder_id}/cancel")
async def reminders_cancel(reminder_id: int, user: dict = Depends(current_user)):
    """取消一条待触发的提醒（前端列表里的取消按钮走这里）。"""
    from .reminders import cancel_reminder

    return await cancel_reminder(user["id"], reminder_id=str(reminder_id))


# ============================================================
# 工具函数与健康检查
# ============================================================

def _role(msg) -> str:
    """LangChain 消息类型 → 简单 role 字符串。"""
    name = type(msg).__name__
    return {
        "HumanMessage": "user",
        "AIMessage": "assistant",
        "ToolMessage": "tool",
        "SystemMessage": "system",
    }.get(name, "unknown")


def _oauth_result_html(success: bool, provider: str, message: str) -> str:
    """生成 OAuth 回调结果页面。"""
    color = "#10b981" if success else "#ef4444"
    icon = "✅" if success else "❌"
    title = "授权成功" if success else "授权失败"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{title} - {provider}</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                display: flex; align-items: center; justify-content: center;
                min-height: 100vh; margin: 0; background: #f5f5f5;
            }}
            .card {{
                background: white; padding: 40px 60px; border-radius: 12px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.1); text-align: center;
                max-width: 480px;
            }}
            .icon {{ font-size: 48px; margin-bottom: 16px; }}
            h1 {{ color: {color}; margin: 0 0 12px 0; font-size: 24px; }}
            p {{ color: #666; margin: 8px 0; line-height: 1.6; }}
            .hint {{ color: #999; font-size: 14px; margin-top: 24px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="icon">{icon}</div>
            <h1>{title}</h1>
            <p><strong>{provider}</strong></p>
            <p>{message}</p>
            <p class="hint">请关闭此窗口，返回应用查看连接状态。</p>
        </div>
    </body>
    </html>
    """


@app.get("/health")
async def health():
    return {"ok": True}
