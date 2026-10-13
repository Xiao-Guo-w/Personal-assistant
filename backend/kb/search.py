"""
混合检索：向量 + BM25 双通道，RRF 融合，自适应截断。

各阶段的作用：

1. 双通道召回。向量抓语义改写（"报销流程"能命中"费用申请审批步骤"），
   BM25 抓专名、编号、代码片段 —— 两者互补而非竞争。
   附带好处：向量服务挂了会自动降级成纯关键词，功能不会整体不可用。

2. 多路 query。查询改写产出的自包含语句与用户原话**各检索一遍再融合**，
   而不是用改写结果替换原话。改写会引入幻觉，替换掉原话就把它一起请进结果了。

3. RRF 融合。只用排名不用分数，因此不需要把余弦相似度和 BM25 分数
   归一化到同一量纲 —— 归一化方式一换、权重就得重调，非常脆。

   注意：正因为它只用排名，**融合后的分数不含相关性信息**，
   所以下面的自适应截断不能拿它当信号，必须回到原始余弦相似度。

4. 相邻块合并。分块有 15% 重叠，排名靠前的几块很可能是同一段落的副本。

5. 单文档份额上限。长文档很容易霸占全部名额。

6. 自适应截断。token 预算 + 条数上限 + 相似度断崖三重约束，
   命中质量高时少返回、质量平平时多返回，而不是固定切 top-k。
"""

import logging

from ..config import settings
from . import store
from .chunking import count_tokens
from .deps import embedder, vectorstore
from .embeddings import EmbeddingError
from .vectorstore import VectorStoreUnavailable

logger = logging.getLogger(__name__)


async def search(
    user_id: int,
    query: str,
    *,
    top_k: int | None = None,
    token_budget: int | None = None,
    extra_queries: list[str] | None = None,
) -> list[dict]:
    """
    检索用户知识库，返回按相关度排序的片段。

    extra_queries 用于传入查询改写的产物：它与原 query 一同检索后融合。
    """
    queries = _dedupe([query, *(extra_queries or [])])
    if not queries:
        return []

    max_hits = top_k or settings.kb_top_k
    budget = token_budget or settings.kb_token_budget
    pool = max(settings.kb_candidate_pool, max_hits)

    rankings: list[tuple[list[tuple[int, float]], float]] = []
    # 原始余弦相似度，供自适应截断使用（RRF 分数不含相关性信息）
    similarities: dict[int, float] = {}

    query_vectors: list[list[float]] = []
    if embedder.configured:
        try:
            query_vectors = await embedder.embed_documents(queries)
        except (EmbeddingError, VectorStoreUnavailable) as e:
            logger.warning("查询向量化失败，本次降级为纯关键词检索：%s", e)
            query_vectors = []

    for index, text in enumerate(queries):
        keyword_hits = await store.search_chunks_fts(user_id, text, limit=pool)
        if keyword_hits:
            rankings.append((keyword_hits, 1.0))

        if index >= len(query_vectors):
            continue
        try:
            vector_hits = await vectorstore.query(
                user_id, query_vectors[index], top_k=pool,
            )
        except VectorStoreUnavailable as e:
            logger.warning("向量检索不可用，本次降级为纯关键词检索：%s", e)
            break

        numeric = [
            (int(chunk_id), score)
            for chunk_id, score in vector_hits
            if chunk_id.isdigit()
        ]
        if numeric:
            rankings.append((numeric, settings.kb_vector_weight))
            for chunk_id, score in numeric:
                similarities[chunk_id] = max(similarities.get(chunk_id, -1.0), score)

    if not rankings:
        return []

    fused = _rrf(rankings, settings.kb_rrf_k)
    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)

    # 回填正文。非 ready 文档、以及向量库里残留的孤儿 id 都在这里被过滤掉
    rows = await store.load_chunks_by_ids([chunk_id for chunk_id, _ in ordered])
    by_id = {row["chunk_id"]: row for row in rows}

    hits = [
        {
            **by_id[chunk_id],
            "score": score,
            "similarity": similarities.get(chunk_id),
        }
        for chunk_id, score in ordered
        if chunk_id in by_id
    ]
    if not hits:
        return []

    hits = _merge_adjacent(hits)
    hits = _cap_per_doc(hits, settings.kb_max_chunks_per_doc)
    return _select_hits(hits, max_hits, budget)


