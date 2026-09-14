"""全局运行时状态（进程内存）。

- LLM 并发信号量：对 LLM 429 限流的背压；Agent 调用 LLM 前必须 acquire。
- P2 持久队列：共享 ARQ pool + worker→API 事件消费 + lease 巡检（仅 USE_QUEUE 启用）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 默认最大并发 LLM 调用（背压上限）。
LLM_MAX_CONCURRENCY = 32

_llm_semaphore: asyncio.Semaphore | None = None


def init_llm_semaphore(max_concurrency: int = LLM_MAX_CONCURRENCY) -> None:
    """在 lifespan 启动时创建信号量（须在运行中的 loop 内调用）。"""
    global _llm_semaphore
    _llm_semaphore = asyncio.Semaphore(max_concurrency)


def get_llm_semaphore() -> asyncio.Semaphore:
    """返回 LLM 背压信号量（未初始化则懒创建并绑定当前 loop）。"""
    global _llm_semaphore
    if _llm_semaphore is None:
        init_llm_semaphore()
    assert _llm_semaphore is not None
    return _llm_semaphore


# ---------------------------------------------------------------------------
# P2 持久队列：共享 ARQ pool + 事件消费 + lease 巡检（仅 USE_QUEUE 启用）
# ---------------------------------------------------------------------------

SWEEP_INTERVAL_SECONDS = 30

_arq_pool: Any | None = None
_consumer_task: asyncio.Task | None = None
_sweeper_task: asyncio.Task | None = None
_workqueue_ready = False


def workqueue_enabled() -> bool:
    from app.config import get_settings

    return get_settings().USE_QUEUE


def get_arq_pool():
    return _arq_pool


async def init_workqueue(session_factory, event_handler) -> None:
    """API 侧队列就绪：创建 pool + 起事件消费与 lease 巡检。须在 event-loop（lifespan）内调用。"""
    global _arq_pool, _consumer_task, _sweeper_task, _workqueue_ready
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
                    await _arq_pool.enqueue_job("run_agent_task", tid, _job_id=tid)
                await resume_inflight(session_factory, _enqueue)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("lease 巡检失败", exc_info=True)

    _consumer_task = asyncio.create_task(_consumer())
    _sweeper_task = asyncio.create_task(_sweeper())
    _workqueue_ready = True
    logger.info("P2 队列就绪 USE_QUEUE=True")


async def shutdown_workqueue() -> None:
    """回收事件消费/巡检协程 + 关闭 pool。"""
    global _arq_pool, _consumer_task, _sweeper_task, _workqueue_ready
    if not _workqueue_ready:
        return
    for t in (_consumer_task, _sweeper_task):
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
    _consumer_task = _sweeper_task = _arq_pool = None
    _workqueue_ready = False
