"""TaskNode 状态机（P1-2）。

业务真相源的**唯一权威流转定义**：非法迁移抛 ``WorkflowStateError``。
约束：LangGraph 仅作 Agent 内部执行图，严禁驱动本状态机 / 直接改 TaskNode 表。
"""

from __future__ import annotations

# 节点级状态
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"

# 合法迁移表
# - pending -> running：节点开始执行
# - running -> done/failed/blocked：执行结束
# - blocked -> running：解除阻塞重试
# - done -> running：评审打回后重做（A2 回流用）
# - failed 为终态（重试由 engine 在校验后显式置回）
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    PENDING: {RUNNING, FAILED, BLOCKED},
    RUNNING: {DONE, FAILED, BLOCKED},
    DONE: {RUNNING, FAILED},
    BLOCKED: {RUNNING, FAILED},
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
