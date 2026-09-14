# Zeffy-Workplace · 面试级作品集 MVP 任务文档

> 编写依据：`idea-original.md`（原始想法）
> 定位：**单群聊闭环 + Agent 岗位全能力面**的可演示作品集 MVP。语言底座为 Python（Agent 生态最主流），前端为完整 Web 群聊 UI。
> 目标不是"极简能跑"，而是"能在面试中讲清楚每一个 Agent 开发岗位 JD 要求的能力点，并有一份可演示的真实工程"。
> 状态：待审查确认，尚未编码

---

## 〇、设计取舍说明（为什么这样定）

原始模糊文档里的诉求，几乎全部命中 **Agent 开发岗位 JD** 的关键词：
多 Agent 协作、工作流编排、AGENT-LOOP、行为模式（ReAct / Plan-and-Execute）、FUNCTION-CALLING、上下文压缩、SKILL 复用、评测集、可观测、安全。

本文档据此把"作品集 = 架构 + 功能 + 技术栈"三层都抬到面试级，但**用 Phase 切分保证每一阶段可独立交付、可演示、可验收**，避免"全都要、半年起不来"。

**三个贯穿全项目的能力主线**（对应 JD 高频词）：
1. **编排能力**：LangGraph 状态机驱动的多 Agent 协作 + 人机交互(HITL)。
2. **鲁棒性能力**：异步、重试熔断、上下文压缩、记忆、评测。
3. **可观测与安全**：全链路 trace、权限分级、审计回放。

---

## 一、任务目标

**一句话**：在单个群聊窗口内，「人类发任务 → 调度 Agent 拆解 → 角色 Agent 依工作流执行 → 评审把关 → 关键节点人工决策 → 人类验收」，全程人类只参与创建/决策/验收。

**本期必须达成的能力闭环（面试可逐点演示）：**
- 多 Agent 协作（Supervisor 分工 + 评审回流）
- 工作流驱动（LangGraph 有向状态图，支持串行/并行分支）
- 行为模式（ReAct 工具循环 / Plan-and-Execute / Reflection 评审）
- Function/Tool Calling（统一注册、鉴权、幂等）
- 三级记忆 + 上下文自动压缩（token 摘要）
- 异步任务 + 重试熔断 + checkpoint 中断恢复（AGENT-LOOP）
- 交易级安全：关键动作人工审批、白名单工具、全量审计
- 评测体系：准确性 / 耗时 / 成本 / 协作质量 五维指标 + LLM-judge
- 可观测：全链路 trace（Langfuse）
- 完整前端群聊 UI（流式消息、任务卡、审批卡、回放面板）

**本期明确不做**：多租户/团队版、第三方系统对接（Git/Jira/飞书）/ 桌面端。Skill 自动沉淀放在 P2（自动生成可复用 skill 本身是更高难度，先进"半自动+人工校准"）。

---

## 二、技术栈（面试级 · 当前最受认可组合）

### 后端（Python Agent 生态）
| 层 | 选型 | 说明 |
|---|---|---|
| 语言/环境 | **Python 3.12 + uv** | agent 生态主流，uv 现代依赖管理 |
| Agent 框架 | **LangGraph**（+ `langgraph-checkpoint-sql-sync`） | 状态图编排、持久化、`interrupt` 人机交互，agent 岗最主流的工程框架 |
| LLM 接入 | **LangChain-Core / ChatModel 抽象** | provider 无关，可换 OpenAI/DeepSeek/本地 |
| 服务/API | **FastAPI + WebSocket / SSE** | 异步、流式消息推送 |
| 异步任务 | **Redis + Celery**（或 ARQ） | 长任务队列、重试、超时熔断 |
| 数据库 | **PostgreSQL + SQLAlchemy (async)** | 任务/消息/节点/审计/评测结构化数据 |
| 缓存/队列 | **Redis** | 队列、短期状态、限流 |
| 向量库 | **Qdrant**（Docker 单机即可） | 长期记忆/RAG 语义检索 |
| 记忆 | 三级自研 + 可选 **Mem0/Zep** 思路 | 短期会话 / 任务期 / 长期知识库 |
| 评测 | **Ragas**（RAG 侧）+ **DeepEval / pytest + LLM-as-judge** | 指标统计与质量判定 |
| 可观测 | **Langfuse**（自托管/云） | trace、prompt 版本管理、成本、A/B |
| 安全 | pydantic-settings + 权限中间件 + 审计表 | 配置化键 + 分级授权 |