def _dedupe(items: list[str]) -> list[str]:
    """去重保序，丢掉空白项。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = (item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _rrf(
    rankings: list[tuple[list[tuple[int, float]], float]],
    k: int,
) -> dict[int, float]:
    """Reciprocal Rank Fusion：score(d) = Σ weight / (k + rank(d))。"""
    fused: dict[int, float] = {}
    for hits, weight in rankings:
        for rank, (chunk_id, _score) in enumerate(hits, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + weight / (k + rank)
    return fused


def _merge_adjacent(hits: list[dict]) -> list[dict]:
    """把同一文档里 idx 相邻的命中块合并成一段。"""
    grouped: dict[int, list[dict]] = {}
    for hit in hits:
        grouped.setdefault(hit["doc_id"], []).append(hit)

    merged: list[dict] = []
    for items in grouped.values():
        items.sort(key=lambda item: item["idx"])
        current: dict | None = None
        for item in items:
            if current is not None and item["idx"] == current["last_idx"] + 1:
                current["text"] = f"{current['text']}\n\n{item['text']}"
                current["chunk_ids"].append(item["chunk_id"])
                current["last_idx"] = item["idx"]
                current["score"] = max(current["score"], item["score"])
                current["similarity"] = _max_similarity(
                    current.get("similarity"), item.get("similarity"),
                )
            else:
                if current is not None:
                    merged.append(current)
                current = {
                    **item,
                    "chunk_ids": [item["chunk_id"]],
                    "last_idx": item["idx"],
                }
        if current is not None:
            merged.append(current)

    merged.sort(key=lambda item: item["score"], reverse=True)
    return merged


def _max_similarity(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _cap_per_doc(hits: list[dict], cap: int) -> list[dict]:
    """限制单篇文档贡献的片段数。"""
    if cap <= 0:
        return hits
    used: dict[int, int] = {}
    kept: list[dict] = []
    for hit in hits:
        count = used.get(hit["doc_id"], 0)
        if count >= cap:
            continue
        used[hit["doc_id"]] = count + 1
        kept.append(hit)
    return kept


def _select_hits(hits: list[dict], max_hits: int, budget: int) -> list[dict]:
    """
    自适应截断。

    三条约束按优先级：
    1. 条数上限 —— 再多也不超过 max_hits
    2. 相似度断崖 —— 已有命中、且当前命中明显不如最佳时停下。
       这是"自适应"的核心：命中质量高就少返回，平平时才多返回
    3. token 预算 —— 上下文成本的硬上限

    两条刻意保留的例外：
    - 第一条命中无论如何都收下，否则单块超预算就会变成"什么都没搜到"
    - 没有相似度的命中（只被关键词通道命中）不参与断崖判断，
      否则纯关键词模式和关键词独有命中会被误杀
    """
    if not hits:
        return []

    best = max(
        (hit["similarity"] for hit in hits if hit.get("similarity") is not None),
        default=None,
    )
    floor = settings.kb_min_score
    if best is not None:
        floor = max(floor, best * settings.kb_score_gap_ratio)

    kept: list[dict] = []
    used = 0
    for hit in hits:
        if len(kept) >= max_hits:
            break

        similarity = hit.get("similarity")
        if kept and similarity is not None and similarity < floor:
            break

        tokens = count_tokens(hit["text"])
        if kept and used + tokens > budget:
            break

        hit["tokens"] = tokens
        kept.append(hit)
        used += tokens

    return kept
