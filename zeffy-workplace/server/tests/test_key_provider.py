"""P7-A1 KMS 集成 · KeyProvider 单元测试。

覆盖：
1. 默认 local：get_key_provider() → LocalFileProvider（零漂移路由）
2. ENCRYPT_KEY_PROVIDER=kms → KMSProvider 单例
3. KMSProvider 缓存语义：TTL 内命中缓存（少调用 KMS）、到期回源
4. KMSProvider 中断降级：有缓存 → 解密继续返回旧密钥；无缓存 → None
5. 超时分级：中断持续 > recovery → level=critical；初期 warn
6. 后台静默重试：失败不抛异常（不影响主链路）
7. revoke 清缓存；指标白名单（calls/failures/avg_latency，无密钥字节）
8. reset 复位单例与状态
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.security import key_provider as KP


@pytest.fixture(autouse=True)
def _reset():
    KP.reset_key_provider_for_test()
    s = get_settings()
    s.ENCRYPT_KEY_PROVIDER = "local"
    s.KMS_CACHE_TTL = 60
    s.KMS_RECOVERY_S = 300
    yield
    KP.reset_key_provider_for_test()
    s.ENCRYPT_KEY_PROVIDER = "local"


# ---- 1/2. 路由 ----
@pytest.mark.asyncio
async def test_default_local_provider():
    p = KP.get_key_provider()
    assert isinstance(p, KP.LocalFileProvider)


@pytest.mark.asyncio
async def test_kms_route():
    get_settings().ENCRYPT_KEY_PROVIDER = "kms"
    KP.reset_key_provider_for_test()
    p = KP.get_key_provider()
    assert isinstance(p, KP.KMSProvider)
    # 单例
    assert KP.get_key_provider() is p


class _FakeKMS(KP.KMSProvider):
    """可注入的假 KMS：按版本返回固定密钥；可设失败模式。"""

    def __init__(self, *, fail: bool = False, delay: float = 0.0):
        super().__init__()
        self.fail = fail
        self.fetch_count = 0
        self.delay = delay

    def _kms_fetch(self, *, version: int) -> bytes | None:
        import time as _t

        if self.delay:
            _t.sleep(self.delay)
        self.fetch_count += 1
        return None if self.fail else _mk(version)


def _mk(v: int) -> bytes:
    return (v.to_bytes(16, "big") * 2)  # 32B


# ---- 3. 缓存 ----
@pytest.mark.asyncio
async def test_kms_cache_hit_reduces_fetch():
    p = _FakeKMS()
    get_settings().ENCRYPT_KEY_PROVIDER = "kms"
    KP.reset_key_provider_for_test()
    KP._provider_inst = p  # 注入假实现
    # 首次回源
    a = KP.get_key_provider().get_master(version=1)
    assert a == _mk(1)
    # TTL 内再取 → 命中缓存，不再 fetch
    b = KP.get_key_provider().get_master(version=1)
    assert b == _mk(1)
    assert p.fetch_count == 1


@pytest.mark.asyncio
async def test_kms_ttl_expire_refetch():
    p = _FakeKMS()
    get_settings().ENCRYPT_KEY_PROVIDER = "kms"
    KP.reset_key_provider_for_test()
    KP._provider_inst = p
    p._ttl = 0  # 到期即回源
    a = KP.get_key_provider().get_master(version=1)
    assert a == _mk(1)
    c = KP.get_key_provider().get_master(version=1)
    assert c == _mk(1)
    assert p.fetch_count == 2


# ---- 4. 中断降级 ----
@pytest.mark.asyncio
async def test_kms_breakdown_with_cache_degrade():
    p = _FakeKMS()
    get_settings().ENCRYPT_KEY_PROVIDER = "kms"
    KP.reset_key_provider_for_test()
    KP._provider_inst = p
    # 先成功取回，缓存有密钥
    m = KP.get_key_provider().get_master(version=1)
    assert m == _mk(1)
    # 强制过期 + 进入失败模式 → 有缓存降级解密可用
    p._ttl = 0
    p.fail = True
    m2 = KP.get_key_provider().get_master(version=1)
    assert m2 == _mk(1)  # 降级：返回旧缓存（解密继续）
    st = KP.get_key_provider().status()
    assert st["mode"] == "degraded"
    assert st["level"] in ("warn", "critical")


@pytest.mark.asyncio
async def test_kms_breakdown_no_cache_none():
    p = _FakeKMS(fail=True)
    get_settings().ENCRYPT_KEY_PROVIDER = "kms"
    KP.reset_key_provider_for_test()
    KP._provider_inst = p
    assert KP.get_key_provider().get_master(version=1) is None
    assert KP.get_key_provider().status()["mode"] == "degraded"


# ---- 5. 超时分级 ----
@pytest.mark.asyncio
async def test_kms_critical_after_recovery():
    p = _FakeKMS(fail=True)
    get_settings().ENCRYPT_KEY_PROVIDER = "kms"
    get_settings().KMS_RECOVERY_S = 0  # 立即超时
    KP.reset_key_provider_for_test()
    KP._provider_inst = p
    assert KP.get_key_provider().get_master(version=1) is None
    st = KP.get_key_provider().status()
    assert st["level"] == "critical"


# ---- 6. 后台静默重试（异常不上抛） ----
@pytest.mark.asyncio
async def test_kms_fetch_exception_swallowed():
    class _Throw(KP.KMSProvider):
        def _kms_fetch(self, *, version: int) -> bytes | None:
            raise RuntimeError("KMS 网络异常")

    get_settings().ENCRYPT_KEY_PROVIDER = "kms"
    KP.reset_key_provider_for_test()
    KP._provider_inst = _Throw()
    # 不应上抛，返回 None，指标记失败
    assert KP.get_key_provider().get_master(version=1) is None
    assert KP.get_key_provider().status()["failures"] == 1


# ---- 7. revoke + 指标白名单 ----
@pytest.mark.asyncio
async def test_revoke_clears_cache_and_metric_whitelist():
    p = _FakeKMS()
    get_settings().ENCRYPT_KEY_PROVIDER = "kms"
    KP.reset_key_provider_for_test()
    KP._provider_inst = p
    KP.get_key_provider().get_master(version=2)
    assert KP.get_key_provider().status()["cached_versions"] == [2]
    KP.get_key_provider().revoke()
    assert KP.get_key_provider().status()["cached_versions"] == []
    # 指标白名单：无密钥字节
    st = KP.get_key_provider().status()
    assert set(st.keys()) == {"mode", "level", "degraded_s", "cached_versions",
                              "calls", "failures", "avg_latency_ms", "recovery_s"}
    text = str(st)
    assert ".key" not in text and "C:" not in text


@pytest.mark.asyncio
async def test_local_provider_requires_no_kms_config():
    # local 模式不需要 KMS 配置，仍能 get_key_provider
    get_settings().ENCRYPT_KEY_PROVIDER = "local"
    KP.reset_key_provider_for_test()
    assert isinstance(KP.get_key_provider(), KP.LocalFileProvider)
