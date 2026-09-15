"""P6-6-6 密钥轮换 · 运维自动化脚本（进程内复用 app 模块，与压测脚本同模式）。

子命令：
- ``gen --out-dir DIR --ver N [--created EPOCH]``  生成新主密钥（双副本 + #zfk 头）
- ``scan``                                        本地扫描存量密文版本引用分布
- ``rewrap --version N [--batch B] [--dry-run]``  把 N 版本密文 DEK 重裹到当前版本（分批）
- ``status``                                      打印加密健康/生命周期/引用分布

用法（在 server 目录，PYTHONPATH 指向 server 根）::

    $env:PYTHONPATH="<repo>/server"
    uv run python scripts/rotate_keys.py gen --out-dir /secure/keys --ver 2
    uv run python scripts/rotate_keys.py status
    uv run python scripts/rotate_keys.py scan
    uv run python scripts/rotate_keys.py rewrap --version 1 --batch 50 --dry-run
    uv run python scripts/rotate_keys.py rewrap --version 1 --batch 50

安全约束（对齐手册 §7）：
- 不打印任何密钥内容；仅元信息（版本/指纹前缀/计数）。
- 重裹仅显式按版本扫描出的 keys；dry-run 先预览。
- 不执行回收/删除（三阶段回收需人工 + admin API，脚本仅门槛预览）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def _banner() -> None:
    print("== P6-6-6 密钥轮换自动化 ==", flush=True)


# ---- gen：生成新主密钥（双副本 + #zfk 头） ----
def cmd_gen(args) -> int:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ver = args.ver
    created = args.created or int(time.time())
    master = os.urandom(32)
    fp = HKDF(algorithm=hashes.SHA256(), length=8, salt=b"zfk-fp",
              info=b"fp").derive(master).hex()
    for suffix in ("a", "b"):
        p = out / f"master_v{ver}{suffix}.key"
        with open(p, "wb") as f:
            f.write(f"#zfk ver={ver} created={created} fp={fp}\n".encode() + master)
        os.chmod(p, 0o600)
        print(f"· 已生成 {p}（0600）", flush=True)
    print(f"· 版本 {ver} 指纹 {fp[:8]}…", flush=True)
    print("下一步：更新 ENCRYPT_CIPHER_VERSION / ENCRYPT_MASTER_KEYFILES，旧版本进 ENCRYPT_LEGACY_KEYFILES（见运维手册 §2）", flush=True)
    return 0


async def _scan_refs() -> dict:
    """本地扫描正式产物密文的版本分布（不加载密钥，仅读头 ver）。"""
    from app.storage import get_backend
    from app.storage.crypto_gate import is_encrypted_blob
    from app.storage.governance import _list_governance_keys

    backend = get_backend()
    refs: dict[int, list[str]] = {}
    for key in await _list_governance_keys(backend):
        try:
            blob = await backend.get(key)
        except Exception:  # noqa: BLE001
            continue
        if is_encrypted_blob(blob):
            ver = blob[7]
            refs.setdefault(ver, []).append(key)
    return refs


async def _cmd_scan(args) -> int:
    refs = await _scan_refs()
    print(f"· 版本引用分布（{sum(len(v) for v in refs.values())} 个密文）", flush=True)
    for ver in sorted(refs):
        print(f"  v{ver}: {len(refs[ver])}", flush=True)
    return 0


def cmd_scan(args) -> int:
    import asyncio

    return asyncio.run(_cmd_scan(args))


# ---- status：健康/生命周期 ----
async def _cmd_status(args) -> int:
    from app.storage.crypto_gate import crypto_metrics, key_lifecycle_metrics

    m = crypto_metrics()
    lc = key_lifecycle_metrics()
    refs = await _scan_refs()
    print(f"· enabled={m['enabled']} key_loaded={m['key_loaded']} "
          f"cipher_version={m['cipher_version']}", flush=True)
    print(f"· 生命周期: current={lc['current_version']} 到期剩余="
          f"{lc['expire_in_days']}d 级别={lc['expiry_level']} "
          f"归档={lc['legacy_versions']} 灰度={lc['gray_version']}({lc['gray_ratio']})", flush=True)
    print(f"· 引用分布: { {k: len(v) for k, v in refs.items()} }", flush=True)
    return 0


def cmd_status(args) -> int:
    import asyncio

    return asyncio.run(_cmd_status(args))


# ---- rewrap：按版本分批重裹（进程内；dry-run 预览） ----
async def _rewrap_version(ver: int, batch: int, dry_run: bool) -> int:
    from app.storage.crypto_gate import rotate_rewrap_deks

    refs = _scan_refs()
    keys = refs.get(ver, [])
    if not keys:
        print(f"· v{ver} 无密文引用，无需重裹", flush=True)
        return 0
    print(f"· v{ver} 共 {len(keys)} 个密文待重裹（batch={batch}）", flush=True)
    if dry_run:
        print(f"· [dry-run] 将处理前 {min(batch, len(keys))} 个示例：{keys[:min(batch, 3)]}…", flush=True)
        return 0
    total_ok = total_fail = 0
    for i in range(0, len(keys), batch):
        chunk = keys[i:i + batch]
        res = await rotate_rewrap_deks(keys=chunk)
        total_ok += int(res.get("rewrapped", 0))
        total_fail += int(res.get("failed", 0))
        print(f"· 批次 {i // batch + 1}: rewrapped={res.get('rewrapped')} "
              f"failed={res.get('failed')}", flush=True)
        for fk in res.get("failed_keys") or []:
            print(f"  ! 失败 {fk}", flush=True)
    print(f"· 完成: rewrapped={total_ok} failed={total_fail}", flush=True)
    return 0 if total_fail == 0 else 1


def cmd_rewrap(args) -> int:
    import asyncio

    return asyncio.run(_rewrap_version(args.version, args.batch, args.dry_run))


def main() -> int:
    ap = argparse.ArgumentParser(prog="rotate_keys")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="生成新主密钥（双副本 + #zfk 头）")
    g.add_argument("--out-dir", required=True, help="输出目录（0600 权限写入）")
    g.add_argument("--ver", type=int, required=True, help="新版本号（须 > 当前）")
    g.add_argument("--created", type=int, default=0, help="创建时间 epoch（默认 now）")
    g.set_defaults(fn=cmd_gen)

    sub.add_parser("scan", help="本地扫描存量密文版本引用分布").set_defaults(fn=cmd_scan)
    sub.add_parser("status", help="加密健康/生命周期/引用分布").set_defaults(fn=cmd_status)

    r = sub.add_parser("rewrap", help="把某版本密文 DEK 重裹到当前版本（分批）")
    r.add_argument("--version", type=int, required=True)
    r.add_argument("--batch", type=int, default=50)
    r.add_argument("--dry-run", action="store_true", help="仅预览不执行")
    r.set_defaults(fn=cmd_rewrap)

    args = ap.parse_args()
    _banner()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
