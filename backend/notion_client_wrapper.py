"""
Notion 客户端封装（OAuth 用户授权版 · 页面模式）。

- 用户在集成设置页点击「连接 Notion」完成 OAuth 授权
- 授权后 access_token / workspace_id 加密存入 UserIntegration
- Notion OAuth token 长期有效，无需刷新
- 所有 API 调用以用户身份执行，操作范围由用户授权的页面决定

设计取舍（为什么不用数据库了）：
数据库模式下要经过 database → data source → 属性 schema 三道解析，
标题属性名（"Name" / "名称" / "标题"）和 ID 任何一处对不上就是
400 validation_error，排查成本很高。改成页面模式后：
  - 创建页面 = 在指定父页面下建子页面，只有 parent + title 两个参数
  - 不需要读 schema、不存在属性名不匹配、不需要解析 data source ID
代价是没有结构化属性，无法按条件检索，只能"列子页面"。

⚠️ Notion API 要求新页面必须挂在父级下，不支持在工作区根目录建页面，
   所以必须有一个被授权的「父页面」；父页面在 OAuth 授权时就要勾选。

API 版本：2025-09-03。本文件只用到 pages.* / blocks.children.* / search，
这些都是长期稳定的接口。
"""

from notion_client import (
    APIResponseError,
    AsyncClient,
    RequestTimeoutError,
    UnknownHTTPResponseError,
)

from .config import settings
from .integrations import get_integration, set_integration


# ============================================================
# 常量与进程内缓存
# ============================================================

# 与 notion-client 3.x 配套的 API 版本
NOTION_API_VERSION = "2025-09-03"

# Notion 限制：单次请求最多 100 个 block；单个富文本最长 2000 字符
MAX_BLOCKS_PER_REQUEST = 100
MAX_TEXT_CHARS = 2000

# 用户级 AsyncClient 缓存。
# 注意：token 变更（重新授权）时必须清掉，否则会继续用旧 token 调用。
_clients: dict[int, AsyncClient] = {}


class NotionNotConfiguredError(Exception):
    """用户未完成 Notion OAuth 授权，或父页面未配置 / 配置非法。"""


class NotionAuthError(Exception):
    """Token 失效，需重新授权。"""


# ============================================================
# OAuth 授权 URL 与回调
# ============================================================

def build_authorize_url(state: str) -> str:
    """构造 Notion OAuth 授权页 URL。"""
    redirect_uri = f"{settings.oauth_redirect_base}/api/oauth/notion/callback"
    return (
        f"https://api.notion.com/v1/oauth/authorize"
        f"?client_id={settings.notion_client_id}"
        f"&response_type=code"
        f"&owner=user"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
    )


async def exchange_code_for_token(code: str) -> dict:
    """用授权码换 Notion access_token。"""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.notion.com/v1/oauth/token",
            auth=(settings.notion_client_id, settings.notion_client_secret),
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{settings.oauth_redirect_base}/api/oauth/notion/callback",
            },
            headers={
                "Content-Type": "application/json",
                "Notion-Version": NOTION_API_VERSION,
            },
        )
        if resp.status_code != 200:
            raise NotionAuthError(f"换取 Notion token 失败：{resp.text}")
        return resp.json()


async def save_oauth_result(user_id: int, token_data: dict) -> None:
    """
    把 OAuth 授权结果加密存入 UserIntegration。

    两个容易踩的坑，这里都处理了：
    1. 整体覆盖 config 会把用户选好的默认父页面冲掉（重新授权后又要重选），
       所以先读旧配置再合并。旧的 default_database_id 已随数据库模式下线，
       这里不再保留。
    2. 换了 token 必须让旧客户端失效，否则 _clients 里缓存的还是旧 token。
    """
    old = await get_integration(user_id, "notion") or {}

    config = {}
    if old.get("default_parent_page_id"):
        config["default_parent_page_id"] = old["default_parent_page_id"]

    config.update({
        "access_token": token_data["access_token"],
        "workspace_id": token_data.get("workspace_id", ""),
        "workspace_name": token_data.get("workspace_name", ""),
        "bot_id": token_data.get("bot_id", ""),
        "owner_type": (token_data.get("owner") or {}).get("type", ""),
    })

    await invalidate_client(user_id)
    await set_integration(user_id, "notion", config)


