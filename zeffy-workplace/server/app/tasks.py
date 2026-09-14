"""后台任务抽象 —— 进程内 TaskRunner（P1 壳）。

⚠️⚠️⚠️ 重大限制（审查强制，切勿忽略）⚠️⚠️⚠️
- 本实现基于 ``asyncio.create_task``，是「进程内内存任务，无持久化」。
- **服务重启 / OOM / Ctrl-C 期间，所有运行中、等待 HITL 中断的任务全部丢失，状态无法恢复**。
- 没有持久化队列、没有任务持久记录、没有任务重试、没有死信。
- 持久任务队列 + 断点恢复（Celery/ARQ）属 **P2**。
- P1 开发测试**严禁通过重启服务来使长任务生效**；验收该限制的回避方式见 P1 施工文档。

职责边界（P1）：
- ``submit``：把任务投递到进程内后台协程，**立即返回**——业务长任务禁止在 WS 协程内运行。
- ``subscribe``：按 run_id 订阅进度回调，用于经 WS 推送状态。
- ``shutdown``：lifespan 关闭时 cancel + await 所有运行中任务，避免
  ``Task was destroyed but it is pending`` 告警与协程泄漏。
- 未捕获异常**不允许静默吞掉**：发 ``failed`` 事件（订阅方可写审计 + 置 Task failed）。
  **TaskRunner 本身不实现重试**；重试归属 WorkflowEngine / AgentRunner 层。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 任务执行函式：接收「run 上下文」字典，返回任意结果。
TaskFn = Callable[[dict[str, Any]], Awaitable[Any]]
# 进度回调：订阅者用于把事件经 WS 推送给前端。
ProgressCb = Callable[["TaskRunner", str, str, dict[str, Any]], Awaitable[None]]


@dataclass
class _TaskRecord:
    run_id: str
    task_db_id: str | None
    meta: dict[str, Any]
    coro: asyncio.Task[Any] | None = field(default=None)


class TaskRunner:
    """进程内后台任务执行器（仅 P1 单 worker；多实例属 P2）。"""

    def __init__(self) -> None:
        self._records: dict[str, _TaskRecord] = {}
        self._subscribers: dict[str, list[ProgressCb]] = {}

    def subscribe(self, run_id: str, cb: ProgressCb) -> None:
        """订阅某 run_id 的进度事件（须先于 submit 或立刻调用均可）。"""
        self._subscribers.setdefault(run_id, [])
        self._subscribers[run_id].append(cb)

    async def _emit(self, run_id: str, event: str, payload: dict[str, Any]) -> None:
        for cb in list(self._subscribers.get(run_id, [])):
            try:
                await cb(self, run_id, event, payload)
            except Exception:  # noqa: BLE001 订阅回调异常不影响任务本体
                logger.exception("进度回调失败 run_id=%s event=%s", run_id, event)

    def submit(
        self,
        run_id: str,
        fn: TaskFn,
        *,
        task_db_id: str | None = None,
        **meta: Any,
    ) -> None:
        """投递后台任务，立即返回。run_id 重复则抛错（防覆盖）。"""
        if run_id in self._records:
            raise RuntimeError(f"run_id 已存在：{run_id}（禁止覆盖运行中任务）")
        record = _TaskRecord(
            run_id=run_id,
            task_db_id=task_db_id,
            meta={"run_id": run_id, "task_db_id": task_db_id, **meta},
        )
        record.coro = asyncio.create_task(self._run(record, fn), name=f"ztask:{run_id}")
        self._records[run_id] = record

    async def _run(self, record: _TaskRecord, fn: TaskFn) -> None:
        try:
            await self._emit(record.run_id, "running", {"task_db_id": record.task_db_id})
            result = await fn(record.meta)
            await self._emit(record.run_id, "done", {"result": result})
        except asyncio.CancelledError:
            logger.warning("后台任务被取消 run_id=%s", record.run_id)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("后台任务异常 run_id=%s", record.run_id)
            # 不静默：发 failed 事件，订阅方负责写 AuditLog + 置 Task failed。
            await self._emit(
                record.run_id,
                "failed",
                {"error": str(exc), "task_db_id": record.task_db_id},
            )
        finally:
            self._records.pop(record.run_id, None)
            self._subscribers.pop(record.run_id, None)

    @property
    def active_count(self) -> int:
        return len(self._records)

    async def shutdown(self) -> None:
        """取消并等待全部运行中任务；防 pending 告警与协程泄漏。"""
        pending = [r.coro for r in self._records.values() if r.coro and not r.coro.done()]
        if not pending:
            return
        logger.warning("TaskRunner shutdown：取消 %d 个运行中任务", len(pending))
        for coro in pending:
            coro.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._records.clear()
        self._subscribers.clear()


# 进程级单例（P1 单 worker 有效）。
_default: TaskRunner | None = None


def get_runner() -> TaskRunner:
    """获取全局 TaskRunner 单例。"""
    global _default
    if _default is None:
        _default = TaskRunner()
    return _default
