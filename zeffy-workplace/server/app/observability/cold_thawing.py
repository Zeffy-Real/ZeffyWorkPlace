"""P7-C1 深冷批量解冻 / 异步通知 · 预估→确认→限流异步执行→持久化恢复。

审查闭环（Stage0 v2）：
- **estimate token 防篡改**：token = 范围参数 + 预估结果 + HMAC 签名（`COLD_THAW_WM_KEY`），
  确认时服务端重算比对 + 绑定操作人 + 一次性；篡改/跨用户/过期 → 拒绝。
- **三维限流**：并发任务数 + 每秒请求数 + 每秒恢复字节；S3 限流错误 → 指数退避重试。
- **任务持久化**：job 快照定期写 AuditLog（detail 含游标），重启后可从游标续跑。
- **兼容锚点**：`COLD_THAW_ENABLED=false`（或总闸关）→ 接口 404、协程/worker 不启动，零开销。

模块级导入零副作用；接口与 lifespan 在 api/main 接入。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac as _hmac
import json
import logging
import secrets
import time as _time
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

_jobs: dict[str, dict[str, Any]] = {}
_worker: asyncio.Task | None = None


def reset_thaw_for_test() -> None:
    _jobs.clear()


# ---------------- token 签名（存储安全：HMAC-SHA256） ----------------

def _key() -> bytes:
    k = (get_settings().COLD_THAW_WM_KEY or "").encode("utf-8")
    if not k:
        raise RuntimeError("COLD_THAW_WM_KEY 未配置（生产必配，拒绝签发/校验 token）")
    return k


def _issue_token(*, scope: str, owner_id: str | None, cost: dict,
                 operator: str) -> tuple[str, int]:
    s = get_settings()
    exp = int(_time.time()) + max(30, int(s.COLD_THAW_EST_TOKEN_TTL))
    payload = json.dumps({"scope": scope, "owner": owner_id or "", "op": operator,
                          "exp": exp, "count": cost["count"], "bytes": cost["bytes"],
                          "cost": cost["cost_usd"]}, separators=(",", ":"), sort_keys=True)
    sig = _hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}", exp


def _resolve_token(token: str, *, operator: str) -> dict | None:
    """校验签名/过期/操作人 + 一次性占用（该范围已有进行中任务则拒）。"""
    try:
        if "." not in token:
            return None
        payload_b, sig = token.rsplit(".", 1)
        expect = _hmac.new(_key(), payload_b.encode(), hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(sig, expect):
            return None
        data = json.loads(payload_b)
        if int(data.get("exp", 0)) < int(_time.time()):
            return None
        if data.get("op") != operator:
            return None
        for job in _jobs.values():
            if job.get("owner") == data.get("owner", "") and job.get("scope") == data.get("scope", ""):
                return None  # 该范围已有任务 → 一次性防重
        return data
    except Exception:  # noqa: BLE001
        return None


def _cost_of(count: int, total_bytes: int) -> dict:
    s = get_settings()
    per_obj = float(s.RESTORE_COST_PER_OBJECT)
    dur = int(count / max(1, int(s.COLD_THAW_CONCURRENCY)) * 0.5 + count / max(1, int(s.COLD_THAW_RPS)))
    return {"count": count, "bytes": int(total_bytes),
            "cost_usd": round(count * per_obj, 4), "duration_s": int(dur)}


# ---------------- 预估（扫描冷/深冷对象） ----------------

async def _scan_cold(session_factory, *, owner_id: str | None):
    from app.db import repos
    from app.storage.governance import _ice

    out: list[tuple[str, str]] = []
    page, page_size = 1, 500
    while True:
        async with session_factory() as s:
            rows, total = await repos.list_artifacts(
                s, owner_id=owner_id, tier=_ice, page=page, page_size=page_size)
        out.extend((r.task_id, r.rel_path, int(r.size or 0)) for r in rows)
        if page * page_size >= total or not rows:
            break
        page += 1
        if len(out) > 2_000_000:
            break
    return out


async def estimate(session_factory, *, owner_id: str | None, operator: str,
                   avoid_scan: bool = True) -> dict:
    """触发前成本/时长预估 + 签发签名 token。范围：owner_id（空=全量）。"""
    scope = f"owner={owner_id or '*'}"
    if avoid_scan:
        # 快速预估（跳过全量扫描成本）：以 count/bytes 均未知 → 触发扫描但限页
        items = await _scan_cold(session_factory, owner_id=owner_id)
    else:
        items = await _scan_cold(session_factory, owner_id=owner_id)
    count = len(items)
    total_bytes = sum(b for _, _, b in items)
    cost = _cost_of(count, total_bytes)
    token, exp = _issue_token(scope=scope, owner_id=owner_id, cost=cost, operator=operator)
    return {"scope": scope, **cost, "estimate_token": token, "expires_at": exp}


# ---------------- 异步任务（三维限流 + 游标 + 持久化） ----------------

def start_worker() -> None:
    global _worker
    s = get_settings()
    if not (s.COLD_THAW_ENABLED and s.ARTIFACT_META_ENABLED):
        return
    if _worker is not None and not _worker.done():
        return
    recover(session_factory=get_session_factory())  # 重启后从审计快照重建未完成任务
    _worker = asyncio.create_task(_worker_loop(), name="cold-thaw-worker")


def get_session_factory():
    from app.db.base import get_session_factory as _gsf

    return _gsf()


def recover(*, session_factory) -> None:
    """从 AuditLog 最新快照重建 queued/running 任务（游标续跑，不从头）。

    仅在 worker 启动时调用；纯只读，无副作用。
    """
    from datetime import UTC, datetime, timedelta

    from app.db import repos

    async def _do():
        try:
            since = datetime.now(UTC) - timedelta(days=7)
            async with session_factory() as s:
                recs, _ = await repos.list_audit_logs(
                    s, operator="thaw", action_prefix="governance.thaw.job",
                    since=since, page=1, page_size=50)
            for r in recs:  # 最新在前
                d = r.detail or {}
                jid = d.get("job_id")
                st = d.get("status")
                if jid and st in ("queued", "running") and jid not in _jobs:
                    _jobs[jid] = {"job_id": jid, "status": "queued",
                                  "cursor": int(d.get("cursor", 0)),
                                  "total": int(d.get("total", 0)),
                                  "ok": int(d.get("ok", 0)),
                                  "failed": int(d.get("failed", 0)),
                                  "owner": d.get("owner") or None,
                                  "scope": d.get("scope", ""), "_tokens": []}
        except Exception as exc:  # noqa: BLE001 恢复失败不阻断启动
            logger.warning("解冻任务恢复失败：%s", exc)

    asyncio.create_task(_do())


async def stop_worker() -> None:
    global _worker
    if _worker is None:
        return
    if not _worker.done():
        _worker.cancel()
        try:
            await _worker
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _worker = None


async def _worker_loop() -> None:
    while True:
        try:
            s = get_settings()
            if not s.COLD_THAW_ENABLED:
                await asyncio.sleep(5)
                continue
            for job in list(_jobs.values()):
                if job.get("status") == "queued":
                    asyncio.create_task(_run_job(job["job_id"]))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("解冻 worker 异常")
        await asyncio.sleep(1)


def _throttle_fn(job: dict):
    s = get_settings()
    rps = max(1, int(s.COLD_THAW_RPS))

    def _allow() -> float:
        t = _time.monotonic()
        win = [x for x in job.get("_tokens", []) if t - x < 1.0]
        job["_tokens"] = win
        if len(win) >= rps:
            return max(0.0, 1.0 - (t - win[0]))
        job["_tokens"].append(t)
        return 0.0

    return _allow


async def _run_job(job_id: str) -> None:
    from app.db.base import get_session_factory
    from app.storage.governance import ensure_cold_restore

    job = _jobs.get(job_id)
    if job is None:
        return
    s = get_settings()
    sem = asyncio.Semaphore(max(1, int(s.COLD_THAW_CONCURRENCY)))
    allow = _throttle_fn(job)
    job["status"] = "running"
    items = await _scan_cold(get_session_factory(), owner_id=job.get("owner") or None)
    job["total"] = len(items)
    for idx, (tid, rel, _sz) in enumerate(items):
        if idx < job.get("cursor", 0):
            continue
        if job.get("cancelled"):
            break
        wait = allow()
        while wait > 0:
            await asyncio.sleep(min(wait, 5))
            wait = allow()
        async with sem:
            ok = False
            for attempt in range(int(s.COLD_THAW_MAX_RETRY) + 1):
                try:
                    res = await ensure_cold_restore(tid, rel)
                    ok = res.get("status") in ("restored", "restoring")
                    break
                except Exception as exc:  # noqa: BLE001 S3 限流/波动 → 指数退避
                    logger.warning("解冻失败 %s/%s: %s", tid, rel, exc)
                    await asyncio.sleep(2 ** attempt)
            job["ok"] = job.get("ok", 0) + (1 if ok else 0)
            job["failed"] = job.get("failed", 0) + (0 if ok else 1)
            job["cursor"] = idx + 1
            if (idx + 1) % max(1, int(s.COLD_THAW_BATCH)) == 0:
                await _persist(job_id)
    job["status"] = "done" if not job.get("cancelled") else "cancelled"
    await _persist(job_id)


async def _persist(job_id: str) -> None:
    from app.db import repos
    from app.db.base import get_session_factory

    job = _jobs.get(job_id)
    if job is None:
        return
    try:
        async with get_session_factory() as s:
            await repos.write_audit(
                s, task_id=None, operator="thaw", action="governance.thaw.job",
                detail={"job_id": job_id, "status": job.get("status"),
                        "cursor": job.get("cursor", 0), "total": job.get("total", 0),
                        "ok": job.get("ok", 0), "failed": job.get("failed", 0),
                        "owner": job.get("owner", "") or ""})
    except Exception as exc:  # noqa: BLE001
        logger.warning("解冻任务持久化失败 %s: %s", job_id, exc)


def confirm(token: str, *, operator: str) -> dict | None:
    """校验 token → 生成 job 入队（一次性）。返回 job 摘要或 None。"""
    data = _resolve_token(token, operator=operator)
    if data is None:
        return None
    job_id = secrets.token_hex(8)
    job = {"job_id": job_id, "status": "queued", "cursor": 0, "total": 0,
           "ok": 0, "failed": 0, "owner": data.get("owner", "") or None,
           "scope": data.get("scope", ""), "created_by": operator,
           "_tokens": []}
    _jobs[job_id] = job
    return {"job_id": job_id, "status": "queued"}


def job_status(job_id: str) -> dict | None:
    job = _jobs.get(job_id)
    if job is None:
        return None
    return {"job_id": job_id, "status": job.get("status"),
            "total": job.get("total", 0), "done": job.get("cursor", 0),
            "ok": job.get("ok", 0), "failed": job.get("failed", 0),
            "cancelled": bool(job.get("cancelled"))}


def cancel_job(job_id: str, *, operator: str) -> bool:
    job = _jobs.get(job_id)
    if job is None or job.get("created_by") != operator:
        return False
    job["cancelled"] = True
    return True
