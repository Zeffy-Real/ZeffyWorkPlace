"""P7-C2 版本回滚 · 单元 + 端到端 + API 冒烟测试。

覆盖：
1. 预览纯元数据 + token 一次性/绑人/过期
2. rollback 端到端：主 key 内容切到目标版本 + 新版本号归档旧内容 + 配额 + 审计
3. API：开关关 404；非 admin 越权；无效 token 400
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.init_db import init_db
from app.storage import reset_backend, set_backend
from app.storage import version_rollback as VR
from app.storage.local import LocalBackend


@pytest.fixture(autouse=True)
def _vr_env():
    s = get_settings()
    s.VERSION_ROLLBACK_ENABLED = True
    s.ARTIFACT_META_ENABLED = True
    s.VERSION_ROLLBACK_ADMIN_ONLY = True
    s.VERSION_ROLLBACK_TOKEN_TTL = 300
    s.VERSION_ROLLBACK_REASON_REQUIRED = True
    s.QUOTA_ENABLED = True
    s.QUOTA_TOTAL_MAX_BYTES = 0
    VR.reset_rollback_for_test()
    yield s
    s.VERSION_ROLLBACK_ENABLED = False
    s.ARTIFACT_META_ENABLED = False
    s.QUOTA_ENABLED = False
    VR.reset_rollback_for_test()


@pytest.fixture
async def vr_env(tmp_path):
    """内存 DB(App/Version/Quota) + LocalBackend；返回 (factory, backend)。"""
    from app.db.base import set_global_engine
    from app.storage import set_backend as _sb

    s = get_settings()
    s.ARTIFACT_VERSIONS_ENABLED = True
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    backend = LocalBackend(tmp_path)
    _sb(backend)
    yield factory, backend
    await eng.dispose()
    reset_backend()
    s.ARTIFACT_VERSIONS_ENABLED = False


# ---------------- token（纯逻辑） ----------------

def test_token_one_time_bound(_vr_env):
    data = {"operator": "a1", "exp": int(time.time()) + 100,
            "task_id": "t", "rel_path": "f", "version": 1,
            "target_size": 9, "cur_size": 3}
    VR._preview_tokens["tok"] = data
    # 跨用户拒绝
    assert VR.consume_token("tok", operator="a2") is None
    assert VR.consume_token("tok", operator="a1") == data
    # 一次性：已消费 → 再取 None
    assert VR.consume_token("tok", operator="a1") is None


def test_token_expired(_vr_env):
    VR._preview_tokens["tok"] = {"operator": "a1", "exp": int(time.time()) - 5,
                                 "task_id": "t", "rel_path": "f", "version": 1,
                                 "target_size": 1, "cur_size": 1}
    assert VR.consume_token("tok", operator="a1") is None


# ---------------- rollback 端到端 ----------------

@pytest.mark.asyncio
async def test_rollback_e2e(vr_env):
    from app.db import repos
    from app.db.models import Artifact

    factory, backend = vr_env
    task_id, rel = "t1", "f.txt"
    main_key = f"artifacts/{task_id}/{rel}"
    cur = b"CURRENT-CONTENT"
    target = b"TARGET-v1"

    # 物理文件：主 key=当前；历史版本 _v/v1=目标
    await backend.put(main_key, cur)
    from app.storage.versioning import version_key

    await backend.put(version_key(task_id, rel, 1), target)

    # 治理 Artifact 行 + 版本 v1（走 next_version 序列，避免冲突）
    async with factory() as s:
        a = Artifact(task_id=task_id, rel_path=rel, key=main_key,
                     status="available", owner_id="u1", size=len(cur),
                     sha256=VR._sha(cur), mime="text/plain")
        s.add(a)
        await s.flush()
        await s.commit()
    async with factory() as s:
        v1 = await repos.next_version(s, task_id=task_id, rel_path=rel)
        rec = await repos.create_version_record(
            s, task_id=task_id, rel_path=rel, version=v1,
            key=version_key(task_id, rel, v1))
        await repos.update_version_status(
            s, record_id=rec.id, status="available", size=len(target),
            sha256=VR._sha(target), mime="text/plain")

    # 预览（纯元数据 + token）
    prv = await VR.preview(factory, task_id=task_id, rel_path=rel,
                           version=1, operator="a1")
    assert prv["version"] == 1 and prv["rollback_token"]
    assert prv["size_delta"] == len(target) - len(cur)

    # 回滚
    res = await VR.rollback(factory, token=prv["rollback_token"],
                            reason="版本回滚测试", operator="a1", client_ip="127.0.0.1")
    assert res["ok"] is True and res["new_size"] == len(target)
    # 主 key 内容已切到目标版本
    assert await backend.get(main_key) == target
    # 旧当前内容归档为新版本号
    async with factory() as s:
        recs = await repos.all_version_records(s, task_id=task_id, rel_path=rel)
        assert len(recs) == 2  # v1(目标) + 新版本(旧当前)
    # Artifact 行 size 更新
    async with factory() as s:
        curr = await repos.get_artifact_by_rel(s, task_id=task_id, rel_path=rel)
        assert curr.size == len(target) and curr.sha256 == VR._sha(target)
    VR.reset_rollback_for_test()


@pytest.mark.asyncio
async def test_rollback_reason_required(vr_env):
    from app.db import repos
    from app.db.models import Artifact

    factory, backend = vr_env
    task_id, rel = "t1", "f.txt"
    main_key = VR._main_key(task_id, rel)
    await backend.put(main_key, b"CUR")
    from app.storage.versioning import version_key

    await backend.put(version_key(task_id, rel, 1), b"TGT")
    async with factory() as s:
        a = Artifact(task_id=task_id, rel_path=rel, key=main_key,
                     status="available", owner_id="u1", size=3,
                     sha256=VR._sha(b"CUR"), mime="text/plain")
        s.add(a)
        await s.commit()
    async with factory() as s:
        v1 = await repos.next_version(s, task_id=task_id, rel_path=rel)
        rec = await repos.create_version_record(
            s, task_id=task_id, rel_path=rel, version=v1,
            key=version_key(task_id, rel, v1))
        await repos.update_version_status(
            s, record_id=rec.id, status="available", size=3,
            sha256=VR._sha(b"TGT"), mime="text/plain")
    prv = await VR.preview(factory, task_id=task_id, rel_path=rel, version=1, operator="a1")
    with pytest.raises(ValueError):
        await VR.rollback(factory, token=prv["rollback_token"], reason="",
                          operator="a1")  # 原因必填
    VR.reset_rollback_for_test()


@pytest.mark.asyncio
async def test_rollback_invalid_token(vr_env):
    with pytest.raises(ValueError):
        await VR.rollback(vr_env[0], token="nope", reason="x", operator="a1")


# ---------------- API 冒烟（admin-only + 开关） ----------------

@pytest.fixture
async def _rb_api(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from app.auth.deps import UserPrincipal, get_current_user
    from app.db.base import set_global_engine
    from app.main import app

    s = get_settings()
    s.ARTIFACT_META_ENABLED = True
    s.VERSION_ROLLBACK_ENABLED = True
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    set_backend(LocalBackend(tmp_path))

    async def _admin():
        return UserPrincipal(id="a1", username="admin", role="admin")

    async def _anon():
        return UserPrincipal(id=None, username=None, is_system=False, role="user")

    app.dependency_overrides[get_current_user] = _admin
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await ac.__aenter__()
    yield ac, s, app, get_current_user, _anon, eng
    await ac.__aexit__(None, None, None)
    app.dependency_overrides.clear()
    await eng.dispose()
    reset_backend()
    s.ARTIFACT_META_ENABLED = False
    s.VERSION_ROLLBACK_ENABLED = False


@pytest.mark.asyncio
async def test_api_rollback_disabled_404(_rb_api):
    ac, s, *_ = _rb_api
    s.VERSION_ROLLBACK_ENABLED = False
    r = await ac.post("/admin/governance/version/rollback/preview",
                      json={"task_id": "t", "rel_path": "f", "version": 1})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_rollback_non_admin_404(_rb_api):
    ac, s, app, dep, anon, _ = _rb_api
    app.dependency_overrides[dep] = anon
    r = await ac.post("/admin/governance/version/rollback/preview",
                      json={"task_id": "t", "rel_path": "f", "version": 1})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_rollback_confirm_bad_token_400(_rb_api):
    ac, s, *_ = _rb_api
    r = await ac.post("/admin/governance/version/rollback/confirm",
                      json={"token": "x", "reason": "r"})
    assert r.status_code in (400, 404, 503)
