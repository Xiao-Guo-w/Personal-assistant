"""
长期记忆读写 + 对用户友好的展示元数据。

包含：
- 基础读写：get_user_memories / get_user_memories_for_prompt / set_user_memory
- 展示元数据：MEMORY_KEY_META / _resolve_meta / _format_value
- 前端接口：list_user_memories_detailed / upsert_user_memory / delete_user_memory
"""

import re
from datetime import datetime, timezone

from sqlalchemy import select

from .db import session_scope
from .models import UserMemory


# ============================================================
# 展示元数据：把 key 翻译成人话
# ============================================================

MEMORY_KEY_META: dict[str, dict] = {
    # ---------- 时间与日程 ----------
    "default_meeting_duration": {
        "label": "默认会议时长", "category": "时间与日程",
        "icon": "⏱️", "format": "duration_minutes",
    },
    "timezone": {
        "label": "所在时区", "category": "时间与日程",
        "icon": "🌏", "format": "plain",
    },
    "work_hours_start": {
        "label": "工作开始时间", "category": "时间与日程",
        "icon": "🌅", "format": "time_hhmm",
    },
    "work_hours_end": {
        "label": "工作结束时间", "category": "时间与日程",
        "icon": "🌆", "format": "time_hhmm",
    },
    "default_reminder_advance": {
        "label": "默认提前提醒（分钟）", "category": "时间与日程",
        "icon": "🔔", "format": "duration_minutes",
    },
    # ---------- 常用联系人 ----------
    "contact_zhangsan_email": {
        "label": "张三的邮箱", "category": "常用联系人",
        "icon": "👤", "format": "email",
    },
    "contact_lisi_email": {
        "label": "李四的邮箱", "category": "常用联系人",
        "icon": "👤", "format": "email",
    },
    # ---------- 沟通与邮件 ----------
    "email_signature": {
        "label": "邮件签名", "category": "沟通与邮件",
        "icon": "✍️", "format": "multiline",
    },
    "reply_tone": {
        "label": "邮件回复语气", "category": "沟通与邮件",
        "icon": "🎭", "format": "plain",
    },
}


CATEGORY_ORDER = [
    "时间与日程", "常用联系人", "沟通与邮件", "我的记忆", "其他",
]

MEMORY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _resolve_meta(key: str, label: str = "") -> dict:
    """
    返回 key 对应的展示元数据。

    优先级：用户自定义名称 → 精确匹配 → 通配规则 → 兜底转换。
    """
    if label:
        return {
            "label": label, "category": "我的记忆",
            "icon": "📝", "format": "plain",
        }

    if key in MEMORY_KEY_META:
        return MEMORY_KEY_META[key]

    # 通配：contact_<name>_email → "<name> 的邮箱"
    if key.startswith("contact_") and key.endswith("_email"):
        name = key[len("contact_"):-len("_email")]
        return {
            "label": f"{name} 的邮箱", "category": "常用联系人",
            "icon": "👤", "format": "email",
        }

    # 通配：default_<xxx>_duration
    if key.startswith("default_") and key.endswith("_duration"):
        subject = key[len("default_"):-len("_duration")]
        return {
            "label": f"{subject} 默认时长", "category": "时间与日程",
            "icon": "⏱️", "format": "duration_minutes",
        }

    # 兜底：下划线转空格 + 首字母大写
    fallback_label = key.replace("_", " ").strip()
    if fallback_label:
        fallback_label = fallback_label[0].upper() + fallback_label[1:]

    return {
        "label": fallback_label or key, "category": "其他",
        "icon": "🔖", "format": "plain",
    }


def _format_value(raw: str, fmt: str) -> str:
    """把原始值格式化成用户友好的展示文本。"""
    raw = str(raw)

    if fmt == "duration_minutes":
        try:
            m = int(raw)
        except ValueError:
            return raw
        if m < 60:
            return f"{m} 分钟"
        h, mm = divmod(m, 60)
        return f"{h} 小时" + (f" {mm} 分钟" if mm else "")

    if fmt == "boolean":
        return "是" if raw.lower() in ("true", "1", "yes", "是") else "否"

    return raw


# ============================================================
# 基础读写
# ============================================================