### 前端（完整群聊 UI）
| 层 | 选型 | 说明 |
|---|---|---|
| 框架 | **React 18/19 + TypeScript + Vite** | 主流、生态全 |
| 样式 | **Tailwind CSS** + shadcn/ui 组件底座 | 快速搭建 + 后续可按设计令牌定制 |
| 状态 | **Zustand** | 轻量、SS 友好 |
| 实时 | **WebSocket 客户端** + 流式渲染 | 消息逐 token / 分块到达 |
| 渲染 | **react-markdown + 代码高亮** | 产物卡片内联预览 |

> 说明：Qdrant/PostgreSQL/Redis 用 `docker compose up` 一键起，保证评审者环境可复现。

---

## 三、总体架构（插件化分层 × 多 Agent 协同）

### 1. 多 Agent 协作拓扑（LangGraph 图结构）
```
                人类（群里）
                  │ 任务/反馈
                  ▼
            ┌──────────────┐
            │  Supervisor  │  调度总管：理解意图 → 拆解子步骤 → 派单
            │ (Planner)    │  (Plan-and-Execute；拆解产出 DAG)
            └──────┬───────┘
                   │ 派发(可并行)
         ┌─────────┼──────────┐
         ▼         ▼          ▼
   [文档 Agent] [设计 Agent] [代码 Agent] ... 领域执行 Agent（ReAct+工具）
         │         │          │
         └─────────┼──────────┘
                   ▼
            ┌──────────────┐
            │  Reviewer    │  评审 Agent：产物审查/冲突仲裁/风险识别
            │ (Reflection) │  → 打回修正(回流) / 通过进下一节点
            └──────┬───────┘
                   ▼
         人类验收卡片（成功/驳回/迭代）
```

- **HITL（人机交互）**：关键决策节点用 LangGraph `interrupt` 暂停图，等人类确认后 `Command(resume)` 恢复，checkpoint 保证从断点继续。
- **可扩展**：新增领域 Agent = 注册一个 Node + 角色 Prompt，不改图骨架。

### 2. 六层模块架构（沿用你原文档的插件化理念，落实到 LangGraph/HITL）
| 层 | 模块 | 面试可讲点 |
|---|---|---|
| 接入层 | Web 前端 + WebSocket | 流式 UI、卡片交互 |
| 编排层 | LangGraph 图 + 工作流模板 + Celery 队列 | 状态机、DAG、并行分支、超时熔断 |
| Agent 层 | Supervisor / 领域 Agent / Reviewer | 角色可插拔、行为范式可配置 |
| 记忆层 | 三级记忆 + 向量检索 + 自动压缩 | checkpoint、RAG、重放 |
| 工具层 | 工具注册表 + Function Calling 协议 | schema、鉴权、幂等、审计 |
| 基础设施 | PostgreSQL/Redis/Qdrant + Langfuse | 持久化、可观测、安全 |

---

## 四、功能清单（逐项对齐 Agent 岗位 JD 能力）

### A. 任务编排与工作流
- [ ] 工作流模板：通用（需求→文档→设计→实现→评审→验收）、轻量（需求→拆解→执行→验收）
- [ ] 拆解算法：类型匹配历史最佳方案 + LLM 推理；≤2 层；自动识别依赖生成 DAG，无依赖并行
- [ ] 状态机：待分配→执行中→评审中→已完成/已验收/失败/阻塞，全状态可追溯
- [ ] 异步编排：Celery + Redis 队列；优先级 / 抢占；重试(默认1次)+熔断；超时升级人类

### B. Agent 行为模式
- [ ] Plan-and-Execute：先计划后执行，中途校验、计划动态调整
- [ ] ReAct：推理→行动→观察循环，用于工具型任务
- [ ] Reflection(Review)：产出→自审→修正→复审，用于评审节点
- [ ] AGENT-LOOP：LangGraph checkpoint 中断/恢复，上下文完整保留

### C. 工具与 Function Calling
- [ ] 统一工具注册表：声明 name/参数(schema)/返回值/权限等级/适用场景
- [ ] 自动发现：Agent 按任务匹配工具
- [ ] 幂等 + 重试 + 熔断；写操作带幂等标识
- [ ] 内置工具集：文件读写(白名单)、Markdown 生成、知识库检索、代码检查(只读)

### D. 记忆与上下文工程
- [ ] 三级记忆：短期(会话) / 中期(任务产物·向量检索) / 长期(知识库·RAG)
- [ ] **上下文压缩**：超 token 阈值(默认 30 轮或 ~8k token)自动摘要，保留决策/节点/结论，压缩后用工作 LLM 继续
- [ ] 动态召回：任务执行时注入相关历史任务/技能/领域知识
- [ ] 遗忘/归档：低价值记忆定期归档（P2 强化）

### E. 安全
- [ ] 工具权限分级：敏感操作强制人类审批（interrupt 卡片）
- [ ] 工作区白名单 + 路径穿越防护
- [ ] 数据隔离：任务/租户内存隔离（本期单用户，接口预留）
- [ ] 全量审计日志 + 全链路回放

