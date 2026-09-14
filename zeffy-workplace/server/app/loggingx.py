"""P4 trace → 日志：把 trace_id 注入每条日志记录；可选 JSON 结构化输出供外部采集器关联。

- ``TraceIdFilter``：为每条日志记录附加 ``trace_id``（取自当前链路 contextvar），
  使 ELK/Loki/OTel 等外部系统可跨模块按 trace_id 聚合同一条事务的所有日志。
- ``setup_logging``：lifespan 启动调用；默认仅在根 logger 加 filter（保持现有 console），
  ``LOG_JSON_OUTPUT=true`` 时追加 JSON 结构化 handler（无需外部 logging 库，手写 formatter）。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class TraceIdFilter(logging.Filter):
    """为每条日志记录注入当前链路 trace_id（无则自动生成）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from app.tracing import get_trace_id

            record.trace_id = get_trace_id()
        except Exception:  # noqa: BLE001
            record.trace_id = ""
        return True


class JsonLogFormatter(logging.Formatter):
    """单行 JSON 日志：含 trace_id 与标准字段，便于外部日志系统按 trace 关联。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", ""),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key in ("ip", "user_id", "task_id"):
            extra = getattr(record, key, None)
            if extra:
                payload[key] = extra
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def setup_logging() -> None:
    """根 logger 注入 trace filter；LOG_JSON_OUTPUT=true 追加 JSON handler。幂等。"""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    if not any(isinstance(f, TraceIdFilter) for f in root.filters):
        root.addFilter(TraceIdFilter())

    from app.config import get_settings

    if get_settings().LOG_JSON_OUTPUT:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonLogFormatter())
        handler.setLevel(logging.INFO)
        # 避免重复加多个 JSON handler
        if not any(type(h).__name__ == "StreamHandler" and isinstance(h.formatter, JsonLogFormatter)
                   for h in root.handlers):
            root.addHandler(handler)
        if root.level > logging.INFO:
            root.setLevel(logging.INFO)
    _configured = True
