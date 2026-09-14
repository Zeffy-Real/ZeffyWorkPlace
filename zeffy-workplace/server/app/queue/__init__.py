"""queue 包（P2）：ARQ 持久任务队列 + 事件回传 + 断点恢复 + lease 巡检。

分层：
- ``arqs``：WorkerSettings、worker job 函数、pool 构造。
- ``events``：worker→API 事件回传（Redis Pub/Sub，带 seq 排序）。
- ``gateway``：API 侧入队入口（DB 优先 + 失败回滚 + 审计）。
- ``recovery``：worker 启动期 DB rescan + lease 过期巡检。
"""

from app.queue.events import TASK_EVENT_CHANNEL, publish_task_event
from app.queue.gateway import enqueue_resume, enqueue_task

__all__ = ["enqueue_task", "enqueue_resume", "TASK_EVENT_CHANNEL", "publish_task_event"]
