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

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, WebSocket
from sqlalchemy import text

from app.api.admin import router as admin_router
from app.api.artifacts import router as artifacts_router
from app.api.auth_routes import router as auth_router
from app.api.billing import router as billing_router
from app.api.schemas import (
    AdvanceRequest,
    ErrorOut,
    HealthOut,
    MetricsOut,
    NodeListOut,
    NodeOut,
    ShareIn,
    ShareListOut,
    ShareOut,
    TaskCreate,
    TaskListOut,
    TaskOut,
)
from app.api.ws import task_event_handler, websocket_endpoint
from app.appstate import init_llm_semaphore, init_workqueue, shutdown_workqueue
from app.auth.deps import UserPrincipal, get_current_user
from app.config import get_settings
from app.db.base import ensure_workspace_root, get_engine
from app.db.repos import (
    RepositoryError,
    create_task,
    get_task,
    list_nodes,
    list_tasks,
    list_tasks_accessible,
    write_audit,
)
from app.observability import metrics as obs_metrics
from app.tasks import get_runner
from app.utils.version import get_app_version
from app.workflow import WorkflowStateError, engine

# 受保护端点依赖：AUTH 关→匿名（P2 兼容）；开→必需合法 token
CurrentUser = Annotated[UserPrincipal, Depends(get_current_user)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # P1 起 schema 由 alembic 管理（启动前连库先 `alembic upgrade head`）。
    from app.loggingx import setup_logging

    setup_logging()  # P4：trace_id 注入日志 + 可选 JSON 结构化输出
    ensure_workspace_root()
    # 必须在运行中的 event-loop 内创建背压信号量。
    init_llm_semaphore()
    # P2：API 侧启动队列（pool + 事件消费 + lease 巡检）；USE_QUEUE=false 则跳过（回退 in-process）。
    from app.db.base import get_session_factory

    await init_workqueue(get_session_factory(), task_event_handler)
    # P3-2：统一监控（指标采集 + 告警触发/恢复），与 USE_QUEUE 解耦。
    import redis.asyncio as aioredis

    from app.observability import instance_reg, metrics
    from app.observability.instance_reg import InstanceError, check_clock_skew, register

    metrics_redis = aioredis.from_url(get_settings().REDIS_URL)
    await metrics.collect_metrics(get_session_factory(), redis=metrics_redis)
    metrics.start_monitor(get_session_factory(), redis=metrics_redis)

    # P4-2：告警外部通知（异步队列独立 worker，不发则零打扰）
    from app.observability import notify

    notify.configure_dispatcher(redis=metrics_redis, session_factory=get_session_factory())
    notify.get_dispatcher().start()

    # P5：产物存储 GC（临时文件清理 + 生命周期回收）后台协程
    from app.storage import start_gc

    start_gc(get_session_factory())

    # P4-1：时钟校验 + API 实例注册/心跳（ENABLE_ADMIN 仅控制 /admin 路由，注册恒后台运行）
    inst_ticker = None
    try:
        await check_clock_skew(metrics_redis, max_skew=get_settings().MAX_CLOCK_SKEW)
        inst_id = get_settings().instance_id
        if await register(metrics_redis, instance_id=inst_id, kind="api",
                          host=instance_reg.current_host(),
                          ttl=get_settings().INSTANCE_HEARTBEAT_TTL):
            inst_ticker = instance_reg.start_ticker(
                metrics_redis, instance_id=inst_id,
                ttl=get_settings().INSTANCE_HEARTBEAT_TTL)
    except InstanceError as exc:
        logger.warning("API 实例注册/时钟校验失败（忽略继续）：%s", exc)
    yield
    if inst_ticker is not None:
        await instance_reg.shutdown_ticker(inst_ticker)
    await instance_reg.unregister(metrics_redis, instance_id=get_settings().instance_id)
    # P4-2：停止通知 dispatcher（幂等）
    from app.observability import notify

    await notify.stop_dispatcher()
    # P5：停止存储 GC + 释放后端连接
    from app.storage import close_backend, stop_gc

    await stop_gc()
    await close_backend()
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

logger = logging.getLogger(__name__)

# P4：全链路 trace_id 中间件——读 X-Trace-ID（无则生成）set 进 contextvar，响应头回传。
# 审计 write_audit 自动带当前 trace，跨模块可追溯。
@app.middleware("http")
async def _trace_middleware(request, call_next):
    from app.tracing import TRACE_HEADER, set_trace_id

    incoming = request.headers.get(TRACE_HEADER)
    trace = set_trace_id(incoming or None)
    response = await call_next(request)
    response.headers[TRACE_HEADER] = trace
    return response

# P3-3 Auth 路由（注册/登录/登出/me）
app.include_router(auth_router)
# P4-1 Admin 路由（/admin/cluster，ENABLE_ADMIN 控制 → 默认 404）
app.include_router(admin_router)
# P4-4 成本统计路由（/billing/summary, /billing/export.csv）
app.include_router(billing_router)
# P5 产物读取路由（/artifacts，鉴权 can_view/can_edit）
app.include_router(artifacts_router)


@app.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    """健康分级（P3-2 + P5）：DB 挂 → unhealthy；Redis/存储后端挂 → degraded；全好 → healthy。

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

    # P5：存储后端健康（S3 挂 → degraded）
    from app.storage import get_backend

    storage_health = await get_backend().health()

    status: Literal["healthy", "degraded", "unhealthy"] = "healthy"
    if not db_ok:
        status = "unhealthy"
    elif redis_ok is False or not storage_health.get("ok", True):
        status = "degraded"
    return HealthOut(status=status, db=db_ok, redis=redis_ok,
                     version=get_app_version(), metrics_status=metrics_status,
                     storage=storage_health)


@app.get("/metrics", response_model=MetricsOut)
async def metrics_endpoint() -> MetricsOut:
    """P3-2 指标快照：返回后台采集的缓存值，不实时查库（防高频打挂存储）。"""
    out = dict(obs_metrics.get_metrics())
    out["storage"] = None
    try:
        from app.storage import storage_metrics

        out["storage"] = storage_metrics()
    except Exception:  # noqa: BLE001
        out["storage"] = None
    return out


@app.post("/tasks", response_model=TaskOut, responses={400: {"model": ErrorOut}})
async def create_task_endpoint(payload: TaskCreate,
                               user: CurrentUser) -> TaskOut:
    """创建任务（P0 仅落库；P1 绑定工作流后编排执行）。P3-3：写 owner_id。P4-4b：写优先级。"""
    # 🔴 高优（priority=2）仅 admin 可提交（防全员高优退化单队列）；AUTH off 放行
    if payload.priority >= 2 and user.authenticated and not user.role_is_admin():
        raise HTTPException(status_code=403, detail="仅管理员可提交高优先级任务")
    from app.db.base import get_session_factory

    factory = get_session_factory()
    try:
        async with factory() as session:
            task = await create_task(
                session,
                title=payload.title,
                description=payload.description,
                workflow_id=payload.workflow_id,
                owner_id=user.authenticated and user.id or None,
                priority=payload.priority,
            )
            if user.authenticated:
                await write_audit(session, task_id=task.id, operator="user",
                                  action="task_create",
                                  detail={"user_id": user.id, "priority": payload.priority})
            return TaskOut.model_validate(task)
    except RepositoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/tasks", response_model=TaskListOut)
async def list_tasks_endpoint(user: CurrentUser,
                              status: str | None = None) -> TaskListOut:
    from app.db.base import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        # AUTH off / admin → 全量；普通登录用户 → owner ∪ 分享
        if not user.authenticated or user.role_is_admin():
            items = await list_tasks(session, status=status)
        else:
            items = await list_tasks_accessible(session, user_id=user.id, status=status)
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
async def list_nodes_endpoint(task_id: str, user: CurrentUser) -> NodeListOut:
    from app.db.base import get_session_factory
    from app.workflow.templates import get_template

    factory = get_session_factory()
    async with factory() as session:
        task = await get_task(session, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
        await _require_caps(session, user, task, "view")
        items = await list_nodes(session, task_id)
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
async def advance_node_debug(task_id: str, body: AdvanceRequest,
                             user: CurrentUser) -> NodeOut:
    """本地调试：手动推进一个工作流节点（未初始化则先 start）。

    返回推进后的**下一运行节点**；任务收尾时返回最后一个 done 节点。
    """
    if not get_settings().ENABLE_DEBUG_ADVANCE:
        raise HTTPException(status_code=403, detail="本地调试推进接口已禁用")

    from app.db.base import get_session_factory
    from app.db.repos import get_node

    factory = get_session_factory()
    async with factory() as session:
        task = await get_task(session, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
        await _require_caps(session, user, task, "edit")

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


# ---------------------------------------------------------------------------
# P4-3 协作分享（仅 owner/admin 可管理；越权统一 404）
# ---------------------------------------------------------------------------


async def _require_manage_share(session, user: UserPrincipal, task) -> None:
    from app.auth import permissions as perm

    if not await perm.can_manage_share(session, user, task):
        raise HTTPException(status_code=404, detail="Not Found")


@app.get("/tasks/{task_id}/shares", response_model=ShareListOut,
         responses={404: {"model": ErrorOut}})
async def list_share_endpoint(task_id: str, user: CurrentUser) -> ShareListOut:
    from app.db import repos as r_
    from app.db.base import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        task = await get_task(session, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
        await _require_manage_share(session, user, task)
        items = await r_.list_shares(session, task_id)
        return ShareListOut(items=[ShareOut.model_validate(s) for s in items], total=len(items))


@app.put("/tasks/{task_id}/shares", response_model=ShareOut,
         responses={404: {"model": ErrorOut}, 400: {"model": ErrorOut}})
async def upsert_share_endpoint(task_id: str, body: ShareIn, user: CurrentUser) -> ShareOut:
    from app.db import repos as r_
    from app.db.base import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        task = await get_task(session, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
        await _require_manage_share(session, user, task)
        try:
            share = await r_.upsert_share(session, task_id=task_id, user_id=body.user_id,
                                          role=body.role)
            await write_audit(session, task_id=task_id, operator="user",
                              action="share_add", detail={"by": user.id, "to": body.user_id,
                                                          "role": body.role})
        except RepositoryError:
            raise HTTPException(status_code=400, detail="分享失败：目标用户不存在") from None
        return ShareOut.model_validate(share)


@app.delete("/tasks/{task_id}/shares/{user_id}", responses={404: {"model": ErrorOut}})
async def remove_share_endpoint(task_id: str, user_id: str, user: CurrentUser) -> dict:
    from app.db import repos as r_
    from app.db.base import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        task = await get_task(session, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
        await _require_manage_share(session, user, task)
        await r_.remove_share(session, task_id=task_id, user_id=user_id)
        await write_audit(session, task_id=task_id, operator="user",
                          action="share_remove", detail={"by": user.id, "from": user_id})
        return {"ok": True}


async def _require_caps(session, user: UserPrincipal, task, need: str) -> None:
    """🔴 统一权限判定（读→can_view，写→can_edit）；无权限一律 404（防任务 ID 枚举）。
    AUTH off 匿名 → can_* 恒 True（P2 兼容）。"""
    from app.auth import permissions as perm

    if need == "edit":
        ok = await perm.can_edit(session, user, task)
    else:
        ok = await perm.can_view(session, user, task)
    if not ok:
        raise HTTPException(status_code=404, detail="Not Found")
