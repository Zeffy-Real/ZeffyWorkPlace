"""P6-5 S3 真实协议验证 · S3 生命周期自动化 + 物理分层对账（moto server，零 Docker）。

Moto 起真实 S3 HTTP 端点，S3Backend 经 aiobotocore 走 S3 REST 协议验证：
- N1 apply_lifecycle：写入 Standard→IA 生命周期规则（无 Expiration）、幂等（重复调用不重复 PUT）
- N1 reconcile_tier_physical：对账物理存储类 vs 元数据期望，漂移 → copy 修正回 IA；再对账 drift=0
- 关闭开关回退：S3_LIFECYCLE_ENABLED=false → apply_lifecycle 返回 False

用法：cd server && uv run python scripts/smoke_s3_p65_real.py   （退出码 0=全过）
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from moto.moto_server.threaded_moto_server import ThreadedMotoServer

from app.storage.s3 import S3Backend

SERVER_PORT = 9001
BUCKET = "zeffy-p65"


class _MiniSettings:
    S3_ENDPOINT = f"http://127.0.0.1:{SERVER_PORT}"
    S3_BUCKET = BUCKET
    S3_REGION = "us-east-1"
    S3_ACCESS_KEY = "minioadmin"
    S3_SECRET_KEY = "minioadmin"
    ST_ARTIFACT_PUBLIC_BASE = ""
    ST_SIGNED_URL_TTL = 900


def _enable_cfg() -> bool:
    from app.config import get_settings

    s = get_settings()
    s.S3_LIFECYCLE_ENABLED = True
    s.TIER_COLD_S3_CLASS = "STANDARD_IA"
    s.TIER_COLD_ACCESS_AGE = 30 * 86400
    return True


async def _main() -> None:
    server = ThreadedMotoServer(port=SERVER_PORT)
    server.start()
    s = _MiniSettings()
    backend = S3Backend(s)

    import boto3

    b = boto3.client("s3", endpoint_url=s.S3_ENDPOINT,
                     aws_access_key_id=s.S3_ACCESS_KEY,
                     aws_secret_access_key=s.S3_SECRET_KEY,
                     region_name=s.S3_REGION)
    b.create_bucket(Bucket=BUCKET)

    passed = 0
    failed = 0

    async def check(name: str, cond: bool, detail: object = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [ok] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name} :: {detail}")

    try:
        print("== P6-5 S3 真实协议验证 (moto server) ==")

        # 0) 关闭开关回退：S3_LIFECYCLE_ENABLED=false → apply_lifecycle False
        from app.config import get_settings

        gs = get_settings()
        gs.S3_LIFECYCLE_ENABLED = False
        await check("apply_lifecycle disabled -> False", (await backend.apply_lifecycle()) is False)

        # 1) 开启后 apply_lifecycle：写入 Standard→IA 规则
        _enable_cfg()
        applied = await backend.apply_lifecycle()
        await check("apply_lifecycle.enabled -> True", applied is True)
        rule = b.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"][0]
        trans = (rule.get("Transitions") or [])
        await check("lifecycle transition -> STANDARD_IA",
                    trans and trans[0].get("StorageClass") == "STANDARD_IA", rule)
        await check("lifecycle has NO Expiration", not rule.get("Expiration"), rule)

        # 2) 幂等：重复调用不变化（规则仍为同一份，日数一致）
        stmp = dict(b.get_bucket_lifecycle_configuration(Bucket=BUCKET))
        await backend.apply_lifecycle()
        rule2 = b.get_bucket_lifecycle_configuration(Bucket=BUCKET)["Rules"][0]
        await check("lifecycle idempotent (unchanged)",
                    (rule2.get("Transitions") or []) == trans and not rule2.get("Expiration"))

        # 3) reconcile：新建对象默认 STANDARD，元数据期望 IA → 漂移并被 copy 修正
        key = "artifacts/p65/cold.txt"
        await backend.put(key, b"cold payload", mode="overwrite")
        head0 = b.head_object(Bucket=BUCKET, Key=key)
        await check("object storage-class STANDARD initially",
                    (head0.get("StorageClass") or "STANDARD") == "STANDARD", head0.get("StorageClass"))
        r1 = await backend.reconcile_tier_physical([key])
        await check("reconcile drift -> fixed", r1["drift"] == 1 and r1["fixed"] == 1, r1)
        await check("object now STANDARD_IA after fix",
                    b.head_object(Bucket=BUCKET, Key=key).get("StorageClass") == "STANDARD_IA")
        r2 = await backend.reconcile_tier_physical([key])
        await check("reconcile 2nd run drift=0", r2["drift"] == 0 and r2["fixed"] == 0, r2)

        # 4) 空 keys / 关闭场景
        r0 = await backend.reconcile_tier_physical([])
        await check("reconcile empty keys no-op", r0 == {"checked": 0, "drift": 0, "fixed": 0}, r0)

        print(f"\nRESULT: {passed} passed, {failed} failed")
    finally:
        server.stop()

    if failed:
        raise SystemExit(1)
    print("P6-5 S3 真实协议验证通过")


if __name__ == "__main__":
    asyncio.run(_main())