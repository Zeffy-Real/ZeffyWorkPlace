"""HTTP API 请求/响应 Pydantic schema（不裸 dict）。P1 在此扩展字段。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=10000)
    workflow_id: str = Field(default="generic")


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str
    workflow_id: str
    status: str
    created_at: datetime


class TaskListOut(BaseModel):
    items: list[TaskOut]
    total: int


# --- P1-2 调试接口（仅本地开发；P1-5 后须走网关开关禁用） -------------------

class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    node_name: str
    status: str
    input: dict | None = None
    output: dict | None = None
    error: str | None = None
    created_at: datetime


class NodeListOut(BaseModel):
    items: list[NodeOut]
    total: int


class AdvanceRequest(BaseModel):
    node_id: str
    result: dict | None = Field(default=None)


# ---


class HealthOut(BaseModel):
    status: str
    db: bool
    version: str


class ErrorOut(BaseModel):
    detail: str


# --- P1-3 WS 入参校验（禁止裸 dict 透传业务逻辑） -----------------------------

class WsIncoming(BaseModel):
    """WS 上行消息的通用校验骨架：kind 白名单 + payload 结构。"""

    kind: str = Field(min_length=1)
    msg_id: str | None = None
    task_id: str | None = None
    payload: dict | None = Field(default_factory=dict)


class WsUserMessage(WsIncoming):
    """合法的业务上行：user_message。kind 单词校验后由 ws 层二次核对。"""

    kind: Literal["user_message"]
    payload: dict = Field(..., description="须含 text 字段")

    @property
    def text(self) -> str:
        v = (self.payload or {}).get("text", "")
        return v if isinstance(v, str) else ""
