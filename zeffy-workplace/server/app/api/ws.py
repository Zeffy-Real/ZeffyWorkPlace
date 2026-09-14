"""WebSocket 端点与会话管理。

P0：单实例内存会话管理（仅演示用，生产不可）。P1 迁移 Redis 管理会话 + 多实例广播。
依赖方向：api/ws 层 → repo 层（可 import repo）；repo 禁止 import api/ws。

P1-1 里程碑：把 echo handler 接上 TaskRunner 后台任务——
WS 协程**只负责接收触发信号 + 推送状态**，不运行长任务（业务长任务放 TaskRunner 后台）。
P1-3：用真实 Agent 分发（AgentRunner + 工具注册表）替换 demo；
入参加 pydantic 校验，非法 kind / 缺失字段直接丢弃并记审计，不透传业务逻辑到 WS 协程。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.api.schemas import WsUserMessage
from app.agents.runner import AgentRunner, build_agent_runner
from app.db.base import get_session_factory
from app.tasks import TaskRunner, get_runner
from app.tools.registry import ToolRegistry
from app.tools.fs import make_fs_tools
from app.wsmessage import WsKind, WsKindLiteral, WsMessage, parse_incoming

logger = logging.getLogger(__name__)

# 活跃连接集合（内存级）：connection_id -> WebSocket
_active_connections: dict[str, WebSocket] = {}


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
        return await runner.run(session, task_id, emit=emit)


async def _dispatch_handler(conn_id: str, ws: WebSocket, msg: WsMessage) -> None:
    """P1-3 分发处理：把合法 user_message 提交为一个 Agent 后台任务。

    - 入参加 pydantic 校验：非法 kind / 缺失 text 字段直接丢弃 + 审计，不跑业务。
    - 长任务走 TaskRunner 后台；WS 只 submit + 订阅推送（task_node_update / agent_message）。
    """
    if msg.kind != WsKind.USER_MESSAGE.value:
        await _push(ws, WsKind.SYSTEM_NOTIFY.value,
                    {"error": f"P1-3 仅接受 user_message，收到 {msg.kind}"})
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
                                 workflow_id="lightweight")
        await write_audit(session, task_id=task.id, operator="user",
                          action="ws_submit", detail={"text": text})

    # 后台提交
    run_id = str(uuid.uuid4())
    runner = get_runner()
    registry = _make_registry()

    task_wrapper: dict = {"task_id": task.id}

    async def progress(r: TaskRunner, rid: str, event: str, payload: dict) -> None:
        # runner.run 返回的最终结果经 done 事件携带；中间事件走 emit 直推。
        if event == "running":
            await _push(ws, WsKind.TASK_UPDATE.value,
                        {"run_id": rid, "event": event, "payload": task_wrapper}, task_id=task.id)
            return
        if event == "done":
            await _push(ws, WsKind.TASK_UPDATE.value,
                        {"run_id": rid, "event": event, "payload": {"task_db_id": task.id,
                                                                    "result": payload.get("result")}},
                        task_id=task.id)
            return
        if event == "failed":
            await _push(ws, WsKind.TASK_UPDATE.value,
                        {"run_id": rid, "event": event, "payload": payload}, task_id=task.id)
            return

    async def emit(kind: str, payload: dict) -> None:
        # 把 AgentRunner 内部事件直接映射为 WS 下发 kind
        await _push(ws, kind, payload, task_id=task.id)

    runner.subscribe(run_id, progress)
    runner.submit(run_id, _agent_task, task_db_id=task.id, emit=emit, registry=registry)


# 可插拔 handler：P0 echo → P1-1 demo → P1-3 真实 Agent 分发。
message_handler: Callable[[str, WebSocket, WsMessage], Awaitable[None]] = _dispatch_handler


async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    conn_id = str(uuid.uuid4())
    _active_connections[conn_id] = ws
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


async def _send_error(ws: WebSocket, detail: str) -> None:
    err = WsMessage(
        kind="system_notify",
        payload={"error": detail},
    )
    await ws.send_json(err.to_dict())
