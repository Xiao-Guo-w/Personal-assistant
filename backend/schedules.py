"""
日程：系统内自建，不对接任何第三方日历 API。

设计要点：
- 时间口径与提醒完全一致：UTC（naive）入库，展示时按用户时区换算，
  解析走 timeutil（两边共用同一套解析，避免"提醒按本地解释、日程按 UTC 解释"）
- 用户说"下午 3 点开会"但没给时长时，用长期偏好 default_meeting_duration
  补出结束时间，而不是把 end 留空让下游各自去猜
- 通知复用提醒机制：落日程时顺带建一条 kind=reminder、schedule_id 指向本行的
  提醒，到点由提醒调度器通过用户绑定的 QQ 邮箱 + 站内收件箱投递
- 取消是软删除（status=cancelled），同时取消它还没触发的提醒；
  改期会把关联提醒一起挪 —— 否则用户会在错误的时间点收到邮件
"""

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from .db import session_scope
from .models import Reminder, Schedule
from .reminders import row_payload, user_timezone
from .timeutil import (
    TimeParseError, format_local, parse_future_iso, parse_iso, to_iso_utc, utc_now,
)


# 默认提前多少分钟提醒；0 = 到点提醒，负数 = 不提醒
DEFAULT_REMIND_MINUTES = 15
# 没给结束时间、又没有长期偏好时的兜底时长
FALLBACK_DURATION_MINUTES = 60

VALID_STATUSES = ("active", "cancelled", "all")


class ScheduleError(Exception):
    """日程参数错误（重试也不会成功）。"""


# ============================================================
# 内部工具
# ============================================================

def _parse_when(raw: str, tz_name: str, what: str) -> datetime:
    """时间字符串 → UTC（naive），且必须是未来时间。"""
    try:
        return parse_future_iso(raw, tz_name, what=what)
    except TimeParseError as e:
        raise ScheduleError(str(e)) from e


def _parse_bound(raw: str, tz_name: str, what: str) -> datetime:
    """查询区间的边界：允许是过去的时间（"上周有什么安排"）。"""
    try:
        return parse_iso(raw, tz_name, what=what)
    except TimeParseError as e:
        raise ScheduleError(str(e)) from e


async def _default_duration_minutes(user_id: int) -> int:
    """
    用户偏好的默认会议时长。

    读不到或值不合法时退回 FALLBACK_DURATION_MINUTES：偏好是锦上添花，
    不该因为它没设就把建日程这件事整个卡住。
    """
    from .memory import get_user_memories

    try:
        memories = await get_user_memories(user_id)
    except Exception:  # noqa: BLE001 —— 读偏好失败不该阻断建日程
        return FALLBACK_DURATION_MINUTES

    try:
        minutes = int(str(memories.get("default_meeting_duration", "")).strip())
    except (TypeError, ValueError):
        return FALLBACK_DURATION_MINUTES
    return minutes if minutes > 0 else FALLBACK_DURATION_MINUTES


def _parse_schedule_id(value: str) -> int | None:
    """从 "sch_12" / "12" 里取出数字 ID；取不出来返回 None。"""
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return int(digits) if digits else None


def _normalize_advance(value) -> int:
    """提醒提前量：非法值退回默认；夹到 -1 ~ 7 天，避免离谱的定时。"""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_REMIND_MINUTES
    return max(-1, min(minutes, 7 * 24 * 60))


def _reminder_when(start_at: datetime, advance: int) -> datetime | None:
    """
    由"日程开始时间 + 提前量"算出提醒时间。

    返回 None 表示来不及提醒了（算出来已经是过去）：日程照建，只是不挂提醒。
    这比整条请求失败更符合用户预期 —— 用户是在离会议很近的时候才想起来建的。
    """
    if advance < 0:
        return None
    when = start_at - timedelta(minutes=advance)
    if when <= utc_now() - timedelta(seconds=60):
        return None
    return when


def _serialize(row: Schedule, tz_name: str, reminder_id: int | None = None) -> dict:
    """ORM 对象 → 接口/工具用的 dict（本地可读文案 + ISO 双份）。"""
    return {
        "id": row.id,
        "schedule_id": f"sch_{row.id}",
        "title": row.title,
        "start_at": to_iso_utc(row.start_at),
        "start_at_local": format_local(row.start_at, tz_name),
        "end_at": to_iso_utc(row.end_at),
        "end_at_local": format_local(row.end_at, tz_name),
        "location": row.location or "",
        "description": row.description or "",
        "status": row.status,
        "reminder_id": f"rem_{reminder_id}" if reminder_id else "",
        "created_at": to_iso_utc(row.created_at),
    }


async def _pending_reminder_ids(schedule_ids: list[int]) -> dict[int, int]:
    """批量取"每条日程还没触发的提醒 ID"，避免逐条查库。"""
    if not schedule_ids:
        return {}
    async with session_scope() as db:
        rows = (
            await db.execute(
                select(Reminder.schedule_id, Reminder.id).where(
                    Reminder.schedule_id.in_(schedule_ids),
                    Reminder.status == "pending",
                )
            )
        ).all()
    return {schedule_id: reminder_id for schedule_id, reminder_id in rows}


