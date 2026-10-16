"""
工具定义。

用 LangChain 的 @tool 装饰器，Schema 从签名 + docstring 自动生成。
风险元数据（risk / retryable）在 TOOL_META 里，不下发给 LLM。

需要真实副作用的工具（日程 / 提醒 / 邮箱）函数本身只做参数校验和占位，
真正执行在 executor 里特判处理：
1. 需要异步（AsyncClient / httpx）
2. 需要用户级凭证隔离（user_id 传给 executor 处理）

日程不再对接第三方日历 API：它落在系统自己的库里，通知通过用户绑定的
QQ 邮箱发出（见 backend/schedules.py 与 backend/reminders.py）。
"""

from langchain_core.tools import tool

from .config import settings


# ============================================================
# 日程工具（真实落库 + 关联提醒，实现在 backend/schedules.py）
# ============================================================

@tool
def create_schedule(
    title: str,
    start: str,
    end: str = "",
    location: str = "",
    description: str = "",
    remind_minutes: int = 15,
) -> dict:
    """在系统内创建日程，并按提前量设置一封邮件提醒。

    日程保存在系统自己的数据库里，不写任何第三方日历；到点会通过用户绑定的
    QQ 邮箱发提醒邮件，同时在应用内通知。

    Args:
        title: 日程标题，例如"和张三开周会"
        start: 开始时间，ISO8601 格式且必须带时区偏移，形如 YYYY-MM-DDTHH:MM:SS+08:00。
               用户说"明天下午 3 点"这类相对时间以系统提示里的当前时间为基准换算，
               不要沿用对话历史里的日期。
        end: 结束时间，同上格式且必须晚于开始时间；留空时按用户的默认会议时长自动补算
        location: 地点，可选
        description: 备注，可选
        remind_minutes: 提前多少分钟发邮件提醒。默认 15（提前 15 分钟）；
                        0 表示到点提醒；负数表示不需要提醒。
    """
    if not title or not start:
        raise ValueError("日程标题和开始时间不能为空")
    return {
        "title": title, "start": start, "end": end,
        "location": location, "description": description,
        "remind_minutes": remind_minutes, "status": "pending",
    }


@tool
def list_schedules(
    time_min: str = "",
    time_max: str = "",
    status: str = "active",
    limit: int = 20,
) -> dict:
    """查询用户的日程列表。

    Args:
        time_min: 查询起始时间，ISO8601 带时区偏移；留空表示从现在开始
        time_max: 查询结束时间，ISO8601 带时区偏移；留空表示不限
        status: active（有效，默认）/ cancelled（已取消）/ all（全部）
        limit: 最多返回条数
    """
    return {
        "time_min": time_min, "time_max": time_max,
        "status": status, "limit": limit,
    }


@tool
def update_schedule(
    schedule_id: str = "",
    locate_title: str = "",
    title: str = "",
    start: str = "",
    end: str = "",
    location: str = "",
    description: str = "",
    remind_minutes: int | None = None,
) -> dict:
    """修改一条已存在的日程（改期 / 改标题 / 改地点等）。

    只改传入的字段，留空的字段保持原样。改了开始时间时，关联的邮件提醒会一起
    挪到新的时间点。

    Args:
        schedule_id: 日程 ID，形如 sch_12（可先用 list_schedules 查出来）
        locate_title: 或者用现有标题定位，例如"周会"（模糊匹配最近要开始的那条）
        title: 新标题，留空表示不改
        start: 新开始时间，ISO8601 带时区偏移，留空表示不改
        end: 新结束时间，ISO8601 带时区偏移，留空表示不改
        location: 新地点，留空表示不改
        description: 新备注，留空表示不改
        remind_minutes: 新的提前提醒分钟数（0 = 到点提醒，负数 = 不提醒）；
                        留空表示保持原来的提前量
    """
    if not schedule_id and not locate_title:
        raise ValueError("请提供要修改的日程 ID（sch_12）或日程标题")
    return {
        "schedule_id": schedule_id, "locate_title": locate_title,
        "title": title, "start": start, "end": end,
        "location": location, "description": description,
        "remind_minutes": remind_minutes, "status": "pending",
    }


@tool
def cancel_schedule(schedule_id: str = "", locate_title: str = "") -> dict:
    """取消一条日程（同时取消它还没发出的提醒）。

    Args:
        schedule_id: 日程 ID，形如 sch_12（可先用 list_schedules 查出来）
        locate_title: 或者用日程标题定位，例如"周会"（模糊匹配最近要开始的那条）
    """
    if not schedule_id and not locate_title:
        raise ValueError("请提供要取消的日程 ID（sch_12）或日程标题")
    return {
        "schedule_id": schedule_id, "locate_title": locate_title,
        "status": "pending",
    }


# ============================================================
# 邮箱工具（占位，真实调用在 executor）
# ============================================================

@tool
def search_email(query: str, max_results: int = 5) -> dict:
    """搜索用户 QQ 邮箱中的邮件。

    Args:
        query: 搜索关键词（主题模糊匹配）或 from:xxx（按发件人）
        max_results: 最多返回条数
    """
    return {"query": query, "max_results": max_results, "status": "pending"}


@tool
def send_email(to: str, subject: str, body: str) -> dict:
    """通过用户的 QQ 邮箱立即发送邮件。如果要"到某个时间再发"，改用 schedule_email。

    Args:
        to: 收件人邮箱
        subject: 邮件主题
        body: 邮件正文（纯文本）
    """
    if not to or not subject:
        raise ValueError("收件人和主题不能为空")
    return {"to": to, "subject": subject, "body": body, "status": "pending"}


