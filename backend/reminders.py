"""
提醒：解析 → 落库 → 调度 → 投递。

设计要点：
- 时间统一按 UTC（naive）入库（和 models 里 created_at 的口径一致），
  展示/发信时再按用户时区换算；解析时兼容"带时区"和"不带时区按用户时区解释"
- 投递前用一条原子 UPDATE 抢占（pending → firing），只有抢到的进程负责投递，
  多进程或重启都不会重复发
- 投递渠道：邮件（主）+ 站内收件箱（记录本身即为通知，不依赖外部服务）
- 邮件失败不影响站内：错误写进 last_error，前端能看到"邮件投递失败"
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from .config import settings
from .db import session_scope
from .models import Reminder, User


logger = logging.getLogger("assistant.reminders")

DEFAULT_TZ = "Asia/Shanghai"

# 查询用的状态白名单（all 表示不过滤）；failed 专指"该发出去但没发成功"
VALID_STATUSES = ("pending", "fired", "failed", "cancelled", "all")


class ReminderError(Exception):
    """提醒参数错误（重试也不会成功）。"""


# ============================================================
# 时间处理
# ============================================================

def _now_utc() -> datetime:
    """当前 UTC（naive），与库里存储口径一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _zone(tz_name: str) -> ZoneInfo:
    """安全地取时区，名称非法时退回默认时区。"""
    try:
        return ZoneInfo(tz_name or DEFAULT_TZ)
    except Exception:  # noqa: BLE001 —— 时区名写错不该让提醒创建失败
        return ZoneInfo(DEFAULT_TZ)


def parse_remind_at(raw: str, tz_name: str = DEFAULT_TZ) -> datetime:
    """
    把时间字符串解析成 UTC（naive）。

    - 带时区（2026-10-02T10:00:00+08:00 / ...Z）→ 直接换算
    - 不带时区（2026-10-02T10:00:00）→ 按用户时区解释
    - 明显过去的时间 → 报错（提醒设在过去没有意义）
    """
    text = (raw or "").strip()
    if not text:
        raise ReminderError(
            "缺少提醒/发送时间，请给出具体时间（例如 2026-10-02T10:00:00+08:00）。"
        )

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise ReminderError(
            f"无法识别的时间格式「{raw}」，请用 ISO8601，"
            "例如 2026-10-02T10:00:00+08:00。"
        ) from e

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz_name))

    utc = dt.astimezone(timezone.utc).replace(tzinfo=None)

    # 给 60 秒宽限：模型偶尔会把"现在"算得差几秒
    if utc < _now_utc() - timedelta(seconds=60):
        raise ReminderError("提醒时间已经过去了，请给一个未来的时间。")
    return utc


def to_iso_utc(dt: datetime | None) -> str:
    """库里的 naive UTC → 带时区的 ISO8601（给接口/前端用）。"""
    if dt is None:
        return ""
    return dt.replace(tzinfo=timezone.utc).isoformat()


def format_local(dt_utc: datetime | None, tz_name: str) -> str:
    """UTC（naive）→ 用户本地时间的可读文案。"""
    if dt_utc is None:
        return ""
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(_zone(tz_name)).strftime(
        "%Y-%m-%d %H:%M"
    )


def _parse_reminder_id(value: str) -> int | None:
    """
    从 "rem_12" / "12" 这类写法里取出数字 ID。

    取不出来返回 None（例如模型把提醒内容当 ID 传了），由调用方决定怎么兜底。
    """
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return int(digits) if digits else None


async def _user_timezone(user_id: int) -> str:
    """读用户时区；查不到就用默认值。"""
    async with session_scope() as db:
        tz = (
            await db.execute(select(User.timezone).where(User.id == user_id))
        ).scalar_one_or_none()
    return tz or DEFAULT_TZ


def _row_payload(row: Reminder) -> dict:
    """
    解析 payload_json（只有定时邮件用）。

    解析失败按空处理，不让一条脏数据把列表接口整个搞崩。
    """
    if (row.kind or "reminder") != "email":
        return {}
    try:
        data = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _serialize(row: Reminder, tz_name: str) -> dict:
    """ORM 对象 → 接口/工具用的 dict。"""
    payload = _row_payload(row)
    return {
        "id": row.id,
        "reminder_id": f"rem_{row.id}",
        "kind": row.kind or "reminder",
        "text": row.text,
        # 定时邮件才有：收件人和主题，供前端/模型区分展示
        "to": payload.get("to", ""),
        "subject": payload.get("subject", ""),
        "remind_at": to_iso_utc(row.remind_at),
        "remind_at_local": format_local(row.remind_at, tz_name),
        "status": row.status,
        "fired_via": row.fired_via or "",
        "last_error": row.last_error or "",
        "created_at": to_iso_utc(row.created_at),
        "fired_at": to_iso_utc(row.fired_at),
    }


# ============================================================
# 增 / 查 / 取消
# ============================================================