async def _find_schedule(db, user_id: int, schedule_id: str, locate_title: str):
    """
    定位一条有效日程：优先按 ID，其次按标题模糊匹配最近要开始的那条。

    模型常常只记得内容、不知道 ID，所以两种定位方式都要支持。
    """
    numeric = _parse_schedule_id(schedule_id)
    keyword = (locate_title or "").strip()
    if numeric is None and not keyword:
        raise ScheduleError("请提供要操作的日程 ID（sch_12）或日程标题。")

    stmt = select(Schedule).where(
        Schedule.user_id == user_id,
        Schedule.status == "active",
    )
    if numeric is not None:
        stmt = stmt.where(Schedule.id == numeric)
    else:
        stmt = stmt.where(Schedule.title.like(f"%{keyword}%"))
    stmt = stmt.order_by(Schedule.start_at.asc()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


# ============================================================
# 增 / 查 / 改 / 取消
# ============================================================

async def create_schedule(
    user_id: int,
    title: str,
    start: str,
    end: str = "",
    location: str = "",
    description: str = "",
    remind_minutes: int = DEFAULT_REMIND_MINUTES,
) -> dict:
    """创建日程，并按其提前量挂一条邮件提醒。"""
    name = (title or "").strip()
    if not name:
        raise ScheduleError("日程标题不能为空。")
    if len(name) > 200:
        name = name[:200]

    tz_name = await user_timezone(user_id)
    start_at = _parse_when(start, tz_name, "日程开始")

    if (end or "").strip():
        end_at = _parse_when(end, tz_name, "日程结束")
        if end_at <= start_at:
            raise ScheduleError("日程结束时间必须晚于开始时间。")
    else:
        end_at = start_at + timedelta(
            minutes=await _default_duration_minutes(user_id)
        )

    advance = _normalize_advance(remind_minutes)
    remind_at = _reminder_when(start_at, advance)
    place = (location or "").strip()[:200]
    note = (description or "").strip()[:2000]

    if advance < 0:
        reminder_note = ""
    elif remind_at is None:
        reminder_note = "日程时间太近，来不及提前提醒，本次没有设置邮件提醒。"
    else:
        reminder_note = ""

    async with session_scope() as db:
        row = Schedule(
            user_id=user_id,
            title=name,
            start_at=start_at,
            end_at=end_at,
            location=place,
            description=note,
            status="active",
        )
        db.add(row)
        await db.flush()
        schedule_id = row.id

        reminder_id: int | None = None
        if remind_at is not None:
            reminder = Reminder(
                user_id=user_id,
                kind="reminder",
                schedule_id=schedule_id,
                payload_json=json.dumps(
                    {"remind_minutes": advance}, ensure_ascii=False,
                ),
                text=f"日程：{name}",
                remind_at=remind_at,
                status="pending",
            )
            db.add(reminder)
            await db.flush()
            reminder_id = reminder.id

    return {
        "schedule_id": f"sch_{schedule_id}",
        "id": schedule_id,
        "title": name,
        "start_at": to_iso_utc(start_at),
        "start_at_local": format_local(start_at, tz_name),
        "end_at": to_iso_utc(end_at),
        "end_at_local": format_local(end_at, tz_name),
        "location": place,
        "description": note,
        "status": "active",
        "reminder_id": f"rem_{reminder_id}" if reminder_id else "",
        "remind_at_local": format_local(remind_at, tz_name),
        "note": reminder_note,
    }


async def list_schedules(
    user_id: int,
    time_min: str = "",
    time_max: str = "",
    status: str = "active",
    limit: int = 20,
) -> dict:
    """
    列出日程。

    默认只给"从现在往后"的有效日程 —— 用户问"我最近有什么安排"时，
    翻出一堆上周的旧日程只会干扰判断；要查过去就显式给 time_min。
    """
    wanted = (status or "active").strip().lower()
    if wanted not in VALID_STATUSES:
        wanted = "active"
    try:
        size = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        size = 20

    tz_name = await user_timezone(user_id)

    lower = (
        _parse_bound(time_min, tz_name, "查询起始")
        if (time_min or "").strip() else utc_now()
    )
    upper = (
        _parse_bound(time_max, tz_name, "查询结束")
        if (time_max or "").strip() else None
    )

    async with session_scope() as db:
        stmt = select(Schedule).where(Schedule.user_id == user_id)
        if wanted != "all":
            stmt = stmt.where(Schedule.status == wanted)
        stmt = stmt.where(Schedule.start_at >= lower)
        if upper is not None:
            stmt = stmt.where(Schedule.start_at <= upper)
        rows = (
            await db.execute(stmt.order_by(Schedule.start_at.asc()).limit(size))
        ).scalars().all()

    pending = await _pending_reminder_ids([r.id for r in rows])
    items = [_serialize(r, tz_name, pending.get(r.id)) for r in rows]

    return {
        "count": len(items),
        "status": wanted,
        "from_local": format_local(lower, tz_name),
        "to_local": format_local(upper, tz_name),
        "schedules": items,
    }


async def update_schedule(
    user_id: int,
    schedule_id: str = "",
    locate_title: str = "",
    title: str = "",
    start: str = "",
    end: str = "",
    location: str = "",
    description: str = "",
    remind_minutes: int | None = None,
) -> dict:
    """
    修改日程：只改传入的字段，留空表示不动。

    开始时间变了就把关联的待触发提醒一起挪到"新开始时间 - 原提前量"，
    否则邮件会在旧的时间点发出来 —— 那比没有提醒更糟。
    """
    tz_name = await user_timezone(user_id)
    new_title = (title or "").strip()

    async with session_scope() as db:
        row = await _find_schedule(db, user_id, schedule_id, locate_title)
        if row is None:
            return {
                "updated": False,
                "message": "没有找到匹配的有效日程。可先用 list_schedules 查看。",
            }

        if new_title:
            row.title = new_title[:200]

        old_start = row.start_at
        old_end = row.end_at

        moved = False
        if (start or "").strip():
            row.start_at = _parse_when(start, tz_name, "日程开始")
            moved = True
        if (end or "").strip():
            row.end_at = _parse_when(end, tz_name, "日程结束")
        elif moved:
            # 只改了开始时间：结束时间跟着平移，保持原有时长
            duration = (old_end - old_start) if old_end else timedelta(0)
            if duration <= timedelta(0):
                duration = timedelta(minutes=await _default_duration_minutes(user_id))
            row.end_at = row.start_at + duration

        if row.end_at is not None and row.end_at <= row.start_at:
            raise ScheduleError("日程结束时间必须晚于开始时间。")

        if (location or "").strip():
            row.location = location.strip()[:200]
        if (description or "").strip():
            row.description = description.strip()[:2000]

        reminder = (
            await db.execute(
                select(Reminder).where(
                    Reminder.schedule_id == row.id,
                    Reminder.status == "pending",
                ).order_by(Reminder.remind_at.asc()).limit(1)
            )
        ).scalar_one_or_none()

        advance = _current_advance(reminder)
        # 标题改了，待触发提醒的文案也要跟着改，否则侧边栏里还挂着旧标题
        if reminder is not None:
            reminder.text = f"日程：{row.title}"
        if remind_minutes is not None:
            advance = _normalize_advance(remind_minutes)
            moved = True   # 提前量变了，同样要重算提醒时间

        reminder_note = ""
        # 只有真正有待触发提醒时才回传给前端"已设提醒"，
        # 否则刚被取消的那条会被显示成"提醒还在"
        active_reminder_id: int | None = reminder.id if reminder else None
        if moved:
            new_when = _reminder_when(row.start_at, advance)
            if new_when is None:
                if reminder is not None:
                    reminder.status = "cancelled"
                    active_reminder_id = None
                    reminder_note = "原提醒已取消：新的时间太近，来不及提前提醒。"
            else:
                if reminder is None:
                    reminder = Reminder(
                        user_id=user_id,
                        kind="reminder",
                        schedule_id=row.id,
                        text=f"日程：{row.title}",
                        remind_at=new_when,
                        status="pending",
                    )
                    db.add(reminder)
                    await db.flush()
                else:
                    reminder.remind_at = new_when
                active_reminder_id = reminder.id
                reminder.text = f"日程：{row.title}"
                reminder.payload_json = json.dumps(
                    {"remind_minutes": advance}, ensure_ascii=False,
                )
                reminder_note = f"提醒已同步到 {format_local(new_when, tz_name)}。"

        result = _serialize(row, tz_name, active_reminder_id)
        result["reminder_note"] = reminder_note

    result["updated"] = True
    return result


def _current_advance(reminder: Reminder | None) -> int:
    """从关联提醒里取回原来的提前量；取不到就用默认值。"""
    if reminder is None:
        return DEFAULT_REMIND_MINUTES
    try:
        stored = row_payload(reminder).get("remind_minutes")
    except Exception:  # noqa: BLE001
        stored = None
    return _normalize_advance(stored)


async def cancel_schedule(
    user_id: int, schedule_id: str = "", locate_title: str = "",
) -> dict:
    """取消一条日程（软删除），并把它还没触发的提醒一起取消。"""
    tz_name = await user_timezone(user_id)

    async with session_scope() as db:
        row = await _find_schedule(db, user_id, schedule_id, locate_title)
        if row is None:
            return {"cancelled": False, "message": "没有找到匹配的有效日程。"}

        row.status = "cancelled"
        reminders = (
            await db.execute(
                select(Reminder).where(
                    Reminder.schedule_id == row.id,
                    Reminder.status == "pending",
                )
            )
        ).scalars().all()
        for reminder in reminders:
            reminder.status = "cancelled"

        result = _serialize(row, tz_name)
        result["cancelled"] = True
        result["cancelled_reminders"] = len(reminders)

    return result


