"""P1-3 WebSocket 分发测试：
- 未知 kind → system_notify（沿用，无需 DB）。
- 合法 user_message 但空 text → 入参校验拒绝（不投递）。
- 合法 user_message → 建任务并投递 AgentRunner 后台任务（提交契约）。

WS 协程不跑 LLM/DB；长任务由 TaskRunner 后台执行，这里校验「触发→提交」解耦。
"""

import json

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import ws as ws_mod
from app.api.schemas import WsUserMessage
from app.api.ws import websocket_endpoint
from app.db.init_db import init_db


@pytest.fixture
def ws_client():
    tmp = FastAPI()

    @tmp.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket_endpoint(websocket)

    return TestClient(tmp)


def test_ws_rejects_unknown_kind(ws_client):
    with ws_client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"kind": "bogus", "payload": "x"}))
        raw = json.loads(ws.receive_text())
        assert raw["kind"] == "system_notify"


def test_ws_rejects_empty_text(ws_client):
    """user_message 但 text 为空 → system_notify（由 _dispatch_handler 校验拦截）。"""
    with ws_client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"kind": "user_message", "payload": {"text": "  "}}))
        raw = json.loads(ws.receive_text())
        assert raw["kind"] == "system_notify"
        assert "不能为空" in (raw.get("payload", {}).get("error", "") if isinstance(raw["payload"], dict) else "")


async def test_ws_dispatch_valid_message_submits_agent_task(monkeypatch):
    """合法消息 → 建任务 + 投递（校验 WS→AgentRunner 触发契约）。"""
    # 内存 sqlite 注入为全局 session factory
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await init_db(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ws_mod, "get_session_factory", lambda: factory)

    received = {}

    class FakeRunner:
        def subscribe(self, run_id, cb):
            received["run_id"] = run_id
            received["cb"] = cb

        def submit(self, run_id, fn, *, task_db_id=None, **meta):
            received["fn"] = fn
            received["task_db_id"] = task_db_id
            received["meta"] = meta
            received["submitted"] = True

        def active_count(self):
            return 0

    monkeypatch.setattr(ws_mod, "get_runner", lambda: FakeRunner())
    monkeypatch.setattr(ws_mod, "_make_registry", lambda: ws_mod.ToolRegistry())

    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send_json(self, data):
            self.sent.append(data)

    fw = FakeWS()
    await ws_mod._dispatch_handler("c1", fw, ws_mod.parse_incoming(
        json.dumps({"kind": "user_message", "payload": {"text": "帮我写个报告"}})))

    assert received.get("submitted")
    assert received["task_db_id"]
    assert received["run_id"]
    # 后台任务含 emit（转 WS 下发）与 registry
    assert "emit" in received["meta"]
    await engine.dispose()


def test_wsuser_message_parses_text():
    m = WsUserMessage.model_validate({"kind": "user_message", "payload": {"text": "hello"}})
    assert m.text == "hello"