async def create_reminder(user_id: int, text: str, remind_at: str) -> dict:
    """创建提醒：校验时间和内容 → 落库为 pending，等调度器投递。"""
    content = (text or "").strip()
    if not content:
        raise ReminderError("提醒内容不能为空。")
    if len(content) > 300:
        content = content[:300]

    tz_name = await _user_timezone(user_id)
    when = parse_remind_at(remind_at, tz_name)

    async with session_scope() as db:
        row = Reminder(user_id=user_id, text=content, remind_at=when, status="pending")
        db.add(row)
        await db.flush()   # 拿到自增 ID，commit 由 session_scope 负责
        reminder_id = row.id

    return {
        "reminder_id": f"rem_{reminder_id}",
        "id": reminder_id,
        "text": content,
        "remind_at": to_iso_utc(when),
        "remind_at_local": format_local(when, tz_name),
        "status": "pending",
    }


async def create_scheduled_email(
    user_id: int, to: str, subject: str, body: str, send_at: str,
) -> dict:
    """
    安排一封定时邮件：到点后由调度器真正发给收件人。

    和提醒共用一张表（kind=email），所以列表、取消、审计都沿用同一套逻辑；
    邮件正文放在 payload_json 里，到点才读取发送。
    """
    recipient = (to or "").strip()
    title = (subject or "").strip()
    content = body or ""

    if not recipient or not title:
        raise ReminderError("收件人和主题不能为空。")
    if "@" not in recipient:
        raise ReminderError(f"收件人地址看起来不对：{recipient}")

    tz_name = await _user_timezone(user_id)
    when = parse_remind_at(send_at, tz_name)

    payload = json.dumps(
        {"to": recipient, "subject": title, "body": content[:5000]},
        ensure_ascii=False,
    )
    display = f"给 {recipient} 发送《{title}》"

    async with session_scope() as db:
        row = Reminder(
            user_id=user_id,
            kind="email",
            payload_json=payload,
            text=display,
            remind_at=when,
            status="pending",
        )
        db.add(row)
        await db.flush()
        reminder_id = row.id

    return {
        "reminder_id": f"rem_{reminder_id}",
        "id": reminder_id,
        "kind": "email",
        "to": recipient,
        "subject": title,
        "text": display,
        "send_at": to_iso_utc(when),
        "send_at_local": format_local(when, tz_name),
        "status": "pending",
    }


async def list_reminders(user_id: int, status: str = "pending", limit: int = 10) -> dict:
    """列出提醒。status 取 pending / fired / cancelled / all。"""
    wanted = (status or "pending").strip().lower()
    if wanted not in VALID_STATUSES:
        wanted = "pending"
    try:
        size = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        size = 10

    tz_name = await _user_timezone(user_id)

    async with session_scope() as db:
        stmt = select(Reminder).where(Reminder.user_id == user_id)
        if wanted != "all":
            stmt = stmt.where(Reminder.status == wanted)
        # 待触发的按"最近的先"，历史记录按"最近的先"
        order = Reminder.remind_at.asc() if wanted == "pending" else Reminder.remind_at.desc()
        rows = (await db.execute(stmt.order_by(order).limit(size))).scalars().all()

    items = [_serialize(r, tz_name) for r in rows]
    return {"count": len(items), "status": wanted, "reminders": items}


async def cancel_reminder(user_id: int, reminder_id: str = "", text: str = "") -> dict:
    """
    取消一条还没触发的提醒。

    两种定位方式（模型往往只知道内容、不知道 ID）：
    - reminder_id："rem_12" 或 "12"
    - text：按内容模糊匹配，取消最近要触发的那一条
    """
    numeric = _parse_reminder_id(reminder_id)
    keyword = (text or "").strip()
    if numeric is None and not keyword:
        raise ReminderError("请提供要取消的提醒 ID（rem_12）或提醒内容。")

    tz_name = await _user_timezone(user_id)

    async with session_scope() as db:
        stmt = select(Reminder).where(
            Reminder.user_id == user_id,
            Reminder.status == "pending",
        )
        if numeric is not None:
            stmt = stmt.where(Reminder.id == numeric)
        else:
            stmt = stmt.where(Reminder.text.like(f"%{keyword}%"))
        stmt = stmt.order_by(Reminder.remind_at.asc()).limit(1)
        row = (await db.execute(stmt)).scalar_one_or_none()

        if row is None:
            return {"cancelled": False, "message": "没有找到匹配的待触发提醒。"}

        row.status = "cancelled"
        result = _serialize(row, tz_name)

    result["cancelled"] = True
    return result


# ============================================================
# 站内收件箱
# ============================================================

async def list_inbox(user_id: int, limit: int = 20) -> dict:
    """
    站内通知：已经投递过（成功或失败）、但用户还没点"知道了"的任务。

    前端会定时轮询它，到点后就能在聊天页顶部看到提醒卡片。
    失败的定时邮件也在这里出现 —— 用户必须能看到"没发出去"。
    """
    tz_name = await _user_timezone(user_id)
    async with session_scope() as db:
        rows = (
            await db.execute(
                select(Reminder)
                .where(
                    Reminder.user_id == user_id,
                    Reminder.status.in_(("fired", "failed")),
                    Reminder.acknowledged_at.is_(None),
                )
                .order_by(Reminder.fired_at.desc())
                .limit(max(1, min(int(limit or 20), 50)))
            )
        ).scalars().all()

    return {"count": len(rows), "reminders": [_serialize(r, tz_name) for r in rows]}