# ============================================================
# 客户端获取（只从 OAuth 存储读）
# ============================================================

async def get_notion_client(user_id: int) -> AsyncClient:
    """
    获取用户的 Notion 客户端。

    无全局降级：用户必须完成 OAuth 授权。
    """
    if user_id in _clients:
        return _clients[user_id]

    config = await get_integration(user_id, "notion")
    if not config or not config.get("access_token"):
        raise NotionNotConfiguredError(
            "未连接 Notion。请在「集成设置」页面点击「连接 Notion」。"
        )

    client = AsyncClient(
        auth=config["access_token"],
        notion_version=NOTION_API_VERSION,
    )
    _clients[user_id] = client
    return client


async def invalidate_client(user_id: int) -> None:
    """清缓存（授权变更后调用）。"""
    client = _clients.pop(user_id, None)
    if client is not None:
        await client.aclose()


async def close_all_clients() -> None:
    """应用关闭时释放所有客户端。"""
    for client in _clients.values():
        await client.aclose()
    _clients.clear()


# ============================================================
# 默认父页面
# ============================================================

async def get_user_default_parent_page(user_id: int) -> str:
    """读取用户设定的默认父页面 ID；未设置返回空串。"""
    config = await get_integration(user_id, "notion")
    if config and config.get("default_parent_page_id"):
        return config["default_parent_page_id"]
    return ""


async def set_default_parent_page(user_id: int, page_id: str) -> None:
    """设置用户默认父页面 ID（页面模式下所有新页面的落点）。"""
    config = await get_integration(user_id, "notion") or {}
    config["default_parent_page_id"] = page_id
    await set_integration(user_id, "notion", config)


def _is_uuid_like(value: str) -> bool:
    """
    判断是否像 Notion 的 ID：32 位十六进制，允许带 4 个连字符。

    page_id 是 UUID，长度不对（例如界面标签只显示了前 8 位）一定不是合法 ID。
    """
    compact = (value or "").strip().replace("-", "")
    return len(compact) == 32 and all(c in "0123456789abcdefABCDEF" for c in compact)


async def _resolve_parent_page_id(user_id: int, parent_page_id: str) -> str:
    """
    把上层传进来的父页面引用规范成可用的 ID。

    用户可能从聊天里粘一个被截断的 ID（界面上只显示前 8 位时很容易发生），
    LLM 也会原样转发。这里做两级兜底 + 一次明确报错：

    1. 空值 → 用用户设置的默认父页面
    2. 是默认父页面 ID 的前缀 → 按默认父页面处理
    3. 其它非法值 → 抛可读错误，绝不把垃圾 ID 发给 Notion
    """
    ref = (parent_page_id or "").strip()

    if not ref:
        ref = await get_user_default_parent_page(user_id)
        if not ref:
            raise NotionNotConfiguredError(
                "未设置 Notion 父页面。请到「集成设置 → Notion」里选择默认父页面。"
            )
        return ref

    if _is_uuid_like(ref):
        return ref

    default = await get_user_default_parent_page(user_id)
    if default and len(ref) >= 8:
        # 去掉连字符后按前缀比对：典型场景是用户粘贴了截断 ID
        if default.replace("-", "").lower().startswith(ref.replace("-", "").lower()):
            return default

    raise NotionNotConfiguredError(
        f"页面 ID「{ref}」看起来不完整（Notion 的 ID 是 32 位十六进制）。"
        "请到「集成设置 → Notion → 列出可写入的页面」里复制完整 ID。"
    )


# ============================================================
# 业务封装
# ============================================================

