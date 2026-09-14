"""全局配置中心：pydantic-settings 读环境变量。"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。字段来自 .env / 环境变量，自带默认值便于本地无 key 演示。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务
    PORT: int = 8787
    WORKSPACE_ROOT: Path = Path("./workspace")
    APP_VERSION: str = "0.1.0"

    # LLM（模型配置 = 全局默认；运行时参数可覆盖，见 llm.py）
    LLM_PROVIDER: str = "openai"
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TEMPERATURE: float = 0.3
    LLM_MAX_TOKENS: int = 2048

    # 基建
    DATABASE_URL: str = (
        "postgresql+asyncpg://zeffy:zeffy@localhost:5433/zeffy_workplace"
    )
    REDIS_URL: str = "redis://localhost:6380"
    # P0 不校验 Qdrant 连通，P1 才连接
    QDRANT_URL: str = "http://localhost:6333"

    # 可观测（P3 启用）
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = ""

    # 上下文压缩（P1 启用）
    CONTEXT_MAX_ROUNDS: int = 30
    CONTEXT_MAX_TOKENS: int = 8000

    # P1-2 调试用：允许手动推进工作流节点。仅本地开发，P1-5 后应关闭。
    ENABLE_DEBUG_ADVANCE: bool = True

    # ---- P2：ARQ 持久任务队列 + 事件回传 ----
    USE_QUEUE: bool = True  # false 则回退到 P1 in-process TaskRunner（回滚开关）
    ARQ_QUEUE_NAME: str = "zeffy"
    TASK_EVENT_CHANNEL: str = "zw:tasks"
    WORKER_ID: str = ""  # 空则启动时自生成 <hostname>-<pid>-<rand6>（P3 多 worker 唯一标识）
    ARQ_JOB_TIMEOUT: int = 900  # 单节点 run 的宽裕超时（秒）
    ARQ_MAX_TRIES: int = 3  # job 级最大重试次数
    ARQ_BACKOFF: float = 2.0  # 指数退避基秒
    # 🔴 一致性巡检：queued 滞留超过该时长才重新入队（避免与刚入队的活跃 job 竞争）
    QUEUED_STALE_SECONDS: int = 60
    # ---- P3 多 worker / 观察 ----
    ARQ_MAX_JOBS: int = 4  # 每 worker 并发上限（多 worker 并行消费）
    DB_POOL_SIZE: int = 10  # 数据库连接池大小（按 worker 规模配比）
    DEAD_SCAN_INTERVAL: int = 45  # 死任务/恢复扫描周期（秒），< lease TTL 防频繁
    # 恢复扫描分布式锁（Redis SET NX EX）TTL（秒）；仅一个实例持有锁执行全局扫描
    DEAD_SCAN_LOCK_TTL: int = 40
    LEASE_TTL: int = 0  # 0 时用 ARQ_JOB_TIMEOUT*1.5
    # ---- P3 鉴权 ----
    AUTH_ENABLED: bool = False  # 兼容锚点：False 行为与 P2 完全一致
    AUTH_TOKEN_TTL: int = 3600 * 24 * 7  # token 有效期（秒）
    REGISTRATION_ENABLED: bool = False  # 默认关闭公开注册（防滥用）
    PASSWORD_ITERATIONS: int = 100000  # pbkdf2_hmac_sha256 迭代次数（≥100000）

    @property
    def worker_id(self) -> str:
        """多 worker 唯一身份：hostname-pid-rand6；可被 WORKER_ID 显式覆盖。"""
        if self.WORKER_ID:
            return self.WORKER_ID
        import os
        import secrets
        import socket

        return f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"

    @property
    def lease_ttl(self) -> int:
        return self.LEASE_TTL or int(self.ARQ_JOB_TIMEOUT * 1.5)

    @property
    def llm_api_key_set(self) -> bool:
        return bool(self.LLM_API_KEY and self.LLM_API_KEY != "your-key-here")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# 模块级便利引用
settings = get_settings()
