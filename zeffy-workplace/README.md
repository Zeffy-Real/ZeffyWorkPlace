# Zeffy-Workplace

单群聊闭环的多 Agent 协作工作台。人类只负责「创意提出、重大决策、最终验收」，其余链路由多个 Agent 在群聊式界面中自主拆解、协作、执行、评审。

> 定位：Agent 开发岗位面试级作品集。语言底座 Python（Agent 生态主流），前端为完整 Web 群聊 UI。
> 当前进度：**P7 批1/批2/批3 全部交付**（C3/B4/B3 → A1/B1/B2 → A3/C1/C2，审查 10 项 P0 全闭环，全量回归 409 passed）。
> 已完成迭代：P0 骨架 → P1 Agent 链路 → P2 多 worker 队列恢复 → P3 鉴权 → P4 任务优先级/RBAC/审计 → P5 产物断点续传/流式落盘/S3 验证 → P6 产物治理(元表/配额/事务/分层/回收站/对账) → P6-2 智能分层/配额智能/运营体验 → P6-3 运维可观测(灰河中心化/守护器) → P6-4 运维体验 灰度/治理面板/健康评分 → P6-5 存储容量经济(S3生命周期/分层参数化/容量报表) → P6-6 多方向增强(治理策略引擎/前端Token收尾/深冷恢复/数据加密) → P7 主题(安全增强/合规自动化/运维稳定性)。

## 产物治理（P6 / P6-2，默认全关，零漂移）
所有治理能力受总开关 `ARTIFACT_META_ENABLED` 控制，**默认关闭**——关闭时与 P5 纯存储行为完全一致（兼容锚点由 `test_compat_anchor.py` 强制覆盖）。子开关仅在总闸开启时生效：
- **配额**：`QUOTA_ENABLED`（原子增减、预扣/结账/返还、超限熔断、system 豁免）；
- **事务批次**：`TX_ENABLED`（_tx 暂存 + 元表状态原子可见 + TTL 自动回滚）；
- **存储分层**：`TIER_ENABLED`（P6-2 起按 `last_access` 访问频率冷化 + 冷却期防抖）；
- **回收站**：`RECYCLE_ENABLED`（软删仍占配额，恢复前校验，过期物理删）；
- **对账** / **审计**：`RECONCILE_ENABLED` / `AUDIT_GOVERNANCE_ENABLED`；
- **配额智能报表**：`QUOTA_HISTORY_ENABLED`（历史采样 + 趋势预测 ETA + 成本核算）。
治理 API 位于 `/artifacts/*`（stats / tx / recycle / quota/report / audit / batch）。

## 产物加密（P6-6-4，默认全关，零漂移）
数据加密受总开关 `ARTIFACT_ENCRYPT_ENABLED` 控制（且仅在治理总闸 `ARTIFACT_META_ENABLED` 开启时经 `encrypt_enabled()` 生效），**默认关闭**——关闭时与明文存储行为完全一致。开关/配置：
- `ENCRYPT_BLOCK_BYTES`：分块大小（默认 262144，每块独立 nonce+tag）
- `ENCRYPT_CIPHER_VERSION`：当前加密版本（单调，禁止回退）
- `ENCRYPT_LEGACY_VERSIONS`：可解密旧版本白名单（逗号分隔，含 1）
- `ENCRYPT_MASTER_KEYFILES`：主密钥文件路径列表（≥2 副本，逗号分隔，0600）
- `ENCRYPT_HMAC_KEYFILE`：元数据 HMAC 密钥文件（独立于 DEK 主密钥）

密码学核心位于 `app/storage/crypto.py`（自包含，不侵入存储后端协议），已闭环方案 P0 ①③⑤⑦ 及 ② Range：
- **P0-1 Nonce 唯一性**：每文件 8 字节随机 seed + 块索引(4B) → 12 字节 nonce，同密钥跨文件/同文件跨块不重复
- **P0-2 Range**：块对齐内部解密后按用户偏移截取，`Content-Range` 反映用户请求而非内部对齐
- **P0-3 先验后出**：`AESGCM.decrypt` 校验 tag 通过才返回明文，失败抛 `EncryptError`，不返回坏数据
- **P0-5 头完整性**：密文头带独立 HMAC（元数据密钥派生），解密前先验，篡改即判定损坏
- **P0-7 边界**：0 字节透传、小块单块、整块不加空块

