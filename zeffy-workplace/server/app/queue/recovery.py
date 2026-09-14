"""断点恢复 + lease 巡检（P2）。

🔴 审查核心：
- **恢复白名单**：仅 ``queued``（滞留超时）与「lease 过期 / 无 lease 且长时间无更新」的
  auto 节点 running 重新入队；``done / failed / blocked / pending`` 一律不动；
  HITL/human 等待节点**不自动入队**（留人工 resume）。
- **一致性巡检（🔴）**：入队成功但 job 丢失 / worker 死亡时节点会滞留 ``queued``——
  ``queued_at`` 距今超 ``QUEUED_STALE_SECONDS`` 才重新入队（避免与刚入队的活跃 job 竞争），
  幂等键 ``_job_id=task_id`` 保证不重复执行。
- **死信（🔴）**：``attempts`` ≥ ``ARQ_MAX_TRIES`` 仍滞留 → 节点置 failed + 任务 failed + 审计，
  终止无限重试。
- **lease 死任务回收（🔴）**：worker 每轮对 active 节点续约 lease（见 arqs._make_lease_renewer）；
  巡检回收两类死任务——lease 显式过期；lease 缺失且 ``updated_at`` 超过 grace
  （= ARQ_JOB_TIMEOUT × 1.5，兜住 worker 永久死亡无任何 lease 痕迹的节点）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import update

from app.config import get_settings
from app.db import repos
from app.db.models import TaskNode
from app.workflow.state_machine import FAILED, QUEUED, RUNNING
from app.workflow.templates import NODE_HITL, NODE_HUMAN, get_template

logger = logging.getLogger(__name__)

# 入队回调：async (task_id) -> None
EnqueueCb = Callable[[str], Awaitable[None]]


def _lease_grace_seconds() -> float:
    return get_settings().ARQ_JOB_TIMEOUT * 1.5


def _lease_expired(lease: dict | None) -> bool:
    """lease 显式存在且已过期 → 死任务。lease 缺失由 updated_at 兜底判断。"""
    if not lease:
        return False
    try:
        exp = datetime.fromisoformat(lease["expire_at"])
        return datetime.now(UTC) > exp.replace(tzinfo=UTC)
    except Exception:  # noqa: BLE001
        return False


def _running_stale(node: TaskNode) -> bool:
    """running 节点死任务判定：lease 过期，或无 lease 且 updated_at 超过 grace。"""
    if _lease_expired(node.lease):
        return True
    if node.lease:
        return False  # 有 lease 且未过期 → 活跃
    # lease 缺失：用 updated_at（最后状态变更时间）兜底
    ref = node.updated_at or node.created_at
    if ref is None:
        return True
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ref).total_seconds() > _lease_grace_seconds()


def _queued_stale(node: TaskNode) -> bool:
    """queued 滞留判定：入队后超过 QUEUED_STALE_SECONDS 仍未被认领。"""
    ref = node.queued_at or node.created_at
    if ref is None:
        return True
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ref).total_seconds() > get_settings().QUEUED_STALE_SECONDS


async def _is_hitl(task, node) -> bool:
    try:
        spec = next((s for s in get_template(task.workflow_id).nodes
                     if s.name == node.node_name), None)
    except ValueError:
        return False
    return bool(spec and spec.type in {NODE_HITL, NODE_HUMAN})


async def _dead_letter(session_factory, task, node, reason: str) -> None:
    """死信：节点 failed + 任务 failed + 审计（终止无限重试）。"""
    async with session_factory() as s:
        hit = await repos.dead_letter_node(s, node.id, reason=reason)
        if not hit:
            return  # 节点已流转（并发）→ 不动任务
        await repos.set_task_status(s, task.id, FAILED)
        await s.commit()
        await repos.write_audit(s, task_id=task.id, operator="system",
                                action="dead_letter", detail={"node": node.node_name,
                                                              "attempts": node.attempts,
                                                              "reason": reason})


async def resume_inflight(session_factory, enqueue: EnqueueCb) -> dict:
    """扫描白名单内滞留任务并重新入队；返回统计（供测试/日志断言）。"""
    stats = {"queued": 0, "queued_fresh_skip": 0, "recover": 0, "hitl_skip": 0,
             "active_skip": 0, "ignored": 0, "dead_letter": 0, "re_enqueued": 0}
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

        # 🔴 死信：attempts 超限仍滞留（queued 或 running）→ 终止重试
        if node.attempts >= get_settings().ARQ_MAX_TRIES:
            await _dead_letter(session_factory, task, node,
                               f"重试次数超限（attempts={node.attempts}）")
            stats["dead_letter"] += 1
            continue

        recover = False
        if node.status == QUEUED:
            if not _queued_stale(node):
                # 刚入队（活跃窗口内）→ 不与在途 job 竞争
                stats["queued_fresh_skip"] += 1
                continue
            stats["queued"] += 1
            recover = True
        elif node.status == RUNNING:
            if await _is_hitl(task, node):
                stats["hitl_skip"] += 1
                continue
            if not _running_stale(node):
                stats["active_skip"] += 1
                continue
            # 死任务：回置 queued + 清 lease，重新入队
            async with session_factory() as s:
                if await repos.set_node_status(s, node.id, RUNNING, QUEUED):
                    await s.execute(update(TaskNode).where(TaskNode.id == node.id)
                                    .values(lease=None))
                    await s.commit()
            stats["recover"] += 1
            recover = True

        if recover:
            try:
                await enqueue(node.task_id)
            except Exception as exc:  # noqa: BLE001 巡检入队失败仅告警，下轮重试
                logger.warning("巡检重入队失败 task=%s：%s", node.task_id, exc)
                continue
            seen.add(node.task_id)
            stats["re_enqueued"] += 1
    return stats
