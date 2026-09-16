"""P7-B4 加密异常自动诊断 · 解密失败原因自动归类（纯只读观察增量）。

与既有 ``decrypt_fail``/``tamper`` 计数与 ``crypto.decrypt.tamper`` 审计**并行**：本模块在解密
失败捕获点接收 ``C.EncryptError``，按 message 关键词归类（密钥不匹配/文件损坏/头篡改/版本
不支持/密钥未加载/unknown），产出**可能性提示 + 置信度**；仅高置信续命中才触发告警，
unknown 与低/分散样本仅记录，绝不给出操作建议。

审查红线（§4/§7）：
- **仅做可能性提示、不做操作建议**：告警文案只陈述 类别/置信度/样本数，绝不含任何处置指令；
- **高置信才告警，低置信仅记录**：基准置信度 H/M/L + 样本一致率修正，仅置信=高 且命中≥阈值 才触发；
- **零敏感输出**：快照/告警 detail 排除 密钥、密文、指纹、文件路径、错误原文（可能含路径上下文）；
- **安全/运维标签**：tamper_hmac、unsupported_version → security；cipher_corrupt → ops；其余 → generic；
- **分类优先级**：同时命中多特征按 security > key > file 归一类，避免重复计数；
- **冷却防风暴**：同类冷却期内不重复告警；首次启动存量历史不告警（避免上线即风暴）。

兼容锚点：``ENCRYPT_DIAGNOSE_ENABLED`` / ``ARTIFACT_META_ENABLED`` / ``ARTIFACT_ENCRYPT_ENABLED``
任一关闭 → ``record()`` 零采样 / ``run_scan()`` 零告警 / ``health_snapshot()`` 空，零 IO/计算/行为变化。
"""
from __future__ import annotations

import threading as _threading
import time as _time
from typing import Any

from app.config import get_settings

# 分类特征表：(类别, 置信度基准, 特征关键词) —— 顺序即优先级：security > key > file
_CLASS_TOKENS: list[tuple[str, str, str]] = [
    ("unsupported_version", "H", "不支持的加密版本"),
    ("tamper_hmac", "H", "头 HMAC 校验失败"),
    ("key_not_loaded", "H", "密钥未解锁"),
    ("key_mismatch", "M", "DEK 封套校验失败"),
    ("key_mismatch", "M", "认证失败"),  # "块 {idx} 认证失败" → 密钥/篡改
    ("cipher_corrupt", "H", "密文头损坏"),
    ("cipher_corrupt", "H", "密文流"),
    ("cipher_corrupt", "H", "残留帧"),
    ("cipher_corrupt", "H", "块长度头越界"),
    ("cipher_corrupt", "H", "密文块损坏"),
]
# 域 → 类别分组（用于置信度修正与告警分级）
_SECURITY_CLASSES = {"unsupported_version", "tamper_hmac", "key_not_loaded"}
_OPS_CLASSES = {"cipher_corrupt"}
# 类别标签表：类别 → (基准置信度, 安全/运维分类)
_CLASS_LABEL: dict[str, tuple[str, str]] = {
    "key_mismatch": ("M", "generic"),
    "cipher_corrupt": ("H", "ops"),
    "tamper_hmac": ("H", "security"),
    "unsupported_version": ("H", "security"),
    "key_not_loaded": ("H", "security"),
    "unknown": ("L", "generic"),
}
_CONF_ORDER = {"L": 0, "M": 1, "H": 2}
_CONF_NAMES = ("L", "M", "H")


def _classify(msg: str) -> tuple[str, str, str]:
    """按优先级归类：返回 (类别, 置信度基准, 域)。unknown → (unknown, L, security)。

    优先级由 _CLASS_TOKENS 顺序保证（security 特征在前，避免重复计数）。
    """
    for cls, conf, token in _CLASS_TOKENS:
        if token in msg:
            if cls in _SECURITY_CLASSES:
                return cls, conf, "security"
            if cls in _OPS_CLASSES:
                return cls, conf, "file"
            return cls, conf, "key"
    return "unknown", "L", "unknown"


def _domain_of(cls: str) -> str:
    # unknown 独立域（绝不参与告警：仅记录）
    if cls == "unknown":
        return "unknown"
    if cls in _SECURITY_CLASSES:
        return "security"
    if cls in _OPS_CLASSES:
        return "file"
    if cls == "key_mismatch":
        return "key"
    return "security"


