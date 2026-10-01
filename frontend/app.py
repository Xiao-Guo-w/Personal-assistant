"""
Streamlit 前端。

页面流：
1. 未登录 → 登录/注册表单
2. 已登录但 onboarded=False → 4 步引导页
3. 已引导 → 主界面（聊天 / 记忆管理 / 集成设置）
"""

import requests
import streamlit as st

API = "http://localhost:8000"

st.set_page_config(page_title="个人事务助理", page_icon="🤖", layout="wide")

# 跨交互状态
for k, v in {
    "token": None,
    "user": None,
    "sessions": [],
    "current_session": None,
    "messages": [],
    "status": "IDLE",
    "view": "chat",                 # chat / memory / settings
    "confirm_delete_session": None,
    "onboarding_step": 0,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ============================================================
# HTTP 封装
# ============================================================

def _headers() -> dict:
    return {"Authorization": f"Bearer {st.session_state.token}"}


def _api_get(path: str):
    return requests.get(f"{API}{path}", headers=_headers(), timeout=10)


def _api_post(path: str, json: dict | None = None):
    return requests.post(f"{API}{path}", json=json or {}, headers=_headers(), timeout=120)


def _api_put(path: str, json: dict | None = None, params: dict | None = None):
    return requests.put(
        f"{API}{path}", json=json or {}, params=params or {},
        headers=_headers(), timeout=10,
    )


def _api_delete(path: str):
    return requests.delete(f"{API}{path}", headers=_headers(), timeout=10)


# ============================================================
# 登录 / 注册页
# ============================================================

def render_auth_page():
    st.title("🤖 个人事务助理")
    tab_login, tab_register = st.tabs(["登录", "注册"])

    with tab_login:
        username = st.text_input("用户名", key="login_user")
        password = st.text_input("密码", type="password", key="login_pwd")
        if st.button("登录", type="primary"):
            r = requests.post(
                f"{API}/api/auth/login",
                json={"username": username, "password": password}, timeout=10,
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
                    "username": new_user, "password": new_pwd,
                    "display_name": display, "timezone": tz,
                }, timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                st.session_state.token = data["token"]
                st.session_state.user = data["user"]
                st.rerun()
            else:
                st.error(f"注册失败：{r.json().get('detail', r.text)}")


# ============================================================
# 引导流程（4 步）
# ============================================================

def _refresh_me():
    r = _api_get("/api/me")
    if r.status_code == 200:
        st.session_state.user = r.json()


def _skip_all_onboarding():
    r = _api_post("/api/onboarding/complete", json={})
    if r.status_code == 200:
        _refresh_me()
        create_new_session()
        st.rerun()


def _finish_onboarding():
    payload = {}
    if "_onboarding_tz" in st.session_state:
        payload["timezone"] = st.session_state.pop("_onboarding_tz")
    if "_onboarding_duration" in st.session_state:
        payload["default_meeting_duration"] = st.session_state.pop("_onboarding_duration")

    r = _api_post("/api/onboarding/complete", json=payload)
    if r.status_code == 200:
        _refresh_me()
        create_new_session()
        st.rerun()
    else:
        st.error(f"完成失败：{r.text}")


def render_onboarding_page():
    """引导流程：欢迎 → 基础偏好 → 集成（OAuth）→ 完成。"""
    step = st.session_state.onboarding_step
    total = 4

    st.progress(step / (total - 1) if total > 1 else 1.0)
    st.caption(f"第 {step + 1} / {total} 步")

    # ---------- Step 0：欢迎 ----------
    if step == 0:
        st.title("👋 欢迎使用个人事务助理")
        st.markdown(
            f"""
            你好，**{st.session_state.user.get('display_name') or st.session_state.user['username']}**！

            我是你的事务助理，可以帮你：

            - 📅 **管理日程**：「帮我约张三明天下午 3 点开会」
            - 📧 **处理邮件**：「查一下我最近关于方案的邮件」
            - ⏰ **设置提醒**：「提醒我今晚 8 点交周报」
            - 📝 **记录 Notion**：「在 Notion 里记一条：今天完成了 X」

            我会在**执行有风险的操作前**（如发邮件、创建日程）先让你确认，
            也会记住你告诉我的长期偏好（如「以后默认会议 1 小时」）。

            接下来用 2 分钟完成基础设置，让体验更顺滑。
            """
        )
        st.write("")
        col1, col2 = st.columns([1, 1])
        if col1.button("开始设置 →", type="primary", use_container_width=True):
            st.session_state.onboarding_step = 1
            st.rerun()
        if col2.button("跳过全部", use_container_width=True):
            _skip_all_onboarding()

    # ---------- Step 1：基础偏好 ----------
    elif step == 1:
        st.title("🌏 基础偏好")
        st.caption("这些偏好会跨会话生效，随时可以在「长期记忆」里修改。")

        tz_options = [
            "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore",
            "America/New_York", "America/Los_Angeles",
            "Europe/London", "Europe/Berlin", "UTC",
        ]
        default_tz = st.session_state.user.get("timezone", "Asia/Shanghai")
        tz_index = tz_options.index(default_tz) if default_tz in tz_options else 0
        selected_tz = st.selectbox("你的时区", tz_options, index=tz_index)

        duration = st.radio(
            "默认会议时长",
            options=[30, 60, 90, 120],
            index=1,
            format_func=lambda x: f"{x} 分钟",
            horizontal=True,
        )
        st.caption("Agent 在你说「约个会」但没提时长时，会默认用这个。")

        st.write("")
        col1, col2, col3 = st.columns([1, 1, 1])
        if col1.button("← 上一步", use_container_width=True):
            st.session_state.onboarding_step = 0
            st.rerun()
        if col2.button("跳过", use_container_width=True):
            st.session_state.onboarding_step = 2
            st.rerun()
        if col3.button("下一步 →", type="primary", use_container_width=True):
            st.session_state["_onboarding_tz"] = selected_tz
            st.session_state["_onboarding_duration"] = duration
            st.session_state.onboarding_step = 2
            st.rerun()

    # ---------- Step 2：集成（OAuth） ----------
    elif step == 2:
        st.title("🔗 连接你的工具（可选）")
        st.caption("点击下方按钮跳转授权页，完成后返回本页。也可以直接跳过。")

        r = _api_get("/api/integrations/status")
        status = r.json() if r.status_code == 200 else {}

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("### 📅 飞书日历")
            fs = status.get("feishu_calendar", {})
            if fs.get("connected"):
                st.success(f"✅ 已连接{('：' + fs.get('name', '')) if fs.get('name') else ''}")
            else:
                _render_connect_button(
                    label="连接飞书日历",
                    authorize_path="/api/oauth/feishu/authorize",
                    key="onboard_feishu",
                )

        with col2:
            st.markdown("### 📝 Notion")
            nt = status.get("notion", {})
            if nt.get("connected"):
                st.success(f"✅ 已连接{('：' + nt.get('workspace_name', '')) if nt.get('workspace_name') else ''}")
            else:
                _render_connect_button(
                    label="连接 Notion",
                    authorize_path="/api/oauth/notion/authorize",
                    key="onboard_notion",
                )

        st.caption("💡 QQ 邮箱可在「集成设置」中配置。")

        st.write("")
        col1, col2, col3 = st.columns([1, 1, 1])
        if col1.button("← 上一步", use_container_width=True):
            st.session_state.onboarding_step = 1
            st.rerun()
        if col2.button("跳过", use_container_width=True):
            st.session_state.onboarding_step = 3
            st.rerun()
        if col3.button("下一步 →", type="primary", use_container_width=True):
            st.session_state.onboarding_step = 3
            st.rerun()

    # ---------- Step 3：完成 ----------
    elif step == 3:
        st.title("🎉 设置完成")
        st.markdown(
            """
            一切就绪！现在你可以：

            - 直接对 Agent 说「帮我约张三明天下午 3 点开会」
            - 或者在侧边栏「⚙️ 集成设置」里继续配置其他集成
            - 在「📖 长期记忆」里查看 Agent 记住的偏好
            """
        )
        st.write("")
        if st.button("进入聊天 →", type="primary", use_container_width=True):
            _finish_onboarding()


def _render_connect_button(label: str, authorize_path: str, key: str):
    """在引导页渲染一个 OAuth 连接按钮。"""
    if st.button(f"🔗 {label}", key=key, use_container_width=True):
        r = _api_get(authorize_path)
        if r.status_code != 200:
            st.error("获取授权链接失败")
            return
        auth_url = r.json().get("authorize_url", "")
        if not auth_url:
            st.error("授权链接为空")
            return
        st.markdown(
            f"""
            <a href="{auth_url}" target="_blank"
               style="display: block; padding: 10px; background: #3b82f6; color: white;
                      text-align: center; text-decoration: none; border-radius: 6px;
                      font-weight: bold; margin-top: 8px;">
                ➡️ 前往授权
            </a>
            """,
            unsafe_allow_html=True,
        )


# ============================================================
# 会话与聊天的辅助函数
# ============================================================

def load_sessions():
    r = _api_get("/api/sessions")
    if r.status_code == 200:
        st.session_state.sessions = r.json()


def create_new_session():
    r = _api_post("/api/sessions")
    if r.status_code == 200:
        sess = r.json()
        st.session_state.current_session = sess["id"]
        st.session_state.messages = []
        st.session_state.status = "IDLE"
        st.session_state.view = "chat"
        load_sessions()


def load_session_messages(session_id: str):
    r = _api_get(f"/api/sessions/{session_id}")
    if r.status_code == 200:
        data = r.json()
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


def delete_session(session_id: str):
    r = _api_delete(f"/api/sessions/{session_id}")
    if r.status_code != 200:
        st.error(f"删除失败：{r.text}")
        return

    if st.session_state.current_session == session_id:
        st.session_state.current_session = None
        st.session_state.messages = []
        st.session_state.status = "IDLE"

    st.session_state.confirm_delete_session = None
    load_sessions()
    st.toast("会话已删除", icon="🗑️")


def send_message(text: str):
    if not st.session_state.current_session:
        create_new_session()

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


# ============================================================
# 视图：聊天
# ============================================================

def render_chat_page():
    st.title("🤖 个人事务助理 Agent")

    if not st.session_state.current_session:
        st.info("请在左侧新建或选择一个会话开始对话。")
        return

    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

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

    prompt = st.chat_input("输入你的指令…")
    if prompt:
        with st.spinner("思考中…"):
            send_message(prompt)
        st.rerun()


# ============================================================
# 视图：长期记忆
# ============================================================

def render_memory_page():
    col_title, col_back = st.columns([5, 1])
    with col_title:
        st.title("📖 长期记忆")
        st.caption("Agent 记住的关于你的偏好，会跨会话生效。")
    with col_back:
        st.write("")
        if st.button("← 返回聊天", use_container_width=True):
            st.session_state.view = "chat"
            st.rerun()

    r = _api_get("/api/memory")
    if r.status_code != 200:
        st.error(f"加载失败：{r.text}")
        return

    memories = r.json()
    if not memories:
        st.info(
            "还没有任何长期记忆。\n\n试试对 Agent 说：\n"
            "- 「以后默认会议 1 小时」\n"
            "- 「我的时区是 Asia/Shanghai」\n"
            "- 「张三的邮箱是 zhangsan@example.com」"
        )
        return

    st.caption(f"共 **{len(memories)}** 条记忆")

    grouped: dict[str, list[dict]] = {}
    for m in memories:
        grouped.setdefault(m["category"], []).append(m)

    category_order = ["时间与日程", "常用联系人", "沟通与邮件", "工具与集成", "其他"]
    ordered_cats = [c for c in category_order if c in grouped]
    ordered_cats += [c for c in grouped if c not in category_order]

    for cat in ordered_cats:
        items = grouped[cat]
        st.subheader(f"{cat}（{len(items)}）")

        for item in items:
            with st.container(border=True):
                cols = st.columns([6, 1, 1])

                with cols[0]:
                    st.markdown(f"#### {item['icon']} {item['label']}")
                    value_text = item["display_value"]
                    if "\n" in value_text:
                        st.markdown(value_text)
                    else:
                        st.markdown(f"**{value_text}**")

                with cols[1]:
                    with st.popover("详情", use_container_width=True):
                        st.text(f"内部键名：{item['key']}")
                        st.text(f"原始值：{item['raw_value']}")
                        st.text(f"更新时间：{item.get('updated_at') or '未知'}")

                with cols[2]:
                    if st.button(
                        "🗑️ 删除", key=f"del_mem_{item['key']}",
                        use_container_width=True, help="删除此条记忆",
                    ):
                        dr = _api_delete(f"/api/memory/{item['key']}")
                        if dr.status_code == 200:
                            st.toast(f"已删除：{item['label']}", icon="🗑️")
                            st.rerun()
                        else:
                            st.error(f"删除失败：{dr.text}")

        st.write("")


# ============================================================
# 视图：集成设置
# ============================================================

def render_settings_page():
    """
    集成设置页。

    飞书日历和 Notion 走 OAuth；
    QQ 邮箱因官方无第三方 OAuth 接口，保留手动配置。
    """
    col_title, col_back = st.columns([5, 1])
    with col_title:
        st.title("⚙️ 集成设置")
        st.caption("点击「连接」完成授权，无需手动查找凭证。")
    with col_back:
        st.write("")
        if st.button("← 返回聊天", use_container_width=True):
            st.session_state.view = "chat"
            st.rerun()

    r = _api_get("/api/integrations/status")
    status_data = r.json() if r.status_code == 200 else {}

    tab_notion, tab_feishu, tab_email = st.tabs(["Notion", "飞书日历", "QQ邮箱"])

    # ============ Notion ============
    with tab_notion:
        _render_oauth_tab(
            provider="notion",
            title="Notion",
            description="连接 Notion 后，Agent 可以帮你读写你的 Notion 页面。",
            status=status_data.get("notion", {}),
            authorize_path="/api/oauth/notion/authorize",
        )

        # Notion 特有：默认数据库选择
        notion_status = status_data.get("notion", {})
        if notion_status.get("connected"):
            st.divider()
            st.subheader("默认数据库")
            st.caption("Agent 未指定数据库时会写入这里。")

            if st.button("🔍 列出所有数据库", use_container_width=True):
                dr = _api_get("/api/integrations/notion/databases")
                if dr.status_code == 200:
                    databases = dr.json().get("databases", [])
                    if not databases:
                        st.warning(
                            "未找到任何 database。请在 Notion 中授权包含 database 的页面。"
                        )
                    else:
                        st.session_state["_notion_databases"] = databases
                else:
                    st.error(f"查询失败：{dr.text}")

            databases = st.session_state.get("_notion_databases", [])
            if databases:
                options = {
                    f"{d['title'] or '(无标题)'} · {d['id'][:8]}": d["id"]
                    for d in databases
                }
                selected = st.selectbox("选择默认数据库", list(options.keys()))
                if st.button("💾 设为默认", type="primary", use_container_width=True):
                    db_id = options[selected]
                    dr = requests.put(
                        f"{API}/api/integrations/notion/default-database",
                        params={"database_id": db_id},
                        headers=_headers(), timeout=10,
                    )
                    if dr.status_code == 200:
                        st.success("已设置")
                        st.rerun()
                    else:
                        st.error(f"设置失败：{dr.text}")

            current_db = notion_status.get("default_database_id", "")
            if current_db:
                st.caption(f"当前默认数据库：`{current_db}`")

    # ============ 飞书日历 ============
    with tab_feishu:
        _render_oauth_tab(
            provider="feishu_calendar",
            title="飞书日历",
            description="连接飞书日历后，Agent 可以帮你查询和创建日程。",
            status=status_data.get("feishu_calendar", {}),
            authorize_path="/api/oauth/feishu/authorize",
        )

    # ============ QQ邮箱（手动） ============
    with tab_email:
        st.caption(
            "QQ 邮箱官方未提供第三方 OAuth 接口，"
            "需手动获取授权码完成配置。"
        )

        with st.expander("📖 如何获取 QQ 邮箱授权码？", expanded=False):
            st.markdown(
                """
                1. 登录 [mail.qq.com](https://mail.qq.com)
                2. 点击「设置」→「账户」
                3. 找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务」
                4. 开启「IMAP/SMTP服务」（需短信验证）
                5. 点击「生成授权码」，获得 16 位授权码
                """
            )

        email_status = status_data.get("email", {})
        if email_status.get("connected"):
            st.success(f"✅ 已配置：{email_status.get('address')}")

        r = _api_get("/api/integrations/email")
        current = r.json() if r.status_code == 200 else None

        address = st.text_input(
            "QQ 邮箱地址",
            value=current.get("address", "") if current else "",
            placeholder="your_qq@qq.com",
        )
        auth_code = st.text_input("授权码", type="password", placeholder="16位授权码")

        c_save, c_del = st.columns(2)
        if c_save.button("💾 保存邮箱配置", type="primary", use_container_width=True):
            if not address or not auth_code:
                st.error("邮箱地址和授权码不能为空")
            else:
                r = _api_put("/api/integrations/email", {
                    "address": address, "auth_code": auth_code,
                })
                if r.status_code == 200:
                    st.success("已保存")
                    st.rerun()
                else:
                    st.error(f"保存失败：{r.text}")

        if current and current.get("configured"):
            if c_del.button("🗑️ 删除邮箱配置", use_container_width=True):
                _api_delete("/api/integrations/email")
                st.rerun()


def _render_oauth_tab(
    provider: str, title: str, description: str,
    status: dict, authorize_path: str,
):
    """OAuth 集成 Tab 的通用渲染逻辑。"""
    st.caption(description)

    if status.get("connected"):
        info = status.get("name") or status.get("workspace_name") or ""
        st.success(f"✅ 已连接{('：' + info) if info else ''}")

        if st.button(f"🔌 断开 {title} 连接", use_container_width=True):
            r = _api_delete(f"/api/integrations/{provider}")
            if r.status_code == 200:
                st.toast(f"已断开 {title}", icon="🔌")
                st.rerun()
            else:
                st.error(f"断开失败：{r.text}")
    else:
        st.info(f"尚未连接 {title}")

        if st.button(f"🔗 连接 {title}", type="primary", use_container_width=True):
            r = _api_get(authorize_path)
            if r.status_code != 200:
                st.error(f"获取授权链接失败：{r.text}")
                return

            auth_url = r.json().get("authorize_url", "")
            if not auth_url:
                st.error("授权链接为空")
                return

            st.markdown(
                f"""
                <div style="padding: 16px; background: #f0f9ff; border-radius: 8px;
                            border-left: 4px solid #3b82f6; margin: 16px 0;">
                    <p style="margin: 0 0 12px 0; color: #1e40af; font-weight: bold;">
                        📋 授权链接已生成
                    </p>
                    <p style="margin: 0 0 12px 0; color: #666; font-size: 14px;">
                        点击下方按钮跳转授权页面，完成后返回本页刷新查看状态。
                    </p>
                    <a href="{auth_url}" target="_blank"
                       style="display: inline-block; padding: 10px 24px; background: #3b82f6;
                              color: white; text-decoration: none; border-radius: 6px;
                              font-weight: bold;">
                        ➡️ 前往 {title} 授权
                    </a>
                </div>
                """,
                unsafe_allow_html=True,
            )

            st.caption(
                "💡 提示：飞书授权页支持扫码登录，"
                "可以用飞书 App 扫码完成授权。"
            )

            if st.button("🔄 我已授权，刷新状态", use_container_width=True):
                st.rerun()


# ============================================================
# 主入口路由
# ============================================================

# 1. 未登录
if st.session_state.token is None:
    render_auth_page()
    st.stop()

# 2. 已登录但未完成引导
if not st.session_state.user.get("onboarded", False):
    render_onboarding_page()
    st.stop()

# 3. 首次加载会话
if not st.session_state.sessions:
    load_sessions()


# ============================================================
# 侧边栏
# ============================================================

with st.sidebar:
    u = st.session_state.user
    st.markdown(f"**👤 {u.get('display_name') or u.get('username')}**")
    st.caption(f"@{u.get('username')} · {u.get('timezone')}")

    # 登出
    if st.button("登出", use_container_width=True):
        _api_post("/api/auth/logout")
        for k in ["token", "user", "sessions", "current_session",
                  "messages", "status", "view", "confirm_delete_session",
                  "onboarding_step"]:
            if k in ("token", "user", "current_session", "confirm_delete_session"):
                st.session_state[k] = None
            elif k in ("sessions", "messages"):
                st.session_state[k] = []
            elif k == "view":
                st.session_state[k] = "chat"
            elif k == "onboarding_step":
                st.session_state[k] = 0
            else:
                st.session_state[k] = "IDLE"
        st.session_state.pop("_onboarding_tz", None)
        st.session_state.pop("_onboarding_duration", None)
        st.rerun()

    st.divider()
    st.subheader("会话")

    if st.button("🆕 新建会话", use_container_width=True, type="primary"):
        create_new_session()
        st.rerun()

    # 会话列表（带删除 + 二次确认）
    for s in st.session_state.sessions:
        is_current = s["id"] == st.session_state.current_session
        is_confirming = st.session_state.confirm_delete_session == s["id"]

        col_title, col_del = st.columns([5, 1])

        with col_title:
            label = f"{'▶ ' if is_current else ''}{s['title']}"
            if st.button(label, key=f"open_{s['id']}", use_container_width=True):
                st.session_state.current_session = s["id"]
                st.session_state.view = "chat"
                load_session_messages(s["id"])
                st.rerun()

        with col_del:
            if not is_confirming:
                if st.button(
                    "🗑️", key=f"del_sess_{s['id']}",
                    use_container_width=True, help="删除此会话",
                ):
                    st.session_state.confirm_delete_session = s["id"]
                    st.rerun()

        if is_confirming:
            with st.container(border=True):
                st.warning(f"确定删除「{s['title']}」？不可撤销。")
                c_yes, c_no = st.columns(2)
                if c_yes.button("确认", key=f"yes_{s['id']}",
                                type="primary", use_container_width=True):
                    delete_session(s["id"])
                    st.rerun()
                if c_no.button("取消", key=f"no_{s['id']}",
                               use_container_width=True):
                    st.session_state.confirm_delete_session = None
                    st.rerun()

    st.divider()
    st.subheader("个人")

    if st.button("📖 长期记忆", use_container_width=True):
        st.session_state.view = "memory"
        st.rerun()

    if st.button("⚙️ 集成设置", use_container_width=True):
        st.session_state.view = "settings"
        st.rerun()

    if st.button("🎬 重新引导", use_container_width=True):
        st.session_state.onboarding_step = 0
        st.session_state.view = "chat"
        st.session_state.user["onboarded"] = False
        st.rerun()


# ============================================================
# 主区域路由
# ============================================================

if st.session_state.view == "memory":
    render_memory_page()
elif st.session_state.view == "settings":
    render_settings_page()
else:
    render_chat_page()