### F. 评测与运营
- [ ] 五维指标：任务准确率 / 一次通过率 / 执行耗时 / 步骤数 / token 成本
- [ ] LLM-as-judge 评审打分；Ragas 评估 RAG 检索质量
- [ ] Prompt 版本化 + A/B（Langfuse）
- [ ] 失败案例库归档（P2 复盘）

### G. 可观测
- [ ] Langfuse trace：每次运行完整 trace + token 计费 + prompt 版本
- [ ] 日志分级 + 错误告警

### H. 前端群聊 UI
- [ ] 三栏：左侧任务导航 / 中间群聊流 / 右侧任务详情+回放
- [ ] 流式消息渲染；任务卡、产物卡、审批卡、追问卡
- [ ] 决策审批交互（同意/驳回/修改意见）；回放面板
- [ ] 设计规范：统一圆角/中性色 6 档/单一强调色/8pt 栅格，符合你的 UI 偏好（回避 AI-SLOP）

---

## 五、新增/修改文件清单（工程骨架）

```
zeffy-workplace/
├── docker-compose.yml            # PostgreSQL / Redis / Qdrant
├── pyproject.toml                # uv：后端依赖
├── .env.example
├── server/
│   ├── app/
│   │   ├── main.py               # FastAPI 入口 + WebSocket/SSE
│   │   ├── config.py             # pydantic-settings（读 env）
│   │   ├── agents/               # 各 Agent 角色
│   │   │   ├── base.py           # Agent 接口规范
│   │   │   ├── supervisor.py     # 调度/拆解
│   │   │   ├── domain.py         # 文档/设计/代码领域 Agent
│   │   │   └── reviewer.py       # 评审
│   │   ├── graph.py              # LangGraph 图构建 + checkpointer + interrupt
│   │   ├── workflow.py           # 工作流模板定义/执行器
│   │   ├── tools/                # 工具注册表 + 内置工具(fs/markdown/search)
│   │   ├── memory/               # 三级记忆 + 向量检索 + 上下文压缩
│   │   ├── memory/compressor.py
│   │   ├── task_queue.py         # Celery + Redis
│   │   ├── db/                   # SQLAlchemy models + 迁移
│   │   ├── eval/                 # 指标 + LLM-judge + Ragas
│   │   └── observability.py      # Langfuse
│   └── tests/
│       ├── test_planner.py
│       ├── test_workflow.py
│       ├── test_tools_fs.py
│       ├── test_compressor.py
│       └── test_eval.py
├── client/                       # React+TS+Vite
│   ├── src/
│   │   ├── App.tsx / Chat.tsx / TaskCard.tsx / ApprovalCard.tsx / ReplayPanel.tsx
│   │   ├── store/ (Zustand)
│   │   └── lib/api.ts            # WebSocket client
└── docs/
    └── workflow-templates.json
```

---

## 六、数据库设计

PostgreSQL（`tasks / messages / task_nodes / audit_logs / eval_runs`），checkpoint 走 LangGraph(thread_id)。

| 表 | 关键字段 | 说明 |
|---|---|---|
| `tasks` | id, title, description, workflow_id, status, config, created/updated_at | 任务主表 |
| `messages` | id, task_id, sender_role, content, type, status, created_at | 群聊消息流 |
| `task_nodes` | id, task_id, node_name, status, input, output, error, records | 含实际产物体 |
| `audit_logs` | id, task_id, operator(agent/human), action, detail, at | 全量审计 |
| `eval_runs` | id, task_id, metrics(jsonb), judge_result, at | 评测运行 |

向量数据存 Qdrant（PID: task 或 global），SQL 表与向量相互用 id 关联。

---

## 七、环境变量（.env.example 摘要）

```
# LLM
LLM_PROVIDER=openai
LLM_API_KEY=__YOUR_KEY__
LLM_BASE_URL=
LLM_MODEL=gpt-4o-mini

# 基建（docker compose）
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://localhost:6379
QDRANT_URL=http://localhost:6333

# 服务
PORT=8787
WORKSPACE_ROOT=./workspace

# 可观测
LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST

# 上下文压缩
CONTEXT_MAX_ROUNDS=30
CONTEXT_MAX_TOKENS=8000
```
约束：API Key 只存 env，`.env` 进 `.gitignore`，不提交。

---

## 八、安全约束（复审）
1. 工具白名单 + 路径穿越防护（fs 工具限定 `WORKSPACE_ROOT`）。
2. 敏感/不可逆操作（删除、覆盖、对外发布）强制人类审批（LangGraph interrupt）。
3. 默认不提供任意 shell 执行；如后续扩展需分级授权+审计。
4. 全操作审计 + 回放可追溯；单用户部署，不假设公网暴露。

