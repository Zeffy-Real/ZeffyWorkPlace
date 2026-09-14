"""P4-2 告警外部通知：异步队列投递 + 内容白名单 + 风暴抑制 + 重试退避。

审查🔴：
- **异步化不阻塞监控**：monitor tick 只把事件 enqueue，独立 worker 消费发送；失败捕获不回抛。
- **内容白名单（安全红线）**：payload 仅 指标/级别/状态/阈值/实例id/时间，禁止任何业务/用户/配置数据。
- **风暴抑制**：触发与恢复各自独立冷却（Redis 键按 metric+state）+ 单通道每分钟上限，超限写「告警风暴」审计。
- 失败 3 次指数退避后写 ``notify_failed`` 审计，当天不再重试（由 rate/冷却兜底防止刷频）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

LEVELS = ("warning", "critical")


@dataclass
class NotificationEvent:
    metric: str
    level: str = "warning"  # warning | critical
    state: str = "triggered"  # triggered | resolved
    threshold: str = ""
    instance_id: str = ""
    triggered_at: str = ""


def build_payload(ev: NotificationEvent) -> dict:
    """🔴 内容白名单：仅允许以下字段，禁止混入业务/用户/内部配置。"""
    return {
        "type": "zw.alert",
        "metric": ev.metric,
        "level": ev.level,
        "state": ev.state,
        "threshold": ev.threshold,
        "instance_id": ev.instance_id,
        "triggered_at": ev.triggered_at,
    }


# ---------------------------------------------------------------------------
# Notifier 抽象 + 实现
# ---------------------------------------------------------------------------


class Notifier:
    async def send(self, payload: dict) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


class WebhookNotifier(Notifier):
    def __init__(self, url: str) -> None:
        self.url = url

    async def send(self, payload: dict) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(self.url, json=payload)
            r.raise_for_status()


def _smtp_send_sync(cfg: dict, subject: str, body: str) -> None:
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(cfg["to"])
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15) as s:
        s.login(cfg["username"], cfg["password"])
        s.send_message(msg)


class SmtpNotifier(Notifier):
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    async def send(self, payload: dict) -> None:
        subject = f"[Zeffy] {payload['level']} {payload['metric']} {payload['state']}"
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        await asyncio.to_thread(_smtp_send_sync, self.cfg, subject, body)


def build_notifiers() -> dict[str, list[Notifier]]:
    """据配置构造 {channel: [notifiers]}；无配置默认空（零通知）。"""
    from app.config import get_settings

    s = get_settings()
    ns: dict[str, list[Notifier]] = {}
    if s.NOTIFY_WEBHOOK_URLS:
        ns["webhook"] = [WebhookNotifier(u) for u in s.NOTIFY_WEBHOOK_URLS]
    if s.SMTP_HOST and s.SMTP_TO:
        ns["mail"] = [SmtpNotifier({
            "host": s.SMTP_HOST, "port": s.SMTP_PORT,
            "username": s.SMTP_USERNAME, "password": s.SMTP_PASSWORD,
            "from": s.SMTP_FROM or s.SMTP_USERNAME, "to": list(s.SMTP_TO),
        })]
    return ns


def rules_for(metric: str, level: str) -> list[str]:
    """NOTIFY_RULES 命中 → 通道列表。无规则不通知。"""
    from app.config import get_settings

    return [r.get("channel") for r in get_settings().NOTIFY_RULES
            if r.get("metric") == metric and r.get("level") == level]


# ---------------------------------------------------------------------------
# 核心投递：冷却 + 风暴 + 重试（纯函数式，便于测试）
# ---------------------------------------------------------------------------

_COOLDOWN_KEY = "zw:ncooldown"
_RATE_KEY = "zw:nrate"


async def _cooldowned(redis: Any, metric: str, state: str) -> bool:
    """冷却期内返回 True（跳过）。触发与恢复各自独立键。"""
    key = f"{_COOLDOWN_KEY}:{metric}:{state}"
    from app.config import get_settings

    ttl = max(1, get_settings().ALERT_COOLDOWN)
    try:
        return not bool(await redis.set(key, "1", nx=True, ex=ttl))
    except Exception:  # noqa: BLE001 无 redis 退化为放行一次
        return False


async def _rate_ok(redis: Any, channel: str) -> bool:
    """单通道每分钟上限（风暴抑制）；超限 False + 风暴审计。"""
    from app.config import get_settings

    cap = max(1, get_settings().NOTIFY_MAX_PER_MIN)
    minute = int(time.time() // 60)
    key = f"{_RATE_KEY}:{channel}:{minute}"
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, 120)
        return int(n) <= cap
    except Exception:  # noqa: BLE001
        return True


async def _audit(session_factory, *, operator: str, action: str, detail: dict) -> None:
    try:
        from app.db import repos

        async with session_factory() as s:
            await repos.write_audit(s, task_id=None, operator=operator,
                                    action=action, detail=detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("通知审计落库失败：%s", exc)


async def dispatch_one(ev: NotificationEvent, notifiers: dict[str, list[Notifier]],
                       redis: Any | None = None, session_factory: Any | None = None) -> dict:
    """投递一条通知事件；返回探测结果（供测试断言）。不抛、不阻塞调用方。"""
    channels = rules_for(ev.metric, ev.level)
    if not channels:
        return {"sent": 0, "skipped_cooldown": 0, "skipped_rate": 0, "failed": 0}
    payload = build_payload(ev)
    out = {"sent": 0, "skipped_cooldown": 0, "skipped_rate": 0, "failed": 0}

    for ch in channels:
        # 冷却（触发/恢复各自独立）
        if redis is not None and await _cooldowned(redis, ev.metric, ev.state):
            out["skipped_cooldown"] += 1
            continue
        # 风暴：单通道每分钟上限
        if redis is not None and not await _rate_ok(redis, ch):
            out["skipped_rate"] += 1
            if session_factory is not None:
                await _audit(session_factory, operator="notify",
                             action="notify_storm", detail={"channel": ch, "metric": ev.metric})
            continue
        for nf in notifiers.get(ch, []):
            if await _send_with_retry(nf, payload, session_factory, ev):
                out["sent"] += 1
            else:
                out["failed"] += 1
    return out


def _backoff_delay(attempt: int) -> float:
    return min(getattr(_sett, "NOTIFY_BACKOFF_BASE", 2.0) ** attempt, 30)


def _sett():
    from app.config import get_settings

    return get_settings()


async def _send_with_retry(nf: Notifier, payload: dict, session_factory: Any,
                           ev: NotificationEvent) -> bool:
    retries = max(0, _sett().NOTIFY_RETRIES)
    for attempt in range(retries + 1):
        try:
            await nf.send(payload)
            return True
        except Exception as exc:  # noqa: BLE001 失败重试；不回抛
            logger.warning("通知发送失败(第%d次) metric=%s：%s", attempt + 1, ev.metric, exc)
            if attempt < retries:
                await asyncio.sleep(_backoff_delay(attempt))
    if session_factory is not None:
        await _audit(session_factory, operator="notify", action="notify_failed",
                     detail={"metric": ev.metric, "state": ev.state, "error": "已重试用尽"})
    return False


# ---------------------------------------------------------------------------
# 异步队列：monitor 生产 → 独立 worker 消费发送（不阻塞监控主循环）
# ---------------------------------------------------------------------------


class NotifyDispatcher:
    def __init__(self, notifiers: dict[str, list[Notifier]] | None = None,
                 redis: Any | None = None, session_factory: Any | None = None) -> None:
        self.q: asyncio.Queue[NotificationEvent] = asyncio.Queue()
        self.notifiers = notifiers or build_notifiers()
        self.redis = redis
        self.session_factory = session_factory
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001
                await self._task
            self._task = None

    async def enqueue(self, ev: NotificationEvent) -> None:
        await self.q.put(ev)

    async def _run(self) -> None:
        while True:
            try:
                ev = await self.q.get()
                await dispatch_one(ev, self.notifiers, redis=self.redis,
                                   session_factory=self.session_factory)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 单条失败不终止 worker
                logger.warning("通知投递异常", exc_info=True)


# 模块级单例（lifespan 启停）
_dispatcher: NotifyDispatcher | None = None


def get_dispatcher() -> NotifyDispatcher | None:
    return _dispatcher


def configure_dispatcher(redis: Any = None, session_factory: Any = None) -> NotifyDispatcher:
    """初始化/重配全局 dispatcher（lifespan 调用；幂等）。"""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = NotifyDispatcher(redis=redis, session_factory=session_factory)
    else:
        _dispatcher.redis = redis or _dispatcher.redis
        _dispatcher.session_factory = session_factory or _dispatcher.session_factory
    return _dispatcher


async def enqueue_alert_events(events: list[dict], *, level_map: dict[str, str] | None = None) -> None:
    """把 run_monitor_tick 返回的触发/恢复事件转为通知事件入队。AUTH 无关，仅在有配置时生效。"""
    d = _dispatcher
    if d is None:
        return
    for e in events:
        metric = e.get("metric", "")
        level = (level_map or {}).get(metric, "warning")
        ev = NotificationEvent(metric=metric, level=level, state=e.get("state", "triggered"),
                               instance_id=_get_instance_id())
        await d.enqueue(ev)


def _get_instance_id() -> str:
    try:
        from app.config import get_settings

        return get_settings().instance_id
    except Exception:  # noqa: BLE001
        return ""


async def stop_dispatcher() -> None:
    global _dispatcher
    if _dispatcher is not None:
        await _dispatcher.stop()
        _dispatcher = None
