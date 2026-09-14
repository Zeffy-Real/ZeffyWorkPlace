"""P4 trace_id 贯穿：进程内上下文 + 生成/透传。

- HTTP 请求：进入时读 ``X-Trace-ID``（无则生成）set 进 contextvar，经 AuditLog.trace_id 落库，
  响应头回传该 trace_id，前端/运维可跨请求链路追踪。
- WS 连接：连接建立时生成连接级 trace_id，消息处理沿用。
- worker 后台任务：job 开始时 set 任务级 trace_id（默认以 task 为主体）。
- 未显式给定 trace 时惰性生成新值（保证审计可追溯，不为空）。
"""

from __future__ import annotations

import contextvars
import uuid

_trace_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "zw_trace_id", default=""
)

TRACE_HEADER = "X-Trace-ID"


def new_trace_id() -> str:
    return uuid.uuid4().hex


def set_trace_id(trace_id: str | None = None) -> str:
    """set 当前 trace_id；None 则生成。返回生效的 trace_id。"""
    value = (trace_id or "").strip() or new_trace_id()
    _trace_var.set(value)
    return value


def get_trace_id() -> str:
    """取当前 trace_id；无则生成并绑定（保证审计/日志可追溯）。"""
    value = _trace_var.get()
    if not value:
        value = set_trace_id(new_trace_id())
    return value


def reset_trace() -> None:
    _trace_var.set("")
