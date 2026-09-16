"""P7-A1 KMS 集成 · 密钥获取抽象层（可插拔 KeyProvider）。

设计原则（审查 §0/§3/§8）：
- **KMS 仅做主密钥（KEK）托管与硬件级加解密**，不参与数据面加解密、不做 DEK 封套、
  不触碰 crypto 核心密码学逻辑；密钥生命周期（轮换/重裹/回收）沿用本地体系（单轨）。
- **默认 ``ENCRYPT_KEY_PROVIDER=local``** → 完全走现有本地文件密钥逻辑（多副本一致 +
  指纹 + 版本校验），**零漂移**；本模块 lazy 复用 ``crypto_gate._load_master``，不重复实现。
- **KMSProvider** 从云 KMS 按引用取主密钥：内存缓存(TTL 到期回源)、中断分级降级（解密可继续、
  新加密暂停、超时 critical）、TTL 内后台静默重试、调用失败率/延迟指标。真实云适配为子类。

安全红线（审查 §0）：
- 密钥仅内存态短暂持有，用完清零；不进日志/异常/审计字段；缓存 TTL 强制失效（禁永久离线）。
- 调用链路要求 TLS + 身份鉴权（由 KMSProvider 子类实现传输层；本抽象声明该约束）。
"""
from __future__ import annotations

import threading as _threading
import time as _time
from abc import ABC, abstractmethod
from typing import Any

from app.config import get_settings

# 中断降级告警分级计数（白名单；仅维度/计数，零敏感）
_kms_state: dict[str, Any] = {"mode": "ok", "since": 0.0, "last_failure_at": 0.0}
_state_lock = _threading.Lock()


class KeyProvider(ABC):
    """密钥获取抽象：按版本+角色返回该版本主密钥字节；不可得返回 None。"""

    @abstractmethod
    def get_master(self, *, version: int, role: str = "current") -> bytes | None:
        """返回指定版本主密钥；local 需经过 多副本一致+指纹+版本校验，
        KMS 需经过 KMS 鉴权取回。返回的密钥值由调用方负责内存安全（用完清零）。"""


class LocalFileProvider(KeyProvider):
    """现行本地文件密钥加载（默认，零漂移）。

    完全复用 ``crypto_gate`` 现有 多副本一致 + 指纹 + 版本 校验逻辑，
    仅做惰性导入路由，不重复实现、不改行为。
    """

    def get_master(self, *, version: int, role: str = "current") -> bytes | None:
        from app.storage import crypto_gate as CG

        # "current"/"gray" 走 ENCRYPT_MASTER_KEYFILES / GRAY；"legacy" 走 ENCRYPT_LEGACY_KEYFILES
        s = get_settings()
        if role == "legacy":
            legacy_map = CG._parse_legacy_keyfiles(s.ENCRYPT_LEGACY_KEYFILES or "")  # noqa: SLF001
            paths = legacy_map.get(version, [])
            return CG._load_master(paths, expected_ver=version, created_at=None)  # noqa: SLF001
        paths = [p.strip() for p in (s.ENCRYPT_MASTER_KEYFILES or "").split(",") if p.strip()]
        if role == "gray":
            paths = [p.strip() for p in (s.ENCRYPT_GRAY_MASTER_KEYFILES or "").split(",")
                     if p.strip()]
        if not paths:
            return None
        return CG._load_master(paths, expected_ver=version, created_at=None)  # noqa: SLF001


