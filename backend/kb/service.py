"""
跨存储编排：同时涉及 SQLite 与 Chroma 的操作放这里。

本地库是权威源，Chroma 是可重建的派生索引。这个定位决定了删除顺序：

    先删 Chroma，再删 SQLite

两种崩溃后果不对称：
- 先删 Chroma 崩在中间 → SQLite 里文档还在，用户再点一次删除即可（重试幂等）
- 先删 SQLite 崩在中间 → 向量成孤儿。虽然检索时会因为取不到正文而被过滤掉
  （见 store.load_chunks_by_ids 的 join），但会白占空间、白占召回名额

Chroma 删除报错时不停下来：用户的意图是删掉这份资料，而且孤儿向量在检索
回填正文时会被自然过滤，不会出现"删了还能搜到"。
"""

import logging

from sqlalchemy import delete

from . import store
from ..db import session_scope
from ..models import KbDocument
from .deps import embedder, vectorstore
from .vectorstore import VectorStoreUnavailable

logger = logging.getLogger(__name__)


async def get_stats(user_id: int) -> dict:
    """知识库统计。向量条数取 Chroma，正文条数取 SQLite。"""
    data = await store.stats(user_id)
    data["vector_configured"] = embedder.configured
    data["embedding_model"] = embedder.name
    data["embedding_dim"] = embedder.dim
    try:
        data["vectors"] = await vectorstore.count(user_id)
    except VectorStoreUnavailable:
        data["vectors"] = 0
    return data


async def delete_document(user_id: int, doc_id: int) -> bool:
    """删除一篇文档：先 Chroma，后 SQLite。"""
    doc = await store.get_document(user_id, doc_id)
    if doc is None:
        return False

    try:
        await vectorstore.delete_document(user_id, doc_id)
    except VectorStoreUnavailable as e:
        # 向量库不可用不影响删除本身；孤儿向量会在检索回填时被过滤掉
        logger.warning("删除文档 %s 的向量失败（继续删本地数据）：%s", doc_id, e)

    await store.delete_chunks(doc_id)
    await _delete_document_row(user_id, doc_id)
    return True


async def _delete_document_row(user_id: int, doc_id: int) -> None:
    async with session_scope() as db:
        await db.execute(
            delete(KbDocument).where(
                KbDocument.id == doc_id,
                KbDocument.user_id == user_id,
            )
        )


async def reindex_document(user_id: int, doc_id: int) -> None:
    """
    重新入库：清掉旧分块与向量，回到待处理队列重跑。

    解析失败的文档没有 content_text，无法原地重跑 —— 抛可读的错误
    让前端提示用户重新上传，而不是假装重试成功。
    """
    doc = await store.get_document(user_id, doc_id)
    if doc is None:
        raise ValueError("文档不存在")

    if not (doc.content_text or "").strip():
        raise ValueError("这篇文档没有解析出内容，需要重新上传")

    try:
        await vectorstore.delete_document(user_id, doc_id)
    except VectorStoreUnavailable as e:
        logger.warning("清理文档 %s 的旧向量失败：%s", doc_id, e)

    await store.delete_chunks(doc_id)
    await store.mark_pending(doc_id)


async def rebuild_user_vectors(user_id: int) -> dict:
    """
    用 SQLite 里已存的向量副本重建 Chroma（不调用 embedding 服务）。

    用途：
    - 换了向量库 / Chroma 目录损坏，直接重建，不必重新花钱调 API
    - 之前没配向量服务、现在配好了：先把缺向量的块补算，再整体灌进 Chroma
    """
    chunks = await store.list_chunks_for_user(user_id)
    if not chunks:
        return {"chunks": 0, "vectors": 0, "reembedded": 0}

    missing = [c for c in chunks if not c["embedding"]]
    reembedded = 0
    if missing and embedder.configured:
        vectors = await embedder.embed_documents([c["text"] for c in missing])
        for item, vector in zip(missing, vectors):
            blob = store.pack_vector(vector)
            await store.update_chunk_embedding(item["id"], blob, len(vector))
            item["embedding"] = blob
            reembedded += 1

    ready = [c for c in chunks if c["embedding"]]
    try:
        await vectorstore.drop_user(user_id)
        total = 0
        # 分批灌入，避免一次性构造过大的请求
        for start in range(0, len(ready), 200):
            batch = ready[start : start + 200]
            await vectorstore.upsert(
                user_id,
                ids=[str(c["id"]) for c in batch],
                embeddings=[store.unpack_vector(c["embedding"]) for c in batch],
                metadatas=[{"doc_id": c["doc_id"]} for c in batch],
            )
            total += len(batch)
    except VectorStoreUnavailable as e:
        raise ValueError(f"重建向量索引失败：{e}") from e

    return {"chunks": len(chunks), "vectors": total, "reembedded": reembedded}
