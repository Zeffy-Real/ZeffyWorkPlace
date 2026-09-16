"""P7-A3 压缩层 · 单元测试（决策 / gzip 往返 / 熵三级 / 内存常量级）。

聚焦与加密解耦的压缩判定与流式往返；加密头承载压缩标识由 A3-S1b 专项测试覆盖。
"""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.storage import compress as COMP


async def _iter_bytes(chunks: list[bytes]) -> object:
    """把字节列表包装为 async iterable。"""
    async def _g():
        for c in chunks:
            yield c
    return _g()


def _aiter(lst):
    """同步 list → async iterator（逐对象 yield）。"""
    async def _g():
        for x in lst:
            yield x
    return _g()


@pytest.fixture(autouse=True)
def _compress_env():
    s = get_settings()
    s.COMPRESS_ENABLED = True
    s.COMPRESS_ALGO = "gzip"
    s.COMPRESS_LEVEL = 6
    s.COMPRESS_MIN_SIZE = 1024
    s.COMPRESS_EXT_INCLUDE = ""
    s.COMPRESS_EXT_EXCLUDE = ""
    s.COMPRESS_ENTROPY_CHECK = True
    yield s
    s.COMPRESS_ENABLED = False
    s.COMPRESS_EXT_INCLUDE = ""
    s.COMPRESS_EXT_EXCLUDE = ""
    s.COMPRESS_ALGO = "gzip"
    s.COMPRESS_ENTROPY_CHECK = True
    s.COMPRESS_MIN_SIZE = 1024


def test_switch_off_shortcircuit(_compress_env):
    _compress_env.COMPRESS_ENABLED = False
    assert COMP.decide_compress(rel_path="a.txt", total=10000, first_chunk=b"x") == (False, "", 0)


def test_decide_min_size_skip(_compress_env):
    _compress_env.COMPRESS_MIN_SIZE = 1000
    # 小于阈值 → 不压缩（三级之一：不采样直接跳过）
    assert COMP.decide_compress(rel_path="a.txt", total=500, first_chunk=b"1234") == (False, "", 0)


def test_decide_ext_include_whitelist(_compress_env):
    _compress_env.COMPRESS_EXT_INCLUDE = ".txt,.json"
    assert COMP.decide_compress(rel_path="x.log", total=5000, first_chunk=b"aaa") == (False, "", 0)
    should, algo, lv = COMP.decide_compress(rel_path="x.json", total=5000, first_chunk=b"aaa")
    assert should is True and algo == "gzip"


def test_decide_ext_exclude(_compress_env):
    _compress_env.COMPRESS_EXT_EXCLUDE = ""
    should, _, _ = COMP.decide_compress(rel_path="x.png", total=5000, first_chunk=b"\xff\xd8\xff")
    assert should is False  # 默认排除集含 .png


def test_decide_entropy_high_skip(_compress_env):
    # 高熵首块（近似随机/已压缩）→ 跳过
    import os
    rnd = os.urandom(256)
    should, _, _ = COMP.decide_compress(rel_path="x.bin", total=100000, first_chunk=rnd)
    # .bin 在默认排除集 → 直接跳过；改用 .dat（不在排除集）
    should, _, _ = COMP.decide_compress(rel_path="x.dat", total=100000, first_chunk=rnd)
    assert should is False


def test_decide_low_entropy_compress(_compress_env):
    text = b"hello world " * 20
    should, algo, lv = COMP.decide_compress(rel_path="x.dat", total=100000, first_chunk=text)
    assert should is True and algo == "gzip" and lv == 6


def test_fingerprint_empty_when_not_compress(_compress_env):
    assert COMP.compress_fingerprint(False, "", 0) == ""
    assert COMP.compress_fingerprint(True, "gzip", 6) == "gzip:6"


async def test_gzip_roundtrip_chunks(_compress_env):
    plain = b"A" * 3000 + b"#" * 100 + b"B" * 2000
    comp_stream = COMP.compress_iter(_aiter([plain[i:i + 256] for i in range(0, len(plain), 256)]))
    compressed = b"".join([c async for c in comp_stream])
    # 高重复文本应被压缩变小
    assert len(compressed) < len(plain)
    dec_stream = COMP.decompress_iter(_aiter([compressed]), algo="gzip")
    out = b"".join([c async for c in dec_stream])
    assert out == plain


async def test_gzip_roundtrip_text_utf8(_compress_env):
    plain = ("加密合规报表中文测试内容。" * 40).encode("utf-8")
    comp = b"".join([c async for c in COMP.compress_iter(_aiter([plain[:1000], plain[1000:]]))])
    out = b"".join([c async for c in COMP.decompress_iter(_aiter([comp]), algo="gzip")])
    assert out == plain


async def test_compress_empty(_compress_env):
    comp = b"".join([c async for c in COMP.compress_iter(_aiter([b""]))])
    out = b"".join([c async for c in COMP.decompress_iter(_aiter([comp]), algo="gzip")])
    assert out == b""


async def test_memory_constant_peak(_compress_env):
    """流式内存常量级：同时刻不整块加载明文，峰值限于压缩器常量缓冲。"""
    import tracemalloc

    big = (b"prefix-" + b"x" * 2000) * 300  # ~600KB 明文
    # big 在 tracemalloc.start() 前分配，不被统计；分块惰性产出
    async def _gen():
        for i in range(0, len(big), 2048):
            yield big[i:i + 2048]

    tracemalloc.start()
    try:
        total = 0
        async for c in COMP.compress_iter(_gen()):
            total += len(c)  # 流式消费，不累积
        cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # 常量级标准：峰值 ≤ 2×统一块缓冲（即未整块加载 600KB 明文）
    assert total > 0
    assert peak <= 2 * int(_compress_env.COMPRESS_BLOCK_BYTES)


def test_entropy_sample_bounds():
    assert COMP._entropy_sample(b"") == 0.0
    assert COMP._entropy_sample(b"\x00" * 100) == 0.0
    import os
    assert COMP._entropy_sample(os.urandom(256)) > 7.0
