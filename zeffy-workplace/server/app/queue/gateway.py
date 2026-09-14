"""API 侧入队入口（P2）：DB 优先 + 失败回滚 + 审计（🔴 审查一致性）。

「先更 DB 还是先入队」一致性策略（DB 优先 + 补偿）：
1. 先把就绪节点置为 ``queued``（DB 原子更新）；commit。
2. 再提交 ARQ job（``_job_id=task_id``/``resume-<task_id>-<n>`` 去重）。
3. 入队失败 → 立刻回滚节点 ``queued→pending`` + 写审计 + 抛错（由调用方转前端通知）。
4. 后台巡检（recovery）兜底扫描滞留 ``queued`` 的任务重新入队。
"""

from __future__ import annotations

import logging
from typing import Any

from app.db import repos
from app.workflow import engine as engine_mod
from app.workflow.state_machine import PENDING, QUEUED, WorkflowStateError
from app.workflow.templates import get_template

logger = logging.getLogger(__name__)


def make_worker_context(session_factory, registry: Any, publish_redis: Any) -> dict:
    """构建 ARQ ctx 依赖（worker 与 API 测试共享）。"""
    return {
        "session_factory": session_factory,
        "registry": registry,
        "publish_redis": publish_redis,
    }


async def mark_ready_queued(session, task, tpl=None) -> bool:
    """把第一个「就绪且 pending」的节点置 queued；成功 True，无就绪 False。"""
    tpl = tpl or get_template(task.workflow_id)
    nodes = {n.node_name: n for n in await repos.list_nodes(session, task.id)}
    for spec in tpl.nodes:
        node = nodes[spec.name]
        if node.status != PENDING:
            continue
        deps = spec.depends_on or []
        if not all(nodes[d].status == "done" for d in deps):
            continue
        if await repos.set_node_queued(session, node.id):
            await session.commit()
            return True
    return False


async def enqueue_task(session, task, pool, *, emit: Any = None) -> None:
    """DB 优先标记 queued + 入队 run_agent_task；入队失败回滚 + 审计。"""
    if task.status in {"done", "failed"}:
        return
    tpl = get_template(task.workflow_id)
    if not await repos.list_nodes(session, task.id):
        await engine_mod.prepare(session, task)  # 建节点（不激活）+ 拓扑校验
    queued = await mark_ready_queued(session, task, tpl)
    if not queued:
        return  # 无可入队节点（如仅剩 HITL 等人工），等待
    try:
        await pool.enqueue_job("run_agent_task", task.id, _job_id=task.id)
        await repos.write_audit(session, task_id=task.id, operator="system",
                                action="enqueue_task", detail={"task_id": task.id})
        if emit:
            await emit("task_update",
                       {"task_id": task.id, "event": "queued", "payload": {"task_db_id": task.id}})
    except Exception as exc:  # noqa: BLE001 入队失败补偿回滚
        logger.warning("任务入队失败补偿回滚 task=%s：%s", task.id, exc)
        for node in await repos.list_nodes(session, task.id):
            if node.status == QUEUED:
                await repos.set_node_status(session, node.id, QUEUED, PENDING)
        await session.commit()
        await repos.write_audit(session, task_id=task.id, operator="system",
                                action="enqueue_failed", detail={"error": str(exc)})
        raise


async def enqueue_resume(session, task_id: str, decision: dict, pool,
                         *, emit: Any = None) -> None:
    """HITL 人工决策：持久化到节点 payload + 入队 run_agent_resume（独立 job 键去重）。"""
    # 找到被中断（running/blocked）的节点，持久化决策
    nodes = await repos.list_nodes(session, task_id)
    target = next((n for n in nodes if n.status in {"running", "blocked", "queued"}), None)
    if target is None:
        raise WorkflowStateError(f"任务 {task_id} 无可恢复节点，拒绝重复提交决策")
    await repos.set_node_payload(session, target.id, {"decision": decision})
    await session.commit()
    job_key = f"resume-{task_id}-{target.id[:8]}"
    try:
        await pool.enqueue_job("run_agent_resume", task_id, decision, _job_id=job_key)
        await repos.write_audit(session, task_id=task_id, operator="user", action="enqueue_resume",
                                detail={"node": target.node_name, "kind": decision.get("kind")})
        if emit:
            await emit("task_update", {"task_id": task_id, "event": "resume_queued"})
    except Exception as exc:  # noqa: BLE001
        await repos.write_audit(session, task_id=task_id, operator="system",
                                action="enqueue_resume_failed", detail={"error": str(exc)})
        raise
