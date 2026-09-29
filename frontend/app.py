"""
Streamlit 前端（多用户版）。

页面流：
1. 未登录 → 登录/注册表单
2. 已登录 → 左侧会话列表，右侧聊天窗口

所有状态管理都在后端，前端只做展示和交互。
"""

import requests
import streamlit as st

API = "http://localhost:8000"

st.set_page_config(page_title="个人事务助理", page_icon="🤖", layout="wide")

# Streamlit 每次交互都重跑整个脚本，
# 跨交互的状态要放在 session_state 里
for k, v in {
    "token": None,            # 登录 token
    "user": None,             # 当前用户信息
    "sessions": [],           # 会话列表
    "current_session": None,  # 当前会话 ID
    "messages": [],           # 当前会话消息
    "status": "IDLE",         # 状态机状态
}.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ============================================================
# HTTP 封装
# ============================================================

def _headers() -> dict:
    """所有带鉴权的请求都要带 Authorization 头。"""
    return {"Authorization": f"Bearer {st.session_state.token}"}


def _api_get(path: str):
    return requests.get(f"{API}{path}", headers=_headers(), timeout=10)


def _api_post(path: str, json: dict | None = None):
    return requests.post(f"{API}{path}", json=json or {}, headers=_headers(), timeout=120)


# ============================================================
# 登录 / 注册页
# ============================================================

def render_auth_page():
    """未登录时的页面：登录 + 注册两个 Tab。"""
    st.title("🤖 个人事务助理")
    tab_login, tab_register = st.tabs(["登录", "注册"])

    with tab_login:
        username = st.text_input("用户名", key="login_user")
        password = st.text_input("密码", type="password", key="login_pwd")
        if st.button("登录", type="primary"):
            r = requests.post(
                f"{API}/api/auth/login",
                json={"username": username, "password": password},
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                st.session_state.token = data["token"]
                st.session_state.user = data["user"]
                st.rerun()
            else:
                st.error(f"登录失败：{r.json().get('detail', r.text)}")

    with tab_register:
        new_user = st.text_input("用户名", key="reg_user")
        new_pwd = st.text_input("密码", type="password", key="reg_pwd")
        display = st.text_input("显示名（可选）", key="reg_display")
        tz = st.text_input("时区", value="Asia/Shanghai", key="reg_tz")
        if st.button("注册", type="primary"):
            r = requests.post(
                f"{API}/api/auth/register",
                json={
                    "username": new_user,
                    "password": new_pwd,
                    "display_name": display,
                    "timezone": tz,
                },
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                st.session_state.token = data["token"]
                st.session_state.user = data["user"]
                st.rerun()
            else:
                st.error(f"注册失败：{r.json().get('detail', r.text)}")


# 未登录 → 只显示认证页并停止后续渲染
if st.session_state.token is None:
    render_auth_page()
    st.stop()


# ============================================================
# 已登录：会话管理
# ============================================================

def load_sessions():
    """拉取当前用户所有会话。"""
    r = _api_get("/api/sessions")
    if r.status_code == 200:
        st.session_state.sessions = r.json()


def create_new_session():
    """新建会话并切换过去。"""
    r = _api_post("/api/sessions")
    if r.status_code == 200:
        sess = r.json()
        st.session_state.current_session = sess["id"]
        st.session_state.messages = []
        st.session_state.status = "IDLE"
        load_sessions()


def load_session_messages(session_id: str):
    """加载历史会话的消息列表。"""
    r = _api_get(f"/api/sessions/{session_id}")
    if r.status_code == 200:
        data = r.json()
        # 只渲染 user / assistant 消息，忽略 tool / system
        st.session_state.messages = [
            {"role": m["role"], "content": m["content"]}
            for m in data.get("messages", [])
            if m["role"] in ("user", "assistant")
        ]
        stage = data.get("stage") or "IDLE"
        st.session_state.status = (
            "WAITING_CONFIRMATION" if stage == "WAITING_CONFIRMATION" else "IDLE"
        )
    else:
        st.session_state.messages = []
        st.session_state.status = "IDLE"


def send_message(text: str):
    """发消息给后端并追加到本地消息列表。"""
    if not st.session_state.current_session:
        create_new_session()

    # 先把用户消息上屏，界面立即响应
    st.session_state.messages.append({"role": "user", "content": text})
    r = _api_post("/api/chat", json={
        "session_id": st.session_state.current_session,
        "message": text,
    })
    if r.status_code != 200:
        reply = f"请求失败：{r.status_code} {r.text}"
        status = "ERROR"
    else:
        data = r.json()
        reply = data.get("reply", "")
        status = data.get("status", "DONE")
    st.session_state.messages.append({"role": "assistant", "content": reply})
    st.session_state.status = status


# 首次加载会话列表
if not st.session_state.sessions:
    load_sessions()


# ============================================================
# 侧边栏：用户信息 + 会话列表
# ============================================================

with st.sidebar:
    u = st.session_state.user
    st.markdown(f"**👤 {u.get('display_name') or u.get('username')}**")
    st.caption(f"@{u.get('username')} · {u.get('timezone')}")

    # 登出：清空所有前端状态
    if st.button("登出", use_container_width=True):
        _api_post("/api/auth/logout")
        for k in ["token", "user", "sessions", "current_session", "messages", "status"]:
            st.session_state[k] = None if k in ("token", "user", "current_session") else (
                [] if k in ("sessions", "messages") else "IDLE"
            )
        st.rerun()

    st.divider()
    st.subheader("会话")

    if st.button("🆕 新建会话", use_container_width=True, type="primary"):
        create_new_session()
        st.rerun()

    # 会话列表
    for s in st.session_state.sessions:
        is_current = s["id"] == st.session_state.current_session
        label = f"{'▶ ' if is_current else ''}{s['title']}"
        if st.button(label, key=s["id"], use_container_width=True):
            st.session_state.current_session = s["id"]
            load_session_messages(s["id"])
            st.rerun()

    st.divider()
    # 查看当前用户的长期记忆
    if st.button("📖 查看长期记忆", use_container_width=True):
        r = _api_get("/api/memory")
        if r.status_code == 200:
            mem = r.json()
            st.json(mem) if mem else st.caption("（暂无）")


# ============================================================
# 主区域：聊天窗口
# ============================================================

st.title("🤖 个人事务助理 Agent")

if not st.session_state.current_session:
    st.info("请在左侧新建或选择一个会话开始对话。")
    st.stop()

# 渲染历史消息
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])

# 等待确认时显示按钮
if st.session_state.status == "WAITING_CONFIRMATION":
    c1, c2 = st.columns(2)
    if c1.button("✅ 确认", use_container_width=True, type="primary"):
        with st.spinner("执行中…"):
            send_message("确认")
        st.rerun()
    if c2.button("❌ 取消", use_container_width=True):
        with st.spinner("处理中…"):
            send_message("取消")
        st.rerun()

# 底部输入框
prompt = st.chat_input("输入你的指令…")
if prompt:
    with st.spinner("思考中…"):
        send_message(prompt)
    st.rerun()