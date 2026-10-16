"""
时间文案：把"现在几点"讲成模型和用户都不容易弄错的样子。

为什么单独一个模块：
- 只依赖标准库，不引 langchain / sqlalchemy，调度器和图谱两侧都能用
- 本地时间为主、UTC 作对照：只给 UTC 钟点、又要求输出 +08:00 的 ISO8601 时，
  模型很容易把 UTC 的钟点直接贴上 +08:00，时间整整差 8 小时（会算到过去）
- 解析与格式化也放在这里：日程与提醒必须共用同一套口径，
  否则"提醒按本地时间解释、日程按 UTC 解释"这类错位迟早会发生
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


# ============================================================
# 解析与格式化（日程 / 提醒共用）
# ============================================================

class TimeParseError(ValueError):
    """时间字符串无法解析或落在过去。调用方各自包装成自己的业务异常。"""


def utc_now() -> datetime:
    """当前 UTC（naive），与库里存储时间戳的口径一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_iso(
    raw: str, tz_name: str | None = DEFAULT_TZ, what: str = "提醒",
) -> datetime:
    """
    把时间字符串解析成 UTC（naive），不做"过去 / 未来"判断。

    - 带时区（2026-10-02T10:00:00+08:00 / ...Z）→ 直接换算
    - 不带时区（2026-10-02T10:00:00）→ 按用户时区解释

    what：报错文案里的主语（"提醒" / "日程" / "邮件发送" / "查询区间"）。
    查询区间这类用途允许过去的时间（"上周有什么安排"），
    所以"必须是未来"的检查放在下面的 parse_future_iso 里。
    """
    text = (raw or "").strip()
    if not text:
        raise TimeParseError(
            f"缺少{what}时间：请给出 ISO8601 格式的时间并带时区偏移，"
            f"例如 {iso_example(tz_name)}。"
        )

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise TimeParseError(
            f"无法识别的时间格式「{raw}」：请用 ISO8601 并带时区偏移，"
            f"例如 {iso_example(tz_name)}。"
        ) from e

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=safe_zone(tz_name))

    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_future_iso(
    raw: str, tz_name: str | None = DEFAULT_TZ, what: str = "提醒",
) -> datetime:
    """
    同 parse_iso，但额外要求时间必须在未来。

    报错里一律带上"现在是几点"：老会话里模型容易把"现在"算到对话当初那天，
    只回一句"时间已经过去了"，它下一次还是照着错的日期算；把当前时间写进去，
    它才有可能自己纠正过来。
    """
    utc = parse_iso(raw, tz_name, what)

    # 给 60 秒宽限：模型偶尔会把"现在"算得差几秒
    if utc < utc_now() - timedelta(seconds=60):
        raise TimeParseError(
            f"{what}时间已经过去了：算出来的是 {to_iso_utc(utc)}，"
            f"而现在是 {now_text(tz_name)}。"
            "请以现在为准重新给一个未来的时间。"
        )
    return utc


def to_iso_utc(dt: datetime | None) -> str:
    """库里的 naive UTC → 带时区的 ISO8601（给接口 / 前端用）。"""
    if dt is None:
        return ""
    return dt.replace(tzinfo=timezone.utc).isoformat()


def format_local(dt_utc: datetime | None, tz_name: str | None = DEFAULT_TZ) -> str:
    """UTC（naive）→ 用户本地时间的可读文案。"""
    if dt_utc is None:
        return ""
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(
        safe_zone(tz_name)
    ).strftime("%Y-%m-%d %H:%M")


def format_local_full(dt_utc: datetime | None, tz_name: str | None = DEFAULT_TZ) -> str:
    """带星期的本地时间文案（确认文案与邮件正文里更好读）。"""
    if dt_utc is None:
        return ""
    local = dt_utc.replace(tzinfo=timezone.utc).astimezone(safe_zone(tz_name))
    return f"{local.strftime('%Y-%m-%d %H:%M')}（星期{weekday_text(local)}）"
