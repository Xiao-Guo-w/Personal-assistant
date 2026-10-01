"""
QQ 邮箱客户端封装（多用户隔离版）。

- 每个用户用自己的 QQ 邮箱 + 授权码（从加密存储的 UserIntegration 读）
- 发送用 SMTP，读取用 IMAP
- 同步库（smtplib/imaplib）通过 asyncio.to_thread 执行，避免阻塞事件循环
- 错误分类：网络/超时可重试，认证失败不可重试

说明：QQ 邮箱未提供第三方 OAuth 接口，需用户手动获取授权码。
"""

import asyncio
import email
import imaplib
import smtplib
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from .integrations import get_integration


class EmailNotConfiguredError(Exception):
    """用户未配置邮箱时抛出。"""


class EmailAuthError(Exception):
    """授权码错误或服务未开启。"""


class EmailSendError(Exception):
    """邮件发送失败。"""


# QQ 邮箱服务器配置（固定值）
SMTP_SERVER = "smtp.qq.com"
SMTP_PORT = 465          # SSL
IMAP_SERVER = "imap.qq.com"
IMAP_PORT = 993          # SSL


async def _get_email_config(user_id: int) -> dict:
    """读取用户的邮箱配置（解密后）。"""
    config = await get_integration(user_id, "email")
    if not config or not config.get("address") or not config.get("auth_code"):
        raise EmailNotConfiguredError(
            "未配置 QQ 邮箱。请在「集成设置」页面填写你的 QQ 邮箱地址和授权码。"
        )
    return config


# ============================================================
# 发送邮件（SMTP）
# ============================================================

def _send_email_sync(address: str, auth_code: str, to: str, subject: str, body: str) -> dict:
    """
    同步发送邮件。

    QQ 邮箱要求使用 SSL 连接（端口 465），
    认证密码为授权码而非 QQ 密码。
    """
    msg = MIMEMultipart()
    msg["From"] = address
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
            server.login(address, auth_code)
            server.sendmail(address, [to], msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise EmailAuthError(f"QQ邮箱认证失败，请检查授权码：{e}") from e
    except (smtplib.SMTPException, OSError) as e:
        raise EmailSendError(f"邮件发送失败：{e}") from e

    return {
        "message_id": msg["Message-ID"] or "sent",
        "to": to,
        "subject": subject,
        "status": "sent",
    }


async def send_email(user_id: int, to: str, subject: str, body: str) -> dict:
    """异步发送邮件。"""
    config = await _get_email_config(user_id)
    return await asyncio.to_thread(
        _send_email_sync,
        config["address"],
        config["auth_code"],
        to,
        subject,
        body,
    )


async def send_self_email(user_id: int, subject: str, body: str) -> dict:
    """
    给用户自己发一封邮件（提醒投递用）。

    收件人直接用配置里的邮箱地址，所以用户不需要额外填任何东西；
    没配邮箱会抛 EmailNotConfiguredError，由调用方决定是否降级。
    """
    config = await _get_email_config(user_id)
    return await asyncio.to_thread(
        _send_email_sync,
        config["address"],
        config["auth_code"],
        config["address"],
        subject,
        body,
    )


# ============================================================
# 读取邮件（IMAP）
# ============================================================

def _decode_mime_header(value: str) -> str:
    """解码 MIME 编码的邮件头（如 =?UTF-8?B?...?=）。"""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for content, charset in parts:
        if isinstance(content, bytes):
            decoded.append(content.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(content)
    return "".join(decoded)


def _extract_body(msg: email.message.Message) -> str:
    """从邮件对象中提取纯文本正文。"""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
        return ""
    else:
        payload = msg.get_payload(decode=True)
        return payload.decode(
            msg.get_content_charset() or "utf-8", errors="replace"
        ) if payload else ""


def _search_email_sync(
    address: str, auth_code: str, query: str, max_results: int
) -> dict:
    """
    同步搜索邮件。

    query 支持：
    - 关键词：在主题中模糊匹配
    - from:xxx：按发件人搜索
    - 空字符串：返回最近邮件
    """
    try:
        with imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT) as mail:
            mail.login(address, auth_code)
            mail.select("INBOX")

            # 构造 IMAP 搜索条件
            if query.startswith("from:"):
                sender = query[5:].strip()
                status, data = mail.search(None, f'(FROM "{sender}")')
            elif query:
                status, data = mail.search(None, f'(SUBJECT "{query}")')
            else:
                status, data = mail.search(None, "ALL")

            if status != "OK":
                return {"messages": [], "query": query, "count": 0}

            msg_ids = data[0].split()
            msg_ids = msg_ids[-max_results:]

            messages = []
            for msg_id in reversed(msg_ids):
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                messages.append({
                    "id": msg_id.decode(),
                    "from": _decode_mime_header(msg.get("From", "")),
                    "subject": _decode_mime_header(msg.get("Subject", "")),
                    "date": msg.get("Date", ""),
                    "snippet": _extract_body(msg)[:200],
                })

            return {"messages": messages, "query": query, "count": len(messages)}

    except imaplib.IMAP4.error as e:
        raise EmailAuthError(f"QQ邮箱 IMAP 认证失败：{e}") from e


async def search_email(user_id: int, query: str, max_results: int = 5) -> dict:
    """异步搜索邮件。"""
    config = await _get_email_config(user_id)
    return await asyncio.to_thread(
        _search_email_sync,
        config["address"],
        config["auth_code"],
        query,
        max_results,
    )
