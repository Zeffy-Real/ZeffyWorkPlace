"""ARQ worker（P2）：任务级 job + 原子认领(lease) + 崩溃防护。

- 独立进程运行：``python -m arq app.queue.arqs.WorkerSettings``（Makefile ``worker``）。
- **单 worker 定位（审查⭐）**：P2 始终单 worker 进程，不并发 claim；多 worker 水平扩展属 P3。
- job 隔离：每个 job 新 AgentRunner；进程级复用 session_factory / 工具注册表 / publish 连接（on_startup 构造）。
- 🔴 job 异常**绝不允许击穿 worker 主进程**：最外层 try/except → 置 running 节点 failed + 审计。
- 🔴 幂等：``_job_id=task_id``（普通）/ ``resume-<task>-<node>`（resume）去重；节点认领用原子 ``queued→running`` 乐观锁。
- 🔴 lease：claim 时写 ``{worker_id, expire_at}``（ARQ_JOB_TIMEOUT×1.5）；lease 过期死任务由 recovery 巡检重置入队。
- 副作用幂等（审查三层之三）：Agent 工具沿用 fs「存在判断/临时文件+rename」约定，重跑不重复覆盖。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from arq import create_pool
from arq.connections import RedisSettings

from app.agents.runner import AgentRunner
from app.config import get_settings
from app.db import repos
from app.queue.events import publish_task_event
from app.workflow.state_machine import FAILED, RUNNING, WorkflowStateError

logger = logging.getLogger(__name__)

JOB_RUN = "run_agent_task"
JOB_RESUME = "run_agent_resume"


def make_emit(publish_redis: Any, task_id: str):
    """构造 worker 侧 emit：逐条 publish 到事件通道。

    🔴 seq 用 Redis INCR 生成 per-task 单调递增（跨 job 不重置），使 API 事件序列器
    （按 task_id 期望单调）在 run/resume 等多次 job 间也能正确接受；无 incr（测试桩）则回退内存计数。
    """
    st = {"seq": 0}
    key = f"taskseq:{task_id}"

    async def emit(kind: str, payload: dict) -> None:
        try:
            seq = await publish_redis.incr(key)
        except Exception:  # noqa: BLE001 测试桩无 incr → 内存计数
            st["seq"] += 1
            seq = st["seq"]
        try:
            await publish_task_event(publish_redis, task_id, seq, kind, payload)
        except Exception as exc:  # noqa: BLE001 发布失败不阻断执行（对账兜底）
            logger.warning("事件发布失败 task=%s kind=%s：%s", task_id, kind, exc)

    return emit


async def run_agent_task(ctx, task_id: str) -> dict | None:
    """worker job：认领节点 + 驱动任务到中断/完成。返回 outcome dict。"""
    sf = ctx["session_factory"]
    registry = ctx["registry"]
    publish_redis = ctx["publish_redis"]
    try:
        async with sf() as session:
            # claim：若存在 queued 就绪节点，原子认领为 running
            await _do_claim(session, task_id)
            await session.commit()
            runner = AgentRunner()
            runner.registry = registry
            return await runner.run(session, task_id, emit=make_emit(publish_redis, task_id))
    except WorkflowStateError as exc:
        await _fail_task_running_node(ctx, task_id, str(exc))
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 不击穿 worker
        logger.exception("run_agent_task 异常 task=%s", task_id)
        await _fail_task_running_node(ctx, task_id, f"{type(exc).__name__}: {exc}")
        return {"status": "error", "error": str(exc)}


async def run_agent_resume(ctx, task_id: str, decision: dict) -> dict:
    """worker job：对中断任务应用人工决策并续跑。"""
    sf = ctx["session_factory"]
    registry = ctx["registry"]
    publish_redis = ctx["publish_redis"]
    try:
        async with sf() as session:
            runner = AgentRunner()
            runner.registry = registry
            return await runner.run_resume(session, task_id, decision,
                                           emit=make_emit(publish_redis, task_id))
    except WorkflowStateError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_agent_resume 异常 task=%s", task_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


async def _do_claim(session, task_id: str) -> str | None:
    """原子认领第一个 queued 就绪节点 → running，写 lease+attempts；无则 None。"""
    nodes = await repos.list_nodes(session, task_id)
    for n in nodes:
        if n.status != "queued":
            continue
        lease_expire = datetime.now(UTC) + timedelta(
            seconds=get_settings().ARQ_JOB_TIMEOUT * 1.5
        )
        if await repos.claim_node(session, n.id, worker_id=get_settings().WORKER_ID,
                                  lease_expire_at=lease_expire):
            return n.node_name
    return None


async def _fail_task_running_node(ctx, task_id: str, reason: str) -> None:
    """把任务当前 running 节点置 failed（若存在），任务 failed，写审计。不击穿。"""
    try:
        async with ctx["session_factory"]() as session:
            nodes = await repos.list_nodes(session, task_id)
            running = next((n for n in nodes if n.status == RUNNING), None)
            if running:
                if not await repos.set_node_status(session, running.id, RUNNING, FAILED):
                    logger.warning("节点 %s 非 running，无法置 failed")
                await repos.set_node_error(session, running.id, reason)
            await repos.set_task_status(session, task_id, FAILED)
            await session.commit()
            await repos.write_audit(session, task_id=task_id, operator="system",
                                    action="worker_job_failed", detail={"error": reason})
    except Exception:  # noqa: BLE001 兜底的兜底
        logger.exception("fail_task_running_node 失败 task=%s", task_id)


# ---------------------------------------------------------------------------
# Worker 生命周期
# ---------------------------------------------------------------------------


def redis_settings() -> RedisSettings:
    url = get_settings().REDIS_URL  # 形如 redis://localhost:6380
    host, port = "localhost", 6380
    try:
        host = url.split("://")[1].split(":")[0]
        port = int(url.split("://")[1].split(":")[1].split("/")[0])
    except Exception:  # noqa: BLE001
        pass
    return RedisSettings(host=host, port=port)


async def on_startup(ctx: dict) -> None:
    import redis.asyncio as aioredis

    from app.config import get_settings as _gs
    from app.db.base import get_session_factory
    from app.tools.fs import make_fs_tools
    from app.tools.registry import ToolRegistry

    ctx["session_factory"] = get_session_factory()
    reg = ToolRegistry()
    for spec in make_fs_tools(_gs().WORKSPACE_ROOT):
        reg.register(spec)
    ctx["registry"] = reg
    # 独立 publish 连接（避免与 ARQ 内部连接抢占）
    url = _gs().REDIS_URL
    ctx["publish_redis"] = aioredis.from_url(url)
    logger.info("ARQ worker 启动完成 worker_id=%s", _gs().WORKER_ID)


async def on_shutdown(ctx: dict) -> None:
    pr = ctx.pop("publish_redis", None)
    try:
        if pr is not None:
            await pr.aclose()
    except Exception:  # noqa: BLE001
        pass


class WorkerSettings:
    """arq 入口：python -m arq app.queue.arqs.WorkerSettings。"""

    functions = [run_agent_task, run_agent_resume]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = redis_settings()
    job_timeout = get_settings().ARQ_JOB_TIMEOUT
    max_tries = get_settings().ARQ_MAX_TRIES
    keep_result = 60  # 秒
    max_jobs = 1  # 🔴 单 worker 串行，不并发（P2 定位）


async def create_arq_pool():
    """API 侧入队用 pool（ArqRedis）。"""
    return await create_pool(redis_settings())


Settings = WorkerSettings  # arq 兼容别名
