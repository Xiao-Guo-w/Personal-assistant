"""
向量化客户端（OpenAI 兼容协议）。

为什么自己写而不是用别家的 SDK：
embedding 服务的选择会变，OpenAI 兼容协议是事实标准，用 httpx 直接调
/embeddings 就不必为每家的 SDK 增加依赖，换服务只改 .env。

三项配置（base_url / api_key / model）留空即视为未配置，整个知识库
自动降级为纯关键词检索，不会因为没配向量服务而无法使用。
"""

import asyncio
import random

import httpx


class EmbeddingNotConfigured(Exception):
    """没有配置向量化服务。"""


class EmbeddingError(Exception):
    """向量化调用失败（已重试过）。"""


class Embedder:
    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        dim: int = 0,
        batch_size: int = 32,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.batch_size = max(1, batch_size)
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        # 配置里给 0 表示"首次调用时自动探测"
        self._dim = int(dim or 0)

    # ---------- 状态 ----------

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def dim(self) -> int:
        """已知的向量维度；0 表示还没探测出来。"""
        return self._dim

    @property
    def name(self) -> str:
        """写进 kb_documents.embedding_model 的标识。"""
        return self.model or ""

    def _endpoint(self) -> str:
        # 允许用户直接给到 /embeddings 后缀，避免拼成 /v1/embeddings/embeddings
        if self.base_url.endswith("/embeddings"):
            return self.base_url
        return f"{self.base_url}/embeddings"

    # ---------- 对外接口 ----------

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量编码。返回顺序与入参一致。"""
        self._require_configured()
        if not texts:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self.embed_documents([text])
        return vectors[0]

    # ---------- 内部实现 ----------

    def _require_configured(self) -> None:
        if not self.configured:
            raise EmbeddingNotConfigured("未配置向量化服务")

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": batch}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries):
                try:
                    response = await client.post(
                        self._endpoint(), json=payload, headers=headers,
                    )
                except httpx.HTTPError as e:
                    # 网络层问题（超时 / 连接失败）：可重试
                    last_error = e
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(2**attempt + random.random())
                        continue
                    raise EmbeddingError(f"向量化服务连接失败：{e}") from e

                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = EmbeddingError(
                        f"向量化服务返回 {response.status_code}：{_brief(response)}"
                    )
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(2**attempt + random.random())
                        continue
                    raise last_error

                if response.status_code >= 400:
                    # 4xx：参数 / 鉴权 / 模型名写错，重试没有意义
                    raise EmbeddingError(
                        f"向量化服务返回 {response.status_code}：{_brief(response)}"
                    )

                return self._parse(response)

        raise last_error or EmbeddingError("向量化失败")

    def _parse(self, response: httpx.Response) -> list[list[float]]:
        try:
            data = response.json()
            items = data["data"]
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError(f"向量化服务返回了无法解析的内容：{_brief(response)}") from e

        if not isinstance(items, list) or not items:
            raise EmbeddingError("向量化服务没有返回任何向量")

        # 部分服务不保证返回顺序，按 index 排一遍更稳妥
        try:
            items = sorted(items, key=lambda item: item.get("index", 0))
            vectors = [item["embedding"] for item in items]
        except (TypeError, KeyError) as e:
            raise EmbeddingError("向量化服务的响应格式不符合 OpenAI 兼容协议") from e

        for vector in vectors:
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("向量化服务返回了空的向量")

        # 首次调用时探测维度，并校验批内一致性
        dim = len(vectors[0])
        if any(len(v) != dim for v in vectors):
            raise EmbeddingError("同一次请求返回的向量维度不一致")

        if self._dim == 0:
            self._dim = dim
        elif self._dim != dim:
            raise EmbeddingError(
                f"维度不匹配：配置为 {self._dim}，服务实际返回 {dim}。"
                "换过 embedding 模型时需要重建知识库索引。"
            )

        return vectors


def _brief(response: httpx.Response) -> str:
    """截断响应体，避免把整页 HTML 错误页贴进错误信息。"""
    try:
        text = response.text
    except Exception:  # noqa: BLE001
        return "<无法读取响应>"
    text = " ".join(text.split())
    return text[:200]
