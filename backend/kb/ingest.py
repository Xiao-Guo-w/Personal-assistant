"""
后台入库 worker：分块 → 查重 → 向量化 → 写索引。

为什么解析不在这里：
解析放在上传请求里（线程池执行），失败时能立刻返回可读原因、不落库；
这样也不需要保留临时文件，重启后待处理文档天然可恢复 —— 全文已经
存在 kb_documents.content_text 里。

worker 只负责慢且需要重试的部分：向量化。

任务来源是数据库（status='pending'）而不是内存队列：进程重启后
待处理的文档不会丢，配合启动时的 reset_stuck_indexing 实现自愈。
"""

import asyncio
import logging

from ..config import settings
from . import store
from .chunking import chunk_text
from .deps import embedder, ingest_wake, vectorstore
from .embeddings import EmbeddingError
from .vectorstore import VectorStoreUnavailable

logger = logging.getLogger(__name__)

# 一次处理多少条块（写 SQLite 与写 Chroma 共用）
_BATCH = 200


async def run_ingest_worker(stop: asyncio.Event) -> None:
    """入库主循环。由 FastAPI lifespan 启动与停止。"""
    logger.info("知识库入库 worker 已启动")
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                ingest_wake.wait(),
                timeout=settings.kb_worker_interval_seconds,
            )
        except asyncio.TimeoutError:
            # 兜底轮询：捡起被漏掉的 pending 文档
            pass
        ingest_wake.clear()

        try:
            pending = await store.list_pending_documents(limit=20)
        except Exception:  # noqa: BLE001
            logger.exception("读取待处理文档失败")
            continue

        for doc_id, user_id in pending:
            if stop.is_set():
                break
            try:
                await process_document(doc_id, user_id)
            except Exception:  # noqa: BLE001
                logger.exception("文档 %s 入库时出现未预期错误", doc_id)
                await store.mark_failed(doc_id, "入库过程中出现未预期的错误，请重试")

    logger.info("知识库入库 worker 已退出")


async def process_document(doc_id: int, user_id: int) -> None:
    """处理一篇待入库文档。异常一律转成 failed 状态，不向上抛。"""
    doc = await store.get_document(user_id, doc_id)
    if doc is None or doc.status != "pending":
        return

    await store.mark_indexing(doc_id)

    text = (doc.content_text or "").strip()
    if not text:
        await store.mark_failed(doc_id, "文档内容为空，请重新上传")
        return

    try:
        chunks = chunk_text(
            text,
            chunk_size=settings.kb_chunk_size,
            overlap=settings.kb_chunk_overlap,
            min_tokens=settings.kb_min_chunk_tokens,
        )
    except Exception as e:  # noqa: BLE001
        await store.mark_failed(doc_id, f"分块失败：{e}")
        return

    if not chunks:
        await store.mark_failed(doc_id, "没有从文档里切分出有效内容")
        return

    # ---------- 组块 + 块级查重 ----------
    hashes = [store.hash_text(chunk.text) for chunk in chunks]
    try:
        reusable = await store.find_reusable_vectors(user_id, hashes)
    except Exception:  # noqa: BLE001
        logger.exception("块级查重查询失败，按全部重新向量化处理")
        reusable = {}

    rows: list[dict] = []
    for idx, (chunk, content_hash) in enumerate(zip(chunks, hashes)):
        blob = reusable.get(content_hash)
        rows.append({
            "idx": idx,
            "text": chunk.text,
            "content_hash": content_hash,
            "token_count": chunk.token_count,
            "embedding": blob,
            "dimension": len(store.unpack_vector(blob)) if blob else 0,
        })

    try:
        chunk_ids = await store.insert_chunks(doc_id, user_id, rows)
    except Exception as e:  # noqa: BLE001
        await store.mark_failed(doc_id, f"写入分块失败：{e}")
        return

    # ---------- 向量化 ----------
    todo = [
        (chunk_id, row)
        for chunk_id, row in zip(chunk_ids, rows)
        if not row["embedding"]
    ]
    if todo and embedder.configured:
        try:
            await _embed_pending(doc_id, user_id, todo)
        except (EmbeddingError, VectorStoreUnavailable) as e:
            # 到这里分块已经入库，但文档仍是 indexing → 不会被检索命中
            await store.mark_failed(doc_id, str(e))
            return
        except Exception as e:  # noqa: BLE001
            await store.mark_failed(doc_id, f"向量化失败：{e}")
            return

    # ---------- 写向量索引 ----------
    # 复用来的向量也要写：它们的 chunk id 是新的，Chroma 里还没有
    indexed = [
        (chunk_id, row)
        for chunk_id, row in zip(chunk_ids, rows)
        if row["embedding"]
    ]
    if indexed:
        try:
            for start in range(0, len(indexed), _BATCH):
                batch = indexed[start : start + _BATCH]
                await vectorstore.upsert(
                    user_id,
                    ids=[str(chunk_id) for chunk_id, _ in batch],
                    embeddings=[
                        store.unpack_vector(row["embedding"]) for _, row in batch
                    ],
                    metadatas=[{"doc_id": doc_id} for _ in batch],
                )
        except VectorStoreUnavailable as e:
            await store.mark_failed(doc_id, str(e))
            return

    await store.mark_ready(
        doc_id,
        chunk_count=len(chunk_ids),
        embedding_model=embedder.name if embedder.configured else "",
        embedding_dim=embedder.dim if embedder.configured else 0,
    )


async def _embed_pending(doc_id: int, user_id: int, todo: list[tuple[int, dict]]) -> None:
    """
    为缺向量的块批量编码，边算边回写。

    分批回写而不是全部算完再写：单批失败时已经算好的部分不会白费
    （虽然重试目前是整体重来，但至少不会被一次超时清空全部进度）。
    """
    texts = [row["text"] for _, row in todo]
    vectors = await embedder.embed_documents(texts)
    if len(vectors) != len(texts):
        raise EmbeddingError("向量化返回的条数与请求不一致")

    updates: list[tuple[int, bytes, int]] = []
    for (chunk_id, row), vector in zip(todo, vectors):
        blob = store.pack_vector(vector)
        row["embedding"] = blob
        row["dimension"] = len(vector)
        updates.append((chunk_id, blob, len(vector)))

    # 先落 SQLite（权威源），再落 Chroma；顺序反过来的话，
    # 中途崩溃会在向量库里留下没有正文的孤儿 id，只能白占召回名额
    for start in range(0, len(updates), _BATCH):
        await store.update_chunk_embeddings(updates[start : start + _BATCH])
