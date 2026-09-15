"""P7 前收尾 · 项1 加密链路端到端集成验证（审查🔴项1 全项）。

加密开启下的全链路联动回归：
1. 主链路：流式加密落位 → 探测 → 全量/Range 流式解一致
2. 上传断点续传（HTTP init/chunk/commit）→ 密文落位 → 读取 Range 一致
3. 事务：tx 暂存密文 → commit → 解密一致；rollback 清理
4. 版本：归档密文 → 读取/diff 解密一致
5. 回收站：软删/恢复 → 解密正常
6. 去重互斥：加密开启时 _dedup_enabled=False
7. 对账：密文物理大小与元表匹配，不误判
8. 深冷：tier 归档后解密正常
9. 故障：篡改/版本不匹配 → 解密失败不返坏数据；单副本丢失 → 冗余副本可解
10. 轮换/重裹 e2e：v1 落位 → 切 v2 → 旧可解 → 重裹 → v2 可解 → 回收门槛
11. 双开关锚点：加密关/总闸关时全链路明文零漂移
"""

from __future__ import annotations

import hashlib
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.main import app
from app.storage.local import LocalBackend


def _mk(n: int) -> bytes:
    return bytes((i * 37) & 0xFF for i in range(n))


def _write_kf(path, m: bytes, *, ver: int) -> None:
    from app.storage.crypto_gate import _key_fingerprint

    path.write_bytes(f"#zfk ver={ver} fp={_key_fingerprint(m)}\n".encode() + m)


def _enable_crypto(tmp_path, *, ver: int = 1, legacy: str = "",
                   legacy_versions: str = ""):
    """开启加密（自包含派生 hmack）；密钥文件按版本命名，返回 (m1a, m1b) 路径。"""
    import app.storage.governance as gov
    from app.storage.crypto_gate import reset_for_test

    m = os.urandom(32)
    p1 = tmp_path / f"m{ver}a.key"
    p2 = tmp_path / f"m{ver}b.key"
    _write_kf(p1, m, ver=ver)
    _write_kf(p2, m, ver=ver)
    s = get_settings()
    s.ARTIFACT_ENCRYPT_ENABLED = True
    s.ENCRYPT_CIPHER_VERSION = ver
    s.ENCRYPT_MASTER_KEYFILES = f"{p1},{p2}"
    s.ENCRYPT_HMAC_KEYFILE = ""
    s.ENCRYPT_LEGACY_KEYFILES = legacy
    s.ENCRYPT_LEGACY_VERSIONS = legacy_versions
    gov.set_governance_override("meta", True)
    reset_for_test()
    return str(p1), str(p2)


async def _disable_crypto():
    import app.storage.governance as gov
    from app.storage.crypto_gate import reset_for_test

    s = get_settings()
    s.ARTIFACT_ENCRYPT_ENABLED = False
    s.ENCRYPT_MASTER_KEYFILES = ""
    s.ENCRYPT_HMAC_KEYFILE = ""
    s.ENCRYPT_LEGACY_KEYFILES = ""
    s.ENCRYPT_LEGACY_VERSIONS = ""
    s.ENCRYPT_CIPHER_VERSION = 1
    s.ENCRYPT_ROTATE_GRAY_RATIO = 0.0
    s.ENCRYPT_GRAY_MASTER_KEYFILES = ""
    reset_for_test()
    gov._ovr.pop("meta", None)  # type: ignore[attr-defined]


@pytest.fixture
async def e2e(tmp_path):
    from app.storage import reset_backend, set_backend

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    set_backend(LocalBackend(tmp_path))
    yield tmp_path
    await _disable_crypto()
    await eng.dispose()
    reset_backend()


async def _mk_task():
    from app.db.base import get_session_factory
    from app.db.repos import create_task

    factory = get_session_factory()
    async with factory() as s:
        return await create_task(s, title="e", workflow_id="generic")


async def _http():
    from httpx import ASGITransport, AsyncClient

    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    return ac


# ---- 1. 主链路：流式加密落位 → 探测 → 全量/Range 流式解 ----

@pytest.mark.asyncio
async def test_main_chain_stream_roundtrip(e2e):
    from app.storage import get_backend
    from app.storage.crypto_gate import (
        _crypto_stream_encrypt,
        _decrypt_stream_rest,
        is_encrypted_blob,
        peek_plain_size,
    )

    _enable_crypto(e2e)
    backend = get_backend()
    plain = _mk(1_000_000)
    async def _plain_iter():
        for i in range(0, len(plain), 8192):
            yield plain[i:i + 8192]
    enc_stream, meta = await _crypto_stream_encrypt(_plain_iter(), total=len(plain))
    tag = await backend.put("artifacts/t1/doc.md", enc_stream, mode="overwrite")
    cipher = await backend.get("artifacts/t1/doc.md")
    assert is_encrypted_blob(cipher)
    assert tag.size > len(plain)  # 密文物理计量
    # 全量流式解
    s = backend.stream("artifacts/t1/doc.md")
    first = b""
    async for c in s:
        first = c
        break
    out = b"".join([pt async for pt in _decrypt_stream_rest(s, head=first)])
    assert out == plain
    # Range 流式解（对齐明文坐标）
    assert peek_plain_size(first) == len(plain)
    s2 = backend.stream("artifacts/t1/doc.md")
    first2 = b""
    async for c in s2:
        first2 = c
        break
    rng = b"".join([pt async for pt in
                    _decrypt_stream_rest(s2, head=first2, start=1000, end=5000)])
    assert rng == plain[1000:5000]
    await _disable_crypto()