async def get_user_memories(user_id: int) -> dict[str, str]:
    """读取用户所有偏好，返回 {key: value}。供按 key 取值的业务逻辑使用。"""
    async with session_scope() as db:
        result = await db.execute(
            select(UserMemory).where(UserMemory.user_id == user_id)
        )
        rows = result.scalars().all()
    return {row.key: row.value for row in rows}


async def get_user_memories_for_prompt(user_id: int) -> dict[str, str]:
    """
    供系统提示使用：把用户自定义名称也带进提示词。

    自定义记忆的 key 是随机标识，只有 value 会丢上下文；这里输出
    「展示名称（key）」: value，既保留用户可读语义，也保留稳定键名。
    """
    async with session_scope() as db:
        result = await db.execute(
            select(UserMemory).where(UserMemory.user_id == user_id)
        )
        rows = result.scalars().all()

    items: dict[str, str] = {}
    for row in rows:
        meta = _resolve_meta(row.key, row.label or "")
        items[f"{meta['label']}（{row.key}）"] = row.value
    return items


async def set_user_memory(
    user_id: int, key: str, value: str, label: str | None = None,
) -> None:
    """写入 / 更新用户偏好。"""
    async with session_scope() as db:
        existing = await db.execute(
            select(UserMemory).where(
                UserMemory.user_id == user_id,
                UserMemory.key == key,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            row.value = value
            if label is not None:
                row.label = label
        else:
            db.add(
                UserMemory(
                    user_id=user_id,
                    key=key,
                    value=value,
                    label=label or "",
                )
            )


def _serialize_memory(row: UserMemory) -> dict:
    """ORM 对象 → 前端展示项。"""
    meta = _resolve_meta(row.key, row.label or "")
    updated = row.updated_at
    if updated is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return {
        "key": row.key,
        "label": meta["label"],
        "category": meta["category"],
        "icon": meta["icon"],
        "raw_value": row.value,
        "display_value": _format_value(row.value, meta["format"]),
        "updated_at": updated.isoformat() if updated else None,
    }


async def upsert_user_memory(
    user_id: int, key: str, value: str, label: str = "",
) -> None:
    """
    用户从界面新增 / 修改一条长期记忆。

    key 是注入模型提示词时使用的稳定标识；label 只负责展示，
    因此自定义记忆用随机 key + 用户填写的名称。
    """
    clean_key = (key or "").strip()
    if not MEMORY_KEY_RE.fullmatch(clean_key):
        raise ValueError("记忆标识只能包含英文字母、数字、下划线、点和短横线，且不超过 64 个字符。")

    clean_value = (value or "").strip()
    if not clean_value:
        raise ValueError("记忆内容不能为空。")
    if len(clean_value) > 4000:
        raise ValueError("记忆内容最多 4000 个字符。")

    clean_label = (label or "").strip()
    if len(clean_label) > 120:
        raise ValueError("记忆名称最多 120 个字符。")

    await set_user_memory(
        user_id, clean_key, clean_value, clean_label or None,
    )


async def get_user_memory_detailed(user_id: int, key: str) -> dict | None:
    """按 key 读取一条展示项，供写接口返回最新状态。"""
    async with session_scope() as db:
        row = (
            await db.execute(
                select(UserMemory).where(
                    UserMemory.user_id == user_id,
                    UserMemory.key == key,
                )
            )
        ).scalar_one_or_none()
    return _serialize_memory(row) if row is not None else None


# ============================================================
# 展示用接口
# ============================================================

async def list_user_memories_detailed(user_id: int) -> list[dict]:
    """返回带展示元数据的记忆列表。"""
    async with session_scope() as db:
        result = await db.execute(
            select(UserMemory)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.updated_at.desc())
        )
        rows = result.scalars().all()

    items: list[dict] = []
    for row in rows:
        items.append(_serialize_memory(row))

    def _cat_rank(item: dict) -> int:
        cat = item["category"]
        return CATEGORY_ORDER.index(cat) if cat in CATEGORY_ORDER else len(CATEGORY_ORDER)

    items.sort(key=_cat_rank)
    return items


async def delete_user_memory(user_id: int, key: str) -> bool:
    """删除一条记忆；返回是否成功。"""
    async with session_scope() as db:
        result = await db.execute(
            select(UserMemory).where(
                UserMemory.user_id == user_id,
                UserMemory.key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await db.delete(row)
        return True
