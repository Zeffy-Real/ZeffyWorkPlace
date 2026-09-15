# Zeffy-Workplace

单群聊闭环的多 Agent 协作工作台。人类只负责「创意提出、重大决策、最终验收」，其余链路由多个 Agent 在群聊式界面中自主拆解、协作、执行、评审。

> 定位：Agent 开发岗位面试级作品集。语言底座 Python（Agent 生态主流），前端为完整 Web 群聊 UI。
> 当前进度：**P6-2 阶段二**（产物生命周期治理 + 可选优化/去重全部落地）。

## 核心能力（P0 → P6-2）

| 阶段 | 能力 |
|---|---|
| P0 | 骨架与基建：FastAPI + React 群聊 UI + 活水 echo 链路 |
| P1 | 多 Agent 协作链路（LangGraph） |
| P2 | 多 worker 队列/恢复/lease/心跳（ARQ + Redis 分布式锁） |
| P3 | 鉴权（JWT/PBKDF2/token 撤销/owner 归属） |
| P4 | 任务优先级/拓扑 DAG/RBAC + 分享/审计 trace 贯穿 |
| P5 | 产物分布式存储：版本管理/在线预览/断点续传/流式直写磁盘/真实 S3 协议(Moto) 验证 |
| P6 | 产物生命周期治理：`artifacts` 元表 + 配额 + 事务批次 + 存储分层 + 软删回收站 + 全状态对账 + 审计 |
| P6-2 | 治理可选优化：智能分层(访问频率) / 配额智能(预测/报表/成本) / 运营体验(审计查询+批量) / **重复数据去重(内容寻址+引用计数)** |

### 产物治理（P6 / P6-2）
所有治理能力受总开关 `ARTIFACT_META_ENABLED` 控制，**默认关闭**——关闭时与 P5 纯存储行为完全一致（兼容锚点由 `test_compat_anchor.py` 强制覆盖）。子开关仅在总闸开启时生效：
- **配额**：`QUOTA_ENABLED`（原子增减、预扣/结账/返还、超限熔断、system 豁免）
- **事务批次**：`TX_ENABLED`（`_tx` 暂存 + 元表状态原子可见 + TTL 自动回滚）
- **存储分层**：`TIER_ENABLED`（按 `last_access` 访问频率冷化 + 冷却期防抖）
- **回收站**：`RECYCLE_ENABLED`（软删仍占配额，恢复前校验，过期物理删）
- **对账/审计**：`RECONCILE_ENABLED` / `AUDIT_GOVERNANCE_ENABLED`
- **配额智能**：`QUOTA_HISTORY_ENABLED`（历史采样 + 趋势预测 ETA + 成本核算）
- **重复数据去重**：`DEDUP_ENABLED`（内容寻址 `artifacts/_dedup/{sha}` + 引用计数 + 原子占坑 + 存量 backfill）

治理 API 位于 `/artifacts/*`（stats / tx / recycle / quota/report / audit / batch）。

### 运维可观测（P6-4）
治理指标并入 `/metrics`（`governance` 段：配额使用率 TopN、分层占比、事务成功率、对账分类、灰度命中），告警支持触发/恢复迟滞与按维度独立冷却；管理端点统一 `admin` 越权 404。
- **运维状态**：`GET /admin/governance/status` —— 各功能当前有效值（覆盖/灰度/配置默认）+ 守护任务运行态/上次运行时间/结果/错误（脱敏）
- **应急回滚**：`POST /admin/governance/emergency-disable` —— 需 `confirm=true` + `reason`，一键关闭全部治理（幂等）
- **单功能开关**：`POST /admin/governance/{feature}` `{enabled, reason}`（运行时覆盖，重启恢复配置默认）
- **灰度管理**：`GET/POST /admin/governance/gates` `{feature, add[], remove[]}` —— 勾选 owner 后该用户实际生效；优先级 **运行时覆盖 > 灰度名单 > 配置默认**
- **关断短路**：`ARTIFACT_META_ENABLED=false` 时指标/告警/守护全零开销，行为与 P5 完全一致
- **配置快照**：`GET /admin/governance/snapshots`、`POST /admin/governance/snapshot`、`POST /admin/governance/snapshot/{key}/restore`（一键回滚上一版本）
- **告警历史**：`GET /admin/governance/alerts`（admin-only，30 天治理/系统告警触发与恢复，detail 含 metric/level/threshold/current）

