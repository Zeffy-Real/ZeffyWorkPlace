"""全局运行时状态（进程内存）。

预埋 LLM 并发信号量，作为对 LLM 429 限流的**背压**：
- 限制同时进行的 LLM 调用数，防止大量并发任务触发 429 时协程无限堆积、event-loop 饥饿。
- P1 单进程有效；多实例部署（P2）需改为分布式信号量/网关侧限流。
- **P1-3 起，Agent 调用 ``LLMClient.agenerate`` 前必须先 acquire 本信号量。**

创建时机：信号量须在**运行的 event-loop** 中创建以正确绑定 loop；
故由 ``app.main.lifespan`` 启动时调用 ``init_llm_semaphore()``。
"""

from __future__ import annotations

import asyncio

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
