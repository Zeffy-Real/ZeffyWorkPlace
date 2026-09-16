"""P7-B4 加密异常自动诊断 · 单元测试。

覆盖：
1. 开关短路：诊断关/总闸关/加密关 → record 零采样、run_scan 零告警
2. 分类：5 类 + unknown，带优先级（security > key > file）不重复计数
3. 置信度修正：连续 LIFT 次上调；占比 < SPARSE 下调
4. 告警：仅高置信 + 命中≥阈值触发；命中≥MIN_HITS*HIGH_SCALE → high，否则 warn
5. 冷却：同域冷却期内不重复告警（幂等）
6. 首次启动抑制：存量历史不告警
7. 白名单：快照/告警不含处置建议、不含敏感（错误原文/路径）
8. 级联接入：crypto_gate 解密失败 → record 被调用（诊断窗口更新）
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.observability import encrypt_diagnose as D


@pytest.fixture(autouse=True)
def _reset():
    D.reset_diagnose_for_test()
    yield
    D.reset_diagnose_for_test()
    s = get_settings()
    s.ENCRYPT_DIAGNOSE_ENABLED = False
    s.ARTIFACT_ENCRYPT_ENABLED = False
    s.ARTIFACT_META_ENABLED = False


def _enable():
    s = get_settings()
    s.ENCRYPT_DIAGNOSE_ENABLED = True
    s.ARTIFACT_ENCRYPT_ENABLED = True
    s.ARTIFACT_META_ENABLED = True


def _prime(extra=None):
    """首次 run_scan 走抑制，返回空；后续可告警。"""
    _enable()
    if extra:
        extra()
    return D.run_scan()


@pytest.mark.asyncio
async def test_disabled_short_circuit():
    s = get_settings()
    # 全关：零采样零告警
    s.ENCRYPT_DIAGNOSE_ENABLED = False
    s.ARTIFACT_META_ENABLED = True
    s.ARTIFACT_ENCRYPT_ENABLED = True
    D.record("DEK 封套校验失败")
    assert D.run_scan() == []
    assert D.health_snapshot()["enabled"] is False
    # 诊断开，但加密关 → record 短路（零采样）
    s.ENCRYPT_DIAGNOSE_ENABLED = True
    s.ARTIFACT_ENCRYPT_ENABLED = False
    D.record("DEK 封套校验失败")
    assert D.health_snapshot()["total_samples"] == 0
    assert D.run_scan() == []


@pytest.mark.asyncio
async def test_classify_key_mismatch():
    cls, conf, dom = D._classify("DEK 封套校验失败（主密钥不匹配）")
    assert cls == "key_mismatch" and conf == "M" and dom == "key"


@pytest.mark.asyncio
async def test_classify_tamper_hmac_security():
    cls, conf, dom = D._classify("密文头 HMAC 校验失败（元数据被篡改）")
    assert cls == "tamper_hmac" and conf == "H" and dom == "security"


@pytest.mark.asyncio
async def test_classify_unsupported_version_security():
    cls, conf, dom = D._classify("不支持的加密版本：9")
    assert cls == "unsupported_version" and conf == "H" and dom == "security"


@pytest.mark.asyncio
async def test_classify_cipher_corrupt_ops():
    cls, conf, dom = D._classify("密文头损坏")
    assert cls == "cipher_corrupt" and conf == "H" and dom == "file"


@pytest.mark.asyncio
async def test_classify_key_not_loaded():
    cls, conf, dom = D._classify("密钥未解锁，无法解密")
    assert cls == "key_not_loaded" and conf == "H" and dom == "security"


@pytest.mark.asyncio
async def test_classify_unknown_low():
    cls, conf, dom = D._classify("some unexpected internal error")
    assert cls == "unknown" and conf == "L" and dom == "unknown"


@pytest.mark.asyncio
async def test_priority_no_double_count():
    # 「块 认证失败」在 _CLASS_TOKENS 中早于 cipher_corrupt → 归 key_mismatch（key 域）
    cls, conf, dom = D._classify("块 3 认证失败（篡改/密钥错误），已丢弃")
    assert cls == "key_mismatch" and dom == "key"


@pytest.mark.asyncio
async def test_first_run_silent_then_alarm():
    _prime()  # 首次：抑制 → 空
    s = get_settings()
    for _ in range(s.ENCRYPT_DIAGNOSE_MIN_HITS + 2):
        D.record("DEK 封套校验失败")
    ev = D.run_scan()
    assert any(e["type"] == "encrypt-diagnose" for e in ev)
    # 冷却期内再次 run_scan → 不重复（幂等）
    ev2 = D.run_scan()
    assert all(e["type"] != "encrypt-diagnose" for e in ev2)


@pytest.mark.asyncio
async def test_warn_level_at_threshold():
    _prime()
    s = get_settings()
    for _ in range(s.ENCRYPT_DIAGNOSE_MIN_HITS):
        D.record("不支持的加密版本：7")  # security 高置信
    ev = next(e for e in D.run_scan() if e["type"] == "encrypt-diagnose")
    assert ev["level"] == "warn"


@pytest.mark.asyncio
async def test_high_level_double_threshold():
    _prime()
    s = get_settings()
    for _ in range(s.ENCRYPT_DIAGNOSE_MIN_HITS * int(s.ENCRYPT_DIAGNOSE_HIGH_SCALE) + 1):
        D.record("不支持的加密版本：7")
    ev = next(e for e in D.run_scan() if e["type"] == "encrypt-diagnose")
    assert ev["level"] == "high"


@pytest.mark.asyncio
async def test_low_confidence_silent_unknown():
    _prime()
    s = get_settings()
    # unknown → 低置信 L，即使超阈值也不告警
    for _ in range(s.ENCRYPT_DIAGNOSE_MIN_HITS + 2):
        D.record("weird internal error")
    ev = D.run_scan()
    assert all(e["type"] != "encrypt-diagnose" for e in ev)


@pytest.mark.asyncio
async def test_sparse_lowers_confidence_silent():
    _prime()
    # 分散：大量 unknown + 少量 cipher_corrupt → 占比 < SPARSE → 下调不提告警
    for i in range(20):
        D.record("weird internal error" if i % 2 else "some other random msg")
    D.record("密文头损坏")
    ev = D.run_scan()
    # cipher_corrupt 占比不足 30% → 下调 → 不告警
    assert all(e["type"] != "encrypt-diagnose" for e in ev)


@pytest.mark.asyncio
async def test_whitelist_no_sensitive_no_advice():
    _prime()
    s = get_settings()
    for _ in range(s.ENCRYPT_DIAGNOSE_MIN_HITS + 1):
        D.record("密文头损坏")
    ev = next(e for e in D.run_scan() if e["type"] == "encrypt-diagnose")
    snap = D.health_snapshot()
    # 告警 value：只含 类别/置信度/样本；无处置指令、无路径/密钥片段
    assert "诊断" in ev["value"]
    assert all(tok not in ev["value"] for tok in ("轮换", "重裹", "删除",
                                                  "C:", ".key", "DEK 封套校验失败（"))
    # 快照 classes 白名单字段
    for c in snap["classes"]:
        assert set(c.keys()) == {"class", "base_confidence", "confidence", "hits", "category"}
    assert snap["total_samples"] >= s.ENCRYPT_DIAGNOSE_MIN_HITS


@pytest.mark.asyncio
async def test_gate_diag_record_feeds_diagnose():
    """级联：crypto_gate 解密失败接入点 _diag_record → 诊断采样。

    不经过 decrypt_artifact 的 DB 审计，直接验证实际接入函数；诊断采样由 _diag_record 触发。
    """
    _enable()
    s = get_settings()
    from app.storage import crypto_gate as CG

    CG._diag_record("DEK 封套校验失败")
    snap = D.health_snapshot()
    assert snap["total_samples"] == 1
    # 密钥不匹配归 key 域，置信度基准 M
    keycls = next(c for c in snap["classes"] if c["class"] == "key_mismatch")
    assert keycls["base_confidence"] == "M"
    # 关掉诊断后：快照整体禁用（enabled=False、样本归零）
    s.ENCRYPT_DIAGNOSE_ENABLED = False
    CG._diag_record("DEK 封套校验失败")
    snap_off = D.health_snapshot()
    assert snap_off["enabled"] is False
    assert snap_off["total_samples"] == 0
