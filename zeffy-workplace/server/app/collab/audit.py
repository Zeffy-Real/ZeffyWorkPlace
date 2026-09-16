"""P7-D2 协作审计：内存环形缓冲 + 独立 JSON 落盘（可观测/可追）。

审查闭环（D2 范围界定 v2 协作审计可视化 + 退出机制可量化）：
- 记录并行子步进度/结果、评审轮次与收敛原因、共享上下文访问等事件。
- **资源隔离**：存储为独立 ``AGENT_COLLAB_STORE_DIR`` 下的 audit.json，**不触碰主线
  storage/governance/crypto/DB**（审计仅供探索线可观测，主线审计仍走系统 AuditLog）。
- 循环覆盖近 N 条（内存）防内存膨胀；落盘原子写入，失败仅告警不阻断。
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _ts() -> str:
    return datetime.now(UTC).isoformat()


class CollabAudit:
    """协作审计存储（进程内环形 + JSON 落盘）。"""

    def __init__(self, store_dir: str | Path = "", *, capacity: int = 500) -> None:
        s = get_settings()
        self._dir = Path(store_dir or s.AGENT_COLLAB_STORE_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "audit.json"
        self._ring: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        try:
            with self._file.open("r", encoding="utf-8") as fh:
                entries = json.load(fh)
            self._ring = deque(entries[-self._ring.maxlen:]) if entries else deque()
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("协作审计加载失败(%s)以空态启动：%s", self._file, exc)

    def _persist(self) -> None:
        tmp = self._file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(list(self._ring), ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._file)
        except OSError as exc:  # noqa: BLE001 落盘失败仅告警
            logger.warning("协作审计落盘失败：%s", exc)

    # ---- 记录 ----
    def record(self, event: str, task_id: str | None = None, **payload: Any) -> dict[str, Any]:
        entry = {"ts": _ts(), "clock": time.monotonic(), "event": event, **payload}
        if task_id:
            entry["task_id"] = task_id
        self._ring.append(entry)
        self._persist()
        return entry

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        return list(self._ring)[-n:]

    def for_task(self, task_id: str, n: int = 200) -> list[dict[str, Any]]:
        out = [e for e in self._ring if e.get("task_id") == task_id]
        return out[-n:]


_audit: CollabAudit | None = None


def get_collab_audit() -> CollabAudit:
    """进程内单例（惰性构造，存储于 AGENT_COLLAB_STORE_DIR）。测试可直接实例化。"""
    global _audit
    if _audit is None:
        _audit = CollabAudit()
    return _audit
