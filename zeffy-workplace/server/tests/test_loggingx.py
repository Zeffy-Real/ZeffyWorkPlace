"""P4 trace→日志：日志记录注入 trace_id；JSON 结构化输出可解析。"""

from __future__ import annotations

import io
import json
import logging

from app.config import get_settings
from app.loggingx import JsonLogFormatter, TraceIdFilter, setup_logging
from app.tracing import set_trace_id


def test_trace_filter_injects_trace_id():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(trace_id)s::%(message)s"))
    logger = logging.getLogger("app.tests.trace")
    lvl = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.addFilter(TraceIdFilter())
    try:
        # 无 trace → 惰性生成，仍注入
        set_trace_id("trace-abc")
        stream.truncate(0)
        stream.seek(0)
        logger.info("hello")
        line = stream.getvalue()
        assert line.startswith("trace-abc::hello"), line
        # 换 trace → 新日志带新 trace（contextvar 隔离）
        set_trace_id("trace-xyz")
        stream.truncate(0)
        stream.seek(0)
        logger.info("second")
        assert stream.getvalue().startswith("trace-xyz::second")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(lvl)
        for f in list(logger.filters):
            if isinstance(f, TraceIdFilter):
                logger.removeFilter(f)


def test_json_formatter_parseable_with_trace():
    record = logging.LogRecord("app.x", logging.INFO, __name__, 0,
                               "json line", None, None)
    record.trace_id = "trace-json"
    out = JsonLogFormatter().format(record)
    parsed = json.loads(out)
    assert parsed["trace_id"] == "trace-json"
    assert parsed["message"] == "json line"
    assert parsed["level"] == "INFO"


def test_setup_logging_idempotent():
    get_settings().LOG_JSON_OUTPUT = True
    root = logging.getLogger()
    setup_logging()
    setup_logging()
    trace_filters = [f for f in root.filters if isinstance(f, TraceIdFilter)]
    assert len(trace_filters) == 1, "重复调用不应累积 filter"
    json_handlers = [h for h in root.handlers
                     if isinstance(getattr(h, "formatter", None), JsonLogFormatter)]
    assert len(json_handlers) <= 1
    # 根 filter 路径：活跃 trace 下，经 root 传给捕获 handler 的记录应带 trace_id
    stream = io.StringIO()
    cap = logging.StreamHandler(stream)
    cap.setFormatter(logging.Formatter("%(trace_id)s"))
    root.addHandler(cap)
    try:
        set_trace_id("trace-root-1")
        root.info("root-line")
        assert "trace-root-1" in stream.getvalue()
    finally:
        root.removeHandler(cap)
        get_settings().LOG_JSON_OUTPUT = False