async def inspect_parent_page(user_id: int, page_id: str) -> dict:
    """
    校验父页面：真正访问一次 Notion，确认集成有权读写它。

    设置默认父页面时先调用它，把"ID 错 / 没授权这个页面"挡在写入之前。
    """
    ref = await _resolve_parent_page_id(user_id, page_id)
    client = await get_notion_client(user_id)

    try:
        page = await client.pages.retrieve(page_id=ref)
    except APIResponseError as e:
        raise _translate_error(e)

    return {"page_id": page.get("id", ref), "title": _extract_title(page)}


async def create_page_under_parent(
    user_id: int, parent_page_id: str, title: str, content: str = "",
) -> dict:
    """
    在父页面下创建子页面。

    页面模式下这是唯一的新建入口：只要 parent + title 两个参数，
    正文按行拆成段落 block 一起提交（一次请求最多 100 段）。
    """
    if not title:
        raise ValueError("标题不能为空")

    parent_id = await _resolve_parent_page_id(user_id, parent_page_id)
    client = await get_notion_client(user_id)

    properties = {
        "title": {"title": [{"text": {"content": title[:MAX_TEXT_CHARS]}}]},
    }
    blocks = _content_to_blocks(content)

    kwargs: dict = {
        # 父级是页面：Notion 要求显式给出 parent.type
        "parent": {"type": "page_id", "page_id": parent_id},
        "properties": properties,
    }
    if blocks:
        kwargs["children"] = blocks

    try:
        response = await client.pages.create(**kwargs)
        return {
            "page_id": response["id"],
            "title": title,
            "url": response.get("url", ""),
            "parent_page_id": parent_id,
            "content_blocks": len(blocks),
            # 正文过长时只写入前 100 段，明确告知而不是静默丢弃
            "content_truncated": len(content.splitlines()) > len(blocks),
            "status": "created",
        }
    except APIResponseError as e:
        raise _translate_error(e)


async def list_child_pages(
    user_id: int, parent_page_id: str = "", page_size: int = 10,
) -> dict:
    """
    列出父页面下的子页面。

    页面模式下没有数据库的 filter/query，只能取父页面的 children
    再筛出 child_page 类型（子页面的 block id 就是它的 page id）。
    """
    parent_id = await _resolve_parent_page_id(user_id, parent_page_id)
    client = await get_notion_client(user_id)

    try:
        response = await client.blocks.children.list(
            block_id=parent_id, page_size=page_size,
        )
    except APIResponseError as e:
        raise _translate_error(e)

    pages = [
        {
            "page_id": block["id"],
            "title": (block.get("child_page") or {}).get("title", ""),
        }
        for block in response.get("results", [])
        if block.get("type") == "child_page"
    ]
    return {
        "count": len(pages),
        "pages": pages,
        "parent_page_id": parent_id,
        "has_more": bool(response.get("has_more")),
    }


async def update_page(
    user_id: int, page_id: str, title: str = "", append_content: str = "",
) -> dict:
    """
    更新页面：改标题和/或追加正文。

    页面没有数据库那些属性，能改的只有标题（properties.title）和正文
    （往 children 里 append block），所以这里不再接收任意 properties。
    """
    client = await get_notion_client(user_id)
    changed_fields: list[str] = []

    try:
        if title:
            await client.pages.update(
                page_id=page_id,
                properties={
                    "title": {"title": [{"text": {"content": title[:MAX_TEXT_CHARS]}}]},
                },
            )
            changed_fields.append("title")

        if append_content:
            blocks = _content_to_blocks(append_content)
            if blocks:
                await client.blocks.children.append(block_id=page_id, children=blocks)
                changed_fields.append(f"content({len(blocks)} 段)")
    except APIResponseError as e:
        raise _translate_error(e)

    if not changed_fields:
        # 参数错误重试也不会成功，executor 会把它当不可重试错误处理
        raise ValueError("请提供新的标题（title）或要追加的正文（append_content）")

    return {
        "page_id": page_id,
        "updated": changed_fields,
        "status": "updated",
    }


