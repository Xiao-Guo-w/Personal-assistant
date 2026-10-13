"""
时间文案：把"现在几点"讲成模型和用户都不容易弄错的样子。

为什么单独一个模块：
- 只依赖标准库，不引 langchain / sqlalchemy，调度器和图谱两侧都能用
- 本地时间为主、UTC 作对照：只给 UTC 钟点、又要求输出 +08:00 的 ISO8601 时，
  模型很容易把 UTC 的钟点直接贴上 +08:00，时间整整差 8 小时（会算到过去）
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Shanghai"

_WEEKDAYS = "一二三四五六日"


def safe_zone(tz_name: str | None) -> ZoneInfo:
    """取时区；名字非法或为空时退回默认时区（时区名写错不该让功能失败）。"""
    try:
        return ZoneInfo(tz_name or DEFAULT_TZ)
    except Exception:  # noqa: BLE001
        return ZoneInfo(DEFAULT_TZ)


def local_now(tz_name: str | None = None) -> datetime:
    """用户时区的当前时间（带 tzinfo）。"""
    return datetime.now(safe_zone(tz_name))


def zone_label(tz_name: str | None) -> str:
    """文案里该写的时区名：名字非法时写实际生效的那个，避免自相矛盾。"""
    if tz_name and str(safe_zone(tz_name)) == str(tz_name):
        return tz_name
    return DEFAULT_TZ


def offset_text(dt: datetime) -> str:
    """时区偏移的 +08:00 写法（strftime('%z') 给的是 +0800）。"""
    raw = dt.strftime("%z") or "+0000"
    return f"{raw[:3]}:{raw[3:]}"


def weekday_text(dt: datetime) -> str:
    """星期几：星期一是 0，对应"一"。"""
    return _WEEKDAYS[dt.weekday()]


def now_lines(tz_name: str | None = None) -> str:
    """系统提示里的"当前时间"块：本地时间在前，UTC 作对照。"""
    local = local_now(tz_name)
    utc = local.astimezone(timezone.utc)
    return (
        f"本地时间：{local.strftime('%Y-%m-%d %H:%M:%S')}"
        f"（{zone_label(tz_name)}，UTC{offset_text(local)}，星期{weekday_text(local)}）\n"
        f"UTC 时间：{utc.strftime('%Y-%m-%dT%H:%M:%S+00:00')}"
    )


def time_hint(tz_name: str | None = None) -> str:
    """一行紧凑的当前时间，用来贴在用户最新一条消息前面。"""
    local = local_now(tz_name)
    return (
        f"〔当前时间：{local.strftime('%Y-%m-%d %H:%M:%S')}"
        f" 星期{weekday_text(local)}，UTC{offset_text(local)}（{zone_label(tz_name)}）〕"
    )


def now_text(tz_name: str | None = None) -> str:
    """报错文案里的当前时间（本地时间 + 偏移 + 星期）。"""
    local = local_now(tz_name)
    return (
        f"{local.strftime('%Y-%m-%d %H:%M:%S')}"
        f"（{zone_label(tz_name)}，UTC{offset_text(local)}，星期{weekday_text(local)}）"
    )


def iso_example(tz_name: str | None = None) -> str:
    """
    给模型的时间格式示例：用"一小时后的本地时间"。

    示例日期一旦写死（例如 2026-10-02T09:00:00+08:00），模型会照抄那个日期，
    提示词再怎么写"以当前时间为准"也拦不住。
    """
    local = local_now(tz_name) + timedelta(hours=1)
    return local.strftime("%Y-%m-%dT%H:%M:%S") + offset_text(local)
