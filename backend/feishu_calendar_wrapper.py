"""
飞书日历客户端封装（OAuth 用户授权版）。

- 用户在集成设置页点击「连接飞书」完成 OAuth 授权
- 授权后 access_token / refresh_token / open_id 加密存入 UserIntegration
- access_token 过期时自动用 refresh_token 刷新
- 所有 API 调用以用户身份执行，操作的是用户个人主日历
"""

import time
from datetime import datetime, timezone

import httpx

from .config import settings
from .integrations import get_integration, set_integration


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
FEISHU_AUTH_BASE = "https://open.feishu.cn/open-apis/authen/v1"
FEISHU_ACCOUNTS_BASE = "https://accounts.feishu.cn/open-apis/authen/v1"
scope = "calendar:calendar calendar:calendar:readonly"

class FeishuNotConfiguredError(Exception):
    """用户未配置飞书日历。"""


class FeishuAuthError(Exception):
    """Token 无效或刷新失败，需要重新授权。"""


class FeishuApiError(Exception):
    """飞书 API 返回业务错误。"""


# ============================================================
# OAuth 授权 URL 与回调
# ============================================================

def build_authorize_url(state: str) -> str:
    """
    构造飞书 OAuth 授权页 URL。

    用户访问该 URL 后可选扫码或登录授权。
    """
    redirect_uri = f"{settings.oauth_redirect_base}/api/oauth/feishu/callback"
    return (
        f"{FEISHU_ACCOUNTS_BASE}/authorize"
        f"?client_id={settings.feishu_app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&state={state}"
        f"&scope={scope}"
    )