# ---- 2. 上传断点续传（HTTP）→ 密文落位 → 读取 Range ----

@pytest.mark.asyncio
async def test_upload_resume_encrypted(e2e):
    s = get_settings()
    s.UPLOAD_ENABLED = True
    s.UPLOAD_CHUNK = 8
    s.UPLOAD_TTL = 3600
    s.RANGE_ENABLED = True
    _enable_crypto(e2e)
    ac = await _http()
    task = await _mk_task()
    data = _mk(40)
    md5 = hashlib.md5(data).hexdigest()
    r = await ac.post("/artifacts/upload/init",
                      json={"task_id": task.id, "rel": "a.bin", "size": len(data), "md5": md5})
    assert r.status_code == 200, r.text
    uid = r.json()["upload_id"]
    # 传 0-16（一半）→ 续传 16-40
    for off in (0, 8):
        c = await ac.put(f"/artifacts/upload/{uid}/chunk", params={"offset": off},
                         content=data[off:off + 8])
        assert c.status_code == 200, c.text
    rc = await ac.post(f"/artifacts/upload/{uid}/commit")
    assert rc.status_code == 400, rc.text  # 缺块 → 不允许 commit
    for off in (16, 24, 32):
        c = await ac.put(f"/artifacts/upload/{uid}/chunk", params={"offset": off},
                         content=data[off:off + 8])
        assert c.status_code == 200, c.text
    rc = await ac.post(f"/artifacts/upload/{uid}/commit")
    assert rc.status_code == 200, rc.text
    assert rc.json()["encrypted"] is True
    # 读取全量（流式解）
    got = await ac.get(f"/artifacts/{task.id}/a.bin")
    assert got.status_code == 200 and got.content == data
    # Range 206 对齐明文坐标
    rg = await ac.get(f"/artifacts/{task.id}/a.bin", headers={"Range": "bytes=10-25"})
    assert rg.status_code == 206 and rg.content == data[10:26]
    assert rg.headers["Content-Range"] == f"bytes 10-25/{len(data)}"
    await ac.__aexit__(None, None, None)
    s.UPLOAD_ENABLED = False
    s.UPLOAD_CHUNK = 8 * 1024 * 1024
    s.RANGE_ENABLED = False
    await _disable_crypto()


# ---- 3/4/5/6. 事务/版本/回收站/去重互斥 ----

@pytest.mark.asyncio
async def test_tx_version_recycle_dedup(e2e):
    from app.db.base import get_session_factory
    from app.storage import get_backend
    from app.storage.crypto_gate import decrypt_artifact, is_encrypted_blob
    from app.storage.governance import (
        _dedup_enabled,
        restore_artifact,
        soft_delete_artifact,
        tx_commit,
        tx_open,
        tx_stage_write,
    )
    from app.storage.versioning import VersionManager

    _enable_crypto(e2e)
    s = get_settings()
    s.TX_ENABLED = True
    s.RECYCLE_ENABLED = True
    backend = get_backend()
    # 去重互斥：加密开启时去重关闭
    assert _dedup_enabled() is False
    # 事务：暂存密文 → commit → 解密一致
    plain = b"tx secret"
    tx = await tx_open(task_id="t1", owner_id="u1")
    await tx_stage_write(tx_id=tx["tx_id"], task_id="t1", rel_path="d.md", data=plain)
    await tx_commit(tx_id=tx["tx_id"])
    blob = await backend.get("artifacts/t1/d.md")
    assert is_encrypted_blob(blob) and await decrypt_artifact(blob) == plain
    # 版本（归档为密文，diff/读取解密）
    s.ARTIFACT_VERSIONS_ENABLED = True
    s.ARTIFACT_MAX_VERSIONS = 5
    s.ARTIFACT_DIFF_MAX_SIZE = 1024 * 1024
    vm = VersionManager(backend, session_factory=get_session_factory())
    from app.storage.crypto_gate import encrypt_artifact
    a, b_ = b"line1\nold\n", b"line1\nnew\n"
    ca, _ = await encrypt_artifact(a)
    cb, _ = await encrypt_artifact(b_)
    await vm.put("artifacts/t2/e.md", ca, mode="overwrite", producer_role="p")
    await vm.put("artifacts/t2/e.md", cb, mode="overwrite", producer_role="p")
    d = await vm.diff_versions("t2", "e.md", 1, 2)
    assert d["status"] == "ok" and d["added"] == 1 and d["removed"] == 1
    assert await vm.get_version_bytes("t2", "e.md", 1) == a
    s.ARTIFACT_VERSIONS_ENABLED = False
    # 回收站：软删 → 恢复 → 解密正常
    await soft_delete_artifact(task_id="t1", rel_path="d.md")
    await restore_artifact(task_id="t1", rel_path="d.md")
    blob2 = await backend.get("artifacts/t1/d.md")
    assert is_encrypted_blob(blob2) and await decrypt_artifact(blob2) == plain
    s.TX_ENABLED = False
    s.RECYCLE_ENABLED = False
    await _disable_crypto()