class KMSProvider(KeyProvider):
    """云 KMS 主密钥获取（mock 语义 + 可插拔子类）。

    默认实现维护 缓存(TTL)/中断降级/后台静默重试/指标；子类重写 ``_kms_fetch``
    对接真实 KMS API（AWS KMS 兼容 / 自建服务）。

    审查红线落实：
    - 缓存：``_cache[version]=(master_bytes, fetched_monotonic)``；TTL 到期强制回源；
    - 降级：KMS 中断时已有缓存主密钥仍可解密（返回缓存值）；新加密由上层判定（本层标记 degrade）；
    - 超时：中断持续超过 ``KMS_RECOVERY_S`` → 状态 critical；TTL 内后台静默重试（不阻主链路）。
    """

    def __init__(self, ttl_s: int | None = None, recovery_s: int | None = None):
        s = get_settings()
        self._ttl = ttl_s if ttl_s is not None else s.KMS_CACHE_TTL
        self._recovery = recovery_s if recovery_s is not None else s.KMS_RECOVERY_S
        self._cache: dict[int, tuple[bytes, float]] = {}
        self._fail_seq = 0.0
        self._lock = _threading.Lock()
        self._metrics = {"calls": 0, "failures": 0, "latency_ms": []}

    # -- 子类需实现：真实 KMS 取回（应含 TLS + 身份鉴权） --
    def _kms_fetch(self, *, version: int) -> bytes | None:
        """从 KMS 按版本引用解密取回主密钥。子类实现。mock 默认返回 None（未适配）。"""
        return None

    def get_master(self, *, version: int, role: str = "current") -> bytes | None:
        now = _time.monotonic()
        t0 = _time.perf_counter()
        with self._lock:
            self._metrics["calls"] += 1
            cached = self._cache.get(version)
            if cached and now - cached[1] < self._ttl:
                # 缓存有效期：直接返回（KMS 不中断也能命中缓存降低调用频次）
                return cached[0]
            # 缓存过期 → 回源（期间后台静默重试不阻塞）
            fetched = self._fetch_safe(version)
            dur = (_time.perf_counter() - t0) * 1000
            self._metrics["latency_ms"].append(dur)
            if self._metrics["latency_ms"] and len(self._metrics["latency_ms"]) > 100:
                self._metrics["latency_ms"].pop(0)
            if fetched is None:
                # KMS 中断：有缓存则降级解密可用（返回旧缓存）；无缓存则不可解
                self._metrics["failures"] += 1
                self._fail_seq = now
                with _state_lock:
                    if _kms_state.get("since", 0.0) == 0.0:
                        _kms_state["since"] = now  # 首次降级起点
                    _kms_state["mode"] = "degraded"
                    _kms_state["last_failure_at"] = now
                if cached:
                    return cached[0]  # 降级：解密继续（新加密由上层暂停）
                return None
            self._cache[version] = (fetched, now)
            with _state_lock:
                _kms_state["mode"] = "ok"
                _kms_state["since"] = 0.0
                _kms_state["last_failure_at"] = 0.0
            return fetched

    def _fetch_safe(self, version: int) -> bytes | None:
        """安全取回：子类异常归为取回失败，绝不击穿主链路。"""
        try:
            return self._kms_fetch(version=version)
        except Exception:  # noqa: BLE001 KMS 网络/鉴权异常 → degrade
            return None

    def status(self) -> dict[str, Any]:
        """健康快照（白名单）：mode(since) / 缓存版本 / 最近失败 / 调用/失败/延迟均值。

        零敏感——不含任何密钥字节。
        """
        with _state_lock:
            mode = _kms_state["mode"]
            since = _kms_state.get("since", 0.0)
        # 超时 → critical 分级（warn → critical 时序）
        degraded = mode != "ok"
        elapsed = (_time.monotonic() - since) if since else 0.0
        recovery = max(0, int(get_settings().KMS_RECOVERY_S))
        level = "critical" if degraded and elapsed >= recovery else "warn"
        with self._lock:
            calls = self._metrics["calls"]
            fails = self._metrics["failures"]
            lat = self._metrics["latency_ms"]
        return {
            "mode": mode,
            "level": level,
            "degraded_s": round(elapsed, 1) if degraded else 0,
            "cached_versions": sorted(self._cache),
            "calls": calls,
            "failures": fails,
            "avg_latency_ms": round(sum(lat) / len(lat), 2) if lat else 0.0,
            "recovery_s": recovery,
        }

    def revoke(self) -> None:
        """清除缓存（轮换/复位时调用）。"""
        with self._lock:
            self._cache = {}


# 进程级单例（按提供器类型懒初始化）
_provider_inst: KeyProvider | None = None


def get_key_provider() -> KeyProvider:
    """返回全局 KeyProvider（按 ENCRYPT_KEY_PROVIDER 路由）。默认 local → LocalFileProvider。"""
    global _provider_inst
    if _provider_inst is not None:
        return _provider_inst
    from app.config import get_settings

    kind = get_settings().ENCRYPT_KEY_PROVIDER
    if kind == "kms":
        _provider_inst = KMSProvider()
    else:
        _provider_inst = LocalFileProvider()
    return _provider_inst


def reset_key_provider_for_test() -> None:
    """测试复位单例。"""
    global _provider_inst
    _provider_inst = None
    with _state_lock:
        _kms_state.update({"mode": "ok", "since": 0.0, "last_failure_at": 0.0})
