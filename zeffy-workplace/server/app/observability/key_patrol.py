"""P7-C3 密钥健康度巡检 · 周期性主动核验密钥文件系统状态（纯只读增量）。

与现有加密告警（``metrics._encrypt_alarm_scan``，基于运行时计数快照）互补：
本模块**主动核验文件系统层面**的密钥健康 —— 副本完整性/一致性、权限、版本回退、
灰度游离、到期基准 —— 这些无法从 ``crypto_metrics()`` 运行时快照得到。

安全红线（审查 §6）：
- **零敏感输出**：告警 detail 仅 级别/维度/版本号/时间戳/计数，绝不输出密钥、指纹、密文、文件路径；
- **不还原密钥字节**：副本一致性用**文件级 SHA256 比对**（不解析出 master），权限用 stat；
  元数据（版本/创建时间/指纹存在性）仅读 ``#zfk`` 头首行，不落入主密钥内存态；
- **不触发解锁**：不调用 ``crypto_gate._unlock``，不加载密钥束进内存；
- **分级差异化冷却**：critical 30min / high 1h / warn 3h，严重更及时、低级别更静默；
- **首次静默**：``KEY_PATROL_FIRST_SILENT`` 下首次巡检存量异常仅记录不告警（防上线即风暴）；
- **不风暴**：同维度冷却期内不重复告警，多故障收敛分级单条。

兼容锚点：``KEY_PATROL_ENABLED=false`` 或总闸 ``ARTIFACT_META_ENABLED`` 关闭时，
``run_key_patrol()`` 直接返回空，零额外 IO / 计算 / 行为变化。
"""
from __future__ import annotations

import hashlib
import os
import time as _time
from typing import Any

from app.config import get_settings
from app.storage import crypto_gate as CG

# 分级态与冷却（维度 → (级别, 最后告警时间)；独立于 metrics 的 _gov_alarm_state，避免耦合）
_patrol_state: dict[str, tuple[str, float]] = {}
_patrol_ran = False


def reset_patrol_for_test() -> None:
    """测试复位：清态与冷却，允许重放首次静默。"""
    global _patrol_state, _patrol_ran
    _patrol_state = {}
    _patrol_ran = False


def _cooldown_for(level: str) -> float:
    s = get_settings()
    return {
        "critical": s.KEY_PATROL_COOLDOWN_CRITICAL_S,
        "high": s.KEY_PATROL_COOLDOWN_HIGH_S,
        "warn": s.KEY_PATROL_COOLDOWN_WARN_S,
    }.get(level, s.KEY_PATROL_COOLDOWN_WARN_S)


def _should_alarm(dim: str, level: str) -> bool:
    """冷却判定：同维度同级别在冷却期内不重复告警。"""
    now = _time.monotonic()
    cur = _patrol_state.get(dim)
    if cur and cur[0] == level and now - cur[1] < _cooldown_for(level):
        return False
    return True


def _mark(dim: str, level: str) -> None:
    _patrol_state[dim] = (level, _time.monotonic())


def _file_sha256(path: str) -> str | None:
    """文件级 SHA256（流式，内存常量级）。仅用于副本一致性比对，非密钥材料。"""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _mode_group80(path: str) -> int | None:
    """返回文件权限的 others+group 位（&0o077）；不存在 → None。"""
    try:
        return int(os.stat(path).st_mode & 0o077)
    except OSError:
        return None


def _meta_ver(path: str) -> int | None:
    """读 ``#zfk`` 头首行的内嵌版本号（仅首行元数据，不读 body）。"""
    try:
        with open(path, "rb") as f:
            head = f.readline(1024)
        return CG._parse_keyfile_meta(head).get("ver")  # noqa: SLF001 同仓复用解析
    except OSError:
        return None


def key_patrol_health() -> dict[str, Any]:
    """巡检结果快照（admin 白名单；零敏感）。供 /metrics governance + 审计 detail 引用。

    返回仅计数/级别/维度；任何异常都收敛为分级状态 + 计数，不暴露密钥/路径。
    """
    issues = run_key_patrol(write_audit=False)
    by_level = {"critical": 0, "high": 0, "warn": 0}
    for ev in issues:
        by_level[ev["level"]] = by_level.get(ev["level"], 0) + 1
    return {
        "enabled": get_settings().KEY_PATROL_ENABLED,
        "check_count": len(issues),
        "by_level": by_level,
        "issues": [{"level": e["level"], "dim": e["dim"], "value": e["value"]} for e in issues],
    }