密钥模型：每文件独立 DEK（HKDF：主密钥 + 文件盐 → 32B AES 密钥），DEK 用主密钥 AES-GCM 信封包裹（nonce 前置存储随信封一起落盘）。

上层编排门面 `app/storage/crypto_gate.py`（接入 put/get 链路，补齐剩余 P0）：
- **P0-8 密钥安全边界**：`ENCRYPT_MASTER_KEYFILES`（≥2 副本）逐字节交叉校验一致才解锁，副本缺失/不一致即拒解锁；HMAC 密钥独立（缺省由主密钥 HKDF 派生）；内存态持有、绝不落日志/异常
- **P0-4 密文计量**：写路径按密文物理大小回填配额/Content-Length（`size=cipher_size`）；`crypto_metrics()` 明/密双口径
- **P0-9 故障降级**：解锁/加密失败 → 明文 + 严重告警审计，不阻塞写入；解密失败抛错转 4xx，不崩溃
- **P0-10 加密审计**：`crypto.*`（encrypt/decrypt/tamper/degrade/unlock）全操作审计
- **P0-6 事务/版本**：`tx_stage_write` 加密开启时事务内暂存密文（自包含，主密钥解封），commit 原子切换、回滚删暂存即清理无孤儿密钥；版本 `get_version_bytes`/`diff` 对自包含密文先行解密，diff 走明文对比；加密开启时去重联动关闭

写路径（`api/upload.py` commit）在加密开启时收集明文 → `encrypt_artifact` → 落密文并回填密文计量（与去重互斥）；读路径（`api/artifacts.py` get）检测密文后 `decrypt_artifact`/`decrypt_range_artifact` 栈内解密再流式返回（含 Range 206 对齐明文坐标）。加密关闭时读写全走明文，零漂移。

加密可观测（P6-6-5，安全优先最小暴露）：`/admin/governance/encryption-status`（admin-only，越权 404）输出白名单（开关/计数/版本/健康评分/滑动窗口，零密钥材料）；公共 `/metrics` 剥离加密字段；告警复用 governance_alarm 通道——解密失败率（双阈值最小样本量）、集中篡改（滑动窗口）、全局降级/密钥不可用（critical）、零星降级（warn），含迟滞恢复与冷却。

密钥轮换与生命周期（P6-6-6）：多版本主密钥（`ENCRYPT_LEGACY_KEYFILES` 归档仅解密），密文头版本 AAD 绑定防降级，密钥内嵌指纹校验损坏密钥拒载，DEK 重裹仅重裹不重加密（原子+重算 HMAC），三阶段回收前置零引用扫描，到期分级预警（30d warn / 7d high / 1d critical）+ 灰度抽样轮换。运维手册见 `docs/P6-6-6密钥轮换运维手册.md`；自动化脚本 `server/scripts/rotate_keys.py`（gen/status/scan/rewrap）。

P7 前收尾（流式/联动/报表/性能，审查 3 全局 + 11 分项红线闭环）：
- **流式加解密**：`StreamingEncryptor/StreamingDecryptor` 分块 AES-GCM 边读边算、内存常量级（4/16MB 断言 <100MB）；上传/读取链路全程流式，Range 明文坐标分块截取，GB 级文件不 OOM；
- **端到端联动集成**（e2e 6 用例）：加密+去重（同钥复用/异钥不复用）、加密+上传断点续传、加密+下载 Range、加密+回收站软删恢复、加密+GC 对账；故障场景（篡改不返回坏数据、单副本丢失多数副本语义）；
- **合规报表**：`/admin/governance/encryption/report` CSV 导出（注入转义、≤90d、每小时 ≤3 次、字段白名单，仅统计维度零敏感）；
- **性能指标**：encrypt/decrypt 独立维度、环形采样上限 1000、`ENCRYPT_PERF_ENABLED` 可关（关闭零开销）；
- **交付总结**：`.trae/documents/P6-6交付总结.md`（遗留项三级分级 + 上线验收清单，运维手册 §10 逐项勾选）。

> 里程碑修复（本轮编码发现并修复）：
> 1. `_ct_eq` 误用 `hmac_mod.HMAC.compare_digest`（cryptography 未暴露该 API）→ 改用标准库 `hmac.compare_digest`，保证头 HMAC 常量时间比较；
> 2. `wrap_dek` 编码时漏将 nonce 前置，导致信封永远无法解封（Nonce 丢失是 AES-GCM 经典致命错误）→ 信封改为 `nonce + AESGCM(master).encrypt(nonce, dek)`，解封时取出 nonce；
> 3. 补充 `collections.abc.Iterable` 导入与 `_off` 未用循环变量清理，ruff 全绿。

