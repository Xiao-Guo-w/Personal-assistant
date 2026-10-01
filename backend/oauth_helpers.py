"""
OAuth 通用工具。

功能：
- 生成带签名的 state（防 CSRF，含 user_id 和过期时间）
- 校验 state 并解出 user_id

state 格式：base64(user_id|expire_ts|hmac)
"""

import base64
import hashlib
import hmac
import time

from .config import settings


STATE_TTL_SECONDS = 600  # state 有效期 10 分钟


def _sign(payload: str) -> str:
    """用 state_secret 对 payload 做 HMAC 签名。"""
    return hmac.new(
        settings.oauth_state_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()


def generate_state(user_id: int) -> str:
    """
    生成 OAuth state。

    包含 user_id 和过期时间，并用 HMAC 签名，防止伪造。
    """
    expire_ts = int(time.time()) + STATE_TTL_SECONDS
    payload = f"{user_id}|{expire_ts}"
    signature = _sign(payload)
    raw = f"{payload}|{signature}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_state(state: str) -> int:
    """
    校验 state，返回 user_id。

    失败抛 ValueError。
    """
    try:
        raw = base64.urlsafe_b64decode(state.encode()).decode()
        user_id_str, expire_str, signature = raw.rsplit("|", 2)
    except Exception as e:
        raise ValueError(f"state 格式错误：{e}") from e

    # 校验签名
    expected_sig = _sign(f"{user_id_str}|{expire_str}")
    if not hmac.compare_digest(signature, expected_sig):
        raise ValueError("state 签名无效")

    # 校验过期
    if int(expire_str) < int(time.time()):
        raise ValueError("state 已过期")

    return int(user_id_str)