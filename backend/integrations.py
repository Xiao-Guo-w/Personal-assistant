"""
用户级集成配置的读写（加密版）。

关键设计：
- 敏感字段写入前加密、读取时解密，业务层无感
- 兼容历史明文：读到未加密敏感字段时自动加密回写
- 脱敏基于明文做，用户能看到有意义的前缀
"""

import json

from sqlalchemy import select

from .crypto import decrypt, encrypt, is_encrypted, mask
from .db import session_scope
from .models import UserIntegration


# 判定敏感字段的关键词（子串匹配 key 名）
_SENSITIVE_KEYWORDS = (
    "token",
    "secret",
    "key",
    "password",
    "api_key",
    "auth_code",       # QQ 邮箱授权码
    "access_code",
    "private",
    "credential",
)

def _is_sensitive(key: str) -> bool:
    """判断配置项的 key 名是否属于敏感字段。"""
    k = key.lower()
    return any(kw in k for kw in _SENSITIVE_KEYWORDS)


def _encrypt_config(config: dict) -> dict:
    """写入前：把敏感字段的值加密。已加密的跳过。"""
    result = {}
    for k, v in config.items():
        if _is_sensitive(k) and isinstance(v, str) and v and not is_encrypted(v):
            result[k] = encrypt(v)
        else:
            result[k] = v
    return result


def _decrypt_config(config: dict) -> dict:
    """读取后：把敏感字段的值解密。解密失败返回原始密文。"""
    result = {}
    for k, v in config.items():
        if _is_sensitive(k) and isinstance(v, str) and is_encrypted(v):
            try:
                result[k] = decrypt(v)
            except Exception:  # noqa: BLE001
                result[k] = v
        else:
            result[k] = v
    return result


async def get_integration(user_id: int, provider: str) -> dict | None:
    """
    读取用户配置，返回解密后的 dict，并自动升级历史明文。
    """
    async with session_scope() as db:
        result = await db.execute(
            select(UserIntegration).where(
                UserIntegration.user_id == user_id,
                UserIntegration.provider == provider,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None

        try:
            stored_config = json.loads(row.config)
        except json.JSONDecodeError:
            return None

        # 检测明文敏感字段 → 自动升级加密
        needs_upgrade = any(
            _is_sensitive(k) and isinstance(v, str) and v and not is_encrypted(v)
            for k, v in stored_config.items()
        )
        if needs_upgrade:
            row.config = json.dumps(
                _encrypt_config(stored_config), ensure_ascii=False
            )

        return _decrypt_config(stored_config)


async def set_integration(user_id: int, provider: str, config: dict) -> None:
    """
    写入 / 更新配置。传入的 config 是明文，敏感字段在写入前加密。
    """
    payload = json.dumps(_encrypt_config(config), ensure_ascii=False)
    async with session_scope() as db:
        existing = await db.execute(
            select(UserIntegration).where(
                UserIntegration.user_id == user_id,
                UserIntegration.provider == provider,
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            row.config = payload
        else:
            db.add(UserIntegration(
                user_id=user_id, provider=provider, config=payload,
            ))


async def delete_integration(user_id: int, provider: str) -> bool:
    """删除配置；返回是否成功。"""
    async with session_scope() as db:
        result = await db.execute(
            select(UserIntegration).where(
                UserIntegration.user_id == user_id,
                UserIntegration.provider == provider,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await db.delete(row)
        return True


async def list_integrations(user_id: int) -> list[dict]:
    """列出配置，敏感字段脱敏（基于明文）。"""
    async with session_scope() as db:
        result = await db.execute(
            select(UserIntegration).where(UserIntegration.user_id == user_id)
        )
        rows = result.scalars().all()

    items = []
    for row in rows:
        try:
            stored = json.loads(row.config)
        except json.JSONDecodeError:
            stored = {}
        decrypted = _decrypt_config(stored)
        masked = {
            k: mask(v, 8, 4) if _is_sensitive(k) and isinstance(v, str) else v
            for k, v in decrypted.items()
        }
        items.append({
            "provider": row.provider,
            "config_masked": masked,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        })
    return items