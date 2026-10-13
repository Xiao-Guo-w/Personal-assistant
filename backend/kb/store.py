"""
知识库的 SQLite 读写原语。

这里只碰本地库（kb_documents / kb_chunks / kb_fts），
不碰 Chroma —— 跨存储的编排在 service.py。

FTS5 是虚表，无法映射成 ORM 模型，统一用原生 SQL 操作，
rowid 与 kb_chunks.id 一一对齐。
"""

import hashlib
import struct
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, text, update

from ..db import session_scope
from ..models import KbChunk, KbDocument


# ============================================================
# 向量序列化：float32 小端序 BLOB
# ============================================================

def pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_vector(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def doc_to_dict(row: KbDocument) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "ext": row.ext,
        "mime": row.mime,
        "size_bytes": row.size_bytes,
        "char_count": row.char_count,
        "chunk_count": row.chunk_count,
        "status": row.status,
        "error": row.error or "",
        "embedding_model": row.embedding_model or "",
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


# ============================================================
# 文档
# ============================================================

async def find_document_by_hash(user_id: int, content_hash: str) -> KbDocument | None:
    """
    文档级查重：同一用户下同样的内容只允许存在一份。

    刻意**不排除 failed 文档**：(user_id, content_hash) 上有唯一约束，
    失败的那一行仍然占着这个 hash。如果这里假装它不存在，
    重新上传同一份文件就会撞唯一约束报 500。
    调用方按 status 决定怎么处理（失败的那条重新排队即可）。
    """
    async with session_scope() as db:
        result = await db.execute(
            select(KbDocument).where(
                KbDocument.user_id == user_id,
                KbDocument.content_hash == content_hash,
            )
        )
        return result.scalar_one_or_none()


async def create_document(
    user_id: int,
    *,
    title: str,
    ext: str,
    mime: str = "",
    size_bytes: int = 0,
    content_text: str = "",
    char_count: int = 0,
    content_hash: str = "",
    status: str = "pending",
) -> int:
    """
    登记一篇文档。

    解析在上传请求里就完成了，所以这里能拿到真实的 content_hash。
    content_hash 为空时（理论上不会发生）放一个唯一占位值，
    避免并发上传一起撞上 (user_id, content_hash) 唯一约束。
    """
    async with session_scope() as db:
        row = KbDocument(
            user_id=user_id,
            title=title[:255],
            ext=ext,
            mime=mime,
            size_bytes=size_bytes,
            content_text=content_text,
            char_count=char_count,
            content_hash=content_hash or f"pending-{uuid.uuid4().hex}",
            status=status,
        )
        db.add(row)
        await db.flush()
        return int(row.id)


async def mark_indexing(doc_id: int) -> None:
    """开始处理。前端据此把状态从"排队中"切到"正在加入知识库中"。"""
    async with session_scope() as db:
        await db.execute(
            update(KbDocument)
            .where(KbDocument.id == doc_id)
            .values(status="indexing")
        )


async def get_document(user_id: int, doc_id: int) -> KbDocument | None:
    async with session_scope() as db:
        result = await db.execute(
            select(KbDocument).where(
                KbDocument.id == doc_id,
                KbDocument.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()


async def list_documents(user_id: int) -> list[dict]:
    async with session_scope() as db:
        result = await db.execute(
            select(KbDocument)
            .where(KbDocument.user_id == user_id)
            .order_by(KbDocument.created_at.desc(), KbDocument.id.desc())
        )
        return [doc_to_dict(row) for row in result.scalars().all()]


async def mark_ready(
    doc_id: int,
    *,
    chunk_count: int,
    embedding_model: str,
    embedding_dim: int,
) -> None:
    async with session_scope() as db:
        await db.execute(
            update(KbDocument)
            .where(KbDocument.id == doc_id)
            .values(
                status="ready",
                error="",
                chunk_count=chunk_count,
                embedding_model=embedding_model,
                embedding_dim=embedding_dim,
            )
        )


async def mark_failed(doc_id: int, error: str) -> None:
    async with session_scope() as db:
        await db.execute(
            update(KbDocument)
            .where(KbDocument.id == doc_id)
            .values(status="failed", error=error[:1000])
        )


async def mark_pending(doc_id: int) -> None:
    """重试：回到待处理队列。"""
    async with session_scope() as db:
        await db.execute(
            update(KbDocument)
            .where(KbDocument.id == doc_id)
            .values(status="pending", error="")
        )


async def list_pending_documents(limit: int = 10) -> list[tuple[int, int]]:
    """取出待处理的 (doc_id, user_id)。"""
    async with session_scope() as db:
        result = await db.execute(
            select(KbDocument.id, KbDocument.user_id)
            .where(KbDocument.status == "pending")
            .order_by(KbDocument.id)
            .limit(limit)
        )
        return [(int(r[0]), int(r[1])) for r in result.all()]


async def reset_stuck_indexing() -> int:
    """
    启动自愈：进程上次是崩在入库过程中时，会留下 indexing 状态的文档。

    有 content_text 的说明解析已完成，退回 pending 继续跑向量化；
    没有的说明崩在解析阶段，标记失败并提示用户重新上传。
    """
    async with session_scope() as db:
        rows = (
            await db.execute(
                select(KbDocument).where(KbDocument.status == "indexing")
            )
        ).scalars().all()

        for row in rows:
            if (row.content_text or "").strip():
                row.status = "pending"
            else:
                row.status = "failed"
                row.error = "入库过程被中断，请重新上传该文件"
        return len(rows)


# ============================================================
# 分块
# ============================================================

async def delete_chunks(doc_id: int) -> int:
    """删除某文档的全部分块与对应的 FTS 行。"""
    async with session_scope() as db:
        ids = (
            await db.execute(
                select(KbChunk.id).where(KbChunk.doc_id == doc_id)
            )
        ).scalars().all()
        if ids:
            await db.execute(delete(KbChunk).where(KbChunk.doc_id == doc_id))
            # FTS5 虚表不支持参数化 IN，用 rowid 逐条删（数量有限，开销可接受）
            for chunk_id in ids:
                await db.execute(
                    text("DELETE FROM kb_fts WHERE rowid = :rid"),
                    {"rid": int(chunk_id)},
                )
        return len(ids)


async def find_reusable_vectors(user_id: int, hashes: list[str]) -> dict[str, bytes]:
    """
    块级查重：同一用户下已经有内容相同、且已算出向量的块，
    直接复用它的向量，省掉一次 embedding 调用。

    一次查完所有 hash，避免逐块查询产生 N 次往返。
    """
    if not hashes:
        return {}
    async with session_scope() as db:
        result = await db.execute(
            select(KbChunk.content_hash, KbChunk.embedding)
            .where(
                KbChunk.user_id == user_id,
                KbChunk.content_hash.in_(hashes),
                KbChunk.embedding.is_not(None),
            )
        )
        reused: dict[str, bytes] = {}
        for content_hash, blob in result.all():
            reused.setdefault(content_hash, blob)
        return reused


async def insert_chunks(
    doc_id: int,
    user_id: int,
    rows: list[dict],
) -> list[int]:
    """
    批量写入分块，返回自增 id 列表（顺序与入参一致）。

    rows 每项：{idx, text, content_hash, token_count, embedding(bytes|None), dimension}
    """
    ids: list[int] = []
    async with session_scope() as db:
        for item in rows:
            row = KbChunk(
                doc_id=doc_id,
                user_id=user_id,
                idx=item["idx"],
                text=item["text"],
                content_hash=item["content_hash"],
                token_count=item.get("token_count", 0),
                embedding=item.get("embedding"),
                dimension=item.get("dimension", 0),
            )
            db.add(row)
            await db.flush()
            ids.append(int(row.id))
            # FTS 行与 chunk id 对齐，供关键词通道检索
            await db.execute(
                text("INSERT INTO kb_fts(rowid, text) VALUES (:rid, :txt)"),
                {"rid": int(row.id), "txt": item["text"]},
            )
    return ids


async def update_chunk_embedding(chunk_id: int, embedding: bytes, dimension: int) -> None:
    async with session_scope() as db:
        await db.execute(
            update(KbChunk)
            .where(KbChunk.id == chunk_id)
            .values(embedding=embedding, dimension=dimension)
        )


async def update_chunk_embeddings(rows: list[tuple[int, bytes, int]]) -> None:
    """批量回写向量。rows 为 (chunk_id, blob, dimension)。"""
    if not rows:
        return
    async with session_scope() as db:
        for chunk_id, blob, dimension in rows:
            await db.execute(
                update(KbChunk)
                .where(KbChunk.id == chunk_id)
                .values(embedding=blob, dimension=dimension)
            )


async def list_chunks(doc_id: int, limit: int = 200) -> list[dict]:
    async with session_scope() as db:
        result = await db.execute(
            select(KbChunk)
            .where(KbChunk.doc_id == doc_id)
            .order_by(KbChunk.idx)
            .limit(limit)
        )
        return [
            {
                "id": row.id,
                "idx": row.idx,
                "text": row.text,
                "token_count": row.token_count,
                "has_vector": row.embedding is not None,
            }
            for row in result.scalars().all()
        ]


async def list_chunks_for_user(user_id: int) -> list[dict]:
    """取某个用户的全部分块（含向量副本），供重建向量索引用。"""
    async with session_scope() as db:
        result = await db.execute(
            select(KbChunk)
            .where(KbChunk.user_id == user_id)
            .order_by(KbChunk.doc_id, KbChunk.idx)
        )
        return [
            {
                "id": row.id,
                "doc_id": row.doc_id,
                "text": row.text,
                "embedding": row.embedding,
            }
            for row in result.scalars().all()
        ]


async def find_duplicate_document(
    user_id: int,
    content_hash: str,
    *,
    exclude_doc_id: int,
) -> KbDocument | None:
    """
    文档级查重（解析完成后才知道真实 hash，所以在这里查）。

    占位 hash（pending-xxx）不参与比较；同样的占位值只会重复出现
    在还没解析的文档上，不属于重复内容。
    """
    if not content_hash or content_hash.startswith("pending-"):
        return None

    async with session_scope() as db:
        result = await db.execute(
            select(KbDocument)
            .where(
                KbDocument.user_id == user_id,
                KbDocument.content_hash == content_hash,
                KbDocument.id != exclude_doc_id,
                KbDocument.status != "failed",
            )
            .limit(1)
        )
        return result.scalar_one_or_none()


async def load_chunks_by_ids(chunk_ids: list[int]) -> list[dict]:
    """按 chunk id 取回正文与所属文档信息（检索结果回填用）。"""
    if not chunk_ids:
        return []
    async with session_scope() as db:
        result = await db.execute(
            select(
                KbChunk.id,
                KbChunk.doc_id,
                KbChunk.idx,
                KbChunk.text,
                KbChunk.token_count,
                KbDocument.title,
                KbDocument.user_id,
                KbDocument.status,
            )
            .join(KbDocument, KbDocument.id == KbChunk.doc_id)
            .where(
                KbChunk.id.in_(chunk_ids),
                # 只有已完成入库的文档才可被检索命中：
                # 向量化中途失败的文档会有残留的 chunk 行与向量，
                # 不过滤就会出现"删不掉、搜得到"的诡异内容
                KbDocument.status == "ready",
            )
        )
        rows = {}
        for row in result.all():
            rows[int(row[0])] = {
                "chunk_id": int(row[0]),
                "doc_id": int(row[1]),
                "idx": int(row[2]),
                "text": row[3],
                "token_count": int(row[4] or 0),
                "title": row[5],
                "user_id": int(row[6]),
                "doc_status": row[7],
            }
        return [rows[cid] for cid in chunk_ids if cid in rows]


async def search_chunks_fts(
    user_id: int,
    query: str,
    limit: int = 30,
) -> list[tuple[int, float]]:
    """
    关键词通道：FTS5 + BM25。

    trigram 分词器要求查询串至少 3 个字符，中文两字查询（"邮件""日程"）
    匹配不到，这里降级成 LIKE。
    """
    async with session_scope() as db:
        if len(query.strip()) >= 3:
            try:
                result = await db.execute(
                    text(
                        "SELECT kb_fts.rowid AS rid, bm25(kb_fts) AS score "
                        "FROM kb_fts "
                        "JOIN kb_chunks ON kb_chunks.id = kb_fts.rowid "
                        "JOIN kb_documents ON kb_documents.id = kb_chunks.doc_id "
                        "WHERE kb_fts MATCH :q AND kb_chunks.user_id = :uid "
                        "AND kb_documents.status = 'ready' "
                        "ORDER BY score LIMIT :lim"
                    ),
                    {"q": _fts_query(query), "uid": user_id, "lim": limit},
                )
                # bm25 越小越相关，取负号让"分数越大越相关"与向量通道一致
                return [(int(r[0]), -float(r[1])) for r in result.all()]
            except Exception:  # noqa: BLE001 - 查询语法异常时退回 LIKE
                pass

        result = await db.execute(
            text(
                "SELECT kb_chunks.id AS rid, 0.0 AS score FROM kb_chunks "
                "JOIN kb_documents ON kb_documents.id = kb_chunks.doc_id "
                "WHERE kb_chunks.user_id = :uid "
                "AND kb_chunks.text LIKE :q ESCAPE '\\' "
                "AND kb_documents.status = 'ready' "
                "LIMIT :lim"
            ),
            {"uid": user_id, "q": _like_pattern(query), "lim": limit},
        )
        return [(int(r[0]), float(r[1])) for r in result.all()]


def _like_pattern(raw: str) -> str:
    """
    把用户输入包成 LIKE 模式串。

    输入里的 % 和 _ 是 LIKE 的通配符，不转义就会让"50%折扣"这类查询
    变成任意匹配（不是注入，但结果是错的）。反斜杠自身要最先转义，
    并配合查询里的 ESCAPE '\\' 生效。
    """
    escaped = (
        (raw or "").strip()
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _fts_query(raw: str) -> str:
    """
    把用户输入变成安全的 FTS5 MATCH 表达式。

    FTS5 的查询语法把 " * ( ) : ^ - 等字符当运算符，直接拼进去会抛语法错，
    这里整体加双引号当短语处理，并把内部的双引号转义掉。
    """
    cleaned = raw.strip().replace('"', '""')
    return f'"{cleaned}"'


async def stats(user_id: int) -> dict:
    async with session_scope() as db:
        doc_count = await db.scalar(
            select(func.count(KbDocument.id)).where(KbDocument.user_id == user_id)
        )
        chunk_count = await db.scalar(
            select(func.count(KbChunk.id)).where(KbChunk.user_id == user_id)
        )
        total_chars = await db.scalar(
            select(func.coalesce(func.sum(KbDocument.char_count), 0)).where(
                KbDocument.user_id == user_id
            )
        )
    return {
        "documents": int(doc_count or 0),
        "chunks": int(chunk_count or 0),
        "chars": int(total_chars or 0),
    }
