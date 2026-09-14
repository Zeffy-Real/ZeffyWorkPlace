"""全局运行时状态（进程内存）。

- LLM 并发信号量：对 LLM 429 限流的背压；Agent 调用 LLM 前必须 acquire。
- P2 持久队列：共享 ARQ pool + worker→API 事件消费 + lease 巡检（仅 USE_QUEUE 启用）。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 默认最大并发 LLM 调用（背压上限）。
LLM_MAX_CONCURRENCY = 32

# P4-4b：LLM 分级配额池（高/中/低 + 公共池）。缺省做「单一共享」降级（P4 兼容）。
_llm_pools: dict[str, asyncio.Semaphore] | None = None


def init_llm_semaphore(max_concurrency: int = LLM_MAX_CONCURRENCY) -> None:
    """lifespan 启动：按分级配额建池（hi/mid/lo/pub）。"""
    global _llm_pools
    from app.config import get_settings

    s = get_settings()
    hi = s.LLM_QUOTA_HI
    mid = s.LLM_QUOTA_MID
    lo = s.LLM_QUOTA_LO
    pub = s.LLM_QUOTA_PUBLIC or max(0, max_concurrency - hi - mid - lo)
    _llm_pools = {
        "hi": asyncio.Semaphore(hi),
        "mid": asyncio.Semaphore(mid),
        "lo": asyncio.Semaphore(lo),
        "pub": asyncio.Semaphore(pub),
    }


def _pools() -> dict[str, asyncio.Semaphore]:
    global _llm_pools
    if _llm_pools is None:
        init_llm_semaphore()
    assert _llm_pools is not None
    return _llm_pools


class _LlmQuota:
    """分级配额上下文管理器：低优仅用 lo 保底；高/中优主池满则借 pub（不动 lo）。"""

    __slots__ = ("_pools", "_priority", "_pool")

    def __init__(self, pools: dict[str, asyncio.Semaphore], priority: int) -> None:
        self._pools = pools
        self._priority = priority
        self._pool: asyncio.Semaphore | None = None

    async def __aenter__(self) -> _LlmQuota:
        pools = self._pools
        if self._priority <= 0:
            # 🔴 低优保底：只走 lo 池，绝不出借对外（防低优被高优挤死）
            await pools["lo"].acquire()
            self._pool = pools["lo"]
            return self
        key = "hi" if self._priority >= 2 else "mid"
        primary = pools[key]
        # 主池有容量则用主池，否则借公共池（不动 lo 保底）。Py3.13 Semaphore 无 acquire_nowait，
        # 用 _value 容量判断 + acquire 的读改写（事件循环单线程下竞态仅致少量借用偏差，无正确性影响）。
        if getattr(primary, "_value", 0) > 0:
            await primary.acquire()
            self._pool = primary
        else:
            await pools["pub"].acquire()
            self._pool = pools["pub"]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._pool is not None:
            self._pool.release()


def llm_quota(priority: int) -> _LlmQuota:
    """按任务优先级取对应分级的配额上下文管理器，供 Agent LLM 调用包裹。"""
    return _LlmQuota(_pools(), priority)


# 任务优先级 contextvar：AgentRunner 每任务设置，Agent._call 据此选配额池；跨任务上下文隔离。
_priority_var: contextvars.ContextVar[int] = contextvars.ContextVar("zw_priority", default=1)


def set_priority(p: int) -> None:
    _priority_var.set(max(0, int(p or 0)))


def get_priority() -> int:
    return _priority_var.get()


# ---- 兼容旧引用：单一共享语义（未用则保留，供陈旧调用方） ----
def get_llm_semaphore() -> asyncio.Semaphore:
    """兼容旧签名：返回公共池（旧调用方按默认优先级执行时仍可用）。"""
    return _pools()["pub"]


# ---------------------------------------------------------------------------
# P2 持久队列：共享 ARQ pool + 事件消费 + lease 巡检（仅 USE_QUEUE 启用）
# ---------------------------------------------------------------------------

SWEEP_INTERVAL_SECONDS = 30

_arq_pool: Any | None = None
_consumer_task: asyncio.Task | None = None
_sweeper_task: asyncio.Task | None = None
_monitor_task: asyncio.Task | None = None
_workqueue_ready = False


def workqueue_enabled() -> bool:
    from app.config import get_settings

    return get_settings().USE_QUEUE


def get_arq_pool():
    return _arq_pool


async def init_workqueue(session_factory, event_handler) -> None:
    """API 侧队列就绪：创建 pool + 起事件消费与 lease 巡检。须在 event-loop（lifespan）内调用。"""
    global _arq_pool, _consumer_task, _sweeper_task, _monitor_task, _workqueue_ready
    if not workqueue_enabled() or _workqueue_ready:
        return

    import redis.asyncio as aioredis

    from app.config import get_settings as _gs
    from app.queue.arqs import create_arq_pool
    from app.queue.events import consume_task_events
    from app.queue.recovery import resume_inflight

    _arq_pool = await create_arq_pool()

    consumer_redis = aioredis.from_url(_gs().REDIS_URL)

    async def _consumer() -> None:
        try:
            await consume_task_events(consumer_redis, event_handler, stop_event=asyncio.Event())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("事件消费异常退出：%s", exc)

    async def _sweeper() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                async def _enqueue(tid: str) -> None:
                    from app.db import repos as _r
                    from app.queue.priorities import queue_for

                    pri = 1
                    try:
                        async with session_factory() as _s:
                            _t = await _r.get_task(_s, tid)
                            pri = _t.priority if _t else 1
                    except Exception:  # noqa: BLE001
                        pri = 1
                    await _arq_pool.enqueue_job("run_agent_task", tid, _job_id=tid,
                                                _queue_name=queue_for(pri))
                # 🔴 P3 分区扫描锁：多实例仅一个持有者扫描（redis=pool 提供 SET NX EX）
                await resume_inflight(session_factory, _enqueue, redis=_arq_pool)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("lease 巡检失败", exc_info=True)

    _consumer_task = asyncio.create_task(_consumer())
    _sweeper_task = asyncio.create_task(_sweeper())
    _workqueue_ready = True
    logger.info("P3 队列就绪 USE_QUEUE=True")


async def shutdown_workqueue() -> None:
    """回收事件消费/巡检协程 + 关闭 pool。"""
    global _arq_pool, _consumer_task, _sweeper_task, _monitor_task, _workqueue_ready
    if not _workqueue_ready:
        return
    for t in (_consumer_task, _sweeper_task, _monitor_task):
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    if _arq_pool is not None:
        try:
            await _arq_pool.aclose()
        except Exception:  # noqa: BLE001
            pass
    _consumer_task = _sweeper_task = _monitor_task = _arq_pool = None
    _workqueue_ready = False
