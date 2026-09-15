"""P5 存储后端：key 逃逸防护 + 三种幂等模式 + 原子写 + 存量兼容 + 流式 + S3 逻辑（mock）。

核心用例（审查🔴/⭐）：
1. 路径安全：`../`、绝对路径、空、特殊字符 → SecurityError
2. 幂等模式一致性：overwrite / no_overwrite / new 行为一致
3. 原子写入：失败不留损坏文件、不覆盖原文件
4. 存量兼容：P4 `task-<id>/` 产物可读（零差异）
5. 大文件流式：MB 级流式读写，内容完整
6. S3 mock：临时 key 原子写 + no_overwrite 拒绝 + 清理
"""

from __future__ import annotations

import pytest

from app.storage.base import FileExistsError_, SecurityError, normalize_artifact_key, tmp_key
from app.storage.local import LocalBackend

# ---- 1. 路径安全（🔴2） ----

@pytest.mark.parametrize("bad", [
    ("..", "x"), ("../etc/passwd", "x"), ("a", "../passwd"),
    ("a", "/etc/passwd"), ("a", ""), ("a/b", "x"), ("", "x"),
    ("a", "x\x00y"), ("a", "_tmp/leak"),
])
def test_normalize_rejects(bad):
    task_id, rel = bad
    with pytest.raises(SecurityError):
        normalize_artifact_key(task_id, rel)


def test_normalize_accepts_valid():
    assert normalize_artifact_key("t1", "dir/report.md") == "artifacts/t1/dir/report.md"
    assert normalize_artifact_key("t1", "a-b_c.d/x") == "artifacts/t1/a-b_c.d/x"


def test_tmp_key_inside_prefix():
    k = tmp_key()
    assert k.startswith("artifacts/_tmp/")
    assert k.endswith(".part")


# ---- 2/3. LocalBackend：幂等模式 + 原子写 ----

@pytest.fixture
async def backend(tmp_path):
    return LocalBackend(tmp_path)


@pytest.mark.asyncio
async def test_local_put_get_roundtrip(backend):
    meta = await backend.put("artifacts/t1/doc.md", b"# Hi", mode="no_overwrite")
    assert meta.key == "artifacts/t1/doc.md"
    assert meta.rel_path == "doc.md"
    assert meta.abs_path  # 🔴3 本地保留真实路径
    assert meta.exists is True
    assert await backend.get("artifacts/t1/doc.md") == b"# Hi"


@pytest.mark.asyncio
async def test_local_no_overwrite_refuses(backend):
    await backend.put("artifacts/t1/a.md", b"v1", mode="no_overwrite")
    with pytest.raises(FileExistsError_):
        await backend.put("artifacts/t1/a.md", b"v2", mode="no_overwrite")
    assert await backend.get("artifacts/t1/a.md") == b"v1"  # 未被覆盖


@pytest.mark.asyncio
async def test_local_new_suffix_unique(backend):
    await backend.put("artifacts/t1/a.md", b"v1", mode="no_overwrite")
    m = await backend.put("artifacts/t1/a.md", b"v2", mode="new", run_id="run-9")
    assert m.key == "artifacts/t1/a-run-9.md"
    assert await backend.get("artifacts/t1/a.md") == b"v1"


@pytest.mark.asyncio
async def test_local_overwrite_explicit(backend):
    await backend.put("artifacts/t1/a.md", b"v1", mode="no_overwrite")
    await backend.put("artifacts/t1/a.md", b"v2", mode="overwrite")
    assert await backend.get("artifacts/t1/a.md") == b"v2"


@pytest.mark.asyncio
async def test_local_no_tmp_left_after_write(backend):
    await backend.put("artifacts/t1/a.md", b"data", mode="overwrite")
    leftovers = list((backend.root / "_tmp").glob("*.tmp")) if (backend.root / "_tmp").exists() else []
    assert leftovers == []


# ---- 4. 存量兼容（🔴3） ----

@pytest.mark.asyncio
async def test_local_legacy_mapping(tmp_path):
    # P4 旧结构：legacy_root/task-t1/doc.md（legacy_root 独立子目录，避免污染共享临时根）
    legacy = tmp_path / "legacy_root"
    old = legacy / "task-t1"
    old.mkdir(parents=True, exist_ok=True)
    (old / "old.md").write_text("legacy", encoding="utf-8")

    backend = LocalBackend(tmp_path, legacy_root=legacy)
    assert await backend.get("artifacts/t1/old.md") == b"legacy"
    assert await backend.exists("artifacts/t1/old.md")
    keys = await backend.list("artifacts/t1")
    assert "artifacts/t1/old.md" in keys


# ---- 5. 大文件流式（⭐2） ----

@pytest.mark.asyncio
async def test_local_stream_large(backend):
    big = b"x" * (2 * 1024 * 1024)  # 2MB
    await backend.put("artifacts/t1/big.bin", big, mode="overwrite")
    chunks = [c async for c in backend.stream("artifacts/t1/big.bin")]
    assert b"".join(chunks) == big


