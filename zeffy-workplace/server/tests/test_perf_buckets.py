"""P7-B3 分文件大小性能区间统计 · 单元测试。

覆盖：
1. _bucket_of 临界点：0 / 1048575 / 1048576 / 16777215 / 16777216 / 大数（含下不含上）
2. 桶配置空 → 单桶 all（兼容既有）；非法配置 → all
3. _perf_buckets：分桶统计正确（samples/avg/p50/p95/mb_per_s）
4. 数据守恒：各桶 samples 之和 = 全量 _perf_stats.samples（窗口内）
5. 开关短路：ENCRYPT_PERF_ENABLED=false → 无采样、perf_buckets 空
6. 白名单：perf_buckets 仅桶名/统计量，无敏感字段
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.storage import crypto_gate as CG


@pytest.fixture(autouse=True)
def _reset():
    from app.storage.crypto_gate import reset_for_test

    reset_for_test()
    s = get_settings()
    s.ENCRYPT_PERF_ENABLED = True
    s.ENCRYPT_PERF_BUCKETS = "1048576:1-16M,16777216:16M+"
    yield
    reset_for_test()
    s.ENCRYPT_PERF_ENABLED = True
    s.ENCRYPT_PERF_BUCKETS = "1048576:1-16M,16777216:16M+"


# ---- 1. _bucket_of 临界点 ----
@pytest.mark.asyncio
async def test_bucket_boundaries():
    assert CG._bucket_of(0) == "<1M"          # 空/0 → 首桶
    assert CG._bucket_of(1) == "<1M"
    assert CG._bucket_of(1048575) == "<1M"    # 1048576-1
    assert CG._bucket_of(1048576) == "1-16M"  # 含下（1048576 进 1-16M）
    assert CG._bucket_of(16777215) == "1-16M"  # 16777216-1
    assert CG._bucket_of(16777216) == "16M+"  # 含上（16777216 进 16M+）
    assert CG._bucket_of(100 * 1024 * 1024) == "16M+"


@pytest.mark.asyncio
async def test_bucket_of_empty_config():
    s = get_settings()
    s.ENCRYPT_PERF_BUCKETS = ""
    assert CG._bucket_of(0) == "all"
    assert CG._bucket_of(1048576) == "all"
    assert CG._bucket_of(999999999) == "all"


@pytest.mark.asyncio
async def test_bucket_of_invalid_config():
    s = get_settings()
    s.ENCRYPT_PERF_BUCKETS = "abc:bad,1:x"
    assert CG._bucket_of(0) == "all"
    assert CG._bucket_of(10) == "all"


# ---- 3. _perf_buckets 分桶统计 ----
@pytest.mark.asyncio
async def test_perf_buckets_correct():
    # 注入各桶样本（窗口内）
    CG._perf_record("encrypt", 0.010, 1024)        # <1M
    CG._perf_record("encrypt", 0.011, 2048)        # <1M
    CG._perf_record("encrypt", 0.050, 5 * 1024 * 1024)   # 1-16M
    CG._perf_record("encrypt", 0.100, 32 * 1024 * 1024)  # 16M+
    b = CG._perf_buckets("encrypt")
    assert b["<1M"]["samples"] == 2
    assert b["1-16M"]["samples"] == 1
    assert b["16M+"]["samples"] == 1
    assert b["<1M"]["avg_ms"] == pytest.approx(10.5, abs=0.1)  # (10+11)/2
    # 桶键白名单：仅桶标签（不含路径/密钥片段）
    assert set(b.keys()) == {"<1M", "1-16M", "16M+"}


# ---- 4. 数据守恒：桶样本和 = 全量 ----
@pytest.mark.asyncio
async def test_bucket_conservation():
    CG._perf_record("decrypt", 0.01, 512)
    CG._perf_record("decrypt", 0.02, 3 * 1024 * 1024)
    CG._perf_record("decrypt", 0.03, 20 * 1024 * 1024)
    b = CG._perf_buckets("decrypt")
    total_bucket = sum(v["samples"] for v in b.values())
    full = CG._perf_stats("decrypt")
    assert total_bucket == full["samples"]
    assert total_bucket == 3


# ---- 5. 开关短路 ----
@pytest.mark.asyncio
async def test_perf_disabled_no_buckets():
    s = get_settings()
    s.ENCRYPT_PERF_ENABLED = False
    CG._perf_record("encrypt", 0.01, 1024)  # 采样开关关 → _perf_record 内部 return
    assert CG._perf_buckets("encrypt") == {}
    assert CG._perf_stats("encrypt") == {"samples": 0}
    metrics = CG.crypto_metrics()
    # 开关关：无采样 → 分桶空
    assert metrics["perf_buckets"]["encrypt"] == {}
    assert metrics["perf_buckets"]["decrypt"] == {}


# ---- 6. 白名单 ----
@pytest.mark.asyncio
async def test_bucket_output_whitelist():
    CG._perf_record("encrypt", 0.01, 1024)
    b = CG._perf_buckets("encrypt")
    for bucket in b.values():
        assert set(bucket.keys()) == {"samples", "avg_ms", "p50_ms", "p95_ms", "mb_per_s"}
