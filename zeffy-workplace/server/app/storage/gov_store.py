"""P6-4-B 灰度中心化 · Redis 权威存储层。

把运行时覆盖 + 灰度名单的真相源中心化到 Redis；本地判热路径仍读进程内缓存
（见 ``governance.py`` 的 ``_ovr``/``_gray``），本模块负责权威读写、版本号乐观锁、
pub/sub 广播、降级状态机与配置快照。

**读热路径约定**：本模块只在上层显式调用（写、重连全量、定期校验、失效重载）时访问
Redis，不参与灰度判热的每次判定——保证中心化模式零每判定往返。

**降级状态机**：``degraded`` 置位后，读仍可用（调用方保留本地缓存），写直接拒绝抛
``GovConfigUnavailable``；任何成功的 Redis 操作清除 degraded（自愈）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)


class GovConfigUnavailable(Exception):
    """配置服务不可用（Redis 故障 / 降级写拒绝）。HTTP 503 语义。"""


def _now_naive() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


class RedisGovStore:
    """灰度中心化的 Redis 权威存储（键统一前缀 ``gov:{env}:``，环境隔离）。"""

    def __init__(self, redis: Any, env: str, channel: str) -> None:
        self._redis = redis
        self._env = env
        self._channel = channel
        self._prefix = f"gov:{env}:"
        self.degraded = False
        self.degraded_since: float | None = None
        self.last_sync_ts: str | None = None
        self.sync_from = time.monotonic()

    # ---- 键 ----
    def _ovr_key(self, feature: str) -> str:
        return f"{self._prefix}ovr:{feature}"

    def _ovr_ver_key(self, feature: str) -> str:
        return f"{self._prefix}ovr:{feature}:ver"

    def _gray_key(self, feature: str) -> str:
        return f"{self._prefix}gray:{feature}"

    def _gray_ver_key(self, feature: str) -> str:
        return f"{self._prefix}gray:{feature}:ver"

    def _snap_key(self, ts: str) -> str:
        return f"{self._prefix}snapshot:{ts}"

    # ---- 降级 ----
    def mark_degraded(self) -> None:
        if not self.degraded:
            self.degraded = True
            self.degraded_since = time.monotonic()
            logger.warning("灰度配置 Redis 降级：拒绝写，读保留本地缓存")

    def mark_ok(self) -> None:
        if self.degraded:
            self.degraded = False
            self.degraded_since = None
            logger.info("灰度配置 Redis 恢复")

    def _guard_write(self) -> None:
        if self.degraded:
            raise GovConfigUnavailable("配置服务不可用（Redis 降级中）")

    # ---- 覆盖（乐观锁：incr 版本 + 写后回读校验 + 重试） ----
    async def write_override(self, feature: str, val: bool,
                             retries: int = 3) -> dict:
        """写覆盖，返回 {ver, prev_val}。并发覆盖通过版本回读校验 + 重试兜底。

        区分两类失败：连接/IO 错误 → 降级；版本冲突（回读 v 不符）→ 重试不降级。
        """
        self._guard_write()
        prev_val = await self._read_override_no_degrade(feature)
        conflict = False
        for _ in range(max(0, retries)):
            try:
                ver = int(await self._redis.incr(self._ovr_ver_key(feature)))
                await self._redis.set(
                    self._ovr_key(feature), json.dumps({"v": ver, "val": val}))
                got = await self._redis.get(self._ovr_key(feature))
                if got is not None and json.loads(got).get("v") == ver:
                    await self._publish("ovr", feature, ver)
                    self.mark_ok()
                    return {"ver": ver, "prev_val": prev_val}
                conflict = True
                continue  # 回读 v 与本次不符 → 被并发覆盖，重试
            except GovConfigUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 连接/IO → 降级，不重试风暴
                self.mark_degraded()
                raise GovConfigUnavailable(f"配置写入失败：{exc}") from exc
        raise GovConfigUnavailable(
            "配置写入冲突次数超限" if conflict else "配置写入未确认") from None

    async def _read_override_no_degrade(self, feature: str) -> bool | None:
        try:
            got = await self._redis.get(self._ovr_key(feature))
        except Exception:  # noqa: BLE001
            return None
        if not got:
            return None
        try:
            return bool(json.loads(got).get("val"))
        except (ValueError, TypeError):
            return None

    async def load_override(self, feature: str) -> tuple[int | None, bool | None]:
        """读某覆盖权威值 → (ver, val|None)。"""
        got = await self._redis.get(self._ovr_key(feature))
        if not got:
            return None, None
        try:
            d = json.loads(got)
            return int(d.get("v")), bool(d.get("val"))
        except (ValueError, TypeError):
            return None, None

    # ---- 灰度（Set 幂等 + 版本） ----
    async def gray_apply(self, feature: str, add: list[str],
                         remove: list[str]) -> dict:
        self._guard_write()
        try:
            pipe = self._redis.pipeline()
            for oid in add:
                pipe.sadd(self._gray_key(feature), oid)
            for oid in remove:
                pipe.srem(self._gray_key(feature), oid)
            await pipe.execute()
            ver = int(await self._redis.incr(self._gray_ver_key(feature)))
            members, _ = await self.load_gray_members(feature)
            await self._publish("gray", feature, ver)
            self.mark_ok()
            return {"ver": ver, "members": members}
        except GovConfigUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            self.mark_degraded()
            raise GovConfigUnavailable(f"灰度写入失败：{exc}") from exc

    async def load_gray_members(self, feature: str) -> tuple[int | None, list[str]]:
        ver_raw = await self._redis.get(self._gray_ver_key(feature))
        ver = int(ver_raw) if ver_raw else None
        members_raw = await self._redis.smembers(self._gray_key(feature))
        members = sorted(
            [m.decode() if isinstance(m, bytes) else str(m) for m in members_raw or []])
        return ver, members

    # ---- 全量（重连 / 定期校验用） ----
    async def load_all(self) -> dict[str, Any]:
        """拉取全部覆盖 + 灰度（{ovr:{feat:[ver,val]}, gray:{feat:[ver,list]}}）。"""
        keys = await self._redis.keys(f"{self._prefix}ovr:*")
        # 排除独立 :ver 键（不以其为权威值来源）
        feat_ver = ":ver"
        out_ovr: dict[str, list] = {}
        for k in (keys or []):
            ks = k.decode() if isinstance(k, bytes) else str(k)
            plain = ks[:-len(feat_ver)] if ks.endswith(feat_ver) else ks
            if ks.endswith(feat_ver):
                continue  # 版本键只作 incr 用，值来源为 ovr:key 本体
            feat = plain.rsplit(":", 1)[-1]
            ver, val = await self.load_override(feat)
            if ver is not None or val is not None:
                out_ovr[feat] = [ver, val]
        gkeys = await self._redis.keys(f"{self._prefix}gray:*")
        out_gray: dict[str, list] = {}
        for k in (gkeys or []):
            ks = k.decode() if isinstance(k, bytes) else str(k)
            if ks.endswith(feat_ver):
                continue
            feat = ks.rsplit(":", 1)[-1]
            ver, members = await self.load_gray_members(feat)
            out_gray[feat] = [ver, members]
        self.last_sync_ts = _now_naive()
        self.mark_ok()
        return {"ovr": out_ovr, "gray": out_gray}

    # ---- 广播 ----
    async def _publish(self, kind: str, feature: str, ver: int) -> None:
        try:
            await self._redis.publish(
                self._channel,
                json.dumps({"type": kind, "key": feature, "ver": ver}))
        except Exception:  # noqa: BLE001 广播失败不影响已落盘（靠重连全量兜底）
            logger.warning("灰度配置广播失败 kind=%s feat=%s", kind, feature)

    # ---- 快照（⭐5：变更前存盘，供回滚） ----
    async def snapshot(self, snapshot: dict[str, Any]) -> str:
        """把当前全量配置存为一次性快照，返回快照 id（不含此前缀）。"""
        snap_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        await self._redis.set(self._snap_key(snap_id), json.dumps(snapshot),
                              ex=max(60, get_settings().GOV_SNAPSHOT_RETENTION))
        return snap_id

    async def list_snapshots(self) -> list[str]:
        keys = await self._redis.keys(f"{self._prefix}snapshot:*")
        ids = []
        for k in (keys or []):
            ks = k.decode() if isinstance(k, bytes) else str(k)
            ids.append(ks.rsplit(":", 1)[-1])
        return sorted(ids, reverse=True)

    async def load_snapshot(self, key: str) -> dict[str, Any]:
        got = await self._redis.get(self._snap_key(key))
        if not got:
            raise GovConfigUnavailable(f"快照不存在：{key}")
        try:
            return json.loads(got)
        except (ValueError, TypeError) as exc:
            raise GovConfigUnavailable(f"快照损坏：{key}") from exc
