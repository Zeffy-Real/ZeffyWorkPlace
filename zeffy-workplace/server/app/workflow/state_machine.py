"""TaskNode 状态机（P1-2）。

业务真相源的**唯一权威流转定义**：非法迁移抛 ``WorkflowStateError``。
约束：LangGraph 仅作 Agent 内部执行图，严禁驱动本状态机 / 直接改 TaskNode 表。
"""

from __future__ import annotations

# 节点级状态
PENDING = "pending"
QUEUED = "queued"  # P2：已入队等待执行（持久队列）
RUNNING = "running"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"

# 合法迁移表
# - pending -> queued（P2 入队链路）或 pending -> running（P1 直连执行/调试）
# - queued -> running：被 worker 原子认领执行
# - running -> done/failed/blocked：执行结束
# - queued -> failed / pending：入队失败 / 取消入队（🔴 异常出口）
# - blocked -> running / queued：解除阻塞 / 重新入队（HITL 恢复）
# - done -> running：评审打回后重做（A2 回流用）
# - failed 为终态（重试由 engine 在校验后显式置回）
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    PENDING: {QUEUED, RUNNING, FAILED, BLOCKED},
    QUEUED: {RUNNING, FAILED, BLOCKED, PENDING},
    RUNNING: {DONE, FAILED, BLOCKED},
    DONE: {RUNNING, FAILED},
    BLOCKED: {RUNNING, FAILED, QUEUED},
    FAILED: set(),
}


class WorkflowStateError(Exception):
    """非法状态迁移或并发状态冲突。"""


def can_transition(frm: str, to: str) -> bool:
    return to in LEGAL_TRANSITIONS.get(frm, set())


def assert_transition(frm: str, to: str) -> None:
    """校验迁移；非法即抛 ``WorkflowStateError``。"""
    if not can_transition(frm, to):
        raise WorkflowStateError(f"非法状态迁移：{frm} → {to}")
