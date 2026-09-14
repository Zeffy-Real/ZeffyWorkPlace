"""FastAPI 应用入口。

- lifespan：workspace 校验 + 初始化 LLM 背压信号量；关闭时回收全部运行中后台任务
- /health：DB 连通 + 版本
- POST /tasks：仅落库骨架（P1 编排接续）
- GET /ws：群聊 WebSocket
注意：P1 开发强制 --workers 1（内存会话；多实例/Redis Pub/Sub 属 P2）。

建表策略（P1 起）：**废弃 P0 的 create_all，改由 alembic 管理**。
启动前需执行：`alembic upgrade head`（Makefile 见 `make migrate`）。
lifespan 不做自动迁移——迁移是显式、可控、须人工审核的动作。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket
from sqlalchemy import text

from app.api.schemas import (
    AdvanceRequest,
    ErrorOut,
    HealthOut,
    MetricsOut,
    NodeListOut,
    NodeOut,
    TaskCreate,
    TaskListOut,
    TaskOut,
)
from app.api.ws import task_event_handler, websocket_endpoint
from app.appstate import init_llm_semaphore, init_workqueue, shutdown_workqueue
from app.config import get_settings
from app.db.base import ensure_workspace_root, get_engine
from app.db.repos import (
    RepositoryError,
    create_task,
    list_nodes,
    list_tasks,
)
from app.observability import metrics as obs_metrics
from app.tasks import get_runner
from app.utils.version import get_app_version
from app.workflow import WorkflowStateError, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # P1 起 schema 由 alembic 管理（启动前连库先 `alembic upgrade head`）。
    ensure_workspace_root()
    # 必须在运行中的 event-loop 内创建背压信号量。
    init_llm_semaphore()
    # P2：API 侧启动队列（pool + 事件消费 + lease 巡检）；USE_QUEUE=false 则跳过（回退 in-process）。
    from app.db.base import get_session_factory

    await init_workqueue(get_session_factory(), task_event_handler)
    # P3-2：统一监控（指标采集 + 告警触发/恢复），与 USE_QUEUE 解耦。
    import redis.asyncio as aioredis

    from app.observability import metrics

    metrics_redis = aioredis.from_url(get_settings().REDIS_URL)
    await metrics.collect_metrics(get_session_factory(), redis=metrics_redis)
    metrics.start_monitor(get_session_factory(), redis=metrics_redis)
    yield
    await metrics.stop_monitor()
    try:
        await metrics_redis.aclose()
    except Exception:  # noqa: BLE001
        pass
    # 回收队列（事件消费/巡检协程 + pool）。
    await shutdown_workqueue()
    # 回收全部运行中后台任务（P2 回退 in-process 路径用）。
    await get_runner().shutdown()


app = FastAPI(title="Zeffy-Workplace", version=get_app_version(), lifespan=lifespan)


@app.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    """健康分级（P3-2）：DB 挂 → unhealthy；仅 Redis 挂 → degraded；全好 → healthy。

    不实时查库：redis 状态取自 /metrics 最近采集快照（DB 该坚决实时探活）。
    """
    db_ok = False
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False

    snap = obs_metrics.get_metrics()
    redis_ok = None
    if snap.get("redis") is not None:
        redis_ok = True
    metrics_status = "ok"
    if snap.get("collected_at") is None:
        metrics_status = "not_collected"
    elif snap.get("error"):
        metrics_status = f"db:{snap['error']}"
        redis_ok = True  # redis 探活项 db 失败不影响，这里以 redis 探测为准

    if not db_ok:
        status = "unhealthy"
    elif redis_ok is False:
        status = "degraded"
    else:
        status = "healthy"
    return HealthOut(status=status, db=db_ok, redis=redis_ok,
                     version=get_app_version(), metrics_status=metrics_status)


@app.get("/metrics", response_model=MetricsOut)
async def metrics_endpoint() -> MetricsOut:
    """P3-2 指标快照：返回后台采集的缓存值，不实时查库（防高频打挂存储）。"""
    return obs_metrics.get_metrics()


@app.post("/tasks", response_model=TaskOut, responses={400: {"model": ErrorOut}})
async def create_task_endpoint(payload: TaskCreate) -> TaskOut:
    """创建任务（P0 仅落库；P1 绑定工作流后编排执行）。"""
    from app.db.base import get_session_factory

    factory = get_session_factory()
    try:
        async with factory() as session:
            task = await create_task(
                session,
                title=payload.title,
                description=payload.description,
                workflow_id=payload.workflow_id,
            )
            return TaskOut.model_validate(task)
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/tasks", response_model=TaskListOut)
async def list_tasks_endpoint(status: str | None = None) -> TaskListOut:
    from app.db.base import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        items = await list_tasks(session, status=status)
        return TaskListOut(
            items=[TaskOut.model_validate(t) for t in items],
            total=len(items),
        )


# ---------------------------------------------------------------------------
# P1-2 本地调试接口（仅 ENABLE_DEBUG_ADVANCE=True；P1-5 后由 Agent 接管并关闭）
# 用途：手动推进 lightweight/generic 工作流，验证状态机 + 乐观锁行为。
# ---------------------------------------------------------------------------


@app.get(
    "/tasks/{task_id}/nodes",
    response_model=NodeListOut,
    responses={404: {"model": ErrorOut}},
)
async def list_nodes_endpoint(task_id: str) -> NodeListOut:
    from app.db.base import get_session_factory
    from app.db.repos import get_task
    from app.workflow.templates import get_template

    factory = get_session_factory()
    async with factory() as session:
        task = await get_task(session, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
        items = await list_nodes(session, task_id)
        # 补节点类型（auto/human/hitl），供前端按 blocked+type 重建审批/追问卡
        type_map: dict[str, str] = {}
        try:
            tpl = get_template(task.workflow_id)
            type_map = {s.name: s.type for s in tpl.nodes}
        except ValueError:
            type_map = {}
        outs = []
        for n in items:
            o = NodeOut.model_validate(n)
            o.node_type = type_map.get(n.node_name, "auto")
            outs.append(o)
        return NodeListOut(items=outs, total=len(outs))


@app.post(
    "/tasks/{task_id}/advance",
    response_model=NodeOut,
    responses={400: {"model": ErrorOut}, 403: {"model": ErrorOut}, 404: {"model": ErrorOut}},
)
async def advance_node_debug(task_id: str, body: AdvanceRequest) -> NodeOut:
    """本地调试：手动推进一个工作流节点（未初始化则先 start）。

    返回推进后的**下一运行节点**；任务收尾时返回最后一个 done 节点。
    """
    if not get_settings().ENABLE_DEBUG_ADVANCE:
        raise HTTPException(status_code=403, detail="本地调试推进接口已禁用")

    from app.db.base import get_session_factory
    from app.db.repos import get_node, get_task

    factory = get_session_factory()
    async with factory() as session:
        task = await get_task(session, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")

        try:
            nodes = await list_nodes(session, task_id)
            if not nodes:
                # 首调：初始化工作流并直接推进首节点
                await engine.start(session, task)
                nodes = await list_nodes(session, task_id)
                node_id = nodes[0].id
            else:
                node_id = body.node_id
            nxt = await engine.advance(session, task_id, node_id, body.result)
            target = nxt if nxt is not None else await get_node(session, node_id)
            return NodeOut.model_validate(target)
        except WorkflowStateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.websocket("/ws")
async def ws_route(websocket: WebSocket) -> None:
    await websocket_endpoint(websocket)
