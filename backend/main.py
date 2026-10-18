"""
FastAPI 入口（多用户隔离 + 加密存储 + 引导 + QQ 邮箱绑定）。

接口分类：
- 认证：/api/auth/*、/api/me、/api/onboarding/*
- 会话：/api/sessions/*
- 聊天：/api/chat
- 记忆：/api/memory/*
- 日程：/api/schedules/*（系统内自建，不走第三方日历 API）
- 提醒：/api/reminders/*
- 集成状态：/api/integrations/status
- QQ 邮箱配置（手动填授权码）：/api/integrations/email
- 知识库：/api/kb/*
"""
from dotenv import load_dotenv

load_dotenv()


import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from .auth import current_user
from .config import settings
from .crypto import EncryptionError, _get_fernet
from .db import close_db, init_db
from .graph import close_graph, get_graph, init_graph
from .memory import (
    delete_user_memory,
    get_user_memory_detailed,
    list_user_memories_detailed,
    upsert_user_memory,
)
from .sessions import (
    assert_owned, create_session, delete_session, list_sessions, update_title,
)
from .streaming import sse_format, stream_turn
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

    # 知识库：先把上次崩溃留下的 indexing 状态复位，再启动入库 worker。
    # 复位和 worker 都在 init_db 之后，此时表一定已就绪
    kb_stop: asyncio.Event | None = None
    kb_task: asyncio.Task | None = None
    if settings.kb_enabled:
        from .kb import store as kb_store
        from .kb.ingest import run_ingest_worker

        stuck = await kb_store.reset_stuck_indexing()
        if stuck:
            logger.info("知识库：%s 篇文档从 indexing 状态恢复", stuck)

        kb_stop = asyncio.Event()
        kb_task = asyncio.create_task(run_ingest_worker(kb_stop))

    yield

    # 先停调度循环，再关连接
    if reminder_stop is not None:
        reminder_stop.set()
    if reminder_task is not None:
        try:
            await asyncio.wait_for(reminder_task, timeout=5)
        except asyncio.TimeoutError:
            reminder_task.cancel()

    if kb_stop is not None:
        kb_stop.set()
    if kb_task is not None:
        try:
            await asyncio.wait_for(kb_task, timeout=5)
        except asyncio.TimeoutError:
            kb_task.cancel()

    from .kb.deps import vectorstore
    await vectorstore.close()

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


class MemoryUpsertRequest(BaseModel):
    """用户从长期记忆页面新增或修改一条记忆。"""
    value: str = Field(min_length=1, max_length=4000)
    label: str = Field(default="", max_length=120)


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


async def _maybe_set_title(session_id: str, user_id: int, message: str) -> None:
    """
    首条消息把默认标题改成消息摘要。

    失败不影响对话本身，所以吞掉异常只记日志。
    在流式端点的 done 事件之前调用，确保前端收到 done 后
    立刻重新拉取会话列表时能拿到新标题。
    """
    try:
        for s in await list_sessions(user_id):
            if s["id"] == session_id and s["title"] == "新会话":
                await update_title(session_id, message[:30])
                break
    except Exception:  # noqa: BLE001
        logger.exception("更新会话标题失败")


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest, user: dict = Depends(current_user)):
    """
    流式聊天入口（SSE）。

    事件类型：
    - token   {"text": "..."}  增量文本，追加到当前气泡
    - replace {"text": "..."}  整条替换（平台文案 / 防演戏校正）
    - done    {"session_id", "status", "pending_action"}
    - error   {"message": "..."}

    权限校验放在开流之前：一旦开始推流就没法再返回 403，
    只能靠 error 事件告知，所以先查归属。
    """
    try:
        await assert_owned(req.session_id, user["id"])
    except PermissionError as e:
        raise HTTPException(403, str(e))

    async def event_source():
        try:
            async for event in stream_turn(
                get_graph(),
                session_id=req.session_id,
                user_id=user["id"],
                timezone=user["timezone"],
                message=req.message,
            ):
                if event["event"] == "done":
                    await _maybe_set_title(req.session_id, user["id"], req.message)
                yield sse_format(event["event"], event["data"])
        except Exception as e:  # noqa: BLE001 —— 响应头已发出，只能用事件报错
            logger.exception("流式对话失败")
            yield sse_format("error", {"message": f"agent error: {e}"})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 让 nginx 之类的反代不要缓冲
        },
    )


# ============================================================
# 记忆接口
# ============================================================

@app.get("/api/memory")
async def get_memory(user: dict = Depends(current_user)):
    """查看当前用户的长期记忆（带展示元数据）。"""
    return await list_user_memories_detailed(user["id"])


