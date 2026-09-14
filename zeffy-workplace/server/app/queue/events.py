"""worker→API 事件回传（P2）。

- 通道：``zw:tasks``（见 config.TASK_EVENT_CHANNEL）。
- 拓扑：P2 为 **一对一**（唯一 worker → 唯一 API 进程）的 worker→API 状态回传，供 API 推 WS。
  这不是 P3 的多实例多对多跨 worker 广播，工作直接铺垫 P3。
- 可靠性（🔴 审查）：
  - 每条事件带 ``seq``（worker 内单调递增）+ ``ts``；
  - API 侧 ``consume_task_events`` 按 (task_id, seq) 排序，过期/乱序事件丢弃，避免前端状态倒跳；
  - P2 接受「API 订阅断开期间丢实时事件」，靠前端进入任务页时 REST 全量拉取对账兜底（后端不重放）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# 事件通道默认名（config.TASK_EVENT_CHANNEL 覆盖）
TASK_EVENT_CHANNEL = "zw:tasks"

# 事件分发结束后，向各 task 的 subscriber 广播。
TaskHandler = Callable[[str, int, str, dict], Awaitable[None]]  # (task_id, seq, kind, payload)


def build_event(task_id: str, seq: int, kind: str, payload: dict) -> dict:
    return {"task_id": task_id, "seq": seq, "kind": kind, "payload": payload, "ts": time.time()}


async def publish_task_event(redis, task_id: str, seq: int, kind: str, payload: dict) -> None:
    """worker 侧发布一条事件到通道（发后即忘，靠 seq+对账兜底）。"""
    msg = build_event(task_id, seq, kind, payload)
    await redis.publish(TASK_EVENT_CHANNEL, json.dumps(msg))


async def consume_task_events(redis, handler: TaskHandler, *, stop_event: asyncio.Event) -> None:
    """API 侧订阅循环：把事件交给 handler（handler 内做 seq 去重/排序 + 推 WS）。

    异常自愈重连：Pub/Sub 断开/超时则重建订阅，不退出。
    """
    while not stop_event.is_set():
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(TASK_EVENT_CHANNEL)
            logger.info("订阅任务事件通道 %s", TASK_EVENT_CHANNEL)
            async for msg in pubsub.listen():
                if stop_event.is_set():
                    break
                if msg.get("type") != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                except (ValueError, TypeError):
                    continue
                try:
                    await handler(data["task_id"], data["seq"], data["kind"], data["payload"])
                except Exception:  # noqa: BLE001 单个事件处理失败不影响后续
                    logger.exception("任务事件处理失败 task=%s", data.get("task_id"))
        except Exception as exc:  # noqa: BLE001 网络/连接异常自愈
            logger.warning("任务事件订阅中断：%s，1s 后重连", exc)
            await asyncio.sleep(1)
            continue
        finally:
            try:
                await pubsub.unsubscribe(TASK_EVENT_CHANNEL)
            except Exception:  # noqa: BLE001
                pass


class EventSequencer:
    """按 (task_id, seq) 保序去重：乱序/过期事件丢弃。API 侧每个 task 一个实例。"""

    def __init__(self) -> None:
        self._next_by_task: dict[str, int] = {}

    def accept(self, task_id: str, seq: int) -> bool:
        """返回 True 表示该事件应被处理（按序且未重复）。"""
        expect = self._next_by_task.get(task_id, 1)
        if seq < expect:
            return False  # 过期/重复
        if seq == expect:
            self._next_by_task[task_id] = expect + 1
            return True
        # seq > expect：出现空缺（订阅断开漏事件），丢弃并记录，靠对账兜底
        logger.warning("事件序列跳变 task=%s expect=%s got=%s（空缺由前端对账兜底）",
                       task_id, expect, seq)
        return False