async def archive_page(user_id: int, page_id: str) -> dict:
    """归档（软删除）页面。"""
    client = await get_notion_client(user_id)
    try:
        # 2025-09-03 版把 archived 改名为 in_trash
        response = await client.pages.update(page_id=page_id, in_trash=True)
        return {"page_id": response["id"], "status": "archived"}
    except APIResponseError as e:
        raise _translate_error(e)


async def list_pages(user_id: int, max_pages: int = 5) -> list[dict]:
    """
    列出授权范围内可见的页面（设置页选"默认父页面"用）。

    两个决定"能不能列全"的细节：
    1. 客户端按 object 类型过滤（search 的结果里混着 data_source 等对象）
    2. 翻页取，最多 max_pages × 100 条
    顺带标出顶层页面（parent.type == "workspace"），设置页优先展示它们。
    """
    client = await get_notion_client(user_id)
    found: list[dict] = []
    cursor: str | None = None

    try:
        for _ in range(max_pages):
            kwargs: dict = {"page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            response = await client.search(**kwargs)

            for item in response.get("results", []):
                if item.get("object") != "page":
                    continue
                parent = item.get("parent") or {}
                found.append({
                    "id": item["id"],
                    "title": _extract_title(item),
                    "url": item.get("url", ""),
                    "parent_type": parent.get("type", ""),
                    "is_top_level": parent.get("type") == "workspace",
                })

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
            if not cursor:
                break
    except APIResponseError as e:
        raise _translate_error(e)

    # 顶层页面排前面，方便用户直接选一个当"笔记本"
    found.sort(key=lambda p: (not p["is_top_level"], p["title"]))
    return found


# ============================================================
# 内部工具
# ============================================================

def _content_to_blocks(content: str) -> list[dict]:
    """
    纯文本正文 → 段落 block 列表。

    规则：按行拆段、跳过空行、每段截断到 Notion 的 2000 字符上限、
    最多 MAX_BLOCKS_PER_REQUEST 段（Notion 单请求限制）。
    """
    if not content or not content.strip():
        return []

    blocks: list[dict] = []
    for line in content.splitlines():
        text = line.strip()
        if not text:
            continue
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{
                    "type": "text",
                    "text": {"content": text[:MAX_TEXT_CHARS]},
                }],
            },
        })
        if len(blocks) >= MAX_BLOCKS_PER_REQUEST:
            break
    return blocks


def _extract_title(page: dict) -> str:
    """页面标题：properties 里 type == title 的那一项（页面父级创建时键名固定为 title）。"""
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            arr = prop.get("title", [])
            if arr:
                return arr[0].get("plain_text", "")
    return ""


def _translate_error(e: Exception):
    """
    Notion 错误翻译。

    保留 code 取值和原始 message：之前只拼 status + 枚举名，
    审计里只能看到 "APIErrorCode.ValidationError"，完全无法定位。
    """
    from .executor import NonRetryableError, RetryableError

    if isinstance(e, RequestTimeoutError):
        return RetryableError("Notion 请求超时，请稍后重试。")

    if isinstance(e, UnknownHTTPResponseError):
        status = getattr(e, "status", 0) or 0
        if status >= 500:
            return RetryableError(f"Notion 服务端错误 ({status})。")
        return NonRetryableError(f"Notion 请求失败 ({status})：{e}")

    status = getattr(e, "status", 0) or 0
    code = getattr(e, "code", "")
    code_text = getattr(code, "value", code) or "unknown"
    message = getattr(e, "message", "") or str(e)

    if status == 429:
        return RetryableError(f"Notion 限流（{code_text}），请稍后重试。")
    if status == 529 or status >= 500:
        return RetryableError(f"Notion 服务端错误 ({status})：{message}")
    if status == 401:
        return NotionAuthError("Notion 授权已失效，请到「集成设置」重新连接。")
    if status == 404:
        return NonRetryableError(
            "找不到该页面，或它没有授权给本应用。"
            "请在 Notion 里把这个页面共享给集成后重试。"
        )
    if status == 400:
        return NonRetryableError(f"Notion 拒绝了请求（{code_text}）：{message}")
    return NonRetryableError(f"Notion API 错误 ({status}/{code_text})：{message}")
