"""断点恢复 + lease 巡检（P2）。

🔴 审查核心：
- **恢复白名单**：仅 ``queued`` 与「lease 过期的 auto 节点 running」重新入队；
  ``done / failed / blocked / pending`` 一律不动；HITL/human 等待节点**不自动入队**（留人工 resume）。
- **lease 死任务回收**：``running`` 且 lease 过期（超 ARQ_JOB_TIMEOUT×1.5）→ 回置 queued + 清 lease，重新入队。
- **去重**：入队用 ``_job_id=task_id``（与正常入队同键），ARQ 对同 id 二次入队不重复执行。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import update

from app.db import repos
from app.db.models import TaskNode
from app.workflow.state_machine import QUEUED, RUNNING
from app.workflow.templates import NODE_HITL, NODE_HUMAN, get_template

logger = logging.getLogger(__name__)

# 入队回调：async (task_id) -> None
EnqueueCb = Callable[[str], Awaitable[None]]


def _lease_stale(lease: dict | None) -> bool:
    """仅当**显式存在且过期**才视为死任务。

    lease=None（任务级 job 的后续节点在活跃执行中本无 lease）→ 视为「活跃未知」，**不回收**，
    交给 ARQ 悲观执行在崩溃时自动重试；避免巡检把正在推进的节点误置 queued 造成并发冲突。
    """
    if not lease:
        return False
    try:
        exp = datetime.fromisoformat(lease["expire_at"])
        return datetime.now(UTC) > exp.replace(tzinfo=UTC)
    except Exception:  # noqa: BLE001
        return False


async def _is_hitl(task, node) -> bool:
    try:
        spec = next((s for s in get_template(task.workflow_id).nodes
                     if s.name == node.node_name), None)
    except ValueError:
        return False
    return bool(spec and spec.type in {NODE_HITL, NODE_HUMAN})


async def resume_inflight(session_factory, enqueue: EnqueueCb) -> dict:
    """扫描白名单内滞留任务并重新入队；返回统计（供测试/日志断言）。"""
    stats = {"queued": 0, "recover": 0, "hitl_skip": 0, "active_skip": 0, "ignored": 0,
             "re_enqueued": 0}
    async with session_factory() as session:
        nodes = await repos.scan_nodes_by_status(session, statuses=("queued", "running"))

    seen: set[str] = set()
    for node in nodes:
        if node.task_id in seen:
            continue
        async with session_factory() as s:
            task = await repos.get_task(s, node.task_id)
        if task is None or task.status in {"done", "failed"}:
            stats["ignored"] += 1
            continue

        recover = False
        if node.status == QUEUED:
            stats["queued"] += 1
            recover = True
        elif node.status == RUNNING:
            if await _is_hitl(task, node):
                stats["hitl_skip"] += 1
                continue
            if not _lease_stale(node.lease):
                # lease=None（活跃/未知）或未过期 → 不回收（ARQ 悲观执行兜底）
                stats["active_skip"] += 1
                continue
            # lease 显式过期死任务：回置 queued + 清 lease
            async with session_factory() as s:
                if await repos.set_node_status(s, node.id, RUNNING, QUEUED):
                    await s.execute(update(TaskNode).where(TaskNode.id == node.id)
                                    .values(lease=None))
                    await s.commit()
            stats["recover"] += 1
            recover = True

        if recover:
            await enqueue(node.task_id)
            seen.add(node.task_id)
            stats["re_enqueued"] += 1
    return stats
