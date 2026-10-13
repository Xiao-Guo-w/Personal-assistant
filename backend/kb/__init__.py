"""
知识库（RAG）子包。

模块分工：
- parsers     文件 → 纯文本
- chunking    纯文本 → 分块（结构优先）
- embeddings  OpenAI 兼容的向量化客户端
- vectorstore Chroma 客户端封装（可离线重建的派生索引）
- store       SQLite 读写（权威数据源）
- ingest      后台入库 worker + 状态机
- search      混合检索（向量 + BM25 + RRF）
"""
