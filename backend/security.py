"""密码 hash 与 token 生成。"""

import hashlib
import hmac
import secrets

from .config import settings


def hash_password(password: str) -> str:
    """生成密码 hash（PBKDF2-HMAC-SHA256，十万次迭代）。"""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        settings.password_salt.encode(),
        100_000,
    )
    return dk.hex()


def verify_password(password: str, password_hash: str) -> bool:
    """常数时间比较，避免时序攻击。"""
    expected = hash_password(password)
    return hmac.compare_digest(expected, password_hash)


def generate_token() -> str:
    """生成登录 token（32 字节随机数的 URL-safe base64）。"""
    return secrets.token_urlsafe(32)