async def acknowledge_reminder(user_id: int, reminder_id: int) -> bool:
    """把站内提醒标记为已知晓。返回是否命中记录。"""
    async with session_scope() as db:
        result = await db.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id, Reminder.user_id == user_id)
            .values(acknowledged_at=_now_utc())
        )
        return result.rowcount == 1


# ============================================================
# 调度与投递
# ============================================================

async def dispatch_due_reminders(limit: int = 50) -> int:
    """
    把所有到点且仍是 pending 的提醒投递出去，返回本次处理条数。

    并发安全：先用一条 UPDATE ... WHERE status='pending' 抢占（pending → firing），
    只有抢占成功的进程负责投递，所以多进程部署或进程重启都不会重复发送。
    """
    now = _now_utc()

    async with session_scope() as db:
        ids = (
            await db.execute(
                select(Reminder.id)
                .where(Reminder.status == "pending", Reminder.remind_at <= now)
                .order_by(Reminder.remind_at.asc())
                .limit(limit)
            )
        ).scalars().all()

    handled = 0
    for reminder_id in ids:
        async with session_scope() as db:
            claimed = await db.execute(
                update(Reminder)
                .where(Reminder.id == reminder_id, Reminder.status == "pending")
                .values(status="firing")
            )
            if claimed.rowcount != 1:
                continue   # 被别的进程抢走了，跳过
        await _deliver(reminder_id)
        handled += 1

    return handled


async def _deliver(reminder_id: int) -> None:
    """
    投递一条计划任务。

    - kind=reminder：给自己发一封提醒邮件 + 站内通知（邮件失败只降级，不算失败）
    - kind=email：把邮件真正发给收件人；失败会把状态置为 failed 并写明原因，
      这样"定时邮件没发出去"不会被当成成功
    两种都会在站内收件箱留一条记录。
    """
    async with session_scope() as db:
        row = (
            await db.execute(select(Reminder).where(Reminder.id == reminder_id))
        ).scalar_one_or_none()
        if row is None or row.status != "firing":
            return
        user_id = row.user_id
        text = row.text
        remind_at = row.remind_at
        kind = row.kind or "reminder"
        payload = _row_payload(row)

    tz_name = await _user_timezone(user_id)
    local_time = format_local(remind_at, tz_name)

    delivered = ["in_app"]
    last_error = ""
    failed = False

    from .email_client_wrapper import EmailNotConfiguredError

    if kind == "email":
        # 定时邮件：到点真正发给收件人，发不出去就是没完成
        try:
            from .email_client_wrapper import send_email

            await send_email(
                user_id,
                to=payload.get("to", ""),
                subject=payload.get("subject", ""),
                body=payload.get("body", ""),
            )
            delivered.insert(0, "email")
        except EmailNotConfiguredError:
            last_error = "未配置邮箱，定时邮件无法发出"
            failed = True
        except Exception as e:  # noqa: BLE001
            last_error = f"定时邮件发送失败：{e}"
            failed = True
    else:
        # 提醒：邮件是"顺带通知"，失败降级为只有站内通知
        try:
            from .email_client_wrapper import send_self_email

            await send_self_email(
                user_id,
                subject=f"⏰ 提醒：{text}",
                body=(
                    f"你设置的提醒到点了。\n\n"
                    f"内容：{text}\n"
                    f"时间：{local_time}\n\n"
                    f"—— 个人事务助理"
                ),
            )
            delivered.insert(0, "email")
        except EmailNotConfiguredError:
            last_error = "未配置邮箱，本次只发了站内通知"
        except Exception as e:  # noqa: BLE001 —— 投递失败不能影响其他提醒
            last_error = f"邮件投递失败：{e}"

    async with session_scope() as db:
        await db.execute(
            update(Reminder)
            .where(Reminder.id == reminder_id)
            .values(
                # 定时邮件发失败标记为 failed，避免"看起来成功了"
                status="failed" if failed else "fired",
                fired_at=_now_utc(),
                fired_via="+".join(delivered),
                last_error=last_error[:500],
            )
        )


async def run_reminder_scheduler(stop_event: asyncio.Event) -> None:
    """
    后台调度循环：启动后立即扫一次（补发宕机期间到点的提醒），之后按间隔轮询。

    单次异常只记录日志、不退出循环 —— 否则一次报错会让后续所有提醒永久失效。
    """
    interval = max(5, int(settings.reminder_scan_interval_seconds or 30))
    logger.info("提醒调度已启动，扫描间隔 %s 秒", interval)

    while not stop_event.is_set():
        try:
            handled = await dispatch_due_reminders()
            if handled:
                logger.info("本次投递了 %s 条提醒", handled)
        except Exception as e:  # noqa: BLE001
            logger.warning("提醒调度出错（已跳过本轮）：%s", e)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    logger.info("提醒调度已停止")
