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
    # ---- P5-1 产物版本管理（默认关，关闭=纯透传零漂移）----
    ARTIFACT_VERSIONS_ENABLED: bool = False  # 默认关：overwrite 行为与 P5-0 逐字节一致
    ARTIFACT_MAX_VERSIONS: int = 5  # 每 rel_path 保留最大版本数（≥1，超限淘汰最旧）
    ARTIFACT_VERSION_SIZE_LIMIT: int = 2 * 1024 * 1024  # 大文件不自动归档阈值（2MB，⭐5）
    ARTIFACT_DIFF_MAX_SIZE: int = 2 * 1024 * 1024  # diff 文件大小上限（2MB，超限 too_large，🔴6）
    ARTIFACT_DIFF_PREVIEW_LINES: int = 200  # diff 预览行数上限（🔴6）
    ARTIFACT_PENDING_TTL: int = 3600  # pending 记录巡检阈值（秒，超时清理，🔴1）
    ARTIFACT_RECONCILE_INTERVAL: int = 86400  # 存储与 DB 对账周期（秒，默认每日，⭐4）
    # ---- P5-4 断点续传（Range，默认关零漂移；前端无 Accept-Ranges 自动降级）----
    RANGE_ENABLED: bool = False  # 后端 Range 支持开关；默认关（无 Range 头行为与 P5-0 一致）
    RANGE_MAX_SIZE: int = 50 * 1024 * 1024  # 前端断点续传最大文件阈值（50MB，超限降级全量，🔴4）
    RANGE_MAX_CONCURRENCY: int = 1  # 前端并行分块上限（审查 2.4🔴4；默认=1 纯串行，与 P5-4 一致）
    # ---- P5-5 上传断点续传（默认关零漂移；可继续的分块上传链路）----
    UPLOAD_ENABLED: bool = False  # 上传 API 开关；关时 /artifacts/upload* 一律 404
    UPLOAD_CHUNK: int = 8 * 1024 * 1024  # 分块大小（8MB）
    UPLOAD_MAX_SIZE: int = 2 * 1024 * 1024 * 1024  # 单文件上传上限（2GB）
    UPLOAD_TTL: int = 24 * 60 * 60  # 未 commit 临时区保留时长（秒，24h）
    # ---- P6 产物生命周期治理（默认全关，零漂移锚点）----
    ARTIFACT_META_ENABLED: bool = False  # 元表总开关；关则配额/事务/分层/计量全跳过（兼容锚点）
    # 配额（按 owner_id）
    QUOTA_ENABLED: bool = False
    QUOTA_ASSET_MAX_BYTES: int = 0  # 单产物上限（0=不限制）
    QUOTA_TOTAL_MAX_BYTES: int = 0  # 用户总配额（0=不限制）
    QUOTA_TIER_COLD_FACTOR: float = 0.1  # cold 归档占用折算系数（可选降权）
    QUOTA_EXEMPT_SYSTEM: bool = True  # system 账号豁免
    # ---- P6-2 O2 配额智能（历史采样 + 趋势预测 + 报表 + 成本核算）----
    QUOTA_HISTORY_ENABLED: bool = False  # 历史采样开关；关则采样/报表端点全 404
    QUOTA_HISTORY_INTERVAL: int = 15 * 60  # 采样周期（秒，默认 15min）
    QUOTA_HISTORY_RETENTION_DAYS: int = 90  # 历史保留窗口（天，过期清理）
    QUOTA_HISTORY_POINTS: int = 32  # 预测/曲线取最近采样点数
    QUOTA_HISTORY_PREDICT_R2: float = 0.9  # 预测相关性阈值（r>=该值才输出 ETA）
    QUOTA_ETA_ALERT_THRESHOLD_HOURS: int = 24 * 7  # 预计 7 天内耗尽 → 高优先级
    QUOTA_COST_HOT_PER_GB: float = 0.0  # 热存储单价（元 / GB·周期）
    QUOTA_COST_COLD_PER_GB: float = 0.0  # 冷存储单价（元 / GB·周期）
    QUOTA_COST_PERIOD_DAYS: int = 1  # 成本核算周期（天）
    # ---- P6-2 O4 重复数据去重（默认关零差异；内容寻址物理文件）----
    DEDUP_ENABLED: bool = False  # 去重总开关；关则写/删/冷化/对账全直通，不查 content/不哈希
    DEDUP_MIN_SIZE: int = 1024 * 1024  # 去重最小文件大小（1MB，小文件哈希开销>空间收益)
    DEDUP_ASYNC_MAX_SIZE: int = 64 * 1024 * 1024  # 超过则异步合并(不阻塞写)；默认 64MB
    DEDUP_COLLISION_CHECK: bool = True  # ⭐哈希碰撞双重校验(sha256+size+前1KB特征)
    # 去重范围可配（⭐）
    DEDUP_NAMESPACE_TASKS: str = ""  # 仅对指定 task_id 前缀启用(逗号分隔)；空=全部正式产物
    DEDUP_EXCLUDE_TYPES: str = ""  # 排除的 MIME 类型(逗号)；敏感/小文件可关
    # 存量去重（O4-F）
    DEDUP_BACKFILL_INTERVAL: int = 24 * 60 * 60  # 存量去重守护周期（秒）
    DEDUP_BACKFILL_BATCH: int = 200  # 每批扫描数
    DEDUP_BACKFILL_SLEEP: float = 0.02  # 批间错峰（秒）
    DEDUP_OLD_TTL: int = 24 * 60 * 60  # 存量合并后旧文件延迟删除 TTL（秒）
    # ---- P6-4 B 治理告警阈值（迟滞：触发>阈值，恢复<恢复阈值）----
    ALERT_QUOTA_CRITICAL: float = 95.0  # critical 级别（配额使用率 %）
    ALERT_QUOTA_HIGH: float = 80.0  # high 触发
    ALERT_QUOTA_RECOVER: float = 70.0  # 恢复
    ALERT_TX_FAIL_RATE: float = 0.5  # 事务失败率告警阈值
    ALERT_RECONCILE_MIN: int = 1  # 单轮对账异常(missing+orphan)达到该值告警
    GOV_METRICS_TIMEOUT: int = 5  # E-3 治理指标采集 DB 段超时保护（秒；超时返回上次缓存不阻塞）
    # ---- P6-6-5 加密可观测告警阈值（双阈值 + 滑动窗口 + 分级）----
    ALERT_ENCRYPT_FAIL_RATE: float = 0.02  # 解密失败率阈值（需同时满足最小样本量）
    ENCRYPT_MIN_SAMPLES: int = 10  # 失败率告警最小解密样本量（防小流量误报）
    ALERT_ENCRYPT_TAMPER_MIN: int = 5  # 滑动窗口内篡改次数阈值（集中攻击）
    ALERT_ENCRYPT_DEGRADE_MIN: int = 5  # 滑动窗口内降级明文次数阈值
    ENCRYPT_WINDOW_S: int = 600  # 篡改/降级/失败率滑动时间窗口（秒）
    # ---- P7-C3 密钥健康度巡检 ----
    KEY_PATROL_ENABLED: bool = False  # 巡检总开关；关则巡检协程完全不启动（零 IO/计算）
    KEY_PATROL_INTERVAL_S: int = 3600  # 巡检周期（秒；低峰期执行，默认 1h）
    KEY_PATROL_FIRST_SILENT: bool = True  # 首次巡检存量异常仅记录不告警（避免上线即风暴）
    # 分级差异化冷却（秒）：critical 30min / high 1h / warn 3h —— 严重更及时、低级别更静默
    KEY_PATROL_COOLDOWN_CRITICAL_S: int = 1800
    KEY_PATROL_COOLDOWN_HIGH_S: int = 3600
    KEY_PATROL_COOLDOWN_WARN_S: int = 10800
    # ---- P7-B4 加密异常自动诊断 ----
    ENCRYPT_DIAGNOSE_ENABLED: bool = False  # 诊断总开关；关则失败点零采样、零告警
    ENCRYPT_DIAGNOSE_WINDOW_S: int = 600  # 失败分类滑动窗口（秒）
    ENCRYPT_DIAGNOSE_MIN_HITS: int = 5  # 某类触发告警的最小命中次数（高置信门槛）
    ENCRYPT_DIAGNOSE_CONFIRM_LIFT: int = 3  # 连续同类 N 次 → 置信度上调一级
    ENCRYPT_DIAGNOSE_SPARSE_RATIO: float = 0.3  # 同类占比 < 该值 → 置信度下调一级
    ENCRYPT_DIAGNOSE_HIGH_SCALE: float = 2.0  # 命中 ≥ MIN_HITS*SCALE → 告警升级为 high
    ENCRYPT_DIAGNOSE_COOLDOWN_S: int = 1800  # 同类诊断告警冷却（秒）
    # ---- P6-4-B 灰度中心化（多实例一致性；默认关=进程内，P6-4 零漂移）----
    GOV_CENTRALIZE: bool = False  # 开启后覆盖/灰度以 Redis 为权威源 + pub/sub 失效 + 版本号/定期校验
    GOV_ENV: str = "default"  # 环境隔离前缀 gov:{GOV_ENV}:*，测试/生产不串写
    GOV_REDIS_CHANNEL: str = "gov:central"  # pub/sub 广播频道
    GOV_SYNC_INTERVAL: int = 300  # 定期全量校验周期（秒；最终兜底）
    GOV_SYNC_MIN_INTERVAL: float = 5.0  # 全量同步节流：该秒内至多 1 次全量
    GOV_CACHE_TTL: int = 0  # 本地缓存绝对过期兜底（0=不启用，仅靠消息+定期校验）
    GOV_FADE_MAX_MS: int = 1000  # 失效拉取随机延迟上限（打散防风暴）
    GOV_DEGRADE_ALERT_AFTER: int = 60  # 持续降级超过该秒数升级为 high 告警
    GOV_SNAPSHOT_RETENTION: int = 24 * 60 * 60  # 配置快照保留 TTL（秒）
    # 存储分层
    TIER_ENABLED: bool = False
    TIER_COLD_ARCHIVE_AGE: int = 30 * 24 * 60 * 60  # 冷化年龄（秒，默认30天）
    TIER_COLD_DIR: str = "_cold"  # Local 冷归档目录（相对 STORAGE_ROOT）
    TIER_COLD_S3_CLASS: str = "STANDARD_IA"  # S3 冷归档 StorageClass
    # ---- P6-5 存储容量经济：生命周期自动化 + 分层参数化（默认关，零漂移）----
    S3_LIFECYCLE_ENABLED: bool = False  # S3 生命周期(cold→IA)自动化；Local/未配 S3 忽略
    S3_LIFECYCLE_EXPIRE: bool = False  # 生命周期是否过期删除（默认关，删除统一走 GC）
    TIER_LIFECYCLE_SCAN_INTERVAL: int = 24 * 60 * 60  # 生命周期/物理分层对账周期（秒）
    TIER_NAMESPACE_POLICY: str = ""  # JSON {ns_prefix: {"warm_after": 秒, "cold_after": 秒, "ice_after": 秒}}
    TIER_PHYSICAL_DRIFT_ALERT: int = 10  # 物理存储类 vs 元数据 tier 偏差超过该数告警
    # ---- P6-6-1 治理策略引擎（配额/分层/保留 参数化为 JSON；默认关零漂移）----
    POLICY_ENGINE_ENABLED: bool = False  # 启用策略引擎覆盖全局治理参数
    POLICY_JSON: str = ""  # JSON {kind: {scope_prefix: {field: value}}}，scope 取 task/owner 前缀
    # 策略可覆盖字段白名单（防止策略注入未支持字段）：kind -> fields
    POLICY_ALLOWED_FIELDS: dict = {
        "quota": {"total_max_bytes", "gray_list", "exempt_system", "asset_max_bytes"},
        "tier": {"warm_after", "cold_after", "pinned_max_count", "pinned_max_ratio"},
        "retention": {"done_days", "failed_days"},
    }
    POLICY_AUDIT_INTERVAL: int = 30  # 策略失败/冲突审计节流间隔（秒，防热路径刷审计）
    # ---- P6-6-3 深冷层(ice)与冷读恢复（默认关，Local 退化 cold，零漂移）----
    TIER_ICE_ENABLED: bool = False  # 启用 ice 深冷层 + 冷读 restore（需 S3）
    TIER_ICE_S3_CLASS: str = "GLACIER"  # ice 深冷存储类
    TIER_ICE_AGE: int = 90 * 24 * 60 * 60  # cold→ice 过渡年龄（秒）
    QUOTA_TIER_ICE_FACTOR: float = 0.1  # ice 配额折算系数（比 cold 低，引导归档）
    RESTORE_COOLDOWN: int = 300  # restore 触发冷却（秒，Redis SETNX 去重+防风暴）
    RESTORE_EXPIRE_S: int = 12 * 3600  # restore 解冻成功后可读窗口（秒），到期自动回 ice
    RESTORE_TIER: str = "Standard"  # restore 取回等级：Standard | Expedited
    RESTORE_LOCK_TTL: int = 1800  # restore 分布式锁超时（秒，防死锁）
    RESTORE_COST_PER_OBJECT: float = 0.01  # 单次 restore 预估费用（USD，成本估算与审计）
    # ---- P6-6-4 数据加密（默认关零漂移；最高安全模块，10项P0闭环后启用）----
    ARTIFACT_ENCRYPT_ENABLED: bool = False  # 产物加密总开关
    ENCRYPT_BLOCK_BYTES: int = 262144  # 分块大小（每块独立 nonce+tag）
    ENCRYPT_CIPHER_VERSION: int = 1  # 当前加密版本（单调，禁止回退）
    ENCRYPT_LEGACY_VERSIONS: str = ""  # 可解密的旧版本白名单（逗号分隔，含1）
    ENCRYPT_MASTER_KEYFILES: str = ""  # 主密钥文件路径列表(≥2副本，逗号分隔，0600)
    ENCRYPT_HMAC_KEYFILE: str = ""  # 元数据 HMAC 密钥文件（独立于 DEK 主密钥）
    # ---- P6-6-6 密钥轮换与生命周期 ----
    ENCRYPT_LEGACY_KEYFILES: str = ""  # 历史版本主密钥归档（"v:path1,path2;v2:path1,path2"，仅解密）
    ENCRYPT_KEY_ROTATE_DAYS: int = 365  # 主密钥有效期（天，到期提示轮换）
    ENCRYPT_KEY_WARN_DAYS: int = 30  # 到期预警提前量（天；≤30d warn / ≤7d high / ≤1d critical）
    ENCRYPT_KEY_CREATED_AT: int = 0  # 主密钥创建时间（epoch 秒；0=按密钥文件内嵌，无则 mtime）
    ENCRYPT_ROTATE_GRAY_RATIO: float = 0.0  # 新密钥灰度写入抽样比率 0~1（0=全切当前版本）
    ENCRYPT_GRAY_MASTER_KEYFILES: str = ""  # 灰度验证用新版本主密钥副本（可选；配合 ROTATE_GRAY_RATIO）
    ENCRYPT_PERF_ENABLED: bool = True  # 性能采样开关（关闭后零采样开销）
    ENCRYPT_PERF_BUCKETS: str = "1048576:1-16M,16777216:16M+"  # 分桶阈值(字节:标签)，按升序逗号分隔；首个隐式桶为 <最低档；空=不分桶兼容既有
    # ---- P7-A1 KMS 集成（默认 local 走本地文件，零漂移）----
    ENCRYPT_KEY_PROVIDER: str = "local"  # 主密钥来源：local=本地文件(默认,零漂移)；kms=云 KMS 托管
    KMS_CACHE_TTL: int = 60  # KMS 主密钥缓存 TTL（秒；到期强制回源，禁永久离线）
    KMS_RECOVERY_S: int = 300  # KMS 中断到告警 critical 的等待秒数（分级时序 warn→critical）
    KMS_REGION: str = ""  # KMS region（留空=自建/no-region）
    KMS_ENDPOINT: str = ""  # KMS endpoint（空=厂商默认）
    KMS_KEY_REF: str = ""  # 主密钥引用/别名（由 KMSProvider 子类解析）
    # ---- P7-B1 报表定时自动归档（默认关，零漂移）----
    REPORT_ARCHIVE_ENABLED: bool = False  # 定时归档总开关；关则协程不启动
    REPORT_ARCHIVE_CRON: str = "0 3 * * *"  # 归档调度 cron（分 时 日 月 周）；默认每日 3 点低峰
    REPORT_ARCHIVE_TZ: str = ""  # cron 时区（ZoneInfo 名，如 Asia/Shanghai；空=系统时区）
    REPORT_ARCHIVE_TIMEOUT_S: int = 7200  # 单轮归档最大执行时长（秒；超时中止并告警）
    REPORT_ARCHIVE_RETENTION_DAYS: int = 180  # 归档保留天数（复用生命周期回收）
    REPORT_ARCHIVE_FORMAT: str = "json"  # 归档格式（json/csv）
    # ---- P7-B2 加密合规报表 PDF 导出（默认关，零漂移）----
    REPORT_PDF_ENABLED: bool = False  # PDF 导出总开关；关则 format=pdf 返回 404
    REPORT_PDF_WATERMARK: bool = True  # 是否渲染水印（合规建议恒开，可关供调试）
    REPORT_PDF_WM_TEXT: str = ""  # 自定义水印文案（空=默认：Zeffy 合规报表 {ts} {operator}）
    REPORT_PDF_FONT_PATH: str = ""  # 可选外部中文字体路径（方案C升级位；空=CID STSong-Light）
    REPORT_PDF_MAX_ROWS: int = 2000  # 单 PDF 行数上限（防超大报表渲染阻塞）
    # ---- P7-A3 压缩 + 加密（默认关，零漂移）----
    COMPRESS_ENABLED: bool = False  # 压缩总开关；关则写路径零改动（加密前不压缩）
    COMPRESS_ALGO: str = "gzip"  # 压缩算法（gzip 标准库 / zstd 需装 zstandard）
    COMPRESS_LEVEL: int = 6  # 压缩级别（gzip 1-9 / zstd 1-22）
    COMPRESS_MIN_SIZE: int = 1024  # 最小压缩字节阈值（低于则跳过）
    COMPRESS_EXT_INCLUDE: str = ""  # 仅压扩展名集合（逗号分隔；空=白名单不限制）
    COMPRESS_EXT_EXCLUDE: str = ""  # 跳过扩展名（空=用内置已压缩/二进制集）
    COMPRESS_ENTROPY_CHECK: bool = True  # 三级熵采样：高熵（已压缩/二进制）跳过
    COMPRESS_BLOCK_BYTES: int = 262144  # 统一块缓冲（=加密块大小；压缩+加密共享单块）
    # ---- P7-C1 深冷批量解冻（默认关，零漂移）----
    COLD_THAW_ENABLED: bool = False  # 批量解冻总开关；关则接口 404、worker 不启动
    COLD_THAW_CONCURRENCY: int = 8  # 并发恢复任务数（三维限流之一）
    COLD_THAW_RPS: int = 50  # 每秒恢复请求数上限（三维限流之二）
    COLD_THAW_PEAK_BYTES_S: int = 0  # 每秒恢复字节上限（三维之三；0=仅并发/RPS）
    COLD_THAW_BATCH: int = 100  # 批次对象数（持久化粒度）
    COLD_THAW_EST_TOKEN_TTL: int = 600  # estimate token 有效期（秒）
    COLD_THAW_MAX_RETRY: int = 3  # 单对象指数退避最大重试（S3 限流/波动）
    COLD_THAW_WM_KEY: str = ""  # token HMAC 签名密钥（生产必配；缺省拒绝签发）
    # ---- P7-C2 版本回滚（默认关，零漂移）----
    VERSION_ROLLBACK_ENABLED: bool = False  # 回滚总开关；关则预览/回滚接口 404
    VERSION_ROLLBACK_ADMIN_ONLY: bool = True  # 回滚仅 admin（true 时 can_edit 亦不可）
    VERSION_ROLLBACK_TOKEN_TTL: int = 300  # 预览→回滚 token 有效期（秒）
    VERSION_ROLLBACK_REASON_REQUIRED: bool = True  # 回滚原因必填（审计可溯）
    # ---- P6-2 O1 智能分层（按访问频率冷化；TIER_ENABLED 为主开关）----
    TIER_COLD_ACCESS_AGE: int = 30 * 24 * 60 * 60  # 按 last_access 的冷化年龄（秒，默认30天）
    TIER_WARM_AGE: int = 7 * 24 * 60 * 60  # N1 三级分层：超该年龄 → warm（秒，默认7天）
    TIER_PINNED_MAX_COUNT: int = 20  # N1 置顶数量上限（单用户）
    TIER_PINNED_MAX_RATIO: float = 0.1  # N1 置顶总大小 ≤ 配额比例（10%）
    TIER_TOUCH_TTL: int = 60  # 热度埋点防放大：距上次更新不足该秒数则完全跳过写库
    TIER_COOL_DOWN: int = 60 * 60  # 冷却期：回暖/刚访问后该秒数内不冷化，且期内读不刷新 last_access
    # ---- P6 审查闭环 · 批次级独立开关 + 治理参数（异常可单关单能）----
    RECONCILE_ENABLED: bool = False  # 对账守护（批 G）
    RECONCILE_BATCH: int = 500  # 对账分批扫描大小
    RECONCILE_PAGE_SLEEP: float = 0.02  # 对账批次间错峰等待（秒）
    RECONCILE_ORPHAN_GC: bool = True  # 有文件无记录 → 自动清理孤儿（关闭则仅告警）
    TX_ENABLED: bool = False  # 事务框架/可见性隔离（批 I）
    TX_TTL: int = 60 * 60 * 12  # 事务批次最长存活（秒），超时自动回滚
    TX_STAGING_DIR: str = "_tx"  # 事务暂存目录（相对 STORAGE_ROOT）
    RECYCLE_ENABLED: bool = False  # 软删除回收站（批 J）
    ST_RECYCLE_RETENTION_DAYS: int = 7  # 回收站保留天数，过期物理删并释放配额
    AUDIT_GOVERNANCE_ENABLED: bool = True  # 治理全链路审计（批 L）
    # 灰度：配额按 owner 白名单启用；空串=全部用户（逗号分隔 owner 集）
    QUOTA_GRAY_LIST: str = ""

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
