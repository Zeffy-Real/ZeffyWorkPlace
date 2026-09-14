"""WebSocket 端点与会话管理。

P0：单实例内存会话管理（仅演示用，生产不可）。P1 迁移 Redis 管理会话 + 多实例广播。
依赖方向：api/ws 层 → repo 层（可 import repo）；repo 禁止 import api/ws。

P1-1 里程碑：把 echo handler 接上 TaskRunner 后台任务——
WS 协程**只负责接收触发信号 + 推送状态**，不运行长任务（业务长任务放 TaskRunner 后台）。
P1-3：用真实 Agent 分发（AgentRunner + 工具注册表）替换 demo；
入参加 pydantic 校验，非法 kind / 缺失字段直接丢弃并记审计，不透传业务逻辑到 WS 协程。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.agents.runner import AgentRunner, build_agent_runner
from app.api.schemas import WsUserDecision, WsUserMessage
from app.appstate import get_arq_pool, workqueue_enabled
from app.auth.deps import UserPrincipal
from app.config import get_settings
from app.db.base import get_session_factory
from app.queue.events import EventSequencer
from app.queue.gateway import enqueue_resume, enqueue_task
from app.tasks import TaskRunner, get_runner
from app.tools.fs import make_fs_tools
from app.tools.registry import ToolRegistry
from app.wsmessage import WsKind, WsKindLiteral, WsMessage, parse_incoming

logger = logging.getLogger(__name__)

# 活跃连接集合（内存级）：connection_id -> WebSocket
_active_connections: dict[str, WebSocket] = {}

# P2：task_id -> 订阅该任务事件的 WS 连接集（API 进程内存；worker 事件经 Pub/Sub 回传后路由至此）
_task_ws: dict[str, set[WebSocket]] = {}
_task_sequencer = EventSequencer()

# P3-3：WebSocket -> 绑定用户 id（None=匿名/AUTH off）。task_event_handler 据 owner 过滤。
_ws_user: dict[WebSocket, str | None] = {}

# WS 认证握手超时（收到 auth 帧前可等待的最长时间）
AUTH_HANDSHAKE_TIMEOUT = 8.0


async def _push(ws: WebSocket, kind: str, payload: dict, task_id: str | None = None) -> None:
    await ws.send_json(
        WsMessage(kind=cast(WsKindLiteral, kind), payload=payload, task_id=task_id).to_dict()
    )


def _make_registry() -> ToolRegistry:
    """构建并注入 P1-3 工具注册表（fs 白名单）。"""
    from app.config import get_settings

    registry = ToolRegistry()
    for spec in make_fs_tools(get_settings().WORKSPACE_ROOT):
        registry.register(spec)
    return registry


async def _agent_task(meta: dict) -> dict:
    """WS 触发的后台 Agent 任务：独立 DB session 运行 AgentRunner。

    此函数在 TaskRunner 后台协程执行（不在 WS 协程内跑 LLM / DB）。
    进度事件经 meta['emit']（由订阅回调转发到 WS）推送。
    """
    emit: Callable[[str, dict], Awaitable[None]] = meta["emit"]
    registry: ToolRegistry = meta["registry"]
    task_id: str = meta["task_db_id"]
    runner: AgentRunner = build_agent_runner(registry)

    factory = get_session_factory()
    async with factory() as session:
        if meta.get("resume"):
            return await runner.run_resume(session, task_id, meta["resume"]["decision"], emit=emit)
        return await runner.run(session, task_id, emit=emit)


async def _dispatch_handler(conn_id: str, ws: WebSocket, msg: WsMessage) -> None:
    """P1-3/5 分发处理：
    - user_message：提交一个新的 Agent 后台任务（新工作流）。
    - user_decision：对中断任务给出人工决策（审批 / 追问补充），提交 resume 后台任务。
    入参加 pydantic 校验：非法 kind / 缺失字段直接丢弃 + 日志，不透传业务逻辑到 WS 协程。
    长任务走 TaskRunner 后台；WS 只 submit + 订阅推送。
    """
    if msg.kind == WsKind.USER_DECISION.value:
        await _handle_user_decision(conn_id, ws, msg)
        return
    if msg.kind != WsKind.USER_MESSAGE.value:
        await _push(ws, WsKind.SYSTEM_NOTIFY.value,
                    {"error": f"仅接受 user_message / user_decision，收到 {msg.kind}"})
        return

    try:
        incoming = WsUserMessage.model_validate({
            "kind": msg.kind, "payload": msg.payload if isinstance(msg.payload, dict) else {},
            "task_id": msg.task_id, "msg_id": msg.msg_id,
        })
    except ValidationError as exc:
        await _push(ws, WsKind.SYSTEM_NOTIFY.value, {"error": f"入参非法：{exc.errors()}"})
        logger.warning("WS 入参校验失败 conn=%s：%s", conn_id, exc.errors())
        return

    text = incoming.text.strip()
    if not text:
        await _push(ws, WsKind.SYSTEM_NOTIFY.value, {"error": "payload.text 不能为空"})
        return

    # 建任务 + 初始化工作流（默认 lightweight；P1 暂无前端指定 workflow_id）
    factory = get_session_factory()
    async with factory() as session:
        from app.db.repos import create_task, write_audit

        task = await create_task(session, title=text[:120], description=text,
                                 workflow_id="lightweight",
                                 owner_id=_ws_user.get(ws) or None)
        await write_audit(session, task_id=task.id, operator="user",
                          action="ws_submit", detail={"text": text})

    await _submit(ws, task.id, resume=None)


async def _handle_user_decision(conn_id: str, ws: WebSocket, msg: WsMessage) -> None:
    """P1-5：接收人工对中断任务的决策并提交 resume 后台任务。"""
    try:
        incoming = WsUserDecision.model_validate({
            "kind": msg.kind, "payload": msg.payload if isinstance(msg.payload, dict) else {},
            "task_id": msg.task_id, "msg_id": msg.msg_id,
        })
    except ValidationError as exc:
        await _push(ws, WsKind.SYSTEM_NOTIFY.value, {"error": f"入参非法：{exc.errors()}"})
        logger.warning("WS user_decision 校验失败 conn=%s：%s", conn_id, exc.errors())
        return

    payload = incoming.payload or {}
    task_id = payload.get("task_id") or incoming.task_id
    decision = incoming.decision
    if not task_id:
        await _push(ws, WsKind.SYSTEM_NOTIFY.value, {"error": "payload.task_id 缺失"})
        return
    if decision.get("kind") not in {"approval", "answer"}:
        await _push(ws, WsKind.SYSTEM_NOTIFY.value,
                    {"error": "decision.kind 须为 approval 或 answer"})
        return

    # 🔴 越权守卫：鉴权下提交决策须归属本人
    if not await _ws_assert_owner(ws, task_id):
        return

    await _submit(ws, task_id, resume={"decision": decision})


async def _ws_assert_owner(ws: WebSocket, task_id: str) -> bool:
    """AUTH on 且任务 owner 不符 → 拒绝（返回 False）。AUTH off / 系统任务放行。"""
    if not get_settings().AUTH_ENABLED:
        return True
    from app.db import repos

    factory = get_session_factory()
    try:
        async with factory() as session:
            owner = await repos.get_owner_or_none(session, task_id)
    except Exception:  # noqa: BLE001
        owner = None
    uid = _ws_user.get(ws)
    if owner is not None and owner == uid:
        return True
    await _push(ws, WsKind.SYSTEM_NOTIFY.value,
                {"error": "无权对该任务提交决策"}, task_id=task_id)
    return False


def _queue_available() -> bool:
    return workqueue_enabled() and get_arq_pool() is not None


def _subscribe_task_ws(ws: WebSocket, task_id: str) -> None:
    _task_ws.setdefault(task_id, set()).add(ws)


def _unsubscribe_ws(ws: WebSocket) -> None:
    for s in list(_task_ws.values()):
        s.discard(ws)
    _ws_user.pop(ws, None)


class WSRejected(Exception):
    """WS 认证被拒：携带错误码。"""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def _validate_token(token: str) -> UserPrincipal | None:
    """校验 WS token（AUTH on 且 token 合法→UserPrincipal）。"""
    from app.auth import tokens as tok
    from app.db import repos

    if not token.startswith("zwt_"):
        return None
    factory = get_session_factory()
    try:
        async with factory() as session:
            user = await repos.get_user_by_token(session, tok.hash_token(token))
    except Exception as exc:  # noqa: BLE001
        logger.warning("WS token 校验异常：%s", exc)
        return None
    if user is None:
        return None
    return UserPrincipal(id=user.id, username=user.username, is_system=user.is_system)


async def _auth_ws(ws: WebSocket) -> UserPrincipal:
    """连接后认证：AUTH off→匿名；on→优先 query?token= 兜底，否则等 auth 帧（8s）。"""
    if not get_settings().AUTH_ENABLED:
        return UserPrincipal(None)
    query_token = ws.query_params.get("token")
    if query_token:
        user = await _validate_token(query_token)
        if user is not None:
            return user
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_HANDSHAKE_TIMEOUT)
    except (TimeoutError, WebSocketDisconnect, Exception) as exc:  # noqa: BLE001
        raise WSRejected(1008, "认证超时：请先发送 auth 帧携带 token") from exc
    try:
        data = json.loads(raw)
        token = (data.get("payload") or {}).get("token") if isinstance(data, dict) else None
    except ValueError:
        token = None
    if not isinstance(token, str) or not token:
        raise WSRejected(1008, "缺少 auth token")
    user = await _validate_token(token)
    if user is None:
        raise WSRejected(1008, "token 无效或已过期")
    return user


async def task_event_handler(task_id: str, seq: int, kind: str, payload: dict) -> None:
    """API 事件回调：worker 经 Pub/Sub 回传的事件 → 按 task_id 路由到订阅连接。

    🔴 乱序/重复：经 ``EventSequencer`` 按 (task_id, seq) 排序去重；空缺丢弃（前端 REST 对账兜底）。
    🔴 越权过滤（P3-3）：事件按任务 owner 匹配，只推给绑定该 owner 的连接（AUTH off 不过滤）。
    """
    if not _task_sequencer.accept(task_id, seq):
        return
    owner_id = await _resolve_owner(task_id)
    if owner_id:
        payload = {**payload, "owner_id": owner_id}  # 🔴 payload 携带 owner（前端/对账可鉴）
    for ws in list(_task_ws.get(task_id, ())):
        if not _ws_allowed(ws, owner_id):
            continue
        try:
            await _push(ws, kind, payload, task_id=task_id)
        except Exception:  # noqa: BLE001 单连接失败不影响其它
            pass


def _ws_allowed(ws: WebSocket, owner_id: str | None) -> bool:
    """AUTH off → 放行；AUTH on → 连接用户须等于 owner（system 任务仅 system 可见）。"""
    if not get_settings().AUTH_ENABLED:
        return True
    uid = _ws_user.get(ws)
    if uid is None:
        return False
    if owner_id is None:
        return False  # 无主任务（迁移到 system 前）普通用户不可见
    return uid == owner_id


async def _resolve_owner(task_id: str) -> str | None:
    """读任务 owner（事件过滤用）。AUTH off 不费这个查询？仍查一次以保证 payload 携带 owner。"""
    from app.db import repos

    factory = get_session_factory()
    try:
        async with factory() as session:
            return await repos.get_owner_or_none(session, task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("解析任务 owner 失败 task=%s：%s", task_id, exc)
        return None


async def _submit(ws: WebSocket, task_id: str, *, resume: dict | None) -> None:
    """提交任务：P2 走持久队列（enqueue）；USE_QUEUE=false 回退 P1 in-process。"""
    if _queue_available():
        await _submit_queue(ws, task_id, resume=resume)
    else:
        await _submit_inprocess(ws, task_id, resume=resume)


async def _submit_queue(ws: WebSocket, task_id: str, *, resume: dict | None) -> None:
    """P2：DB 优先入队（gateway），后续节点事件经 Pub/Sub→task_event_handler 推送。"""
    pool = get_arq_pool()
    _subscribe_task_ws(ws, task_id)
    factory = get_session_factory()
    try:
        async with factory() as session:
            if resume:
                await enqueue_resume(session, task_id, resume["decision"], pool)
            else:
                from app.db.repos import get_task

                task = await get_task(session, task_id)
                await enqueue_task(session, task, pool)
    except Exception as exc:  # noqa: BLE001 入队失败转前端通知
        logger.warning("队列入队失败 task=%s：%s", task_id, exc)
        await _push(ws, WsKind.SYSTEM_NOTIFY.value,
                    {"error": f"任务提交失败(队列)：{exc}"}, task_id=task_id)
        return
    # 入队确认（审查：前端收到确认才更新 UI）
    await _push(ws, WsKind.TASK_UPDATE.value,
                {"event": "enqueued", "task_db_id": task_id, "queued": True}, task_id=task_id)


async def _submit_inprocess(ws: WebSocket, task_id: str, *, resume: dict | None) -> None:
    """P1 in-process 回退路径（USE_QUEUE=false / 队列未就绪）。"""
    run_id = str(uuid.uuid4())
    runner = get_runner()
    registry = _make_registry()

    task_wrapper: dict = {"task_id": task_id}

    async def progress(r: TaskRunner, rid: str, event: str, payload: dict) -> None:
        if event == "running":
            await _push(ws, WsKind.TASK_UPDATE.value,
                        {"run_id": rid, "event": event, "payload": task_wrapper}, task_id=task_id)
            return
        if event == "done":
            await _push(ws, WsKind.TASK_UPDATE.value,
                        {"run_id": rid, "event": event, "payload": {"task_db_id": task_id,
                                                                    "result": payload.get("result")}},
                        task_id=task_id)
            return
        if event == "failed":
            await _push(ws, WsKind.TASK_UPDATE.value,
                        {"run_id": rid, "event": event, "payload": payload}, task_id=task_id)
            return

    async def emit(kind: str, payload: dict) -> None:
        await _push(ws, kind, payload, task_id=task_id)

    meta: dict = {"emit": emit, "registry": registry}
    if resume:
        meta["resume"] = resume
    runner.subscribe(run_id, progress)
    runner.submit(run_id, _agent_task, task_db_id=task_id, **meta)


# 可插拔 handler：P0 echo → P1-1 demo → P1-3 真实 Agent 分发。
message_handler: Callable[[str, WebSocket, WsMessage], Awaitable[None]] = _dispatch_handler


async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    conn_id = str(uuid.uuid4())
    try:
        # 连接后认证（AUTH on）：校验失败 → 发错误 + 关闭
        user = await _auth_ws(ws)
    except WSRejected as exc:
        await _send_error(ws, exc.reason)
        try:
            await ws.close(code=exc.code)
        except Exception:  # noqa: BLE001
            pass
        return
    _active_connections[conn_id] = ws
    _ws_user[ws] = user.id if user.authenticated else None
    # 认证成功下发 ack（仅鉴权开启时）；AUTH off 前端以 onopen 即视为就绪，保持 P2 无握手消息
    if get_settings().AUTH_ENABLED:
        await _push(ws, WsKind.SYSTEM_NOTIFY.value,
                    {"auth_ok": True, "authenticated": bool(user.authenticated)})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = parse_incoming(raw)
            except ValueError as exc:
                await _send_error(ws, str(exc))
                continue
            await message_handler(conn_id, ws, msg)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        _active_connections.pop(conn_id, None)
        _unsubscribe_ws(ws)


async def _send_error(ws: WebSocket, detail: str) -> None:
    err = WsMessage(
        kind="system_notify",
        payload={"error": detail},
    )
    await ws.send_json(err.to_dict())
