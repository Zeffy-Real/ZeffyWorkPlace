"""P5 S3 后端 · 真实 S3 REST 协议验证（moto server，零 Docker/零镜像依赖）。

背景：MinIO 镜像在当前网络（Docker Hub 被阻断 + daocloud 禁 minio）不可获取。
本脚本改用 moto（纯 Python）起**真实 S3 HTTP 端点**，`S3Backend` 经 aiobotocore
真实走 S3 REST 协议（put/copy/head/get/range/list/delete/head_bucket/multipart），
验证 S3 后端在真实协议下的行为，与 mock 桩（tests/_FakeClient）结论一致。

用法：
    cd server && uv run python scripts/smoke_s3_real.py

退出码 0=全过；非 0=失败（打印失败项）。
"""

from __future__ import annotations

import asyncio
import hashlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from moto.moto_server.threaded_moto_server import ThreadedMotoServer

from app.storage.s3 import S3Backend

SERVER_PORT = 9000
BUCKET = "zeffy"


class _MiniSettings:
    """极简设置对象，满足 S3Backend 构造所读字段。"""

    S3_ENDPOINT = f"http://127.0.0.1:{SERVER_PORT}"
    S3_BUCKET = BUCKET
    S3_REGION = "us-east-1"
    S3_ACCESS_KEY = "minioadmin"
    S3_SECRET_KEY = "minioadmin"
    ST_ARTIFACT_PUBLIC_BASE = ""
    ST_SIGNED_URL_TTL = 900


def _mk(size: int) -> bytes:
    return bytes((i * 37) & 0xFF for i in range(size))


async def _main() -> None:
    server = ThreadedMotoServer(port=SERVER_PORT)
    server.start()
    s = _MiniSettings()
    backend = S3Backend(s)

    # moto server 起后 bucket 不存在，先建 bucket（真实 create_bucket）
    import boto3

    client = boto3.client("s3", endpoint_url=s.S3_ENDPOINT,
                          aws_access_key_id=s.S3_ACCESS_KEY,
                          aws_secret_access_key=s.S3_SECRET_KEY,
                          region_name=s.S3_REGION)
    client.create_bucket(Bucket=BUCKET)

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
        print("== 真实 S3 协议验证 (moto server) ==")
        # 0) 健康/Bucket 可达
        h = await backend.health()
        await check("health.head_bucket reachable", h["ok"] and h["backend"] == "s3", h)

        # 1) put 单对象 + get 回读（overwrite）
        key = "artifacts/t1/doc.md"
        data = b"# Hi S3 real"
        m = await backend.put(key, data, mode="overwrite")
        await check("put returns meta.key/backend", m.key == key and m.backend == "s3")
        got = await backend.get(key)
        await check("get roundtrip", got == data)

        # 2) no_overwrite 拒绝 + new 后缀（幂等三态）
        try:
            await backend.put(key, b"v2", mode="no_overwrite")
            await check("no_overwrite refuses", False, "未拒绝")
        except Exception:
            await check("no_overwrite refuses", True)
        m2 = await backend.put("artifacts/t1/a.md", b"v1", mode="new", run_id="r9")
        await check("new suffix key", m2.key == "artifacts/t1/a-r9.md", m2.key)

        # 3) 大文件流式（多段上传 + .part 临时 key 原子落位）
        big = _mk(2 * 1024 * 1024 + 101)  # 略超 2MiB，触发多分块
        mb = await backend.put("artifacts/t1/big.bin", big, mode="overwrite")
        await check("stream put size/backend", mb.backend == "s3")
        chunks = [c async for c in backend.stream("artifacts/t1/big.bin")]
        joined = b"".join(chunks)
        await check("stream roundtrip ==2MiB+101", joined == big)
        await check("sha256 integrity", mb.sha256 == hashlib.sha256(big).hexdigest())

        # 4) stream 指定 start（Range 请求）
        part = b"".join([c async for c in backend.stream("artifacts/t1/big.bin", start=1000)])
        await check("stream start=1000 matches", part == big[1000:])

        # 5) size / fingerprint（head_object ETag 强指纹）
        sz = await backend.size(key)
        await check("size == len", sz == len(data))
        fp = await backend.fingerprint(key)
        await check("fingerprint(etag) present", bool(fp), fp)

        # 6) exists / delete
        await check("exists true", await backend.exists(key))
        await check("delete true", await backend.delete(key) is True)
        await check("exists false after delete", not await backend.exists(key))
        # 注释：S3 语义为幂等删除，不存在对象 delete 仍返回成功（204），
        # 与真实 AWS/MinIO 一致；Local 后端返回 False 属后端特有语义差异。
        await check("delete idempotent (S3 semantics)", await backend.delete(key) is True)

        # 7) list 前缀（排除 _tmp）
        keys = await backend.list("artifacts/")
        await check("list excludes _tmp", all(not k.startswith("artifacts/_tmp/") for k in keys))
        await check("list contains big", "artifacts/t1/big.bin" in keys, keys)

        print(f"\nRESULT: {passed} passed, {failed} failed")
    finally:
        server.stop()

    if failed:
        raise SystemExit(1)
    print("S3 真实协议验证通过")


if __name__ == "__main__":
    asyncio.run(_main())