@app.put("/api/memory/{key}")
async def save_memory(
    key: str,
    req: MemoryUpsertRequest,
    user: dict = Depends(current_user),
):
    """新增或修改一条长期记忆。"""
    try:
        await upsert_user_memory(user["id"], key, req.value, req.label)
    except ValueError as e:
        raise HTTPException(400, str(e))

    item = await get_user_memory_detailed(user["id"], key)
    if item is None:  # pragma: no cover - 写入后立即读取失败属于异常
        raise HTTPException(500, "记忆保存后未能读回，请刷新重试")
    return item


@app.delete("/api/memory/{key}")
async def delete_memory(key: str, user: dict = Depends(current_user)):
    """删除一条长期记忆。"""
    ok = await delete_user_memory(user["id"], key)
    if not ok:
        raise HTTPException(404, "记忆不存在")
    return {"ok": True}


# ============================================================
# 集成状态查询与断开
#
# 只保留 QQ 邮箱：日程与提醒都在系统内部，不再需要第三方授权。
# ============================================================

@app.get("/api/integrations/status")
async def integrations_status(user: dict = Depends(current_user)):
    """查询用户所有集成的连接状态。"""
    from .integrations import get_integration

    result = {}

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

    if provider != "email":
        raise HTTPException(400, f"未知 provider：{provider}")

    ok = await delete_integration(user["id"], provider)
    if not ok:
        raise HTTPException(404, f"未连接 {provider}")
    return {"ok": True}


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
# 日程接口
#
# 日程存在系统自己的库里；到点的通知由提醒调度器通过用户绑定的
# QQ 邮箱发出，所以这一层只负责增删查改。
# ============================================================

@app.get("/api/schedules")
async def schedules_list(
    time_min: str = "",
    time_max: str = "",
    status: str = "active",
    limit: int = 20,
    user: dict = Depends(current_user),
):
    """查询日程列表。status 取 active / cancelled / all。"""
    from .schedules import ScheduleError, list_schedules

    try:
        return await list_schedules(
            user["id"], time_min=time_min, time_max=time_max,
            status=status, limit=limit,
        )
    except ScheduleError as e:
        raise HTTPException(400, str(e))


@app.post("/api/schedules/{schedule_id}/cancel")
async def schedules_cancel(schedule_id: int, user: dict = Depends(current_user)):
    """取消一条日程（同时取消它还没发出的提醒）。"""
    from .schedules import cancel_schedule

    return await cancel_schedule(user["id"], schedule_id=str(schedule_id))


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

class KbSearchRequest(BaseModel):
    """知识库检索测试请求。"""
    query: str = Field(min_length=1, max_length=500)
    top_k: int | None = None


def _require_kb() -> None:
    if not settings.kb_enabled:
        raise HTTPException(404, "知识库功能未启用")


