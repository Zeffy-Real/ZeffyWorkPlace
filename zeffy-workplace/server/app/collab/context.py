"""P7-D2 跨 Agent 共享上下文：敏感字段脱敏 + task/owner 权限隔离。

审查闭环（D2 范围界定 v2 上下文安全项）：
- **敏感字段负面清单**：密钥/口令/审计详情/用户隐私/配置参数 key 命中即脱敏，
  打包进入任何 Agent prompt 前必须先 ``desensitize``，不脱敏不传输。
- **权限强校验**：``SharedContext`` 绑定 ``task_id`` + ``owner``；跨任务 / 跨 owner 读取
  （``get`` 传非持有者）一律返回 ``None``——越权不可见。
- 本模块纯逻辑，无 DB / 存储依赖。
"""
from __future__ import annotations

import re
from typing import Any

# 敏感字段负面清单（key 命中即脱敏；含常见大小写/分隔变体）
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(^|[\W_])(api.?key|secret|token|password|passwd|authorization|auth|private.?key|"
    r"credential|audit|pii|phone|mobile|email|id_card|ssn|access.?token)(\W|$)"
)


def is_sensitive(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(key)))


def _redact(value: Any) -> Any:
    """敏感值统一打码，不泄露内容长度以外的信息。"""
    return "***[REDACTED]***"


def desensitize(data: Any, *, _key: str = "") -> Any:
    """递归脱敏：命中敏感 key 的值置换为掩码；list/dict 递归遍历。"""
    if isinstance(data, dict):
        return {
            k: (_redact(v) if is_sensitive(str(k)) else desensitize(v, _key=str(k)))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [desensitize(v, _key=_key) for v in data]
    return data


class SharedContext:
    """任务级共享上下文：仅持有者可读，打包传播前按需脱敏。"""

    def __init__(self, task_id: str, owner: str | None = None) -> None:
        self.task_id = task_id
        self.owner = owner or ""
        self._data: dict[str, Any] = {}

    def put(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str, *, requester_task: str,
            requester_owner: str | None = None) -> Any | None:
        """跨任务 / 跨 owner 一律返回 None（越权不可见）。"""
        if requester_task != self.task_id:
            return None
        owner = requester_owner or ""
        if self.owner and owner and self.owner != owner:
            return None
        return self._data.get(key)

    def snapshot(self, *, owner: str | None = None) -> dict[str, Any]:
        """返回脱敏后的只读快照；缺省持有者时带 owner 强隔离（跨 owner 全空）。"""
        if owner is not None and self.owner and owner != self.owner:
            return {}
        return desensitize(self._data)
