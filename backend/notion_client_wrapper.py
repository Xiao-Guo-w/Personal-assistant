"""
Notion 客户端封装（OAuth 用户授权版）。

- 用户在集成设置页点击「连接 Notion」完成 OAuth 授权
- 授权后 access_token / workspace_id 加密存入 UserIntegration
- Notion OAuth token 长期有效，无需刷新
- 所有 API 调用以用户身份执行，操作范围由用户授权的页面决定
"""

from notion_client import AsyncClient, APIResponseError

from .config import settings
from .integrations import get_integration, set_integration


_clients: dict[int, AsyncClient] = {}


class NotionNotConfiguredError(Exception):
    """用户未完成 Notion OAuth 授权。"""


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
                "Notion-Version": "2022-06-28",
            },
        )
        if resp.status_code != 200:
            raise NotionAuthError(f"换取 Notion token 失败：{resp.text}")
        return resp.json()


async def save_oauth_result(user_id: int, token_data: dict) -> None:
    """把 OAuth 授权结果加密存入 UserIntegration。"""
    config = {
        "access_token": token_data["access_token"],
        "workspace_id": token_data.get("workspace_id", ""),
        "workspace_name": token_data.get("workspace_name", ""),
        "bot_id": token_data.get("bot_id", ""),
        "owner_type": (token_data.get("owner") or {}).get("type", ""),
    }
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
        notion_version="2022-06-28",
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


async def get_user_default_database(user_id: int) -> str:
    """
    读取用户设定的默认数据库 ID。

    OAuth 授权后 Notion 返回的是页面访问范围，
    没有固定默认数据库，需用户从授权范围内的 database 中选定。
    """
    config = await get_integration(user_id, "notion")
    if config and config.get("default_database_id"):
        return config["default_database_id"]
    return ""


async def set_default_database(user_id: int, database_id: str) -> None:
    """设置用户默认数据库 ID。"""
    config = await get_integration(user_id, "notion") or {}
    config["default_database_id"] = database_id
    await set_integration(user_id, "notion", config)


# ============================================================
# 业务封装
# ============================================================

async def create_page_in_database(
    user_id: int, database_id: str, title: str,
    properties: dict | None = None,
) -> dict:
    """在指定数据库创建页面。"""
    client = await get_notion_client(user_id)

    if not database_id:
        database_id = await get_user_default_database(user_id)
    if not database_id:
        raise NotionNotConfiguredError(
            "未指定数据库，且未配置默认数据库。请在「集成设置」中填写 database_id。"
        )

    page_properties = {"Name": {"title": [{"text": {"content": title}}]}}
    if properties:
        page_properties.update(properties)

    try:
        response = await client.pages.create(
            parent={"database_id": database_id},
            properties=page_properties,
        )
        return {
            "page_id": response["id"],
            "title": title,
            "url": response.get("url", ""),
            "status": "created",
        }
    except APIResponseError as e:
        raise _translate_error(e)


async def query_database(
    user_id: int, database_id: str,
    filter_obj: dict | None = None, page_size: int = 10,
) -> dict:
    """查询数据库内容。"""
    client = await get_notion_client(user_id)

    if not database_id:
        database_id = await get_user_default_database(user_id)
    if not database_id:
        raise NotionNotConfiguredError("未指定数据库，且未配置默认数据库")

    kwargs = {"database_id": database_id, "page_size": page_size}
    if filter_obj:
        kwargs["filter"] = filter_obj

    try:
        response = await client.databases.query(**kwargs)
        pages = [
            {
                "page_id": p["id"],
                "url": p.get("url", ""),
                "title": _extract_title(p),
            }
            for p in response.get("results", [])
        ]
        return {"count": len(pages), "pages": pages}
    except APIResponseError as e:
        raise _translate_error(e)


async def update_page(user_id: int, page_id: str, properties: dict) -> dict:
    """更新页面属性。"""
    client = await get_notion_client(user_id)
    try:
        response = await client.pages.update(page_id=page_id, properties=properties)
        return {
            "page_id": response["id"],
            "url": response.get("url", ""),
            "status": "updated",
        }
    except APIResponseError as e:
        raise _translate_error(e)


async def archive_page(user_id: int, page_id: str) -> dict:
    """归档（软删除）页面。"""
    client = await get_notion_client(user_id)
    try:
        response = await client.pages.update(page_id=page_id, archived=True)
        return {"page_id": response["id"], "status": "archived"}
    except APIResponseError as e:
        raise _translate_error(e)


async def search_pages(user_id: int, query: str = "", page_size: int = 10) -> dict:
    """搜索用户授权范围内的所有页面 / 数据库。"""
    client = await get_notion_client(user_id)
    try:
        kwargs = {"page_size": page_size}
        if query:
            kwargs["query"] = query
        response = await client.search(**kwargs)

        results = []
        for item in response.get("results", []):
            result_type = item.get("object")
            title = ""
            if result_type == "page":
                title = _extract_title(item)
            elif result_type == "database":
                title_arr = item.get("title", [])
                if title_arr:
                    title = title_arr[0].get("plain_text", "")

            results.append({
                "id": item["id"],
                "type": result_type,
                "title": title,
                "url": item.get("url", ""),
            })

        return {"count": len(results), "results": results}
    except APIResponseError as e:
        raise _translate_error(e)


# ============================================================
# 内部工具
# ============================================================

def _extract_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            arr = prop.get("title", [])
            if arr:
                return arr[0].get("plain_text", "")
    return ""


def _translate_error(e: APIResponseError):
    """Notion 错误翻译。404 表示权限不足，不可重试。"""
    from .executor import RetryableError, NonRetryableError

    status = e.status
    code = e.code

    if status in (429, 529):
        return RetryableError(f"Notion 限流：{code}")
    if 500 <= status < 600:
        return RetryableError(f"Notion 服务端错误 ({status}): {code}")
    if status == 401:
        return NotionAuthError(f"Notion 认证失败：{code}")
    return NonRetryableError(f"Notion API 错误 ({status}): {code}")