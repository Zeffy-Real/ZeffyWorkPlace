"""断点恢复 + lease 巡检（P2/P3）。

🔴 审查核心：
- **恢复白名单（P3 修订）**：仅「lease 过期 / 无 lease 且超时」的 auto 节点 ``running`` 重新入队；
  **``queued`` 任务保留在 ARQ 队列（ARQ 持久化重启保留），不重复入队**；``done/failed/blocked/pending`` 不动；
  HITL/human 等待节点不自动入队（留人工 resume）。
- **分区扫描锁（P3）**：用 Redis ``SET NX EX`` 分布式锁保证同一时刻仅一个实例全局扫描，
  避免多 worker/多 API 重复扫描入队；每次回收写 AuditLog 带 ``worker_id`` 可追溯。
- **lease 死任务回收**：worker 对 active 节点周期续约 lease（``liveness_beat``，见 arqs）；
  巡检回收两类死任务——lease 显式过期；lease 缺失且 ``updated_at`` 超 grace
  （＝ lease TTL，兜住 worker 永久死亡无 lease 痕迹的节点）。执行中的长任务因持续续约不被误杀。
- **死信**：``attempts ≥ ARQ_MAX_TRIES`` 仍滞留的 running 死任务 → failed + 审计，终止重试。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from app.config import get_settings
from app.db import repos
from app.db.models import TaskNode
from app.workflow.state_machine import FAILED, QUEUED, RUNNING
from app.workflow.templates import NODE_HITL, NODE_HUMAN, get_template

logger = logging.getLogger(__name__)

# 入队回调：async (task_id) -> None
EnqueueCb = Callable[[str], Awaitable[None]]

SCAN_LOCK_KEY = "zw:scan:lock"


def _lease_grace_seconds() -> float:
    return float(get_settings().lease_ttl)


def _lease_expired(lease: dict | None) -> bool:
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
        return False
    ref = node.updated_at or node.created_at
    if ref is None:
        return True
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ref).total_seconds() > _lease_grace_seconds()


async def _is_hitl(task, node) -> bool:
    try:
        spec = next((s for s in get_template(task.workflow_id).nodes
                     if s.name == node.node_name), None)
    except ValueError:
        return False
    return bool(spec and spec.type in {NODE_HITL, NODE_HUMAN})


async def _dead_letter(session_factory, task, node, reason: str) -> None:
    async with session_factory() as s:
        hit = await repos.dead_letter_node(s, node.id, reason=reason)
        if not hit:
            return
        await repos.set_task_status(s, task.id, FAILED)
        await s.commit()
        await repos.write_audit(s, task_id=task.id, operator="system",
                                action="dead_letter", detail={"node": node.node_name,
                                                              "attempts": node.attempts,
                                                              "reason": reason})


async def resume_inflight(session_factory, enqueue: EnqueueCb, *, redis: Any = None) -> dict:
    """分区扫描白名单内滞留任务并重新入队；返回统计。

    :param redis: 可选；提供则用 ``SET NX EX`` 抢全局扫描锁（多实例仅一个扫描），
      抢不到锁返回 ``{"locked_out":1}`` 不重复扫描。
    """
    stats = {"queued_skip": 0, "recover": 0, "hitl_skip": 0, "active_skip": 0,
             "ignored": 0, "dead_letter": 0, "re_enqueued": 0, "locked_out": 0}

    # 🔴 分区扫描锁：同一时刻仅一个实例执行全局扫描（防多实例重复入队/DB 压力）
    wid = get_settings().worker_id
    if redis is not None:
        try:
            got = await redis.set(SCAN_LOCK_KEY, wid, nx=True,
                                  ex=get_settings().DEAD_SCAN_LOCK_TTL)
        except Exception:  # noqa: BLE001 锁不可用则退化为无锁（仅告警）
            got = True
            logger.warning("scan 锁获取失败，退化为无锁执行")
        if not got:
            stats["locked_out"] = 1
            return stats

    async with session_factory() as session:
        nodes = await repos.scan_nodes_by_status(session, statuses=("queued", "running"))

    seen: set[str] = set()
    for node in nodes:
        # 🔴 P3：queued 保留在 ARQ（持久化），不重复入队；只处理 running 死任务
        if node.status != RUNNING:
            stats["queued_skip"] += 1
            continue
        if node.task_id in seen:
            continue
        async with session_factory() as s:
            task = await repos.get_task(s, node.task_id)
        if task is None or task.status in {"done", "failed"}:
            stats["ignored"] += 1
            continue

        # 🔴 死信：attempts 超限仍滞留 running → 终止重试
        if node.attempts >= get_settings().ARQ_MAX_TRIES:
            await _dead_letter(session_factory, task, node,
                               f"重试次数超限（attempts={node.attempts}）")
            stats["dead_letter"] += 1
            continue

        if await _is_hitl(task, node):
            stats["hitl_skip"] += 1
            continue
        if not _running_stale(node):
            stats["active_skip"] += 1
            continue

        # 死任务：回置 queued + 清 lease，重新入队
        async with session_factory() as s:
            if not await repos.set_node_status(s, node.id, RUNNING, QUEUED):
                continue  # 并发已流转
            await s.execute(update(TaskNode).where(TaskNode.id == node.id).values(lease=None))
            await s.commit()
            await repos.write_audit(s, task_id=task.id, operator="system",
                                    action="scan_recover", detail={"node": node.node_name,
                                                                    "worker_id": wid,
                                                                    "task_id": task.id})
        stats["recover"] += 1
        try:
            await enqueue(node.task_id)
        except Exception as exc:  # noqa: BLE001 巡检入队失败仅告警，下轮重试
            logger.warning("巡检重入队失败 task=%s：%s", node.task_id, exc)
            continue
        seen.add(node.task_id)
        stats["re_enqueued"] += 1
    return stats