@app.post("/api/kb/documents")
async def kb_upload(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    """
    上传文件入库。

    解析在这里同步完成（线程池执行）：解析失败能立刻返回可读原因、
    不会留下失败记录，也不需要保留临时文件。慢的部分（向量化）
    交给后台 worker。
    """
    _require_kb()

    from pathlib import Path

    from .kb import parsers
    from .kb import store as kb_store
    from .kb.chunking import normalize_text
    from .kb.deps import kick_ingest

    filename = Path(file.filename or "").name
    if not parsers.is_supported(filename):
        raise HTTPException(
            400, f"不支持的文件类型。目前支持 {parsers.SUPPORTED_LABEL}",
        )

    data = await file.read()
    limit = settings.kb_max_file_mb * 1024 * 1024
    if len(data) > limit:
        raise HTTPException(
            413, f"文件超过 {settings.kb_max_file_mb}MB 上限",
        )

    try:
        # pypdf / python-docx 是纯 Python 的 CPU 密集操作，不能占着事件循环
        ext, raw_text = await asyncio.to_thread(parsers.parse, filename, data)
    except parsers.KbParseError as e:
        raise HTTPException(400, str(e))

    text = normalize_text(raw_text)
    if not text:
        raise HTTPException(400, "文件里没有有效的文本内容")

    content_hash = kb_store.hash_text(text)
    duplicate = await kb_store.find_document_by_hash(user["id"], content_hash)
    if duplicate is not None:
        if duplicate.status == "failed":
            # 之前这份内容入库失败（多半是向量服务临时不可用）。
            # 直接把那一条重新排队，比让用户去列表里找"重试"更省事，
            # 也避免为同样的内容再建一行。
            from .kb import service as kb_service

            await kb_service.reindex_document(user["id"], duplicate.id)
            kick_ingest()
            retried = await kb_store.get_document(user["id"], duplicate.id)
            if retried is None:  # pragma: no cover
                raise HTTPException(500, "重新排队失败，请刷新列表")
            return kb_store.doc_to_dict(retried)

        raise HTTPException(409, f"这份内容已经在知识库里了：{duplicate.title}")

    doc_id = await kb_store.create_document(
        user["id"],
        title=filename,
        ext=ext,
        mime=file.content_type or "",
        size_bytes=len(data),
        content_text=text,
        char_count=len(text),
        content_hash=content_hash,
    )
    kick_ingest()

    doc = await kb_store.get_document(user["id"], doc_id)
    if doc is None:  # pragma: no cover - 刚写入就查不到属于异常情况
        raise HTTPException(500, "文档登记后未能读回，请刷新列表确认")
    return kb_store.doc_to_dict(doc)


@app.get("/api/kb/documents")
async def kb_list(user: dict = Depends(current_user)):
    """列出当前用户上传的全部文件。前端按 ext 分组、按 status 驱动交互。"""
    _require_kb()

    from .kb import store as kb_store

    return await kb_store.list_documents(user["id"])


@app.get("/api/kb/documents/{doc_id}")
async def kb_document(doc_id: int, user: dict = Depends(current_user)):
    _require_kb()

    from .kb import store as kb_store

    doc = await kb_store.get_document(user["id"], doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    return kb_store.doc_to_dict(doc)


@app.get("/api/kb/documents/{doc_id}/chunks")
async def kb_document_chunks(
    doc_id: int,
    limit: int = 50,
    user: dict = Depends(current_user),
):
    """查看分块结果，用于调分块参数与排查召回问题。"""
    _require_kb()

    from .kb import store as kb_store

    doc = await kb_store.get_document(user["id"], doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    return await kb_store.list_chunks(doc_id, limit=limit)


@app.delete("/api/kb/documents/{doc_id}")
async def kb_delete(doc_id: int, user: dict = Depends(current_user)):
    """
    删除文件。进行中的文档不允许删除 —— 索引 worker 正在写数据，
    此时删除会撞在它身上。失败的文档放行，否则用户没有出口。
    """
    _require_kb()

    from .kb import service as kb_service
    from .kb import store as kb_store

    doc = await kb_store.get_document(user["id"], doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if doc.status in ("pending", "indexing"):
        raise HTTPException(409, "文件正在加入知识库中，完成后再删除")

    await kb_service.delete_document(user["id"], doc_id)
    return {"ok": True}


@app.post("/api/kb/documents/{doc_id}/reindex")
async def kb_reindex(doc_id: int, user: dict = Depends(current_user)):
    """重试失败的文件：清掉旧索引，重新排队。"""
    _require_kb()

    from .kb import service as kb_service
    from .kb.deps import kick_ingest

    try:
        await kb_service.reindex_document(user["id"], doc_id)
    except ValueError as e:
        raise HTTPException(400, str(e))

    kick_ingest()
    return {"ok": True}


@app.post("/api/kb/search")
async def kb_search(
    req: KbSearchRequest,
    user: dict = Depends(current_user),
):
    """
    检索测试。不经过 LLM，直接看召回结果 ——
    这是把"召回不行"和"模型不会用"两个问题分开定位的关键入口。
    """
    _require_kb()

    from .kb.search import search

    hits = await search(user["id"], req.query, top_k=req.top_k)
    return {
        "query": req.query,
        "count": len(hits),
        "hits": [
            {
                "doc_id": hit["doc_id"],
                "source": hit["title"],
                "chunk_id": hit["chunk_ids"][0],
                "score": round(hit["score"], 6),
                # 原始余弦相似度，供调参用。为 null 表示这条只被关键词通道命中
                "similarity": (
                    round(hit["similarity"], 4)
                    if hit.get("similarity") is not None
                    else None
                ),
                "tokens": hit.get("tokens", 0),
                "text": hit["text"],
            }
            for hit in hits
        ],
    }


@app.get("/api/kb/stats")
async def kb_stats(user: dict = Depends(current_user)):
    """容量统计与向量服务状态。"""
    _require_kb()

    from .kb import service as kb_service

    return await kb_service.get_stats(user["id"])


@app.post("/api/kb/rebuild")
async def kb_rebuild(user: dict = Depends(current_user)):
    """
    用本地已存的向量副本重建 Chroma 索引，不调用 embedding 服务。

    用途：换了向量库目录、Chroma 数据损坏、或者刚配好 embedding
    服务需要把之前缺向量的文档补齐。
    """
    _require_kb()

    from .kb import service as kb_service

    try:
        return await kb_service.rebuild_user_vectors(user["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))


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
    return {"ok": True}
