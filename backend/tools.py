"""
工具定义。

用 LangChain 的 @tool 装饰器，Schema 从签名 + docstring 自动生成。
风险元数据（risk / retryable）在 TOOL_META 里，不下发给 LLM。

真实 API 调用的工具（日历 / 邮箱 / Notion）函数本身只做参数校验和占位，
真正调用在 executor 里特判执行：
1. 需要异步（AsyncClient / httpx）
2. 需要用户级凭证隔离（user_id 传给 executor 处理）
"""

import uuid

from langchain_core.tools import tool
from pydantic import BaseModel, Field


class CreateEventInput(BaseModel):
    """创建日程的输入 Schema。"""
    title: str = Field(description="日程标题")
    start: str = Field(description="开始时间，ISO8601 带时区")
    end: str = Field(description="结束时间，ISO8601 带时区")
    attendees: list[str] = Field(default_factory=list, description="参与人 ID 列表（飞书 open_id）")
    description: str = Field(default="", description="日程描述")


# ============================================================
# 日历工具（占位，真实调用在 executor）
# ============================================================

@tool
def list_calendar_events(time_min: str, time_max: str) -> dict:
    """查询用户飞书日历中指定时间范围内的日程。

    Args:
        time_min: 起始时间，ISO8601 格式
        time_max: 结束时间，ISO8601 格式
    """
    return {"time_min": time_min, "time_max": time_max, "status": "pending"}


@tool(args_schema=CreateEventInput)
def create_calendar_event(title, start, end, attendees=None, description=""):
    """在用户飞书日历中创建日程。

    Args:
        title: 日程标题
        start: 开始时间，ISO8601 带时区
        end: 结束时间，ISO8601 带时区
        attendees: 参与人列表（飞书 open_id，ou_ 开头）
        description: 日程描述
    """
    return {
        "title": title, "start": start, "end": end,
        "attendees": attendees or [], "description": description,
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
    """通过用户的 QQ 邮箱发送邮件。

    Args:
        to: 收件人邮箱
        subject: 邮件主题
        body: 邮件正文（纯文本）
    """
    if not to or not subject:
        raise ValueError("收件人和主题不能为空")
    return {"to": to, "subject": subject, "body": body, "status": "pending"}


# ============================================================
# 提醒工具（mock）
# ============================================================

@tool
def create_reminder(text: str, remind_at: str) -> dict:
    """创建提醒。

    Args:
        text: 提醒内容
        remind_at: 提醒时间，ISO8601 带时区
    """
    return {
        "reminder_id": f"rem_{uuid.uuid4().hex[:8]}",
        "text": text, "remind_at": remind_at, "status": "scheduled",
    }


# ============================================================
# Notion 工具（占位，真实调用在 executor）
# ============================================================

@tool
def create_notion_page(
    database_id: str = "",
    title: str = "",
    properties: dict | None = None,
) -> dict:
    """在用户自己的 Notion 数据库中创建页面。

    Args:
        database_id: 目标数据库 ID。留空则用用户配置的默认数据库。
        title: 页面标题
        properties: 其他属性（可选），如 {"Status": {"select": {"name": "Todo"}}}
    """
    if not title:
        raise ValueError("标题不能为空")
    return {
        "database_id": database_id,
        "title": title,
        "properties": properties or {},
        "status": "pending",
    }


@tool
def query_notion_database(
    database_id: str = "",
    filter_obj: dict | None = None,
    page_size: int = 10,
) -> dict:
    """查询用户自己的 Notion 数据库。

    Args:
        database_id: 目标数据库 ID。留空则用默认数据库。
        filter_obj: Notion 原生 filter 结构（可选）
        page_size: 最多返回条数
    """
    return {
        "database_id": database_id,
        "filter_obj": filter_obj,
        "page_size": page_size,
        "status": "pending",
    }


@tool
def update_notion_page(page_id: str, properties: dict) -> dict:
    """更新用户自己的 Notion 页面属性。

    Args:
        page_id: 页面 ID
        properties: 要更新的属性（Notion 原生格式）
    """
    return {"page_id": page_id, "properties": properties, "status": "pending"}


@tool
def archive_notion_page(page_id: str) -> dict:
    """归档（软删除）用户自己的 Notion 页面。

    Args:
        page_id: 页面 ID
    """
    return {"page_id": page_id, "status": "pending"}


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
# 注册表与元数据
# ============================================================

ALL_TOOLS = [
    list_calendar_events,
    create_calendar_event,
    search_email,
    send_email,
    create_reminder,
    create_notion_page,
    query_notion_database,
    update_notion_page,
    archive_notion_page,
    remember_preference,
]

TOOL_META: dict[str, dict] = {
    "list_calendar_events":   {"risk": "safe",    "retryable": True},
    "create_calendar_event":  {"risk": "confirm", "retryable": True},
    "search_email":           {"risk": "safe",    "retryable": True},
    "send_email":             {"risk": "confirm", "retryable": True},
    "create_reminder":        {"risk": "confirm", "retryable": True},
    "create_notion_page":     {"risk": "confirm", "retryable": True},
    "query_notion_database":  {"risk": "safe",    "retryable": True},
    "update_notion_page":     {"risk": "confirm", "retryable": True},
    "archive_notion_page":    {"risk": "confirm", "retryable": True},
    "remember_preference":    {"risk": "safe",    "retryable": True},
}

TOOL_MAP = {t.name: t for t in ALL_TOOLS}


def get_tool_meta(name: str) -> dict:
    """未知工具默认按最危险处理。"""
    return TOOL_META.get(name, {"risk": "confirm", "retryable": False})