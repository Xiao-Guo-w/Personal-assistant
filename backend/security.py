"""
密码 hash 与 token 生成。

- 密码用 PBKDF2-HMAC-SHA256，十万次迭代（生产建议换 bcrypt / argon2）
- token 用 secrets.token_urlsafe(32) 生成，32 字节熵足够安全
"""

import hashlib
import hmac
# 密码学安全随机数
import secrets

from .config import settings


def hash_password(password: str) -> str:
    """
    生成密码 hash。

    用全局 salt + 密码本身参与 PBKDF2。
    返回十六进制字符串，长度 64。
    """
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        settings.password_salt.encode(),
        100_000,      # 十万次迭代，平衡安全与性能
    )
    return dk.hex()


def verify_password(password: str, password_hash: str) -> bool:
    """
    常数时间比较，避免时序攻击。

    hmac.compare_digest 保证无论相等与否，执行时间一致，
    攻击者无法通过测量响应时间推测密码。
    """
    expected = hash_password(password)
    return hmac.compare_digest(expected, password_hash)


def generate_token() -> str:
    """
    生成登录 token。

    secrets.token_urlsafe(32) 生成 32 字节随机数的 URL 安全 base64，
    熵足够高，不担心被暴力破解。
    """
    return secrets.token_urlsafe(32)