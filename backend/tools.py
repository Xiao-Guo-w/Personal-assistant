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
#
# 页面模式：不使用数据库，所有新页面都是「默认父页面」下的子页面，
# 因此这里只有 parent_page_id，没有 database_id / properties。
# ============================================================

@tool
def create_notion_page(
    title: str,
    content: str = "",
    parent_page_id: str = "",
) -> dict:
    """在用户自己的 Notion 父页面下创建子页面。

    Args:
        title: 页面标题
        content: 页面正文（纯文本，按行分段，可选）
        parent_page_id: 父页面 ID。留空则用用户设置的默认父页面。
    """
    if not title:
        raise ValueError("标题不能为空")
    return {
        "title": title,
        "content": content,
        "parent_page_id": parent_page_id,
        "status": "pending",
    }


@tool
def list_notion_pages(
    parent_page_id: str = "",
    page_size: int = 10,
) -> dict:
    """列出用户 Notion 父页面下的子页面。

    Args:
        parent_page_id: 父页面 ID。留空则用默认父页面。
        page_size: 最多返回条数
    """
    return {
        "parent_page_id": parent_page_id,
        "page_size": page_size,
        "status": "pending",
    }


@tool
def update_notion_page(
    page_id: str,
    title: str = "",
    append_content: str = "",
) -> dict:
    """修改 Notion 页面的标题，或在页面末尾追加正文。

    Args:
        page_id: 页面 ID
        title: 新标题（可选，留空表示不改标题）
        append_content: 追加到页面末尾的正文（纯文本，可选）
    """
    if not title and not append_content:
        raise ValueError("请提供新标题（title）或要追加的正文（append_content）")
    return {
        "page_id": page_id,
        "title": title,
        "append_content": append_content,
        "status": "pending",
    }


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
    list_notion_pages,
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
    "list_notion_pages":      {"risk": "safe",    "retryable": True},
    "update_notion_page":     {"risk": "confirm", "retryable": True},
    "archive_notion_page":    {"risk": "confirm", "retryable": True},
    "remember_preference":    {"risk": "safe",    "retryable": True},
}

TOOL_MAP = {t.name: t for t in ALL_TOOLS}


def get_tool_meta(name: str) -> dict:
    """未知工具默认按最危险处理。"""
    return TOOL_META.get(name, {"risk": "confirm", "retryable": False})
