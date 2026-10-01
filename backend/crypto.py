"""
敏感字段的对称加解密。

使用 cryptography.Fernet：
- 底层是 AES-128-CBC 加密 + HMAC-SHA256 完整性校验
- 密文带时间戳，可配合 ttl 参数实现过期
- URL-safe base64 编码，便于存数据库

设计要点：
- 密钥缺失时直接抛异常（fail-fast，不静默降级）
- 解密失败抛 DecryptionError，业务层决定如何处理
- is_encrypted 用于识别密文，兼容历史明文数据
"""

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


class EncryptionError(Exception):
    """加解密过程中的错误基类。"""


class DecryptionError(EncryptionError):
    """解密失败（密钥错误、密文损坏）。"""


def _get_fernet() -> Fernet:
    """
    获取 Fernet 实例。

    每次调用都从 settings 读，方便测试时替换；
    Fernet 构造开销极小，不用缓存。
    """
    if not settings.encryption_key:
        raise EncryptionError(
            "ENCRYPTION_KEY 未配置。请在 .env 中设置。"
            "生成方式：python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(settings.encryption_key.encode())
    except (ValueError, TypeError) as e:
        raise EncryptionError(f"ENCRYPTION_KEY 格式错误：{e}") from e


def encrypt(plaintext: str) -> str:
    """加密明文字符串。空字符串直接返回空。"""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """解密密文。空字符串返回空；失败抛 DecryptionError。"""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise DecryptionError(
            "解密失败：密钥不匹配或密文损坏。"
        ) from e


def is_encrypted(value: str) -> bool:
    """
    判断字符串是否为 Fernet 密文。

    Fernet 密文以 "gAAAAA" 开头，长度 >= 60。
    用于兼容历史明文数据。
    """
    if not value or len(value) < 60:
        return False
    if not value.startswith("gAAAAA"):
        return False
    try:
        _get_fernet().extract_timestamp(value.encode())
        return True
    except Exception:  # noqa: BLE001
        return False


def mask(value: str, head: int = 8, tail: int = 4) -> str:
    """
    脱敏显示。

    - 长度不足时全用 *
    - 否则保留前 head 位 + 后 tail 位
    """
    if not value:
        return ""
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}...{value[-tail:]}"