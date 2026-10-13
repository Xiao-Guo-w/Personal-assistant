"""
知识库的单例依赖。

单独成模块是为了打断循环导入：ingest / search / service 都要用到
embedder 与 vectorstore，但不应该互相 import。
"""

import asyncio

from ..config import settings
from .embeddings import Embedder
from .vectorstore import VectorStore

# 向量化客户端。三项配置留空时 configured 为 False，检索自动降级为纯关键词
embedder = Embedder(
    base_url=settings.embedding_base_url,
    api_key=settings.embedding_api_key,
    model=settings.embedding_model,
    dim=settings.embedding_dim,
    batch_size=settings.embedding_batch_size,
    timeout=settings.embedding_timeout_seconds,
    max_retries=settings.embedding_max_retries,
)

vectorstore = VectorStore(settings.kb_chroma_path)

# 入库 worker 的唤醒信号。上传/重试后 set()，worker 收到就立刻开工，
# 不需要等下一次轮询。
ingest_wake = asyncio.Event()


def vector_enabled() -> bool:
    """向量通道是否可用（配置齐全且 chromadb 能装上）。"""
    return settings.kb_enabled and embedder.configured


def kick_ingest() -> None:
    """唤醒入库 worker。"""
    ingest_wake.set()