## P7 产物治理·安全增强（默认全关，零漂移）
P7 分三批交付，各主题独立开关默认关闭，受总闸 `ARTIFACT_META_ENABLED` 短路，关闭零漂移。
- **批1（价值快享）**：C3 密钥健康度巡检（文件级 SHA256 不还原密钥/分级差异化冷却/首次静默）、B4 加密异常自诊断（解密失败归因/高置信告警）、B3 分文件大小性能区间统计（分桶吞吐/延迟/失败率）。
- **批2（安全基础）**：A1 KMS 集成（可插拔 KeyProvider，云 KMS 托管主密钥缓存回源）、B1 报表定时自动归档（低峰 cron/单轮防重叠/加密落位/失败告警）、B2 合规报表 PDF 导出（中文/水印/防复制/元数据/分页）。
- **批3（体验增强）**：A3 压缩+加密（压缩标识入密文头 HMAC/流式串联内存常量级/三级熵决策）、C1 深冷批量解冻（预估防篡改 token/三维限流/游标持久化恢复）、C2 版本回滚（纯元数据预览/事务化原子回滚/权限升级）。
- **交付总结**：`docs/P7-批3交付总结.md`（审查 10 项 P0 闭环表、遗留项分级（无高风险上线前置）、兼容锚点、批次关系与运维要点）。

## 技术栈
- 后端：Python 3.12 + FastAPI + LangGraph（P1 引入）+ SQLAlchemy(async) + PostgreSQL / Redis / Qdrant
- 前端：React 18/19 + TypeScript + Vite + Tailwind
- 管理：uv、docker compose、ruff、mypy

## 目录结构
```
zeffy-workplace/
├── docker-compose.base.yml      # PG + Redis（P0 必启）
├── docker-compose.vector.yml    # Qdrant（P1 启用）
├── server/                      # FastAPI 后端
├── client/                      # React 前端
├── workspace/                   # 工具可写白名单根（gitignore）
└── .env.example
```

## 快速开始（P0）

环境要求：Docker、uv、Node 18+。

### 1. 起基础依赖（PG + Redis）
```bash
docker compose -f docker-compose.base.yml up -d
```
> P0 只需要 base。Qdrant 无需启动，留到 P1。
> 端口映射：PostgreSQL→`5433`、Redis→`6380`（宿主机）；避免与本机已有的原生 PG/Redis 冲突。
> 若你的机器无冲突，可改回标准 5432/6379。

### 2. 后端
```bash
cd server
uv sync
cp ../.env.example .env   # 编辑你的 LLM_API_KEY（可选，P0 无 key 也能跑）
uv run uvicorn app.main:app --reload --workers 1
```
> ⚠️ P0 强制单 worker（`--workers 1`）。多 worker 下 WS 长连接会断开，P1 引入 Redis Pub/Sub 解决。

### 3. 前端
```bash
cd client
npm install
npm run dev              # http://localhost:5173
```
> 输入"ping" 发送，应收到结构化 JSON 的 "pong"（`kind: system_notify`）。

### 4. 测试与静态检查
```bash
cd server
uv run pytest            # 单元 + 集成测试
uv run ruff check .      # 静态检查
uv run mypy app          # 类型检查
```

### LLM 自检
```bash
cd server
uv run python -m app.llm --self-test
```
> 无 key 时抛 `LLMConfigError` 并给出友好提示；有 key 返回一段文本 + usage 占位。

## 常用命令（Makefile，可选）
```bash
make up        # 起 base 依赖
make test      # 后端测试
make lint      # ruff + mypy
```

## 版本记录（防环境漂移）
- Python：3.12.x
- uv：0.11.x
- Node：22.x / npm 10.x
- Docker：29.x
- 后端依赖：锁定于 `server/uv.lock`
- 前端依赖：锁定精确版本于 `client/package.json`（不使用 `^` 宽松范围）

## 本地开发提示
- 重置数据库（改模型后）：`docker compose -f docker-compose.base.yml down -v`（清除 volume 重建）
- P0 的 `metadata.create_all` 仅限开发用；P1 起切换 alembic 迁移。
- WS 消息统一为结构化 JSON（见 `server/app/wsmessage.py`），禁止裸文本收发。