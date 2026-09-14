"""P1-2 状态机测试：合法 / 非法迁移、终态、WorkflowStateError。"""

import pytest

from app.workflow.state_machine import (
    WorkflowStateError,
    assert_transition,
    can_transition,
)


def test_can_transition_legal_paths():
    assert can_transition("pending", "running")
    assert can_transition("pending", "failed")
    assert can_transition("running", "done")
    assert can_transition("running", "failed")
    assert can_transition("running", "blocked")
    assert can_transition("blocked", "running")
    assert can_transition("done", "running")  # 评审打回重做（A2）
    assert can_transition("done", "failed")


def test_illegal_transitions_are_rejected():
    assert not can_transition("pending", "done")     # 未经 running 直接 done
    assert not can_transition("failed", "running")   # 终态不可回退
    assert not can_transition("failed", "done")
    assert not can_transition("blocked", "done")     # 须先回 running


def test_assert_transition_raises_on_illegal():
    with pytest.raises(WorkflowStateError):
        assert_transition("pending", "done")
    assert_transition("pending", "running")  # 合法不抛
