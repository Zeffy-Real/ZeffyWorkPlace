"""P7-A3 压缩层 · 压缩决策 + 流式压缩/解压 wrap（gzip 内置 / zstd 可选）。

与加密解耦：本模块只负责「是否压缩 + 分块压缩输出 + 分块解压输入」。
压缩发生在**加密之前**（明文侧），解压发生在**解密之后**；压缩标识（compressed/algo/level）
由调用方决定如何承载与认证（读侧以密文头压缩标识为解析依据）。

三级熵策略（审查🔴）：
1. 扩展名白名单（include / exclude）；命中排除集 → 不压缩；
2. 阈值（< COMPRESS_MIN_SIZE 不采样不压缩）；
3. 块级动态：首块采样熵 → 高熵（疑似已压缩/二进制）跳过，否则压缩（**单向决策不回退流**）。

兼容锚点：``COMPRESS_ENABLED=false`` 时决策恒返回不压缩，零开销。
"""
from __future__ import annotations

import math
import zlib
from collections.abc import AsyncIterable, AsyncIterator

from app.config import get_settings

# 默认排除已压缩/二进制集（压缩无收益）
DEFAULT_EXCLUDE = {
    ".gz", ".zip", ".7z", ".bz2", ".xz", ".zst",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".mp3", ".mp4", ".mkv", ".mov", ".avi", ".webm", ".wav", ".flac",
    ".pdf", ".parquet", ".h5", ".hdf5", ".bin",
    ".woff", ".woff2", ".ttf", ".otf", ".so", ".dll", ".exe",
}

# 允许的压缩算法与级别域
_ALGO_LEVELS = {
    "gzip": (1, 9),
    "zstd": (1, 22),
}


def _entropy_sample(buf: bytes) -> float:
    """缓冲区香农熵（0..8）。已压缩/二进制熵接近 8。"""
    if not buf:
        return 0.0
    counts = [0] * 256
    for b in buf:
        counts[b] += 1
    n = len(buf)
    return -sum(c / n * math.log2(c / n) for c in counts if c)


def _ext(rel_path: str) -> str:
    base = rel_path.rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()


def decide_compress(*, rel_path: str, total: int | None,
                    first_chunk: bytes) -> tuple[bool, str, int]:
    """三级熵决策（单向，不回退流）。返回 (should_compress, algo, level)。"""
    s = get_settings()
    if not s.COMPRESS_ENABLED:
        return False, "", 0
    algo = (s.COMPRESS_ALGO or "gzip").lower()
    if algo not in _ALGO_LEVELS:
        algo = "gzip"
    lo, hi = _ALGO_LEVELS[algo]
    level = max(lo, min(hi, int(s.COMPRESS_LEVEL or 6)))

    ext = _ext(rel_path)
    inc = {x.lower().lstrip(".") for x in (s.COMPRESS_EXT_INCLUDE or "").split(",") if x.strip()}
    # 排除集：配置为空 → 用默认内置集（但显式配置空字符串视为用默认）
    raw_exc = (s.COMPRESS_EXT_EXCLUDE or "").split(",") if s.COMPRESS_EXT_EXCLUDE else []
    exc = {x.lower().lstrip(".") for x in raw_exc if x.strip()}
    if not exc:
        exc = {e.lstrip(".") for e in DEFAULT_EXCLUDE}

    # 一级：类型白名单 / 排除集
    if inc and (not ext or ext not in inc):
        return False, "", 0
    if ext and ext in exc:
        return False, "", 0
    # 二级：阈值（不采样直接不压缩）
    if total is not None and total < max(1, int(s.COMPRESS_MIN_SIZE)):
        return False, "", 0
    # 三级：首块熵采样；高熵（已压缩/二进制）跳过
    if s.COMPRESS_ENTROPY_CHECK and first_chunk and _entropy_sample(first_chunk) >= 7.0:
        return False, "", 0
    return True, algo, level


def compress_fingerprint(should: bool, algo: str, level: int) -> str:
    """压缩参数指纹（并入去重键第二维度）。未压缩 → 空串。"""
    return f"{algo}:{level}" if should else ""


def compress_iter(data: AsyncIterable[bytes], *, algo: str | None = None,
                  level: int | None = None) -> AsyncIterator[bytes]:
    """流式压缩：明文 async iter → 压缩块 async iter（内存常量级）。gzip 标准库；zstd 需 zstandard。"""
    s = get_settings()
    a = (algo or s.COMPRESS_ALGO or "gzip").lower()
    if a not in _ALGO_LEVELS:
        a = "gzip"
    lo, hi = _ALGO_LEVELS[a]
    lv = max(lo, min(hi, int(level if level is not None else s.COMPRESS_LEVEL)))
    if a == "gzip":
        return _gzip_compress(data, lv)
    return _zstd_compress(data, lv)


def decompress_iter(data: AsyncIterable[bytes], *, algo: str = "gzip",
                    level: int | None = None) -> AsyncIterator[bytes]:
    """流式解压：压缩块 async iter → 明文 async iter（内存常量级）。"""
    a = (algo or "gzip").lower()
    if a not in _ALGO_LEVELS:
        a = "gzip"
    if a == "gzip":
        return _gzip_decompress(data)
    return _zstd_decompress(data, level)


async def _gzip_compress(data: AsyncIterable[bytes], level: int) -> AsyncIterator[bytes]:
    comp = zlib.compressobj(level=level, wbits=31)  # gzip member
    try:
        async for chunk in data:
            if chunk:
                out = comp.compress(chunk)
                if out:
                    yield out
        tail = comp.flush()
        if tail:
            yield tail
    finally:
        pass


async def _gzip_decompress(data: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    dec = zlib.decompressobj(wbits=31)
    try:
        async for chunk in data:
            out = dec.decompress(chunk)
            if out:
                yield out
        tail = dec.flush()
        if tail:
            yield tail
    except zlib.error as exc:
        raise ValueError(f"压缩数据损坏（gzip）：{exc}") from exc


async def _zstd_compress(data: AsyncIterable[bytes], level: int) -> AsyncIterator[bytes]:
    import zstandard  # noqa: PLC0415 可选依赖，仅 zstd 时引入

    comp = zstandard.ZstdCompressor(level=level).compressobj()
    try:
        async for chunk in data:
            if chunk:
                out = comp.compress(chunk)
                if out:
                    yield out
        tail = comp.flush()
        if tail:
            yield tail
    finally:
        pass


async def _zstd_decompress(data: AsyncIterable[bytes],
                           level: int | None) -> AsyncIterator[bytes]:
    import zstandard  # noqa: PLC0415

    dctx = zstandard.ZstdDecompressor()
    dec = dctx.decompressobj()
    try:
        async for chunk in data:
            out = dec.decompress(chunk)
            if out:
                yield out
        tail = dec.decompress(b"", len(dec.unused_data) or 0) if hasattr(dec, "unused_data") else b""
        if tail:
            yield tail
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"压缩数据损坏（zstd）：{exc}") from exc
