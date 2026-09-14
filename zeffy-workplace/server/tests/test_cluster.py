"""P4-1 集群：实例注册/心跳/注销/ID 唯一/时钟校验 / /admin gate / metrics cluster。

用 fakeredis + 内存 SQLite + ASGI 分别覆盖 Redis 层与 HTTP 层。
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.base import set_global_engine
from app.db.init_db import init_db
from app.main import app
from app.observability import instance_reg
from app.observability import metrics as metrics_mod


@pytest.mark.asyncio
async def test_register_list_unregister():
    r = fakeredis.aioredis.FakeRedis()
    assert await instance_reg.register(r, instance_id="api-1", kind="api", host="h1", ttl=10)
    assert await instance_reg.register(r, instance_id="wk-1", kind="worker", host="h1", ttl=10)
    insts = await instance_reg.list_instances(r)
    kinds = {i["kind"] for i in insts}
    assert kinds == {"api", "worker"}
    await instance_reg.unregister(r, instance_id="api-1")
    assert len(await instance_reg.list_instances(r)) == 1


@pytest.mark.asyncio
async def test_register_id_conflict_rejected():
    """🔴 同一实例 ID 已在线 → 二次注册拒绝（防多进程租约混淆）。"""
    r = fakeredis.aioredis.FakeRedis()
    assert await instance_reg.register(r, instance_id="dup-1", kind="worker", host="h", ttl=10)
    assert not await instance_reg.register(r, instance_id="dup-1", kind="worker", host="h2", ttl=10)


@pytest.mark.asyncio
async def test_clock_skew_ok_and_exceeded():
    class TimeStub:
        def __init__(self, server_sec: float):
            self.server = server_sec

        async def time(self):
            return (int(self.server), 0)

    import time as time_mod
    local = time_mod.time()
    # 偏差在限内 → 返回偏差，不抛
    skew = await instance_reg.check_clock_skew(TimeStub(local), max_skew=5.0)
    assert skew <= 5.0
    # 偏差超限 → InstanceError
    with pytest.raises(instance_reg.InstanceError):
        await instance_reg.check_clock_skew(TimeStub(local + 60), max_skew=5.0)


@pytest.mark.asyncio
async def test_metrics_cluster_counts():
    r = fakeredis.aioredis.FakeRedis()
    await instance_reg.register(r, instance_id="a", kind="api", host="h", ttl=10)
    await instance_reg.register(r, instance_id="w1", kind="worker", host="h", ttl=10)
    await instance_reg.register(r, instance_id="w2", kind="worker", host="h", ttl=10)
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.ext.asyncio import create_async_engine as _cae

    eng = _cae("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    snap = await metrics_mod.collect_metrics(factory, redis=r)
    assert snap["cluster"]["by_kind"] == {"api": 1, "worker": 2}
    await eng.dispose()
    metrics_mod._snapshot.clear()
    metrics_mod._snapshot.update({"collected_at": None, "error": "metrics not collected yet"})


async def _admin_client(**settings_overrides):
    s = get_settings()
    for k, v in settings_overrides.items():
        setattr(s, k, v)
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(eng)
    set_global_engine(eng)
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    await ac.__aenter__()
    return ac, eng


def _reset_admin():
    s = get_settings()
    s.ENABLE_ADMIN = False
    s.AUTH_ENABLED = False


@pytest.mark.asyncio
async def test_admin_cluster_gate_default_off():
    """🔴 ENABLE_ADMIN 默认关 / AUTH off → /admin/cluster 一律 404。"""
    ac, eng = await _admin_client(ENABLE_ADMIN=False, AUTH_ENABLED=False)
    try:
        r = await ac.get("/admin/cluster")
        assert r.status_code == 404
    finally:
        await ac.__aexit__(*([None] * 3))
        await eng.dispose()
        _reset_admin()
