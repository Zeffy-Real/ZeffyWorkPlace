"""P6-6-1 治理策略引擎：把治理参数（配额/分层/保留）从全局配置抽为可按作用域差异化的 JSON 策略。

核心语义（审查🔴闭环）：
- **全有或全无**：命中条目中任一字段非法/不在白名单 → 整条忽略，该作用域回退默认（不半吊子生效）。
- **优先级**：task > ns > role > global（同类取最长前缀）；多命中取最高优先级，冲突写审计告警。
- **不追溯存量**：策略仅对 resolve 时刻的判定生效（调用方负责「变更不追溯」语义，见各模块）。
- **性能缓存**：解析结果 TTL 缓存，高频路径（配额校验）毫秒返回。

用法：``resolve_policy(kind, scope_id, defaults) -> dict``（未启用/未命中/非法 → defaults 原样合成）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# 作用域前缀 → 优先级分（task > ns > role > global）
_SCOPE_SCORE = {
    "task": 4,
    "ns": 3,
    "role": 2,
    "global": 1,
}


class _PolicyCache:
    """TTL 解析缓存：key=(kind, scope_id)；策略变更由 GLOBAL_POLICY_TS 递增失效。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], tuple[float, dict]] = {}
        self._cfg_ts: str = ""

    def _cfg_key(self) -> str:
        return f"{get_settings().POLICY_JSON}|{get_settings().POLICY_ENGINE_ENABLED}"

    def get(self, kind: str, scope_id: str, ttl: float) -> dict | None:
        key = (kind, scope_id)
        ts = self._cfg_key()
        with self._lock:
            if ts != self._cfg_ts:
                self._data.clear()
                self._cfg_ts = ts
            hit = self._data.get(key)
            if hit and (time.time() - hit[0]) < ttl:
                return hit[1]
            return None

    def put(self, kind: str, scope_id: str, result: dict) -> None:
        with self._lock:
            self._data[(kind, scope_id)] = (time.time(), result)


_cache = _PolicyCache()
_RESOLVE_TTL = 30.0  # 秒


def _load_policy() -> dict[str, Any]:
    """解析 POLICY_JSON；非法/未启用 → 空策略。"""
    s = get_settings()
    if not s.POLICY_ENGINE_ENABLED:
        return {}
    raw = (s.POLICY_JSON or "").strip()
    if not raw:
        return {}
    try:
        pol = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return pol if isinstance(pol, dict) else {}


def _validate_entry(kind: str, fields: Any) -> dict | None:
    """全有或全无：字段须全在被允许集合且值类型合法，否则返回 None（整条忽略）。"""
    s = get_settings()
    allowed = (s.POLICY_ALLOWED_FIELDS or {}).get(kind) or set()
    if not isinstance(fields, dict):
        return None
    out: dict[str, Any] = {}
    for field, val in fields.items():
        if field not in allowed:
            return None  # 未允许字段 → 整条失效
        if field in ("gray_list",):
            if not isinstance(val, str):
                return None
        elif field in ("exempt_system",):
            if not isinstance(val, bool):
                return None
        else:
            if not isinstance(val, (int, float)) or val < 0:
                return None
            val = int(val)
        out[field] = val
    return out


def _best_entry_for(kind: str, scope_id: str, pol: dict[str, Any]) -> tuple[int, dict] | None:
    """返回 (score, fields)；空 scope 命中 global。"""
    entries = pol.get(kind)
    if not isinstance(entries, dict):
        return None
    best: tuple[int, dict] | None = None
    conflict = False
    for scope_key, fields in entries.items():
        fields = _validate_entry(kind, fields)
        if fields is None:
            continue  # 全有或全无：非法条目忽略
        score, prefix = 0, ""
        if scope_key == "global":
            score, prefix = 1, ""
        else:
            for pref in ("task:", "ns:", "role:"):
                if scope_key.startswith(pref):
                    prefix = scope_key[len(pref):]
                    score = _SCOPE_SCORE.get(pref.strip(":"), 0) * 100
                    break
            else:
                continue  # 未知前缀，忽略
            if not prefix or not scope_id.startswith(prefix):
                continue  # 作用域不匹配
            score += len(prefix)  # 同类最长前缀优先
        if best is None or score > best[0]:
            best = (score, fields)
        elif best[0] == score:
            conflict = True
    if conflict:
        # 同优先级冲突 → 审计告警（不阻塞，取其中一条，行为由上层测试锁定）
        logger.warning("治理策略冲突：kind=%s scope=%s 存在同优先级条目", kind, scope_id)
    return best


def resolve_policy(kind: str, scope_id: str, defaults: dict[str, Any]) -> dict[str, Any]:
    """解析 kind 策略并覆盖 defaults；未启用/未命中/非法 → defaults 合成。"""
    cached = _cache.get(kind, scope_id, _RESOLVE_TTL)
    if cached is not None:
        return cached
    pol = _load_policy()
    best = _best_entry_for(kind, scope_id, pol)
    result = dict(defaults)
    if best is not None:
        result.update(best[1])
    _cache.put(kind, scope_id, result)
    return result
