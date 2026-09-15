"""P6-6-1 治理策略引擎：把治理参数（配额/分层/保留）从全局配置抽为可按作用域差异化的 JSON 策略。

核心语义（审查🔴闭环）：
- **全有或全无**：命中条目中任一字段非法/不在白名单 → 整条忽略，该作用域回退默认（不半吊子生效）。
- **优先级**：task > ns > role > global（同类取最长前缀）；多命中取最高优先级，冲突写审计告警。
- **不追溯存量**：策略仅对 resolve 时刻的判定生效（调用方负责「变更不追溯」语义，见各模块）。
- **性能缓存**：解析结果 TTL 缓存，高频路径（配额校验）毫秒返回。

用法：``resolve_policy(kind, scope_id, defaults) -> dict``（未启用/未命中/非法 → defaults 原样合成）。
"""

from __future__ import annotations

import asyncio
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

# ---- B 剩余问题闭环：审计 + 命中统计（🔴1 冲突写审计 / 🔴2 失败写审计含错误与内容） ----
_counters = {"resolve": 0, "cache_hit": 0, "parse_fail": 0,
             "entry_invalid": 0, "conflict": 0}
_last_audit: dict[tuple[str, str], float] = {}


def policy_metrics() -> dict:
    """策略引擎可观测：命中/缓存命中/失败/冲突统计（并入 /metrics）。"""
    return {"enabled": get_settings().POLICY_ENGINE_ENABLED,
            "counters": dict(_counters),
            "cache_ttl": _RESOLVE_TTL}


def metrics_reset() -> None:
    """测试复位：清空计数与审计节流表。"""
    _counters.update(resolve=0, cache_hit=0, parse_fail=0,
                     entry_invalid=0, conflict=0)
    _last_audit.clear()


def _should_audit(key: tuple[str, str]) -> bool:
    """审计节流：同 (action, kind) 至少间隔 POLICY_AUDIT_INTERVAL 秒一次，防热路径刷审计。"""
    now = time.time()
    with _cache._lock:
        last = _last_audit.get(key, 0.0)
        if now - last < get_settings().POLICY_AUDIT_INTERVAL:
            return False
        _last_audit[key] = now
        return True


def _audit(action: str, *, kind: str, detail: dict | None = None,
           ok: bool = False, error: str = "") -> None:
    """把策略事件写入治理审计（action 前缀 policy.*）。热路径节流 + 尽力而为。

    resolve_policy 是同步函数，但调用方均在 async 上下文（配额/分层）；用当前事件循环
    以 fire-and-forget 任务落库，无运行循环（纯同步调用）则跳过（不阻塞解析）。
    """
    if not _should_audit((action, kind)):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        from app.storage.governance import _audit_gov

        loop.create_task(_audit_gov(
            action=f"policy.{action}", detail={**(detail or {}), "kind": kind},
            ok=ok, error=error))
    except Exception:  # noqa: BLE001  审计失败不影响策略判定
        pass


def _load_policy() -> dict[str, Any]:
    """解析 POLICY_JSON；非法/未启用 → 空策略。解析失败写审计（含错误与内容片段）。"""
    s = get_settings()
    if not s.POLICY_ENGINE_ENABLED:
        return {}
    raw = (s.POLICY_JSON or "").strip()
    if not raw:
        return {}
    try:
        pol = json.loads(raw)
    except (ValueError, TypeError) as exc:
        _counters["parse_fail"] += 1
        _audit("parse_fail", kind="*", detail={"reason": str(exc),
               "snippet": raw[:120]}, error=f"POLICY_JSON 解析失败：{exc}")
        return {}
    if not isinstance(pol, dict):
        _counters["parse_fail"] += 1
        _audit("parse_fail", kind="*", detail={"reason": "not_object"},
               error="POLICY_JSON 非 JSON 对象")
        return {}
    return pol


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
    """返回 (score, fields)；空 scope 命中 global。

    全有或全无（🔴2）：命中条目校验非法 → 整条忽略并写审计（含字段/值 reasons，节流）。
    冲突（🔴1）：同优先级多命中 → 写审计告警。
    """
    entries = pol.get(kind)
    if not isinstance(entries, dict):
        return None
    best: tuple[int, dict] | None = None
    conflict = False
    for scope_key, fields in entries.items():
        fields = _validate_entry(kind, fields)
        if fields is None:
            _counters["entry_invalid"] += 1
            _audit("entry_invalid", kind=kind,
                   detail={"scope": str(scope_key)[:120], "why": "非法字段/值"})
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
        _counters["conflict"] += 1
        _audit("conflict", kind=kind,
               detail={"scope": scope_id, "why": "同优先级条目冲突"})
        logger.warning("治理策略冲突：kind=%s scope=%s 存在同优先级条目", kind, scope_id)
    return best


def resolve_policy(kind: str, scope_id: str, defaults: dict[str, Any]) -> dict[str, Any]:
    """解析 kind 策略并覆盖 defaults；未启用/未命中/非法 → defaults 合成。"""
    _counters["resolve"] += 1
    cached = _cache.get(kind, scope_id, _RESOLVE_TTL)
    if cached is not None:
        _counters["cache_hit"] += 1
        return cached
    pol = _load_policy()
    best = _best_entry_for(kind, scope_id, pol)
    result = dict(defaults)
    if best is not None:
        result.update(best[1])
    _cache.put(kind, scope_id, result)
    return result
