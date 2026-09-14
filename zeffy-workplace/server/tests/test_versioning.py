"""P5-1 产物版本管理：版本链/回溯/幂等/淘汰/级联/删除/diff/路径安全/半状态巡检/纯透传。

核心用例（审查🔴/⭐）：
1. 多次 overwrite → 递增版本可回溯
2. 幂等收敛：同内容+同 run_id 不产生新版本；不同 run_id 产生
3. 超上限淘汰（存储+DB 同步）
4. 级联删除（主 key → 版本全清）；删单版本不重排、max 不减小
5. diff 边界：文本统计 / 二进制 not_text / 大文件 too_large
6. 路径安全：非法 task_id 拒绝
7. 半状态巡检：写入失败 → failed 记录可被清理
8. 纯透传：关闭开关不产生版本（行为与 P5-0 一致）
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import repos
from app.db.init_db import init_db
from app.storage import version_sweep_once
from app.storage.base import SecurityError, StorageError
from app.storage.local import LocalBackend
from app.storage.versioning import VersionManager, version_key


@pytest.fixture
async def vm_env(tmp_path):
    """开启版本的 VersionManager + 内存 sqlite session factory。"""
    s = get_settings()
    s.ARTIFACT_VERSIONS_ENABLED = True
    s.ARTIFACT_MAX_VERSIONS = 5
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    vm = VersionManager(LocalBackend(tmp_path), session_factory=factory)
    yield vm, factory
    await eng.dispose()
    s.ARTIFACT_VERSIONS_ENABLED = False
    s.ARTIFACT_MAX_VERSIONS = 5


# ---- 1. 版本链 + 回溯 ----

@pytest.mark.asyncio
async def test_version_chain_and_backtrack(vm_env):
    vm, _ = vm_env
    key = "artifacts/t1/doc.md"
    await vm.put(key, b"v1 content", mode="overwrite", run_id="r1")
    await vm.put(key, b"v2 content", mode="overwrite", run_id="r2")
    await vm.put(key, b"v3 content", mode="overwrite", run_id="r3")

    lst = await vm.list_versions("t1", "doc.md")
    assert lst["total"] == 3
    assert [i["version"] for i in lst["items"]] == [3, 2, 1]  # 倒序

    assert await vm.get_version_bytes("t1", "doc.md", 1) == b"v1 content"
    assert await vm.get_version_bytes("t1", "doc.md", 3) == b"v3 content"
    assert await vm.get_version_bytes("t1", "doc.md", 9) is None  # 不存在

    # 最新 = 主 key
    assert await vm.get(key) == b"v3 content"


# ---- 2. 幂等收敛 ----

@pytest.mark.asyncio
async def test_idempotent_same_content_same_run(vm_env):
    vm, _ = vm_env
    key = "artifacts/t1/doc.md"
    await vm.put(key, b"same", mode="overwrite", run_id="r1")
    await vm.put(key, b"same", mode="overwrite", run_id="r1")  # 重复执行
    lst = await vm.list_versions("t1", "doc.md")
    assert lst["total"] == 1  # 不产生新版本


@pytest.mark.asyncio
async def test_same_content_diff_run_creates_version(vm_env):
    vm, _ = vm_env
    key = "artifacts/t1/doc.md"
    await vm.put(key, b"same", mode="overwrite", run_id="r1")
    await vm.put(key, b"same", mode="overwrite", run_id="r2")
    lst = await vm.list_versions("t1", "doc.md")
    assert lst["total"] == 2


# ---- 3. 超上限淘汰 ----

@pytest.mark.asyncio
async def test_prune_keeps_newest(vm_env):
    vm, _ = vm_env
    s = get_settings()
    s.ARTIFACT_MAX_VERSIONS = 3
    try:
        key = "artifacts/t1/doc.md"
        for i in range(5):
            await vm.put(key, f"c{i}".encode(), mode="overwrite", run_id=f"r{i}")
        lst = await vm.list_versions("t1", "doc.md")
        assert [i["version"] for i in lst["items"]] == [5, 4, 3]
        # 存储对象同步清理：v1/v2 归档 key 已不存在
        assert await vm.get_version_bytes("t1", "doc.md", 1) is None
        assert await vm.get_version_bytes("t1", "doc.md", 5) == b"c4"
    finally:
        s.ARTIFACT_MAX_VERSIONS = 5


# ---- 4. 级联删除 / 单版本删除 ----

@pytest.mark.asyncio
async def test_cascade_delete_main_key(vm_env):
    vm, factory = vm_env
    key = "artifacts/t1/doc.md"
    await vm.put(key, b"a", mode="overwrite", run_id="r1")
    await vm.put(key, b"b", mode="overwrite", run_id="r2")
    assert (await vm.list_versions("t1", "doc.md"))["total"] == 2
    assert await vm.delete(key) is True
    assert (await vm.list_versions("t1", "doc.md"))["total"] == 0
    async with factory() as s:
        assert await repos.all_version_records(s, task_id="t1", rel_path="doc.md") == []


@pytest.mark.asyncio
async def test_delete_single_version_no_renumber(vm_env):
    vm, _ = vm_env
    key = "artifacts/t1/doc.md"
    await vm.put(key, b"a", mode="overwrite", run_id="r1")
    await vm.put(key, b"b", mode="overwrite", run_id="r2")
    await vm.put(key, b"c", mode="overwrite", run_id="r3")
    assert await vm.delete_version("t1", "doc.md", 2) is True
    assert await vm.get_version_bytes("t1", "doc.md", 1) == b"a"
    assert await vm.get_version_bytes("t1", "doc.md", 3) == b"c"
    assert await vm.get_version_bytes("t1", "doc.md", 2) is None  # 缺口保留
    lst = await vm.list_versions("t1", "doc.md")
    assert [i["version"] for i in lst["items"]] == [3, 1]  # 不重排
    assert await vm.delete_version("t1", "doc.md", 2) is False  # 已删


# ---- 5. diff 边界 ----

@pytest.mark.asyncio
async def test_diff_text(vm_env):
    vm, _ = vm_env
    key = "artifacts/t1/note.md"
    await vm.put(key, b"line1\nline2\nline3\n", mode="overwrite", run_id="r1")
    await vm.put(key, b"line1\nline2-new\nline3\nline4\n", mode="overwrite", run_id="r2")
    d = await vm.diff_versions("t1", "note.md", 1, 2)
    assert d["status"] == "ok"
    assert d["added"] == 2 and d["removed"] == 1
    assert any("line2" in ln for ln in d["preview"])


@pytest.mark.asyncio
async def test_diff_binary_not_text(vm_env):
    vm, _ = vm_env
    key = "artifacts/t1/pic.png"
    await vm.put(key, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, mode="overwrite", run_id="r1")
    await vm.put(key, b"\x89PNG\r\n\x1a\n" + b"\x01" * 32, mode="overwrite", run_id="r2")
    d = await vm.diff_versions("t1", "pic.png", 1, 2)
    assert d["status"] == "not_text"


@pytest.mark.asyncio
async def test_diff_too_large(vm_env):
    vm, _ = vm_env
    s = get_settings()
    s.ARTIFACT_DIFF_MAX_SIZE = 64
    try:
        key = "artifacts/t1/big.log"
        await vm.put(key, b"x" * 100, mode="overwrite", run_id="r1")
        await vm.put(key, b"y" * 100, mode="overwrite", run_id="r2")
        d = await vm.diff_versions("t1", "big.log", 1, 2)
        assert d["status"] == "too_large"
    finally:
        s.ARTIFACT_DIFF_MAX_SIZE = 2 * 1024 * 1024


# ---- 6. 路径安全 ----

@pytest.mark.asyncio
async def test_version_key_space_safety():
    assert version_key("t1", "a/b.md", 1) == "artifacts/_v/t1/" + \
        __import__("hashlib").sha1(b"a/b.md").hexdigest()[:12] + "/v1"
    with pytest.raises(SecurityError):
        version_key("../etc", "x.md", 1)


@pytest.mark.asyncio
async def test_put_rejects_invalid_key(vm_env):
    vm, _ = vm_env
    with pytest.raises(SecurityError):
        await vm.put("artifacts/../etc/passwd", b"x", mode="overwrite")


# ---- 7. 半状态巡检（写入失败 → failed 可清理） ----

@pytest.mark.asyncio
async def test_failed_write_swept(vm_env, tmp_path):
    vm, factory = vm_env
    key = "artifacts/t1/doc.md"
    await vm.put(key, b"base", mode="overwrite", run_id="r0")

    class _Broken(LocalBackend):
        name = "local"

        async def put(self, k, d, mode="overwrite", **kw):
            if k.startswith("artifacts/t1/doc.md"):
                raise StorageError("boom")
            return await super().put(k, d, mode=mode, **kw)

    bad = VersionManager(_Broken(tmp_path), session_factory=factory)
    with pytest.raises(StorageError):
        await bad.put(key, b"new", mode="overwrite", run_id="r1")

    async with factory() as s:
        rows = await repos.all_version_records(s, task_id="t1", rel_path="doc.md")
        assert any(r.status == repos.FAILED for r in rows)
    # 巡检清理 failed
    cleaned = await version_sweep_once(factory, bad)
    assert cleaned >= 1
    async with factory() as s:
        rows = await repos.all_version_records(s, task_id="t1", rel_path="doc.md")
        assert all(r.status != repos.FAILED for r in rows)


# ---- 8. 纯透传（关闭开关） ----

@pytest.mark.asyncio
async def test_disabled_passthrough(tmp_path):
    s = get_settings()
    s.ARTIFACT_VERSIONS_ENABLED = False
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    backend = LocalBackend(tmp_path)
    vm = VersionManager(backend, session_factory=factory)
    try:
        key = "artifacts/t1/doc.md"
        meta = await vm.put(key, b"data", mode="overwrite")
        assert meta.backend == "local"
        # 关闭时不产生版本记录
        lst = await vm.list_versions("t1", "doc.md")
        assert lst["total"] == 0
        assert await vm.get(key) == b"data"
    finally:
        await eng.dispose()