def run_key_patrol(*, write_audit: bool = True) -> list[dict[str, str]]:
    """执行一轮密钥健康巡检，返回告警事件（与 metrics 告警同构，可复用审计通道）。

    幂等：重复调用不产生副作用（冷却内同维度不重复进事件列表）；不修改任何存/密文。
    """
    global _patrol_ran
    s = get_settings()
    events: list[dict[str, str]] = []
    # 兼容锚点：总闸/巡检开关任一关闭 → 短路，零开销零告警
    if not s.KEY_PATROL_ENABLED or not s.ARTIFACT_META_ENABLED or not s.ARTIFACT_ENCRYPT_ENABLED:
        _patrol_ran = True
        return events

    cur_paths = [p.strip() for p in (s.ENCRYPT_MASTER_KEYFILES or "").split(",") if p.strip()]

    def emit(dim: str, level: str, value: str) -> None:
        nonlocal events
        # 首次静默：仅标记已巡检、不写入告警态（不埋冷却），存量异常先记录不告警
        if s.KEY_PATROL_FIRST_SILENT and not _patrol_ran:
            return
        if _should_alarm(dim, level):
            _mark(dim, level)
            events.append({"type": "encrypt-patrol", "level": level,
                           "status": "triggered", "dim": dim, "value": value})

    # —— 巡检项 1：主密钥副本数量与一致性（文件级 SHA256 比对，不还原密钥）——
    if cur_paths:
        hashes = [_file_sha256(p) for p in cur_paths]
        nonempty = [p for p in cur_paths if os.path.exists(p)]
        if len(cur_paths) < 2:
            emit("key-copies", "high", f"主密钥副本数不足（{len(cur_paths)}，应≥2）")
        elif len(nonempty) < len(cur_paths):
            emit("key-copies", "high", f"主密钥存在缺失副本（{len(nonempty)}/{len(cur_paths)} 在位）")
        elif len(set(h for h in hashes if h)) > 1:
            emit("key-consistency", "high", "主密钥副本内容不一致（建议核对备份）")

    # —— 巡检项 2：密钥文件权限过宽（&0o077 非零）——
    for p in cur_paths:
        g = _mode_group80(p)
        if g is not None and g != 0:
            emit("key-permission", "warn", "密钥文件权限存在 group/other 读写风险（建议 0600）")
            break  # 同权限维度收敛为一条，避免逐文件风暴

    # —— 巡检项 3：版本回退（历史版本 ≥ 当前版本）——
    legacy_map = CG._parse_legacy_keyfiles(s.ENCRYPT_LEGACY_KEYFILES or "")  # noqa: SLF001
    cur_ver = s.ENCRYPT_CIPHER_VERSION
    if any(ver >= cur_ver for ver in legacy_map):
        emit("key-version", "critical", "检测到历史版本 ≥ 当前版本（回退降级风险）")
    elif cur_paths:
        # 文件内嵌版本与配置版本不符
        file_ver = _meta_ver(cur_paths[0])
        if file_ver is not None and file_ver != cur_ver:
            emit("key-version", "critical", f"密钥文件内嵌版本({file_ver})与配置版本({cur_ver})不符")

    # —— 巡检项 4：灰度游离（配置了灰度副本但比率=0，或比率>0但无灰副本）——
    gray_paths = [p.strip() for p in (s.ENCRYPT_GRAY_MASTER_KEYFILES or "").split(",") if p.strip()]
    ratio = s.ENCRYPT_ROTATE_GRAY_RATIO
    if gray_paths and ratio == 0:
        emit("key-gray", "warn", "配置了灰度密钥副本但抽样比率=0（灰度游离未接入）")
    elif not gray_paths and ratio > 0:
        emit("key-gray", "warn", "抽样比率>0 但未配置灰度密钥副本（灰度配置不完整）")

    # —— 巡检项 5：到期基准异常（无内嵌 created 且依赖 mtime）——
    if cur_paths:
        meta = None
        try:
            with open(cur_paths[0], "rb") as f:
                head = f.readline(1024)
            meta = CG._parse_keyfile_meta(head)  # noqa: SLF001
        except OSError:
            pass
        if meta is not None and not meta.get("created") and not s.ENCRYPT_KEY_CREATED_AT:
            emit("key-expiry-basis", "warn", "密钥到期基准依赖 mtime（部署/迁移可能被改，建议用内嵌 created 或配置指定）")

    _patrol_ran = True
    return events