async def exchange_code_for_token(code: str) -> dict:
    """
    用授权码换 user_access_token。

    飞书 OAuth 流程：
    1. 先获取 app_access_token
    2. 再用 app_access_token + code 换 user_access_token
    """
    async with httpx.AsyncClient(timeout=15) as client:
        # 第一步：获取 app_access_token
        app_resp = await client.post(
            f"{FEISHU_API_BASE}/auth/v3/app_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )
        app_data = app_resp.json()
        if app_data.get("code") != 0:
            raise FeishuAuthError(f"获取 app_access_token 失败：{app_data.get('msg')}")
        app_access_token = app_data["app_access_token"]

        # 第二步：用 code 换 user_access_token
        token_resp = await client.post(
            f"{FEISHU_AUTH_BASE}/access_token",
            headers={
                "Authorization": f"Bearer {app_access_token}",
                "Content-Type": "application/json",
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
            },
        )
        token_data = token_resp.json()
        if token_data.get("code") != 0:
            raise FeishuAuthError(f"换取 user_access_token 失败：{token_data.get('msg')}")

        data = token_data.get("data", {})
        return {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_in": data.get("expires_in", 7200),
            "open_id": data.get("open_id"),
            "name": data.get("name", ""),
        }


async def _refresh_user_token(user_id: int, config: dict) -> dict:
    """
    用 refresh_token 刷新 user_access_token。

    刷新成功后写回数据库。
    """
    async with httpx.AsyncClient(timeout=15) as client:
        # 先获取 app_access_token
        app_resp = await client.post(
            f"{FEISHU_API_BASE}/auth/v3/app_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )
        app_data = app_resp.json()
        if app_data.get("code") != 0:
            raise FeishuAuthError(f"获取 app_access_token 失败：{app_data.get('msg')}")
        app_access_token = app_data["app_access_token"]

        # 刷新 token
        resp = await client.post(
            f"{FEISHU_AUTH_BASE}/refresh_access_token",
            headers={
                "Authorization": f"Bearer {app_access_token}",
                "Content-Type": "application/json",
            },
            json={
                "grant_type": "refresh_token",
                "refresh_token": config["refresh_token"],
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuAuthError(
                f"刷新 token 失败：{data.get('msg')}（需重新授权）"
            )

        token_data = data.get("data", {})
        new_config = {
            **config,
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": time.time() + token_data.get("expires_in", 7200),
        }
        await set_integration(user_id, "feishu_calendar", new_config)
        return new_config


async def save_oauth_result(user_id: int, token_data: dict) -> None:
    """把 OAuth 授权结果加密存入 UserIntegration。"""
    config = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "open_id": token_data["open_id"],
        "name": token_data.get("name", ""),
        "expires_at": time.time() + token_data.get("expires_in", 7200),
    }
    await set_integration(user_id, "feishu_calendar", config)


# ============================================================
# 内部：获取有效的 user_access_token
# ============================================================

async def _get_valid_token(user_id: int) -> tuple[str, str]:
    """
    获取有效的 access_token 和 open_id。

    过期时自动刷新。
    """
    config = await get_integration(user_id, "feishu_calendar")
    if not config or not config.get("refresh_token"):
        raise FeishuNotConfiguredError(
            "未连接飞书日历。请在「集成设置」页面点击「连接飞书」。"
        )

    # 提前 5 分钟视为过期，避免边界问题
    if config.get("expires_at", 0) - 300 < time.time():
        config = await _refresh_user_token(user_id, config)

    return config["access_token"], config["open_id"]


async def _request(user_id: int, method: str, path: str, **kwargs) -> dict:
    """以用户身份发起飞书 API 请求。"""
    access_token, _ = await _get_valid_token(user_id)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.request(
            method, f"{FEISHU_API_BASE}{path}", headers=headers, **kwargs
        )
        data = resp.json()

        if data.get("code") == 99991663:  # token 无效
            raise FeishuAuthError("飞书 token 已失效，请重新授权")
        if data.get("code") != 0:
            raise FeishuApiError(f"飞书 API 错误：{data.get('msg')}")

        return data.get("data", {})


# ============================================================
# 时间格式转换
# ============================================================

def _iso_to_timestamp(iso_str: str) -> str:
    """ISO8601 → 秒级时间戳字符串（飞书 API 要求）。"""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        try:
            int(iso_str)
            return iso_str
        except ValueError:
            raise ValueError(f"无法解析时间格式：{iso_str}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp()))


def _timestamp_to_iso(ts: str) -> str:
    """秒级时间戳字符串 → ISO8601（UTC）。"""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return ts


# ============================================================
# 业务封装
# ============================================================

async def get_primary_calendar_id(user_id: int) -> str:
    """
    获取用户主日历 ID。

    使用 user_access_token，返回用户个人的主日历。
    """
    data = await _request(user_id, "POST", "/calendar/v4/calendars/primary")
    calendars = data.get("calendars", [])
    if not calendars:
        raise FeishuApiError("无法获取主日历信息")
    return calendars[0]["calendar"]["calendar_id"]


async def list_events(user_id: int, time_min: str, time_max: str) -> dict:
    """查询主日历在时间范围内的日程。"""
    calendar_id = await get_primary_calendar_id(user_id)

    data = await _request(
        user_id, "GET",
        f"/calendar/v4/calendars/{calendar_id}/events",
        params={
            "start_time": _iso_to_timestamp(time_min),
            "end_time": _iso_to_timestamp(time_max),
            "page_size": 50,
        },
    )

    events = [
        {
            "event_id": e.get("event_id"),
            "title": e.get("summary", "(无标题)"),
            "start": _timestamp_to_iso((e.get("start_time") or {}).get("timestamp", "")),
            "end": _timestamp_to_iso((e.get("end_time") or {}).get("timestamp", "")),
            "location": (e.get("location") or {}).get("name", ""),
        }
        for e in data.get("items", [])
    ]
    return {"events": events, "time_min": time_min, "time_max": time_max}


async def create_event(
    user_id: int, title: str, start: str, end: str,
    attendees: list[str] | None = None, description: str = "",
) -> dict:
    """在主日历创建日程。参与人需单独接口添加。"""
    calendar_id = await get_primary_calendar_id(user_id)

    body = {
        "summary": title,
        "start_time": {"timestamp": _iso_to_timestamp(start)},
        "end_time": {"timestamp": _iso_to_timestamp(end)},
        "reminders": [{"minutes": 15}],
    }
    if description:
        body["description"] = description

    data = await _request(
        user_id, "POST",
        f"/calendar/v4/calendars/{calendar_id}/events",
        json=body,
    )
    event = data.get("event", {})
    event_id = event.get("event_id")

    # 添加参与人（失败不影响主流程）
    if attendees and event_id:
        try:
            await _request(
                user_id, "POST",
                f"/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees",
                json={
                    "attendees": [{"type": "user", "user_id": a} for a in attendees if a]
                },
            )
        except Exception:  # noqa: BLE001
            pass

    return {
        "event_id": event_id,
        "title": title,
        "start": start,
        "end": end,
        "status": "created",
    }