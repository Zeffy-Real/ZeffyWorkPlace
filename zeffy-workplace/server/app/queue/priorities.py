"""P4-4b 任务优先级：队列命名空间唯一入口（全模块复用，杜绝错表/错位）。

- 队列 ZSET 键 = ``ARQ_QUEUE_NAME`` 的派生值（worker/pool/入队/指标全走这里）。
- ``ARQ_PRIORITY_ENABLED=false``：一律基队列（P4 行为）。开启：0→_lo，1→基，2→_hi。
- DAG 全节点继承任务优先级，本模块只按优先级出队列名。
"""

from __future__ import annotations

from app.config import get_settings

PRIORITY_LOW = 0
PRIORITY_MID = 1
PRIORITY_HIGH = 2


def priority_enabled() -> bool:
    return get_settings().ARQ_PRIORITY_ENABLED


def base_queue() -> str:
    return get_settings().ARQ_QUEUE_NAME or "zeffy"


def queue_for(priority: int) -> str:
    """按优先级返回队列命名空间（ZSET 键）。DAG 全节点共用任务该值。"""
    base = base_queue()
    if not priority_enabled() or priority == PRIORITY_MID:
        return base
    if priority <= PRIORITY_LOW:
        return f"{base}_lo"
    return f"{base}_hi"


def all_queues() -> set[str]:
    """活跃命名空间全集（指标 LLEN 聚合 / 监控用）。"""
    base = base_queue()
    return {base, f"{base}_hi", f"{base}_lo"}
