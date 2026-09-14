# Zeffy-Workplace

单群聊闭环的多 Agent 协作工作台。人类只负责「创意提出、重大决策、最终验收」，其余链路由多个 Agent 在群聊式界面中自主拆解、协作、执行、评审。

> 定位：Agent 开发岗位面试级作品集。语言底座 Python（Agent 生态主流），前端为完整 Web 群聊 UI。
> 当前处于 **P0：骨架与基建**（活水 echo 链路）。

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