---

## 九、验收标准（可测量，对应功能清单）

| # | 验收点 | 门槛 |
|---|---|---|
| A1 | 端到端闭环 | 输入真实任务（如"写一份 PRD"）零人工跑到验收卡，产物入 workspace |
| A2 | 多 Agent 协作 | 触发一次"评审打回→修正→通过"回流 |
| A3 | 并行分支 | 工作流中有无依赖子任务并行执行（trace 可见） |
| A4 | HITL 决策 | 触发一次 interrupt 审批；驳回后 Agent 修正重试或终止 |
| A5 | 追问 | 信息不足时出追问卡，补充后从断点继续，上下文不丢 |
| A6 | 上下文压缩 | 跑满 CONTEXT_MAX_ROUNDS 后自压摘要，收尾节点基于摘要仍成功；压缩前后 token 统计下降≥设定目标 |
| A7 | 记忆召回 | 新任务可召回历史任务/技能（Qdrant 命中） |
| A8 | 重试熔断 | 构造失败调用 → 重试 → 2 次失败升级人工介入卡 |
| A9 | 回放 | 任意任务全链路节点/输入输出/决策 reason 可查 |
| A10 | 评测 | 一条任务产出 eval_runs：五维指标 + judge 打分可查 |
| A11 | 可观测 | Langfuse 中可见该任务完整 trace + token 计费 + prompt 版本 |
| A12 | 前端 | 流式渲染 + 四种卡片 + 回放面板可用，符合设计规范 |

---

## 十、风险点与回滚方案

| 风险 | 影响 | 预案/回滚 |
|---|---|---|
| LangGraph 中断/返回 与异步队列叠加复杂 | 不可恢复态 | 精简：先用"同步图 + HTTP 内 checkpoint"，首版不让 Celery 与 interrupt 同时上线 |
| 拆解质量差 / 跑偏 | 产物不合格 | 每节点自检 + Reviewer 回流；整体驳回重拆 |
| token 膨胀 | 成本/延迟 | 强制 ≤2 层拆解 + 上下文压缩兜底 |
| Agent 死循环 | 卡死 | 节点重试上限 + 熔断 + 超时升级人工 |
| 基建依赖多(Docker) 评审者环境难复现 | 演示失败 | docker compose 一键起 + README 复现说明 |
| 上下文压缩误伤决策信息 | 质量下降 | 摘要保留决策/节点/结论白名单字段；压缩后带人工复核点 |
| 生态快速变动(LangGraph API) | 锁版本 | pyproject 锁依赖 + README 记录版本 |

---

## 十一、测试用例要点

**单元（pytest）**
1. `planner`：拆解 ≥1 子步骤且 ≤2 层；模板节点数约束。
2. `workflow`：非法状态迁移抛错；并行分支计数正确。
3. `tools_fs`：白名单读写成功；`../` 穿越/绝对路径越界拒绝。
4. `compressor`：超阈值产出摘要；保留决策/节点字段；token 下降达标。
5. `memory`：召回命中历史任务/技能。
6. `eval`：judge 打分返回合法；指标可入库。

**端到端/手工（对 A1~A12）**
7. 开一条"写 PRD"任务 → A1~A12 逐项过。
8. 构造信息不足 → A5；构造失败调用 → A8。
9. 跑满 N 轮 → A6 token 统计。
10. 新任务触发记忆召回 → A7。

---

## 十二、实现步骤（审查通过后执行）

**P0 · 骨架与基建**
1. docker compose（PG/Redis/Qdrant）+ u/v 工程 + `.env.example` + LLM 薄封装。
2. SQLAlchemy 建表 + LangGraph checkpointer + FastAPI 空服务 + 极简群聊 echo。

**P1 · 编排闭环**
3. Supervisor 拆解 + 工作流状态机 + 领域 Agent 执行 + Reviewer 回流。
4. Function Calling 工具注册表 + fs/markdown 工具 + 上下文压缩。
5. 打通端到端：任务→回购→验收（A1/A2/A4/A5/A6）。

**P2 · 强化能力**
6. Celery 异步化 + 并行分支 + 重试熔断（A3/A8）。
7. 三级记忆向量召回（A7）+ 回放/审计（A9）。

**P3 · 评测与可观测**
8. 五维评测 + LLM-judge + Ragas（A10）+ Langfuse trace/prompt 版本（A11）。

**P4 · 前端群聊 UI**
9. 完整三栏 UI + 流式 + 四种卡片 + 回放面板（A12），应用设计规范。
10. 全量单测 + 手工验收 A1~A12，产出变更摘要 + README。