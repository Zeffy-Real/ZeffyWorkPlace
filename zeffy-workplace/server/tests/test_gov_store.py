"""P6-4-B 灰度中心化测试。

用 fakeredis 验证：
- 多实例一致性：共享同一 Redis 的两个 store / 两个 governance 实例，写 A → B 读到一致；
- 版本号乐观锁：重复写版本递增、回读校验；
- 降级状态机：Redis 故障写拒绝(GovConfigUnavailable)、读保留本地缓存、恢复自愈；
- 环境隔离：不同 GOV_ENV 不串写；
- 快照：打快照 / 列出 / 恢复；
- 兼容锚点：GOV_CENTRALIZE=false 时远程写零漂移回进程内。
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.config import get_settings
from app.storage.gov_store import GovConfigUnavailable, RedisGovStore


@pytest.fixture
def _gov_central():
    """重置 governance 中心化全局（store/缓存），开启 GOV_CENTRALIZE。"""
    from app.storage import governance as govm

    saved = (govm._gov_store, dict(govm._ovr), dict(govm._gray),
             dict(govm._ovr_ver), dict(govm._gray_ver))
    govm._gov_store = None
    govm._ovr.clear()
    govm._gray.clear()
    govm._ovr_ver.clear()
    govm._gray_ver.clear()
    yield govm
    govm._gov_store = saved[0]
    govm._ovr.clear()
    govm._ovr.update(saved[1])
    govm._gray.clear()
    govm._gray.update(saved[2])
    govm._ovr_ver.clear()
    govm._ovr_ver.update(saved[3])
    govm._gray_ver.clear()
    govm._gray_ver.update(saved[4])


def _make_store(redis, env="test"):
    return RedisGovStore(redis, env=env, channel="gov:test:pub")


# ---- 多实例一致 + 版本 ----
@pytest.mark.asyncio
async def test_multi_instance_shared_store(_gov_central):
    redis = fakeredis.aioredis.FakeRedis()
    a, b = _make_store(redis), _make_store(redis)
    r1 = await a.write_override("quota", False)
    assert r1["ver"] >= 1 and r1["prev_val"] is None
    ver_b, val_b = await b.load_override("quota")
    assert val_b is False and int(ver_b or 0) >= 1
    # 灰度：A 写 → B 读到一致；Set 幂等合并
    await a.gray_apply("dedup", ["u1", "u2"], [])
    v, m = await b.load_gray_members("dedup")
    assert m == ["u1", "u2"]
    await a.gray_apply("dedup", ["u1"], [])  # 幂等重复
    v2, m2 = await b.load_gray_members("dedup")
    assert m2 == ["u1", "u2"]
    # 版本递增
    await a.write_override("quota", True)
    ver2, _ = await b.load_override("quota")
    assert int(ver2 or 0) > r1["ver"]


@pytest.mark.asyncio
async def test_env_isolation(_gov_central):
    redis = fakeredis.aioredis.FakeRedis()
    a, b = _make_store(redis, env="test"), _make_store(redis, env="prod")
    await a.write_override("quota", False)
    ver_p, val_p = await b.load_override("quota")
    assert val_p is None and ver_p is None  # 互不串写


# ---- 降级状态机 ----
@pytest.mark.asyncio
async def test_degrade_write_reject_keep_read(_gov_central):
    from app.storage import governance as govm

    redis = fakeredis.aioredis.FakeRedis()
    store = _make_store(redis)
    s = get_settings()
    s.GOV_CENTRALIZE = True
    govm._set_gov_store_for_test(store)
    r = await govm.set_governance_override_remote("quota", False)
    assert r["centralized"] is True
    assert govm._ovr.get("quota") is False

    # 模拟 Redis 故障：标记降级 → 写拒绝 503
    store.mark_degraded()
    with pytest.raises(GovConfigUnavailable):
        await govm.set_governance_override_remote("quota", True)
    # 读保留本地缓存（不回退默认，不回源）
    assert govm._ovr.get("quota") is False
    st = govm.governance_status()
    assert st["degraded"] is True and st["centralized"] is True
    # 恢复：写入成功 → 自愈
    store.mark_ok()
    s.GOV_CENTRALIZE = True
    res = await govm.set_governance_override_remote("quota", True)
    assert res["centralized"] is True and govm._ovr.get("quota") is True
    s.GOV_CENTRALIZE = False


# ---- 快照 ----
@pytest.mark.asyncio
async def test_snapshot_roundtrip(_gov_central):
    redis = fakeredis.aioredis.FakeRedis()
    store = _make_store(redis)
    await store.write_override("quota", False)
    await store.gray_apply("dedup", ["u1"], [])
    snap_key = await store.snapshot(await store.load_all())
    assert snap_key
    snap_id = snap_key.rsplit(":", 1)[-1]
    assert snap_id in await store.list_snapshots()
    restored = await store.load_snapshot(snap_key)
    assert restored["ovr"]["quota"][1] is False
    assert restored["gray"]["dedup"][1] == ["u1"]
    with pytest.raises(GovConfigUnavailable):
        await store.load_snapshot("nonexistent")


# ---- 兼容锚点：关中心化零漂移 ----
@pytest.mark.asyncio
async def test_anchor_centralize_off(_gov_central):
    from app.storage import governance as govm

    s = get_settings()
    s.GOV_CENTRALIZE = False
    govm._set_gov_store_for_test(None)
    r = await govm.set_governance_override_remote("quota", False)
    assert r["centralized"] is False  # 走进程内
    assert govm._ovr.get("quota") is False
    st = govm.governance_status()
    assert st["centralized"] is False and st["single_instance_only"] is True


# ---- 全量拉取写入本地缓存（重连/定期校验路径） ----
@pytest.mark.asyncio
async def test_load_all_into_cache(_gov_central):
    from app.storage import governance as govm

    redis = fakeredis.aioredis.FakeRedis()
    store = _make_store(redis)
    await store.write_override("quota", True)
    await store.gray_apply("dedup", ["u1"], [])
    s = get_settings()
    s.GOV_CENTRALIZE = True
    govm._set_gov_store_for_test(store)
    # 模拟一个全新实例：缓存空 → 全量同步填充
    govm._ovr.clear()
    govm._gray.clear()
    res = await govm.gov_load_all_into_cache()
    assert res["loaded"] is True
    assert govm._ovr.get("quota") is True
    assert govm._gray.get("dedup") == {"u1"}
    s.GOV_CENTRALIZE = False