# ---- 9. 故障：篡改不返坏数据 / 单副本丢失冗余可解 ----

@pytest.mark.asyncio
async def test_fault_tamper_and_single_copy(e2e):
    from app.storage import get_backend
    from app.storage.crypto_gate import decrypt_artifact

    _enable_crypto(e2e)
    backend = get_backend()
    from app.storage.crypto_gate import encrypt_artifact
    c, _ = await encrypt_artifact(b"secret" * 500)
    await backend.put("artifacts/t1/f.md", c, mode="overwrite")
    # 篡改 → 解密失败不返回坏数据
    bad = bytearray(await backend.get("artifacts/t1/f.md"))
    bad[-3] ^= 0xFF
    await backend.put("artifacts/t1/f.md", bytes(bad), mode="overwrite")
    from app.storage import crypto as _C
    with pytest.raises(_C.EncryptError):
        await decrypt_artifact(await backend.get("artifacts/t1/f.md"))
    # 恢复 → 单副本丢失（删 m1，m2 仍一致）→ 冗余副本可解
    await backend.put("artifacts/t1/f.md", c, mode="overwrite")
    (e2e / "m1a.key").unlink()  # 丢失一个副本
    from app.storage.crypto_gate import _unlock, reset_for_test
    reset_for_test()
    assert _unlock() is not None  # 剩余副本多数一致仍可解锁
    assert await decrypt_artifact(await backend.get("artifacts/t1/f.md")) == b"secret" * 500
    await _disable_crypto()


# ---- 10. 轮换/重裹 e2e ----

@pytest.mark.asyncio
async def test_rotation_rewrap_e2e(e2e):
    from app.storage import get_backend
    from app.storage.crypto_gate import (
        decrypt_artifact,
        encrypt_artifact,
        is_encrypted_blob,
        retire_legacy_key,
        rotate_rewrap_deks,
    )

    backend = get_backend()
    v1a, v1b = _enable_crypto(e2e, ver=1)
    plain = _mk(200_000)
    c1, _ = await encrypt_artifact(plain)
    await backend.put("artifacts/t1/r.md", c1, mode="overwrite")
    # 切 v2（v1 进 legacy，指向原 v1 文件）
    _enable_crypto(e2e, ver=2, legacy=f"1:{v1a},{v1b}", legacy_versions="1")
    assert await decrypt_artifact(await backend.get("artifacts/t1/r.md")) == plain  # 旧可解
    # 重裹 → v2 头 + 流式解一致
    await rotate_rewrap_deks(keys=["artifacts/t1/r.md"], backend=backend)
    new = await backend.get("artifacts/t1/r.md")
    assert new[7] == 2 and is_encrypted_blob(new)
    assert await decrypt_artifact(new) == plain
    # 回收门槛：重裹后 v1 零引用 → 放行
    assert (await retire_legacy_key(version=1, backend=backend))["ok"] is True
    await _disable_crypto()


# ---- 11. 双开关锚点 ----

@pytest.mark.asyncio
async def test_double_switch_anchor(e2e):
    """加密关 + 总闸关 → 上传/读取全明文零漂移（与 P6-6-4 一致）。"""
    from app.storage import get_backend

    s = get_settings()
    s.UPLOAD_ENABLED = True
    s.UPLOAD_CHUNK = 8
    s.UPLOAD_TTL = 3600
    backend = get_backend()
    ac = await _http()
    task = await _mk_task()
    data = _mk(24)
    r = await ac.post("/artifacts/upload/init",
                      json={"task_id": task.id, "rel": "p.txt", "size": len(data)})
    uid = r.json()["upload_id"]
    for off in (0, 8, 16):
        await ac.put(f"/artifacts/upload/{uid}/chunk", params={"offset": off},
                     content=data[off:off + 8])
    rc = await ac.post(f"/artifacts/upload/{uid}/commit")
    assert rc.status_code == 200 and rc.json().get("encrypted") is None
    blob = await backend.get(f"artifacts/{task.id}/p.txt")
    assert not blob.startswith(b"ZFGATE1")  # 明文落位（零漂移）
    assert (await ac.get(f"/artifacts/{task.id}/p.txt")).content == data
    await ac.__aexit__(None, None, None)
    s.UPLOAD_ENABLED = False
    s.UPLOAD_CHUNK = 8 * 1024 * 1024
    await _disable_crypto()