# ---- 删除 + 健康 ----

@pytest.mark.asyncio
async def test_local_delete_and_health(backend):
    await backend.put("artifacts/t1/a.md", b"v1", mode="no_overwrite")
    assert await backend.delete("artifacts/t1/a.md") is True
    assert await backend.delete("artifacts/t1/a.md") is False
    h = await backend.health()
    assert h["ok"] is True and h["backend"] == "local"


# ---- 6. S3 mock（逻辑路径；不依赖真实 MinIO） ----

class _NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self, n: int = -1):
        if n < 0:
            d, self._data = self._data, b""
            return d
        d, self._data = self._data[:n], self._data[n:]
        return d


class _FakeClient:
    """最小 S3 client 桩：内存对象存储。"""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.exceptions = type("Exc", (), {"NoSuchKey": _NoSuchKey, "ClientError": RuntimeError})

    async def put_object(self, *, Bucket, Key, Body=None):
        self.store[Key] = bytes(Body or b"")
        return {}

    async def copy_object(self, *, Bucket, Key, CopySource):
        src = CopySource["Key"]
        if src not in self.store:
            raise _NoSuchKey
        self.store[Key] = self.store[src]
        return {"CopyObjectResult": {}}

    async def delete_object(self, *, Bucket, Key):
        self.store.pop(Key, None)
        return {}

    async def head_object(self, *, Bucket, Key):
        if Key not in self.store:
            raise _NoSuchKey
        return {"ContentLength": len(self.store[Key]), "ETag": f'"{hash(Key)}"'}

    async def get_object(self, *, Bucket, Key, Range=None):
        if Key not in self.store:
            raise _NoSuchKey
        data = self.store[Key]
        if Range:
            # Range: bytes=N- → 从偏移切片
            m = __import__("re").match(r"bytes=(\d+)-", Range)
            if m:
                data = data[int(m.group(1)):]
        import hashlib
        return {"Body": _Body(data),
                "ETag": f'"{hashlib.md5(self.store[Key]).hexdigest()}"'}

    async def create_multipart_upload(self, *, Bucket, Key):
        return {"UploadId": "u1"}

    async def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body):
        return {"ETag": f'"part-{PartNumber}"'}

    async def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload):
        return {}

    async def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        return {}

    def get_paginator(self, name):
        class _P:
            def __init__(self, store):
                self.store = store

            async def paginate(self, *, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in self.store if k.startswith(Prefix)]}
        return _P(self.store)

    async def head_bucket(self, *, Bucket):
        return {}


@pytest.fixture
async def s3(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.storage.s3 import S3Backend

    s = get_settings()
    s.S3_ENDPOINT = "http://minio:9000"
    s.S3_BUCKET = "zeffy"
    backend = S3Backend(s)
    fake = _FakeClient()

    async def _client():
        return fake

    monkeypatch.setattr(backend, "_get_client", _client)
    return backend


@pytest.mark.asyncio
async def test_s3_put_get_atomic_no_tmp(s3):
    m = await s3.put("artifacts/t1/doc.md", b"# Hi", mode="no_overwrite")
    assert m.key == "artifacts/t1/doc.md"
    assert m.abs_path is None  # 🔴3 S3 无本地路径
    assert m.url is None  # 未配 public_base
    assert await s3.get("artifacts/t1/doc.md") == b"# Hi"
    # 原子写临时 key 已清理
    keys = await s3.list("artifacts/")
    assert all(not k.startswith("artifacts/_tmp/") for k in keys)


@pytest.mark.asyncio
async def test_s3_no_overwrite_refuses(s3):
    await s3.put("artifacts/t1/a.md", b"v1", mode="no_overwrite")
    with pytest.raises(FileExistsError_):
        await s3.put("artifacts/t1/a.md", b"v2", mode="no_overwrite")
    assert await s3.get("artifacts/t1/a.md") == b"v1"


@pytest.mark.asyncio
async def test_s3_new_suffix(s3):
    m = await s3.put("artifacts/t1/a.md", b"v2", mode="new", run_id="r9")
    assert m.key == "artifacts/t1/a-r9.md"


@pytest.mark.asyncio
async def test_s3_stream(s3):
    big = b"y" * (2 * 1024 * 1024)
    await s3.put("artifacts/t1/big.bin", big, mode="overwrite")
    chunks = [c async for c in s3.stream("artifacts/t1/big.bin")]
    assert b"".join(chunks) == big


@pytest.mark.asyncio
async def test_s3_delete_and_health(s3):
    await s3.put("artifacts/t1/a.md", b"v1", mode="no_overwrite")
    assert await s3.delete("artifacts/t1/a.md") is True
    h = await s3.health()
    assert h["ok"] is True and h["backend"] == "s3"
