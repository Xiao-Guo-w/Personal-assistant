"""
全局配置。

通过 pydantic-settings 从环境变量 / .env 读取，
部署时只改环境变量即可，代码零改动。

所有字段都有默认值，本地开发零配置就能跑。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ---------- LLM ----------
    openai_api_key: str = ""
    openai_base_url: str = "https://api.agnes-ai.cn/v1"
    model: str = "agnes-2.5-flash"
    # Agnes Thinking 模式：编码 / 调试 / 多步 Agent 可开
    enable_thinking: bool = False
    # 单次最大输出 token。Agnes 最大输出 65.5K，但业务不需要拉满
    max_output_tokens: int = 8192

    # ---------- 数据库 ----------
    # 两个独立的 SQLite 文件：
    # - db_path：业务表（用户 / 会话 / 幂等 / 审计 / 记忆）
    # - checkpoint_db：LangGraph 图状态

    db_path: str = "assistant.db"
    checkpoint_db: str = "checkpoints.db"

    # ---------- 业务参数 ----------
    # 危险操作确认有效期：超时后 pending_action 作废，避免老参数被误执行
    confirm_ttl_seconds: int = 300
    # 工具调用失败的最大重试次数（含首次）
    max_retries: int = 3

    # ---------- 提醒调度 ----------
    # 后台扫描间隔（秒）：越小越准时，越大越省资源；最小 5 秒
    reminder_scan_interval_seconds: int = 30
    # 关掉后提醒只落库不投递（本地调试用），默认开启
    reminder_scheduler_enabled: bool = True

    # ---------- 上下文管理 ----------
    # Agnes 上下文窗口 512K，这里给业务侧留出充足但不过量的预算
    max_history_tokens: int = 32000
    # 消息条数超过此阈值触发旧消息摘要
    summarize_threshold: int = 60
    # 摘要时保留最近多少条消息不被压缩
    summarize_keep_recent: int = 20
    # 单个工具结果的最大字符数
    tool_result_max_chars: int = 1600

    # ---------- 认证 ----------
    # token 有效期（默认 7 天）
    token_ttl_seconds: int = 7 * 24 * 3600
    # 密码 hash 的全局盐（生产环境应随机生成并妥善保管）
    password_salt: str = "change-me-please"

    # ---------- 加密 ----------
    encryption_key: str = ""

    # ---------- 知识库（RAG） ----------
    # 总开关：关闭后不下发检索工具、不启动入库 worker
    kb_enabled: bool = True
    # Chroma 持久化目录。和 SQLite 一样必须放本地磁盘，不能放网络盘
    kb_chroma_path: str = "chroma_db"
    # 原生文件解析后的文本存哪：直接存在 kb_documents.content_text
    # （重试与"换分块参数重建"都要靠它，避免要求用户重新上传）

    # 向量化服务（OpenAI 兼容协议）。三项留空则自动降级为纯关键词检索
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    # 0 表示首次调用时自动探测维度
    embedding_dim: int = 0
    embedding_batch_size: int = 32
    embedding_timeout_seconds: int = 30
    # 单条 embedding 请求失败后的重试次数（含首次）
    embedding_max_retries: int = 3

    # ---------- 分块 ----------
    # 目标块大小与重叠（token）。结构优先切分，超长段落才按 token 硬切
    kb_chunk_size: int = 500
    kb_chunk_overlap: int = 80
    # 小于此 token 数的碎片会并入相邻块
    kb_min_chunk_tokens: int = 40

    # ---------- 检索 ----------
    kb_top_k: int = 5
    # 进入 RRF 融合前，每个通道各取多少候选
    kb_candidate_pool: int = 30
    # RRF 平滑常数
    kb_rrf_k: int = 60
    # 向量通道权重（关键词通道固定为 1.0）
    kb_vector_weight: float = 1.0
    # 拼进上下文的总 token 预算，超出即截断
    kb_token_budget: int = 2000
    # 单篇文档最多贡献几个块，防止长文档霸榜
    kb_max_chunks_per_doc: int = 3
    # 返回给模型的单个片段最大字符数
    kb_snippet_max_chars: int = 500

    # ---------- 自适应截断 ----------
    # 余弦相似度绝对下限。低于它的片段一律丢弃（第一条除外，避免全空）
    kb_min_score: float = 0.3
    # 相对断崖：相似度低于"最佳命中的这个比例"就不再往下取。
    # 0.45 表示最佳命中 0.8 时，低于 0.36 的就不再看了
    kb_score_gap_ratio: float = 0.45

    # ---------- 查询改写 ----------
    # 多轮对话里出现指代/省略时，先改写成自包含的检索语句。
    # 关掉可以省一次模型调用
    kb_query_rewrite: bool = True
    # 改写调用超时（秒）。超时就直接用原 query，不拖慢对话
    # 改写发生在检索之前、属于"看不见的等待"，给太宽等于把延迟让给它
    kb_rewrite_timeout_seconds: int = 4

    # ---------- 上传 ----------
    kb_max_file_mb: int = 20
    # 入库 worker 的兜底轮询间隔（秒）。正常情况下由 wake 信号即时触发，
    # 这个间隔只用于"漏掉的 pending 文档"的兜底捡起与开机自愈
    kb_worker_interval_seconds: int = 15

    # .env 加载配置；extra="ignore" 允许 .env 有额外字段不报错
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# 全局单例，其他模块直接 import settings 使用
settings = Settings()
