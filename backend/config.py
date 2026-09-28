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
    openai_base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"

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

    # ---------- 上下文管理 ----------
    # 发给 LLM 的历史消息 token 预算
    max_history_tokens: int = 6000
    # 消息条数超过此阈值触发旧消息摘要
    summarize_threshold: int = 30
    # 摘要时保留最近多少条消息不被压缩
    summarize_keep_recent: int = 10
    # 单个工具结果的最大字符数（超出截断）
    tool_result_max_chars: int = 800

    # ---------- 认证 ----------
    # token 有效期（默认 7 天）
    token_ttl_seconds: int = 7 * 24 * 3600
    # 密码 hash 的全局盐（生产环境应随机生成并妥善保管）
    password_salt: str = "change-me-please"

    # .env 加载配置；extra="ignore" 允许 .env 有额外字段不报错
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


# 全局单例，其他模块直接 import settings 使用
settings = Settings()