### 运维体验（P6-4-A）
前端治理面板新增四项（手写 SVG、无图表依赖）：
- **治理健康评分**：可解释扣分明细 + 三色分级（0-60 红 / 60-80 黄 / 80-100 绿）；普通用户=个人维度，admin=全局维度
- **分层占比条**：热/冷/其他，占比 <5% 保底宽度；分母 0 → 空态
- **配额趋势折线**：按实际时间范围展示，后端降采样 ≤60 点、单点/数据不足有标注
- **告警历史面板**（admin-only）：未恢复置顶、按 metric/level 分组、显示当前值与阈值

### 灰度中心化（P6-4-B，多实例一致性）
设置 `GOV_CENTRALIZE=true` 后，运行时覆盖 + 灰度名单改为 **Redis 权威源 + 本地缓存 + Pub/Sub 失效**，任意实例写入后各实例一致生效：
- **启动原子**：服务接客前完成一次全量预加载；失败按降级启动，不含半就绪判定
- **消息可靠**：订阅重连全量拉取 + 版本号比对 + 定期校验（`GOV_SYNC_INTERVAL`）双兜底
- **写可靠**：版本号乐观锁 + 写后回读校验 + 并发重试；`/gates` 批量原子（pipeline）幂等
- **降级**：Redis 故障时读保留本地缓存、写返回 503，恢复自动追平；持续由 `GOV_DEGRADE_ALERT_AFTER` 升级告警
- **防风暴**：失效拉取随机延迟（`GOV_FADE_MAX_MS`）+ 全量同步节流（`GOV_SYNC_MIN_INTERVAL`）
- **环境隔离**：键前缀 `gov:{GOV_ENV}:*`，测试/生产不串写

> **单实例约束已解除**：默认 `GOV_CENTRALIZE=false` 保持进程内语义（与 P6-4 零差异）；多实例部署开启 `GOV_CENTRALIZE=true` 后中心化一致。

## 技术栈
- 后端：Python 3.12 + FastAPI + LangGraph + SQLAlchemy(async) + PostgreSQL / Redis / Qdrant
- 前端：React 19 + TypeScript + Vite
- 存储：Local（NFS 兼容） / S3（真实协议 · aiobotocore）
- 管理：uv、docker compose、ruff、mypy、pytest、vitest

## 目录结构
```
zeffy-workplace/
├── docker-compose.base.yml      # PG + Redis
├── docker-compose.vector.yml    # Qdrant（P1 启用）
├── server/                      # FastAPI 后端
├── client/                      # React 前端
├── workspace/                   # 工具可写白名单根（gitignore）
└── .env.example
```

## 快速开始
```bash
# 依赖（PG + Redis）
docker compose -f docker-compose.base.yml up -d

# 后端
cd server && uv sync && cp ../.env.example .env
uv run uvicorn app.main:app --reload --workers 1

# 前端
cd client && npm install && npm run dev   # http://localhost:5173
```

## 测试与静态检查
```bash
cd server
uv run pytest            # 单元 + 集成（224+ passed）
uv run ruff check app tests migrations
uv run mypy app
uv run python scripts/smoke_s3_real.py   # S3 真实协议冒烟（17 项）
cd ../client && npm test                 # vitest（11 passed）
```

## 工程纪律
- 兼容锚点：治理/存储增强默认关，关闭态 P5 零漂移、零额外 IO。
- 迁移：人工逐行审核，禁止 autogenerate 直接执行；降级破坏性操作前必须备份。
- 明文约束由 `server/app/` 内模块注释 + 测试强制，运维回滚以开关降级为主。

## 版本记录
- Python 3.12 / uv 0.11 / Node 22；后端依赖锁定于 `server/uv.lock`。