def _conf_level(pts_total: int, same: int, consec: int, base: str) -> str:
    """置信度修正（审查⭐2）：连续 LIFT 次上调一级；占比 < SPARSE 下调一级。"""
    s = get_settings()
    lvl = _CONF_ORDER[base]
    if consec >= s.ENCRYPT_DIAGNOSE_CONFIRM_LIFT:
        lvl = min(2, lvl + 1)
    if pts_total > 0 and (same / pts_total) < s.ENCRYPT_DIAGNOSE_SPARSE_RATIO:
        lvl = max(0, lvl - 1)
    return _CONF_NAMES[lvl]


# 滑动窗口：域 → [monotonic]；连续计数与最后一次告警（冷却）
_wins: dict[str, list[float]] = {"security": [], "key": [], "file": [], "unknown": []}
_runs: dict[str, int] = {}  # 域 → 累计运行次数（连续计数近似）
_last_emit: dict[str, float] = {}
_n_alarms = 0
_lock = _threading.Lock()
_ran = False  # 是否已启动首轮（首次抑制存量历史告警）


def record(msg: str) -> None:
    """由解密失败捕获点调用：记录一次失败归类（重入安全）。

    兼容锚点：诊断关/总闸关/加密关 → 零采样。
    """
    s = get_settings()
    if not s.ENCRYPT_DIAGNOSE_ENABLED or not s.ARTIFACT_META_ENABLED or not s.ARTIFACT_ENCRYPT_ENABLED:
        return
    cls, _conf, dom = _classify(msg)
    now = _time.monotonic()
    with _lock:
        wins = _wins[dom]
        wins.append(now)
        ws = s.ENCRYPT_DIAGNOSE_WINDOW_S
        _wins[dom] = [t for t in wins if now - t <= ws]
        _runs[dom] = _runs.get(dom, 0) + 1


def run_scan() -> list[dict[str, str]]:
    """扫描分类窗口，产出需告警事件（幂等 + 冷却 + 首次抑制）。由 run_monitor_tick 挂载。"""
    global _ran, _n_alarms
    s = get_settings()
    if not s.ENCRYPT_DIAGNOSE_ENABLED or not s.ARTIFACT_META_ENABLED or not s.ARTIFACT_ENCRYPT_ENABLED:
        _ran = True
        return []
    now = _time.monotonic()
    with _lock:
        total = sum(len(v) for v in _wins.values())
        events: list[dict[str, str]] = []
        # 首次启动：存量历史只记录不告警（避免上线即大量历史告警）
        if not _ran:
            _ran = True
            return events
        for dom, wins in _wins.items():
            if dom == "unknown":  # unknown 仅记录，绝不告警
                continue
            same = len(wins)
            if same < s.ENCRYPT_DIAGNOSE_MIN_HITS:
                continue
            consec = _runs.get(dom, 0)
            conf = _conf_level(total, same, consec, "H")
            if conf != "H":  # 仅高置信触发
                continue
            # 冷却：同域冷却期内不重复
            key = f"encrypt-diagnose:{dom}"
            last = _last_emit.get(key)
            if last and now - last < s.ENCRYPT_DIAGNOSE_COOLDOWN_S:
                continue
            _last_emit[key] = now
            _n_alarms += 1
            # 告警分级：命中 ≥ MIN_HITS*HIGH_SCALE → high，否则 warn
            level = ("high" if same >= s.ENCRYPT_DIAGNOSE_MIN_HITS * s.ENCRYPT_DIAGNOSE_HIGH_SCALE
                     else "warn")
            events.append({
                "type": "encrypt-diagnose",
                "level": level,
                "status": "triggered",
                "dim": key,
                "value": f"{dom}诊断置信度=高（窗口命中 {same}/{total}）",
            })
    return events


def health_snapshot() -> dict[str, Any]:
    """诊断结果白名单快照（admin 引用；无敏感/无建议）。"""
    s = get_settings()
    if not s.ENCRYPT_DIAGNOSE_ENABLED:
        return {"enabled": False, "classes": [], "total_samples": 0, "alarms": 0}
    with _lock:
        total = sum(len(v) for v in _wins.values())
        classes = []
        for cls, (base, label) in _CLASS_LABEL.items():
            dom = _domain_of(cls)
            hit = len(_wins.get(dom, []))
            consec = _runs.get(dom, 0)
            conf = _conf_level(total, hit, consec, base)
            classes.append({"class": cls, "base_confidence": base,
                            "confidence": conf, "hits": hit, "category": label})
        alarms = _n_alarms
    return {"enabled": True, "classes": classes,
            "total_samples": total, "alarms": alarms}


def reset_diagnose_for_test() -> None:
    """测试复位：清窗口/连续/事件/冷却/首轮标记。"""
    global _wins, _runs, _last_emit, _n_alarms, _ran
    _wins = {"security": [], "key": [], "file": [], "unknown": []}
    _runs = {}
    _last_emit = {}
    _n_alarms = 0
    _ran = False
