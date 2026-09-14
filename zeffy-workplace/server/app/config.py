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
    # P4 trace 接入外部日志：true 时输出单行 JSON（含 trace_id），供 ELK/Loki/OTel 采集关联
    LOG_JSON_OUTPUT: bool = False

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
    # ---- P3-2 监控 / 告警 ----
    METRICS_INTERVAL: int = 30  # 指标/告警后台采集周期（秒）；/metrics 返回缓存值而非实时查库
    METRICS_TREND_MINUTES: int = 10  # 吞吐/失败率统计窗口（分钟）
    ALERT_QUEUE_THRESHOLD: int = 20  # 滞留 queued 节点数超阈值触发「队列积压」告警
    ALERT_FAILURE_THRESHOLD: float = 0.5  # 节点失败率（0~1）超阈值触发「失败率」告警
    ALERT_COOLDOWN: int = 300  # 同指标告警/恢复冷却（秒），Redis 冷却键防刷屏
    WORKER_HEARTBEAT_TTL: int = 90  # worker 心跳存活（秒）；离线后该时长未被识别
    # ---- P4-1 集群化部署 ----
    ENABLE_ADMIN: bool = False  # /admin/* 路由开关（默认关 → 404），实例注册仅后台运行
    INSTANCE_HEARTBEAT_TTL: int = 90  # 实例(api/worker)心跳存活（秒），TTL=心跳×3
    MAX_CLOCK_SKEW: float = 5.0  # 启动时钟校验：与 Redis 服务端时钟偏差上限（秒），超限拒绝启动
    # ---- P4-2 告警外部通知（默认零通知，向后兼容）----
    NOTIFY_RULES: list[dict] = []  # [{"metric":"queue_depth","level":"warning","channel":"webhook"}]，空=不通知
    NOTIFY_WEBHOOK_URLS: list[str] = []  # 通用 webhook 端点（POST JSON）
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_TO: list[str] = []  # 收件人列表
    NOTIFY_MAX_PER_MIN: int = 30  # 单通道每分钟最大通知数（风暴抑制）
    NOTIFY_RETRIES: int = 3  # 发送失败重试次数（指数退避）
    NOTIFY_BACKOFF_BASE: float = 2.0  # 重试退避基秒
    # ---- P4-4 成本统计（元/百万 token；缺价格记 0）----
    MODEL_PRICING: dict = {}  # {"model": {"prompt_per_1m":..,"completion_per_1m":..}}
    BILLING_WINDOW_DAYS: int = 30  # 聚合时间窗（天）；更长周期留 P5 离线
    # ---- P4-4b 任务优先级：分级队列 + LLM 分级配额 ----
    ARQ_PRIORITY_ENABLED: bool = False  # 兼容锚点：关=单队列（P4 行为）
    LLM_QUOTA_HI: int = 5  # 高优 LLM 并发配额
    LLM_QUOTA_MID: int = 3  # 中优配额
    LLM_QUOTA_LO: int = 2  # 低优保底配额（不对外出借，防饿死）
    LLM_QUOTA_PUBLIC: int = 0  # 公共池；0 = 总并发 - (hi+mid+lo)，高/中优满时借用
    # ---- P5 分布式产物存储（默认 local，与 P4 零漂移）----
    STORAGE_BACKEND: str = "local"  # local | s3
    STORAGE_ROOT: str = ""  # local 后端根；空=WORKSPACE_ROOT（P4 零漂移）；可挂共享卷 NFS/EFS 跨机
    S3_ENDPOINT: str = ""  # 配了该值才启用 s3；留空 = local
    S3_BUCKET: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = ""
    ST_ARTIFACT_PUBLIC_BASE: str = ""  # 对象存储直链/预签名前缀（可选）
    ST_SIGNED_URL_TTL: int = 900  # 预签名 URL 过期秒（默认 15 分钟，🔴4）
    ST_RETENTION_FAILED_DAYS: int = 7  # 失败任务产物保留（⭐5）
    ST_RETENTION_DONE_DAYS: int = 30  # 完成任务产物保留（⭐5）
    ST_GARBAGE_INTERVAL: int = 3600  # 临时文件/过期产物清理周期（🔴5，⭐5）
    ST_TMP_MAX_AGE: int = 86400  # 临时文件早于此秒数自动清理（默认 24h）

    @property
    def instance_id(self) -> str:
        """集群实例全局唯一 ID：hostname-pid-randhex（跨物理节点/进程唯一）。"""
        if self.WORKER_ID:
            return self.WORKER_ID
        import os
        import secrets
        import socket

        # 审查🔴：WORKER_ID 强制 hostname-pid-randhex，注册时校验唯一性
        return f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"

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
