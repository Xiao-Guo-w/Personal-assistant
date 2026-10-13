"""
Chroma 向量库封装。

定位：Chroma 是**可离线重建的派生索引**，不是权威数据源。
正文、元数据、向量副本都在 SQLite 里，这里整个删掉也能重建。

三个必须显式处理的点：

1. embedding_function 必须显式传 None。
   Chroma 的默认 embedding 函数是 ONNXMiniLM_L6_V2，会在首次使用时
   下载并加载一个本地 ONNX 模型。本项目要求不使用本地模型，所以
   一律显式传 None，并在所有 add/upsert/query 调用里自带向量。

2. 遥测默认开启，必须显式关掉。

3. chromadb 是同步客户端，而本项目是单进程 async 后端。
   所有调用统一走 asyncio.to_thread，否则会阻塞整个事件循环。

隔离策略：每个用户一个 collection（kb_u{user_id}）。
隔离由构造保证，而不是依赖每次查询都记得写 where 过滤器。
"""

import asyncio
from typing import Any


class VectorStoreUnavailable(Exception):
    """Chroma 未安装或初始化失败。"""


class VectorStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._client: Any = None
        self._collections: dict[int, Any] = {}
        self._lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    def _ensure_client(self) -> Any:
        """延迟导入并初始化客户端（同步，必须在工作线程里调用）。"""
        if self._client is not None:
            return self._client

        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError as e:  # pragma: no cover
            raise VectorStoreUnavailable(
                "未安装 chromadb，无法启用向量检索"
            ) from e

        try:
            self._client = chromadb.PersistentClient(
                path=self.path,
                settings=Settings(anonymized_telemetry=False, allow_reset=False),
            )
        except Exception as e:  # noqa: BLE001
            raise VectorStoreUnavailable(f"Chroma 初始化失败：{e}") from e

        return self._client

    async def _get_collection(self, user_id: int) -> Any:
        """取（或建）某个用户的 collection。"""
        cached = self._collections.get(user_id)
        if cached is not None:
            return cached

        async with self._lock:
            cached = self._collections.get(user_id)
            if cached is not None:
                return cached

            def _open() -> Any:
                client = self._ensure_client()
                return client.get_or_create_collection(
                    name=_collection_name(user_id),
                    # 余弦空间：与 SQLite 侧存归一化无关，Chroma 内部处理
                    metadata={"hnsw:space": "cosine"},
                    embedding_function=None,
                )

            collection = await asyncio.to_thread(_open)
            self._collections[user_id] = collection
            return collection

    # ---------- 写入 ----------

    async def upsert(
        self,
        user_id: int,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict] | None = None,
    ) -> None:
        """写入或覆盖向量。ids 用 chunk id 的字符串形式。"""
        if not ids:
            return
        if len(ids) != len(embeddings):
            raise ValueError("ids 与 embeddings 数量不一致")

        collection = await self._get_collection(user_id)
        metas = metadatas or [{} for _ in ids]

        def _write() -> None:
            collection.upsert(ids=ids, embeddings=embeddings, metadatas=metas)

        try:
            await asyncio.to_thread(_write)
        except Exception as e:  # noqa: BLE001
            raise VectorStoreUnavailable(f"写入向量库失败：{e}") from e

    # ---------- 查询 ----------

    async def query(
        self,
        user_id: int,
        embedding: list[float],
        top_k: int = 30,
    ) -> list[tuple[str, float]]:
        """
        KNN 检索，返回 [(chunk_id, 相似度)]，按相似度降序。

        Chroma 的 cosine 空间返回的是距离（1 - 余弦相似度），这里换算成相似度，
        让上层与关键词通道的"分数越大越相关"约定一致。
        """
        if top_k <= 0:
            return []

        collection = await self._get_collection(user_id)

        def _query() -> Any:
            return collection.query(
                query_embeddings=[embedding],
                n_results=top_k,
                include=["distances"],
            )

        result = await asyncio.to_thread(_query)
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            (str(chunk_id), 1.0 - float(distance))
            for chunk_id, distance in zip(ids, distances)
        ]

    # ---------- 删除 ----------

    async def delete_ids(self, user_id: int, ids: list[str]) -> None:
        if not ids:
            return
        collection = await self._get_collection(user_id)

        def _delete() -> None:
            collection.delete(ids=ids)

        try:
            await asyncio.to_thread(_delete)
        except Exception as e:  # noqa: BLE001
            raise VectorStoreUnavailable(f"删除向量失败：{e}") from e

    async def delete_document(self, user_id: int, doc_id: int) -> None:
        """按元数据删除某篇文档的全部向量。"""
        collection = await self._get_collection(user_id)

        def _delete() -> None:
            collection.delete(where={"doc_id": int(doc_id)})

        try:
            await asyncio.to_thread(_delete)
        except Exception as e:  # noqa: BLE001
            raise VectorStoreUnavailable(f"删除文档向量失败：{e}") from e

    async def drop_user(self, user_id: int) -> None:
        """整库删除该用户的数据（删账号 / 重置时用）。"""
        name = _collection_name(user_id)

        def _drop() -> None:
            client = self._ensure_client()
            try:
                client.delete_collection(name=name)
            except Exception:  # noqa: BLE001 - collection 不存在时忽略
                pass

        await asyncio.to_thread(_drop)
        self._collections.pop(user_id, None)

    async def count(self, user_id: int) -> int:
        collection = await self._get_collection(user_id)
        return int(await asyncio.to_thread(collection.count))

    async def close(self) -> None:
        """释放客户端引用。Chroma 的 PersistentClient 没有显式 close。"""
        self._collections.clear()
        self._client = None


def _collection_name(user_id: int) -> str:
    return f"kb_u{int(user_id)}"