@tool
def schedule_email(to: str, subject: str, body: str, send_at: str) -> dict:
    """定时发送邮件：到点后系统自动把邮件发给收件人。

    Args:
        to: 收件人邮箱
        subject: 邮件主题
        body: 邮件正文（纯文本）
        send_at: 发送时间，ISO8601 格式且必须带时区偏移，形如 YYYY-MM-DDTHH:MM:SS+08:00。
                 "明天早上 9 点"这类相对时间以系统提示里的当前时间为基准换算，
                 不要沿用对话历史里的日期。
    """
    if not to or not subject:
        raise ValueError("收件人和主题不能为空")
    return {
        "to": to, "subject": subject, "body": body, "send_at": send_at,
        "status": "pending",
    }


# ============================================================
# 提醒工具（真实落库 + 后台调度投递）
#
# 真实实现见 backend/reminders.py：落库为 pending，
# 由后台调度循环到点后通过用户绑定的 QQ 邮箱发提醒，并写站内收件箱。
# ============================================================

@tool
def create_reminder(text: str, remind_at: str) -> dict:
    """创建定时提醒。到点后会给用户绑定的 QQ 邮箱发提醒邮件，并在应用内通知。

    Args:
        text: 提醒内容，例如"交周报"
        remind_at: 提醒时间，ISO8601 格式且必须带时区偏移，形如 YYYY-MM-DDTHH:MM:SS+08:00。
                   用户说"今晚 8 点"这类相对时间以系统提示里的当前时间为基准换算，
                   不要沿用对话历史里的日期。
    """
    if not text:
        raise ValueError("提醒内容不能为空")
    return {"text": text, "remind_at": remind_at, "status": "pending"}


@tool
def list_reminders(status: str = "pending", limit: int = 10) -> dict:
    """查询用户的提醒列表。

    Args:
        status: pending（待触发，默认）/ fired（已触发）/ cancelled（已取消）/ all（全部）
        limit: 最多返回条数
    """
    return {"status": status, "limit": limit}


@tool
def cancel_reminder(reminder_id: str = "", text: str = "") -> dict:
    """取消一个还没触发的提醒。

    Args:
        reminder_id: 提醒 ID，形如 rem_12（可先用 list_reminders 查出来）
        text: 或者用提醒内容定位，例如"去钓鱼"（模糊匹配最近要触发的那条）
    """
    if not reminder_id and not text:
        raise ValueError("请提供要取消的提醒 ID（rem_12）或提醒内容")
    return {"reminder_id": reminder_id, "text": text, "status": "pending"}


# ============================================================
# 记忆工具
# ============================================================

@tool
def remember_preference(key: str, value: str) -> dict:
    """记住用户的长期偏好，跨会话生效。

    用户说“以后默认会议 1 小时”“我的时区是上海”时调用。

    Args:
        key: 偏好键，用简洁英文下划线命名，如 default_meeting_duration
        value: 偏好值，如 60 或 Asia/Shanghai
    """
    # 实际写入在 executor 里特判处理（走异步 ORM）
    return {"key": key, "value": value, "status": "remembered"}


# ============================================================
# 知识库检索（真实实现见 backend/kb/search.py）
# ============================================================

@tool
def search_knowledge_base(query: str, top_k: int = 5) -> dict:
    """在用户上传的知识库文件中检索相关片段。

    当问题可能和用户上传过的文件（产品文档、合同、笔记等）有关时，
    先用这个工具查，再基于查到的原文回答。查不到就如实说没找到。

    Args:
        query: 检索语句。写清楚要查什么，不要把整个问题原样丢进来
        top_k: 最多返回几个片段，默认 5
    """
    if not query.strip():
        raise ValueError("检索语句不能为空")
    return {"query": query, "top_k": top_k, "status": "pending"}


# ============================================================
# 注册表与元数据
# ============================================================

ALL_TOOLS = [
    create_schedule,
    list_schedules,
    update_schedule,
    cancel_schedule,
    search_email,
    send_email,
    schedule_email,
    create_reminder,
    list_reminders,
    cancel_reminder,
    remember_preference,
]

# 知识库关闭时不把检索工具下发给模型：模型看不见就不会去调，
# 比"模型调了再报错"干净
if settings.kb_enabled:
    ALL_TOOLS.append(search_knowledge_base)

TOOL_META: dict[str, dict] = {
    "create_schedule":        {"risk": "confirm", "retryable": True},
    "list_schedules":         {"risk": "safe",    "retryable": True},
    "update_schedule":        {"risk": "confirm", "retryable": True},
    "cancel_schedule":        {"risk": "confirm", "retryable": True},
    "search_email":           {"risk": "safe",    "retryable": True},
    "send_email":             {"risk": "confirm", "retryable": True},
    "schedule_email":         {"risk": "confirm", "retryable": True},
    "create_reminder":        {"risk": "confirm", "retryable": True},
    "list_reminders":         {"risk": "safe",    "retryable": True},
    "cancel_reminder":        {"risk": "confirm", "retryable": True},
    "remember_preference":    {"risk": "safe",    "retryable": True},
    "search_knowledge_base":  {"risk": "safe",    "retryable": True},
}

TOOL_MAP = {t.name: t for t in ALL_TOOLS}


def get_tool_meta(name: str) -> dict:
    """未知工具默认按最危险处理。"""
    return TOOL_META.get(name, {"risk": "confirm", "retryable": False})
