"""P1-1 TaskRunner 单元测试。

覆盖：
- 任务执行并依次发 running/done 事件；
- 状态计数 active_count；
- run_id 重复提交被拦截；
- 异常走 failed 事件而非静默；
- shutdown 能取消并回收 pending 任务（防 "Task was destroyed" 泄漏）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.tasks import TaskRunner


async def _ok(meta: dict) -> str:
    return f"ok:{meta['run_id']}"


async def _fail(_meta: dict) -> None:
    raise RuntimeError("boom")


def _collector():
    events: list[tuple[str, str, dict]] = []

    async def cb(r: TaskRunner, rid: str, event: str, payload: dict) -> None:
        events.append((rid, event, payload))

    return events, cb


async def test_submit_runs_and_emits_events():
    runner = TaskRunner()
    events, cb = _collector()
    runner.subscribe("r1", cb)
    runner.submit("r1", _ok)
    # 等待任务收尾（done 事件在 run 完成后、finally 清理前触发）。
    await asyncio.gather(*[r.coro for r in runner._records.values()])
    assert [e[1] for e in events] == ["running", "done"]
    assert events[-1][2]["result"] == "ok:r1"
    assert runner.active_count == 0


async def test_duplicate_run_id_rejected():
    runner = TaskRunner()
    runner.submit("r1", _ok)
    with pytest.raises(RuntimeError):
        runner.submit("r1", _ok)
    await runner.shutdown()


async def test_exception_emits_failed_not_silent():
    runner = TaskRunner()
    events, cb = _collector()
    runner.subscribe("r1", cb)
    runner.submit("r1", _fail)
    await asyncio.gather(*[r.coro for r in runner._records.values()])
    assert [e[1] for e in events] == ["running", "failed"]
    assert "boom" in events[-1][2]["error"]


async def test_shutdown_cancels_pending_tasks():
    runner = TaskRunner()

    async def long(_meta: dict) -> None:
        await asyncio.Event().wait()  # 永不结束，模拟 HITL 等待

    runner.submit("r1", long)
    await asyncio.sleep(0)  # 让任务开始运行
    assert runner.active_count == 1
    await runner.shutdown()
    assert runner.active_count == 0
    # 无 pending 任务残留（True 表示 gather 未残留）。


async def test_shutdown_idempotent_when_empty():
    runner = TaskRunner()
    await runner.shutdown()  # 空库不应报错
    assert runner.active_count == 0
