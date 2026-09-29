"""
工具定义。

用 LangChain 的 @tool 装饰器，Schema 从签名 + docstring 自动生成。
风险的元数据（risk / retryable）单独维护在 TOOL_META 里，
绝不下发到 LLM 上下文，避免模型参与安全判断。
"""

import uuid

from langchain_core.tools import tool
from pydantic import BaseModel, Field


class CreateEventInput(BaseModel):
    """创建日程的输入 Schema（复杂参数用 Pydantic 更清晰）。"""
    title: str = Field(description="日程标题")
    start: str = Field(description="开始时间，ISO8601 带时区")
    end: str = Field(description="结束时间，ISO8601 带时区")
    attendees: list[str] = Field(default_factory=list, description="参与人邮箱列表")
    description: str = Field(default="", description="日程描述")


# ============================================================
# mock 实现（入门先跑通链路，再接真实 API）
# ============================================================

@tool
def list_calendar_events(time_min: str, time_max: str) -> dict:
    """查询指定时间范围内的日历日程。

    Args:
        time_min: 起始时间 ISO8601
        time_max: 结束时间 ISO8601
    """
    return {
        "events": [
            {
                "id": "evt_demo_1",
                "title": "和产品对齐需求",
                "start": "2026-09-24T10:00:00+08:00",
                "end": "2026-09-24T11:00:00+08:00",
            }
        ],
        "time_min": time_min,
        "time_max": time_max,
    }


@tool(args_schema=CreateEventInput)
def create_calendar_event(title, start, end, attendees=None, description=""):
    """创建日历日程。"""
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:8]}",
        "title": title,
        "start": start,
        "end": end,
        "attendees": attendees or [],
        "status": "created",
    }


@tool
def search_email(query: str, max_results: int = 5) -> dict:
    """搜索邮件。

    Args:
        query: 搜索关键词，例如 from:lisi
        max_results: 最多返回条数
    """
    return {
        "messages": [
            {
                "id": "msg_demo_1",
                "from": "lisi@example.com",
                "subject": "关于明天的方案",
                "snippet": "附件是最新版本，麻烦看下…",
            }
        ][:max_results],
        "query": query,
    }


@tool
def send_email(to: str, subject: str, body: str) -> dict:
    """发送邮件。"""
    return {
        "message_id": f"msg_{uuid.uuid4().hex[:8]}",
        "to": to,
        "subject": subject,
        "status": "sent",
    }


@tool
def create_notion_page(database_id: str, title: str, properties: dict | None = None) -> dict:
    """在 Notion 数据库中创建页面。"""
    return {
        "page_id": f"page_{uuid.uuid4().hex[:8]}",
        "title": title,
        "url": f"https://notion.so/page_{uuid.uuid4().hex[:8]}",
        "status": "created",
    }


@tool
def create_reminder(text: str, remind_at: str) -> dict:
    """创建提醒。"""
    return {
        "reminder_id": f"rem_{uuid.uuid4().hex[:8]}",
        "text": text,
        "remind_at": remind_at,
        "status": "scheduled",
    }


@tool
def remember_preference(key: str, value: str) -> dict:
    """记住用户的长期偏好，跨会话生效。

    用户说“以后默认会议 1 小时”“我的时区是上海”时调用。

    Args:
        key: 偏好键，用简洁英文下划线命名，如 default_meeting_duration
        value: 偏好值，如 60 或 Asia/Shanghai
    """
    # 实际写入在 executor 里特判处理（走异步 ORM），
    # 这里只返回占位结果，保持同步 handler 的纯粹性
    return {"key": key, "value": value, "status": "remembered"}


# ============================================================
# 注册表与元数据
# ============================================================

ALL_TOOLS = [
    list_calendar_events,
    create_calendar_event,
    search_email,
    send_email,
    create_notion_page,
    create_reminder,
    remember_preference,
]

# 风险与重试元数据（不进入 LLM 上下文）
# risk=safe 直接执行；risk=confirm 必须先经用户确认
# retryable 决定 executor 是否对失败进行重试
TOOL_META: dict[str, dict] = {
    "list_calendar_events":   {"risk": "safe",    "retryable": True},
    "create_calendar_event":  {"risk": "confirm", "retryable": True},
    "search_email":           {"risk": "safe",    "retryable": True},
    "send_email":             {"risk": "confirm", "retryable": True},
    "create_notion_page":     {"risk": "confirm", "retryable": True},
    "create_reminder":        {"risk": "confirm", "retryable": True},
    "remember_preference":    {"risk": "safe",    "retryable": True},
}

# 名称 → 可调用对象
TOOL_MAP = {t.name: t for t in ALL_TOOLS}


def get_tool_meta(name: str) -> dict:
    """未知工具默认按最危险处理。"""
    return TOOL_META.get(name, {"risk": "confirm", "retryable": False})