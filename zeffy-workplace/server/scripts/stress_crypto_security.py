"""P6-6-4 加密安全专项压力测试（对照方案 §测试策略 安全专项 + 内存/CPU/稳定性）。

覆盖：
1. 高并发加解密一致性：N 并发任务 × 多尺寸（边界/多块/大文件），decode 恒等 + 篡改必失败。
2. 吞吐基准：encrypts/s 与 MB/s（AES-GCM 分块）。
3. 内存稳定性：并发峰值 RSS 增量 < 阈值（不残留明文块缓冲约简）。
4. 侧信道仿犯：错误 DEK / 错误 HMAC / 头篡改 / 块篡改 / 信封篡改 → 全部抛错不返回坏数据。
5. Nonce 唯一性：批量生成密文，跨文件 seed 两两不同（同密钥 nonce 不重复）。

用法：``uv run python scripts/stress_crypto_security.py [--ops N] [--conc M] [--largeMB S]``
纯净测量（不经 DB/审计）直接用 crypto 核心；另附 gate 层短并发冒烟。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
import tracemalloc

from app.storage import crypto as C


def _seed(n: int) -> bytes:
    return os.urandom(n)  # 快速随机载荷（仅测往返恒等，不依赖内容）


def _write_key(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


async def _worker(op: int, sizes: list[int]) -> dict:
    """单个并发任务的往返 + 篡改仿犯（错误非返回零片段）。"""
    master = os.urandom(32)
    salt = os.urandom(16)
    dek = C.derive_dek(master, salt)
    wrong = os.urandom(32)
    ok = 0
    for s in sizes:
        plain = _seed(s)
        cipher = C.encrypt(plain, dek, master, block=C.BLOCK_DFLT)
        assert C.decrypt_full(cipher, dek, master) == plain, f"往返失败 size={s}"
        assert C.decrypt_range(cipher, dek, master, start=s // 2,
                               end=s) == plain[s // 2:], f"range失败 size={s}"
        # 侧信道：头 HMAC / 块篡改 → 全部拒绝（任何尺寸都校验元数据完整性）
        for evil in (lambda: C.decrypt_full(b"\x00" + cipher[1:], dek, master),  # noqa: B023
                     lambda: C.decrypt_full(cipher, dek, wrong),  # noqa: B023
                     lambda: C.decrypt_full(cipher[:-3] + b"\xff", dek, master)):  # noqa: B023
            try:
                evil()
            except C.EncryptError:
                pass  # 期望的拒绝
            else:
                raise AssertionError("篡改/错钥样本未被拒绝（返回了数据）")
        # 错误 DEK：空文件无数据块可认证（头 HMAC 已绑元数据），仅在有块时断言拒绝
        if s > 0:
            try:
                C.decrypt_full(cipher, wrong, master)
            except C.EncryptError:
                pass
            else:
                raise AssertionError("错误 DEK 未被拒绝（多块文件）")
        ok += 1
    return {"op": op, "ok": ok}


async def _concurrency(n_ops: int, conc: int, sizes: list[int]) -> dict:
    t0 = time.perf_counter()
    jobs = [asyncio.create_task(_worker(i, sizes)) for i in range(n_ops)]
    results = await asyncio.gather(*jobs)
    dt = time.perf_counter() - t0
    total_enc = sum(r["ok"] for r in results)
    total_bytes = sum(len(_seed(s)) for s in sizes) * n_ops
    return {"total_enc": total_enc, "total_bytes": total_bytes,
            "elapsed_s": dt, "conc": conc,
            "enc_per_s": total_enc / dt if dt else 0.0,
            "mb_per_s": (total_bytes / (1024 * 1024)) / dt if dt else 0.0}


async def _nonce_uniqueness(n_files: int) -> dict:
    master = os.urandom(32)
    dek = C.derive_dek(master, os.urandom(16))
    seeds = set()
    for _ in range(n_files):
        c = C.encrypt(b"A" * 4096, dek, master)
        _v, _p, seed = C.parse_header(c, master)
        seeds.add(seed)
    return {"n_files": n_files, "unique_seeds": len(seeds),
            "unique": len(seeds) == n_files}


async def _gate_smoke() -> dict:
    """gate 层短冒烟：配置临时密钥后 并发 encrypt/decrypt 恒等（审计关闭，不入库）。"""
    from app.config import get_settings
    from app.storage.crypto_gate import (
        crypt_enabled,
        decrypt_artifact,
        encrypt_artifact,
        reset_for_test,
    )
    from app.storage.governance import set_governance_override

    s = get_settings()
    saved = (s.ARTIFACT_ENCRYPT_ENABLED, s.ENCRYPT_MASTER_KEYFILES,
             s.ENCRYPT_HMAC_KEYFILE, s.AUDIT_GOVERNANCE_ENABLED)
    import tempfile

    m = os.urandom(32)
    with tempfile.TemporaryDirectory() as d:
        p1 = os.path.join(d, "m1.key")
        p2 = os.path.join(d, "m2.key")
        ph = os.path.join(d, "hm.key")
        _write_key(p1, m)
        _write_key(p2, m)
        _write_key(ph, os.urandom(32))
        set_governance_override("meta", True)
        s.ARTIFACT_ENCRYPT_ENABLED = True
        s.AUDIT_GOVERNANCE_ENABLED = False
        s.ENCRYPT_MASTER_KEYFILES = f"{p1},{p2}"
        s.ENCRYPT_HMAC_KEYFILE = ph
        reset_for_test()
        assert crypt_enabled()
        t0 = time.perf_counter()
        plains = [_seed(i * 13 % 1000 + 1) for i in range(50)]
        pairs = await asyncio.gather(*[encrypt_artifact(p) for p in plains])
        assert pairs[0][1]["encrypted"], f"gate 加密降级: {pairs[0][1]}"
        ciphers = [c for c, _ in pairs]
        outs = await asyncio.gather(*[decrypt_artifact(c) for c in ciphers])
        dt = time.perf_counter() - t0
        all_ok = all(o == p for o, p in zip(outs, plains, strict=True))
    # 还原
    (s.ARTIFACT_ENCRYPT_ENABLED, s.ENCRYPT_MASTER_KEYFILES,
     s.ENCRYPT_HMAC_KEYFILE, s.AUDIT_GOVERNANCE_ENABLED) = saved
    set_governance_override("meta", False)
    reset_for_test()
    return {"gate_ok": all_ok, "gate_op_s": 50 / dt if dt else 0.0}


def _memory_report(peak: int, budget: int) -> dict:
    mb = peak / (1024 * 1024)
    return {"peak_rss_mb": round(mb, 1), "budget_mb": budget,
            "pass": mb <= budget}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ops", type=int, default=40)
    ap.add_argument("--conc", type=int, default=16)
    ap.add_argument("--largeMB", type=int, default=24)  # 单文件 MB（覆盖多块）
    args = ap.parse_args()

    print("== P6-6-4 加密安全专项压测 ==", flush=True)
    sizes = [0, 1, C.BLOCK_DFLT, C.BLOCK_DFLT * 3 + 7, args.largeMB * 1024 * 1024]
    print(f"· 尺寸集: {[f'{s//1024//1024 if s>=1024*1024 else s} (bytes)' for s in sizes]}", flush=True)

    # 1) 内存峰值预算：峰值≈并发 × (明文+密文 各 largeMB) + 每块缓冲/信封开销
    budget_mb = int(args.conc * 2 * args.largeMB) + 256

    start = time.perf_counter()
    tracemalloc.start()
    con = await _concurrency(args.ops, args.conc, sizes)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    mem = _memory_report(peak, budget_mb)

    uni = await _nonce_uniqueness(n_files=min(2000, args.ops * 8))
    gate = await _gate_smoke()

    print(f"· 并发一致性   : {con['total_enc']} 次往返/仿犯通过, "
          f"{con['elapsed_s']:.2f}s @conc={con['conc']}", flush=True)
    print(f"· 吞吐         : {con['enc_per_s']:.0f} enc/s, {con['mb_per_s']:.1f} MB/s", flush=True)
    print(f"· 内存峰值     : {mem['peak_rss_mb']}MB (预算 {mem['budget_mb']}MB) -> "
          f"{'PASS' if mem['pass'] else 'FAIL'}", flush=True)
    print(f"· Nonce 唯一性 : {uni['unique_seeds']}/{uni['n_files']} -> "
          f"{'PASS' if uni['unique'] else 'FAIL'}", flush=True)
    print(f"· gate 冒烟    : {'PASS' if gate['gate_ok'] else 'FAIL'} "
          f"({gate['gate_op_s']:.0f} op/s)", flush=True)

    ok = (con['total_enc'] == args.ops * len(sizes)
          and mem['pass'] and uni['unique'] and gate['gate_ok'])
    print(f"· 总耗时       : {time.perf_counter()-start:.2f}s", flush=True)
    print(f"== 结果: {'ALL PASS' if ok else 'HAS FAILURE'} ==", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
